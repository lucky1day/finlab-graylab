from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import text

from tests.test_daily_native_coordinator_mysql import (
    MIGRATIONS,
    _seed_test_registry,
    _temporary_mysql,
    _real_policy_and_configs,
    apply_migration_files,
)
TEST_EPOCH = {
    "epoch": 999_999,
    "mode": "ledger",
    "record_sha256": "9" * 64,
}
DATABASE_NAME = "bfl_real_replay_0123456789"
SERVER_UUID = "01234567-89ab-cdef-0123-456789abcdef"
PORT = 43306
PRIVATE_ROOT = Path("/private/tmp/bfl-real-replay")
SOCKET_PATH = PRIVATE_ROOT / "mysql.sock"
DATADIR = PRIVATE_ROOT / "data"
SECURE_FILE_DIR = PRIVATE_ROOT / "secure"


def _generation_fixture():
    from tests.test_databridge_generation_executor import (
        DataBridgeGenerationExecutorTests,
    )

    return DataBridgeGenerationExecutorTests()


def _tamper_generation_file(path: Path) -> None:
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n")


class _DBAPICursor:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row
        self._identity_only = False
        self.description = tuple(
            (name, None, None, None, None, None, None)
            for name in row
        )

    def execute(self, statement) -> None:
        normalized = " ".join(str(statement).split()).upper()
        self._identity_only = (
            normalized == "SELECT DATABASE(), @@SERVER_UUID"
        )
        if self._identity_only:
            self.description = (
                ("database_name", None, None, None, None, None, None),
                ("server_uuid", None, None, None, None, None, None),
            )

    def fetchone(self) -> tuple[object, ...]:
        if self._identity_only:
            return (
                self._row["database_name"],
                self._row["server_uuid"],
            )
        return tuple(self._row.values())

    def close(self) -> None:
        return None


class _DBAPIConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def cursor(self) -> _DBAPICursor:
        return _DBAPICursor(self._row)


class _Connection:
    def __init__(self, engine: _Engine, row: dict[str, object]) -> None:
        self.engine = engine
        self._row = row
        self.info: dict[str, object] = {}
        self.connection = SimpleNamespace(
            driver_connection=_DBAPIConnection(row)
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

class _Engine:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row
        self.url = SimpleNamespace(database=row["database_name"])
        self.dialect = SimpleNamespace(name="mysql")

    def connect(self) -> _Connection:
        return _Connection(self, self._row)


def _isolated_engine(
    *,
    database_name: str = DATABASE_NAME,
    server_uuid: str = SERVER_UUID,
) -> _Engine:
    return _Engine(
        {
            "version": "8.0.45",
            "database_name": database_name,
            "server_uuid": server_uuid,
            "session_time_zone": "+00:00",
            "storage_engine": "InnoDB",
            "sql_mode": "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
            "isolation_level": "REPEATABLE-READ",
            "bind_address": "127.0.0.1",
            "port": PORT,
            "socket_path": str(SOCKET_PATH),
            "datadir": f"{DATADIR}/",
            "secure_file_priv": f"{SECURE_FILE_DIR}/",
            "foreign_key_checks": 1,
            "log_bin": 0,
            "local_infile": 0,
        }
    )


@contextmanager
def _verified_isolation(engine: _Engine):
    from harness.daily_real_replay import verify_real_replay_database

    with (
        patch("shared.daily_coordinator_mode.event.listen") as listen,
        patch(
            "shared.daily_coordinator_mode.event.contains",
            return_value=True,
        ),
    ):
        isolation = verify_real_replay_database(
            engine,
            expected_database_name=DATABASE_NAME,
            expected_server_uuid=SERVER_UUID,
            expected_port=PORT,
            expected_private_root=PRIVATE_ROOT,
        )
        yield isolation, listen


class DailyRealReplayGateTests(unittest.TestCase):
    def test_repository_mutators_are_not_reexported(self) -> None:
        from harness import daily_real_replay

        self.assertFalse(
            hasattr(daily_real_replay, "create_schedule_occurrence")
        )
        self.assertFalse(
            hasattr(
                daily_real_replay,
                "register_seal_and_bind_schedule_occurrence_generation",
            )
        )

    def test_shared_epoch_capability_has_no_external_registration_seam(
        self,
    ) -> None:
        import inspect

        from shared import daily_coordinator_mode

        self.assertFalse(
            hasattr(
                daily_coordinator_mode,
                "register_verified_isolated_daily_database",
            )
        )
        parameters = inspect.signature(
            daily_coordinator_mode.
            verify_and_register_isolated_daily_database
        ).parameters
        self.assertNotIn("isolation", parameters)
        self.assertNotIn("connection_guard", parameters)

    def test_opens_same_day_parent_bound_generations(self) -> None:
        from harness.daily_real_replay import (
            open_real_replay_generations,
        )

        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )

        self.assertEqual(
            inputs.native_generation.generation_id,
            native.generation_id,
        )
        self.assertEqual(
            inputs.databridge_generation.generation_id,
            databridge.generation_id,
        )
        self.assertEqual(inputs.business_date, "2026-07-24")
        self.assertEqual(inputs.feature_date, "2026-07-23")

    def test_rejects_databridge_bound_to_another_native_parent(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            open_real_replay_generations,
        )

        fixture = _generation_fixture()
        with (
            tempfile.TemporaryDirectory() as native_root,
            tempfile.TemporaryDirectory() as databridge_root,
        ):
            unrelated_native, _ = fixture._generation(
                Path(native_root)
            )
            _, databridge = fixture._delivery_generation(
                Path(databridge_root)
            )
            with self.assertRaisesRegex(
                DailyRealReplayError,
                "Native parent",
            ):
                open_real_replay_generations(
                    native_manifest=unrelated_native.manifest_path,
                    databridge_manifest=databridge.manifest_path,
                )

    def test_guarded_create_builds_non_sla_21_25_occurrence(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        opened_at = datetime(
            2026,
            7,
            26,
            1,
            30,
            tzinfo=timezone.utc,
        )
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            captured: dict[str, object] = {}

            def create(create_engine, **kwargs) -> int:
                self.assertIs(create_engine, engine)
                captured.update(kwargs)
                return 41

            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence",
                    side_effect=create,
                ),
            ):
                occurrence_id = create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=opened_at,
                    epoch_payload=TEST_EPOCH,
                )

        self.assertEqual(occurrence_id, 41)
        args = captured
        self.assertEqual(args["predict_date"], "2026-07-24")
        self.assertEqual(args["feature_date"], "2026-07-23")
        self.assertEqual(len(args["item_policy_by_base"]), 21)
        self.assertEqual(len(args["target_dates"]), 25)
        expected_cutoff = datetime(
            2026,
            7,
            26,
            16,
            0,
            tzinfo=timezone.utc,
        )
        self.assertEqual(args["sla_deadline_at"], expected_cutoff)
        self.assertEqual(args["recovery_cutoff_at"], expected_cutoff)
        self.assertTrue(
            all(
                row["deadline_at"] == expected_cutoff
                for row in args["item_policy_by_base"].values()
            )
        )

        policy_json = args["policy_json"]
        projection = policy_json["real_replay_projection"]
        self.assertEqual(
            projection["algorithm_execution"],
            "real_17_native_plus_4_blackbox_v2",
        )
        self.assertEqual(
            projection["capacity_qualification"],
            "EXCLUDED",
        )
        self.assertEqual(
            projection["sla_qualification"],
            "EXCLUDED",
        )
        self.assertEqual(
            projection["native_generation_id"],
            native.generation_id,
        )
        self.assertEqual(
            projection["databridge_generation_id"],
            databridge.generation_id,
        )
        self.assertEqual(
            policy_json["daily_coordinator_epoch"],
            TEST_EPOCH,
        )
        self.assertFalse(
            any(
                row["cache_spec_fingerprint"] is not None
                for row in policy_json["schemes"]
            )
        )
        self.assertEqual(
            {
                row["input_compatibility"]
                for row in policy_json["schemes"]
                if row["runtime_type"] == "blackbox_v2"
            },
            {"databridge_v1"},
        )
        self.assertEqual(
            sum(
                row["input_compatibility"] == "generation_v1"
                for row in policy_json["schemes"]
            ),
            14,
        )
        self.assertEqual(
            sum(
                row["input_compatibility"] == "live_source_0629"
                for row in policy_json["schemes"]
            ),
            3,
        )
        v2_rows = sorted(
            (
                row
                for row in args["item_policy_by_base"].values()
                if row["release_offset_minutes"] in {0, 2, 4, 6}
                and row["resource_class"] == "blackbox_v2"
            ),
            key=lambda row: row["release_offset_minutes"],
        )
        self.assertEqual(
            [row["release_offset_minutes"] for row in v2_rows],
            [0, 2, 4, 6],
        )
        self.assertTrue(
            all(row["release_at"] <= opened_at for row in v2_rows)
        )

    def test_guarded_create_rejects_mutated_generation_context(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            weekend_inputs = replace(
                inputs,
                native_generation=replace(
                    inputs.native_generation,
                    business_date="2026-07-25",
                ),
                databridge_generation=replace(
                    inputs.databridge_generation,
                    business_date="2026-07-25",
                ),
                business_date="2026-07-25",
                feature_date="2026-07-24",
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "generation context differs from reopened manifests",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=weekend_inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime(
                        2026,
                        7,
                        26,
                        1,
                        30,
                        tzinfo=timezone.utc,
                    ),
                    epoch_payload=TEST_EPOCH,
                )

    def test_guarded_create_rejects_non_date_context_audit_drift(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            drifted_inputs = (
                replace(
                    inputs,
                    native_generation=replace(
                        inputs.native_generation,
                        source_commit_token="a" * 64,
                    ),
                ),
                replace(
                    inputs,
                    databridge_generation=replace(
                        inputs.databridge_generation,
                        upstream_business_digest="b" * 64,
                    ),
                ),
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence",
                    return_value=41,
                ) as persist,
            ):
                for drifted in drifted_inputs:
                    with (
                        self.subTest(
                            generation_type=(
                                "native"
                                if drifted.native_generation
                                is not inputs.native_generation
                                else "databridge"
                            ),
                        ),
                        self.assertRaisesRegex(
                            DailyRealReplayError,
                            "generation context differs from "
                            "reopened manifests",
                        ),
                    ):
                        create_real_replay_occurrence(
                            engine,
                            isolation=isolation,
                            policy=policy,
                            configs=configs,
                            inputs=drifted,
                            schedule_key=(
                                "isolated-real-replay-v1-unit"
                            ),
                            opened_at=datetime(
                                2026,
                                7,
                                26,
                                1,
                                30,
                                tzinfo=timezone.utc,
                            ),
                            epoch_payload=TEST_EPOCH,
                        )
            persist.assert_not_called()

    def test_create_rehashes_native_manifest_before_persistence(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            _tamper_generation_file(
                native.files["api_wind_daily.csv"]
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence",
                ) as persist,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "generation manifest revalidation failed",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime(
                        2026,
                        7,
                        26,
                        1,
                        30,
                        tzinfo=timezone.utc,
                    ),
                    epoch_payload=TEST_EPOCH,
                )
            persist.assert_not_called()

    def test_create_rejects_invalid_nested_generation_context(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            invalid = replace(
                inputs,
                native_generation=SimpleNamespace(
                    manifest_path=native.manifest_path,
                ),
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence",
                ) as persist,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "verified generation inputs are required",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=invalid,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime(
                        2026,
                        7,
                        26,
                        1,
                        30,
                        tzinfo=timezone.utc,
                    ),
                    epoch_payload=TEST_EPOCH,
                )
            persist.assert_not_called()

    def test_guarded_create_rejects_reopened_non_trading_predict_date(
        self,
    ) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )
        from tests.test_databridge_generation_executor import (
            _FrozenCalendarConnection,
            _delivery_dataset,
        )
        from tests.test_databridge_input_generation import (
            SCHEMA_PATH,
            _native_cutoff_context,
        )
        from tests.test_native_input_generation import _Engine, _Rows

        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        class _NonTradingPredictConnection(
            _FrozenCalendarConnection
        ):
            def execute(self, statement, params=None):
                if "FROM t_trade_calendar" in str(statement):
                    return _Rows(
                        [
                            {"rdate": "2026-07-23", "trade_flag": "1"},
                            {"rdate": "2026-07-24", "trade_flag": "0"},
                            {"rdate": "2026-07-27", "trade_flag": "1"},
                            {"rdate": "2026-07-28", "trade_flag": "1"},
                            {"rdate": "2026-07-29", "trade_flag": "1"},
                            {"rdate": "2026-07-30", "trade_flag": "1"},
                        ]
                    )
                return super().execute(statement, params)

        source_engine = _Engine()
        source_engine.connection = _NonTradingPredictConnection()
        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        with tempfile.TemporaryDirectory() as tmpdir:
            native = _native_cutoff_context(engine=source_engine)
            databridge = create_databridge_generation(
                _delivery_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "predict_date is not a trading day",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime(
                        2026,
                        7,
                        26,
                        1,
                        30,
                        tzinfo=timezone.utc,
                    ),
                    epoch_payload=TEST_EPOCH,
                )

    def test_replay_occurrence_rejects_formal_schedule_namespace(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence"
                ) as create,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "schedule namespace",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="critical-daily-signals-v1",
                    opened_at=datetime.now(timezone.utc),
                    epoch_payload=TEST_EPOCH,
                )
            create.assert_not_called()

    def test_replay_occurrence_rejects_compatibility_identity_swap(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        mutated_schemes = dict(policy.schemes)
        approved_id = "daily_1y_xgb_1y13_0629"
        unrelated_id = "t1_daily"
        mutated_schemes[approved_id] = replace(
            mutated_schemes[approved_id],
            input_compatibility="generation_v1",
        )
        mutated_schemes[unrelated_id] = replace(
            mutated_schemes[unrelated_id],
            input_compatibility="live_source_0629",
        )
        mutated_policy = replace(
            policy,
            schemes=MappingProxyType(mutated_schemes),
        )
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence"
                ) as create,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "compatibility identities",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=mutated_policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime.now(timezone.utc),
                    epoch_payload=TEST_EPOCH,
                )
            create.assert_not_called()

    def test_replay_occurrence_rejects_duplicate_payload_id(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )
        from scheduler.daily_runtime import _policy_payload

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        fixture = _generation_fixture()

        def corrupt_payload(*args, **kwargs):
            payload = _policy_payload(*args, **kwargs)
            generation_rows = [
                row
                for row in payload["schemes"]
                if row["input_compatibility"] == "generation_v1"
            ]
            generation_rows[0]["scheme_id"] = generation_rows[1][
                "scheme_id"
            ]
            return payload

        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay._policy_payload",
                    side_effect=corrupt_payload,
                ),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence"
                ) as create,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "compatibility identities",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime.now(timezone.utc),
                    epoch_payload=TEST_EPOCH,
                )
            create.assert_not_called()

    def test_replay_occurrence_rejects_native_v2_identity_swap(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        mutated_schemes = dict(policy.schemes)
        native_id = "t1_daily"
        v2_id = "one_y_t5_liq_excess_a_v1"
        native = mutated_schemes[native_id]
        v2 = mutated_schemes[v2_id]
        mutated_schemes[native_id] = replace(
            native,
            runtime_type="blackbox_v2",
            resource_class=v2.resource_class,
            input_compatibility="databridge_v1",
            v2_release_offset_min=0,
        )
        mutated_schemes[v2_id] = replace(
            v2,
            runtime_type="native_adapter",
            resource_class=native.resource_class,
            input_compatibility="generation_v1",
            v2_release_offset_min=None,
        )
        mutated_policy = replace(
            policy,
            schemes=MappingProxyType(mutated_schemes),
        )
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native_generation, databridge = (
                fixture._delivery_generation(Path(tmpdir))
            )
            inputs = open_real_replay_generations(
                native_manifest=native_generation.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence"
                ) as create,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "compatibility identities",
                ),
            ):
                create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=mutated_policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime.now(timezone.utc),
                    epoch_payload=TEST_EPOCH,
                )
            create.assert_not_called()

    def test_replay_identity_gate_rejects_native_v2_mode_only_swap(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            _assert_input_compatibility_identities,
        )
        from scheduler.daily_runtime import _policy_payload

        policy, configs = _real_policy_and_configs()
        mutated_schemes = dict(policy.schemes)
        native_id = "t1_daily"
        v2_id = "one_y_t5_liq_excess_a_v1"
        mutated_schemes[native_id] = replace(
            mutated_schemes[native_id],
            input_compatibility="databridge_v1",
        )
        mutated_schemes[v2_id] = replace(
            mutated_schemes[v2_id],
            input_compatibility="generation_v1",
        )
        mutated_policy = replace(
            policy,
            schemes=MappingProxyType(mutated_schemes),
        )
        payload = _policy_payload(
            mutated_policy,
            daily_coordinator_epoch=TEST_EPOCH,
        )

        with self.assertRaisesRegex(
            DailyRealReplayError,
            "compatibility identities",
        ):
            _assert_input_compatibility_identities(
                payload,
                expected_policy_schemes=mutated_policy.schemes,
                expected_configs=configs,
            )

    def test_replay_occurrence_allows_approved_0629_generation_migration(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        migrated_id = "daily_1y_xgb_1y13_0629"
        migrated_schemes = dict(policy.schemes)
        migrated_schemes[migrated_id] = replace(
            migrated_schemes[migrated_id],
            input_compatibility="generation_v1",
            source_package_sha256=None,
        )
        migrated_policy = replace(
            policy,
            schemes=MappingProxyType(migrated_schemes),
        )
        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_create_schedule_occurrence",
                    return_value=41,
                ) as create,
            ):
                occurrence_id = create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=migrated_policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime.now(timezone.utc),
                    epoch_payload=TEST_EPOCH,
                )

        self.assertEqual(occurrence_id, 41)
        create.assert_called_once()

    def test_replay_identity_gate_rejects_noncanonical_scheme_id(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            _assert_input_compatibility_identities,
        )
        from scheduler.daily_runtime import _policy_payload

        policy, configs = _real_policy_and_configs()
        payload = _policy_payload(
            policy,
            daily_coordinator_epoch=TEST_EPOCH,
        )
        for invalid_id in ("", "t1_daily "):
            with self.subTest(invalid_id=invalid_id):
                mutated = {
                    **payload,
                    "schemes": [
                        dict(item)
                        for item in payload["schemes"]
                    ],
                }
                row = next(
                    item
                    for item in mutated["schemes"]
                    if item["scheme_id"] == "t1_daily"
                )
                row["scheme_id"] = invalid_id
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "compatibility identities",
                ):
                    _assert_input_compatibility_identities(
                        mutated,
                        expected_policy_schemes=policy.schemes,
                        expected_configs=configs,
                    )

    def test_replay_identity_gate_rejects_unknown_runtime(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            _assert_input_compatibility_identities,
        )
        from scheduler.daily_runtime import _policy_payload

        policy, configs = _real_policy_and_configs()
        scheme_id = "t1_daily"
        mutated_schemes = dict(policy.schemes)
        mutated_configs = dict(configs)
        mutated_schemes[scheme_id] = replace(
            mutated_schemes[scheme_id],
            runtime_type="alien_runtime",
        )
        mutated_configs[scheme_id] = replace(
            mutated_configs[scheme_id],
            runtime_type="alien_runtime",
        )
        mutated_policy = replace(
            policy,
            schemes=MappingProxyType(mutated_schemes),
        )
        payload = _policy_payload(
            mutated_policy,
            daily_coordinator_epoch=TEST_EPOCH,
        )

        with self.assertRaisesRegex(
            DailyRealReplayError,
            "compatibility identities",
        ):
            _assert_input_compatibility_identities(
                payload,
                expected_policy_schemes=mutated_policy.schemes,
                expected_configs=mutated_configs,
            )

    def test_replay_identity_gate_rejects_source_package_drift(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            _assert_input_compatibility_identities,
        )
        from scheduler.daily_runtime import _policy_payload

        policy, configs = _real_policy_and_configs()
        scheme_id = "daily_1y_xgb_1y13_0629"
        migrated_schemes = dict(policy.schemes)
        migrated_schemes[scheme_id] = replace(
            migrated_schemes[scheme_id],
            input_compatibility="generation_v1",
        )
        migrated_policy = replace(
            policy,
            schemes=MappingProxyType(migrated_schemes),
        )
        migrated_payload = _policy_payload(
            migrated_policy,
            daily_coordinator_epoch=TEST_EPOCH,
        )
        with self.assertRaisesRegex(
            DailyRealReplayError,
            "compatibility identities",
        ):
            _assert_input_compatibility_identities(
                migrated_payload,
                expected_policy_schemes=migrated_policy.schemes,
                expected_configs=configs,
            )

        payload = _policy_payload(
            policy,
            daily_coordinator_epoch=TEST_EPOCH,
        )
        row = next(
            item
            for item in payload["schemes"]
            if item["scheme_id"] == scheme_id
        )
        row["source_package_sha256"] = "a" * 64
        with self.assertRaisesRegex(
            DailyRealReplayError,
            "compatibility identities",
        ):
            _assert_input_compatibility_identities(
                payload,
                expected_policy_schemes=policy.schemes,
                expected_configs=configs,
            )

    def test_database_identity_rejects_production_schema(self) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            verify_real_replay_database,
        )

        with self.assertRaisesRegex(
            DailyRealReplayError,
            "database",
        ):
            verify_real_replay_database(
                _isolated_engine(database_name="bond_db"),
                expected_database_name="bond_db",
                expected_server_uuid=SERVER_UUID,
                expected_port=PORT,
                expected_private_root=PRIVATE_ROOT,
            )

    def test_database_identity_requires_full_mysql_contract(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            verify_real_replay_database,
        )

        invalid_values = {
            "version": "8.0.44",
            "session_time_zone": "SYSTEM",
            "storage_engine": "MyISAM",
            "sql_mode": "NO_ENGINE_SUBSTITUTION",
            "isolation_level": "READ-COMMITTED",
            "foreign_key_checks": 0,
            "bind_address": "0.0.0.0",
            "log_bin": 1,
            "local_infile": 1,
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                engine = _isolated_engine()
                engine._row[field] = value
                with (
                    patch(
                        "shared.daily_coordinator_mode.event.listen"
                    ),
                    self.assertRaises(DailyRealReplayError),
                ):
                    verify_real_replay_database(
                        engine,
                        expected_database_name=DATABASE_NAME,
                        expected_server_uuid=SERVER_UUID,
                        expected_port=PORT,
                        expected_private_root=PRIVATE_ROOT,
                    )

    def test_connection_guard_revalidates_checked_out_connection(
        self,
    ) -> None:
        engine = _isolated_engine()
        with _verified_isolation(engine) as (_isolation, listen):
            listen.assert_called_once()
            connection_guard = listen.call_args.args[2]
            engine._row["database_name"] = "bond_db"
            with self.assertRaisesRegex(
                RuntimeError,
                "database",
            ):
                connection_guard(engine.connect())

    def test_database_capability_cannot_be_registered_twice(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            verify_real_replay_database,
        )

        engine = _isolated_engine()
        with _verified_isolation(engine) as (_isolation, listen):
            with self.assertRaisesRegex(
                DailyRealReplayError,
                "already registered",
            ):
                verify_real_replay_database(
                    engine,
                    expected_database_name=DATABASE_NAME,
                    expected_server_uuid=SERVER_UUID,
                    expected_port=PORT,
                    expected_private_root=PRIVATE_ROOT,
                )
            listen.assert_called_once()

    def test_failed_database_verification_releases_reservation(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            verify_real_replay_database,
        )

        engine = _isolated_engine()
        engine._row["version"] = "8.0.44"
        with self.assertRaises(DailyRealReplayError):
            verify_real_replay_database(
                engine,
                expected_database_name=DATABASE_NAME,
                expected_server_uuid=SERVER_UUID,
                expected_port=PORT,
                expected_private_root=PRIVATE_ROOT,
            )
        engine._row["version"] = "8.0.45"
        with _verified_isolation(engine) as (isolation, listen):
            self.assertEqual(isolation.engine_identity, id(engine))
            listen.assert_called_once()

    def test_database_verification_never_connects_under_registry_lock(
        self,
    ) -> None:
        import weakref

        from harness.daily_real_replay import verify_real_replay_database
        from shared import daily_coordinator_mode

        class TrackingLock:
            held = False

            def __enter__(self):
                if self.held:
                    raise AssertionError("capability lock re-entered")
                self.held = True
                return self

            def __exit__(self, *_args):
                self.held = False

        capability_check = getattr(
            daily_coordinator_mode,
            "_is_real_replay_isolation_capability",
        )

        class ReentrantEngine(_Engine):
            def connect(self):
                capability_check(self, None)
                return super().connect()

        engine = ReentrantEngine(_isolated_engine()._row.copy())
        with (
            patch.object(
                daily_coordinator_mode,
                "_verified_isolation_lock",
                TrackingLock(),
            ),
            patch.object(
                daily_coordinator_mode,
                "_verified_isolations",
                weakref.WeakKeyDictionary(),
            ),
            patch.object(
                daily_coordinator_mode,
                "_pending_isolations",
                weakref.WeakKeyDictionary(),
            ),
            patch(
                "shared.daily_coordinator_mode.event.listen"
            ),
        ):
            isolation = verify_real_replay_database(
                engine,
                expected_database_name=DATABASE_NAME,
                expected_server_uuid=SERVER_UUID,
                expected_port=PORT,
                expected_private_root=PRIVATE_ROOT,
            )

        self.assertEqual(isolation.engine_identity, id(engine))

    def test_guarded_create_rejects_another_engine(self) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            create_real_replay_occurrence,
            open_real_replay_generations,
        )

        policy, configs = _real_policy_and_configs()
        isolated_engine = _isolated_engine()
        another_engine = _isolated_engine()
        fixture = _generation_fixture()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _verified_isolation(isolated_engine) as (
                isolation,
                _listen,
            ),
            patch(
                "harness.daily_real_replay."
                "_repository_create_schedule_occurrence"
            ) as create,
        ):
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            with self.assertRaisesRegex(
                DailyRealReplayError,
                "registered capability drift",
            ):
                create_real_replay_occurrence(
                    another_engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key="isolated-real-replay-v1-unit",
                    opened_at=datetime.now(timezone.utc),
                    epoch_payload=TEST_EPOCH,
                )
        create.assert_not_called()

    def test_isolated_epoch_authority_is_bound_to_guarded_engine(
        self,
    ) -> None:
        from harness.daily_real_replay import bind_real_replay_epoch
        from shared.daily_coordinator_mode import (
            assert_daily_coordinator_epoch_payload_matches_current,
        )

        engine = _isolated_engine()
        another_engine = _isolated_engine()
        with _verified_isolation(engine) as (isolation, _listen):
            identity = bind_real_replay_epoch(
                engine,
                isolation=isolation,
                epoch_payload=TEST_EPOCH,
            )
            self.assertEqual(identity.policy_payload(), TEST_EPOCH)
            self.assertEqual(identity.source, "isolated_real_replay")
            self.assertEqual(
                assert_daily_coordinator_epoch_payload_matches_current(
                    TEST_EPOCH,
                    engine=engine,
                ),
                identity,
            )
            with self.assertRaises(RuntimeError):
                assert_daily_coordinator_epoch_payload_matches_current(
                    TEST_EPOCH,
                )
            with self.assertRaises(RuntimeError):
                assert_daily_coordinator_epoch_payload_matches_current(
                    TEST_EPOCH,
                    engine=another_engine,
                )
            connection = engine.connect()
            self.assertEqual(
                assert_daily_coordinator_epoch_payload_matches_current(
                    TEST_EPOCH,
                    engine=connection,
                ),
                identity,
            )
            self.assertEqual(
                connection.info["daily-real-replay-v1"],
                SERVER_UUID,
            )
            engine._row["database_name"] = "bond_db"
            with self.assertRaisesRegex(
                RuntimeError,
                "guard validation|actual database identity",
            ):
                assert_daily_coordinator_epoch_payload_matches_current(
                    TEST_EPOCH,
                    engine=connection,
                )

    def test_shared_epoch_binder_rejects_forged_engine_metadata(
        self,
    ) -> None:
        from shared.daily_coordinator_mode import (
            bind_isolated_daily_coordinator_epoch,
        )

        engine = _isolated_engine()
        forged = SimpleNamespace(
            engine_identity=id(engine),
            database_name=DATABASE_NAME,
            server_uuid=SERVER_UUID,
            connection_guard=lambda _connection: None,
        )
        engine._bfl_real_replay_isolation = forged
        with self.assertRaisesRegex(
            RuntimeError,
            "registered database isolation",
        ):
            bind_isolated_daily_coordinator_epoch(
                engine,
                frozen=TEST_EPOCH,
                database_name=DATABASE_NAME,
                server_uuid=SERVER_UUID,
                isolation=forged,
                connection_marker_key="daily-real-replay-v1",
            )

    def test_shared_epoch_binder_rejects_unregistered_real_capability(
        self,
    ) -> None:
        from dataclasses import replace

        from shared.daily_coordinator_mode import (
            bind_isolated_daily_coordinator_epoch,
        )

        engine = _isolated_engine()
        with _verified_isolation(engine) as (isolation, _listen):
            forged = replace(isolation)
            engine._bfl_real_replay_isolation = forged
            with self.assertRaisesRegex(
                RuntimeError,
                "registered database isolation",
            ):
                bind_isolated_daily_coordinator_epoch(
                    engine,
                    frozen=TEST_EPOCH,
                    database_name=DATABASE_NAME,
                    server_uuid=SERVER_UUID,
                    isolation=forged,
                    connection_marker_key="daily-real-replay-v1",
                )

    def test_bound_epoch_rejects_removed_guard_listener(
        self,
    ) -> None:
        from harness.daily_real_replay import bind_real_replay_epoch
        from shared.daily_coordinator_mode import (
            assert_daily_coordinator_epoch_payload_matches_current,
        )

        engine = _isolated_engine()
        with _verified_isolation(engine) as (isolation, _listen):
            bind_real_replay_epoch(
                engine,
                isolation=isolation,
                epoch_payload=TEST_EPOCH,
            )
            connection = engine.connect()
            isolation.connection_guard(connection)
            with (
                patch(
                    "shared.daily_coordinator_mode.event.contains",
                    return_value=False,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "guard listener",
                ),
            ):
                assert_daily_coordinator_epoch_payload_matches_current(
                    TEST_EPOCH,
                    engine=connection,
                )

    def test_replay_epoch_rebinding_is_idempotent_but_immutable(
        self,
    ) -> None:
        from harness.daily_real_replay import bind_real_replay_epoch

        engine = _isolated_engine()
        with _verified_isolation(engine) as (isolation, _listen):
            first = bind_real_replay_epoch(
                engine,
                isolation=isolation,
                epoch_payload=TEST_EPOCH,
            )
            second = bind_real_replay_epoch(
                engine,
                isolation=isolation,
                epoch_payload=TEST_EPOCH,
            )
            self.assertEqual(first, second)
            changed = {**TEST_EPOCH, "epoch": TEST_EPOCH["epoch"] + 1}
            with self.assertRaisesRegex(
                RuntimeError,
                "binding changed",
            ):
                bind_real_replay_epoch(
                    engine,
                    isolation=isolation,
                    epoch_payload=changed,
                )

    def test_scheduled_executor_uses_bound_replay_epoch_before_claim(
        self,
    ) -> None:
        from harness.daily_real_replay import bind_real_replay_epoch
        from scheduler.scheduled_executor import execute_scheduled_item
        from tests.test_scheduled_executor import _envelope

        engine = _isolated_engine()
        another_engine = _isolated_engine()
        envelope = _envelope()
        envelope.occurrence.policy_json[
            "daily_coordinator_epoch"
        ] = dict(TEST_EPOCH)
        with _verified_isolation(engine) as (isolation, _listen):
            bind_real_replay_epoch(
                engine,
                isolation=isolation,
                epoch_payload=TEST_EPOCH,
            )
            with (
                patch(
                    "scheduler.scheduled_executor."
                    "read_schedule_execution_envelope",
                    return_value=envelope,
                ),
                patch(
                    "scheduler.scheduled_executor.start_schedule_attempt",
                    side_effect=RuntimeError("claim sentinel"),
                ) as claim,
            ):
                bound = execute_scheduled_item(engine, item_id=11)
                self.assertEqual(bound.status, "claim_rejected")
                claim.assert_called_once_with(
                    engine,
                    item_id=11,
                    trigger_origin="apscheduler",
                )

                claim.reset_mock()
                unbound = execute_scheduled_item(
                    another_engine,
                    item_id=11,
                )
                self.assertEqual(unbound.status, "claim_rejected")
                claim.assert_not_called()

    def test_registers_real_manifests_parent_first(self) -> None:
        from harness.daily_real_replay import (
            open_real_replay_generations,
            register_real_replay_generations,
        )

        fixture = _generation_fixture()
        engine = _isolated_engine()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            calls: list[dict[str, object]] = []

            def register(_engine, **kwargs):
                calls.append(kwargs)
                expected = (
                    17
                    if kwargs["generation_type"] == "native_source"
                    else 4
                )
                return kwargs["generation_id"], expected

            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_register_generation",
                    side_effect=register,
                ),
            ):
                result = register_real_replay_generations(
                    engine,
                    occurrence_id=41,
                    inputs=inputs,
                    isolation=isolation,
                )

        self.assertEqual(
            result,
            {
                "native_source": (native.generation_id, 17),
                "databridge_v1": (databridge.generation_id, 4),
            },
        )
        self.assertEqual(
            [call["generation_type"] for call in calls],
            ["native_source", "databridge_v1"],
        )
        self.assertEqual(calls[0]["occurrence_id"], 41)
        self.assertEqual(
            calls[0]["manifest_sha256"],
            native.manifest_sha256,
        )
        self.assertEqual(
            calls[1]["manifest_sha256"],
            databridge.manifest_sha256,
        )
        self.assertEqual(
            calls[1]["native_generation_id"],
            native.generation_id,
        )
        self.assertEqual(
            calls[1]["native_manifest_sha256"],
            native.manifest_sha256,
        )

    def test_database_identity_is_rechecked_before_registration(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            open_real_replay_generations,
            register_real_replay_generations,
        )

        engine = _isolated_engine()
        fixture = _generation_fixture()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _verified_isolation(engine) as (isolation, _listen),
            patch(
                "harness.daily_real_replay."
                "_repository_register_generation"
            ) as register,
        ):
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            engine._row["server_uuid"] = (
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            )
            with self.assertRaisesRegex(
                DailyRealReplayError,
                "identity drift",
            ):
                register_real_replay_generations(
                    engine,
                    occurrence_id=41,
                    inputs=inputs,
                    isolation=isolation,
                )
        register.assert_not_called()

    def test_registration_rehashes_databridge_before_parent_write(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            open_real_replay_generations,
            register_real_replay_generations,
        )

        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            _tamper_generation_file(
                databridge.data_dir / "daily_output.csv"
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_register_generation",
                ) as register,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "generation manifest revalidation failed",
                ),
            ):
                register_real_replay_generations(
                    engine,
                    occurrence_id=41,
                    inputs=inputs,
                    isolation=isolation,
                )
            register.assert_not_called()

    def test_registration_rejects_invalid_nested_context(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            open_real_replay_generations,
            register_real_replay_generations,
        )

        engine = _isolated_engine()
        fixture = _generation_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            native, databridge = fixture._delivery_generation(
                Path(tmpdir)
            )
            inputs = open_real_replay_generations(
                native_manifest=native.manifest_path,
                databridge_manifest=databridge.manifest_path,
            )
            invalid = replace(
                inputs,
                databridge_generation=SimpleNamespace(
                    manifest_path=databridge.manifest_path,
                ),
            )
            with (
                _verified_isolation(engine) as (isolation, _listen),
                patch(
                    "harness.daily_real_replay."
                    "_repository_register_generation",
                ) as register,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "verified generation inputs are required",
                ),
            ):
                register_real_replay_generations(
                    engine,
                    occurrence_id=41,
                    inputs=invalid,
                    isolation=isolation,
                )
            register.assert_not_called()


@unittest.skipUnless(
    os.environ.get("BFL_DAILY_REAL_REPLAY_MYSQL") == "1",
    "set BFL_DAILY_REAL_REPLAY_MYSQL=1 for isolated replay MySQL guard",
)
class DailyRealReplayMySQLGuardTests(unittest.TestCase):
    def test_real_engine_checks_identity_on_each_connection(self) -> None:
        from harness.daily_real_replay import (
            REAL_REPLAY_SCHEMA_VERSION,
            verify_real_replay_database,
        )

        with _temporary_mysql() as server:
            suffix = uuid.uuid4().hex[:10]
            schema = f"bfl_real_replay_{suffix}"
            username = f"bfl_rr_{suffix}"
            password = uuid.uuid4().hex + uuid.uuid4().hex
            admin = server._socket_admin_engine()
            try:
                with admin.begin() as connection:
                    connection.exec_driver_sql(
                        f"CREATE DATABASE `{schema}` "
                        "CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_0900_ai_ci"
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
            server._schema_credentials[schema] = (username, password)
            engine = server.engine(schema)
            try:
                isolation = verify_real_replay_database(
                    engine,
                    expected_database_name=schema,
                    expected_server_uuid=(
                        server.expected_server_uuid or ""
                    ),
                    expected_port=server.port,
                    expected_private_root=server.root,
                )
                with engine.begin() as connection:
                    self.assertEqual(
                        connection.info[REAL_REPLAY_SCHEMA_VERSION],
                        isolation.server_uuid,
                    )
                    connection.execute(
                        text(
                            "CREATE TABLE replay_guard_probe "
                            "(probe_id INT PRIMARY KEY)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO replay_guard_probe "
                            "(probe_id) VALUES (1)"
                        )
                    )
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.info[REAL_REPLAY_SCHEMA_VERSION],
                        isolation.server_uuid,
                    )
                    self.assertEqual(
                        connection.execute(
                            text(
                                "SELECT COUNT(*) "
                                "FROM replay_guard_probe"
                            )
                        ).scalar_one(),
                        1,
                    )
            finally:
                engine.dispose()

    def test_guarded_repository_creates_and_binds_21_25_occurrence(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            bind_real_replay_epoch,
            create_real_replay_occurrence,
            open_real_replay_generations,
            register_real_replay_generations,
            verify_real_replay_database,
        )
        from scheduler.repository import (
            read_schedule_occurrence_snapshot,
        )

        policy, configs = _real_policy_and_configs()
        fixture = _generation_fixture()
        with (
            _temporary_mysql() as server,
            tempfile.TemporaryDirectory() as generation_root,
        ):
            suffix = uuid.uuid4().hex[:10]
            schema = f"bfl_real_replay_{suffix}"
            username = f"bfl_rr_{suffix}"
            password = uuid.uuid4().hex + uuid.uuid4().hex
            admin = server._socket_admin_engine()
            try:
                with admin.begin() as connection:
                    connection.exec_driver_sql(
                        f"CREATE DATABASE `{schema}` "
                        "CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_0900_ai_ci"
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
            server._schema_credentials[schema] = (username, password)
            engine = server.engine(schema)
            try:
                isolation = verify_real_replay_database(
                    engine,
                    expected_database_name=schema,
                    expected_server_uuid=(
                        server.expected_server_uuid or ""
                    ),
                    expected_port=server.port,
                    expected_private_root=server.root,
                )
                apply_migration_files(engine, MIGRATIONS)
                _seed_test_registry(engine, policy, configs)
                native, databridge = fixture._delivery_generation(
                    Path(generation_root)
                )
                inputs = open_real_replay_generations(
                    native_manifest=native.manifest_path,
                    databridge_manifest=databridge.manifest_path,
                )
                bind_real_replay_epoch(
                    engine,
                    isolation=isolation,
                    epoch_payload=TEST_EPOCH,
                )
                opened_at = datetime.now(timezone.utc)
                occurrence_id = create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key=(
                        f"isolated-real-replay-v1-{suffix}"
                    ),
                    opened_at=opened_at,
                    epoch_payload=TEST_EPOCH,
                )
                bindings = register_real_replay_generations(
                    engine,
                    occurrence_id=occurrence_id,
                    inputs=inputs,
                    isolation=isolation,
                )
                snapshot = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=occurrence_id,
                )
            finally:
                engine.dispose()

        self.assertEqual(snapshot.occurrence.expected_item_count, 21)
        self.assertEqual(snapshot.occurrence.expected_target_count, 25)
        self.assertEqual(snapshot.actual_item_count, 21)
        self.assertEqual(snapshot.actual_target_count, 25)
        self.assertEqual(len(snapshot.items), 21)
        next_local_date = (
            opened_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            + timedelta(days=1)
        )
        expected_cutoff = datetime(
            next_local_date.year,
            next_local_date.month,
            next_local_date.day,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).astimezone(timezone.utc)
        expected_stored_cutoff = expected_cutoff.replace(tzinfo=None)
        self.assertEqual(
            snapshot.occurrence.sla_deadline_at,
            expected_stored_cutoff,
        )
        self.assertEqual(
            snapshot.occurrence.recovery_cutoff_at,
            expected_stored_cutoff,
        )
        self.assertTrue(
            all(
                summary.item.deadline_at == expected_stored_cutoff
                for summary in snapshot.items
            )
        )
        self.assertEqual(
            bindings,
            {
                "native_source": (native.generation_id, 17),
                "databridge_v1": (
                    databridge.generation_id,
                    4,
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
