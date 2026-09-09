from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import DOCTOR_ROLES, USER_ROLES, build_doctor_login_link, build_existing_doctor_login_link, build_password_hash, get_current_user
from app.db import get_db
from app.models import User
from app.schemas import UserCreateIn, UserOut, UserUpdateIn

router = APIRouter(prefix="/api/users", tags=["users"])


def _to_out(request: Request, user: User, db: Session, ensure_login_link: bool = False) -> UserOut:
    login_link = None
    if user.role in DOCTOR_ROLES:
        link_builder = build_doctor_login_link if ensure_login_link else build_existing_doctor_login_link
        login_link = link_builder(user.username, str(request.base_url).rstrip("/"), db=db)
    return UserOut(
        username=user.username,
        role=user.role,
        doctor_name=user.doctor_name,
        login_link=login_link,
    )


def _require_admin(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user


@router.get("", response_model=list[UserOut])
def list_users(request: Request, db: Session = Depends(get_db), _user=Depends(_require_admin)):
    users = db.query(User).order_by(User.role, User.username).all()
    return [_to_out(request, user, db) for user in users]


@router.post("", response_model=UserOut)
def create_user(payload: UserCreateIn, request: Request, db: Session = Depends(get_db), _user=Depends(_require_admin)):
    username = payload.username.strip()
    if not username:
        raise HTTPException(400, "Имя пользователя обязательно")
    if db.get(User, username):
        raise HTTPException(409, "Пользователь уже существует")
    if payload.role not in USER_ROLES:
        raise HTTPException(400, "Недопустимая роль")
    doctor_name = payload.doctor_name.strip() if payload.doctor_name else ""
    if not doctor_name:
        raise HTTPException(400, "Имя врача обязательно")
    if not payload.password.strip():
        raise HTTPException(400, "Пароль обязателен")

    user = User(
        username=username,
        role=payload.role,
        doctor_name=doctor_name,
        password_hash=build_password_hash(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _to_out(request, user, db, ensure_login_link=True)


@router.patch("/{username}", response_model=UserOut)
def update_user(
    username: str,
    payload: UserUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(_require_admin),
):
    user = db.get(User, username)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if payload.role is not None:
        if payload.role not in USER_ROLES:
            raise HTTPException(400, "Недопустимая роль")
        user.role = payload.role
    if payload.doctor_name is not None:
        user.doctor_name = payload.doctor_name.strip() or None
    if payload.password:
        user.password_hash = build_password_hash(payload.password)
    if not user.doctor_name:
        raise HTTPException(400, "Имя врача обязательно")
    db.commit()
    db.refresh(user)
    return _to_out(request, user, db, ensure_login_link=True)


@router.delete("/{username}")
def delete_user(username: str, db: Session = Depends(get_db), _user=Depends(_require_admin)):
    user = db.get(User, username)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.username == "admin":
        raise HTTPException(400, "Нельзя удалить admin")
    db.delete(user)
    db.commit()
    return {"ok": True}
