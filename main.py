import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import admin, playroom
from services.gemini import init_gemini

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_is_dev = os.environ.get("ENV", "production").lower() in ("dev", "development", "local")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        init_gemini(gemini_key)
        logger.info("Gemini client inicializado ✓")
    else:
        logger.warning("GEMINI_API_KEY no configurada — endpoints de Playroom no funcionarán")
    yield
    # ── Shutdown ──


app = FastAPI(
    title="CabildoOS API",
    version="2.0.0",
    description="Backend de administración para CabildoOS (verificación migrada a Cloudflare Workers)",
    lifespan=lifespan,
    # Swagger/OpenAPI deshabilitado en producción (expone superficie de ataque)
    docs_url="/docs" if _is_dev else None,
    redoc_url="/redoc" if _is_dev else None,
    openapi_url="/openapi.json" if _is_dev else None,
)

# ── CORS ───────────────────────────────────────────────────────────────────────
origins = [
    os.environ.get("FRONTEND_ORIGIN", "https://cabildoos.pages.dev"),
    "https://cabildoos.pages.dev",
    "https://cabildodevenezuela.com",
    "https://www.cabildodevenezuela.com",
    "https://dev.cabildodevenezuela.com",
    "https://admin.cabildodevenezuela.com",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:5500",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(admin.router)
app.include_router(playroom.router)


@app.get("/")
async def root():
    return {
        "service": "CabildoOS API",
        "version": "2.0.0",
        "status": "ok",
        "note": "Verificación de identidad migrada a verify.cabildodevenezuela.com",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "2.0.0",
        "status": "ok",
    }
