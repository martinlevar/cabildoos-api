-- ═══════════════════════════════════════════════════════════════════════════
-- CabildoOS — Tabla de verificaciones (privacidad por diseño)
-- NO se guardan datos personales. Solo el resultado y la foto censurada.
-- Correr en Supabase → SQL Editor
-- ═══════════════════════════════════════════════════════════════════════════

-- Si ya corriste la versión anterior, primero: DROP TABLE IF EXISTS verifications CASCADE;

CREATE TABLE IF NOT EXISTS verifications (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Estado
  status          TEXT NOT NULL DEFAULT 'pendiente_revision'
                  CHECK (status IN ('pendiente_revision', 'aprobado', 'rechazado')),

  -- Resultado del análisis Gemini (true/false, sin datos personales)
  doc_match       BOOLEAN,

  -- Foto censurada: cara visible, datos del documento pixelados
  -- Es lo único que ve el admin
  selfie_doc_url  TEXT,

  -- Revisión humana
  reviewed_by     UUID REFERENCES auth.users(id),
  reviewed_at     TIMESTAMPTZ,
  review_notes    TEXT
);

-- Trigger updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS verifications_updated_at ON verifications;
CREATE TRIGGER verifications_updated_at
  BEFORE UPDATE ON verifications
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Índices
CREATE INDEX IF NOT EXISTS idx_verifications_status     ON verifications(status);
CREATE INDEX IF NOT EXISTS idx_verifications_created_at ON verifications(created_at DESC);

-- RLS
ALTER TABLE verifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_full_access" ON verifications
  FOR ALL TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM auth.users
      WHERE auth.users.id = auth.uid()
      AND auth.users.raw_user_meta_data->>'role' IN ('admin', 'verificador')
    )
  );
