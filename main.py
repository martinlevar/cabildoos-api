import os
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="CabildoOS API",
    version="2.0.0",
    description="Backend de administración para CabildoOS (verificación migrada a Cloudflare Workers)",
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


@app.get("/")
async def root():
    return {
        "service": "CabildoOS API",
        "version": "2.0.0",
        "status": "ok",
        "docs": "/docs",
        "note": "Verificación de identidad migrada a verify.cabildodevenezuela.com",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "2.0.0",
        "status": "ok",
    }
