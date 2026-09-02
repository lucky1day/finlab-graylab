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


SESSION_LIFETIME = timedelta(hours=12)


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
        with self._engine.begin() as connection:
            session = repository.lock_session_user(
                connection, token_hash, self._now()
            )
        if session is None:
            raise AuthError("not_authenticated", 401)
        user, expires_at = session
        return SessionResult(user=user, expires_at=expires_at)

    def logout(self, token: str | None) -> None:
        user = self.authenticate(token)
        assert token is not None
        with self._engine.begin() as connection:
            repository.revoke_session(
                connection, digest_session_token(token), self._now()
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
            self._lock_admin(
                connection,
                token_hash,
                self._now(),
            )
            return repository.list_users(connection)

    def _require_admin(self, token: str | None) -> AuthUser:
        user = self.authenticate(token)
        if user.must_change_password:
            raise AuthError("password_change_required", 403)
        if user.role != "admin":
            raise AuthError("forbidden", 403)
        return user

    def _lock_admin(self, connection: Any, token_hash: bytes, now: datetime) -> AuthUser:
        session = repository.lock_session_user(connection, token_hash, now)
        if session is None:
            raise AuthError("not_authenticated", 401)
        actor, _expires_at = session
        if actor.must_change_password:
            raise AuthError("password_change_required", 403)
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
    ) -> AuthUser:
        token_hash = _session_token_hash(token)
        self._require_admin(token)
        normalized = normalize_username(username)
        password_hash = hash_password(initial_password)
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

    def change_username(
        self,
        token: str | None,
        *,
        user_id: int,
        username: str,
        request_id: str,
    ) -> AuthUser:
        normalized = normalize_username(username)
        return self._mutate_user(
            token,
            user_id=user_id,
            request_id=request_id,
            operation="username",
            value=normalized,
        )

    def change_role(
        self,
        token: str | None,
        *,
        user_id: int,
        role: str,
        request_id: str,
    ) -> AuthUser:
        if role not in {"admin", "user"}:
            raise AuthError("invalid_role", 400)
        return self._mutate_user(
            token,
            user_id=user_id,
            request_id=request_id,
            operation="role",
            value=role,
        )

    def change_status(
        self,
        token: str | None,
        *,
        user_id: int,
        status: str,
        request_id: str,
    ) -> AuthUser:
        if status not in {"active", "disabled"}:
            raise AuthError("invalid_status", 400)
        return self._mutate_user(
            token,
            user_id=user_id,
            request_id=request_id,
            operation="status",
            value=status,
        )

    def _mutate_user(
        self,
        token: str | None,
        *,
        user_id: int,
        request_id: str,
        operation: str,
        value: str,
    ) -> AuthUser:
        token_hash = _session_token_hash(token)
        now = self._now()
        try:
            with self._engine.begin() as connection:
                actor = self._lock_admin(connection, token_hash, now)
                target = repository.lock_user_by_id(connection, user_id)
                if target is None:
                    raise AuthError("user_not_found", 404)
                current = getattr(target, operation)
                if current == value:
                    return target
                if target.is_protected_admin:
                    raise AuthError("protected_admin", 409)
                if operation in {"role", "status"} and actor.id == target.id:
                    if value in {"user", "disabled"}:
                        raise AuthError("cannot_modify_self", 409)
                if (
                    target.role == "admin"
                    and target.status == "active"
                    and (
                        (operation == "role" and value == "user")
                        or (operation == "status" and value == "disabled")
                    )
                    and len(repository.lock_active_admin_ids(connection)) <= 1
                ):
                    raise AuthError("last_active_admin", 409)
                if operation == "username":
                    repository.update_username(connection, target.id, value)
                elif operation == "role":
                    repository.update_role(connection, target.id, value)
                else:
                    repository.update_status(
                        connection,
                        user_id=target.id,
                        status=value,
                        actor_user_id=actor.id,
                        now=now,
                    )
                repository.revoke_all_sessions(connection, target.id, now)
                repository.insert_audit(
                    connection,
                    event_type=f"user_{operation}_changed",
                    actor_user_id=actor.id,
                    target_user_id=target.id,
                    request_id=request_id,
                    detail={"before": current, "after": value},
                )
                updated = repository.lock_user_by_id(connection, target.id)
                assert updated is not None
                return updated
        except IntegrityError as exc:
            if operation == "username":
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
                must_change_password=True,
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
