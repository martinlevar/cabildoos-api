from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime
import uuid


# ── Paso 1: datos declarados por el usuario ───────────────────────────────────

class DatosDeclarados(BaseModel):
    nombre: str
    apellido: str
    numero_doc: str
    tipo_doc: str          # DNI_AR | PASAPORTE | CEDULA_VE | LICENCIA
    fecha_nac: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None


# ── Verificación de documento (paso 2) ───────────────────────────────────────

class VerificarDocumentoRequest(BaseModel):
    verification_id: str
    image_b64: str          # JPEG base64 sin prefijo data:
    tipo_doc: str
    nombre_declarado: str
    apellido_declarado: str
    numero_declarado: str
    pais_declarado: Optional[str] = None
    fecha_nac_declarada: Optional[str] = None  # DD/MM/YYYY declarada por el usuario


class DocumentoExtraido(BaseModel):
    nombre_completo: Optional[str] = None
    numero_documento: Optional[str] = None
    fecha_nacimiento: Optional[str] = None
    tipo_documento: Optional[str] = None
    pais_emisor: Optional[str] = None       # país extraído del documento
    es_documento_real: bool = False
    nombre_coincide: bool = False
    numero_coincide: bool = False
    fecha_coincide: bool = False
    pais_coincide: bool = False             # país declarado == país del documento
    confianza: float = 0.0
    observaciones: Optional[str] = None


class VerificarDocumentoResponse(BaseModel):
    ok: bool
    match: bool
    extracted: DocumentoExtraido
    foto_url: Optional[str] = None
    error: Optional[str] = None


# ── Submit final (paso 4) ────────────────────────────────────────────────────

class SubmitVerificacionRequest(BaseModel):
    verification_id: str
    # Solo la foto censurada — cara visible, datos del documento pixelados
    selfie_doc_b64: str
    # Resultado booleano del análisis Gemini (sin datos personales)
    gemini_match: bool = False


class SubmitVerificacionResponse(BaseModel):
    ok: bool
    verification_id: str
    status: str             # pending_review | auto_approved | error
    error: Optional[str] = None


# ── Admin ────────────────────────────────────────────────────────────────────

class VerificationRecord(BaseModel):
    id: str
    created_at: datetime
    status: str
    nombre: Optional[str]
    apellido: Optional[str]
    numero_doc: Optional[str]
    tipo_doc: Optional[str]
    email: Optional[str]
    doc_match: Optional[bool]
    doc_confianza: Optional[float]
    doc_foto_url: Optional[str]
    selfie_liveness_url: Optional[str]
    selfie_doc_url: Optional[str]
    liveness_instruccion: Optional[str]
    doc_extracted: Optional[dict]
    review_notes: Optional[str]
    reviewed_at: Optional[datetime]


class StatsResponse(BaseModel):
    total: int
    pending: int
    approved: int
    rejected: int
    hoy: int
    esta_semana: int
