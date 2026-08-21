from datetime import date, datetime

from pydantic import BaseModel, Field


class TranscriptSegmentOut(BaseModel):
    speaker_role: str
    start_ms: int
    end_ms: int
    text: str

    class Config:
        from_attributes = True


class ConsultationListItem(BaseModel):
    id: str
    consultation_date: date
    consultation_type: str
    clinic_division: str
    doctor_name: str
    patient_name: str
    duration_sec: int | None
    overall_score: float | None
    status: str
    created_at: datetime


class ConsultationDetail(BaseModel):
    id: str
    consultation_date: date
    consultation_type: str
    clinic_division: str
    doctor_name: str
    patient_name: str
    duration_sec: int | None
    overall_score: float | None
    status: str
    error_message: str | None
    evaluation_report: str | None
    transcript_text: str | None
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)
    created_at: datetime
    processed_at: datetime | None


class UploadResponse(BaseModel):
    id: str
    status: str
    message: str


class AuthUserOut(BaseModel):
    username: str
    role: str
    doctor_name: str | None


class UserCreateIn(BaseModel):
    username: str
    role: str
    password: str
    doctor_name: str | None = None


class UserUpdateIn(BaseModel):
    role: str | None = None
    password: str | None = None
    doctor_name: str | None = None


class UserOut(BaseModel):
    username: str
    role: str
    doctor_name: str | None
    login_link: str | None = None


class OneCDoctorIn(BaseModel):
    code: str | None = None
    full_name: str | None = None
    position: str | None = None
    category: str | None = None


class OneCPatientIn(BaseModel):
    code: str | None = None
    full_name: str | None = None
    birth_date: str | None = None
    age: int | None = None
    gender: str | None = None
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)


class OneCConsultationIn(BaseModel):
    consultation_date: str | None = None
    consultation_type: str | None = None
    clinic_division: str | None = None
    doctor: OneCDoctorIn
    patient: OneCPatientIn


class OneCConsultationOut(BaseModel):
    consultation_date: str | None
    consultation_type: str | None = None
    clinic_division: str | None = None
    doctor: OneCDoctorIn
    patient: OneCPatientIn
