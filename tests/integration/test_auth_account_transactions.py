"""在显式隔离 MySQL 中验证账户变更、审计与会话的共同事务。"""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from backend.auth import repository
from backend.auth.security import hash_password
from backend.auth.service import AuthError, AuthService


pytestmark = pytest.mark.mysql_integration
NOW = datetime(2098, 1, 1)


def _account(engine, *, role="user", protected=False):
    username = "auth_" + uuid.uuid4().hex[:16]
    with engine.begin() as connection:
        user_id = connection.execute(text("""
            INSERT INTO t_auth_users
                (username, password_hash, role, status, is_protected_admin,
                 password_changed_at, full_name, organization_name)
            VALUES (:username, :password_hash, :role, 'active', :protected,
                    :now, 'Original name', 'Original org')
        """), {
            "username": username, "password_hash": hash_password("Example123"),
            "role": role, "protected": protected, "now": NOW,
        }).lastrowid
    service = AuthService(engine, now=lambda: NOW)
    token = service.login(username, "Example123").token
    with engine.connect() as connection:
        user = repository.lock_user_by_id(connection, user_id)
    return service, user, token


def _edit(service, token, target, request_id, **changes):
    """通过唯一的管理员编辑入口提交完整表单，仅覆盖指定字段。"""
    form = {field: getattr(target, field) for field in (
        "username", "full_name", "organization_name", "role", "status")}
    form.update(changes)
    return service.edit_user(token, user_id=target.id, request_id=request_id, **form)


def _audit(engine, request_id):
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT event_type, detail FROM t_auth_audit_logs
            WHERE request_id=:request_id ORDER BY id
        """), {"request_id": request_id}).mappings().all()
    return [(row["event_type"], json.loads(row["detail"]) if isinstance(row["detail"], str)
             else row["detail"]) for row in rows]


@pytest.mark.parametrize("field,value", [
    ("username", None), ("role", "admin"), ("status", "disabled"),
])
def test_single_field_changes_preserve_unsubmitted_fields_and_revoke_sessions(
    mysql_test_engine, field, value,
):
    service, _actor, actor_token = _account(mysql_test_engine, role="admin")
    _, target, target_token = _account(mysql_test_engine)
    if field == "username":
        value = "renamed_" + uuid.uuid4().hex[:16]
    request_id = uuid.uuid4().hex
    updated = _edit(service, actor_token, target, request_id, **{field: value})
    assert getattr(updated, field) == value
    for untouched in {"username", "role", "status", "full_name", "organization_name"} - {field}:
        assert getattr(updated, untouched) == getattr(target, untouched)
    assert _audit(mysql_test_engine, request_id) == [
        ("user_" + field + "_changed", {"before": getattr(target, field), "after": value})
    ]
    with pytest.raises(AuthError, match="not_authenticated"):
        service.authenticate(target_token)

    # 恢复账户不会恢复已经撤销的会话。
    if field == "status":
        _edit(service, actor_token, updated, uuid.uuid4().hex, status="active")
        with pytest.raises(AuthError, match="not_authenticated"):
            service.authenticate(target_token)


def test_combined_edit_and_noop_keep_atomic_audit_and_revocation_contract(mysql_test_engine):
    service, _actor, actor_token = _account(mysql_test_engine, role="admin")
    _, target, target_token = _account(mysql_test_engine)
    request_id = uuid.uuid4().hex
    username = "edited_" + uuid.uuid4().hex[:16]
    kwargs = dict(user_id=target.id, username=username, full_name="New name",
                  organization_name="New org", role="admin", status="active")
    updated = service.edit_user(actor_token, request_id=request_id, **kwargs)
    assert (updated.username, updated.full_name, updated.organization_name, updated.role) == (
        username, "New name", "New org", "admin",
    )
    assert _audit(mysql_test_engine, request_id) == [
        ("user_username_changed", {"before": target.username, "after": username}),
        ("user_profile_changed", {"before": {"full_name": "Original name", "organization_name": "Original org"},
                                  "after": {"full_name": "New name", "organization_name": "New org"}}),
        ("user_role_changed", {"before": "user", "after": "admin"}),
    ]
    with pytest.raises(AuthError, match="not_authenticated"):
        service.authenticate(target_token)

    new_token = service.login(username, "Example123").token
    noop_request = uuid.uuid4().hex
    service.edit_user(actor_token, request_id=noop_request, **kwargs)
    assert _audit(mysql_test_engine, noop_request) == []
    assert service.authenticate(new_token).id == target.id


@pytest.mark.parametrize("entry", ["combined", "self"])
def test_profile_only_updates_keep_sessions_and_protected_admin_rules(mysql_test_engine, entry):
    service, actor, token = _account(mysql_test_engine, role="admin", protected=True)
    request_id = uuid.uuid4().hex
    kwargs = dict(full_name="Updated", organization_name=None, request_id=request_id)
    if entry == "combined":
        service.edit_user(token, user_id=actor.id, username=actor.username,
                          role="admin", status="active", **kwargs)
    else:
        service.update_own_profile(token, **kwargs)
    assert service.authenticate(token).full_name == "Updated"
    assert _audit(mysql_test_engine, request_id)[0][0] == "user_profile_changed"
    _, other_admin, other_token = _account(mysql_test_engine, role="admin")

    def protected_state():
        """读回目标及操作者的账户、会话与审计，拒绝操作不得留半份变更。"""
        with mysql_test_engine.connect() as connection:
            params = {"target": actor.id, "other": other_admin.id}
            return [connection.execute(text(statement), params).all() for statement in (
                "SELECT * FROM t_auth_users WHERE id IN (:target,:other) ORDER BY id",
                "SELECT * FROM t_auth_sessions WHERE user_id IN (:target,:other) ORDER BY id",
                "SELECT * FROM t_auth_audit_logs WHERE target_user_id IN (:target,:other) ORDER BY id",
            )]

    before = protected_state()
    for acting_token in (token, other_token):
        for values in [{"role": "user"},
                       {"username": actor.username, "full_name": "Rejected",
                        "organization_name": None, "role": "user", "status": "active"}]:
            rejected_request = uuid.uuid4().hex
            with pytest.raises(AuthError, match="protected_admin"):
                _edit(service, acting_token, actor, rejected_request, **values)
            assert _audit(mysql_test_engine, rejected_request) == []
            assert protected_state() == before
            assert service.authenticate(acting_token).status == "active"
    assert service.authenticate(token).full_name == "Updated"


@pytest.mark.parametrize("failure_stage", ["role", "revocation"])
def test_combined_edit_failure_rolls_back_fields_audit_and_sessions(mysql_test_engine, failure_stage):
    service, _actor, actor_token = _account(mysql_test_engine, role="admin")
    _, target, target_token = _account(mysql_test_engine)
    request_id = uuid.uuid4().hex

    def fail_write(_connection, _cursor, statement, _parameters, _context, _many):
        prefix = "UPDATE t_auth_users SET role" if failure_stage == "role" else "UPDATE t_auth_sessions"
        if statement.lstrip().startswith(prefix):
            raise RuntimeError("injected account write failure")

    event.listen(mysql_test_engine, "after_cursor_execute", fail_write)
    try:
        with pytest.raises(RuntimeError, match="injected account write failure"):
            service.edit_user(actor_token, user_id=target.id,
                              username="rollback_" + uuid.uuid4().hex[:16],
                              full_name="Should roll back", organization_name=None,
                              role="admin", status="active", request_id=request_id)
    finally:
        event.remove(mysql_test_engine, "after_cursor_execute", fail_write)
    assert service.authenticate(target_token) == target
    assert _audit(mysql_test_engine, request_id) == []


def test_concurrent_admin_disabling_cannot_commit_both_accounts_disabled(mysql_test_engine):
    service, first, first_token = _account(mysql_test_engine, role="admin")
    _, second, second_token = _account(mysql_test_engine, role="admin")
    barrier = Barrier(2, timeout=5)

    def disable(token, target):
        barrier.wait()
        try:
            _edit(service, token, target, uuid.uuid4().hex, status="disabled")
            return True
        except AuthError as error:
            assert error.error_code in {"not_authenticated", "last_active_admin"}
        except OperationalError as error:
            # InnoDB 可用死锁回滚中止其中一笔；失败事务不能留半份账户修改。
            assert error.orig.args[0] == 1213
        return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: disable(*args), [
            (first_token, second), (second_token, first),
        ]))
    assert sum(results) == 1
    with mysql_test_engine.connect() as connection:
        states = connection.execute(text("SELECT status FROM t_auth_users WHERE id IN (:first,:second)"),
                                    {"first": first.id, "second": second.id}).scalars().all()
    assert sorted(states) == ["active", "disabled"]
