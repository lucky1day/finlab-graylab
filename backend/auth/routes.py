from __future__ import annotations

import os
import re
import logging
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.repository import AuthUser
from backend.auth.security import PasswordPolicyError, UsernamePolicyError
from backend.auth.service import AuthError, AuthService, SessionResult
from backend.db import get_engine


COOKIE_NAME = "__Host-bfl-session"
SESSION_MAX_AGE_SECONDS = 43_200
MAX_JSON_BODY_BYTES = 8 * 1024
_REQUEST_ID_PATTERN = re.compile(r"[!-~]{1,128}\Z", re.ASCII)
_TRUSTED_ORIGINS = {
    "aliyun-gray": "http://localhost:18110",
    "mac3-production": "https://bond.finailab.cn",
}

router = APIRouter()
logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_StrictModel):
    username: str
    password: str


class EmptyRequest(_StrictModel):
    pass


class ChangePasswordRequest(_StrictModel):
    current_password: str
    new_password: str


class UpdateProfileRequest(_StrictModel):
    full_name: str | None = Field(default=None, max_length=100)
    organization_name: str | None = Field(default=None, max_length=200)


class CreateUserRequest(_StrictModel):
    username: str
    initial_password: str
    role: Literal["admin", "user"]
    full_name: str | None = Field(default=None, max_length=100)
    organization_name: str | None = Field(default=None, max_length=200)


class ChangeUsernameRequest(_StrictModel):
    user_id: int = Field(gt=0)
    username: str


class ChangeRoleRequest(_StrictModel):
    user_id: int = Field(gt=0)
    role: Literal["admin", "user"]


class ResetPasswordRequest(_StrictModel):
    user_id: int = Field(gt=0)
    new_password: str


class ChangeStatusRequest(_StrictModel):
    user_id: int = Field(gt=0)
    status: Literal["active", "disabled"]


class AdminUpdateProfileRequest(UpdateProfileRequest):
    user_id: int = Field(gt=0)


class EditUserRequest(UpdateProfileRequest):
    user_id: int = Field(gt=0)
    username: str
    role: Literal["admin", "user"]
    status: Literal["active", "disabled"]


def _request_id(request: Request) -> str:
    candidate = request.headers.get("x-request-id")
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


def _service() -> AuthService:
    return AuthService(get_engine())


def _token(
    session_token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> str | None:
    return session_token


async def require_safe_json_request(request: Request) -> None:
    """拒绝跨站、非 JSON、过大或缺少浏览器同源元数据的写请求。"""
    if request.headers.get("content-type") != "application/json":
        raise AuthError("invalid_content_type", 415)
    deployment_target = os.getenv("BFL_DEPLOYMENT_TARGET", "")
    expected_origin = _TRUSTED_ORIGINS.get(deployment_target)
    configured_origin = os.getenv("BFL_AUTH_TRUSTED_ORIGIN", "")
    if (
        expected_origin is None
        or configured_origin != expected_origin
        or request.headers.get("origin") != expected_origin
    ):
        raise AuthError("invalid_origin", 403)
    if request.headers.get("sec-fetch-site") != "same-origin":
        raise AuthError("invalid_request_site", 403)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_JSON_BODY_BYTES:
                raise AuthError("request_body_too_large", 413)
        except ValueError:
            raise AuthError("invalid_content_length", 400) from None
    if len(await request.body()) > MAX_JSON_BODY_BYTES:
        raise AuthError("request_body_too_large", 413)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=COOKIE_NAME,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def auth_error_response(_request: Request, exc: AuthError) -> JSONResponse:
    """认证失败只返回固定 error_code，并在 401 时清理 Cookie。"""
    response = JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code},
        headers={"Cache-Control": "no-store"},
    )
    if exc.status_code == 401:
        _clear_session_cookie(response)
    return response


def policy_error_response(
    _request: Request,
    exc: PasswordPolicyError | UsernamePolicyError,
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error_code": str(exc)},
        headers={"Cache-Control": "no-store"},
    )


def validation_error_response(
    request: Request,
    _exc: RequestValidationError,
) -> JSONResponse:
    """请求校验失败不得回显密码或原始输入。"""
    error_code = (
        "invalid_request"
        if request.url.path.startswith(("/api/auth/", "/api/admin/"))
        else "request_validation_failed"
    )
    return JSONResponse(
        status_code=422,
        content={"error_code": error_code},
        headers={"Cache-Control": "no-store"},
    )


def unhandled_error_response(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """未预期错误只记录固定阶段与异常类名。"""
    auth_path = request.url.path.startswith(("/api/auth/", "/api/admin/"))
    logger.error(
        "auth_request_failed failure_stage=unhandled exception_class=%s",
        type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error_code": (
                "auth_unavailable" if auth_path else "internal_server_error"
            )
        },
        headers={"Cache-Control": "no-store"},
    )


def require_authenticated_user(
    token: Annotated[str | None, Depends(_token)],
) -> AuthUser:
    return _service().authenticate(token)


def require_dashboard_user(
    user: Annotated[AuthUser, Depends(require_authenticated_user)],
) -> AuthUser:
    return user


@router.post(
    "/api/auth/login",
    dependencies=[Depends(require_safe_json_request)],
)
def login(payload: LoginRequest) -> JSONResponse:
    result = _service().login(payload.username, payload.password)
    response = JSONResponse(
        {
            "user": result.user.public_dict(),
            "expires_at": result.expires_at.isoformat(timespec="microseconds"),
        },
        headers={"Cache-Control": "no-store"},
    )
    _set_session_cookie(response, result.token)
    return response


@router.post(
    "/api/auth/logout",
    dependencies=[Depends(require_safe_json_request)],
)
def logout(
    _payload: EmptyRequest,
    token: Annotated[str | None, Depends(_token)],
) -> JSONResponse:
    _service().logout(token)
    response = JSONResponse(
        {"status": "ok"}, headers={"Cache-Control": "no-store"}
    )
    _clear_session_cookie(response)
    return response


@router.get("/api/auth/me")
def me(token: Annotated[str | None, Depends(_token)]) -> dict:
    result: SessionResult = _service().current_session(token)
    return {
        "user": result.user.public_dict(),
        "expires_at": result.expires_at.isoformat(timespec="microseconds"),
    }


@router.post(
    "/api/auth/change-password",
    dependencies=[Depends(require_safe_json_request)],
)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> JSONResponse:
    password_changed = _service().change_password(
        token,
        payload.current_password,
        payload.new_password,
        _request_id(request),
    )
    response = JSONResponse(
        {"status": "ok"}, headers={"Cache-Control": "no-store"}
    )
    if password_changed:
        _clear_session_cookie(response)
    return response


@router.post(
    "/api/auth/update-profile",
    dependencies=[Depends(require_safe_json_request)],
)
def update_profile(
    payload: UpdateProfileRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    user = _service().update_own_profile(
        token,
        full_name=payload.full_name,
        organization_name=payload.organization_name,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}


@router.get("/api/admin/users")
def users(token: Annotated[str | None, Depends(_token)]) -> dict:
    return {
        "users": [
            user.public_dict() for user in _service().list_users(token)
        ]
    }


@router.post(
    "/api/admin/users",
    dependencies=[Depends(require_safe_json_request)],
)
def create_user(
    payload: CreateUserRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    user = _service().create_user(
        token,
        username=payload.username,
        initial_password=payload.initial_password,
        role=payload.role,
        full_name=payload.full_name,
        organization_name=payload.organization_name,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}


@router.post(
    "/api/admin/users/change-username",
    dependencies=[Depends(require_safe_json_request)],
)
def change_username(
    payload: ChangeUsernameRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    user = _service().change_username(
        token,
        user_id=payload.user_id,
        username=payload.username,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}


@router.post(
    "/api/admin/users/change-role",
    dependencies=[Depends(require_safe_json_request)],
)
def change_role(
    payload: ChangeRoleRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    user = _service().change_role(
        token,
        user_id=payload.user_id,
        role=payload.role,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}


@router.post(
    "/api/admin/users/reset-password",
    dependencies=[Depends(require_safe_json_request)],
)
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    user = _service().reset_password(
        token,
        user_id=payload.user_id,
        new_password=payload.new_password,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}


@router.post(
    "/api/admin/users/change-status",
    dependencies=[Depends(require_safe_json_request)],
)
def change_status(
    payload: ChangeStatusRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    user = _service().change_status(
        token,
        user_id=payload.user_id,
        status=payload.status,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}


@router.post(
    "/api/admin/users/update-profile",
    dependencies=[Depends(require_safe_json_request)],
)
def admin_update_profile(
    payload: AdminUpdateProfileRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    user = _service().update_user_profile(
        token,
        user_id=payload.user_id,
        full_name=payload.full_name,
        organization_name=payload.organization_name,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}


@router.post(
    "/api/admin/users/edit",
    dependencies=[Depends(require_safe_json_request)],
)
def edit_user(
    payload: EditUserRequest,
    request: Request,
    token: Annotated[str | None, Depends(_token)],
) -> dict:
    """在一个事务中保存管理员用户编辑表单。"""
    user = _service().edit_user(
        token,
        user_id=payload.user_id,
        username=payload.username,
        full_name=payload.full_name,
        organization_name=payload.organization_name,
        role=payload.role,
        status=payload.status,
        request_id=_request_id(request),
    )
    return {"user": user.public_dict()}
