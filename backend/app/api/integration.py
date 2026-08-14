from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app.auth import build_doctor_login_link, get_current_user, login_doctor_by_token, set_login_cookie, token_next_path
from app.db import get_db
from app.schemas import OneCConsultationIn, OneCConsultationOut, OneCDoctorIn, OneCPatientIn

router = APIRouter(prefix="/api/integration", tags=["integration"])


def _repair_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if "Ð" in text or "Ã" in text:
        try:
            return text.encode("latin1").decode("utf-8")
        except UnicodeError:
            return text
    return text


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    items = [_repair_text(item) or "" for item in value.replace("\n", ";").split(";")]
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


def _consultation_query_params(
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
) -> dict[str, str | int]:
    return {
        key: value
        for key, value in {
            "consultation_date": consultation_date,
            "doctor_code": doctor_code,
            "doctor_full_name": doctor_full_name,
            "doctor_position": doctor_position,
            "doctor_category": doctor_category,
            "patient_code": patient_code,
            "patient_full_name": patient_full_name,
            "patient_birth_date": patient_birth_date,
            "patient_age": patient_age,
            "patient_gender": patient_gender,
            "patient_phones": patient_phones,
            "patient_emails": patient_emails,
        }.items()
        if value not in (None, "")
    }


@router.get("/1c-link")
def open_1c_link(
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
):
    params = {
        key: value
        for key, value in {
            "consultation_date": consultation_date,
            "doctor_code": doctor_code,
            "doctor_full_name": doctor_full_name,
            "doctor_position": doctor_position,
            "doctor_category": doctor_category,
            "patient_code": patient_code,
            "patient_full_name": patient_full_name,
            "patient_birth_date": patient_birth_date,
            "patient_age": patient_age,
            "patient_gender": patient_gender,
            "patient_phones": patient_phones,
            "patient_emails": patient_emails,
        }.items()
        if value not in (None, "")
    }
    return RedirectResponse(url="/?" + urlencode(params, doseq=True), status_code=307)


@router.get("/link-doctor")
def link_doctor(
    username: str | None = None,
    token: str | None = None,
    next: str = "/",
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
    db=Depends(get_db),
):
    params = _consultation_query_params(
        consultation_date=consultation_date,
        doctor_code=doctor_code,
        doctor_full_name=doctor_full_name,
        doctor_position=doctor_position,
        doctor_category=doctor_category,
        patient_code=patient_code,
        patient_full_name=patient_full_name,
        patient_birth_date=patient_birth_date,
        patient_age=patient_age,
        patient_gender=patient_gender,
        patient_phones=patient_phones,
        patient_emails=patient_emails,
    )

    if not token:
        if not username:
            raise HTTPException(status_code=400, detail="Требуется username или token")
        next_path = next or "/"
        link = build_doctor_login_link(username, None, next_path=next_path)
        if not link:
            raise HTTPException(404, "Пользователь врача не найден")
        if params:
            link = f"{link}&{urlencode(params, doseq=True)}"
        return RedirectResponse(url=link, status_code=307)

    user = login_doctor_by_token(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Ссылка недействительна или устарела")

    target = next or token_next_path(token) or "/"
    if params:
        target = f"{target}{'&' if '?' in target else '?'}{urlencode(params, doseq=True)}"

    redirect = RedirectResponse(url=target, status_code=302)
    set_login_cookie(redirect, user)
    return redirect


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
        consultation_date=_parse_ddmmyyyy(_repair_text(consultation_date)),
        doctor=OneCDoctorIn(
            code=_repair_text(doctor_code),
            full_name=_repair_text(doctor_full_name),
            position=_repair_text(doctor_position),
            category=_repair_text(doctor_category),
        ),
        patient=OneCPatientIn(
            code=_repair_text(patient_code),
            full_name=_repair_text(patient_full_name),
            birth_date=_parse_ddmmyyyy(_repair_text(patient_birth_date)),
            age=patient_age,
            gender=_repair_text(patient_gender),
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
        consultation_date=_parse_ddmmyyyy(_repair_text(payload.consultation_date)),
        doctor=OneCDoctorIn(
            code=_repair_text(payload.doctor.code),
            full_name=_repair_text(payload.doctor.full_name),
            position=_repair_text(payload.doctor.position),
            category=_repair_text(payload.doctor.category),
        ),
        patient=OneCPatientIn(
            code=_repair_text(payload.patient.code),
            full_name=_repair_text(payload.patient.full_name),
            birth_date=_parse_ddmmyyyy(_repair_text(payload.patient.birth_date)),
            age=payload.patient.age,
            gender=_repair_text(payload.patient.gender),
            phones=[_repair_text(item) or "" for item in payload.patient.phones if _repair_text(item)],
            emails=[_repair_text(item) or "" for item in payload.patient.emails if _repair_text(item)],
        ),
    )
