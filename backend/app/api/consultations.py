import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.auth import can_access_doctor_record, can_view_all_records, get_current_user
from app.config import get_settings
from app.db import get_db
from app.models import Consultation
from app.schemas import BulkRetryResponse, ConsultationDetail, ConsultationListItem, TranscriptSegmentOut, UploadResponse
from app.services.processing_state import reset_for_manual_retry
from app.services.object_storage import delete_audio
from app.tasks import _enqueue_remote, export_audio_task, process_consultation_task, restore_audio_task

router = APIRouter(prefix="/api/consultations", tags=["consultations"])
CONSULTATION_TYPES = {"primary_adult", "primary_child", "repeat_adult"}
BULK_RETRY_ENABLED = False  # Re-enable only after the server checks are complete.
logger = logging.getLogger(__name__)

def _parse_ddmmyyyy_to_date(value: str | None) -> date | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            continue
    raise HTTPException(400, "Дата должна быть в формате дд/мм/гггг")


def _enqueue_processing(db: Session, row: Consultation, generation: int) -> bool:
    try:
        process_consultation_task.delay(row.id, generation)
        return True
    except Exception as exc:
        logger.exception("Could not enqueue consultation id=%s generation=%s", row.id, generation)
        db.execute(
            update(Consultation)
            .where(
                Consultation.id == row.id,
                Consultation.processing_generation == generation,
                Consultation.status == "uploaded",
            )
            .values(error_category="queue", error_message=f"Очередь обработки: {type(exc).__name__}: {exc}")
        )
        db.commit()
        return False


def _admin_error_message(row: Consultation, role: str) -> str | None:
    if role != "admin":
        return None
    if row.error_message:
        return row.error_message
    if row.status == "failed":
        return f"Причина ошибки не сохранена (попыток: {row.processing_attempts}). Проверьте логи worker."
    return None


def _parse_optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise HTTPException(400, "Числовое поле заполнено некорректно") from exc


def _required_text(value: str | None, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(400, f"{field_name} обязательно")
    return normalized


def _normalize_consultation_type(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(400, "Вид консультации обязателен")
    if normalized not in CONSULTATION_TYPES:
        raise HTTPException(400, "Вид консультации должен быть primary_adult, primary_child или repeat_adult")
    return normalized


def _remove_consultation_files(consultation_id: str, audio_path: Path, settings_audio_dir: Path) -> None:
    id_dir = settings_audio_dir / consultation_id
    if id_dir.is_dir():
        shutil.rmtree(id_dir)

    if audio_path.is_file():
        audio_path.unlink(missing_ok=True)

    parent = audio_path.parent
    if parent.is_dir() and parent != settings_audio_dir and not any(parent.iterdir()):
        parent.rmdir()


@router.post("/upload", response_model=UploadResponse)
async def upload_consultation(
    file: UploadFile = File(...),
    doctor_name: str = Form(""),
    patient_name: str = Form(...),
    consultation_date: str = Form(...),
    consultation_type: str = Form(...),
    clinic_division: str = Form(...),
    source_system: str | None = Form(None),
    source_payload_json: str | None = Form(None),
    doctor_code: str | None = Form(None),
    doctor_position: str | None = Form(None),
    doctor_category: str | None = Form(None),
    patient_code: str | None = Form(None),
    patient_birth_date: str | None = Form(None),
    patient_age: str | None = Form(None),
    patient_gender: str | None = Form(None),
    patient_phones_json: str | None = Form(None),
    patient_emails_json: str | None = Form(None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(400, "Файл не указан")

    ext = Path(file.filename).suffix.lower()
    if ext not in {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".webm", ".mp4"}:
        raise HTTPException(400, "Поддерживаются: mp3, wav, ogg, opus, m4a, webm, mp4")

    settings = get_settings()
    if not settings.mock_ai and not all((settings.yandex_api_key, settings.yandex_folder_id,
                                        settings.object_storage_bucket,
                                        settings.object_storage_access_key_id,
                                        settings.object_storage_secret_access_key)):
        raise HTTPException(503, "Обработка аудио пока не настроена. Обратитесь к администратору.")
    consultation_id = str(uuid4())
    dest_dir = settings.audio_dir / consultation_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"audio{ext}"
    if shutil.disk_usage(dest_dir).free < (settings.max_audio_upload_mb + 512) * 1024 * 1024:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(507, "На сервере недостаточно места для загрузки и обработки аудио")

    max_bytes = settings.max_audio_upload_mb * 1024 * 1024
    actual_bytes = 0
    try:
        with dest_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                actual_bytes += len(chunk)
                if actual_bytes > max_bytes:
                    raise HTTPException(413, f"Аудиофайл превышает лимит {settings.max_audio_upload_mb} МБ")
                out.write(chunk)
        if not actual_bytes:
            raise HTTPException(400, "Аудиофайл пуст")
        parsed_consultation_date = _parse_ddmmyyyy_to_date(consultation_date)
        if not parsed_consultation_date:
            raise HTTPException(400, "Дата консультации обязательна")
        normalized_consultation_type = _normalize_consultation_type(consultation_type)
        normalized_clinic_division = _required_text(clinic_division, "Подразделение")

        normalized_doctor_name = doctor_name.strip()
        if user["role"] in {"doctor", "supervisor"}:
            normalized_doctor_name = user["doctor_name"] or user["username"]
        elif not normalized_doctor_name:
            raise HTTPException(400, "Имя врача обязательно")
        normalized_patient_name = _required_text(patient_name, "Имя пациента")
        parsed_patient_birth_date = _parse_ddmmyyyy_to_date(patient_birth_date)
        parsed_patient_age = _parse_optional_int(patient_age)
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise

    consultation = Consultation(
        id=consultation_id,
        source_system=source_system,
        source_payload_json=source_payload_json,
        doctor_code=doctor_code,
        doctor_position=doctor_position,
        doctor_category=doctor_category,
        patient_code=patient_code,
        patient_birth_date=parsed_patient_birth_date,
        patient_age=parsed_patient_age,
        patient_gender=patient_gender,
        patient_phones_json=patient_phones_json,
        patient_emails_json=patient_emails_json,
        consultation_date=parsed_consultation_date,
        consultation_type=normalized_consultation_type,
        clinic_division=normalized_clinic_division,
        doctor_name=normalized_doctor_name,
        patient_name=normalized_patient_name,
        audio_path=str(dest_path),
        original_filename=file.filename,
        audio_size_bytes=actual_bytes,
        status="uploaded",
    )
    try:
        db.add(consultation)
        db.commit()
    except Exception:
        db.rollback()
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise

    queued = _enqueue_processing(db, consultation, consultation.processing_generation)

    return UploadResponse(
        id=consultation_id,
        status="processing" if queued else "uploaded",
        message="Запись загружена, идёт обработка" if queued else "Запись сохранена; обработка начнётся после восстановления очереди",
    )


@router.get("", response_model=list[ConsultationListItem])
def list_consultations(db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = select(Consultation).order_by(Consultation.created_at.desc())
    if not can_view_all_records(user):
        query = query.where(Consultation.doctor_name == user["doctor_name"])

    rows = db.scalars(query).all()
    return [
        ConsultationListItem(
            id=r.id,
            consultation_date=r.consultation_date,
            consultation_type=r.consultation_type,
            clinic_division=r.clinic_division,
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

    if row.storage_key:
        try:
            delete_audio(row.storage_key)
        except Exception as exc:
            logger.exception("Could not delete Object Storage audio id=%s", consultation_id)
            raise HTTPException(503, "Не удалось удалить аудио из хранилища") from exc
    db.delete(row)
    db.commit()

    _remove_consultation_files(consultation_id, audio_path, settings.audio_dir)

    return {"ok": True, "message": "Запись удалена"}


@router.post("/{consultation_id}/export-audio")
def export_consultation_audio(
    consultation_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if user["role"] != "admin":
        raise HTTPException(403, "Только для администратора")

    row = db.get(Consultation, consultation_id)
    if not row:
        raise HTTPException(404, "Запись не найдена")
    if not (get_settings().remote_audio_enabled and get_settings().remote_audio_auto_export):
        raise HTTPException(409, "Удалённое хранилище пока не настроено")
    if row.status != "ready" or row.remote_export_status != "failed":
        raise HTTPException(409, "Ручная выгрузка доступна после неудачных автоматических попыток")
    row.remote_export_status = "pending"
    row.remote_export_queued_at = datetime.utcnow()
    row.remote_export_lease_until = None
    row.remote_export_next_at = None
    row.remote_export_first_failure_at = None
    row.remote_export_attempts = 0
    row.remote_export_error = None
    db.commit()
    _enqueue_remote(export_audio_task, consultation_id)
    return {"ok": True, "message": "Выгрузка поставлена в очередь"}


@router.post("/{consultation_id}/restore-audio")
def restore_consultation_audio(
    consultation_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if user["role"] != "admin":
        raise HTTPException(403, "Только для администратора")
    row = db.get(Consultation, consultation_id)
    if not row:
        raise HTTPException(404, "Запись не найдена")
    if not get_settings().remote_audio_enabled or row.remote_export_status != "done" or not row.remote_audio_sha256:
        raise HTTPException(409, "Аудио не выгружено в удалённое хранилище")
    if Path(row.audio_path).exists():
        raise HTTPException(409, "Аудио уже находится на этом сервере")
    if row.remote_restore_status in {"pending", "restoring"}:
        raise HTTPException(409, "Восстановление уже выполняется")
    row.remote_restore_status = "pending"
    row.remote_restore_queued_at = datetime.utcnow()
    row.remote_restore_lease_until = None
    row.remote_restore_error = None
    db.commit()
    _enqueue_remote(restore_audio_task, consultation_id)
    return {"ok": True, "message": "Восстановление поставлено в очередь"}


@router.post("/{consultation_id}/retry", response_model=UploadResponse)
def retry_consultation_processing(
    consultation_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    row = db.get(Consultation, consultation_id)
    if not row:
        raise HTTPException(404, "Запись не найдена")
    if user["role"] != "admin":
        raise HTTPException(403, "Только для администратора")
    if row.status != "failed":
        raise HTTPException(409, "Ручная обработка доступна после неудачных автоматических попыток")

    if row.processing_stage == "submitting" or row.error_category == "submission_unknown":
        raise HTTPException(409, "ID операции SpeechKit может быть неизвестен. Обратитесь к администратору для проверки операции.")
    generation = reset_for_manual_retry(row)
    db.commit()
    queued = _enqueue_processing(db, row, generation)
    return UploadResponse(
        id=consultation_id,
        status="processing" if queued else "uploaded",
        message="Запись отправлена на повторную обработку" if queued else "Обработка начнётся после восстановления очереди",
    )


@router.post("/retry-all", response_model=BulkRetryResponse)
def retry_all_failed_consultations(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if user["role"] != "admin":
        raise HTTPException(403, "Только для администратора")
    if not BULK_RETRY_ENABLED:
        raise HTTPException(409, "Массовая повторная обработка временно отключена")
    rows = db.scalars(select(Consultation).where(
        Consultation.status.in_(("failed", "uploaded"))
    ).order_by(Consultation.created_at.asc())).all()
    selected = []
    skipped_uncertain = 0
    for row in rows:
        if row.processing_stage == "submitting" or row.error_category == "submission_unknown":
            skipped_uncertain += 1
            continue
        selected.append((row, reset_for_manual_retry(row)))
    db.commit()
    queued = sum(_enqueue_processing(db, row, generation) for row, generation in selected)
    return BulkRetryResponse(
        selected=len(selected), queued=queued,
        waiting_for_queue=len(selected) - queued, skipped_uncertain=skipped_uncertain,
    )


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
        consultation_type=row.consultation_type,
        clinic_division=row.clinic_division,
        doctor_name=row.doctor_name,
        patient_name=row.patient_name,
        duration_sec=row.duration_sec,
        overall_score=row.overall_score,
        status=row.status,
        error_message=_admin_error_message(row, user["role"]),
        processing_stage=row.processing_stage if user["role"] == "admin" else None,
        speechkit_operation_id=row.speechkit_operation_id if user["role"] == "admin" else None,
        retry_available=(user["role"] == "admin" and row.status == "failed"
                         and row.processing_stage != "submitting"
                         and row.error_category != "submission_unknown"),
        export_available=(user["role"] == "admin" and get_settings().remote_audio_enabled
                          and get_settings().remote_audio_auto_export
                          and row.status == "ready" and row.remote_export_status == "failed"),
        restore_available=(user["role"] == "admin" and get_settings().remote_audio_enabled
                           and row.remote_export_status == "done" and bool(row.remote_audio_sha256)
                           and not Path(row.audio_path).exists()
                           and row.remote_restore_status not in {"pending", "restoring"}),
        remote_export_status=row.remote_export_status if user["role"] == "admin" else None,
        remote_export_error=row.remote_export_error if user["role"] == "admin" else None,
        remote_restore_status=row.remote_restore_status if user["role"] == "admin" else None,
        remote_restore_error=row.remote_restore_error if user["role"] == "admin" else None,
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
