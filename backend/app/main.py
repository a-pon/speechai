from pathlib import Path
import os

from fastapi import FastAPI, HTTPException
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


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def _consultation_page() -> FileResponse:
    return FileResponse(CONSULTATION_HTML, media_type="text/html", headers={"Cache-Control": "no-store"})


APP_VERSION = "1.5.0"
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


@app.get("/health/worker")
def worker_health():
    from datetime import datetime
    import redis
    from sqlalchemy import func, or_, select
    from app.db import SessionLocal
    from app.models import Consultation

    settings = get_settings()
    marker = settings.audio_dir.parent / "worker-heartbeat"
    age_seconds = None
    if marker.exists():
        age_seconds = (datetime.utcnow() - datetime.utcfromtimestamp(marker.stat().st_mtime)).total_seconds()
    try:
        redis_ok = bool(redis.Redis.from_url(settings.celery_broker_url, socket_timeout=2).ping())
    except Exception:
        redis_ok = False
    with SessionLocal() as db:
        pending_due = db.scalar(select(func.count()).select_from(Consultation).where(
            Consultation.status.in_(("uploaded", "processing")),
            or_(Consultation.lease_until.is_(None), Consultation.lease_until <= datetime.utcnow()),
        ))
    details = {"worker_heartbeat_age_seconds": age_seconds, "redis_ok": redis_ok,
               "pending_due": pending_due}
    if not (redis_ok and age_seconds is not None and age_seconds < 120):
        raise HTTPException(503, detail=details)
    return {"status": "ok", **details}


@app.get("/health/export-worker")
def export_worker_health():
    from datetime import datetime

    settings = get_settings()
    if not settings.remote_audio_enabled:
        return {"status": "disabled"}
    marker = settings.audio_dir.parent / "export-worker-heartbeat"
    age_seconds = None
    if marker.exists():
        age_seconds = (datetime.utcnow() - datetime.utcfromtimestamp(marker.stat().st_mtime)).total_seconds()
    if age_seconds is None or age_seconds >= 120:
        raise HTTPException(503, detail={"export_worker_heartbeat_age_seconds": age_seconds})
    return {"status": "ok", "export_worker_heartbeat_age_seconds": age_seconds}


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

app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
