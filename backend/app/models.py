import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Consultation(Base):
    __tablename__ = "consultations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_system: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    doctor_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    doctor_position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    doctor_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    patient_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    patient_birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    patient_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    patient_gender: Mapped[str | None] = mapped_column(String(1), nullable=True)
    patient_phones_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    patient_emails_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    consultation_date: Mapped[date] = mapped_column(Date)
    doctor_name: Mapped[str] = mapped_column(String(255))
    patient_name: Mapped[str] = mapped_column(String(255))
    audio_path: Mapped[str] = mapped_column(String(512))
    original_filename: Mapped[str] = mapped_column(String(255))
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluation_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    segments: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="consultation",
        cascade="all, delete-orphan",
        order_by="TranscriptSegment.order_index",
    )


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    doctor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    consultation_id: Mapped[str] = mapped_column(String(36), ForeignKey("consultations.id"))
    speaker_role: Mapped[str] = mapped_column(String(16))
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer)

    consultation: Mapped["Consultation"] = relationship(back_populates="segments")
