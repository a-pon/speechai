from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    (settings.audio_dir.parent).mkdir(parents=True, exist_ok=True)
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(settings.database_url, connect_args=connect_args)


engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _migrate_sqlite() -> None:
    if not get_settings().database_url.startswith("sqlite"):
        return

    inspector = inspect(engine)
    if "consultations" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("consultations")}
    statements = []
    if "consultation_type" not in columns:
        statements.append("ALTER TABLE consultations ADD COLUMN consultation_type VARCHAR(32) NOT NULL DEFAULT 'primary_adult'")
    if "clinic_division" not in columns:
        statements.append("ALTER TABLE consultations ADD COLUMN clinic_division VARCHAR(255) NOT NULL DEFAULT ''")
    if "processing_attempts" not in columns:
        statements.append("ALTER TABLE consultations ADD COLUMN processing_attempts INTEGER NOT NULL DEFAULT 0")
    if "processing_generation" not in columns:
        statements.append("ALTER TABLE consultations ADD COLUMN processing_generation INTEGER NOT NULL DEFAULT 0")

    additions = {
        "processing_stage": "VARCHAR(32) NOT NULL DEFAULT 'prepare'",
        "stage_updated_at": "DATETIME",
        "lease_until": "DATETIME",
        "queued_at": "DATETIME",
        "audio_size_bytes": "INTEGER",
        "storage_key": "VARCHAR(512)",
        "speechkit_operation_id": "VARCHAR(255)",
        "speechkit_started_at": "DATETIME",
        "speechkit_last_poll_at": "DATETIME",
        "error_category": "VARCHAR(32)",
        "recognition_empty_polls": "INTEGER NOT NULL DEFAULT 0",
        "evaluation_chunks_json": "TEXT",
        "stage_first_failure_at": "DATETIME",
        "step_running": "INTEGER NOT NULL DEFAULT 0",
        "worker_interruptions": "INTEGER NOT NULL DEFAULT 0",
        "remote_export_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "remote_export_queued_at": "DATETIME",
        "remote_export_lease_until": "DATETIME",
        "remote_export_next_at": "DATETIME",
        "remote_export_first_failure_at": "DATETIME",
        "remote_export_attempts": "INTEGER NOT NULL DEFAULT 0",
        "remote_export_error": "TEXT",
        "remote_audio_path": "VARCHAR(1024)",
        "remote_audio_sha256": "VARCHAR(64)",
        "remote_exported_at": "DATETIME",
        "remote_restore_status": "VARCHAR(32)",
        "remote_restore_queued_at": "DATETIME",
        "remote_restore_lease_until": "DATETIME",
        "remote_restore_error": "TEXT",
    }
    statements += [f"ALTER TABLE consultations ADD COLUMN {name} {spec}" for name, spec in additions.items() if name not in columns]
    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def init_db() -> None:
    from app import models  # noqa: F401
    from app.auth import create_default_users

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()
    db = SessionLocal()
    try:
        create_default_users(db)
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
