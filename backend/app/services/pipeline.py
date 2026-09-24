"""One durable processing stage per Celery task."""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from sqlalchemy import delete, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import Consultation, TranscriptSegment
from app.services.audio_prepare import AudioValidationError, SilentAudioError, has_usable_signal, prepare_audio
from app.services.mock_ai import mock_transcribe
from app.services.object_storage import audio_uri, upload_audio
from app.services.speechkit import format_transcript, poll_recognition, start_recognition
from app.services.yandex_gpt import (evaluate_from_summaries, evaluate_transcript,
                                      split_transcript, summarize_transcript_chunk)

logger = logging.getLogger(__name__)


def _advance(consultation_id: str, generation: int, stage: str, **values) -> bool:
    if "processing_stage" in values and not (stage == "submit" and values["processing_stage"] == "submitting"):
        values["processing_attempts"] = 0
        values["stage_first_failure_at"] = None
        values["worker_interruptions"] = 0
    with SessionLocal() as db:
        result = db.execute(update(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.processing_generation == generation,
            Consultation.status == "processing",
            Consultation.processing_stage == stage,
        ).values(**values, stage_updated_at=datetime.utcnow()))
        db.commit()
        return result.rowcount == 1


def _save_transcript(consultation_id: str, generation: int, stage: str,
                     segments: list[TranscriptSegment], text: str) -> bool:
    with SessionLocal() as db:
        result = db.execute(update(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.processing_generation == generation,
            Consultation.status == "processing",
            Consultation.processing_stage == stage,
        ).values(transcript_text=text, processing_stage="evaluate", processing_attempts=0,
                 stage_first_failure_at=None, worker_interruptions=0,
                 stage_updated_at=datetime.utcnow()))
        if result.rowcount != 1:
            db.rollback()
            return False
        db.execute(delete(TranscriptSegment).where(TranscriptSegment.consultation_id == consultation_id))
        for segment in segments:
            segment.consultation_id = consultation_id
        db.add_all(segments)
        db.commit()
        return True


async def process_step(consultation_id: str, generation: int) -> int | None:
    """Return seconds until next step, or None when complete/stale."""
    with SessionLocal() as db:
        row = db.get(Consultation, consultation_id)
        if not row or row.status != "processing" or row.processing_generation != generation:
            return None
        stage = row.processing_stage or "prepare"
        path = Path(row.audio_path)
        storage_key = row.storage_key
        operation_id = row.speechkit_operation_id
        started_at = row.speechkit_started_at
        empty_polls = row.recognition_empty_polls
        transcript = row.transcript_text
        consultation_type = row.consultation_type
        summaries = json.loads(row.evaluation_chunks_json or "[]")

    settings = get_settings()
    if stage == "prepare" and transcript:
        return 0 if _advance(consultation_id, generation, stage, processing_stage="evaluate") else None

    if stage == "prepare":
        prepared_path, size, duration = prepare_audio(path)
        if not _advance(consultation_id, generation, stage, audio_path=str(prepared_path),
                        audio_size_bytes=size, duration_sec=duration,
                        processing_stage="evaluate" if transcript else ("mock_stt" if settings.mock_ai else "storage")):
            return None
        logger.info("Audio prepared id=%s bytes=%s duration_sec=%s", consultation_id, size, duration)
        return 0

    if stage == "mock_stt":
        segments, text = mock_transcribe(path)
        return 0 if _save_transcript(consultation_id, generation, stage, segments, text) else None

    if stage == "storage":
        if not storage_key:
            storage_key = f"consultations/{consultation_id}/audio{path.suffix.lower()}"
        upload_audio(path, storage_key)
        return 0 if _advance(consultation_id, generation, stage, storage_key=storage_key,
                             processing_stage="submit") else None

    if stage == "submit":
        if operation_id:
            return 0 if _advance(consultation_id, generation, stage, processing_stage="poll") else None
        if not storage_key:
            raise RuntimeError("Отсутствует ключ аудио в Object Storage")
        # Persist the uncertain window before POST. Recovery never submits it again.
        if not _advance(consultation_id, generation, stage, processing_stage="submitting"):
            return None
        operation_id = await start_recognition(audio_uri(storage_key), path)
        return settings.speechkit_poll_seconds if _advance(
            consultation_id, generation, "submitting", speechkit_operation_id=operation_id,
            speechkit_started_at=datetime.utcnow(), processing_stage="poll") else None

    if stage == "submitting":
        if operation_id:
            return 0 if _advance(consultation_id, generation, stage, processing_stage="poll") else None
        raise RuntimeError("Отправка SpeechKit могла завершиться, но ID операции не сохранён; требуется проверка администратора")

    if stage == "poll":
        if not operation_id or not started_at:
            raise RuntimeError("Нет сохранённого ID или времени запуска SpeechKit")
        if datetime.utcnow() - started_at > timedelta(hours=settings.speechkit_max_hours):
            raise TimeoutError(f"SpeechKit: операция {operation_id} не завершилась за {settings.speechkit_max_hours} ч")
        done, segments = await poll_recognition(operation_id)
        if not done:
            _advance(consultation_id, generation, stage, speechkit_last_poll_at=datetime.utcnow(),
                     processing_attempts=0, stage_first_failure_at=None, worker_interruptions=0)
            return settings.speechkit_poll_seconds
        if not segments:
            if (empty_polls == 0 or empty_polls >= 11) and not has_usable_signal(path):
                raise SilentAudioError("Звуковой сигнал слишком тихий: проверьте исходную аудиозапись")
            if empty_polls >= 11:
                raise RuntimeError(f"SpeechKit: завершённая операция {operation_id} не вернула распознанный текст")
            _advance(consultation_id, generation, stage, speechkit_last_poll_at=datetime.utcnow(),
                     recognition_empty_polls=empty_polls + 1,
                     processing_attempts=0, stage_first_failure_at=None, worker_interruptions=0)
            return settings.speechkit_poll_seconds
        return 0 if _save_transcript(consultation_id, generation, stage, segments,
                                     format_transcript(segments)) else None

    if stage == "evaluate":
        if not transcript:
            raise RuntimeError("Нет сохранённой транскрипции")
        chunks = split_transcript(transcript)
        if len(chunks) == 1:
            report, score = await evaluate_transcript(transcript, consultation_type)
        elif len(summaries) < len(chunks):
            index = len(summaries)
            summary = await summarize_transcript_chunk(chunks[index], index, len(chunks))
            summaries.append(summary)
            return 0 if _advance(consultation_id, generation, stage,
                                 evaluation_chunks_json=json.dumps(summaries, ensure_ascii=False),
                                 processing_attempts=0, stage_first_failure_at=None,
                                 worker_interruptions=0) else None
        else:
            report, score = await evaluate_from_summaries(summaries, consultation_type)
        if score is None or not 1 <= score <= 5:
            raise RuntimeError("YandexGPT: отчёт не содержит корректный общий балл")
        _advance(consultation_id, generation, stage,
                 evaluation_report=report, overall_score=score,
                 status="ready", processing_stage="done",
                 processed_at=datetime.utcnow(), error_message=None,
                 error_category=None)
        return None

    raise RuntimeError(f"Неизвестный этап обработки: {stage}")


def classify_error(exc: Exception, stage: str) -> tuple[str, bool]:
    message = str(exc).lower()
    is_rate_limit = "429" in message
    is_server_error = any(f"http {code}" in message for code in (500, 502, 503, 504))
    is_network_error = (isinstance(exc, httpx.TransportError)
                        or any(word in message for word in ("timeout", "network", "connect", "endpoint")))
    if "недостаточно места" in message:
        return "disk_space", False
    if isinstance(exc, SilentAudioError):
        return "silent_audio", False
    if isinstance(exc, AudioValidationError):
        return "audio_validation", False
    if "не настроен" in message or "задайте yandex" in message:
        return "configuration", False
    if stage == "submitting":
        if "http 429" in message:
            return "provider_rate_limit", True
        if any(f"http {code}" in message for code in (400, 401, 403, 404, 413)):
            return "speechkit_rejected", False
        return "submission_unknown", False
    if isinstance(exc, TimeoutError) and stage == "poll":
        return "speechkit_timeout", False
    if stage == "storage":
        return "object_storage", is_rate_limit or is_server_error or is_network_error
    if stage == "poll":
        if is_rate_limit or is_server_error or is_network_error:
            return "speechkit_transient", True
        return "speechkit_result", False
    if stage == "evaluate":
        if is_rate_limit or is_server_error or is_network_error:
            return "yandexgpt_transient", True
        return "evaluation", False
    if is_network_error:
        return "network", True
    return "application", False


def record_failure(consultation_id: str, generation: int, exc: Exception) -> int | None:
    with SessionLocal() as db:
        row = db.get(Consultation, consultation_id)
        if not row or row.processing_generation != generation or row.status != "processing":
            return None
        category, transient = classify_error(exc, row.processing_stage)
        attempts = row.processing_attempts + 1
        row.processing_attempts = attempts
        if row.stage_first_failure_at is None:
            row.stage_first_failure_at = datetime.utcnow()
        row.step_running = False
        row.error_category = category
        row.error_message = f"{row.processing_stage} [{category}]: {type(exc).__name__}: {str(exc)[:1200]}"
        row.stage_updated_at = datetime.utcnow()
        retry_window = timedelta(hours=get_settings().auto_retry_max_hours)
        if transient and datetime.utcnow() - row.stage_first_failure_at < retry_window:
            if row.processing_stage == "submitting" and category == "provider_rate_limit":
                row.processing_stage = "submit"
            delay = min(60 * (2 ** min(attempts - 1, 5)), 1800)
            row.lease_until = datetime.utcnow() + timedelta(seconds=delay)
        else:
            delay = None
            row.status = "invalid_audio" if category == "silent_audio" else "failed"
            row.lease_until = None
        db.commit()
        if category == "silent_audio":
            logger.warning("Audio has no usable signal id=%s stage=%s", consultation_id,
                           row.processing_stage)
        else:
            logger.error("Processing failed id=%s stage=%s category=%s attempts=%s",
                         consultation_id, row.processing_stage, category, attempts,
                         exc_info=(type(exc), exc, exc.__traceback__))
        return delay
