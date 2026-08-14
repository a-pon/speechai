import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import DoctorLinkToken, User

AUTH_COOKIE_NAME = "speechai_auth"


class UserInfo(TypedDict):
    username: str
    role: Literal["admin", "doctor"]
    doctor_name: str | None
    can_view_all_records: bool


DEFAULT_USERS: list[dict[str, str]] = [
    {"username": "admin", "password": "Q7m4Lp8Z", "role": "admin", "doctor_name": "admin"},
    {"username": "Кухтарская Татьяна", "password": "N6v2Ts5K", "role": "doctor", "doctor_name": "Кухтарская Татьяна"},
    {"username": "Кудзиева Тамара", "password": "H8r3Qp1M", "role": "doctor", "doctor_name": "Кудзиева Тамара"},
    {"username": "Иваненчук Иван", "password": "X4c9Wz2D", "role": "doctor", "doctor_name": "Иваненчук Иван"},
    {"username": "Корнилова Анастасия", "password": "P5t7Jn8A", "role": "doctor", "doctor_name": "Корнилова Анастасия"},
]

FULL_RECORD_ACCESS_USERS = {"Кухтарская Татьяна"}


def _password_hash(password: str) -> str:
    secret = get_settings().session_secret.encode("utf-8")
    return hashlib.sha256(secret + password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hmac.compare_digest(_password_hash(password), password_hash)


def build_password_hash(password: str) -> str:
    return _password_hash(password)


def create_default_users(db: Session) -> None:
    existing = {row.username for row in db.query(User.username).all()}
    for entry in DEFAULT_USERS:
        if entry["username"] in existing:
            continue
        db.add(
            User(
                username=entry["username"],
                role=entry["role"],
                doctor_name=entry["doctor_name"] or None,
                password_hash=build_password_hash(entry["password"]),
            )
        )
    db.commit()


def seed_demo_password() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def authenticate_user(db: Session, username: str, password: str) -> UserInfo | None:
    normalized_username = username.strip()
    user = db.get(User, normalized_username)
    if not user or not verify_password(password, user.password_hash):
        return None

    return {
        "username": user.username,
        "role": user.role,  # type: ignore[return-value]
        "doctor_name": user.doctor_name,
        "can_view_all_records": user.username in FULL_RECORD_ACCESS_USERS or user.role == "admin",
    }


def _normalize_user(user: UserInfo | dict) -> UserInfo | None:
    if not isinstance(user, dict):
        return None
    username = str(user.get("username") or "").strip()
    role = user.get("role")
    if role not in {"admin", "doctor"} or not username:
        return None
    doctor_name = user.get("doctor_name")
    if doctor_name is not None:
        doctor_name = str(doctor_name).strip() or None
    return {
        "username": username,
        "role": role,
        "doctor_name": doctor_name,
        "can_view_all_records": bool(user.get("can_view_all_records", False) or username in FULL_RECORD_ACCESS_USERS or role == "admin"),
    }


def _sign(payload: str) -> str:
    secret = get_settings().session_secret.encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_user(user: UserInfo) -> str:
    payload = json.dumps(user, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    signature = _sign(encoded)
    return f"{encoded}.{signature}"


def _build_doctor_link_payload(payload: dict[str, object] | None) -> str | None:
    if not payload:
        return None
    cleaned = {key: value for key, value in payload.items() if value not in (None, "")}
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _issue_doctor_link_token(
    db: Session,
    username: str,
    next_path: str | None = None,
    payload: dict[str, object] | None = None,
) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    next_value = next_path or "/"
    payload_json = _build_doctor_link_payload(payload)
    for _ in range(10):
        token = secrets.token_urlsafe(12)
        if db.get(DoctorLinkToken, token):
            continue
        db.add(
            DoctorLinkToken(
                token=token,
                username=username,
                next_path=next_value,
                payload_json=payload_json,
                expires_at=expires_at,
            )
        )
        db.commit()
        return token
    raise RuntimeError("Не удалось сгенерировать токен входа врача")


def build_doctor_login_link(
    username: str,
    base_url: str | None = None,
    next_path: str | None = None,
    payload: dict[str, object] | None = None,
) -> str | None:
    db = SessionLocal()
    try:
        user = db.get(User, username.strip())
        if not user or user.role != "doctor":
            return None
        token = _issue_doctor_link_token(db, username.strip(), next_path=next_path, payload=payload)
    finally:
        db.close()
    path = f"/api/integration/link-doctor?token={token}"
    return f"{base_url.rstrip('/')}{path}" if base_url else path


def _load_doctor_link_token(db: Session, token: str | None) -> DoctorLinkToken | None:
    if not token:
        return None
    link = db.get(DoctorLinkToken, token.strip())
    if not link:
        return None
    expires_at = link.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None
    return link


def _decode_legacy_doctor_link_token(token: str | None) -> tuple[str | None, str]:
    if not token or "." not in token:
        return None, "/"
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(encoded), signature):
        return None, "/"
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None, "/"
    username = str(payload.get("username") or "").strip()
    next_path = str(payload.get("next") or "/").strip() or "/"
    expires_at_raw = str(payload.get("expires_at") or "").strip()
    if not username or not expires_at_raw:
        return None, "/"
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        return None, "/"
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None, "/"
    return username, next_path


def _decode_user(cookie_value: str | None) -> UserInfo | None:
    if not cookie_value or "." not in cookie_value:
        return None

    encoded, signature = cookie_value.rsplit(".", 1)
    if not hmac.compare_digest(_sign(encoded), signature):
        return None

    try:
        payload = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        user = json.loads(payload)
    except (ValueError, json.JSONDecodeError):
        return None

    return _normalize_user(user)


def set_login_cookie(response: Response, user: UserInfo) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _encode_user(user),
        httponly=True,
        samesite="lax",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> UserInfo:
    user = _decode_user(request.cookies.get(AUTH_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    db_user = db.get(User, user["username"])
    if not db_user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return {
        "username": db_user.username,
        "role": db_user.role,  # type: ignore[return-value]
        "doctor_name": db_user.doctor_name,
        "can_view_all_records": db_user.username in FULL_RECORD_ACCESS_USERS or db_user.role == "admin",
    }


def can_view_all_records(user: UserInfo) -> bool:
    return bool(user.get("can_view_all_records") or user["role"] == "admin")


def can_access_doctor_record(user: UserInfo, doctor_name: str) -> bool:
    return can_view_all_records(user) or user["doctor_name"] == doctor_name


def login_doctor_by_token(token: str | None, db: Session) -> UserInfo | None:
    link = _load_doctor_link_token(db, token)
    if link:
        user = db.get(User, link.username)
    else:
        username, _next_path = _decode_legacy_doctor_link_token(token)
        if not username:
            return None
        user = db.get(User, username)
    if not user or user.role != "doctor":
        return None
    return {
        "username": user.username,
        "role": user.role,  # type: ignore[return-value]
        "doctor_name": user.doctor_name,
        "can_view_all_records": user.username in FULL_RECORD_ACCESS_USERS or user.role == "admin",
    }


def token_next_path(token: str | None, db: Session | None = None) -> str:
    if db is None:
        db = SessionLocal()
        try:
            link = _load_doctor_link_token(db, token)
            if not link:
                _username, next_path = _decode_legacy_doctor_link_token(token)
                return next_path or "/"
            return link.next_path or "/"
        finally:
            db.close()
    link = _load_doctor_link_token(db, token)
    if not link:
        _username, next_path = _decode_legacy_doctor_link_token(token)
        return next_path or "/"
    return link.next_path or "/"


def token_payload(token: str | None, db: Session) -> dict[str, object]:
    link = _load_doctor_link_token(db, token)
    if not link or not link.payload_json:
        return {}
    try:
        payload = json.loads(link.payload_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
