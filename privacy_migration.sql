CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS private;

-- ═══════════════════════════════════════════════════════════════════════════
-- CabildoOS — MIGRACIÓN DE PRIVACIDAD CRIPTOGRÁFICA
-- Rompe la cadena email → butaca → voto usando HMAC con salts secretos.
-- Ningún JOIN en la BD puede reconstruir esa cadena sin conocer los salts.
-- ═══════════════════════════════════════════════════════════════════════════


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 1: FUNCIONES PRIVADAS CON SALTS INCORPORADOS
-- Los salts están hardcodeados aquí dentro — no en el código ni en env vars.
-- Para verlos hay que tener acceso total a la BD (mismo nivel de riesgo).
-- ═══════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION private.my_seat_token()
RETURNS TEXT LANGUAGE sql SECURITY DEFINER SET search_path = extensions, public AS $$
  SELECT encode(
    hmac(auth.uid()::text::bytea, 'ce107e09132b2578ec23407a083c90f772cb45f9cb882b163a6fe9a556cf5010'::bytea, 'sha256'),
    'hex'
  );
$$;

CREATE OR REPLACE FUNCTION private.my_voter_token()
RETURNS TEXT LANGUAGE sql SECURITY DEFINER SET search_path = extensions, public AS $$
  SELECT encode(
    hmac(auth.uid()::text::bytea, '87b1c8ade7e11b84fba65d8224788548b79647c7d89bddd9d87cfe2999b2044d'::bytea, 'sha256'),
    'hex'
  );
$$;


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 2: TABLA seat_identities
-- Reemplaza profiles.butaca_numero.
-- Keyed por seat_token (opaco) — jamás almacena user_id.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS seat_identities (
  seat_token    TEXT PRIMARY KEY,
  butaca_numero INT  UNIQUE NOT NULL,
  alias         TEXT,
  phrase        TEXT,
  is_public     BOOLEAN NOT NULL DEFAULT false,
  votes_count   INT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION update_seat_identities_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;
DROP TRIGGER IF EXISTS seat_identities_updated_at ON seat_identities;
CREATE TRIGGER seat_identities_updated_at
  BEFORE UPDATE ON seat_identities
  FOR EACH ROW EXECUTE FUNCTION update_seat_identities_updated_at();

ALTER TABLE seat_identities ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own_seat_identity" ON seat_identities
  FOR ALL TO authenticated
  USING (seat_token = private.my_seat_token());

CREATE POLICY "public_read_public_seats" ON seat_identities
  FOR SELECT TO anon, authenticated
  USING (is_public = true);


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 3: MIGRAR DATOS EXISTENTES de profiles a seat_identities
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO seat_identities (seat_token, butaca_numero, alias, phrase, is_public)
SELECT
  encode(extensions.hmac(id::text::bytea, 'ce107e09132b2578ec23407a083c90f772cb45f9cb882b163a6fe9a556cf5010'::bytea, 'sha256'), 'hex'),
  butaca_numero,
  alias,
  phrase,
  COALESCE(is_public, false)
FROM profiles
WHERE butaca_numero IS NOT NULL
ON CONFLICT (seat_token) DO NOTHING;

ALTER TABLE profiles DROP COLUMN IF EXISTS butaca_numero CASCADE;
ALTER TABLE profiles DROP COLUMN IF EXISTS alias CASCADE;
ALTER TABLE profiles DROP COLUMN IF EXISTS phrase CASCADE;
ALTER TABLE profiles DROP COLUMN IF EXISTS is_public CASCADE;


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 4: RPCs PARA seat_identities
-- ═══════════════════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS get_my_seat();
DROP FUNCTION IF EXISTS get_my_seat_profile();
DROP FUNCTION IF EXISTS set_my_seat_profile(text, text, boolean);
DROP FUNCTION IF EXISTS set_profile_visibility(boolean);
CREATE OR REPLACE FUNCTION get_my_seat()
RETURNS INT LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT butaca_numero FROM seat_identities WHERE seat_token = private.my_seat_token();
$$;

CREATE OR REPLACE FUNCTION get_my_seat_profile()
RETURNS TABLE(butaca_numero INT, alias TEXT, phrase TEXT, is_public BOOL, votes_count INT)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT butaca_numero, alias, phrase, is_public, votes_count
  FROM seat_identities WHERE seat_token = private.my_seat_token();
$$;

CREATE OR REPLACE FUNCTION set_my_seat_profile(
  p_alias     TEXT DEFAULT NULL,
  p_phrase    TEXT DEFAULT NULL,
  p_is_public BOOLEAN DEFAULT NULL
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  UPDATE seat_identities SET
    alias     = COALESCE(p_alias,     alias),
    phrase    = COALESCE(p_phrase,    phrase),
    is_public = COALESCE(p_is_public, is_public)
  WHERE seat_token = private.my_seat_token();
END;
$$;

CREATE OR REPLACE FUNCTION set_profile_visibility(p_is_public BOOLEAN)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  UPDATE seat_identities SET is_public = p_is_public
  WHERE seat_token = private.my_seat_token();
END;
$$;


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 5: ACTUALIZAR claim_seat
-- ═══════════════════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS claim_seat(uuid);
CREATE OR REPLACE FUNCTION claim_seat(p_verification_id UUID)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_butaca     INT;
  v_seat_token TEXT;
BEGIN
  SELECT butaca_numero INTO v_butaca
  FROM verifications
  WHERE id = p_verification_id AND status = 'aprobado' AND user_id = auth.uid();

  IF v_butaca IS NULL THEN
    RAISE EXCEPTION 'Verificación no encontrada o no aprobada';
  END IF;

  v_seat_token := private.my_seat_token();

  INSERT INTO seat_identities (seat_token, butaca_numero)
  VALUES (v_seat_token, v_butaca)
  ON CONFLICT (seat_token) DO UPDATE SET butaca_numero = EXCLUDED.butaca_numero;

  RETURN v_butaca;
END;
$$;


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 6: VOTOS — romper la cadena butaca → voto
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE votes DROP COLUMN IF EXISTS seat_number;
ALTER TABLE votes ADD COLUMN IF NOT EXISTS voter_token TEXT;
UPDATE votes SET voter_token = 'migrated_' || id::text WHERE voter_token IS NULL;
ALTER TABLE votes ALTER COLUMN voter_token SET NOT NULL;

ALTER TABLE votes DROP CONSTRAINT IF EXISTS votes_seat_number_question_id_key;
ALTER TABLE votes DROP CONSTRAINT IF EXISTS votes_voter_token_question_id_key;
ALTER TABLE votes ADD CONSTRAINT votes_voter_token_question_id_key UNIQUE (voter_token, question_id);

ALTER TABLE votes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "no_direct_access" ON votes;
CREATE POLICY "no_direct_access" ON votes FOR ALL USING (false);


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 7: REESCRIBIR cast_vote
-- ═══════════════════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS cast_vote(uuid, text, text, text);
CREATE OR REPLACE FUNCTION cast_vote(
  p_question_id UUID,
  p_vote_hash   TEXT,
  p_vote_plain  TEXT,
  p_nonce       TEXT
)
RETURNS JSON LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_voter_token TEXT;
  v_butaca      INT;
BEGIN
  IF p_vote_plain NOT IN ('si', 'no', 'abs') THEN
    RETURN json_build_object('ok', false, 'error', 'Voto inválido');
  END IF;

  v_voter_token := private.my_voter_token();

  IF EXISTS (SELECT 1 FROM votes WHERE voter_token = v_voter_token AND question_id = p_question_id) THEN
    RETURN json_build_object('ok', false, 'error', 'Ya votaste en esta pregunta');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM questions WHERE id = p_question_id AND status = 'activa') THEN
    RETURN json_build_object('ok', false, 'error', 'Esta pregunta no está activa');
  END IF;

  INSERT INTO votes (voter_token, question_id, vote_hash, vote_plain, nonce_reveal)
  VALUES (v_voter_token, p_question_id, p_vote_hash, p_vote_plain, p_nonce);

  SELECT butaca_numero INTO v_butaca FROM seat_identities WHERE seat_token = private.my_seat_token();

  IF v_butaca IS NOT NULL THEN
    INSERT INTO vote_seats (seat_number, question_id) VALUES (v_butaca, p_question_id) ON CONFLICT DO NOTHING;
  END IF;

  UPDATE seat_identities SET votes_count = votes_count + 1 WHERE seat_token = private.my_seat_token();

  RETURN json_build_object('ok', true);
END;
$$;


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 8: FUNCIONES DE CONSULTA DE VOTOS
-- ═══════════════════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS get_my_vote(uuid);
DROP FUNCTION IF EXISTS get_my_vote_stats(int);
DROP FUNCTION IF EXISTS get_my_vote_history(int);
CREATE OR REPLACE FUNCTION get_my_vote(p_question_id UUID)
RETURNS TABLE(vote_plain TEXT) LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  RETURN QUERY SELECT v.vote_plain FROM votes v
    WHERE v.voter_token = private.my_voter_token() AND v.question_id = p_question_id;
END;
$$;

CREATE OR REPLACE FUNCTION get_my_vote_stats(p_seat_number INT DEFAULT NULL)
RETURNS TABLE(vote_plain TEXT, cnt BIGINT) LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  RETURN QUERY SELECT v.vote_plain, count(*)::BIGINT FROM votes v
    WHERE v.voter_token = private.my_voter_token() GROUP BY v.vote_plain;
END;
$$;

CREATE OR REPLACE FUNCTION get_my_vote_history(p_seat_number INT DEFAULT NULL)
RETURNS TABLE(question_id UUID, vote_plain TEXT, created_at TIMESTAMPTZ)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  RETURN QUERY SELECT v.question_id, v.vote_plain, v.created_at FROM votes v
    WHERE v.voter_token = private.my_voter_token() ORDER BY v.created_at DESC;
END;
$$;


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 9 y 10: PERFILES PÚBLICOS PARA EL HEMICICLO
-- ═══════════════════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS get_public_seat_profiles();
DROP FUNCTION IF EXISTS get_profiles_with_vote_counts();
DROP FUNCTION IF EXISTS check_alias_available(text);
CREATE OR REPLACE FUNCTION get_public_seat_profiles()
RETURNS TABLE(butaca_numero INT, alias TEXT, phrase TEXT, votes_count INT)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT butaca_numero, alias, phrase, votes_count FROM seat_identities WHERE is_public = true;
$$;

CREATE OR REPLACE FUNCTION get_profiles_with_vote_counts()
RETURNS TABLE(seat_number INT, alias TEXT, phrase TEXT, vote_count BIGINT, show_alias BOOLEAN, show_phrase BOOLEAN, show_votes BOOLEAN)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT
    si.butaca_numero,
    CASE WHEN si.is_public THEN si.alias  ELSE NULL END,
    CASE WHEN si.is_public THEN si.phrase ELSE NULL END,
    si.votes_count,
    si.is_public,
    si.is_public,
    true
  FROM seat_identities si;
$$;

CREATE OR REPLACE FUNCTION check_alias_available(p_alias TEXT)
RETURNS BOOLEAN LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT NOT EXISTS (SELECT 1 FROM seat_identities WHERE alias = p_alias);
$$;


-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 11: UNIQUE en alias
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE seat_identities ADD CONSTRAINT seat_identities_alias_unique UNIQUE (alias);
