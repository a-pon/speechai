from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.consultations import router as consultations_router
from app.api.integration import router as integration_router
from app.api.users import router as users_router
from app.config import get_settings
from app.db import init_db

STATIC_DIR = Path(__file__).parent / "static"
CONSULTATION_HTML = STATIC_DIR / "consultation.html"


def _consultation_page() -> FileResponse:
    return FileResponse(CONSULTATION_HTML, media_type="text/html", headers={"Cache-Control": "no-store"})


APP_VERSION = "1.4.0"
APP_PORT = int(os.getenv("PORT", "8000"))

app = FastAPI(title="SpeechAI", version=APP_VERSION)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "pages": ["/", "/record/{id}"],
        "port": APP_PORT,
        "max_audio_duration_minutes": settings.max_audio_duration_minutes,
        "recover_pending_on_startup": settings.recover_pending_on_startup,
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/record/{consultation_id}")
def consultation_page(consultation_id: str):
    return _consultation_page()


@app.get("/consultations/{consultation_id}")
def consultation_page_legacy(consultation_id: str):
    """Старый URL — редирект на /record/…"""
    return RedirectResponse(url=f"/record/{consultation_id}", status_code=307)


app.include_router(consultations_router)
app.include_router(auth_router)
app.include_router(integration_router)
app.include_router(users_router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
