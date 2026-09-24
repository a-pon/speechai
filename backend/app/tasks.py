import asyncio
import logging
import threading
import time
import resource
import sys
from datetime import datetime, timedelta

from celery import Celery
from celery.signals import worker_ready
from sqlalchemy import or_, select, update

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Consultation
from app.services.pipeline import process_step, record_failure

settings = get_settings()
logger = logging.getLogger(__name__)
celery_app = Celery("speechai", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 7200},
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=3900,
    task_soft_time_limit=3600,
    beat_schedule={
        "recover-pending-every-minute": {"task": "speechai.recover_pending", "schedule": 60.0},
        "worker-heartbeat-every-30-seconds": {"task": "speechai.worker_heartbeat", "schedule": 30.0},
        "recover-remote-audio-every-minute": {"task": "speechai.recover_remote_audio", "schedule": 60.0},
        **({"export-worker-heartbeat-every-30-seconds": {
            "task": "speechai.export_worker_heartbeat", "schedule": 30.0,
            "options": {"queue": "audio-export"},
        }} if settings.remote_audio_enabled else {}),
    },
)


def _claim(consultation_id: str, generation: int) -> bool:
    now = datetime.utcnow()
    with SessionLocal() as db:
        result = db.execute(update(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.processing_generation == generation,
            Consultation.status.in_(("uploaded", "processing")),
            or_(Consultation.lease_until.is_(None), Consultation.lease_until <= now),
        ).values(status="processing", lease_until=now + timedelta(seconds=settings.processing_lease_seconds),
                 queued_at=None, step_running=True,
                 error_category=None, error_message=None, stage_updated_at=now))
        db.commit()
        return result.rowcount == 1


def _write_worker_heartbeat() -> None:
    marker = get_settings().audio_dir.parent / "worker-heartbeat"
    marker.write_text(datetime.utcnow().isoformat(), encoding="ascii")


@celery_app.task(name="speechai.worker_heartbeat")
def worker_heartbeat_task() -> None:
    _write_worker_heartbeat()


@celery_app.task(name="speechai.export_worker_heartbeat")
def export_worker_heartbeat_task() -> None:
    marker = get_settings().audio_dir.parent / "export-worker-heartbeat"
    marker.write_text(datetime.utcnow().isoformat(), encoding="ascii")


def _heartbeat(consultation_id: str, generation: int, stop: threading.Event) -> None:
    while not stop.wait(30):
        try:
            _write_worker_heartbeat()
            with SessionLocal() as db:
                db.execute(update(Consultation).where(
                    Consultation.id == consultation_id,
                    Consultation.processing_generation == generation,
                    Consultation.status == "processing",
                ).values(lease_until=datetime.utcnow() + timedelta(seconds=settings.processing_lease_seconds)))
                db.commit()
        except Exception:
            logger.exception("Could not renew processing lease id=%s", consultation_id)


def _schedule_next(consultation_id: str, generation: int, delay: int) -> None:
    try:
        process_consultation_task.apply_async(args=(consultation_id, generation), countdown=delay)
    except Exception:
        # Beat will rediscover the due row.
        logger.exception("Could not schedule next stage id=%s", consultation_id)


@celery_app.task(name="speechai.process_consultation")
def process_consultation_task(consultation_id: str, generation: int = 0) -> None:
    if not _claim(consultation_id, generation):
        return
    _write_worker_heartbeat()
    with SessionLocal() as db:
        row = db.get(Consultation, consultation_id)
        stage = row.processing_stage if row else "unknown"
    started = time.monotonic()
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(consultation_id, generation, stop), daemon=True)
    heartbeat.start()
    try:
        delay = asyncio.run(process_step(consultation_id, generation))
    except Exception as exc:
        delay = record_failure(consultation_id, generation, exc)
        # record_failure already set the backoff deadline.
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss_mib = peak_rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        logger.info("Processing step id=%s stage=%s elapsed_sec=%.2f process_peak_rss_mib=%.1f",
                    consultation_id, stage, time.monotonic() - started, peak_rss_mib)
        with SessionLocal() as db:
            row = db.get(Consultation, consultation_id)
            if row and row.processing_generation == generation:
                row.step_running = False
                if delay is not None and row.status == "processing":
                    row.lease_until = datetime.utcnow() + timedelta(seconds=delay)
                db.commit()
    if delay is not None:
        _schedule_next(consultation_id, generation, delay)


@celery_app.task(name="speechai.recover_pending")
def recover_pending_task() -> None:
    now = datetime.utcnow()
    with SessionLocal() as db:
        rows = db.scalars(select(Consultation).where(
            Consultation.status.in_(("uploaded", "processing")),
            or_(Consultation.lease_until.is_(None), Consultation.lease_until <= now),
            or_(Consultation.queued_at.is_(None),
                Consultation.queued_at <= now - timedelta(minutes=5)),
        )).all()
        pending = []
        for row in rows:
            if row.step_running:
                row.worker_interruptions += 1
                row.step_running = False
                if row.worker_interruptions >= 3:
                    row.status = "failed"
                    row.error_category = "worker_interrupted"
                    row.error_message = (
                        f"Worker прервался на этапе {row.processing_stage} трижды. "
                        "Проверьте OOM и логи контейнера."
                    )
                    row.lease_until = None
                    continue
            row.queued_at = now
            pending.append((row.id, row.processing_generation))
        db.commit()
    for consultation_id, generation in pending:
        _schedule_next(consultation_id, generation, 0)


@worker_ready.connect
def recover_pending_on_worker_start(sender=None, **kwargs) -> None:
    if not get_settings().recover_pending_on_startup:
        return
    init_db()
    # A killed worker's lease is allowed to expire, preserving its operation ID.
    recover_pending_task.delay()


def _claim_export(consultation_id: str) -> bool:
    now = datetime.utcnow()
    with SessionLocal() as db:
        result = db.execute(update(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.status == "ready",
            Consultation.remote_export_status.in_(("pending", "exporting")),
            or_(Consultation.remote_export_lease_until.is_(None),
                Consultation.remote_export_lease_until <= now),
            or_(Consultation.remote_export_next_at.is_(None),
                Consultation.remote_export_next_at <= now),
        ).values(remote_export_status="exporting",
                 remote_export_queued_at=None,
                 remote_export_lease_until=now + timedelta(seconds=3900)))
        db.commit()
        return result.rowcount == 1


def _claim_restore(consultation_id: str) -> bool:
    now = datetime.utcnow()
    with SessionLocal() as db:
        result = db.execute(update(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.status == "ready",
            Consultation.remote_export_status == "done",
            Consultation.remote_restore_status.in_(("pending", "restoring")),
            or_(Consultation.remote_restore_lease_until.is_(None),
                Consultation.remote_restore_lease_until <= now),
        ).values(remote_restore_status="restoring",
                 remote_restore_queued_at=None,
                 remote_restore_lease_until=now + timedelta(seconds=3900)))
        db.commit()
        return result.rowcount == 1


@celery_app.task(name="speechai.export_audio")
def export_audio_task(consultation_id: str) -> None:
    if not get_settings().remote_audio_enabled or not _claim_export(consultation_id):
        return
    from pathlib import Path
    from app.services.audio_export import export_audio_file

    try:
        with SessionLocal() as db:
            row = db.get(Consultation, consultation_id)
            audio_path = Path(row.audio_path)
        result = export_audio_file(consultation_id, audio_path)
        persisted = False
        with SessionLocal() as db:
            row = db.get(Consultation, consultation_id)
            if row and row.remote_export_status == "exporting":
                row.remote_export_status = "done"
                row.remote_export_lease_until = None
                row.remote_export_next_at = None
                row.remote_export_error = None
                row.remote_audio_path = result.remote_audio_path
                row.remote_audio_sha256 = result.sha256
                row.remote_exported_at = datetime.utcnow()
                db.commit()
                persisted = True
        # Mark the verified remote copy durable before optionally removing the local file.
        if persisted and get_settings().remote_audio_delete_local_after_upload:
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove local audio after verified export id=%s", consultation_id)
    except Exception as exc:
        logger.exception("Audio export failed id=%s", consultation_id)
        with SessionLocal() as db:
            row = db.get(Consultation, consultation_id)
            if row and row.remote_export_status == "exporting":
                row.remote_export_attempts += 1
                row.remote_export_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                if row.remote_export_first_failure_at is None:
                    row.remote_export_first_failure_at = datetime.utcnow()
                if (datetime.utcnow() - row.remote_export_first_failure_at
                        < timedelta(hours=get_settings().auto_retry_max_hours)):
                    row.remote_export_status = "pending"
                    delay = min(60 * (2 ** min(row.remote_export_attempts - 1, 5)), 1800)
                    row.remote_export_next_at = datetime.utcnow() + timedelta(seconds=delay)
                else:
                    row.remote_export_status = "failed"
                row.remote_export_lease_until = None
                db.commit()
@celery_app.task(name="speechai.restore_audio")
def restore_audio_task(consultation_id: str) -> None:
    if not get_settings().remote_audio_enabled or not _claim_restore(consultation_id):
        return
    from pathlib import Path
    from app.services.audio_export import restore_audio_file

    try:
        with SessionLocal() as db:
            row = db.get(Consultation, consultation_id)
            audio_path = Path(row.audio_path)
            checksum = row.remote_audio_sha256
        restore_audio_file(consultation_id, audio_path, checksum)
        with SessionLocal() as db:
            row = db.get(Consultation, consultation_id)
            if row and row.remote_restore_status == "restoring":
                row.remote_restore_status = "done"
                row.remote_restore_error = None
                row.remote_restore_lease_until = None
                db.commit()
    except Exception as exc:
        logger.exception("Audio restore failed id=%s", consultation_id)
        with SessionLocal() as db:
            row = db.get(Consultation, consultation_id)
            if row and row.remote_restore_status == "restoring":
                row.remote_restore_status = "failed"
                row.remote_restore_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                row.remote_restore_lease_until = None
                db.commit()
def _enqueue_remote(task, consultation_id: str) -> None:
    try:
        task.apply_async(args=(consultation_id,), queue="audio-export")
    except Exception:
        # The next periodic sweep will requeue the marked row.
        logger.exception("Could not enqueue remote audio task id=%s", consultation_id)


@celery_app.task(name="speechai.recover_remote_audio")
def recover_remote_audio_task() -> None:
    settings = get_settings()
    if not settings.remote_audio_enabled:
        return
    now = datetime.utcnow()
    with SessionLocal() as db:
        exports = []
        if settings.remote_audio_auto_export:
            rows = db.scalars(select(Consultation).where(
                Consultation.status == "ready",
                Consultation.remote_export_status.in_(("pending", "exporting")),
                or_(Consultation.remote_export_next_at.is_(None),
                    Consultation.remote_export_next_at <= now),
                or_(Consultation.remote_export_lease_until.is_(None),
                    Consultation.remote_export_lease_until <= now),
                or_(Consultation.remote_export_queued_at.is_(None),
                    Consultation.remote_export_queued_at <= now - timedelta(minutes=5)),
            )).all()
            for row in rows:
                row.remote_export_queued_at = now
                exports.append(row.id)
        restores = []
        rows = db.scalars(select(Consultation).where(
            Consultation.status == "ready",
            Consultation.remote_export_status == "done",
            Consultation.remote_restore_status.in_(("pending", "restoring")),
            or_(Consultation.remote_restore_lease_until.is_(None),
                Consultation.remote_restore_lease_until <= now),
            or_(Consultation.remote_restore_queued_at.is_(None),
                Consultation.remote_restore_queued_at <= now - timedelta(minutes=5)),
        )).all()
        for row in rows:
            row.remote_restore_queued_at = now
            restores.append(row.id)
        db.commit()
    for consultation_id in exports:
        _enqueue_remote(export_audio_task, consultation_id)
    for consultation_id in restores:
        _enqueue_remote(restore_audio_task, consultation_id)
