from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.auth import authenticate_user, clear_login_cookie, get_current_user, set_login_cookie

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(payload: LoginRequest, response: Response):
    user = authenticate_user(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    set_login_cookie(response, user)
    return user


@router.post("/logout")
def logout(response: Response):
    clear_login_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(user=Depends(get_current_user)):
    return user
