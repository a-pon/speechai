from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Consultation
from app.services.audio_utils import get_duration_sec
from app.services.speechkit import transcribe_audio
from app.services.yandex_gpt import evaluate_transcript


async def process_consultation(db: Session, consultation_id: str) -> None:
    consultation = db.get(Consultation, consultation_id)
    if not consultation:
        return

    consultation.status = "processing"
    consultation.error_message = None
    db.commit()

    try:
        audio_path = Path(consultation.audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Файл не найден: {audio_path}")

        if consultation.duration_sec is None:
            consultation.duration_sec = get_duration_sec(audio_path)

        segments, transcript_text = await transcribe_audio(audio_path)
        for seg in segments:
            seg.consultation_id = consultation.id
        consultation.segments = segments
        consultation.transcript_text = transcript_text

        report, overall_score = await evaluate_transcript(transcript_text)
        consultation.evaluation_report = report
        consultation.overall_score = overall_score
        consultation.status = "ready"
        consultation.processed_at = datetime.utcnow()
    except Exception as exc:
        consultation.status = "failed"
        consultation.error_message = str(exc)
    finally:
        db.commit()
