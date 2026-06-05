import shutil
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import can_access_doctor_record, get_current_user
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import Consultation
from app.schemas import ConsultationDetail, ConsultationListItem, TranscriptSegmentOut, UploadResponse
from app.services.pipeline import process_consultation

router = APIRouter(prefix="/api/consultations", tags=["consultations"])


def _run_pipeline(consultation_id: str) -> None:
    db = SessionLocal()
    try:
        import asyncio

        asyncio.run(process_consultation(db, consultation_id))
    finally:
        db.close()


@router.post("/upload", response_model=UploadResponse)
async def upload_consultation(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    doctor_name: str = Form(""),
    patient_name: str = Form(...),
    consultation_date: date = Form(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(400, "Файл не указан")

    ext = Path(file.filename).suffix.lower()
    if ext not in {".mp3", ".wav", ".ogg", ".opus", ".m4a"}:
        raise HTTPException(400, "Поддерживаются: mp3, wav, ogg, opus, m4a")

    settings = get_settings()
    consultation_id = str(uuid4())
    dest_dir = settings.audio_dir / consultation_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"audio{ext}"

    with dest_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    normalized_doctor_name = doctor_name.strip()
    if user["role"] == "doctor":
        normalized_doctor_name = user["doctor_name"] or user["username"]
    elif not normalized_doctor_name:
        raise HTTPException(400, "Имя врача обязательно")

    consultation = Consultation(
        id=consultation_id,
        consultation_date=consultation_date,
        doctor_name=normalized_doctor_name,
        patient_name=patient_name.strip(),
        audio_path=str(dest_path),
        original_filename=file.filename,
        status="uploaded",
    )
    db.add(consultation)
    db.commit()

    background_tasks.add_task(_run_pipeline, consultation_id)

    return UploadResponse(
        id=consultation_id,
        status="processing",
        message="Запись загружена, идёт обработка",
    )


@router.get("", response_model=list[ConsultationListItem])
def list_consultations(db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = select(Consultation).order_by(Consultation.created_at.desc())
    if user["role"] == "doctor":
        query = query.where(Consultation.doctor_name == user["doctor_name"])

    rows = db.scalars(query).all()
    return [
        ConsultationListItem(
            id=r.id,
            consultation_date=r.consultation_date,
            doctor_name=r.doctor_name,
            patient_name=r.patient_name,
            duration_sec=r.duration_sec,
            overall_score=r.overall_score,
            status=r.status,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.delete("/{consultation_id}")
def delete_consultation(
    consultation_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    row = db.get(Consultation, consultation_id)
    if not row:
        raise HTTPException(404, "Запись не найдена")
    if not can_access_doctor_record(user, row.doctor_name):
        raise HTTPException(403, "Недостаточно прав")

    settings = get_settings()
    audio_path = Path(row.audio_path)
    if not audio_path.is_absolute():
        audio_path = Path.cwd() / audio_path

    db.delete(row)
    db.commit()

    id_dir = settings.audio_dir / consultation_id
    if id_dir.is_dir():
        shutil.rmtree(id_dir)
    elif audio_path.is_file():
        audio_path.unlink(missing_ok=True)
        parent = audio_path.parent
        if parent.is_dir() and parent != settings.audio_dir and not any(parent.iterdir()):
            parent.rmdir()

    return {"ok": True, "message": "Запись удалена"}


@router.get("/{consultation_id}", response_model=ConsultationDetail)
def get_consultation(
    consultation_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    row = db.scalar(
        select(Consultation)
        .where(Consultation.id == consultation_id)
        .options(joinedload(Consultation.segments))
    )
    if not row:
        raise HTTPException(404, "Запись не найдена")
    if not can_access_doctor_record(user, row.doctor_name):
        raise HTTPException(403, "Недостаточно прав")

    return ConsultationDetail(
        id=row.id,
        consultation_date=row.consultation_date,
        doctor_name=row.doctor_name,
        patient_name=row.patient_name,
        duration_sec=row.duration_sec,
        overall_score=row.overall_score,
        status=row.status,
        error_message=row.error_message,
        evaluation_report=row.evaluation_report,
        transcript_text=row.transcript_text,
        segments=[
            TranscriptSegmentOut(
                speaker_role=s.speaker_role,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                text=s.text,
            )
            for s in row.segments
        ],
        created_at=row.created_at,
        processed_at=row.processed_at,
    )
