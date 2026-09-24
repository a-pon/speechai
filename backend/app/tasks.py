import logging

from celery import Celery
from celery.signals import worker_ready
from sqlalchemy import update

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Consultation
from app.services.processing_state import claim_processing, recover_pending

settings = get_settings()
logger = logging.getLogger(__name__)
celery_app = Celery("speechai", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Redis otherwise redelivers unacknowledged tasks after its 1-hour default,
    # while a valid audio job may run for up to 3 hours.
    broker_transport_options={"visibility_timeout": 14400},
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=10800,
    task_soft_time_limit=10500,
)


@celery_app.task(name="speechai.process_consultation")
def process_consultation_task(consultation_id: str, generation: int = 0) -> None:
    db = SessionLocal()
    try:
        if not claim_processing(db, consultation_id, generation):
            return
    finally:
        db.close()

    from app.api.consultations import _run_pipeline

    _run_pipeline(consultation_id, generation)


@worker_ready.connect
def recover_pending_on_worker_start(sender=None, **kwargs) -> None:
    if not get_settings().recover_pending_on_startup:
        return

    init_db()
    db = SessionLocal()
    try:
        pending = recover_pending(db)
    finally:
        db.close()

    for consultation_id, generation in pending:
        try:
            process_consultation_task.delay(consultation_id, generation)
        except Exception as exc:
            logger.exception("Could not requeue consultation id=%s generation=%s", consultation_id, generation)
            with SessionLocal() as failed_db:
                failed_db.execute(
                    update(Consultation)
                    .where(
                        Consultation.id == consultation_id,
                        Consultation.processing_generation == generation,
                        Consultation.status == "uploaded",
                    )
                    .values(status="failed", error_message=f"Очередь обработки: {type(exc).__name__}: {exc}")
                )
                failed_db.commit()
