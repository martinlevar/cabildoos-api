-- ═══════════════════════════════════════════════════════════════════════════
-- CabildoOS — Tabla de verificaciones de identidad
-- Correr en Supabase → SQL Editor
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS verifications (
  -- Identidad
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Status del proceso
  status          TEXT NOT NULL DEFAULT 'en_proceso'
                  CHECK (status IN (
                    'en_proceso',
                    'pendiente_revision',
                    'auto_aprobado',
                    'aprobado',
                    'rechazado'
                  )),

  -- Paso 1: datos declarados
  nombre          TEXT,
  apellido        TEXT,
  numero_doc      TEXT,
  tipo_doc        TEXT,   -- DNI_AR | PASAPORTE | CEDULA_VE | LICENCIA
  fecha_nac       TEXT,
  email           TEXT,
  telefono        TEXT,

  -- Paso 2: documento
  doc_foto_url    TEXT,
  doc_extracted   JSONB,  -- respuesta completa de Gemini
  doc_match       BOOLEAN,
  doc_confianza   FLOAT,

  -- Paso 3: selfie liveness
  selfie_liveness_url   TEXT,
  liveness_instruccion  TEXT,

  -- Paso 4: selfie sosteniendo documento
  selfie_doc_url  TEXT,

  -- Revisión por admin
  reviewed_by     UUID REFERENCES auth.users(id),
  reviewed_at     TIMESTAMPTZ,
  review_notes    TEXT
);

-- Trigger para actualizar updated_at automáticamente
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS verifications_updated_at ON verifications;
CREATE TRIGGER verifications_updated_at
  BEFORE UPDATE ON verifications
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Índices útiles
CREATE INDEX IF NOT EXISTS idx_verifications_status     ON verifications(status);
CREATE INDEX IF NOT EXISTS idx_verifications_created_at ON verifications(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_numero_doc ON verifications(numero_doc);
CREATE INDEX IF NOT EXISTS idx_verifications_email      ON verifications(email);

-- ── Row Level Security ───────────────────────────────────────────────────────
ALTER TABLE verifications ENABLE ROW LEVEL SECURITY;

-- El backend usa la service_role key → bypasea RLS completamente
-- Los admins autenticados pueden leer/escribir todo
CREATE POLICY "admins_full_access" ON verifications
  FOR ALL
  TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM auth.users
      WHERE auth.users.id = auth.uid()
      AND auth.users.raw_user_meta_data->>'role' IN ('admin', 'verificador')
    )
  );

-- Vista para el dashboard de admin (incluye conteos por status)
CREATE OR REPLACE VIEW verification_stats AS
SELECT
  status,
  COUNT(*)                                          AS total,
  COUNT(*) FILTER (WHERE created_at::date = CURRENT_DATE)        AS hoy,
  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') AS esta_semana
FROM verifications
GROUP BY status;
