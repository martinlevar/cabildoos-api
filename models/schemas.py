import re
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator, model_validator

# ── Regex de validación ───────────────────────────────────────────────────────

# Letras latinas (incluye tildes, ñ, ü, etc.), espacios, guiones, puntos, apóstrofes
_NOMBRE_RE = re.compile(
    r"^[a-zA-ZáéíóúÁÉÍÓÚàèìòùÀÈÌÒÙâêîôûÂÊÎÔÛäëïöüÄËÏÖÜñÑçÇ'\-\. ]+$"
)
# Solo dígitos
_DIGITS_RE  = re.compile(r"^\d+$")
# Alfanumérico (para pasaporte/licencia)
_ALNUM_RE   = re.compile(r"^[a-zA-Z0-9]+$")
# UUID v4
_UUID_RE    = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# Tipos de documento permitidos
_TIPOS_DOC  = {"DNI", "DNI_AR", "PASAPORTE", "CEDULA_VE", "LICENCIA"}

# Tamaño máximo de imagen en base64 (~8 MB de imagen real ≈ 11 MB en b64)
_MAX_B64_LEN = 12_000_000


def _validar_nombre(v: str, campo: str = "Nombre") -> str:
    v = v.strip()
    if len(v) < 2:
        raise ValueError(f"{campo} demasiado corto (mínimo 2 caracteres)")
    if len(v) > 80:
        raise ValueError(f"{campo} demasiado largo (máximo 80 caracteres)")
    if not _NOMBRE_RE.match(v):
        raise ValueError(f"{campo} solo puede contener letras, espacios, guiones y puntos")
    return v


def _validar_numero_doc(numero: str, tipo: str) -> str:
    numero = numero.strip().upper()
    if len(numero) < 4:
        raise ValueError("Número de documento demasiado corto")
    if len(numero) > 20:
        raise ValueError("Número de documento demasiado largo")
    if tipo in ("DNI", "DNI_AR", "CEDULA_VE"):
        if not _DIGITS_RE.match(numero):
            raise ValueError("El número de DNI/Cédula solo puede contener dígitos")
    elif tipo in ("PASAPORTE", "LICENCIA"):
        if not _ALNUM_RE.match(numero):
            raise ValueError("El número de documento solo puede contener letras y dígitos")
    return numero


def _validar_fecha_nac(v: str) -> str:
    """Acepta YYYY-MM-DD o DD/MM/YYYY. Verifica edad ≥ 16 años."""
    v = v.strip()
    parsed: Optional[date] = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(v, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Fecha de nacimiento inválida (usar YYYY-MM-DD o DD/MM/YYYY)")
    today = date.today()
    if parsed > today:
        raise ValueError("La fecha de nacimiento no puede ser en el futuro")
    if parsed < date(1900, 1, 1):
        raise ValueError("Fecha de nacimiento fuera de rango")
    edad = (today - parsed).days // 365
    if edad < 16:
        raise ValueError("Debés tener al menos 16 años para verificarte")
    return v


def _validar_b64(v: str, campo: str = "Imagen") -> str:
    if not v:
        raise ValueError(f"{campo} no puede estar vacía")
    if len(v) > _MAX_B64_LEN:
        raise ValueError(f"{campo} supera el tamaño máximo permitido (8 MB)")
    # Permitir prefijo data:image/...;base64, o base64 puro
    b64 = v.split(",")[-1] if "," in v else v
    if not re.match(r"^[A-Za-z0-9+/=\n\r]+$", b64[:200]):
        raise ValueError(f"{campo} contiene caracteres inválidos")
    return v


def _validar_uuid(v: str) -> str:
    if not _UUID_RE.match(v.strip()):
        raise ValueError("ID de verificación inválido")
    return v.strip().lower()


# ── Paso 1: datos declarados por el usuario ───────────────────────────────────

class DatosDeclarados(BaseModel):
    nombre:     str
    apellido:   str
    numero_doc: str
    tipo_doc:   str          # DNI_AR | PASAPORTE | CEDULA_VE | LICENCIA
    fecha_nac:  Optional[str] = None
    email:      Optional[EmailStr] = None
    telefono:   Optional[str] = None

    @field_validator("tipo_doc")
    @classmethod
    def val_tipo_doc(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in _TIPOS_DOC:
            raise ValueError(f"Tipo de documento inválido. Permitidos: {', '.join(_TIPOS_DOC)}")
        return v

    @field_validator("nombre", "apellido")
    @classmethod
    def val_nombre(cls, v: str, info) -> str:
        return _validar_nombre(v, info.field_name.capitalize())

    @field_validator("fecha_nac")
    @classmethod
    def val_fecha(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return _validar_fecha_nac(v)
        return v

    @model_validator(mode="after")
    def val_numero_con_tipo(self):
        self.numero_doc = _validar_numero_doc(self.numero_doc, self.tipo_doc)
        return self

    @field_validator("telefono")
    @classmethod
    def val_telefono(cls, v: Optional[str]) -> Optional[str]:
        if v:
            v = v.strip()
            if not re.match(r"^\+?[\d\s\-\(\)]{6,20}$", v):
                raise ValueError("Teléfono inválido")
        return v


# ── Verificación de documento (paso 2) ───────────────────────────────────────

class VerificarDocumentoRequest(BaseModel):
    verification_id:      str
    image_b64:            str
    tipo_doc:             str
    nombre_declarado:     str
    apellido_declarado:   str
    numero_declarado:     str
    pais_declarado:       Optional[str] = None
    fecha_nac_declarada:  Optional[str] = None

    @field_validator("verification_id")
    @classmethod
    def val_vid(cls, v: str) -> str:
        return _validar_uuid(v)

    @field_validator("tipo_doc")
    @classmethod
    def val_tipo(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in _TIPOS_DOC:
            raise ValueError(f"Tipo de documento inválido")
        return v

    @field_validator("nombre_declarado", "apellido_declarado")
    @classmethod
    def val_nombres(cls, v: str, info) -> str:
        return _validar_nombre(v, info.field_name)

    @field_validator("fecha_nac_declarada")
    @classmethod
    def val_fecha(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return _validar_fecha_nac(v)
        return v

    @field_validator("image_b64")
    @classmethod
    def val_imagen(cls, v: str) -> str:
        return _validar_b64(v, "Imagen de documento")

    @model_validator(mode="after")
    def val_numero_con_tipo(self):
        self.numero_declarado = _validar_numero_doc(self.numero_declarado, self.tipo_doc)
        return self

    @field_validator("pais_declarado")
    @classmethod
    def val_pais(cls, v: Optional[str]) -> Optional[str]:
        if v:
            v = v.strip()
            if len(v) > 60 or not re.match(r"^[a-zA-ZáéíóúÁÉÍÓÚñÑ '\-]+$", v):
                raise ValueError("País inválido")
        return v


class DocumentoExtraido(BaseModel):
    nombre_completo:    Optional[str] = None
    numero_documento:   Optional[str] = None
    fecha_nacimiento:   Optional[str] = None
    tipo_documento:     Optional[str] = None
    pais_emisor:        Optional[str] = None
    es_documento_real:  bool = False
    nombre_coincide:    bool = False
    numero_coincide:    bool = False
    fecha_coincide:     bool = False
    pais_coincide:      bool = False
    confianza:          float = 0.0
    observaciones:      Optional[str] = None


class VerificarDocumentoResponse(BaseModel):
    ok:        bool
    match:     bool
    extracted: DocumentoExtraido
    foto_url:  Optional[str] = None
    error:     Optional[str] = None


# ── Submit final (paso 4) ────────────────────────────────────────────────────

class SubmitVerificacionRequest(BaseModel):
    verification_id: str
    selfie_doc_b64:  str
    gemini_match:    bool = False

    @field_validator("verification_id")
    @classmethod
    def val_vid(cls, v: str) -> str:
        return _validar_uuid(v)

    @field_validator("selfie_doc_b64")
    @classmethod
    def val_selfie(cls, v: str) -> str:
        return _validar_b64(v, "Selfie")


class SubmitVerificacionResponse(BaseModel):
    ok:              bool
    verification_id: str
    status:          str
    error:           Optional[str] = None


# ── Admin ────────────────────────────────────────────────────────────────────

class VerificationRecord(BaseModel):
    id:                   str
    created_at:           datetime
    status:               str
    nombre:               Optional[str]
    apellido:             Optional[str]
    numero_doc:           Optional[str]
    tipo_doc:             Optional[str]
    email:                Optional[str]
    doc_match:            Optional[bool]
    doc_confianza:        Optional[float]
    doc_foto_url:         Optional[str]
    selfie_liveness_url:  Optional[str]
    selfie_doc_url:       Optional[str]
    liveness_instruccion: Optional[str]
    doc_extracted:        Optional[dict]
    review_notes:         Optional[str]
    reviewed_at:          Optional[datetime]


class StatsResponse(BaseModel):
    total:        int
    pending:      int
    approved:     int
    rejected:     int
    hoy:          int
    esta_semana:  int
