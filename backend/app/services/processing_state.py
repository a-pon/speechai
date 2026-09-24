from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Consultation


def claim_processing(db: Session, consultation_id: str, generation: int) -> bool:
    """Only one queued message for a generation may start the pipeline."""
    result = db.execute(
        update(Consultation)
        .where(
            Consultation.id == consultation_id,
            Consultation.processing_generation == generation,
            Consultation.status == "uploaded",
            Consultation.processing_attempts < 2,
        )
        .values(
            status="processing",
            error_message=None,
            processing_attempts=Consultation.processing_attempts + 1,
        )
    )
    db.commit()
    return result.rowcount == 1


def reset_for_manual_retry(row: Consultation) -> int:
    row.status = "uploaded"
    row.error_message = None
    row.processing_attempts = 0
    row.processing_generation += 1
    return row.processing_generation


def recover_pending(db: Session) -> list[tuple[str, int]]:
    """Invalidate messages from before a worker restart and queue each row once."""
    rows = db.scalars(
        select(Consultation).where(Consultation.status.in_(("uploaded", "processing")))
    ).all()
    pending = []
    for row in rows:
        if row.processing_attempts >= 2:
            row.status = "failed"
            row.error_message = (
                "Worker прервался во время обработки дважды. Проверьте логи и память worker, "
                "затем запустите повторную обработку вручную."
            )
            continue
        row.processing_generation += 1
        row.status = "uploaded"
        pending.append((row.id, row.processing_generation))
    db.commit()
    return pending

