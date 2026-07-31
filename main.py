import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import verify, admin
from services.gemini import init_gemini, get_gemini_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar Gemini al arrancar
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY no configurada — verificación de documentos no disponible")
    else:
        init_gemini(api_key)
        logger.info("Gemini Vision inicializado ✓")
    yield


app = FastAPI(
    title="CabildoOS API",
    version="1.0.0",
    description="Backend de verificación de identidad y estadísticas para CabildoOS",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────────────
origins = [
    os.environ.get("FRONTEND_ORIGIN", "https://cabildoos.pages.dev"),
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:5500",   # Live Server (VS Code)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(verify.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {
        "service": "CabildoOS API",
        "version": "1.0.0",
        "status": "ok",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    gs = get_gemini_stats()
    return {
        "ok":           True,
        "version":      "1.0.0",
        "gemini_model": gs["model"],
        "status":       "ok",
    }
