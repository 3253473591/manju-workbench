"""Identity 服务：注册、登录、会话、本地初始化管理员。"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import (
    hash_ip,
    hash_password,
    hash_token,
    new_session_token,
    verify_password,
)
from app.core.utils import to_iso, utcnow
from app.models.enums import AccountStatus, Role
from app.models.identity import Session as SessionModel
from app.models.identity import User


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        "status": user.status.value if hasattr(user.status, "value") else str(user.status),
        "lastLoginAt": to_iso(user.last_login_at),
        "createdAt": to_iso(user.created_at),
    }


def find_active_session(db: Session, token_hash: str) -> SessionModel | None:
    record = db.exec(
        select(SessionModel).where(SessionModel.token_hash == token_hash)
    ).first()
    if record is None or record.revoked_at is not None:
        return None
    if record.expires_at <= utcnow():
        return None
    return record


def register(db: Session, *, username: str, password: str, display_name: str) -> User:
    username = username.strip()
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        raise AppError(
            "PASSWORD_POLICY_FAILED",
            f"密码至少 {settings.PASSWORD_MIN_LENGTH} 位",
            details={"minLength": settings.PASSWORD_MIN_LENGTH},
        )
    existing = db.exec(select(User).where(User.username == username)).first()
    if existing is not None:
        raise AppError("REGISTER_USERNAME_EXISTS", "用户名已被占用")

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name.strip() or username,
        # 开放注册，角色固定为 user
        role=Role.user,
        status=AccountStatus.active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login(
    db: Session,
    *,
    username: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str, str]:
    user = db.exec(select(User).where(User.username == username.strip())).first()
    if user is None or not verify_password(password, user.password_hash):
        raise AppError("AUTH_INVALID", "用户名或密码错误")
    if user.status != AccountStatus.active:
        raise AppError("ACCOUNT_DISABLED", "账号已被停用")

    token = new_session_token()
    expires_at = utcnow() + timedelta(hours=settings.SESSION_TTL_HOURS)
    db.add(
        SessionModel(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=expires_at,
            ip_hash=hash_ip(ip),
            user_agent=(user_agent or "")[:512] or None,
        )
    )
    user.last_login_at = utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, token, to_iso(expires_at) or ""


def logout(db: Session, record: SessionModel | None) -> None:
    if record is None:
        return
    record.revoked_at = utcnow()
    db.add(record)
    db.commit()


def revoke_user_sessions(db: Session, user_id: str) -> int:
    """角色变更或停用时吊销该账号所有未吊销会话。"""
    records = db.exec(
        select(SessionModel).where(
            SessionModel.user_id == user_id, SessionModel.revoked_at.is_(None)  # type: ignore[union-attr]
        )
    ).all()
    now = utcnow()
    for record in records:
        record.revoked_at = now
        db.add(record)
    return len(records)


def count_active_admins(db: Session, *, exclude_user_id: str | None = None) -> int:
    statement = select(User).where(
        User.role == Role.admin, User.status == AccountStatus.active
    )
    users = db.exec(statement).all()
    return len([u for u in users if u.id != exclude_user_id])


def ensure_bootstrap_admin(db: Session) -> User | None:
    """首次启动且不存在管理员时 seed 一个；已存在则不覆盖。"""
    existing = db.exec(select(User).where(User.role == Role.admin)).first()
    if existing is not None:
        return None
    admin = User(
        username=settings.BOOTSTRAP_ADMIN_USERNAME,
        password_hash=hash_password(settings.BOOTSTRAP_ADMIN_PASSWORD),
        display_name=settings.BOOTSTRAP_ADMIN_DISPLAY_NAME,
        role=Role.admin,
        status=AccountStatus.active,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin
