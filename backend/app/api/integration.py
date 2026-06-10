from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.schemas import OneCConsultationIn, OneCConsultationOut, OneCDoctorIn, OneCPatientIn

router = APIRouter(prefix="/api/integration", tags=["integration"])


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    items = [item.strip() for item in value.replace("\n", ";").split(";")]
    return [item for item in items if item]


def _parse_ddmmyyyy(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return normalized


@router.get("/1c", response_model=OneCConsultationOut)
def read_1c_payload(
    consultation_date: str | None = None,
    doctor_code: str | None = None,
    doctor_full_name: str | None = None,
    doctor_position: str | None = None,
    doctor_category: str | None = None,
    patient_code: str | None = None,
    patient_full_name: str | None = None,
    patient_birth_date: str | None = None,
    patient_age: int | None = None,
    patient_gender: str | None = None,
    patient_phones: str | None = None,
    patient_emails: str | None = None,
    user=Depends(get_current_user),
):
    if user["role"] != "admin":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Только для администратора")

    return OneCConsultationOut(
        consultation_date=_parse_ddmmyyyy(consultation_date),
        doctor=OneCDoctorIn(
            code=doctor_code,
            full_name=doctor_full_name,
            position=doctor_position,
            category=doctor_category,
        ),
        patient=OneCPatientIn(
            code=patient_code,
            full_name=patient_full_name,
            birth_date=_parse_ddmmyyyy(patient_birth_date),
            age=patient_age,
            gender=patient_gender,
            phones=_split_list(patient_phones),
            emails=_split_list(patient_emails),
        ),
    )


@router.post("/1c/normalize", response_model=OneCConsultationOut)
def normalize_1c_payload(payload: OneCConsultationIn, user=Depends(get_current_user)):
    if user["role"] != "admin":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Только для администратора")

    return OneCConsultationOut(
        consultation_date=_parse_ddmmyyyy(payload.consultation_date),
        doctor=OneCDoctorIn(
            code=payload.doctor.code,
            full_name=payload.doctor.full_name,
            position=payload.doctor.position,
            category=payload.doctor.category,
        ),
        patient=payload.patient,
    )
