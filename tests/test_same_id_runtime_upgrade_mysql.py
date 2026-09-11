"""仅在 loopback 随机独立 schema 验收同 ID MySQL 事务与真实生命周期锁。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from scheduler import repository as repo
from test_native_successor_migration_mysql import (
    _config_and_evidence,
    _create_schema,
    _seed,
)


MYSQL_URL = os.getenv("BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL")
SCHEMA_PREFIX = "bfl_same_id_pytest_"
pytestmark = pytest.mark.skipif(
    not MYSQL_URL,
    reason="BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL is required for isolated MySQL",
)


@pytest.fixture
def mysql_migration():
    assert MYSQL_URL is not None
    url = make_url(MYSQL_URL)
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("isolated same-ID MySQL tests require a loopback server")
    schema = SCHEMA_PREFIX + uuid.uuid4().hex
    admin = create_engine(url.set(database=None), future=True)
    engine = None
    created = False
    try:
        with admin.begin() as conn:
            if not str(conn.exec_driver_sql("SELECT VERSION()").scalar_one()).startswith("8."):
                pytest.fail("same-ID integration requires MySQL 8")
            conn.exec_driver_sql(
                f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        created = True
        engine = create_engine(
            url.set(database=schema), future=True,
            connect_args={"init_command": "SET SESSION time_zone = '+00:00'"},
        )
        _create_schema(engine)
        old, new, _, evidence = _config_and_evidence()
        new.scheme_id = old.scheme_id
        new.horizon = old.horizon
        new.target_rule = old.target_rule
        _seed(engine, old, new, evidence)
        second_old, second_new = deepcopy(old), deepcopy(new)
        second_old.scheme_id = second_new.scheme_id = "second_native"
        old_configs = {cfg.scheme_id: cfg for cfg in (old, second_old)}
        new_configs = {cfg.scheme_id: cfg for cfg in (new, second_new)}
        with engine.begin() as conn:
            # 与 schema 024 的类型、键、JSON、自动更新时间行为保持一致。
            conn.exec_driver_sql("ALTER TABLE t_scheme_registry ADD updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
            conn.exec_driver_sql("CREATE TABLE t_harness_runs (harness_run_id VARCHAR(128) PRIMARY KEY, scheme_id VARCHAR(64) NOT NULL, scheme_version VARCHAR(64), code_hash VARCHAR(64), config_hash VARCHAR(64), stage VARCHAR(64) NOT NULL, status ENUM('running','passed','failed','blocked','error','skipped') NOT NULL, finished_at DATETIME) ENGINE=InnoDB")
            conn.exec_driver_sql("CREATE TABLE t_harness_gate_results (id BIGINT AUTO_INCREMENT PRIMARY KEY, harness_run_id VARCHAR(128) NOT NULL, gate_name VARCHAR(128) NOT NULL, status ENUM('passed','failed','blocked','error','skipped') NOT NULL, finished_at DATETIME, summary_json JSON, FOREIGN KEY (harness_run_id) REFERENCES t_harness_runs(harness_run_id)) ENGINE=InnoDB")
            conn.exec_driver_sql("CREATE TABLE t_input_artifacts (artifact_id VARCHAR(128) PRIMARY KEY, scheme_id VARCHAR(64) NOT NULL) ENGINE=InnoDB")
            conn.exec_driver_sql("CREATE TABLE t_backtest_monthly_metrics (id BIGINT AUTO_INCREMENT PRIMARY KEY, scheme_id VARCHAR(64) NOT NULL) ENGINE=InnoDB")
            conn.exec_driver_sql("CREATE TABLE t_backtest_reproduction_checks (id BIGINT AUTO_INCREMENT PRIMARY KEY, benchmark_id VARCHAR(128) NOT NULL) ENGINE=InnoDB")
            conn.execute(text("INSERT INTO t_scheme_versions (scheme_id,scheme_version,runtime_type,code_hash,config_hash,manifest_hash,status) VALUES (:scheme_id,:scheme_version,:runtime_type,:code_hash,:config_hash,:manifest_hash,'active')"), vars(second_old))
            conn.exec_driver_sql("INSERT INTO t_scheme_registry (scheme_id,base_scheme_id,name,owner,description,horizon,task_type,runtime_type,tenors,frequency,target_tenor,schedule_cron,schedule_timezone,status,deployed_at) SELECT 'second_native__h6__5Y','second_native',name,owner,description,horizon,task_type,runtime_type,tenors,frequency,target_tenor,schedule_cron,schedule_timezone,status,deployed_at FROM t_scheme_registry WHERE base_scheme_id='native_multi'")
            fields = ("scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")
            for scheme_id in old_configs:
                old, new = old_configs[scheme_id], new_configs[scheme_id]
                proof = {
                    "schema_version": "same-id-runtime-upgrade-evidence-v1", "scheme_id": scheme_id,
                    "old_identity": {key: getattr(old, key) for key in fields},
                    "new_identity": {key: getattr(new, key) for key in fields},
                    "environment_fingerprint": new.environment_fingerprint,
                    "data_snapshot_id": new.data_snapshot_id, "generation_id": "generation-1",
                    "equivalence_sha256": "a" * 64, "local_execution_sha256": "b" * 64,
                }
                conn.execute(text("INSERT INTO t_harness_runs VALUES (:scheme_id,:scheme_id,:scheme_version,:code_hash,:config_hash,'native-runtime-upgrade','passed','2026-09-12')"), vars(new))
                conn.execute(text("INSERT INTO t_harness_gate_results (harness_run_id,gate_name,status,finished_at,summary_json) VALUES (:scheme_id,'native-runtime-upgrade','passed','2026-09-12',CAST(:summary AS JSON))"), {
                    "scheme_id": scheme_id,
                    "summary": json.dumps({"passed": True, "errors": [], "evidence": [{"key": "runtime_upgrade", "value": proof}]}),
                })
            server_uuid = str(conn.exec_driver_sql("SELECT @@server_uuid").scalar_one())
        kwargs = dict(
            old_configs=old_configs, new_configs=new_configs,
            harness_run_ids={key: key for key in old_configs}, action="cutover",
            expected_database_name=schema, expected_server_uuid=server_uuid,
        )
        control = {
            "databridge": {"generation_id": "generation-1", "data_snapshot_id": "snapshot-1"},
            "blackbox_environment_fingerprint": "e" * 64, "writer_fenced": True,
        }
        yield engine, kwargs, control
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            # 不接受 URL 或环境变量作为删除目标，只删除本 fixture 生成的精确随机名字。
            assert schema.startswith(SCHEMA_PREFIX)
            assert len(schema) == len(SCHEMA_PREFIX) + 32
            assert all(char in "0123456789abcdef" for char in schema[len(SCHEMA_PREFIX):])
            assert schema != url.database
            with admin.begin() as conn:
                conn.exec_driver_sql(f"DROP DATABASE `{schema}`")
                remaining = conn.execute(text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:schema"), {"schema": schema}).scalar_one()
                assert remaining == 0
        admin.dispose()


def _plan(engine, kwargs, control):
    return repo.read_same_id_runtime_upgrade_plan(engine, **kwargs, control_plane_evidence=control)


def _apply(engine, kwargs, control, plan):
    return repo.apply_same_id_runtime_upgrade(
        engine, **kwargs, expected_plan_sha256=repo.native_successor_plan_sha256(plan),
        approved_by="isolated-mysql-test", approved_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        control_plane_evidence_reader=lambda: control,
    )


def test_mysql_failure_atomicity_cutover_rollback_and_recutover(mysql_migration):
    engine, kwargs, control = mysql_migration
    initial = _plan(engine, kwargs, control)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE t_scheme_registry ADD CONSTRAINT keep_second_native CHECK (base_scheme_id <> 'second_native' OR runtime_type = 'native_adapter')")
    with pytest.raises(OperationalError):
        _apply(engine, kwargs, control, initial)
    assert _plan(engine, kwargs, control) == initial
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE t_scheme_registry DROP CHECK keep_second_native")
    _apply(engine, kwargs, control, initial)
    rollback_args = kwargs | {"action": "rollback"}
    rollback = _plan(engine, rollback_args, control)
    assert rollback["facts"] == initial["facts"]
    assert {row["runtime_type"] for row in rollback["registry"]} == {"blackbox_v2"}
    _apply(engine, rollback_args, control, rollback)
    recutover = _plan(engine, kwargs, control)
    assert recutover["facts"] == initial["facts"]
    _apply(engine, kwargs, control, recutover)
    assert _plan(engine, rollback_args, control)["facts"] == initial["facts"]


@pytest.mark.parametrize("held_scheme_id", ["native_multi", "native_multi_bbv2"])
def test_mysql_upgrade_contends_with_normal_activation_lock(mysql_migration, monkeypatch, held_scheme_id):
    engine, kwargs, control = mysql_migration
    extras = tuple(scheme_id + "_bbv2" for scheme_id in kwargs["old_configs"])
    control = control | {"temporary_writer_check": {"scheme_ids": list(extras)}}
    apply_kwargs = kwargs | {"additional_lifecycle_lock_scheme_ids": extras}
    plan = _plan(engine, kwargs, control)
    monkeypatch.setattr(repo, "_BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC", 0.25)
    with repo._blackbox_activation_advisory_lock(engine, scheme_id=held_scheme_id):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_apply, engine, apply_kwargs, control, plan)
            with pytest.raises(repo.BlackboxActivationLockTimeout):
                future.result(timeout=10)
    assert _plan(engine, kwargs, control) == plan
    # 持锁者退出后，同一真实 MySQL 锁必须可再次获取并完成切换。
    _apply(engine, apply_kwargs, control, plan)


def test_mysql_stale_identity_and_fact_drift_are_rejected(mysql_migration):
    engine, kwargs, control = mysql_migration
    initial = _plan(engine, kwargs, control)
    for invalid in (
        kwargs | {"expected_database_name": "not_the_isolated_database"},
        kwargs | {"expected_server_uuid": "not_the_actual_server"},
    ):
        with pytest.raises(RuntimeError, match="database identity mismatch"):
            _plan(engine, invalid, control)
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE t_scheme_predictions SET predicted_direction=-1 WHERE id=1")
    drifted = _plan(engine, kwargs, control)
    with pytest.raises(RuntimeError, match="plan changed"):
        _apply(engine, kwargs, control, initial)
    assert _plan(engine, kwargs, control) == drifted
