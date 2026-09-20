"""管理员路由：用户管理、角色与启停、模型配置。"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Query
from sqlmodel import func, select

from app.api.deps import AdminUser, DbSession
from app.core.errors import AppError
from app.core.utils import to_iso
from app.models.common import touch
from app.models.enums import AccountStatus, ProviderCapability, Role
from app.models.identity import User
from app.models.model_config import ModelConfig
from app.schemas.identity import UpdateUserRequest, UserEnvelope, UserPage
from app.schemas.model_config import (
    ModelConfigEnvelope,
    ModelConfigInput,
    ModelConfigList,
    ModelConfigOut,
)
from app.services import auth_service

router = APIRouter(prefix="/admin", tags=["admin"])


def model_config_out(config: ModelConfig) -> dict:
    try:
        spec = json.loads(config.spec_json or "{}")
    except json.JSONDecodeError:
        spec = {}
    return {
        "id": config.id,
        "provider": config.provider,
        "capability": config.capability.value,
        "productLabel": config.product_label,
        "endpointId": config.endpoint_id,
        "actualModel": config.actual_model,
        "enabled": config.enabled,
        "isDefault": config.is_default,
        "verifiedAt": to_iso(config.verified_at),
        "spec": spec,
        "priceAmount": str(config.price_amount),
        "priceCurrency": config.price_currency,
        "priceUnit": config.price_unit,
        "concurrencyLimit": config.concurrency_limit,
        "note": config.note,
    }


@router.get("/users", response_model=UserPage, summary="用户列表")
def list_users(
    _admin: AdminUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),  # noqa: N803 - 契约里的名字
    keyword: str | None = None,
) -> dict:
    statement = select(User)
    count_statement = select(func.count()).select_from(User)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        statement = statement.where(User.username.like(pattern))
        count_statement = count_statement.where(User.username.like(pattern))

    total = db.exec(count_statement).one()
    rows = db.exec(
        statement.order_by(User.created_at).offset((page - 1) * pageSize).limit(pageSize)
    ).all()
    return {
        "items": [auth_service.user_out(u) for u in rows],
        "total": int(total or 0),
        "page": page,
        "pageSize": pageSize,
    }


@router.patch("/users/{userId}", response_model=UserEnvelope, summary="调整角色、启停账号")
def update_user(
    userId: str,  # noqa: N803 - 契约里的名字
    payload: UpdateUserRequest,
    _admin: AdminUser,
    db: DbSession,
) -> dict:
    user = db.get(User, userId)
    if user is None:
        raise AppError("RESOURCE_NOT_FOUND", "用户不存在")

    next_role = user.role
    next_status = user.status
    if payload.role is not None:
        try:
            next_role = Role(payload.role)
        except ValueError as exc:
            raise AppError("INVALID_REQUEST", "角色取值非法") from exc
    if payload.status is not None:
        try:
            next_status = AccountStatus(payload.status)
        except ValueError as exc:
            raise AppError("INVALID_REQUEST", "账号状态取值非法") from exc

    removing_admin = user.role == Role.admin and (
        next_role != Role.admin or next_status != AccountStatus.active
    )
    if removing_admin and auth_service.count_active_admins(db, exclude_user_id=user.id) == 0:
        raise AppError(
            "LAST_ADMIN_REQUIRED",
            "必须保留至少一个启用的管理员",
            details={"userId": user.id},
        )

    role_changed = next_role != user.role
    status_changed = next_status != user.status

    user.role = next_role
    user.status = next_status
    if payload.displayName is not None:
        user.display_name = payload.displayName.strip()
    touch(user)
    db.add(user)

    # 角色改变或停用后，该账号已有会话立即失效
    if role_changed or (status_changed and next_status == AccountStatus.disabled):
        auth_service.revoke_user_sessions(db, user.id)
    db.commit()
    db.refresh(user)
    return {"user": auth_service.user_out(user)}


@router.get("/model-configs", response_model=ModelConfigList, summary="模型配置列表")
def list_model_configs(_admin: AdminUser, db: DbSession) -> dict:
    rows = db.exec(select(ModelConfig).order_by(ModelConfig.capability, ModelConfig.product_label)).all()
    return {"items": [model_config_out(c) for c in rows], "total": len(rows)}


def _apply_input(config: ModelConfig, payload: ModelConfigInput) -> None:
    try:
        config.capability = ProviderCapability(payload.capability)
    except ValueError as exc:
        raise AppError("INVALID_REQUEST", "模型能力取值非法") from exc
    try:
        config.price_amount = Decimal(payload.priceAmount or "0")
    except InvalidOperation as exc:
        raise AppError("INVALID_REQUEST", "价格必须是 decimal 字符串") from exc

    config.provider = payload.provider
    config.product_label = payload.productLabel
    config.endpoint_id = payload.endpointId
    config.actual_model = payload.actualModel
    config.enabled = payload.enabled
    config.is_default = payload.isDefault
    config.spec_json = json.dumps(payload.spec or {}, ensure_ascii=False)
    config.price_currency = payload.priceCurrency
    config.price_unit = payload.priceUnit
    config.concurrency_limit = payload.concurrencyLimit
    config.note = payload.note
    if payload.credentialRef:
        config.credential_ref = payload.credentialRef


@router.post(
    "/model-configs",
    status_code=201,
    response_model=ModelConfigEnvelope,
    summary="新增模型配置",
)
def create_model_config(
    payload: ModelConfigInput, _admin: AdminUser, db: DbSession
) -> dict:
    existing = db.exec(
        select(ModelConfig).where(
            ModelConfig.provider == payload.provider,
            ModelConfig.product_label == payload.productLabel,
        )
    ).first()
    if existing is not None:
        raise AppError("BUSINESS_RULE_VIOLATION", "同名模型档位已存在")
    config = ModelConfig(
        provider=payload.provider,
        capability=ProviderCapability.text,
        product_label=payload.productLabel,
    )
    _apply_input(config, payload)
    db.add(config)
    db.commit()
    db.refresh(config)
    return {"modelConfig": model_config_out(config)}


@router.patch(
    "/model-configs/{configId}",
    response_model=ModelConfigEnvelope,
    summary="修改模型配置",
)
def update_model_config(
    configId: str,  # noqa: N803 - 契约里的名字
    payload: ModelConfigInput,
    _admin: AdminUser,
    db: DbSession,
) -> dict:
    config = db.get(ModelConfig, configId)
    if config is None:
        raise AppError("RESOURCE_NOT_FOUND", "模型配置不存在")
    _apply_input(config, payload)
    touch(config)
    db.add(config)
    db.commit()
    db.refresh(config)
    return {"modelConfig": model_config_out(config)}
