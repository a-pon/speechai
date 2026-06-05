import base64
import hashlib
import hmac
import json
from typing import Literal, TypedDict

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


def _sign(payload: str) -> str:
    secret = get_settings().session_secret.encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_user(user: UserInfo) -> str:
    payload = json.dumps(user, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    signature = _sign(encoded)
    return f"{encoded}.{signature}"


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

    if user.get("role") not in {"admin", "doctor"}:
        return None
    return user


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
