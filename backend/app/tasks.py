from celery import Celery
from celery.signals import worker_ready
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Consultation

settings = get_settings()
celery_app = Celery("speechai", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_time_limit=1800,
)


@celery_app.task(name="speechai.process_consultation")
def process_consultation_task(consultation_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(Consultation, consultation_id)
        if not row or row.status not in {"uploaded", "processing"}:
            return
        if row.processing_attempts >= 2:
            row.status = "failed"
            row.error_message = "Автоматическая обработка остановлена после двух попыток. Запустите повторную обработку вручную."
            db.commit()
            return
        row.processing_attempts += 1
        db.commit()
    finally:
        db.close()

    from app.api.consultations import _run_pipeline

    _run_pipeline(consultation_id)


@worker_ready.connect
def recover_pending_on_worker_start(sender=None, **kwargs) -> None:
    if not get_settings().recover_pending_on_startup:
        return

    init_db()
    db = SessionLocal()
    try:
        consultation_ids = db.scalars(
            select(Consultation.id).where(Consultation.status.in_(("uploaded", "processing")))
        ).all()
    finally:
        db.close()

    for consultation_id in consultation_ids:
        process_consultation_task.delay(consultation_id)
