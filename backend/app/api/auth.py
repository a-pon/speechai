from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import (
    authenticate_user,
    build_doctor_login_link,
    clear_login_cookie,
    get_current_user,
    set_login_cookie,
)
from app.db import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class DoctorLinkResponse(BaseModel):
    username: str
    link: str


@router.post("/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = authenticate_user(db, payload.username, payload.password)
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


@router.get("/doctor-link", response_model=DoctorLinkResponse)
def doctor_link(request: Request, username: str, user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Только для администратора")

    link = build_doctor_login_link(username, str(request.base_url).rstrip("/"))
    if not link:
        raise HTTPException(status_code=404, detail="Пользователь врача не найден")

    return DoctorLinkResponse(username=username, link=link)
