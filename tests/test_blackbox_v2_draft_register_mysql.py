from __future__ import annotations

import os
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, text

from tests.mysql_fixtures import temporary_mysql


_SCHEMA_PATTERN = re.compile(r"bfl_real_replay_[0-9a-f]{20}")


def _draft_config():
    return SimpleNamespace(
        scheme_id="draft_mysql_trial",
        scheme_version="1" * 64,
        runtime_type="blackbox_v2",
        status="paused",
        version_status="draft",
        environment_fingerprint="2" * 64,
        data_snapshot_id="draft-mysql-snapshot",
        algorithm_version="1.0.0",
        contract_version="1.0",
        runtime_profile="blackbox-v2-v1",
        code_hash="3" * 64,
        config_hash="4" * 64,
        manifest_hash="5" * 64,
        name="Draft MySQL Trial",
        description="isolated draft registration transaction test",
        horizon=1,
        task_type="T+1",
        tenors=("10Y",),
        frequency="daily",
        schedule=SimpleNamespace(
            cron="0 18 * * 1-5",
            timezone="Asia/Shanghai",
        ),
    )


def _assert_isolated_draft_database(
    engine,
    *,
    expected_schema: str,
    expected_temporary_server_uuid: str,
) -> None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT DATABASE() AS database_name,
                       @@server_uuid AS server_uuid
                """
            )
        ).mappings().one()
    database_name = str(row["database_name"])
    server_uuid = str(row["server_uuid"]).lower()
    expected_uuid = str(expected_temporary_server_uuid).lower()
    forbidden_uuid = os.environ.get(
        "BFL_PRODUCTION_MYSQL_SERVER_UUID",
        "",
    ).strip().lower()
    if (
        database_name == "bond_db"
        or database_name != expected_schema
        or _SCHEMA_PATTERN.fullmatch(database_name) is None
    ):
        raise AssertionError("draft registration test database is not isolated")
    if not expected_uuid or server_uuid != expected_uuid:
        raise AssertionError(
            "draft registration test server UUID is not the temporary datadir UUID"
        )
    if forbidden_uuid and server_uuid == forbidden_uuid:
        raise AssertionError(
            "draft registration test refused the production server UUID"
        )


def _insert_passed_run(
    engine,
    cfg,
    *,
    harness_run_id: str,
    finished_at: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_harness_runs
                    (harness_run_id, scheme_id, scheme_version, stage, status,
                     started_at, finished_at, triggered_by)
                VALUES
                    (:harness_run_id, :scheme_id, :scheme_version, 'all', 'passed',
                     :finished_at, :finished_at, 'isolated-mysql-test')
                """
            ),
            {
                "harness_run_id": harness_run_id,
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "finished_at": finished_at,
            },
        )


def _identity_counts(engine, cfg) -> tuple[int, int]:
    with engine.connect() as connection:
        version_count = int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_scheme_versions
                    WHERE scheme_id = :scheme_id
                      AND scheme_version = :scheme_version
                    """
                ),
                {
                    "scheme_id": cfg.scheme_id,
                    "scheme_version": cfg.scheme_version,
                },
            ).scalar_one()
        )
        registry_count = int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_scheme_registry
                    WHERE base_scheme_id = :scheme_id
                    """
                ),
                {"scheme_id": cfg.scheme_id},
            ).scalar_one()
        )
    return version_count, registry_count


@unittest.skipUnless(
    os.environ.get("BFL_BLACKBOX_DRAFT_REGISTER_MYSQL") == "1",
    "set BFL_BLACKBOX_DRAFT_REGISTER_MYSQL=1 for isolated draft registration MySQL",
)
class BlackboxDraftRegisterMySQLTests(unittest.TestCase):
    def test_lock_latest_run_fence_rollback_and_readback(self) -> None:
        from scheduler.repository import (
            _blackbox_draft_register_advisory_lock,
            read_blackbox_lifecycle_state,
            register_blackbox_draft_identity,
        )

        retained_root = None
        retained_process = None
        with temporary_mysql() as server:
            retained_root = server.root
            retained_process = server.process
            schema, engine = server.create_schema("draft")
            try:
                _assert_isolated_draft_database(
                    engine,
                    expected_schema=schema,
                    expected_temporary_server_uuid=(
                        server.expected_server_uuid or ""
                    ),
                )
                cfg = _draft_config()
                _insert_passed_run(
                    engine,
                    cfg,
                    harness_run_id="hr_old",
                    finished_at="2026-07-27 01:00:00",
                )

                lock_attempted = threading.Event()
                main_thread_id = threading.get_ident()

                def observe_lock_attempt(
                    _conn,
                    _cursor,
                    statement,
                    _parameters,
                    _context,
                    _executemany,
                ) -> None:
                    if (
                        "GET_LOCK" in statement
                        and threading.get_ident() != main_thread_id
                    ):
                        lock_attempted.set()

                with ThreadPoolExecutor(max_workers=1) as executor:
                    with _blackbox_draft_register_advisory_lock(
                        engine,
                        scheme_id=cfg.scheme_id,
                        timeout_sec=5,
                    ):
                        event.listen(
                            engine,
                            "before_cursor_execute",
                            observe_lock_attempt,
                        )
                        try:
                            future = executor.submit(
                                register_blackbox_draft_identity,
                                engine,
                                cfg,
                                expected_harness_run_id="hr_old",
                                lock_timeout_sec=5,
                            )
                            self.assertTrue(
                                lock_attempted.wait(timeout=5),
                                "second connection never attempted GET_LOCK",
                            )
                            _insert_passed_run(
                                engine,
                                cfg,
                                harness_run_id="hr_new",
                                finished_at="2026-07-27 02:00:00",
                            )
                        finally:
                            event.remove(
                                engine,
                                "before_cursor_execute",
                                observe_lock_attempt,
                            )
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "latest passed all-stage harness run changed",
                    ):
                        future.result(timeout=10)

                self.assertEqual(_identity_counts(engine, cfg), (0, 0))

                with (
                    patch(
                        "scheduler.repository."
                        "_blackbox_registry_identity_error",
                        return_value="forced readback mismatch",
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "forced readback mismatch",
                    ),
                ):
                    register_blackbox_draft_identity(
                        engine,
                        cfg,
                        expected_harness_run_id="hr_new",
                    )

                self.assertEqual(_identity_counts(engine, cfg), (0, 0))

                state = register_blackbox_draft_identity(
                    engine,
                    cfg,
                    expected_harness_run_id="hr_new",
                )
                readback = read_blackbox_lifecycle_state(engine, cfg)
                self.assertEqual(_identity_counts(engine, cfg), (1, 1))
                self.assertEqual(readback, state)
                self.assertEqual(state.version_status, "draft")
                self.assertEqual(state.registry_status, "paused")
            finally:
                engine.dispose()

        self.assertIsNotNone(retained_root)
        self.assertIsNotNone(retained_process)
        self.assertIsNotNone(retained_process.poll())
        self.assertFalse(retained_root.exists())
