from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from scheduler.repository import (
    NativeSuccessorBacktestEvidence,
    NativeSuccessorTarget,
    apply_native_successor_migration,
    canonical_native_successor_plan,
    native_successor_plan_sha256,
    read_native_successor_migration_plan,
)


MYSQL_URL = os.getenv("BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL")
pytestmark = pytest.mark.skipif(
    not MYSQL_URL,
    reason=(
        "BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL is required for isolated MySQL tests"
    ),
)
SCHEMA_PREFIX = "bfl_native_successor_pytest_"


def _full_request(scheme_id: str) -> dict[str, str]:
    return {
        "request_id": f"{scheme_id}:2026-05-22:2026-05-22:2026-05-29",
        "predict_date": "2026-05-22",
        "feature_date": "2026-05-22",
        "target_date": "2026-05-29",
        "daily_cutoff_key": "2026-05-22",
        "weekly_cutoff_key": "202621",
        "monthly_cutoff_key": "202605",
    }


def _engine(url: str):
    return create_engine(
        url,
        future=True,
        connect_args={"init_command": "SET SESSION time_zone = '+00:00'"},
    )


def _isolated_urls() -> tuple[str, str, str]:
    assert MYSQL_URL is not None
    parsed = urlsplit(MYSQL_URL)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("migration MySQL integration test requires a loopback server")
    schema_name = f"{SCHEMA_PREFIX}{uuid.uuid4().hex}"
    admin = urlunsplit((parsed.scheme, parsed.netloc, "", parsed.query, ""))
    target = urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{schema_name}", parsed.query, "")
    )
    return admin, target, schema_name


def _config_and_evidence():
    old = SimpleNamespace(
        scheme_id="native_multi",
        scheme_version="native-v1",
        runtime_type="native_adapter",
        code_hash="a" * 64,
        config_hash="b" * 64,
        manifest_hash="c" * 64,
        task_type="weekly_point",
        target_rule="next_week_last",
        horizon=6,
        frequency="weekly",
        schedule=SimpleNamespace(
            cron="0 8 * * 5",
            timezone="Asia/Shanghai",
        ),
        tenors=["5Y"],
    )
    new = SimpleNamespace(
        scheme_id="native_multi_bbv2",
        scheme_version="bb-v1",
        runtime_type="blackbox_v2",
        algorithm_version="1.0.0",
        contract_version="2.0",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        data_snapshot_id="snapshot-1",
        code_hash="d" * 64,
        config_hash="f" * 64,
        manifest_hash="1" * 64,
        task_type="weekly_point",
        target_rule="target_week_end_yield_vs_feature_week_end_yield",
        horizon=1,
        frequency="weekly",
        schedule=SimpleNamespace(
            cron="0 8 * * 5",
            timezone="Asia/Shanghai",
        ),
        tenors=["5Y"],
    )
    target = NativeSuccessorTarget(
        old_base_scheme_id=old.scheme_id,
        new_base_scheme_id=new.scheme_id,
        task_type="weekly_point",
        target_tenor="5Y",
        target_rule="next_week_last",
        old_horizon=6,
        new_horizon=1,
    )
    evidence = NativeSuccessorBacktestEvidence(
        backtest_run_id=42,
        benchmark_id="bbv2-migration",
        data_snapshot_id="snapshot-1",
        generation_id="generation-1",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        code_hash=new.code_hash,
        config_hash=new.config_hash,
        manifest_hash=new.manifest_hash,
        validator_policy_digest="validator-1",
    )
    return old, new, target, evidence


def _control_plane() -> dict[str, object]:
    control: dict[str, object] = {
        "schema_version": "native-successor-control-plane-evidence-v1",
        "wave": "W-test",
        "deployment_target": "aliyun-gray",
        "host": {
            "hostname": "isolated-mysql-test",
            "current_link": "/opt/bond-factor-lab/current",
            "release_root": "/opt/bond-factor-lab/releases/test",
        },
        "release": {
            "current_commit": "a" * 40,
            "archive_sha256": "b" * 64,
            "install_record_sha256": "c" * 64,
            "comparator_source_sha256": "6" * 64,
        },
        "databridge": {
            "generation_id": "generation-1",
            "data_snapshot_id": "snapshot-1",
            "business_digest": "d" * 64,
            "files": {
                filename: str(index) * 64
                for index, filename in enumerate(
                    (
                        "daily_output.csv",
                        "weekly_output.csv",
                        "monthly_output.csv",
                        "api_wind_date.csv",
                        "factor_catalog.csv",
                    ),
                    start=1,
                )
            },
        },
        "native_runtime": {
            "conda_env": "forecast_env",
            "environment_fingerprint": "8" * 64,
        },
        "scheduler": {
            "control_plane": "systemd_one_shot",
            "cadence": "weekly",
            "timer_fenced": True,
            "unique_writer": True,
            "installed_unit_hash": "e" * 64,
            "observed_state_sha256": "a" * 64,
        },
        "dashboard": {
            "active_scheme_set_sha256": "f" * 64,
            "response_sha256": "1" * 64,
        },
        "captured_at": "2026-09-05T00:00:00Z",
        "verified_by": "native-successor-controlled-capture-v1",
    }
    control["_capture_sha256"] = hashlib.sha256(
        json.dumps(
            control,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return control


def _create_schema(engine) -> None:
    statements = (
        """
        CREATE TABLE t_schema_migrations (
            version INT PRIMARY KEY,
            state ENUM('APPLYING','APPLIED') NOT NULL
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE t_scheme_versions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            scheme_id VARCHAR(64) NOT NULL,
            scheme_version VARCHAR(64) NOT NULL,
            runtime_type VARCHAR(32),
            algorithm_version VARCHAR(64),
            contract_version VARCHAR(32),
            runtime_profile VARCHAR(64),
            environment_fingerprint VARCHAR(64),
            data_snapshot_id VARCHAR(128),
            code_hash VARCHAR(64) NOT NULL,
            config_hash VARCHAR(64),
            manifest_hash VARCHAR(64),
            git_commit VARCHAR(64),
            status ENUM('draft','validated','shadow','active','paused','retired') NOT NULL,
            created_by VARCHAR(128),
            approved_by VARCHAR(128),
            approved_at DATETIME,
            UNIQUE KEY uk_scheme_version (scheme_id, scheme_version)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE t_scheme_registry (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            scheme_id VARCHAR(160) NOT NULL,
            base_scheme_id VARCHAR(128) NOT NULL,
            name VARCHAR(128) NOT NULL,
            owner VARCHAR(128) NOT NULL,
            description TEXT,
            horizon INT NOT NULL,
            task_type VARCHAR(32) NOT NULL,
            runtime_type VARCHAR(32) NOT NULL,
            tenors JSON NOT NULL,
            frequency VARCHAR(32) NOT NULL,
            target_tenor VARCHAR(64) NOT NULL,
            schedule_cron VARCHAR(64) NOT NULL,
            schedule_timezone VARCHAR(64) NOT NULL,
            status ENUM('active','paused','archived') NOT NULL,
            deployed_at DATE,
            UNIQUE KEY uk_registry_scheme_id (scheme_id)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE t_scheme_runs (
            run_id BIGINT AUTO_INCREMENT PRIMARY KEY,
            scheme_id VARCHAR(64) NOT NULL,
            predict_date DATE NOT NULL,
            status ENUM('running','success','failed','partial','skipped') NOT NULL
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE t_backtest_runs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            benchmark_id VARCHAR(128) NOT NULL,
            scheme_id VARCHAR(64) NOT NULL,
            data_source VARCHAR(64) NOT NULL,
            status ENUM('running','success','failed','partial') NOT NULL,
            run_mode VARCHAR(32),
            code_hash VARCHAR(64),
            config_hash VARCHAR(64),
            input_artifact_hash VARCHAR(128),
            summary JSON
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE t_backtest_predictions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            run_id BIGINT NOT NULL,
            scheme_id VARCHAR(64) NOT NULL,
            target_tenor VARCHAR(64) NOT NULL,
            horizon INT NOT NULL,
            predict_date DATE NOT NULL,
            feature_date DATE NOT NULL,
            target_date DATE NOT NULL,
            label TINYINT,
            predicted_direction TINYINT,
            confidence DOUBLE,
            source_row JSON,
            extra JSON
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE t_scheme_predictions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            run_id BIGINT,
            backtest_run_id BIGINT,
            scheme_version VARCHAR(64),
            scheme_id VARCHAR(64) NOT NULL,
            target_tenor VARCHAR(64) NOT NULL,
            horizon INT NOT NULL,
            predict_date DATE NOT NULL,
            feature_date DATE NOT NULL,
            target_date DATE NOT NULL,
            predicted_direction TINYINT NOT NULL,
            backtest_actual_direction TINYINT,
            confidence DOUBLE,
            model_version VARCHAR(64),
            extra JSON,
            UNIQUE KEY uk_product_fact (
                scheme_id, target_tenor, horizon, target_date
            ),
            CONSTRAINT ck_fact_source CHECK (
                (run_id IS NOT NULL AND backtest_run_id IS NULL
                    AND backtest_actual_direction IS NULL)
                OR
                (run_id IS NULL AND backtest_run_id IS NOT NULL
                    AND backtest_actual_direction IN (-1, 0, 1))
            )
        ) ENGINE=InnoDB
        """,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


def _seed(engine, old, new, evidence) -> None:
    summary = {
        "scheme_version": new.scheme_version,
        "manifest_hash": new.manifest_hash,
        "script_validator_policy_digest": evidence.validator_policy_digest,
        "data_snapshot_id": evidence.data_snapshot_id,
        "generation_id": evidence.generation_id,
        "runtime_profile": evidence.runtime_profile,
        "environment_fingerprint": evidence.environment_fingerprint,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_schema_migrations (version, state) "
                "VALUES (24, 'APPLIED')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, config_hash, "
                "manifest_hash, status) VALUES "
                "(:scheme_id, :scheme_version, 'native_adapter', :code_hash, "
                ":config_hash, :manifest_hash, 'active')"
            ),
            vars(old),
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_registry "
                "(scheme_id, base_scheme_id, name, owner, description, horizon, "
                "task_type, runtime_type, tenors, frequency, target_tenor, "
                "schedule_cron, schedule_timezone, status, deployed_at) VALUES "
                "('native_multi__h6__5Y', :scheme_id, 'Old display', 'owner-a', "
                "'old description', 6, 'weekly_point', 'native_adapter', "
                "CAST('[\"5Y\"]' AS JSON), 'weekly', '5Y', '0 8 * * 5', "
                "'Asia/Shanghai', 'active', '2026-01-01')"
            ),
            {"scheme_id": old.scheme_id},
        )
        connection.execute(
            text(
                "INSERT INTO t_backtest_runs "
                "(id, benchmark_id, scheme_id, data_source, status, run_mode, "
                "code_hash, config_hash, input_artifact_hash, summary) VALUES "
                "(42, :benchmark_id, :scheme_id, "
                "'blackbox_v2_current_snapshot_as_of', 'success', 'persist', "
                ":code_hash, :config_hash, :input_hash, CAST(:summary AS JSON))"
            ),
            {
                "benchmark_id": evidence.benchmark_id,
                "scheme_id": new.scheme_id,
                "code_hash": new.code_hash,
                "config_hash": new.config_hash,
                "input_hash": evidence.data_snapshot_id,
                "summary": json.dumps(summary),
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_backtest_predictions "
                "(id, run_id, scheme_id, target_tenor, horizon, predict_date, "
                "feature_date, target_date, label, predicted_direction, "
                "source_row, extra) "
                "VALUES (1, 42, :scheme_id, '5Y', 1, '2026-05-22', "
                "'2026-05-22', '2026-05-29', 1, -1, "
                "CAST(:source_row AS JSON), JSON_OBJECT())"
            ),
            {
                "scheme_id": new.scheme_id,
                "source_row": json.dumps(
                    _full_request(new.scheme_id) | {"actual": {}}
                ),
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_predictions "
                "(run_id, backtest_run_id, scheme_version, scheme_id, "
                "target_tenor, horizon, predict_date, feature_date, target_date, "
                "predicted_direction, backtest_actual_direction, model_version, extra) "
                "VALUES (NULL, 99, :scheme_version, :scheme_id, '5Y', 6, "
                "'2026-05-22', '2026-05-22', '2026-05-29', 1, 1, "
                ":scheme_version, JSON_OBJECT())"
            ),
            {
                "scheme_id": old.scheme_id,
                "scheme_version": old.scheme_version,
            },
        )


def _inputs(old, new, target, evidence, control, database_name, server_uuid):
    full_request = _full_request(target.new_base_scheme_id)
    request = {
        key: full_request[key]
        for key in ("request_id", "predict_date", "feature_date", "target_date")
    }
    request_sha256 = hashlib.sha256(
        canonical_native_successor_plan({"requests": [request]}).encode("utf-8")
    ).hexdigest()
    result_sha256 = hashlib.sha256(
        canonical_native_successor_plan(
            {"results": [{**request, "predicted_direction": -1}]}
        ).encode("utf-8")
    ).hexdigest()
    equivalence = {
        "schema_version": "native-successor-equivalence-v1",
        "wave": "W-test",
        "producer": {
            "tool": "native-successor-controlled-comparator",
            "tool_version": "1",
            "comparator_source_sha256": "6" * 64,
            "generated_at": "2026-09-05T00:00:00Z",
        },
        "generation_id": evidence.generation_id,
        "data_snapshot_id": evidence.data_snapshot_id,
        "data_files_sha256": dict(control["databridge"]["files"]),
        "runtime_environment_fingerprint": evidence.environment_fingerprint,
        "targets": [
            {
                "old_base_scheme_id": target.old_base_scheme_id,
                "new_base_scheme_id": target.new_base_scheme_id,
                "task_type": target.task_type,
                "target_tenor": target.target_tenor,
                "old_horizon": target.old_horizon,
                "new_horizon": target.new_horizon,
                "old_code_hash": old.code_hash,
                "new_code_hash": new.code_hash,
                "native_runtime_environment_fingerprint": "8" * 64,
                "request_artifact_sha256": hashlib.sha256(
                    canonical_native_successor_plan(
                        {"requests": [full_request]}
                    ).encode("utf-8")
                ).hexdigest(),
                "request_sha256": request_sha256,
                "request_count": 1,
                "native_result_sha256": result_sha256,
                "successor_result_sha256": result_sha256,
                "request_id_mismatch_count": 0,
                "predict_date_mismatch_count": 0,
                "feature_date_mismatch_count": 0,
                "target_date_mismatch_count": 0,
                "direction_mismatch_count": 0,
            }
        ],
    }
    return {
        "wave": "W-test",
        "targets": (target,),
        "old_configs": {old.scheme_id: old},
        "new_configs": {new.scheme_id: new},
        "backtests": {new.scheme_id: evidence},
        "equivalence_evidence": equivalence,
        "control_plane_evidence": control,
        "expected_database_name": database_name,
        "expected_server_uuid": server_uuid,
    }


def _apply_inputs(inputs):
    payload = dict(inputs)
    control = payload.pop("control_plane_evidence")
    payload["control_plane_evidence_reader"] = lambda: control
    return payload


def test_real_mysql_cutover_failure_rollback_and_recutover() -> None:
    admin_url, target_url, schema_name = _isolated_urls()
    admin = _engine(admin_url)
    engine = None
    created = False
    old, new, target, evidence = _config_and_evidence()
    try:
        with admin.begin() as connection:
            version = str(
                connection.execute(text("SELECT VERSION()")).scalar_one()
            )
            assert version.startswith("8.")
            connection.execute(
                text(
                    f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_0900_ai_ci"
                )
            )
        created = True
        engine = _engine(target_url)
        _create_schema(engine)
        _seed(engine, old, new, evidence)
        with engine.connect() as connection:
            server_uuid = str(
                connection.execute(text("SELECT @@server_uuid")).scalar_one()
            )
        control = _control_plane()
        inputs = _inputs(
            old,
            new,
            target,
            evidence,
            control,
            schema_name,
            server_uuid,
        )
        plan = read_native_successor_migration_plan(engine, **inputs)
        digest = native_successor_plan_sha256(plan)

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE t_scheme_registry ADD CONSTRAINT "
                "ck_test_keep_native_active CHECK "
                "(base_scheme_id <> 'native_multi' OR status = 'active')"
            )
        with pytest.raises(OperationalError):
            apply_native_successor_migration(
                engine,
                action="cutover",
                expected_plan_sha256=digest,
                approved_by="operator-a",
                approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
                **_apply_inputs(inputs),
            )
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM t_scheme_registry "
                    "WHERE base_scheme_id='native_multi'"
                )
            ).scalar_one() == "active"
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_registry "
                    "WHERE base_scheme_id='native_multi_bbv2'"
                )
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_versions "
                    "WHERE scheme_id='native_multi_bbv2'"
                )
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_predictions "
                    "WHERE scheme_id='native_multi_bbv2'"
                )
            ).scalar_one() == 0

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE t_scheme_registry DROP CHECK ck_test_keep_native_active"
            )
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=digest,
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
        rollback_plan = read_native_successor_migration_plan(engine, **inputs)
        apply_native_successor_migration(
            engine,
            action="rollback",
            expected_plan_sha256=native_successor_plan_sha256(rollback_plan),
            approved_by="operator-b",
            approved_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
        retry_plan = read_native_successor_migration_plan(engine, **inputs)
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(retry_plan),
            approved_by="operator-c",
            approved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_predictions "
                    "WHERE scheme_id='native_multi_bbv2'"
                )
            ).scalar_one() == 1
            assert connection.execute(
                text(
                    "SELECT status FROM t_scheme_registry "
                    "WHERE base_scheme_id='native_multi_bbv2'"
                )
            ).scalar_one() == "active"
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(text(f"DROP DATABASE `{schema_name}`"))
        admin.dispose()
