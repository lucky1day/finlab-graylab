from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError

from backend.auth import repository
from backend.auth.repository import AuthUser
from backend.auth.security import (
    UsernamePolicyError,
    digest_session_token,
    hash_password,
    new_session_token,
    normalize_username,
    validate_password,
    verify_dummy_password,
    verify_password,
)
from backend.db import current_http_request_context


SESSION_LIFETIME = timedelta(hours=12)
FULL_NAME_MAX_LENGTH = 100
ORGANIZATION_NAME_MAX_LENGTH = 200


class AuthError(RuntimeError):
    """可安全映射到固定 HTTP 错误码的认证域异常。"""

    def __init__(self, error_code: str, status_code: int) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: AuthUser
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionResult:
    user: AuthUser
    expires_at: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _cooldown(failure_count: int) -> timedelta | None:
    if failure_count >= 7:
        return timedelta(minutes=15)
    if failure_count == 6:
        return timedelta(minutes=5)
    if failure_count == 5:
        return timedelta(seconds=60)
    return None


def _session_token_hash(token: str | None) -> bytes:
    if not token:
        raise AuthError("not_authenticated", 401)
    try:
        return digest_session_token(token)
    except (UnicodeError, ValueError):
        raise AuthError("not_authenticated", 401) from None


def _profile_value(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise AuthError("invalid_profile", 400)
    return normalized


class AuthService:
    """认证事务与账户生命周期的唯一业务入口。"""

    def __init__(
        self,
        engine: Any,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._engine = engine
        self._now = now

    def login(self, username: str, password: str) -> LoginResult:
        try:
            normalized = normalize_username(username)
        except UsernamePolicyError:
            verify_dummy_password(password)
            raise AuthError("invalid_credentials", 401) from None
        now = self._now()
        invalid = False
        result: LoginResult | None = None
        with self._engine.begin() as connection:
            user = repository.lock_user_by_username(connection, normalized)
            if user is None:
                verify_dummy_password(password)
                raise AuthError("invalid_credentials", 401)
            if user.status != "active" or (
                user.login_not_before is not None
                and user.login_not_before > now
            ):
                verify_dummy_password(password)
                raise AuthError("invalid_credentials", 401)
            if not verify_password(user.password_hash, password):
                failure_count = user.failed_login_count + 1
                cooldown = _cooldown(failure_count)
                repository.record_login_failure(
                    connection,
                    user_id=user.id,
                    failed_login_count=failure_count,
                    login_not_before=(now + cooldown if cooldown else None),
                )
                invalid = True
            else:
                if user.failed_login_count or user.login_not_before is not None:
                    repository.clear_login_failures(connection, user.id)
                token = new_session_token()
                expires_at = now + SESSION_LIFETIME
                repository.create_session(
                    connection,
                    user_id=user.id,
                    token_hash=digest_session_token(token),
                    expires_at=expires_at,
                )
                result = LoginResult(
                    user=user, token=token, expires_at=expires_at
                )
        if invalid:
            raise AuthError("invalid_credentials", 401)
        assert result is not None
        return result

    def authenticate(self, token: str | None) -> AuthUser:
        return self.current_session(token).user

    def current_session(self, token: str | None) -> SessionResult:
        token_hash = _session_token_hash(token)
        request_context = current_http_request_context()
        if request_context is None:
            with self._engine.begin() as connection:
                session = repository.get_session_user(
                    connection, token_hash, self._now()
                )
        else:
            request_context.require_remaining("auth_pool_acquire")
            acquire_started_at = request_context.clock()
            try:
                connection = self._engine.connect()
            finally:
                request_context.pool_acquire_seconds += max(
                    0.0,
                    request_context.clock() - acquire_started_at,
                )
            try:
                request_context.require_remaining("auth_db")
                db_started_at = request_context.clock()
                try:
                    with connection.begin():
                        session = repository.get_session_user(
                            connection,
                            token_hash,
                            self._now(),
                        )
                finally:
                    request_context.db_seconds += max(
                        0.0,
                        request_context.clock() - db_started_at,
                    )
            finally:
                connection.close()
        if session is None:
            raise AuthError("not_authenticated", 401)
        user, expires_at = session
        return SessionResult(user=user, expires_at=expires_at)

    def logout(self, token: str | None) -> None:
        token_hash = _session_token_hash(token)
        now = self._now()
        with self._engine.begin() as connection:
            session = repository.lock_session_user(connection, token_hash, now)
            if session is None:
                raise AuthError("not_authenticated", 401)
            repository.revoke_session(
                connection,
                token_hash,
                now,
            )

    def change_password(
        self,
        token: str | None,
        current_password: str,
        new_password: str,
        request_id: str,
    ) -> bool:
        token_hash = _session_token_hash(token)
        now = self._now()
        with self._engine.begin() as connection:
            session = repository.lock_session_user(connection, token_hash, now)
            if session is None:
                raise AuthError("not_authenticated", 401)
            user, _expires_at = session
            if not verify_password(user.password_hash, current_password):
                raise AuthError("invalid_current_password", 400)
            validate_password(new_password)
            if new_password == current_password:
                return False
            repository.update_password(
                connection,
                user_id=user.id,
                password_hash=hash_password(new_password),
                must_change_password=False,
                now=now,
            )
            repository.revoke_all_sessions(connection, user.id, now)
            repository.insert_audit(
                connection,
                event_type="password_changed",
                actor_user_id=user.id,
                target_user_id=user.id,
                request_id=request_id,
                detail={},
            )
            return True

    def list_users(self, token: str | None) -> list[AuthUser]:
        token_hash = _session_token_hash(token)
        with self._engine.begin() as connection:
            session = repository.get_session_user(
                connection, token_hash, self._now()
            )
            if session is None:
                raise AuthError("not_authenticated", 401)
            actor, _expires_at = session
            if actor.role != "admin":
                raise AuthError("forbidden", 403)
            return repository.list_users(connection)

    def _require_admin(self, token: str | None) -> AuthUser:
        user = self.authenticate(token)
        if user.role != "admin":
            raise AuthError("forbidden", 403)
        return user

    def _lock_admin(self, connection: Any, token_hash: bytes, now: datetime) -> AuthUser:
        session = repository.lock_session_user(connection, token_hash, now)
        if session is None:
            raise AuthError("not_authenticated", 401)
        actor, _expires_at = session
        if actor.role != "admin":
            raise AuthError("forbidden", 403)
        return actor

    def create_user(
        self,
        token: str | None,
        *,
        username: str,
        initial_password: str,
        role: str,
        request_id: str,
        full_name: str | None = None,
        organization_name: str | None = None,
    ) -> AuthUser:
        token_hash = _session_token_hash(token)
        self._require_admin(token)
        normalized = normalize_username(username)
        password_hash = hash_password(initial_password)
        normalized_full_name = _profile_value(
            full_name,
            max_length=FULL_NAME_MAX_LENGTH,
        )
        normalized_organization = _profile_value(
            organization_name,
            max_length=ORGANIZATION_NAME_MAX_LENGTH,
        )
        if role not in {"admin", "user"}:
            raise AuthError("invalid_role", 400)
        now = self._now()
        try:
            with self._engine.begin() as connection:
                actor = self._lock_admin(
                    connection, token_hash, now
                )
                user_id = repository.create_user(
                    connection,
                    username=normalized,
                    password_hash=password_hash,
                    role=role,
                    full_name=normalized_full_name,
                    organization_name=normalized_organization,
                    actor_user_id=actor.id,
                    now=now,
                )
                repository.insert_audit(
                    connection,
                    event_type="user_created",
                    actor_user_id=actor.id,
                    target_user_id=user_id,
                    request_id=request_id,
                    detail={"role": role, "username": normalized},
                )
                created = repository.lock_user_by_id(connection, user_id)
                assert created is not None
                return created
        except IntegrityError as exc:
            raise AuthError("username_taken", 409) from exc

    def update_own_profile(
        self,
        token: str | None,
        *,
        full_name: str | None,
        organization_name: str | None,
        request_id: str,
    ) -> AuthUser:
        token_hash = _session_token_hash(token)
        normalized_full_name = _profile_value(
            full_name,
            max_length=FULL_NAME_MAX_LENGTH,
        )
        normalized_organization = _profile_value(
            organization_name,
            max_length=ORGANIZATION_NAME_MAX_LENGTH,
        )
        now = self._now()
        with self._engine.begin() as connection:
            session = repository.lock_session_user(connection, token_hash, now)
            if session is None:
                raise AuthError("not_authenticated", 401)
            user, _expires_at = session
            return self._apply_user_changes_locked(
                connection,
                actor=user,
                target=user,
                changes={
                    "full_name": normalized_full_name,
                    "organization_name": normalized_organization,
                },
                request_id=request_id,
                now=now,
            )

    @staticmethod
    def _apply_user_changes_locked(
        connection: Any,
        *,
        actor: AuthUser,
        target: AuthUser,
        changes: dict[str, str | None],
        request_id: str,
        now: datetime,
    ) -> AuthUser:
        """在调用方锁定的账户事务内应用显式字段，统一保护、审计与会话撤销。"""
        changed = {
            field: value for field, value in changes.items()
            if value != getattr(target, field)
        }
        if not changed:
            return target
        restricted_change = bool(changed.keys() & {"username", "role", "status"})
        if target.is_protected_admin and restricted_change:
            raise AuthError("protected_admin", 409)
        role = changes.get("role", target.role)
        status = changes.get("status", target.status)
        removes_active_admin = (
            target.role == "admin" and target.status == "active"
            and (role == "user" or status == "disabled")
        )
        if actor.id == target.id and (
            ("role" in changed and role == "user")
            or ("status" in changed and status == "disabled")
        ):
            raise AuthError("cannot_modify_self", 409)
        if (
            removes_active_admin
            and len(repository.lock_active_admin_ids(connection)) <= 1
        ):
            raise AuthError("last_active_admin", 409)

        def audit(field: str, before: Any, after: Any) -> None:
            repository.insert_audit(
                connection, event_type=f"user_{field}_changed",
                actor_user_id=actor.id, target_user_id=target.id,
                request_id=request_id, detail={"before": before, "after": after},
            )

        if "username" in changed:
            repository.update_username(connection, target.id, changed["username"])
            audit("username", target.username, changed["username"])
        if changed.keys() & {"full_name", "organization_name"}:
            before = {
                "full_name": target.full_name,
                "organization_name": target.organization_name,
            }
            after = {
                field: changes.get(field, value)
                for field, value in before.items()
            }
            repository.update_profile(connection, user_id=target.id, **after)
            audit("profile", before, after)
        if "role" in changed:
            repository.update_role(connection, target.id, role)
            audit("role", target.role, role)
        if "status" in changed:
            repository.update_status(
                connection, user_id=target.id, status=status,
                actor_user_id=actor.id, now=now,
            )
            audit("status", target.status, status)
        if restricted_change:
            repository.revoke_all_sessions(connection, target.id, now)
        updated = repository.lock_user_by_id(connection, target.id)
        assert updated is not None
        return updated

    def _mutate_user(
        self,
        token: str | None,
        *,
        user_id: int,
        request_id: str,
        changes: dict[str, str | None],
    ) -> AuthUser:
        """锁内复核管理员与目标，只修改调用入口明确提交的字段。"""
        token_hash = _session_token_hash(token)
        now = self._now()
        try:
            with self._engine.begin() as connection:
                actor = self._lock_admin(connection, token_hash, now)
                target = repository.lock_user_by_id(connection, user_id)
                if target is None:
                    raise AuthError("user_not_found", 404)
                return self._apply_user_changes_locked(
                    connection, actor=actor, target=target, changes=changes,
                    request_id=request_id, now=now,
                )
        except IntegrityError as exc:
            if "username" in changes:
                raise AuthError("username_taken", 409) from exc
            raise

    def reset_password(
        self,
        token: str | None,
        *,
        user_id: int,
        new_password: str,
        request_id: str,
    ) -> AuthUser:
        token_hash = _session_token_hash(token)
        self._require_admin(token)
        password_hash = hash_password(new_password)
        now = self._now()
        with self._engine.begin() as connection:
            actor = self._lock_admin(
                connection, token_hash, now
            )
            target = repository.lock_user_by_id(connection, user_id)
            if target is None:
                raise AuthError("user_not_found", 404)
            if target.id == actor.id:
                raise AuthError("cannot_modify_self", 409)
            if target.is_protected_admin:
                raise AuthError("protected_admin", 409)
            if verify_password(target.password_hash, new_password):
                return target
            repository.update_password(
                connection,
                user_id=target.id,
                password_hash=password_hash,
                must_change_password=False,
                now=now,
            )
            repository.revoke_all_sessions(connection, target.id, now)
            repository.insert_audit(
                connection,
                event_type="password_reset",
                actor_user_id=actor.id,
                target_user_id=target.id,
                request_id=request_id,
                detail={},
            )
            updated = repository.lock_user_by_id(connection, target.id)
            assert updated is not None
            return updated

    def edit_user(
        self,
        token: str | None,
        *,
        user_id: int,
        username: str,
        full_name: str | None,
        organization_name: str | None,
        role: str,
        status: str,
        request_id: str,
    ) -> AuthUser:
        """在一个事务中原子保存管理员用户编辑表单。"""
        self._require_admin(token)
        normalized_username = normalize_username(username)
        normalized_full_name = _profile_value(
            full_name,
            max_length=FULL_NAME_MAX_LENGTH,
        )
        normalized_organization = _profile_value(
            organization_name,
            max_length=ORGANIZATION_NAME_MAX_LENGTH,
        )
        if role not in {"admin", "user"}:
            raise AuthError("invalid_role", 400)
        if status not in {"active", "disabled"}:
            raise AuthError("invalid_status", 400)
        return self._mutate_user(
            token, user_id=user_id, request_id=request_id,
            changes={
                "username": normalized_username,
                "full_name": normalized_full_name,
                "organization_name": normalized_organization,
                "role": role,
                "status": status,
            },
        )
