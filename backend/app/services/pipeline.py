import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, update

from app.db import SessionLocal
from app.models import Consultation, TranscriptSegment
from app.services.audio_utils import get_duration_sec
from app.services.speechkit import transcribe_audio
from app.services.yandex_gpt import evaluate_transcript

logger = logging.getLogger(__name__)


def _update_current(consultation_id: str, generation: int, **values) -> bool:
    with SessionLocal() as db:
        result = db.execute(
            update(Consultation)
            .where(
                Consultation.id == consultation_id,
                Consultation.processing_generation == generation,
                Consultation.status == "processing",
            )
            .values(**values)
        )
        if result.rowcount != 1:
            db.rollback()
            logger.info("Discarding stale processing result id=%s generation=%s", consultation_id, generation)
            return False
        db.commit()
        return True


def _save_transcript(
    consultation_id: str,
    generation: int,
    segments: list[TranscriptSegment],
    transcript_text: str,
    duration_sec: int | None,
) -> bool:
    with SessionLocal() as db:
        result = db.execute(
            update(Consultation)
            .where(
                Consultation.id == consultation_id,
                Consultation.processing_generation == generation,
                Consultation.status == "processing",
            )
            .values(transcript_text=transcript_text, duration_sec=duration_sec)
        )
        if result.rowcount != 1:
            db.rollback()
            logger.info("Discarding stale transcript id=%s generation=%s", consultation_id, generation)
            return False
        db.execute(delete(TranscriptSegment).where(TranscriptSegment.consultation_id == consultation_id))
        for segment in segments:
            segment.consultation_id = consultation_id
        db.add_all(segments)
        db.commit()
        return True


async def process_consultation(consultation_id: str, generation: int) -> None:
    with SessionLocal() as db:
        consultation = db.get(Consultation, consultation_id)
        if not consultation or consultation.status != "processing" or consultation.processing_generation != generation:
            return
        audio_path = Path(consultation.audio_path)
        duration_sec = consultation.duration_sec
        transcript_text = consultation.transcript_text
        consultation_type = consultation.consultation_type

    stage = "проверка аудиофайла"
    try:
        if not audio_path.exists():
            raise RuntimeError("Файл аудиозаписи не найден на сервере")
        if duration_sec is None:
            duration_sec = get_duration_sec(audio_path)

        if not transcript_text:
            stage = "SpeechKit: распознавание аудио"
            segments, transcript_text = await transcribe_audio(audio_path)
            if not _save_transcript(consultation_id, generation, segments, transcript_text, duration_sec):
                return

        stage = "YandexGPT: оценка транскрипции"
        report, overall_score = await evaluate_transcript(transcript_text, consultation_type)
        _update_current(
            consultation_id,
            generation,
            evaluation_report=report,
            overall_score=overall_score,
            status="ready",
            processed_at=datetime.utcnow(),
        )
    except Exception as exc:
        logger.exception("Consultation processing failed id=%s stage=%s", consultation_id, stage)
        _update_current(
            consultation_id,
            generation,
            status="failed",
            duration_sec=duration_sec,
            error_message=f"{stage}: {type(exc).__name__}: {exc}",
        )
