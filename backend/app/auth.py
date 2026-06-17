import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict
from urllib.parse import urlencode

from fastapi import HTTPException, Request, Response

from app.config import get_settings

AUTH_COOKIE_NAME = "speechai_auth"


class UserInfo(TypedDict):
    username: str
    role: Literal["admin", "doctor"]
    doctor_name: str | None


USERS: dict[str, dict[str, str]] = {
    "admin": {"password": "adm1n", "role": "admin", "doctor_name": ""},
    "doctor": {"password": "doctor", "role": "doctor", "doctor_name": "doctor"},
}


def authenticate_user(username: str, password: str) -> UserInfo | None:
    normalized_username = username.strip()
    user = USERS.get(normalized_username)
    if not user or user["password"] != password:
        return None

    doctor_name = user["doctor_name"] or None
    return {
        "username": normalized_username,
        "role": user["role"],  # type: ignore[return-value]
        "doctor_name": doctor_name,
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
    }


def _sign(payload: str) -> str:
    secret = get_settings().session_secret.encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_user(user: UserInfo) -> str:
    payload = json.dumps(user, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    signature = _sign(encoded)
    return f"{encoded}.{signature}"


def _build_login_token(username: str, expires_at: datetime) -> str:
    payload = {
        "username": username,
        "expires_at": expires_at.isoformat(),
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    signature = _sign(encoded)
    return f"{encoded}.{signature}"


def build_doctor_login_link(username: str, base_url: str | None = None) -> str | None:
    user = USERS.get(username.strip())
    if not user or user["role"] != "doctor":
        return None
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    token = _build_login_token(username.strip(), expires_at)
    query = urlencode({"token": token})
    path = f"/api/auth/link-doctor?{query}"
    return f"{base_url.rstrip('/')}{path}" if base_url else path


def _decode_login_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(encoded), signature):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    username = str(payload.get("username") or "").strip()
    expires_at_raw = str(payload.get("expires_at") or "").strip()
    if not username or not expires_at_raw:
        return None
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None
    return username


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


def get_current_user(request: Request) -> UserInfo:
    user = _decode_user(request.cookies.get(AUTH_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def can_access_doctor_record(user: UserInfo, doctor_name: str) -> bool:
    return user["role"] == "admin" or user["doctor_name"] == doctor_name


def login_doctor_by_token(token: str | None) -> UserInfo | None:
    username = _decode_login_token(token)
    if not username:
        return None
    user = USERS.get(username)
    if not user or user["role"] != "doctor":
        return None
    doctor_name = user["doctor_name"] or None
    return {
        "username": username,
        "role": user["role"],  # type: ignore[return-value]
        "doctor_name": doctor_name,
    }
