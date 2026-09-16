from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError

from backend.auth.repository import get_session_user, lock_session_user
from scheduler.repository import ActualWriteStats, upsert_actuals_detailed
from shared.models import ActualRecord


pytestmark = pytest.mark.mysql_integration


def test_http_sized_pool_times_out_and_recovers(mysql_test_engine: Engine) -> None:
    engine = create_engine(
        mysql_test_engine.url,
        future=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    first = engine.connect()
    try:
        started = time.monotonic()
        with pytest.raises(PoolTimeoutError):
            engine.connect()
        assert time.monotonic() - started < 1.0
    finally:
        first.close()
    try:
        with engine.connect() as recovered:
            assert recovered.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_session_read_does_not_wait_for_user_write_lock(
    mysql_test_engine: Engine,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    username = f"lock_{suffix}"
    token_hash = hashlib.sha256(username.encode("ascii")).digest()
    now = datetime.now(UTC).replace(tzinfo=None)
    with mysql_test_engine.begin() as connection:
        user_id = connection.execute(
            text(
                """
                INSERT INTO t_auth_users
                    (username, password_hash, role, status,
                     is_protected_admin, must_change_password,
                     password_changed_at)
                VALUES
                    (:username, :password_hash, 'user', 'active', 0, 0, :now)
                """
            ),
            {
                "username": username,
                "password_hash": "$argon2id$integration-placeholder",
                "now": now,
            },
        ).lastrowid
        connection.execute(
            text(
                """
                INSERT INTO t_auth_sessions (user_id, token_hash, expires_at)
                VALUES (:user_id, :token_hash, :expires_at)
                """
            ),
            {
                "user_id": user_id,
                "token_hash": token_hash,
                "expires_at": now + timedelta(hours=1),
            },
        )

    blocker = mysql_test_engine.connect()
    transaction = blocker.begin()
    try:
        blocker.execute(
            text("UPDATE t_auth_users SET full_name = 'pending' WHERE id = :id"),
            {"id": user_id},
        )
        with mysql_test_engine.connect() as reader:
            started = time.monotonic()
            session = get_session_user(reader, token_hash, now)
            elapsed = time.monotonic() - started
        assert session is not None
        assert session[0].username == username
        assert session[0].full_name is None
        assert elapsed < 0.75

        with mysql_test_engine.connect() as lock_waiter:
            lock_waiter.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            with pytest.raises(OperationalError):
                lock_session_user(lock_waiter, token_hash, now)
    finally:
        transaction.rollback()
        blocker.close()

    with mysql_test_engine.begin() as connection:
        assert lock_session_user(connection, token_hash, now) is not None


def test_actual_repeat_write_is_a_mysql_noop(mysql_test_engine: Engine) -> None:
    trade_date = f"2098-{uuid.uuid4().int % 12 + 1:02d}-15"
    record = ActualRecord("10Y", trade_date, 1.91, -1, 1)
    assert upsert_actuals_detailed(mysql_test_engine, [record]) == ActualWriteStats(
        attempted=1,
        inserted=1,
    )
    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE t_scheme_actuals
                SET updated_at = '2000-01-01 00:00:00'
                WHERE tenor = :tenor AND trade_date = :trade_date
                """
            ),
            {"tenor": record.tenor, "trade_date": record.trade_date},
        )

    assert upsert_actuals_detailed(mysql_test_engine, [record]) == ActualWriteStats(
        attempted=1,
        unchanged=1,
    )
    with mysql_test_engine.connect() as connection:
        updated_at = connection.execute(
            text(
                """
                SELECT updated_at FROM t_scheme_actuals
                WHERE tenor = :tenor AND trade_date = :trade_date
                """
            ),
            {"tenor": record.tenor, "trade_date": record.trade_date},
        ).scalar_one()
    assert updated_at == datetime(2000, 1, 1)


def test_concurrent_actual_inserts_report_one_insert_and_one_noop(
    mysql_test_engine: Engine,
) -> None:
    """两个事务都先观测到缺键时，仍只能有一个 inserted。"""
    trade_date = f"2096-{uuid.uuid4().int % 12 + 1:02d}-15"
    record = ActualRecord("7Y", trade_date, 1.87, -1, 1)
    observed_absent = threading.Barrier(2, timeout=5)
    guarded_connections: set[int] = set()
    guard = threading.Lock()

    def synchronize_initial_observation(
        connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split()).lower()
        if not (
            normalized.startswith(
                "select tenor, trade_date, close_yield, direction_1d, direction_5d "
                "from t_scheme_actuals"
            )
            and "for update" not in normalized
        ):
            return
        identity = id(connection)
        with guard:
            if identity in guarded_connections:
                return
            guarded_connections.add(identity)
        observed_absent.wait()

    event.listen(
        mysql_test_engine,
        "after_cursor_execute",
        synchronize_initial_observation,
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(upsert_actuals_detailed, mysql_test_engine, [record])
                for _ in range(2)
            ]
            stats = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(
            mysql_test_engine,
            "after_cursor_execute",
            synchronize_initial_observation,
        )

    assert sorted(
        (item.inserted, item.changed, item.unchanged) for item in stats
    ) == [(0, 0, 1), (1, 0, 0)]
    assert all(item.attempted == 1 for item in stats)
    with mysql_test_engine.connect() as connection:
        stored = connection.execute(
            text(
                """
                SELECT close_yield, direction_1d, direction_5d,
                       created_at, updated_at
                FROM t_scheme_actuals
                WHERE tenor = :tenor AND trade_date = :trade_date
                """
            ),
            {"tenor": record.tenor, "trade_date": record.trade_date},
        ).one()
    assert stored[0:3] == (record.close_yield, record.direction_1d, record.direction_5d)
    assert stored.created_at == stored.updated_at


def test_unique_key_competition_rolls_back_and_can_retry(
    mysql_test_engine: Engine,
) -> None:
    trade_date = f"2097-{uuid.uuid4().int % 12 + 1:02d}-15"
    params = {
        "tenor": "30Y",
        "trade_date": trade_date,
        "close_yield": 2.01,
    }
    statement = text(
        """
        INSERT INTO t_scheme_actuals
            (tenor, trade_date, close_yield, direction_1d, direction_5d)
        VALUES (:tenor, :trade_date, :close_yield, 1, -1)
        """
    )
    first = mysql_test_engine.connect()
    first_transaction = first.begin()
    try:
        first.execute(statement, params)
        with mysql_test_engine.connect() as competitor:
            competitor.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            with pytest.raises(OperationalError):
                with competitor.begin_nested():
                    competitor.execute(statement, params)
    finally:
        first_transaction.rollback()
        first.close()

    with mysql_test_engine.begin() as connection:
        assert connection.execute(
            text(
                """
                SELECT COUNT(*) FROM t_scheme_actuals
                WHERE tenor = :tenor AND trade_date = :trade_date
                """
            ),
            params,
        ).scalar_one() == 0
        connection.execute(statement, params)
