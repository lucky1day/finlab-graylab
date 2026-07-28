from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import weakref
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import OperationalError

from migrations import runner as migration_runner
from migrations.runner import apply_migration_files
from scheduler.daily_runtime import (
    DailyRuntime,
    DefaultDailyRuntimeServices,
    GenerationBuildOutcome,
    _GenerationAvailability,
    _policy_payload,
)
from scheduler.daily_policy import load_daily_policy
from scheduler.discovery import discover_schemes
from scheduler.repository import (
    ScheduledCompletionEvidence,
    complete_scheduled_attempt,
    create_schedule_occurrence,
    create_scheme_run,
    fail_schedule_item_without_attempt,
    mark_schedule_attempt_terminal_failure,
    read_schedule_execution_envelope,
    read_schedule_occurrence_snapshot,
    register_schedule_attempt_process,
    register_seal_and_bind_schedule_occurrence_generation,
    start_schedule_attempt,
    sync_scheme_registry,
)
from shared.models import PredictionRecord


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
MIGRATIONS = tuple(sorted((PROJECT_ROOT / "migrations").glob("*.sql")))
MYSQLD = Path("/usr/local/mysql/bin/mysqld")
MYSQLADMIN = Path("/usr/local/mysql/bin/mysqladmin")
MYSQL_BASEDIR = Path("/usr/local/mysql")
SHANGHAI = ZoneInfo("Asia/Shanghai")
BUSINESS_DATE = date(2026, 7, 24)
FEATURE_DATE = "2026-07-23"
NATIVE_GENERATION_ID = "native-source-20260724-step6-mysql"
INDEPENDENT_FAILURE_SCHEME = "daily_1y_xgb_1y13_0629"
TEST_EPOCH = {
    "epoch": 1,
    "mode": "ledger",
    "record_sha256": "1" * 64,
}
SCHEMA_PREFIX = "bfl_step6_"
_VERIFIED_ISOLATED_ENGINES: weakref.WeakSet[object] = weakref.WeakSet()


class _FixedClock:
    def now_utc(self) -> datetime:
        return datetime(
            2026,
            7,
            23,
            22,
            40,
            0,
            123456,
            tzinfo=timezone.utc,
        )


class _ExpectationVerifier:
    def verify(self, expectation) -> ScheduledCompletionEvidence:
        return ScheduledCompletionEvidence(
            observed_generation_id=expectation.generation_id,
            manifest_sha256=expectation.manifest_sha256,
            generation_dataset_content_id=(
                expectation.generation_dataset_content_id
            ),
            generation_schema_version=(
                expectation.generation_schema_version
            ),
            generation_exporter_version=(
                expectation.generation_exporter_version
            ),
            feature_date=expectation.feature_date,
            scheme_version=expectation.scheme_version,
            code_sha256=expectation.code_sha256,
            config_sha256=expectation.config_sha256,
            native_generation_id=expectation.native_generation_id,
            native_manifest_sha256=(
                expectation.native_manifest_sha256
            ),
            native_dataset_content_id=(
                expectation.native_dataset_content_id
            ),
            native_schema_version=expectation.native_schema_version,
            native_exporter_version=(
                expectation.native_exporter_version
            ),
            native_feature_date=expectation.native_feature_date,
        )


class _TemporaryMySQL:
    """只在显式 opt-in 测试中启动的独立 MySQL 8.0.45。"""

    def __init__(self) -> None:
        # 先完成唯一可能在构造阶段失败的端口分配，避免失败后遗留目录。
        self.port = self._reserve_loopback_port()
        self.root = Path(
            tempfile.mkdtemp(
                prefix="bfl-step6-mysql-",
                dir="/private/tmp",
            )
        ).resolve()
        self.datadir = self.root / "data"
        self.tmpdir = self.root / "tmp"
        self.secure_file_dir = self.root / "secure"
        self.socket_path = self.root / "mysql.sock"
        self.pid_path = self.root / "mysqld.pid"
        self.error_log = self.root / "mysqld.err"
        self.bootstrap_sql = self.root / "bootstrap.sql"
        self.client_config = self.root / "root-client.cnf"
        self.root_password = uuid.uuid4().hex + uuid.uuid4().hex
        self.process: subprocess.Popen[bytes] | None = None
        self.server_uuid: str | None = None
        self.expected_server_uuid: str | None = None
        self._schema_credentials: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _reserve_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _error_text(self) -> str:
        try:
            return self.error_log.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return "<mysqld error log unavailable>"

    def start(self) -> None:
        if not MYSQLD.is_file() or not os.access(MYSQLD, os.X_OK):
            raise unittest.SkipTest(
                f"MySQL server binary is unavailable: {MYSQLD}"
            )
        if not MYSQLADMIN.is_file() or not os.access(
            MYSQLADMIN,
            os.X_OK,
        ):
            raise unittest.SkipTest(
                f"mysqladmin binary is unavailable: {MYSQLADMIN}"
            )
        self.datadir.mkdir(mode=0o700)
        self.tmpdir.mkdir(mode=0o700)
        self.secure_file_dir.mkdir(mode=0o700)
        initialize = subprocess.run(
            [
                os.fspath(MYSQLD),
                "--no-defaults",
                f"--basedir={MYSQL_BASEDIR}",
                f"--datadir={self.datadir}",
                "--initialize-insecure",
            ],
            check=False,
            capture_output=True,
            timeout=45,
        )
        if initialize.returncode != 0:
            output = (
                initialize.stdout + b"\n" + initialize.stderr
            ).decode("utf-8", errors="replace")
            raise RuntimeError(
                "temporary mysqld initialization failed:\n" + output
            )
        auto_config = (self.datadir / "auto.cnf").read_text(
            encoding="utf-8"
        )
        uuid_match = re.search(
            r"(?m)^server-uuid=([0-9a-f-]{36})$",
            auto_config,
        )
        if uuid_match is None:
            raise RuntimeError(
                "temporary MySQL auto.cnf has no server-uuid"
            )
        self.expected_server_uuid = uuid_match.group(1)
        self.bootstrap_sql.write_text(
            "ALTER USER 'root'@'localhost' IDENTIFIED BY "
            f"'{self.root_password}';\n",
            encoding="utf-8",
        )
        self.bootstrap_sql.chmod(0o600)
        self.client_config.write_text(
            "[client]\n"
            "user=root\n"
            f"password={self.root_password}\n"
            f"socket={self.socket_path}\n",
            encoding="utf-8",
        )
        self.client_config.chmod(0o600)

        self.process = subprocess.Popen(
            [
                os.fspath(MYSQLD),
                "--no-defaults",
                f"--basedir={MYSQL_BASEDIR}",
                f"--datadir={self.datadir}",
                "--bind-address=127.0.0.1",
                f"--port={self.port}",
                f"--socket={self.socket_path}",
                f"--pid-file={self.pid_path}",
                f"--log-error={self.error_log}",
                f"--tmpdir={self.tmpdir}",
                f"--secure-file-priv={self.secure_file_dir}",
                f"--init-file={self.bootstrap_sql}",
                "--skip-name-resolve",
                "--skip-log-bin",
                "--mysqlx=OFF",
                "--local-infile=OFF",
                "--skip-symbolic-links",
                "--default-time-zone=+00:00",
                "--default-storage-engine=InnoDB",
                "--transaction-isolation=REPEATABLE-READ",
                "--character-set-server=utf8mb4",
                "--collation-server=utf8mb4_0900_ai_ci",
                "--innodb-buffer-pool-size=256M",
                "--log-timestamps=UTC",
                (
                    "--sql-mode=STRICT_TRANS_TABLES,"
                    "NO_ZERO_DATE,NO_ZERO_IN_DATE,"
                    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    "temporary mysqld exited before readiness:\n"
                    + self._error_text()
                )
            try:
                ping = subprocess.run(
                    [
                        os.fspath(MYSQLADMIN),
                        f"--defaults-extra-file={self.client_config}",
                        "ping",
                        "--silent",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except subprocess.TimeoutExpired:
                ping = None
            if ping is not None and ping.returncode == 0:
                self.bootstrap_sql.unlink(missing_ok=True)
                self._assert_empty_root_password_rejected()
                return
            time.sleep(0.1)
        raise RuntimeError(
            "temporary mysqld did not become ready:\n"
            + self._error_text()
        )

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                subprocess.run(
                    [
                        os.fspath(MYSQLADMIN),
                        f"--defaults-extra-file={self.client_config}",
                        "shutdown",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if process.poll() is None:
            raise RuntimeError(
                "temporary mysqld could not be stopped; directory retained: "
                f"{self.root}"
            )
        self.process = None

    def _socket_admin_engine(self):
        url = URL.create(
            drivername="mysql+pymysql",
            username="root",
            password=self.root_password,
            host="localhost",
            query={
                "charset": "utf8mb4",
                "unix_socket": os.fspath(self.socket_path),
            },
        )
        return create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args={
                "init_command": "SET SESSION time_zone = '+00:00'",
            },
        )

    def _assert_empty_root_password_rejected(self) -> None:
        url = URL.create(
            drivername="mysql+pymysql",
            username="root",
            password="",
            host="localhost",
            query={
                "charset": "utf8mb4",
                "unix_socket": os.fspath(self.socket_path),
            },
        )
        engine = create_engine(url, future=True)
        try:
            try:
                with engine.connect():
                    pass
            except OperationalError:
                return
            raise AssertionError(
                "temporary MySQL root still accepts an empty password"
            )
        finally:
            engine.dispose()

    def engine(self, database: str):
        try:
            username, password = self._schema_credentials[database]
        except KeyError as exc:
            raise RuntimeError(
                f"temporary schema credentials are missing: {database}"
            ) from exc
        url = URL.create(
            drivername="mysql+pymysql",
            username=username,
            password=password,
            host="127.0.0.1",
            port=self.port,
            database=database,
            query={"charset": "utf8mb4"},
        )
        engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args={
                "init_command": "SET SESSION time_zone = '+00:00'",
            },
        )
        try:
            self._assert_isolated_session(engine, database)
        except BaseException:
            engine.dispose()
            raise
        _VERIFIED_ISOLATED_ENGINES.add(engine)
        return engine

    def create_schema(self, purpose: str):
        suffix = uuid.uuid4().hex[:10]
        schema = f"{SCHEMA_PREFIX}{purpose}_{suffix}"
        if (
            schema == "bond_db"
            or re.fullmatch(r"bfl_step6_[a-z]+_[0-9a-f]{10}", schema)
            is None
        ):
            raise RuntimeError(f"unsafe Step 6 schema identity: {schema}")
        username = f"bfl_s6_{suffix}"
        password = uuid.uuid4().hex + uuid.uuid4().hex
        admin = self._socket_admin_engine()
        try:
            with admin.begin() as connection:
                connection.exec_driver_sql(
                    f"CREATE DATABASE `{schema}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
                connection.exec_driver_sql(
                    f"CREATE USER '{username}'@'127.0.0.1' "
                    f"IDENTIFIED BY '{password}'"
                )
                connection.exec_driver_sql(
                    f"GRANT ALL PRIVILEGES ON `{schema}`.* "
                    f"TO '{username}'@'127.0.0.1'"
                )
        finally:
            admin.dispose()
        self._schema_credentials[schema] = (username, password)
        engine = self.engine(schema)
        try:
            apply_migration_files(engine, MIGRATIONS)
            self._assert_migrated_schema(engine)
            self._assert_generic_run_default(engine)
        except BaseException:
            engine.dispose()
            raise
        return schema, engine

    def _assert_isolated_session(self, engine, schema: str) -> None:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT VERSION() AS version,
                           @@server_uuid AS server_uuid,
                           @@session.time_zone AS session_time_zone,
                           @@default_storage_engine AS storage_engine,
                           @@session.sql_mode AS sql_mode,
                           @@foreign_key_checks AS foreign_key_checks,
                           @@session.transaction_isolation AS isolation_level,
                           @@bind_address AS bind_address,
                           @@global.log_bin AS log_bin,
                           @@global.local_infile AS local_infile,
                           @@secure_file_priv AS secure_file_priv,
                           @@port AS port,
                           @@socket AS socket_path,
                           @@datadir AS datadir,
                           DATABASE() AS database_name
                    """
                )
            ).mappings().one()
        self.server_uuid = self.server_uuid or str(row["server_uuid"])
        if str(row["server_uuid"]) != self.server_uuid:
            raise AssertionError("temporary server_uuid changed")
        if str(row["server_uuid"]) != self.expected_server_uuid:
            raise AssertionError(
                "connected server_uuid differs from isolated datadir"
            )
        if not str(row["version"]).startswith("8.0.45"):
            raise AssertionError(
                f"unexpected temporary MySQL version: {row['version']}"
            )
        if str(row["database_name"]) != schema or not schema.startswith(
            SCHEMA_PREFIX
        ):
            raise AssertionError(
                f"unsafe temporary database identity: {row['database_name']}"
            )
        if str(row["session_time_zone"]) != "+00:00":
            raise AssertionError("temporary MySQL session is not UTC")
        if str(row["storage_engine"]).lower() != "innodb":
            raise AssertionError("temporary MySQL default engine is not InnoDB")
        sql_modes = {
            value.strip().upper()
            for value in str(row["sql_mode"]).split(",")
            if value.strip()
        }
        if not sql_modes.intersection(
            {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"}
        ):
            raise AssertionError("temporary MySQL strict SQL mode is disabled")
        if int(row["foreign_key_checks"]) != 1:
            raise AssertionError("temporary MySQL foreign keys are disabled")
        if str(row["isolation_level"]).upper() != "REPEATABLE-READ":
            raise AssertionError(
                "temporary MySQL transaction isolation drifted"
            )
        if str(row["bind_address"]) != "127.0.0.1":
            raise AssertionError("temporary MySQL is not loopback-only")
        if int(row["log_bin"]) != 0:
            raise AssertionError("temporary MySQL binary logging is enabled")
        if int(row["local_infile"]) != 0:
            raise AssertionError("temporary MySQL local_infile is enabled")
        if (
            Path(str(row["secure_file_priv"])).resolve()
            != self.secure_file_dir.resolve()
        ):
            raise AssertionError(
                "temporary MySQL secure_file_priv drifted"
            )
        if int(row["port"]) != self.port:
            raise AssertionError("temporary MySQL loopback port drifted")
        if Path(str(row["socket_path"])) != self.socket_path:
            raise AssertionError("temporary MySQL socket path drifted")
        if Path(str(row["datadir"])).resolve() != self.datadir.resolve():
            raise AssertionError("temporary MySQL datadir is not isolated")

    @staticmethod
    def _assert_migrated_schema(engine) -> None:
        with engine.connect() as connection:
            history = connection.execute(
                text(
                    """
                    SELECT COUNT(*) AS total,
                           MIN(version) AS min_version,
                           MAX(version) AS max_version,
                           SUM(state = 'APPLIED') AS applied_count,
                           SUM(state <> 'APPLIED') AS non_applied_count
                    FROM t_schema_migrations
                    """
                )
            ).mappings().one()
            non_innodb = connection.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                      AND table_type = 'BASE TABLE'
                      AND engine <> 'InnoDB'
                    """
                )
            ).scalars().all()
            bond_db_count = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM information_schema.schemata
                        WHERE schema_name = 'bond_db'
                        """
                    )
                ).scalar_one()
            )
            started_at = connection.execute(
                text(
                    """
                    SELECT column_type AS column_type,
                           is_nullable AS is_nullable,
                           column_default AS column_default,
                           extra AS extra
                    FROM information_schema.columns
                    WHERE table_schema = DATABASE()
                      AND table_name = 't_scheme_runs'
                      AND column_name = 'started_at'
                    """
                )
            ).mappings().one()
        observed_history = (
            int(history["total"]),
            int(history["min_version"]),
            int(history["max_version"]),
            int(history["applied_count"]),
            int(history["non_applied_count"]),
        )
        if observed_history != (18, 1, 18, 18, 0):
            raise AssertionError(
                "temporary MySQL migration history is incomplete: "
                f"{observed_history}"
            )
        if non_innodb:
            raise AssertionError(
                f"temporary MySQL has non-InnoDB tables: {non_innodb}"
            )
        if bond_db_count != 0:
            raise AssertionError(
                "temporary MySQL unexpectedly contains bond_db"
            )
        observed_started_at = (
            str(started_at["column_type"]).lower(),
            str(started_at["is_nullable"]).upper(),
            str(started_at["column_default"]).upper(),
            str(started_at["extra"]).upper(),
        )
        if observed_started_at != (
            "datetime(6)",
            "YES",
            "CURRENT_TIMESTAMP(6)",
            "DEFAULT_GENERATED",
        ):
            raise AssertionError(
                "migration 018 started_at target shape drifted: "
                f"{observed_started_at}"
            )

    @staticmethod
    def _assert_generic_run_default(engine) -> None:
        run_id = create_scheme_run(
            engine,
            scheme_id="step6_generic_default_probe",
            predict_date=BUSINESS_DATE.isoformat(),
            scheme_version="step6-test-v1",
            prediction_phase="gray_live",
        )
        with engine.connect() as connection:
            started_at = connection.execute(
                text(
                    """
                    SELECT started_at
                    FROM t_scheme_runs
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            ).scalar_one()
        if started_at is None:
            raise AssertionError(
                "generic create_scheme_run lost started_at DB default"
            )


@contextmanager
def _temporary_mysql():
    server = _TemporaryMySQL()
    try:
        server.start()
        yield server
    except BaseException:
        diagnostic = server._error_text()
        if diagnostic and diagnostic != "<mysqld error log unavailable>":
            print("\n--- temporary mysqld error log ---\n" + diagnostic)
        raise
    finally:
        try:
            server.stop()
        finally:
            process = server.process
            if process is None or process.poll() is not None:
                shutil.rmtree(server.root)


def _assert_test_epoch(frozen, *, label: str, engine=None) -> None:
    if dict(frozen or {}) != TEST_EPOCH:
        raise RuntimeError(
            f"{label} does not match isolated Step 6 epoch"
        )
    root_engine = getattr(engine, "engine", None)
    if root_engine not in _VERIFIED_ISOLATED_ENGINES:
        raise RuntimeError(
            f"{label} did not use a verified isolated MySQL engine"
        )


class Step6EpochFixtureContractTests(unittest.TestCase):
    def test_epoch_assertion_rejects_missing_engine(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "verified isolated MySQL engine",
        ):
            _assert_test_epoch(
                TEST_EPOCH,
                label="missing engine fixture",
            )

    def test_epoch_assertion_rejects_foreign_engine(self) -> None:
        foreign_engine = create_engine("sqlite+pysqlite:///:memory:")
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "verified isolated MySQL engine",
            ):
                _assert_test_epoch(
                    TEST_EPOCH,
                    label="foreign engine fixture",
                    engine=foreign_engine,
                )
        finally:
            foreign_engine.dispose()


def _real_policy_and_configs(policy_path=POLICY_PATH):
    active_daily = tuple(
        config
        for config in discover_schemes(strict=True)
        if config.status == "active" and config.frequency == "daily"
    )
    policy = load_daily_policy(
        policy_path,
        discovered=active_daily,
    )
    return policy, {
        config.scheme_id: config
        for config in active_daily
        if config.scheme_id in policy.schemes
    }


def _seed_test_registry(engine, policy, configs) -> None:
    expected_blackbox_count = sum(
        item.runtime_type == "blackbox_v2"
        for item in policy.schemes.values()
    )
    sync_scheme_registry(
        engine,
        tuple(configs[scheme_id] for scheme_id in policy.schemes),
    )
    with engine.begin() as connection:
        approved = connection.execute(
            text(
                """
                UPDATE t_scheme_versions
                SET status = 'active',
                    approved_by = 'step6-isolated-fixture',
                    approved_at = UTC_TIMESTAMP()
                WHERE runtime_type = 'blackbox_v2'
                """
            )
        )
        if int(approved.rowcount) != expected_blackbox_count:
            raise AssertionError(
                "isolated test metadata did not approve the frozen V2 "
                "version count: "
                f"{approved.rowcount}/{expected_blackbox_count}"
            )
        activated = connection.execute(
            text(
                """
                UPDATE t_scheme_registry
                SET status = 'active'
                WHERE frequency = 'daily'
                  AND base_scheme_id IN (
                      SELECT scheme_id
                      FROM t_scheme_versions
                      WHERE runtime_type = 'blackbox_v2'
                  )
                """
            )
        )
        if int(activated.rowcount) != expected_blackbox_count:
            raise AssertionError(
                "isolated test Registry did not activate the frozen V2 "
                "target count: "
                f"{activated.rowcount}/{expected_blackbox_count}"
            )


def _occurrence_args(policy, configs, *, schedule_key: str):
    release_base = datetime(2026, 7, 23, 22, 30, tzinfo=timezone.utc)
    target_dates = {
        f"{scheme_id}__h{item.horizon}__{tenor}": (
            "2026-07-24" if item.horizon == 1 else "2026-07-30"
        )
        for scheme_id, item in policy.schemes.items()
        for tenor in item.target_tenors
    }
    item_policy_by_base = {
        scheme_id: {
            "scheme_version": configs[scheme_id].scheme_version,
            "code_sha256": configs[scheme_id].code_hash,
            "config_sha256": configs[scheme_id].config_hash,
            "cache_group": item.cache_group,
            "resource_class": item.resource_class,
            "internal_workers": item.internal_workers,
            "release_offset_minutes": item.v2_release_offset_min or 0,
            "release_at": release_base
            + timedelta(minutes=item.v2_release_offset_min or 0),
            "deadline_at": datetime(
                2026,
                7,
                23,
                23,
                55,
                tzinfo=timezone.utc,
            ),
        }
        for scheme_id, item in policy.schemes.items()
    }
    policy_json = _policy_payload(
        policy,
        daily_coordinator_epoch=TEST_EPOCH,
    )
    cache_qualified_ids = sorted(
        scheme_id
        for scheme_id, item in policy.schemes.items()
        if item.cache_spec_fingerprint is not None
    )
    if len(cache_qualified_ids) != 10:
        raise AssertionError(
            "Step 6 cache-qualified policy baseline drifted: "
            f"{cache_qualified_ids}"
        )
    # Admission 必须保持 BLOCKED，因此本测试不能伪造受签名的 20+20
    # qualification/acceptance 文件。MySQL 用例只证明 ledger claim/事务；
    # 真实 cache prerequisite 顺序由无库矩阵验证，cache completion gate
    # 明确保留给容量阶段。把这一投影写入冻结快照，避免证据被误读。
    policy_json["step6_test_projection"] = {
        "purpose": "mysql_ledger_claim_only",
        "cache_completion_gate": "excluded_admission_blocked",
        "excluded_scheme_ids": cache_qualified_ids,
    }
    for scheme in policy_json["schemes"]:
        scheme["cache_spec_fingerprint"] = None
    return {
        "schedule_key": schedule_key,
        "predict_date": BUSINESS_DATE.isoformat(),
        "feature_date": FEATURE_DATE,
        "target_dates": target_dates,
        "item_policy_by_base": item_policy_by_base,
        "policy_version": policy.version,
        "policy_json": policy_json,
    }


def _create_bound_occurrence(
    engine,
    policy,
    configs,
    *,
    schedule_key: str,
    manifest_root: Path,
    clock: _FixedClock,
) -> tuple[int, object]:
    args = _occurrence_args(
        policy,
        configs,
        schedule_key=schedule_key,
    )
    occurrence_id = create_schedule_occurrence(engine, **args)
    snapshot = read_schedule_occurrence_snapshot(
        engine,
        occurrence_id=occurrence_id,
    )
    if (
        snapshot.actual_item_count != 21
        or snapshot.actual_target_count != 25
    ):
        raise AssertionError(
            "temporary MySQL occurrence is not the real 21/25 matrix"
        )
    native = [
        summary
        for summary in snapshot.items
        if summary.item.runtime_type == "native_adapter"
    ]
    if len(native) != 17 or sum(row.target_count for row in native) != 21:
        raise AssertionError(
            "temporary MySQL occurrence is not the real Native 17/21 matrix"
        )
    generation_id = (
        NATIVE_GENERATION_ID + "-" + schedule_key.rsplit("-", 1)[-1]
    )
    register_seal_and_bind_schedule_occurrence_generation(
        engine,
        occurrence_id=occurrence_id,
        generation_id=generation_id,
        generation_type="native_source",
        business_date=BUSINESS_DATE.isoformat(),
        feature_date=FEATURE_DATE,
        readiness_basis="CLOCK_CONTRACT",
        source_commit_token="c" * 64,
        dataset_content_id="d" * 64,
        schema_version="daily-input-v1",
        exporter_version="step6-test-v1",
        manifest_uri=os.fspath(
            manifest_root / generation_id / "manifest.json"
        ),
        manifest_sha256="e" * 64,
        expected_feature_date=FEATURE_DATE,
        _clock=clock,
    )
    for summary in snapshot.items:
        if summary.item.runtime_type == "blackbox_v2":
            fail_schedule_item_without_attempt(
                engine,
                item_id=summary.item.item_id,
                failure_code="GENERATION_BUILD_FAILED",
                failure_message="Step 6 isolates Native execution",
                _clock=clock,
            )
    return occurrence_id, SimpleNamespace(
        generation_id=generation_id,
        business_date=BUSINESS_DATE.isoformat(),
    )


def _records_for_item(engine, item_id: int) -> list[PredictionRecord]:
    envelope = read_schedule_execution_envelope(
        engine,
        item_id=item_id,
    )
    return [
        PredictionRecord(
            scheme_id=envelope.item.base_scheme_id,
            target_tenor=target.target_tenor,
            horizon=target.horizon,
            predict_date=envelope.occurrence.predict_date,
            feature_date=envelope.generation.feature_date,
            target_date=target.target_date,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            model_version=envelope.item.scheme_version,
            extra={"feature_date": envelope.generation.feature_date},
        )
        for target in envelope.targets
    ]


def _register_and_complete(engine, attempt, clock: _FixedClock) -> None:
    register_schedule_attempt_process(
        engine,
        run_id=attempt.run_id,
        execution_token=attempt.execution_token,
        process_id=700_000 + attempt.run_id,
        process_group_id=700_000 + attempt.run_id,
        _clock=clock,
    )
    register_schedule_attempt_process(
        engine,
        run_id=attempt.run_id,
        execution_token=attempt.execution_token,
        process_id=700_000 + attempt.run_id,
        process_group_id=700_000 + attempt.run_id,
        _clock=clock,
    )
    complete_scheduled_attempt(
        engine,
        run_id=attempt.run_id,
        records=_records_for_item(engine, attempt.item_id),
        trusted_verifier=_ExpectationVerifier(),
        _clock=clock,
    )


class _MySQLCoordinatorServices(DefaultDailyRuntimeServices):
    def __init__(
        self,
        *,
        engine,
        policy,
        clock: _FixedClock,
        failure_scheme_id: str | None = None,
    ) -> None:
        super().__init__(engine=engine)
        self._policy = policy
        self._clock = clock
        self.failure_scheme_id = failure_scheme_id
        self.attempts: Counter[str] = Counter()
        self.alerts: list[dict[str, object]] = []
        self.heartbeats: list[dict[str, object]] = []
        self._counter_lock = threading.Lock()

    def now(self) -> datetime:
        return datetime(2026, 7, 24, 6, 40, tzinfo=SHANGHAI)

    def execute_item(self, *, item_id: int, trigger_origin: str):
        envelope = read_schedule_execution_envelope(
            self.engine,
            item_id=item_id,
        )
        scheme_id = envelope.item.base_scheme_id
        attempt = start_schedule_attempt(
            self.engine,
            item_id=item_id,
            trigger_origin=trigger_origin,
            execution_token=f"step6-{uuid.uuid4().hex}",
            _clock=self._clock,
        )
        register_schedule_attempt_process(
            self.engine,
            run_id=attempt.run_id,
            execution_token=attempt.execution_token,
            process_id=800_000 + attempt.run_id,
            process_group_id=800_000 + attempt.run_id,
            _clock=self._clock,
        )
        with self._counter_lock:
            self.attempts[scheme_id] += 1
        if scheme_id == self.failure_scheme_id:
            mark_schedule_attempt_terminal_failure(
                self.engine,
                run_id=attempt.run_id,
                failure_code="ALGORITHM",
                error_message="Step 6 injected independent failure",
                _clock=self._clock,
            )
            return SimpleNamespace(
                status="failed",
                scheme_id=scheme_id,
                failure_code="ALGORITHM",
                error_message="Step 6 injected independent failure",
            )
        complete_scheduled_attempt(
            self.engine,
            run_id=attempt.run_id,
            records=_records_for_item(self.engine, item_id),
            trusted_verifier=_ExpectationVerifier(),
            _clock=self._clock,
        )
        return SimpleNamespace(
            status="success",
            scheme_id=scheme_id,
            failure_code=None,
            error_message=None,
        )

    def heartbeat(
        self,
        *,
        occurrence_id: int | None,
        state: str,
        details,
    ) -> None:
        self.heartbeats.append(
            {
                "occurrence_id": occurrence_id,
                "state": state,
                "details": details,
            }
        )

    def alert(
        self,
        *,
        code: str,
        severity: str,
        business_date: date,
        occurrence_id: int | None,
        message: str,
        scheme_id: str | None = None,
        details=None,
    ) -> None:
        self.alerts.append(
            {
                "code": code,
                "severity": severity,
                "business_date": business_date,
                "occurrence_id": occurrence_id,
                "message": message,
                "scheme_id": scheme_id,
                "details": details,
            }
        )

    @staticmethod
    def wait_for_progress(seconds: float) -> None:
        del seconds
        threading.Event().wait(0.001)


def _availability(policy, native_generation):
    availability = _GenerationAvailability(
        policy.allowed_resource_combinations
    )
    availability.finish(
        GenerationBuildOutcome(
            native_generation=native_generation,
            databridge_generation=None,
        )
    )
    return availability


def _native_item_ids(snapshot) -> dict[str, int]:
    return {
        summary.item.base_scheme_id: summary.item.item_id
        for summary in snapshot.items
        if summary.item.runtime_type == "native_adapter"
    }


def _run_count(engine, occurrence_id: int) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_scheme_runs AS r
                    JOIN t_schedule_items AS i
                      ON i.item_id = r.schedule_item_id
                    WHERE i.occurrence_id = :occurrence_id
                    """
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        )


def _set_018_applying(engine) -> None:
    with engine.begin() as connection:
        updated = connection.execute(
            text(
                """
                UPDATE t_schema_migrations
                SET state = 'APPLYING',
                    applied_at = NULL
                WHERE version = 18
                  AND state = 'APPLIED'
                """
            )
        )
    if int(updated.rowcount) != 1:
        raise AssertionError(
            "failed to stage isolated migration 018 recovery"
        )


def _recover_018(engine, *, expected_classification: str) -> None:
    inspection = migration_runner.inspect_applying_migration_018(
        engine,
        MIGRATIONS,
    )
    if inspection["classification"] != expected_classification:
        raise AssertionError(
            "unexpected migration 018 recovery classification: "
            f"{inspection}"
        )
    result = migration_runner.recover_applying_migration_018(
        engine,
        MIGRATIONS,
        expected_state_digest=str(inspection["state_digest"]),
    )
    if (
        result["recovery_outcome"] != "APPLIED"
        or result["initial_classification"] != expected_classification
    ):
        raise AssertionError(
            f"migration 018 recovery failed: {result}"
        )


@unittest.skipUnless(
    os.environ.get("BFL_STEP6_MYSQL") == "1",
    "set BFL_STEP6_MYSQL=1 to run isolated MySQL Step 6 verification",
)
class DailyNativeCoordinatorMySQLTests(unittest.TestCase):
    def test_mysql_claim_reentry_and_independent_failure(self) -> None:
        policy, configs = _real_policy_and_configs()
        clock = _FixedClock()
        with (
            _temporary_mysql() as server,
            patch(
                "scheduler.repository."
                "assert_daily_coordinator_epoch_payload_matches_current",
                side_effect=_assert_test_epoch,
            ),
        ):
            success_schema, success_engine = server.create_schema("success")
            try:
                migration_018 = (
                    migration_runner.validate_release_migration_manifest(
                        MIGRATIONS
                    )[-1]
                )
                migration_runner._execute_prepared_migration_files(
                    success_engine,
                    [
                        (
                            migration_018.path,
                            migration_018.statements,
                        )
                    ],
                )
                server._assert_migrated_schema(success_engine)
                _set_018_applying(success_engine)
                _recover_018(
                    success_engine,
                    expected_classification="COMPLETE",
                )
                _seed_test_registry(success_engine, policy, configs)
                occurrence_id, native_generation = (
                    _create_bound_occurrence(
                        success_engine,
                        policy,
                        configs,
                        schedule_key=f"step6-success-{success_schema[-10:]}",
                        manifest_root=server.root,
                        clock=clock,
                    )
                )
                snapshot = read_schedule_occurrence_snapshot(
                    success_engine,
                    occurrence_id=occurrence_id,
                )
                native_ids = _native_item_ids(snapshot)
                claim_item_id = native_ids[INDEPENDENT_FAILURE_SCHEME]
                claim_barrier = threading.Barrier(2)
                claim_engines = [
                    server.engine(success_schema),
                    server.engine(success_schema),
                ]

                def claim(index: int):
                    claim_barrier.wait(timeout=5)
                    try:
                        return start_schedule_attempt(
                            claim_engines[index],
                            item_id=claim_item_id,
                            execution_token=f"step6-race-{index}",
                            _clock=clock,
                        )
                    except RuntimeError as exc:
                        return exc

                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        outcomes = list(pool.map(claim, (0, 1)))
                finally:
                    for engine in claim_engines:
                        engine.dispose()
                winners = [
                    outcome
                    for outcome in outcomes
                    if not isinstance(outcome, Exception)
                ]
                rejected = [
                    outcome
                    for outcome in outcomes
                    if isinstance(outcome, Exception)
                ]
                self.assertEqual(len(winners), 1)
                self.assertEqual(len(rejected), 1)
                self.assertEqual(winners[0].attempt_no, 1)
                _register_and_complete(success_engine, winners[0], clock)

                services = _MySQLCoordinatorServices(
                    engine=success_engine,
                    policy=policy,
                    clock=clock,
                )
                runtime = DailyRuntime(services)
                first = runtime._drive_items(
                    occurrence_id=occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=_availability(
                        policy,
                        native_generation,
                    ),
                    trigger_origin="apscheduler",
                )
                runs_after_first = _run_count(
                    success_engine,
                    occurrence_id,
                )
                second = runtime._drive_items(
                    occurrence_id=occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=_availability(
                        policy,
                        native_generation,
                    ),
                    trigger_origin="startup_catchup",
                )
                replay_id = create_schedule_occurrence(
                    success_engine,
                    **_occurrence_args(
                        policy,
                        configs,
                        schedule_key=(
                            f"step6-success-{success_schema[-10:]}"
                        ),
                    ),
                )
                self.assertEqual(replay_id, occurrence_id)
                self.assertEqual(len(first.dispatched_scheme_ids), 16)
                self.assertNotIn(
                    INDEPENDENT_FAILURE_SCHEME,
                    first.dispatched_scheme_ids,
                )
                self.assertEqual(second.dispatched_scheme_ids, ())
                self.assertEqual(runs_after_first, 17)
                self.assertEqual(
                    _run_count(success_engine, occurrence_id),
                    runs_after_first,
                )
                final_snapshot = read_schedule_occurrence_snapshot(
                    success_engine,
                    occurrence_id=occurrence_id,
                )
                final_native = [
                    row
                    for row in final_snapshot.items
                    if row.item.runtime_type == "native_adapter"
                ]
                self.assertTrue(
                    all(
                        row.item.state == "SUCCESS"
                        and row.item.attempt_no == 1
                        and row.item.current_run_id is not None
                        for row in final_native
                    )
                )
            finally:
                success_engine.dispose()

            failure_schema, failure_engine = server.create_schema("failure")
            try:
                with failure_engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            ALTER TABLE t_scheme_runs
                            MODIFY COLUMN started_at DATETIME NOT NULL
                                DEFAULT CURRENT_TIMESTAMP
                            """
                        )
                    )
                _set_018_applying(failure_engine)
                _recover_018(
                    failure_engine,
                    expected_classification="COMPATIBLE_PARTIAL",
                )
                server._assert_migrated_schema(failure_engine)
                _seed_test_registry(failure_engine, policy, configs)
                failure_occurrence_id, failure_generation = (
                    _create_bound_occurrence(
                        failure_engine,
                        policy,
                        configs,
                        schedule_key=f"step6-failure-{failure_schema[-10:]}",
                        manifest_root=server.root,
                        clock=clock,
                    )
                )
                failure_services = _MySQLCoordinatorServices(
                    engine=failure_engine,
                    policy=policy,
                    clock=clock,
                    failure_scheme_id=INDEPENDENT_FAILURE_SCHEME,
                )
                failure_result = DailyRuntime(
                    failure_services
                )._drive_items(
                    occurrence_id=failure_occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=_availability(
                        policy,
                        failure_generation,
                    ),
                    trigger_origin="apscheduler",
                )
                failure_snapshot = read_schedule_occurrence_snapshot(
                    failure_engine,
                    occurrence_id=failure_occurrence_id,
                )
                failure_native = {
                    row.item.base_scheme_id: row.item
                    for row in failure_snapshot.items
                    if row.item.runtime_type == "native_adapter"
                }
                self.assertEqual(failure_result.status, "finished")
                self.assertEqual(
                    set(failure_result.dispatched_scheme_ids),
                    set(failure_native),
                )
                self.assertEqual(
                    failure_native[
                        INDEPENDENT_FAILURE_SCHEME
                    ].state,
                    "FAILED_TERMINAL",
                )
                self.assertEqual(
                    failure_native[
                        INDEPENDENT_FAILURE_SCHEME
                    ].failure_code,
                    "ALGORITHM",
                )
                self.assertTrue(
                    all(
                        item.state == "SUCCESS"
                        for scheme_id, item in failure_native.items()
                        if scheme_id != INDEPENDENT_FAILURE_SCHEME
                    )
                )
                self.assertTrue(
                    all(
                        item.attempt_no == 1
                        for item in failure_native.values()
                    )
                )
                terminal_alerts = [
                    alert
                    for alert in failure_services.alerts
                    if alert["code"] == "ITEM_TERMINAL_FAILURE"
                ]
                self.assertEqual(len(terminal_alerts), 1)
                self.assertEqual(
                    terminal_alerts[0]["scheme_id"],
                    INDEPENDENT_FAILURE_SCHEME,
                )
                self.assertEqual(
                    terminal_alerts[0]["details"]["failure_code"],
                    "ALGORITHM",
                )
                self.assertEqual(
                    _run_count(failure_engine, failure_occurrence_id),
                    17,
                )
            finally:
                failure_engine.dispose()

            bad_schema, bad_engine = server.create_schema("badshape")
            try:
                with bad_engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            ALTER TABLE t_scheme_runs
                            MODIFY COLUMN started_at DATETIME(3) NULL
                                DEFAULT CURRENT_TIMESTAMP(3)
                            """
                        )
                    )
                _set_018_applying(bad_engine)
                bad_inspection = (
                    migration_runner.inspect_applying_migration_018(
                        bad_engine,
                        MIGRATIONS,
                    )
                )
                self.assertEqual(
                    bad_inspection["classification"],
                    "UNSAFE",
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "refused unsafe state",
                ):
                    migration_runner.recover_applying_migration_018(
                        bad_engine,
                        MIGRATIONS,
                        expected_state_digest=str(
                            bad_inspection["state_digest"]
                        ),
                    )
            finally:
                bad_engine.dispose()

            self.assertRegex(
                server.server_uuid or "",
                r"^[0-9a-f-]{36}$",
            )
