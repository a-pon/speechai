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
