from __future__ import annotations

import inspect
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError

from scheduler.discovery import discover_schemes
from shared.daily_coordinator_mode import DailyCoordinatorEpochIdentity
from shared.models import PredictionRecord


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAILY_POLICY_PATH = (
    PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
)


class _FrozenLedgerClock:
    def __init__(self, value: datetime) -> None:
        self._value = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    def now_utc(self) -> datetime:
        return self._value


class _MutableLedgerClock(_FrozenLedgerClock):
    def set(self, value: datetime) -> None:
        self._value = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class _SequenceLedgerClock:
    def __init__(self, *values: datetime) -> None:
        self._values = [
            (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            )
            for value in values
        ]

    def now_utc(self) -> datetime:
        if not self._values:
            raise AssertionError("ledger clock sequence exhausted")
        return self._values.pop(0)


class _StaticCompletionVerifier:
    def __init__(self, **overrides: object) -> None:
        self._overrides = overrides
        self.expectation = None

    def verify(self, expectation):
        from scheduler.repository import ScheduledCompletionEvidence

        self.expectation = expectation
        values = {
            "observed_generation_id": "generation-20260724",
            "manifest_sha256": "e" * 64,
            "generation_dataset_content_id":
                expectation.generation_dataset_content_id,
            "generation_schema_version":
                expectation.generation_schema_version,
            "generation_exporter_version":
                expectation.generation_exporter_version,
            "feature_date": "2026-07-23",
            "scheme_version": "alpha-v1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "native_generation_id": expectation.native_generation_id,
            "native_manifest_sha256":
                expectation.native_manifest_sha256,
            "native_dataset_content_id":
                expectation.native_dataset_content_id,
            "native_schema_version":
                expectation.native_schema_version,
            "native_exporter_version":
                expectation.native_exporter_version,
            "native_feature_date": expectation.native_feature_date,
        }
        values.update(self._overrides)
        return ScheduledCompletionEvidence(**values)


class _ReturningCompletionVerifier:
    def __init__(self, value: object) -> None:
        self._value = value

    def verify(self, _expectation):
        return self._value


class _ClockAdvancingCompletionVerifier(_StaticCompletionVerifier):
    def __init__(self, clock: _MutableLedgerClock, value: datetime) -> None:
        super().__init__()
        self._clock = clock
        self._value = value

    def verify(self, expectation):
        evidence = super().verify(expectation)
        self._clock.set(self._value)
        return evidence


class _RaisingCompletionVerifier:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def verify(self, _expectation):
        raise self._error


class _EmptyMappingResult:
    def mappings(self):
        return self

    def one_or_none(self):
        return None

    def all(self):
        return []


class _MysqlSqlCaptureConnection:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, _params=None):
        self.statements.append(" ".join(str(statement).split()).lower())
        return _EmptyMappingResult()


class DailyLedgerRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._mode_patcher = patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
        )
        self._mode_patcher.start()
        self._epoch_identity = DailyCoordinatorEpochIdentity(
            epoch=1,
            mode="ledger",
            previous_record_sha256="0" * 64,
            record_sha256="1" * 64,
            source="test",
            transition_id="test-epoch-1",
        )
        self._identity_patcher = patch(
            "shared.daily_coordinator_mode."
            "require_current_daily_coordinator_identity",
            return_value=self._epoch_identity,
        )
        self._identity_patcher.start()
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            for statement in _SQLITE_SCHEMA:
                conn.exec_driver_sql(statement)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._identity_patcher.stop()
        self._mode_patcher.stop()

    def _function(self, name: str):
        from scheduler import repository

        function = getattr(repository, name, None)
        self.assertIsNotNone(function, f"scheduler.repository.{name} is missing")
        if name == "create_schedule_occurrence":
            def create_with_epoch(*args, **kwargs):
                create_kwargs = dict(kwargs)
                policy = dict(create_kwargs.get("policy_json") or {})
                policy.setdefault(
                    "daily_coordinator_epoch",
                    self._epoch_identity.policy_payload(),
                )
                create_kwargs["policy_json"] = policy
                return function(*args, **create_kwargs)

            return create_with_epoch
        if name == "upsert_scheduler_heartbeat":
            def heartbeat_with_epoch(*args, **kwargs):
                heartbeat_kwargs = dict(kwargs)
                if heartbeat_kwargs.get("service_name") == "daily-coordinator":
                    details = dict(heartbeat_kwargs.get("details") or {})
                    details.setdefault(
                        "daily_coordinator_epoch",
                        self._epoch_identity.policy_payload(),
                    )
                    heartbeat_kwargs["details"] = details
                return function(*args, **heartbeat_kwargs)

            return heartbeat_with_epoch
        if name == "start_schedule_attempt":
            def start_with_simulated_process(*args, **kwargs):
                start_kwargs = dict(kwargs)
                requested_process_id = start_kwargs.pop(
                    "process_id",
                    None,
                )
                requested_process_group_id = start_kwargs.pop(
                    "process_group_id",
                    None,
                )
                attempt = function(*args, **start_kwargs)
                repository.register_schedule_attempt_process(
                    args[0],
                    run_id=attempt.run_id,
                    execution_token=attempt.execution_token,
                    process_id=(
                        requested_process_id
                        if requested_process_id is not None
                        else 900_000 + attempt.run_id
                    ),
                    process_group_id=(
                        requested_process_group_id
                        if requested_process_group_id is not None
                        else 900_000 + attempt.run_id
                    ),
                    started_at=kwargs.get("started_at"),
                    _clock=kwargs.get("_clock"),
                )
                return attempt

            return start_with_simulated_process
        return function

    @staticmethod
    def _clock(value: datetime) -> _FrozenLedgerClock:
        return _FrozenLedgerClock(value)

    def _capture_sql(self) -> tuple[list[str], object]:
        statements: list[str] = []

        def capture(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(" ".join(str(statement).split()).lower())

        event.listen(self.engine, "before_cursor_execute", capture)
        return statements, capture

    @staticmethod
    def _sql_index(statements: list[str], *fragments: str) -> int:
        for index, statement in enumerate(statements):
            if all(fragment.lower() in statement for fragment in fragments):
                return index
        raise AssertionError(
            f"SQL statement not observed for fragments={fragments}: "
            f"{statements}"
        )

    @staticmethod
    def _sql_index_after(
        statements: list[str],
        after_index: int,
        *fragments: str,
    ) -> int:
        for index in range(after_index + 1, len(statements)):
            statement = statements[index]
            if all(fragment.lower() in statement for fragment in fragments):
                return index
        raise AssertionError(
            f"SQL statement not observed after index={after_index} "
            f"for fragments={fragments}: {statements}"
        )

    def _seed_registry(
        self,
        *,
        base_scheme_id: str = "alpha",
        runtime_type: str = "native_adapter",
        targets: tuple[tuple[str, int], ...] = (("5Y", 1), ("10Y", 1)),
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_versions
                        (scheme_id, scheme_version, runtime_type, code_hash,
                         config_hash, status)
                    VALUES
                        (:scheme_id, :scheme_version, :runtime_type, :code_hash,
                         :config_hash, 'active')
                    """
                ),
                {
                    "scheme_id": base_scheme_id,
                    "scheme_version": f"{base_scheme_id}-v1",
                    "runtime_type": runtime_type,
                    "code_hash": "a" * 64,
                    "config_hash": "b" * 64,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, runtime_type, status, frequency,
                         task_type, target_tenor, horizon)
                    VALUES
                        (:scheme_id, :base_scheme_id, :runtime_type, 'active', 'daily',
                         :task_type, :target_tenor, :horizon)
                    """
                ),
                [
                    {
                        "scheme_id": f"{base_scheme_id}__h{horizon}__{tenor}",
                        "base_scheme_id": base_scheme_id,
                        "runtime_type": runtime_type,
                        "task_type": "T+1" if horizon == 1 else "T+5",
                        "target_tenor": tenor,
                        "horizon": horizon,
                    }
                    for tenor, horizon in targets
                ],
            )

    def _daily_policy_matrix(self):
        from scheduler.daily_policy import load_daily_policy

        active_daily = tuple(
            config
            for config in discover_schemes(strict=True)
            if config.status == "active" and config.frequency == "daily"
        )
        policy = load_daily_policy(
            DAILY_POLICY_PATH,
            discovered=active_daily,
        )
        for scheme_id, item in policy.schemes.items():
            self._seed_registry(
                base_scheme_id=scheme_id,
                runtime_type=item.runtime_type,
                targets=tuple(
                    (tenor, item.horizon)
                    for tenor in item.target_tenors
                ),
            )
        return policy

    def _daily_policy_occurrence_args(
        self,
        policy,
    ) -> dict[str, object]:
        from scheduler.daily_runtime import _policy_payload

        release_base = datetime(2026, 7, 23, 22, 30)
        target_dates = {
            f"{scheme_id}__h{item.horizon}__{tenor}": (
                "2026-07-24"
                if item.horizon == 1
                else "2026-07-30"
            )
            for scheme_id, item in policy.schemes.items()
            for tenor in item.target_tenors
        }
        item_policy_by_base = {
            scheme_id: {
                "scheme_version": f"{scheme_id}-v1",
                "code_sha256": "a" * 64,
                "config_sha256": "b" * 64,
                "cache_group": item.cache_group,
                "resource_class": item.resource_class,
                "internal_workers": item.internal_workers,
                "release_offset_minutes":
                    item.v2_release_offset_min or 0,
                "release_at": release_base + timedelta(
                    minutes=item.v2_release_offset_min or 0,
                ),
                "deadline_at": datetime(2026, 7, 23, 23, 55),
            }
            for scheme_id, item in policy.schemes.items()
        }
        return {
            "schedule_key": "daily-production",
            "predict_date": "2026-07-24",
            "feature_date": "2026-07-23",
            "target_dates": target_dates,
            "item_policy_by_base": item_policy_by_base,
            "policy_version": policy.version,
            "policy_json": _policy_payload(
                policy,
                daily_coordinator_epoch=(
                    self._epoch_identity.policy_payload()
                ),
            ),
        }

    def _create_generation(
        self,
        *,
        generation_id: str = "generation-20260724",
    ) -> str:
        return self._function("create_input_generation")(
            self.engine,
            **self._generation_params(generation_id),
        )

    def _create_native_calendar_generation(
        self,
        *,
        generation_id: str = "native-calendar-20260724",
    ) -> str:
        params = self._generation_params(generation_id)
        params["manifest_sha256"] = "f" * 64
        created = self._function("create_input_generation")(
            self.engine,
            **params,
        )
        self._function("seal_input_generation")(
            self.engine,
            generation_id=created,
            sealed_at=datetime(2026, 7, 23, 23, 5),
        )
        return created

    @staticmethod
    def _generation_params(generation_id: str) -> dict[str, str]:
        return {
            "generation_id": generation_id,
            "generation_type": "native_source",
            "business_date": "2026-07-24",
            "feature_date": "2026-07-23",
            "readiness_basis": "UPSTREAM_SEAL",
            "source_commit_token": "c" * 64,
            "dataset_content_id": "d" * 64,
            "schema_version": "daily-input-v1",
            "exporter_version": "exporter-v1",
            "manifest_uri": f"/manifests/{generation_id}.json",
            "manifest_sha256": "e" * 64,
        }

    @staticmethod
    def _target_dates(
        *,
        base_scheme_id: str,
        targets: tuple[tuple[str, int], ...],
    ) -> dict[str, str]:
        return {
            f"{base_scheme_id}__h{horizon}__{tenor}": "2026-07-25"
            for tenor, horizon in targets
        }

    @staticmethod
    def _item_policy(
        *base_scheme_ids: str,
    ) -> dict[str, dict[str, object]]:
        return {
            base_scheme_id: {
                "scheme_version": f"{base_scheme_id}-v1",
                "code_sha256": "a" * 64,
                "config_sha256": "b" * 64,
                "cache_group": f"{base_scheme_id}-cache",
                "resource_class": "cpu_standard",
                "internal_workers": 2,
                "release_offset_minutes": 0,
                "release_at": datetime(2026, 7, 23, 23, 0),
                "deadline_at": datetime(2026, 7, 24, 0, 30),
            }
            for base_scheme_id in base_scheme_ids
        }

    @staticmethod
    def _trusted_completion_verifier(
        **overrides: object,
    ) -> _StaticCompletionVerifier:
        return _StaticCompletionVerifier(**overrides)

    def _create_occurrence(
        self,
        *,
        generation_status: str = "SEALED",
        base_scheme_id: str = "alpha",
        runtime_type: str = "native_adapter",
        targets: tuple[tuple[str, int], ...] = (("5Y", 1), ("10Y", 1)),
    ) -> tuple[int, int]:
        self._seed_registry(
            base_scheme_id=base_scheme_id,
            runtime_type=runtime_type,
            targets=targets,
        )
        self._create_generation()
        if generation_status == "SEALED":
            self._function("seal_input_generation")(
                self.engine,
                generation_id="generation-20260724",
            )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id=base_scheme_id,
                targets=targets,
            ),
            item_policy_by_base=self._item_policy(base_scheme_id),
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id "
                        "AND base_scheme_id = :base_scheme_id"
                    ),
                    {
                        "occurrence_id": occurrence_id,
                        "base_scheme_id": base_scheme_id,
                    },
                ).scalar_one()
            )
        if generation_status == "SEALED":
            self._function("bind_schedule_item_input_generation")(
                self.engine,
                item_id=item_id,
                generation_id="generation-20260724",
                expected_feature_date="2026-07-23",
            )
        return occurrence_id, item_id

    def test_occurrence_first_write_rejects_missing_epoch_capability(
        self,
    ) -> None:
        from scheduler import repository

        self._seed_registry()
        with self.assertRaisesRegex(
            RuntimeError,
            "new daily occurrence coordinator epoch is missing",
        ):
            repository.create_schedule_occurrence(
                self.engine,
                schedule_key="daily-production",
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_dates=self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1), ("10Y", 1)),
                ),
                item_policy_by_base=self._item_policy("alpha"),
                policy_json={},
            )
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text("SELECT COUNT(*) FROM t_schedule_occurrences")
                ).scalar_one(),
                0,
            )

    def _create_two_item_occurrence(
        self,
        *,
        bind_generation: bool = True,
    ) -> tuple[int, dict[str, int]]:
        for base_scheme_id, tenor in (("alpha", "5Y"), ("beta", "10Y")):
            self._seed_registry(
                base_scheme_id=base_scheme_id,
                targets=((tenor, 1),),
            )
        self._create_generation()
        self._function("seal_input_generation")(
            self.engine,
            generation_id="generation-20260724",
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates={
                **self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1),),
                ),
                **self._target_dates(
                    base_scheme_id="beta",
                    targets=(("10Y", 1),),
                ),
            },
            item_policy_by_base=self._item_policy("alpha", "beta"),
        )
        with self.engine.connect() as conn:
            item_ids = {
                str(row["base_scheme_id"]): int(row["item_id"])
                for row in conn.execute(
                    text(
                        "SELECT item_id, base_scheme_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).mappings()
            }
        if bind_generation:
            for item_id in item_ids.values():
                self._function("bind_schedule_item_input_generation")(
                    self.engine,
                    item_id=item_id,
                    generation_id="generation-20260724",
                    expected_feature_date="2026-07-23",
                )
        return occurrence_id, item_ids

    @staticmethod
    def _records(
        *,
        base_scheme_id: str = "alpha",
        targets: tuple[tuple[str, int], ...] = (("5Y", 1), ("10Y", 1)),
    ) -> list[PredictionRecord]:
        return [
            PredictionRecord(
                scheme_id=base_scheme_id,
                target_tenor=tenor,
                horizon=horizon,
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_date="2026-07-25",
                prediction_phase="scheduled_live",
                predicted_direction=1,
                extra={"feature_date": "2026-07-23"},
            )
            for tenor, horizon in targets
        ]

    def _create_completed_single_target(
        self,
    ) -> tuple[int, int, int, int, str]:
        """创建一条具备完整 run/prediction 证据的成功 target。"""
        occurrence_id, item_id = self._create_occurrence(
            targets=(("5Y", 1),),
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_input_generations "
                    "SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(targets=(("5Y", 1),)),
            trusted_verifier=self._trusted_completion_verifier(),
            completed_at=datetime(2026, 7, 23, 23, 40),
            _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
        )
        with self.engine.connect() as conn:
            target = conn.execute(
                text(
                    "SELECT target_id, accepted_prediction_id, "
                    "registry_scheme_id "
                    "FROM t_schedule_item_targets "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).mappings().one()
        return (
            occurrence_id,
            item_id,
            int(attempt.run_id),
            int(target["accepted_prediction_id"]),
            str(target["registry_scheme_id"]),
        )

    def test_create_scheme_run_accepts_optional_schedule_audit_fields(self) -> None:
        from scheduler.repository import create_scheme_run

        signature = inspect.signature(create_scheme_run)
        expected_parameters = {
            "schedule_item_id",
            "attempt_no",
            "trigger_origin",
            "queued_at",
            "failure_code",
            "execution_token",
            "process_id",
            "process_group_id",
            "schedule_frequency",
        }
        self.assertTrue(
            expected_parameters.issubset(signature.parameters),
            f"missing parameters: {sorted(expected_parameters - set(signature.parameters))}",
        )

        run_id = create_scheme_run(
            self.engine,
            scheme_id="alpha",
            predict_date="2026-07-24",
            prediction_phase="scheduled_live",
            schedule_item_id=8,
            attempt_no=2,
            trigger_origin="operator_recovery",
            queued_at=datetime(2026, 7, 24, 7, 20),
            failure_code=None,
            execution_token="token-2",
            process_id=123,
            process_group_id=456,
            _clock=self._clock(datetime(2026, 7, 24, 7, 20)),
        )

        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM t_scheme_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).mappings().one()
        self.assertEqual(row["schedule_item_id"], 8)
        self.assertEqual(row["attempt_no"], 2)
        self.assertEqual(row["trigger_origin"], "operator_recovery")
        self.assertEqual(row["execution_token"], "token-2")
        self.assertEqual(row["process_id"], 123)
        self.assertEqual(row["process_group_id"], 456)

    def test_engine_factory_initializes_and_repairs_mysql_utc_session(
        self,
    ) -> None:
        from scheduler import repository

        fake_engine = object()
        cfg = SimpleNamespace(
            user="bond",
            password="secret",
            host="127.0.0.1",
            port=3306,
            database="bond_db",
            charset="utf8mb4",
        )
        with (
            patch.object(
                repository.DatabaseConfig,
                "from_env",
                return_value=cfg,
            ),
            patch.object(
                repository,
                "create_engine",
                return_value=fake_engine,
            ) as create_engine_mock,
            patch("sqlalchemy.event.listen") as listen_mock,
        ):
            actual = repository.create_engine_from_env()

        self.assertIs(actual, fake_engine)
        self.assertEqual(
            create_engine_mock.call_args.kwargs["connect_args"],
            {
                "init_command": (
                    "SET SESSION time_zone = '+00:00'"
                )
            },
        )
        checkout_calls = [
            call
            for call in listen_mock.call_args_list
            if call.args[1] == "checkout"
        ]
        self.assertEqual(len(checkout_calls), 1)
        callback = checkout_calls[0].args[2]

        statements: list[str] = []

        class Cursor:
            def execute(self, statement: str) -> None:
                statements.append(statement)

            def close(self) -> None:
                return None

        class DbapiConnection:
            def cursor(self) -> Cursor:
                return Cursor()

        callback(DbapiConnection(), None, None)
        self.assertEqual(
            statements,
            ["SET SESSION time_zone = '+00:00'"],
        )

    def test_scheme_run_rejects_unapproved_trigger_and_oversized_failure(
        self,
    ) -> None:
        create_run = self._function("create_scheme_run")
        with self.assertRaisesRegex(ValueError, "trigger_origin"):
            create_run(
                self.engine,
                scheme_id="alpha",
                predict_date="2026-07-24",
                trigger_origin="manual",
            )
        with self.assertRaisesRegex(ValueError, "64"):
            create_run(
                self.engine,
                scheme_id="alpha",
                predict_date="2026-07-24",
                failure_code="X" * 65,
            )

    def test_opt_in_creation_fence_rejects_unbound_scheduled_live(self) -> None:
        create_run = self._function("create_scheme_run")

        legacy_run_id = create_run(
            self.engine,
            scheme_id="legacy",
            predict_date="2026-07-24",
            prediction_phase="scheduled_live",
        )
        with self.assertRaisesRegex(RuntimeError, "daily ledger"):
            create_run(
                self.engine,
                scheme_id="fenced",
                predict_date="2026-07-24",
                prediction_phase="scheduled_live",
                enforce_scheduled_live_ledger=True,
            )

        self.assertGreater(legacy_run_id, 0)

    def test_ledger_mode_rejects_unknown_or_daily_unbound_scheduled_live(
        self,
    ) -> None:
        create_run = self._function("create_scheme_run")

        with patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
        ):
            for frequency in (None, "daily"):
                with self.subTest(frequency=frequency):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "daily ledger item",
                    ):
                        create_run(
                            self.engine,
                            scheme_id="daily",
                            predict_date="2026-07-24",
                            prediction_phase="scheduled_live",
                            schedule_frequency=frequency,
                        )
            weekly_run = create_run(
                self.engine,
                scheme_id="weekly",
                predict_date="2026-07-24",
                prediction_phase="scheduled_live",
                schedule_frequency="weekly",
            )

        self.assertGreater(weekly_run, 0)

    def test_ledger_bound_run_rejects_generic_insert_finish_and_fail(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        with self.assertRaisesRegex(RuntimeError, "ledger-bound"):
            self._function("insert_run_predictions")(
                self.engine,
                attempt.run_id,
                self._records(),
                scheme_version="alpha-v1",
            )
        with self.assertRaisesRegex(RuntimeError, "ledger-bound"):
            self._function("finish_scheme_run")(
                self.engine,
                run_id=attempt.run_id,
                status="success",
            )
        with self.assertRaisesRegex(RuntimeError, "ledger-bound"):
            self._function("fail_scheme_run_atomic")(
                self.engine,
                run_id=attempt.run_id,
                scheme_id="alpha",
                run_date="2026-07-24",
                duration_sec=1.0,
                records_returned=0,
                error_message="must use ledger failure API",
            )

        self._assert_no_predictions()
        with self.engine.connect() as conn:
            state = conn.execute(
                text(
                    "SELECT r.status AS run_status, i.state AS item_state "
                    "FROM t_scheme_runs r JOIN t_schedule_items i "
                    "ON i.item_id = r.schedule_item_id "
                    "WHERE r.run_id = :run_id"
                ),
                {"run_id": attempt.run_id},
            ).mappings().one()
        self.assertEqual(state["run_status"], "running")
        self.assertEqual(state["item_state"], "RUNNING")

    def test_generic_run_timestamps_use_internal_clock_and_order(self) -> None:
        run_id = self._function("create_scheme_run")(
            self.engine,
            scheme_id="alpha",
            predict_date="2026-07-24",
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_runs SET started_at = :started_at "
                    "WHERE run_id = :run_id"
                ),
                {
                    "run_id": run_id,
                    "started_at": datetime(2026, 7, 24, 0, 7),
                },
            )
        with self.assertRaisesRegex(ValueError, "internal _clock seam"):
            self._function("finish_scheme_run")(
                self.engine,
                run_id=run_id,
                status="success",
                finished_at=datetime(2026, 7, 24, 0, 7, 8),
            )
        with self.assertRaisesRegex(RuntimeError, "before run started_at"):
            self._function("finish_scheme_run")(
                self.engine,
                run_id=run_id,
                status="success",
                finished_at=datetime(2026, 7, 24, 0, 6),
                _clock=self._clock(datetime(2026, 7, 24, 0, 8)),
            )
        with self.assertRaisesRegex(RuntimeError, "future"):
            self._function("finish_scheme_run")(
                self.engine,
                run_id=run_id,
                status="success",
                finished_at=datetime(2026, 7, 24, 0, 9),
                _clock=self._clock(datetime(2026, 7, 24, 0, 8)),
            )
        self._function("finish_scheme_run")(
            self.engine,
            run_id=run_id,
            status="success",
            finished_at=datetime(2026, 7, 24, 0, 7, 8, 123456),
            _clock=self._clock(datetime(2026, 7, 24, 0, 8)),
        )

        with self.engine.connect() as conn:
            finished_at = conn.execute(
                text(
                    "SELECT finished_at FROM t_scheme_runs "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            ).scalar_one()
        self.assertEqual(
            str(finished_at),
            "2026-07-24 00:07:08.123456",
        )

    def test_generic_queued_at_override_requires_internal_clock(self) -> None:
        create = self._function("create_scheme_run")
        with self.assertRaisesRegex(ValueError, "internal _clock seam"):
            create(
                self.engine,
                scheme_id="alpha",
                predict_date="2026-07-24",
                queued_at=datetime(2026, 7, 24, 0, 1),
            )
        with self.assertRaisesRegex(RuntimeError, "future"):
            create(
                self.engine,
                scheme_id="alpha",
                predict_date="2026-07-24",
                queued_at=datetime(2026, 7, 24, 0, 2),
                _clock=self._clock(datetime(2026, 7, 24, 0, 1)),
            )

    def test_input_generation_create_is_idempotent(self) -> None:
        first = self._create_generation(generation_id="generation-a")
        second = self._create_generation(generation_id="generation-a")

        self.assertEqual(first, "generation-a")
        self.assertEqual(second, "generation-a")
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text("SELECT COUNT(*) FROM t_input_generations")
                ).scalar_one(),
                1,
            )

    def test_input_generation_identity_fields_fail_closed(self) -> None:
        create_generation = self._function("create_input_generation")
        invalid_cases = (
            ("generation_type", "legacy"),
            ("readiness_basis", "manual"),
            ("source_commit_token", "unsafe token with spaces"),
            ("dataset_content_id", "d" * 129),
            ("manifest_sha256", "z" * 64),
        )

        for field, value in invalid_cases:
            with self.subTest(field=field):
                params = self._generation_params(f"invalid-{field}")
                params[field] = value
                with self.assertRaises(ValueError):
                    create_generation(self.engine, **params)

    def test_input_generation_accepts_upstream_tokens_and_prefixed_content_ids(
        self,
    ) -> None:
        params = self._generation_params("logical-generation")
        params["source_commit_token"] = "mysql-gtid:24bc/17"
        params["dataset_content_id"] = f"sha256:{'d' * 64}"

        generation_id = self._function("create_input_generation")(
            self.engine,
            **params,
        )

        self.assertEqual(generation_id, "logical-generation")

    def test_generation_can_be_sealed_then_invalidated_with_reason(self) -> None:
        seal_generation = self._function("seal_input_generation")
        invalidate_generation = self._function("invalidate_input_generation")
        self._create_generation(generation_id="generation-a")

        seal_generation(self.engine, generation_id="generation-a")
        invalidate_generation(
            self.engine,
            generation_id="generation-a",
            reason="source revision",
        )

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT state, sealed_at, invalidated_at, invalid_reason "
                    "FROM t_input_generations WHERE generation_id = 'generation-a'"
                )
            ).mappings().one()
        self.assertEqual(row["state"], "INVALIDATED")
        self.assertIsNotNone(row["sealed_at"])
        self.assertIsNotNone(row["invalidated_at"])
        self.assertEqual(row["invalid_reason"], "source revision")

    def test_cleanup_resolver_only_returns_unreferenced_invalidated_payloads(
        self,
    ) -> None:
        create = self._function("create_input_generation")
        seal = self._function("seal_input_generation")
        invalidate = self._function("invalidate_input_generation")
        resolve = self._function(
            "resolve_reclaimable_generation_payloads"
        )

        for generation_id in (
            "invalidated-free",
            "invalidated-parent",
            "sealed-free",
        ):
            create(
                self.engine,
                **self._generation_params(generation_id),
            )
            seal(self.engine, generation_id=generation_id)
        invalidate(
            self.engine,
            generation_id="invalidated-free",
            reason="unreferenced invalid payload",
        )

        child = self._generation_params("databridge-child")
        child["generation_type"] = "databridge_v1"
        create(
            self.engine,
            **child,
            native_generation_id="invalidated-parent",
            native_manifest_sha256="e" * 64,
        )
        invalidate(
            self.engine,
            generation_id="invalidated-parent",
            reason="still transitively referenced",
        )

        reclaimable = resolve(self.engine)

        self.assertEqual(
            tuple(item.generation_id for item in reclaimable),
            ("invalidated-free",),
        )
        self.assertTrue(
            all(item.state == "INVALIDATED" for item in reclaimable)
        )

    def test_reclaim_finalization_is_leaf_first_and_crash_idempotent(
        self,
    ) -> None:
        create = self._function("create_input_generation")
        seal = self._function("seal_input_generation")
        invalidate = self._function("invalidate_input_generation")
        resolve = self._function(
            "resolve_reclaimable_generation_payloads"
        )
        finalize = self._function(
            "finalize_reclaimed_generation_payload"
        )

        parent = self._generation_params("native-reclaim-parent")
        create(self.engine, **parent)
        seal(self.engine, generation_id=parent["generation_id"])
        child = self._generation_params("databridge-reclaim-child")
        child["generation_type"] = "databridge_v1"
        create(
            self.engine,
            **child,
            native_generation_id=parent["generation_id"],
            native_manifest_sha256=parent["manifest_sha256"],
        )
        seal(self.engine, generation_id=child["generation_id"])
        invalidate(
            self.engine,
            generation_id=child["generation_id"],
            reason="child payload obsolete",
        )
        invalidate(
            self.engine,
            generation_id=parent["generation_id"],
            reason="parent payload obsolete",
        )

        first_wave = resolve(self.engine)
        self.assertEqual(
            tuple(item.generation_id for item in first_wave),
            ("databridge-reclaim-child",),
        )
        child_envelope = first_wave[0]
        self.assertTrue(
            finalize(
                self.engine,
                generation_id=child_envelope.generation_id,
                expected_generation_type=(
                    child_envelope.generation_type
                ),
                expected_business_date=child_envelope.business_date,
                expected_feature_date=child_envelope.feature_date,
                expected_manifest_uri=child_envelope.manifest_uri,
                expected_manifest_sha256=(
                    child_envelope.manifest_sha256
                ),
            )
        )
        # 模拟 payload 已删、DB tombstone finalize 返回前崩溃后的重入。
        self.assertFalse(
            finalize(
                self.engine,
                generation_id=child_envelope.generation_id,
                expected_generation_type=(
                    child_envelope.generation_type
                ),
                expected_business_date=child_envelope.business_date,
                expected_feature_date=child_envelope.feature_date,
                expected_manifest_uri=child_envelope.manifest_uri,
                expected_manifest_sha256=(
                    child_envelope.manifest_sha256
                ),
            )
        )

        second_wave = resolve(self.engine)
        self.assertEqual(
            tuple(item.generation_id for item in second_wave),
            ("native-reclaim-parent",),
        )

    def test_reclaim_finalization_revalidates_exact_db_fence(self) -> None:
        create = self._function("create_input_generation")
        seal = self._function("seal_input_generation")
        invalidate = self._function("invalidate_input_generation")
        resolve = self._function(
            "resolve_reclaimable_generation_payloads"
        )
        finalize = self._function(
            "finalize_reclaimed_generation_payload"
        )
        generation = self._generation_params("invalidated-exact")
        create(self.engine, **generation)
        seal(self.engine, generation_id=generation["generation_id"])
        invalidate(
            self.engine,
            generation_id=generation["generation_id"],
            reason="obsolete",
        )
        envelope = resolve(self.engine)[0]

        with self.assertRaisesRegex(RuntimeError, "DB fence"):
            finalize(
                self.engine,
                generation_id=envelope.generation_id,
                expected_generation_type=envelope.generation_type,
                expected_business_date=envelope.business_date,
                expected_feature_date=envelope.feature_date,
                expected_manifest_uri=envelope.manifest_uri,
                expected_manifest_sha256="f" * 64,
            )
        self.assertEqual(
            tuple(
                item.generation_id
                for item in resolve(self.engine)
            ),
            ("invalidated-exact",),
        )

    def test_databridge_seal_rechecks_linked_native_generation(self) -> None:
        native_generation_id = self._create_native_calendar_generation()
        databridge = self._generation_params("databridge-seal-race")
        databridge["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **databridge,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        self._function("invalidate_input_generation")(
            self.engine,
            generation_id=native_generation_id,
            reason="late-write-detected",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "native generation relation is not SEALED",
        ):
            self._function("seal_input_generation")(
                self.engine,
                generation_id="databridge-seal-race",
            )
        with self.engine.connect() as conn:
            state = conn.execute(
                text(
                    "SELECT state FROM t_input_generations "
                    "WHERE generation_id = 'databridge-seal-race'"
                )
            ).scalar_one()
        self.assertEqual(state, "BUILDING")

    def test_repeated_occurrence_rejects_dynamic_registry_drift(
        self,
    ) -> None:
        self._seed_registry(base_scheme_id="alpha", targets=(("5Y", 1), ("10Y", 1)))
        self._seed_registry(base_scheme_id="beta", targets=(("1Y", 5),))
        create_occurrence = self._function("create_schedule_occurrence")

        first = create_occurrence(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates={
                **self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1), ("10Y", 1)),
                ),
                **self._target_dates(
                    base_scheme_id="beta",
                    targets=(("1Y", 5),),
                ),
            },
            item_policy_by_base=self._item_policy("alpha", "beta"),
        )
        self._seed_registry(base_scheme_id="later", targets=(("7Y", 1),))
        with self.assertRaisesRegex(
            RuntimeError,
            "occurrence immutable snapshot mismatch",
        ):
            create_occurrence(
                self.engine,
                schedule_key="daily-production",
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_dates={
                    **self._target_dates(
                        base_scheme_id="alpha",
                        targets=(("5Y", 1), ("10Y", 1)),
                    ),
                    **self._target_dates(
                        base_scheme_id="beta",
                        targets=(("1Y", 5),),
                    ),
                    **self._target_dates(
                        base_scheme_id="later",
                        targets=(("7Y", 1),),
                    ),
                },
                item_policy_by_base=self._item_policy(
                    "alpha",
                    "beta",
                    "later",
                ),
            )
        with self.engine.connect() as conn:
            occurrence = conn.execute(
                text(
                    "SELECT expected_item_count, expected_target_count, "
                    "policy_version, registry_digest "
                    "FROM t_schedule_occurrences WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": first},
            ).mappings().one()
            item_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_schedule_items "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": first},
            ).scalar_one()
            target_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_schedule_item_targets "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": first},
            ).scalar_one()
        self.assertEqual(occurrence["expected_item_count"], 2)
        self.assertEqual(occurrence["expected_target_count"], 3)
        self.assertEqual(occurrence["policy_version"], "daily-ledger-v1")
        self.assertRegex(occurrence["registry_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(item_count, 2)
        self.assertEqual(target_count, 3)

    def test_real_policy_occurrence_freezes_complete_native_matrix(
        self,
    ) -> None:
        policy = self._daily_policy_matrix()
        create_occurrence = self._function("create_schedule_occurrence")

        occurrence_id = create_occurrence(
            self.engine,
            **self._daily_policy_occurrence_args(policy),
        )
        snapshot = self._function(
            "read_schedule_occurrence_snapshot"
        )(
            self.engine,
            occurrence_id=occurrence_id,
        )
        native_policy_ids = {
            scheme_id
            for scheme_id, item in policy.schemes.items()
            if item.runtime_type == "native_adapter"
        }
        native_items = tuple(
            summary
            for summary in snapshot.items
            if summary.item.runtime_type == "native_adapter"
        )
        frozen_scheme_rows = {
            str(row["scheme_id"]): row
            for row in snapshot.occurrence.policy_json["schemes"]
        }
        frozen_native_modes = {
            scheme_id: str(
                frozen_scheme_rows[scheme_id]["input_compatibility"]
            )
            for scheme_id in native_policy_ids
        }

        self.assertEqual(snapshot.actual_item_count, 21)
        self.assertEqual(snapshot.actual_target_count, 25)
        self.assertEqual(len(native_items), 17)
        self.assertEqual(
            sum(summary.target_count for summary in native_items),
            21,
        )
        self.assertEqual(
            {
                summary.item.base_scheme_id
                for summary in native_items
            },
            native_policy_ids,
        )
        self.assertEqual(
            sum(
                mode == "generation_v1"
                for mode in frozen_native_modes.values()
            ),
            14,
        )
        self.assertEqual(
            {
                scheme_id
                for scheme_id, mode in frozen_native_modes.items()
                if mode == "live_source_0629"
            },
            {
                "daily_10y_lgbm_10y04_0629",
                "daily_1y_xgb_1y13_0629",
                "daily_5y_lgbm_5y10_0629",
            },
        )
        for summary in native_items:
            with self.subTest(
                scheme_id=summary.item.base_scheme_id,
            ):
                self.assertEqual(summary.item.state, "PENDING")
                self.assertEqual(summary.item.attempt_no, 0)
                self.assertIsNone(summary.item.current_run_id)

    def test_real_policy_occurrence_replay_is_idempotent(
        self,
    ) -> None:
        policy = self._daily_policy_matrix()
        create_occurrence = self._function("create_schedule_occurrence")
        occurrence_args = self._daily_policy_occurrence_args(policy)

        first = create_occurrence(self.engine, **occurrence_args)
        second = create_occurrence(self.engine, **occurrence_args)

        self.assertEqual(second, first)
        with self.engine.connect() as conn:
            counts = {
                table: int(
                    conn.execute(
                        text(f"SELECT COUNT(*) FROM {table}")
                    ).scalar_one()
                )
                for table in (
                    "t_schedule_occurrences",
                    "t_schedule_items",
                    "t_schedule_item_targets",
                    "t_scheme_runs",
                )
            }
        self.assertEqual(
            counts,
            {
                "t_schedule_occurrences": 1,
                "t_schedule_items": 21,
                "t_schedule_item_targets": 25,
                "t_scheme_runs": 0,
            },
        )

        drifted_policy = {
            **occurrence_args["policy_json"],
            "evidence_note": "step6-drift-must-be-rejected",
        }
        with self.assertRaisesRegex(
            RuntimeError,
            "occurrence immutable snapshot mismatch",
        ):
            create_occurrence(
                self.engine,
                **{
                    **occurrence_args,
                    "policy_json": drifted_policy,
                },
            )
        snapshot = self._function(
            "read_schedule_occurrence_snapshot"
        )(
            self.engine,
            occurrence_id=first,
        )
        self.assertEqual(snapshot.actual_item_count, 21)
        self.assertEqual(snapshot.actual_target_count, 25)

    def test_occurrence_freezes_required_feature_date_and_rejects_drift(
        self,
    ) -> None:
        self._seed_registry(targets=(("5Y", 1),))
        create_occurrence = self._function("create_schedule_occurrence")
        common = {
            "schedule_key": "feature-date-identity",
            "predict_date": "2026-07-24",
            "target_dates": self._target_dates(
                base_scheme_id="alpha",
                targets=(("5Y", 1),),
            ),
            "item_policy_by_base": self._item_policy("alpha"),
        }

        with self.assertRaises(TypeError):
            create_occurrence(self.engine, **common)
        with self.assertRaisesRegex(ValueError, "earlier than predict_date"):
            create_occurrence(
                self.engine,
                feature_date="2026-07-24",
                **common,
            )

        occurrence_id = create_occurrence(
            self.engine,
            feature_date="2026-07-23",
            **common,
        )
        snapshot = self._function("read_schedule_occurrence_snapshot")(
            self.engine,
            occurrence_id=occurrence_id,
        )
        self.assertEqual(snapshot.occurrence.feature_date, "2026-07-23")

        with self.assertRaisesRegex(
            RuntimeError,
            "occurrence immutable snapshot mismatch",
        ):
            create_occurrence(
                self.engine,
                feature_date="2026-07-22",
                **common,
            )

    def test_execution_envelope_reads_only_frozen_ledger_identity(
        self,
    ) -> None:
        from scheduler.repository import ScheduleExecutionEnvelope

        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM t_scheme_registry"))
            conn.execute(text("DELETE FROM t_scheme_versions"))
        statements, listener = self._capture_sql()
        try:
            envelope = self._function("read_schedule_execution_envelope")(
                self.engine,
                item_id=item_id,
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )

        self.assertIsInstance(envelope, ScheduleExecutionEnvelope)
        self.assertEqual(envelope.occurrence.occurrence_id, occurrence_id)
        self.assertEqual(envelope.occurrence.predict_date, "2026-07-24")
        self.assertEqual(envelope.item.item_id, item_id)
        self.assertEqual(envelope.item.base_scheme_id, "alpha")
        self.assertEqual(envelope.item.scheme_version, "alpha-v1")
        self.assertEqual(envelope.item.code_sha256, "a" * 64)
        self.assertEqual(envelope.item.config_sha256, "b" * 64)
        self.assertEqual(envelope.item.runtime_type, "native_adapter")
        self.assertEqual(envelope.item.cache_group, "alpha-cache")
        self.assertEqual(envelope.item.resource_class, "cpu_standard")
        self.assertEqual(envelope.item.internal_workers, 2)
        self.assertEqual(envelope.item.release_offset_minutes, 0)
        self.assertEqual(
            envelope.item.recovery_cutoff_at,
            datetime(2026, 7, 24, 0, 30),
        )
        self.assertEqual(
            envelope.generation.generation_id,
            "generation-20260724",
        )
        self.assertEqual(
            envelope.generation.manifest_uri,
            "/manifests/generation-20260724.json",
        )
        self.assertEqual(envelope.generation.manifest_sha256, "e" * 64)
        self.assertEqual(envelope.generation.feature_date, "2026-07-23")
        self.assertEqual(envelope.generation.business_date, "2026-07-24")
        self.assertEqual(
            [
                (target.registry_scheme_id, target.target_date)
                for target in envelope.targets
            ],
            [
                ("alpha__h1__10Y", "2026-07-25"),
                ("alpha__h1__5Y", "2026-07-25"),
            ],
        )
        read_sql = " ".join(statements)
        self.assertNotIn("t_scheme_registry", read_sql)
        self.assertNotIn("t_scheme_versions", read_sql)

    def test_occurrence_snapshot_lists_items_and_frozen_aggregate(
        self,
    ) -> None:
        from scheduler.repository import ScheduleOccurrenceSnapshot

        occurrence_id, item_ids = self._create_two_item_occurrence()
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM t_scheme_registry"))
            conn.execute(text("DELETE FROM t_scheme_versions"))
        statements, listener = self._capture_sql()
        try:
            snapshot = self._function("read_schedule_occurrence_snapshot")(
                self.engine,
                occurrence_id=occurrence_id,
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )

        self.assertIsInstance(snapshot, ScheduleOccurrenceSnapshot)
        self.assertEqual(snapshot.occurrence.occurrence_id, occurrence_id)
        self.assertEqual(snapshot.actual_item_count, 2)
        self.assertEqual(snapshot.actual_target_count, 2)
        self.assertEqual(snapshot.actual_accepted_target_count, 0)
        self.assertEqual(dict(snapshot.item_state_counts), {"PENDING": 2})
        self.assertEqual(
            [
                (
                    summary.item.item_id,
                    summary.item.base_scheme_id,
                    summary.target_count,
                    summary.accepted_target_count,
                )
                for summary in snapshot.items
            ],
            [
                (item_ids["alpha"], "alpha", 1, 0),
                (item_ids["beta"], "beta", 1, 0),
            ],
        )
        read_sql = " ".join(statements)
        self.assertNotIn("t_scheme_registry", read_sql)
        self.assertNotIn("t_scheme_versions", read_sql)

    def test_v2_execution_envelope_includes_bound_native_calendar_generation(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        native_generation_id = self._create_native_calendar_generation()
        databridge = self._generation_params("databridge-20260724")
        databridge["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **databridge,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        self._function("seal_input_generation")(
            self.engine,
            generation_id="databridge-20260724",
            sealed_at=datetime(2026, 7, 23, 23, 10),
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="v2-calendar-envelope",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=self._item_policy("v2"),
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )
        self._function("bind_schedule_item_input_generation")(
            self.engine,
            item_id=item_id,
            generation_id="databridge-20260724",
            expected_feature_date="2026-07-23",
        )

        envelope = self._function("read_schedule_execution_envelope")(
            self.engine,
            item_id=item_id,
        )

        self.assertEqual(
            envelope.generation.generation_id,
            "databridge-20260724",
        )
        self.assertEqual(
            envelope.generation.native_generation_id,
            native_generation_id,
        )
        self.assertEqual(
            envelope.calendar_generation.generation_id,
            native_generation_id,
        )
        self.assertEqual(
            envelope.calendar_generation.manifest_sha256,
            "f" * 64,
        )
        self.assertEqual(
            envelope.calendar_generation.manifest_uri,
            f"/manifests/{native_generation_id}.json",
        )

    def test_databridge_generation_requires_sealed_native_relation(
        self,
    ) -> None:
        databridge = self._generation_params("databridge-orphan")
        databridge["generation_type"] = "databridge_v1"

        with self.assertRaisesRegex(ValueError, "native_generation_id"):
            self._function("create_input_generation")(
                self.engine,
                **databridge,
            )
        with self.assertRaisesRegex(RuntimeError, "native generation"):
            self._function("create_input_generation")(
                self.engine,
                **databridge,
                native_generation_id="native-missing",
                native_manifest_sha256="f" * 64,
            )

    def test_unfinished_same_day_generation_blocks_replacement(self) -> None:
        self._create_generation(generation_id="native-building-first")
        replacement = self._generation_params("native-building-second")

        with self.assertRaisesRegex(
            RuntimeError,
            "unfinished input generation",
        ):
            self._function("create_input_generation")(
                self.engine,
                **replacement,
            )

        with self.engine.connect() as conn:
            generations = tuple(
                conn.execute(
                    text(
                        "SELECT generation_id, state "
                        "FROM t_input_generations "
                        "WHERE business_date = '2026-07-24' "
                        "AND generation_type = 'native_source' "
                        "ORDER BY generation_id"
                    )
                ).all()
            )
        self.assertEqual(
            generations,
            (("native-building-first", "BUILDING"),),
        )

    def test_scheduler_heartbeat_upsert_and_read_are_service_scoped(
        self,
    ) -> None:
        upsert = self._function("upsert_scheduler_heartbeat")
        read = self._function("read_scheduler_heartbeat")
        occurrence_id, _item_id = self._create_occurrence()

        first = upsert(
            self.engine,
            service_name="daily-scheduler",
            process_id=123,
            host_name="mac-studio",
            state="STARTING",
            occurrence_id=None,
            details={"phase": "startup"},
            _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
        )
        second = upsert(
            self.engine,
            service_name="daily-scheduler",
            process_id=456,
            host_name="mac-studio",
            state="RUNNING",
            occurrence_id=occurrence_id,
            details={"pending_items": 2},
            _clock=self._clock(datetime(2026, 7, 24, 0, 1)),
        )

        self.assertEqual(first.heartbeat_at, datetime(2026, 7, 24, 0, 0))
        self.assertEqual(second.process_id, 456)
        self.assertEqual(second.state, "RUNNING")
        self.assertEqual(second.occurrence_id, occurrence_id)
        self.assertEqual(second.details, {"pending_items": 2})
        self.assertEqual(read(self.engine, service_name="daily-scheduler"), second)
        self.assertIsNone(read(self.engine, service_name="unknown"))
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text("SELECT COUNT(*) FROM t_scheduler_heartbeat")
                ).scalar_one(),
                1,
            )

    def test_item_input_generation_binding_is_idempotent_but_cannot_switch(
        self,
    ) -> None:
        self._seed_registry(base_scheme_id="alpha", targets=(("5Y", 1),))
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="alpha",
                targets=(("5Y", 1),),
            ),
            item_policy_by_base=self._item_policy("alpha"),
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )
        self._create_generation(generation_id="generation-a")
        self._function("seal_input_generation")(
            self.engine,
            generation_id="generation-a",
        )
        self._create_generation(generation_id="generation-b")
        self._function("seal_input_generation")(
            self.engine,
            generation_id="generation-b",
        )
        bind = self._function("bind_schedule_item_input_generation")

        first = bind(
            self.engine,
            item_id=item_id,
            generation_id="generation-a",
            expected_feature_date="2026-07-23",
        )
        second = bind(
            self.engine,
            item_id=item_id,
            generation_id="generation-a",
            expected_feature_date="2026-07-23",
        )
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            bind(
                self.engine,
                item_id=item_id,
                generation_id="generation-b",
                expected_feature_date="2026-07-23",
            )

        self.assertEqual(first, "generation-a")
        self.assertEqual(second, "generation-a")
        with self.engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT input_generation_id FROM t_schedule_items "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).scalar_one()
        self.assertEqual(stored, "generation-a")

    def test_bind_requires_expected_previous_trading_feature_date(self) -> None:
        _, item_id = self._create_occurrence(generation_status="BUILDING")
        self._function("seal_input_generation")(
            self.engine,
            generation_id="generation-20260724",
        )
        bind = self._function("bind_schedule_item_input_generation")

        with self.assertRaisesRegex(RuntimeError, "feature_date"):
            bind(
                self.engine,
                item_id=item_id,
                generation_id="generation-20260724",
                expected_feature_date="2026-07-22",
            )
        bound = bind(
            self.engine,
            item_id=item_id,
            generation_id="generation-20260724",
            expected_feature_date="2026-07-23",
        )

        self.assertEqual(bound, "generation-20260724")

    def test_bind_trusts_frozen_occurrence_feature_date_not_caller(
        self,
    ) -> None:
        self._seed_registry(base_scheme_id="alpha", targets=(("5Y", 1),))
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="frozen-feature-bind",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="alpha",
                targets=(("5Y", 1),),
            ),
            item_policy_by_base=self._item_policy("alpha"),
        )
        params = self._generation_params("wrong-feature")
        params["feature_date"] = "2026-07-22"
        self._function("create_input_generation")(self.engine, **params)
        self._function("seal_input_generation")(
            self.engine,
            generation_id="wrong-feature",
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "occurrence feature_date",
        ):
            self._function("bind_schedule_item_input_generation")(
                self.engine,
                item_id=item_id,
                generation_id="wrong-feature",
                expected_feature_date="2026-07-22",
            )

    def test_bind_locks_occurrence_then_ordered_siblings_then_generation(
        self,
    ) -> None:
        _, item_id = self._create_occurrence(generation_status="BUILDING")
        self._function("seal_input_generation")(
            self.engine,
            generation_id="generation-20260724",
        )
        statements, listener = self._capture_sql()
        try:
            self._function("bind_schedule_item_input_generation")(
                self.engine,
                item_id=item_id,
                generation_id="generation-20260724",
                expected_feature_date="2026-07-23",
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )

        occurrence_index = self._sql_index(
            statements,
            "from t_schedule_occurrences",
            "where occurrence_id",
        )
        siblings_index = self._sql_index(
            statements,
            "from t_schedule_items",
            "where occurrence_id",
            "order by item_id",
        )
        generation_index = self._sql_index(
            statements,
            "from t_input_generations",
            "where generation_id",
        )
        self.assertLess(occurrence_index, siblings_index)
        self.assertLess(siblings_index, generation_index)

    def test_mysql_canonical_lock_helpers_emit_for_update(self) -> None:
        from scheduler import repository

        conn = _MysqlSqlCaptureConnection()
        repository._read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=1,
            for_update=True,
        )
        repository._read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=1,
            for_update=True,
        )
        repository._read_input_generation_conn(
            conn,
            "generation-1",
            for_update=True,
        )
        repository._read_schedule_run_conn(
            conn,
            run_id=1,
            for_update=True,
        )
        repository._read_schedule_targets_conn(
            conn,
            item_id=1,
            for_update=True,
        )

        self.assertEqual(len(conn.statements), 5)
        self.assertTrue(
            all(statement.endswith("for update") for statement in conn.statements),
            conn.statements,
        )

    def test_same_occurrence_runtime_must_share_one_generation(self) -> None:
        _, item_ids = self._create_two_item_occurrence(
            bind_generation=False,
        )
        params = self._generation_params("generation-other")
        self._function("create_input_generation")(self.engine, **params)
        self._function("seal_input_generation")(
            self.engine,
            generation_id="generation-other",
        )
        bind = self._function("bind_schedule_item_input_generation")
        bind(
            self.engine,
            item_id=item_ids["alpha"],
            generation_id="generation-20260724",
            expected_feature_date="2026-07-23",
        )

        with self.assertRaisesRegex(RuntimeError, "runtime generation"):
            bind(
                self.engine,
                item_id=item_ids["beta"],
                generation_id="generation-other",
                expected_feature_date="2026-07-23",
            )

    def test_atomic_generation_registration_uses_trusted_clock_and_is_idempotent(
        self,
    ) -> None:
        for base_scheme_id, tenor in (("alpha", "5Y"), ("beta", "10Y")):
            self._seed_registry(
                base_scheme_id=base_scheme_id,
                targets=((tenor, 1),),
            )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="atomic-registration",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates={
                **self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1),),
                ),
                **self._target_dates(
                    base_scheme_id="beta",
                    targets=(("10Y", 1),),
                ),
            },
            item_policy_by_base=self._item_policy("alpha", "beta"),
        )
        register = self._function(
            "register_seal_and_bind_schedule_occurrence_generation"
        )
        sealed_at = datetime(2026, 7, 23, 23, 7, 8, 123456)

        first = register(
            self.engine,
            occurrence_id=occurrence_id,
            expected_feature_date="2026-07-23",
            _clock=self._clock(sealed_at),
            **self._generation_params("atomic-native"),
        )
        replay = register(
            self.engine,
            occurrence_id=occurrence_id,
            expected_feature_date="2026-07-23",
            _clock=self._clock(datetime(2026, 7, 23, 23, 9)),
            **self._generation_params("atomic-native"),
        )

        self.assertEqual(first, ("atomic-native", 2))
        self.assertEqual(replay, first)
        with self.engine.connect() as conn:
            generation = conn.execute(
                text(
                    "SELECT state, sealed_at FROM t_input_generations "
                    "WHERE generation_id = 'atomic-native'"
                )
            ).mappings().one()
            item_generations = tuple(
                conn.execute(
                    text(
                        "SELECT input_generation_id "
                        "FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id "
                        "ORDER BY item_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalars()
            )
        self.assertEqual(generation["state"], "SEALED")
        self.assertEqual(
            str(generation["sealed_at"]),
            "2026-07-23 23:07:08.123456",
        )
        self.assertEqual(
            item_generations,
            ("atomic-native", "atomic-native"),
        )

    def test_atomic_registration_recovers_exact_building_but_rejects_drift(
        self,
    ) -> None:
        self._seed_registry(base_scheme_id="alpha", targets=(("5Y", 1),))
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="building-recovery",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="alpha",
                targets=(("5Y", 1),),
            ),
            item_policy_by_base=self._item_policy("alpha"),
        )
        exact = self._generation_params("recover-building")
        self._function("create_input_generation")(self.engine, **exact)

        recovered = self._function(
            "register_seal_and_bind_schedule_occurrence_generation"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            expected_feature_date="2026-07-23",
            _clock=self._clock(datetime(2026, 7, 23, 23, 8)),
            **exact,
        )
        self.assertEqual(recovered, ("recover-building", 1))

        drifted = dict(exact)
        drifted["manifest_sha256"] = "9" * 64
        with self.assertRaisesRegex(
            RuntimeError,
            "immutable provenance mismatch",
        ):
            self._function(
                "register_seal_and_bind_schedule_occurrence_generation"
            )(
                self.engine,
                occurrence_id=occurrence_id,
                expected_feature_date="2026-07-23",
                _clock=self._clock(datetime(2026, 7, 23, 23, 9)),
                **drifted,
            )
        with self.engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT manifest_sha256, state "
                    "FROM t_input_generations "
                    "WHERE generation_id = 'recover-building'"
                )
            ).mappings().one()
        self.assertEqual(stored["manifest_sha256"], "e" * 64)
        self.assertEqual(stored["state"], "SEALED")

    def test_databridge_atomic_registration_requires_parent_bound_to_native_items(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="native",
            runtime_type="native_adapter",
            targets=(("5Y", 1),),
        )
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("10Y", 1),),
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="databridge-parent-binding",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates={
                **self._target_dates(
                    base_scheme_id="native",
                    targets=(("5Y", 1),),
                ),
                **self._target_dates(
                    base_scheme_id="v2",
                    targets=(("10Y", 1),),
                ),
            },
            item_policy_by_base=self._item_policy("native", "v2"),
        )
        native = self._generation_params("parent-native")
        native["manifest_sha256"] = "f" * 64
        self._function("create_input_generation")(self.engine, **native)
        self._function("seal_input_generation")(
            self.engine,
            generation_id="parent-native",
        )
        databridge = self._generation_params("child-databridge")
        databridge["generation_type"] = "databridge_v1"
        register = self._function(
            "register_seal_and_bind_schedule_occurrence_generation"
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Native runtime items.*parent",
        ):
            register(
                self.engine,
                occurrence_id=occurrence_id,
                expected_feature_date="2026-07-23",
                native_generation_id="parent-native",
                native_manifest_sha256="f" * 64,
                _clock=self._clock(datetime(2026, 7, 23, 23, 10)),
                **databridge,
            )
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM t_input_generations "
                        "WHERE generation_id = 'child-databridge'"
                    )
                ).scalar_one(),
                0,
            )
            native_item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id "
                        "AND runtime_type = 'native_adapter'"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )
        self._function("bind_schedule_item_input_generation")(
            self.engine,
            item_id=native_item_id,
            generation_id="parent-native",
            expected_feature_date="2026-07-23",
        )

        statements, listener = self._capture_sql()
        try:
            registered = register(
                self.engine,
                occurrence_id=occurrence_id,
                expected_feature_date="2026-07-23",
                native_generation_id="parent-native",
                native_manifest_sha256="f" * 64,
                _clock=self._clock(datetime(2026, 7, 23, 23, 11)),
                **databridge,
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )
        self.assertEqual(registered, ("child-databridge", 1))
        occurrence_lock = self._sql_index(
            statements,
            "from t_schedule_occurrences",
            "where occurrence_id",
        )
        sibling_lock = self._sql_index_after(
            statements,
            occurrence_lock,
            "from t_schedule_items",
            "order by item_id",
        )
        parent_lock = self._sql_index_after(
            statements,
            sibling_lock,
            "from t_input_generations",
            "where generation_id",
        )
        same_day_scope_lock = self._sql_index_after(
            statements,
            parent_lock,
            "from t_input_generations",
            "where business_date",
        )
        child_insert = self._sql_index_after(
            statements,
            same_day_scope_lock,
            "into t_input_generations",
        )
        self.assertLess(
            occurrence_lock,
            sibling_lock,
        )
        self.assertLess(sibling_lock, parent_lock)
        self.assertLess(parent_lock, same_day_scope_lock)
        self.assertLess(same_day_scope_lock, child_insert)

    def test_atomic_registration_rolls_back_insert_when_release_is_too_late(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="native",
            runtime_type="native_adapter",
            targets=(("5Y", 1),),
        )
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("10Y", 1),),
        )
        policy = self._item_policy("native", "v2")
        policy["v2"]["release_offset_minutes"] = 2
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="atomic-registration-rollback",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates={
                **self._target_dates(
                    base_scheme_id="native",
                    targets=(("5Y", 1),),
                ),
                **self._target_dates(
                    base_scheme_id="v2",
                    targets=(("10Y", 1),),
                ),
            },
            item_policy_by_base=policy,
        )
        register = self._function(
            "register_seal_and_bind_schedule_occurrence_generation"
        )
        parent = self._generation_params("rollback-parent")
        parent["manifest_sha256"] = "f" * 64
        register(
            self.engine,
            occurrence_id=occurrence_id,
            expected_feature_date="2026-07-23",
            _clock=self._clock(datetime(2026, 7, 23, 23, 5)),
            **parent,
        )
        child = self._generation_params("rollback-child")
        child["generation_type"] = "databridge_v1"

        with self.assertRaisesRegex(RuntimeError, "recovery_cutoff_at"):
            register(
                self.engine,
                occurrence_id=occurrence_id,
                expected_feature_date="2026-07-23",
                native_generation_id="rollback-parent",
                native_manifest_sha256="f" * 64,
                _clock=self._clock(datetime(2026, 7, 24, 0, 29)),
                **child,
            )

        with self.engine.connect() as conn:
            child_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_input_generations "
                    "WHERE generation_id = 'rollback-child'"
                )
            ).scalar_one()
            v2_binding = conn.execute(
                text(
                    "SELECT input_generation_id FROM t_schedule_items "
                    "WHERE occurrence_id = :occurrence_id "
                    "AND runtime_type = 'blackbox_v2'"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        self.assertEqual(child_count, 0)
        self.assertIsNone(v2_binding)

    def test_seal_and_bind_generation_covers_all_runtime_items_atomically(
        self,
    ) -> None:
        for base_scheme_id, tenor in (("alpha", "5Y"), ("beta", "10Y")):
            self._seed_registry(
                base_scheme_id=base_scheme_id,
                targets=((tenor, 1),),
            )
        self._create_generation()
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-atomic-generation",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates={
                **self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1),),
                ),
                **self._target_dates(
                    base_scheme_id="beta",
                    targets=(("10Y", 1),),
                ),
            },
            item_policy_by_base=self._item_policy("alpha", "beta"),
        )

        bound = self._function(
            "seal_and_bind_schedule_occurrence_generation"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            generation_id="generation-20260724",
            expected_feature_date="2026-07-23",
            sealed_at=datetime(2026, 7, 23, 23, 5),
        )

        self.assertEqual(bound, ("generation-20260724", 2))
        with self.engine.connect() as conn:
            generation_state = conn.execute(
                text(
                    "SELECT state FROM t_input_generations "
                    "WHERE generation_id = 'generation-20260724'"
                )
            ).scalar_one()
            item_generations = tuple(
                conn.execute(
                    text(
                        "SELECT input_generation_id "
                        "FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id "
                        "ORDER BY item_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalars()
            )
        self.assertEqual(generation_state, "SEALED")
        self.assertEqual(
            item_generations,
            ("generation-20260724", "generation-20260724"),
        )

    def test_generation_seal_rolls_back_when_any_v2_release_is_too_late(
        self,
    ) -> None:
        for base_scheme_id, tenor in (("v2a", "5Y"), ("v2b", "10Y")):
            self._seed_registry(
                base_scheme_id=base_scheme_id,
                runtime_type="blackbox_v2",
                targets=((tenor, 1),),
            )
        native_generation_id = self._create_native_calendar_generation()
        generation = self._generation_params("databridge-atomic")
        generation["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **generation,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        policies = self._item_policy("v2a", "v2b")
        policies["v2a"]["release_offset_minutes"] = 0
        policies["v2b"]["release_offset_minutes"] = 2
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="v2-atomic-generation",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates={
                **self._target_dates(
                    base_scheme_id="v2a",
                    targets=(("5Y", 1),),
                ),
                **self._target_dates(
                    base_scheme_id="v2b",
                    targets=(("10Y", 1),),
                ),
            },
            item_policy_by_base=policies,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "recovery_cutoff_at",
        ):
            self._function(
                "seal_and_bind_schedule_occurrence_generation"
            )(
                self.engine,
                occurrence_id=occurrence_id,
                generation_id="databridge-atomic",
                expected_feature_date="2026-07-23",
                sealed_at=datetime(2026, 7, 24, 0, 29),
            )

        with self.engine.connect() as conn:
            generation_state = conn.execute(
                text(
                    "SELECT state FROM t_input_generations "
                    "WHERE generation_id = 'databridge-atomic'"
                )
            ).scalar_one()
            bound_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_schedule_items "
                    "WHERE occurrence_id = :occurrence_id "
                    "AND input_generation_id IS NOT NULL"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        self.assertEqual(generation_state, "BUILDING")
        self.assertEqual(bound_count, 0)

    def test_v2_bind_finalizes_release_from_seal_plus_frozen_offset(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        native_generation_id = self._create_native_calendar_generation()
        generation = self._generation_params("databridge-20260724")
        generation["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **generation,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        self._function("seal_input_generation")(
            self.engine,
            generation_id="databridge-20260724",
            sealed_at=datetime(2026, 7, 23, 23, 10),
        )
        policy = self._item_policy("v2")
        policy["v2"]["release_offset_minutes"] = 4
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="v2-dynamic-release",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=policy,
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )

        self._function("bind_schedule_item_input_generation")(
            self.engine,
            item_id=item_id,
            generation_id="databridge-20260724",
            expected_feature_date="2026-07-23",
        )

        with self.engine.connect() as conn:
            frozen = conn.execute(
                text(
                    "SELECT release_offset_minutes, release_at "
                    "FROM t_schedule_items WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).mappings().one()
        self.assertEqual(frozen["release_offset_minutes"], 4)
        self.assertEqual(str(frozen["release_at"]), "2026-07-23 23:14:00")

        later = self._generation_params("databridge-later")
        later["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **later,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        self._function("seal_input_generation")(
            self.engine,
            generation_id="databridge-later",
            sealed_at=datetime(2026, 7, 23, 23, 20),
        )
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            self._function("bind_schedule_item_input_generation")(
                self.engine,
                item_id=item_id,
                generation_id="databridge-later",
                expected_feature_date="2026-07-23",
            )
        with self.assertRaisesRegex(RuntimeError, "release_at"):
            self._function("start_schedule_attempt")(
                self.engine,
                item_id=item_id,
                started_at=datetime(2026, 7, 23, 23, 13, 59),
                _clock=self._clock(datetime(2026, 7, 23, 23, 13, 59)),
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 14),
            _clock=self._clock(datetime(2026, 7, 23, 23, 14)),
        )
        self.assertEqual(attempt.attempt_no, 1)

    def test_v2_late_generation_binds_until_occurrence_recovery_cutoff(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        native_generation_id = self._create_native_calendar_generation()
        generation = self._generation_params("databridge-late")
        generation["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **generation,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        self._function("seal_input_generation")(
            self.engine,
            generation_id="databridge-late",
            sealed_at=datetime(2026, 7, 23, 23, 11),
        )
        policy = self._item_policy("v2")
        policy["v2"]["release_offset_minutes"] = 6
        policy["v2"]["deadline_at"] = datetime(2026, 7, 23, 23, 10)
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="v2-late-release",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=policy,
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )

        bound = self._function("bind_schedule_item_input_generation")(
            self.engine,
            item_id=item_id,
            generation_id="databridge-late",
            expected_feature_date="2026-07-23",
        )

        self.assertEqual(bound, "databridge-late")
        with self.engine.connect() as conn:
            release_at = conn.execute(
                text(
                    "SELECT release_at FROM t_schedule_items "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).scalar_one()
        self.assertEqual(str(release_at), "2026-07-23 23:17:00")

    def test_occurrence_requires_explicit_resource_and_version_policy(
        self,
    ) -> None:
        self._seed_registry(targets=(("5Y", 1),))
        create_occurrence = self._function("create_schedule_occurrence")
        target_dates = self._target_dates(
            base_scheme_id="alpha",
            targets=(("5Y", 1),),
        )

        with self.assertRaises(TypeError):
            create_occurrence(
                self.engine,
                schedule_key="missing-policy",
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_dates=target_dates,
            )
        wrong = self._item_policy("alpha")
        wrong["alpha"]["scheme_version"] = "unobserved-v9"
        with self.assertRaisesRegex(ValueError, "scheme_version"):
            create_occurrence(
                self.engine,
                schedule_key="wrong-version",
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_dates=target_dates,
                item_policy_by_base=wrong,
            )

    def test_occurrence_freezes_exact_policy_selected_active_version(
        self,
    ) -> None:
        self._seed_registry(targets=(("5Y", 1),))
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_versions
                        (scheme_id, scheme_version, runtime_type, code_hash,
                         config_hash, status)
                    VALUES
                        ('alpha', 'alpha-v2', 'native_adapter', :code_hash,
                         :config_hash, 'active')
                    """
                ),
                {
                    "code_hash": "c" * 64,
                    "config_hash": "d" * 64,
                },
            )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="exact-policy-version",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="alpha",
                targets=(("5Y", 1),),
            ),
            item_policy_by_base=self._item_policy("alpha"),
        )

        with self.engine.connect() as conn:
            frozen = conn.execute(
                text(
                    "SELECT scheme_version, code_sha256, config_sha256 "
                    "FROM t_schedule_items "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
        self.assertEqual(frozen["scheme_version"], "alpha-v1")
        self.assertEqual(frozen["code_sha256"], "a" * 64)
        self.assertEqual(frozen["config_sha256"], "b" * 64)

    def test_occurrence_rejects_paused_selected_version(self) -> None:
        self._seed_registry(targets=(("5Y", 1),))
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_versions SET status = 'paused' "
                    "WHERE scheme_id = 'alpha' "
                    "AND scheme_version = 'alpha-v1'"
                )
            )

        with self.assertRaisesRegex(ValueError, "active version not found"):
            self._function("create_schedule_occurrence")(
                self.engine,
                schedule_key="paused-policy-version",
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_dates=self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1),),
                ),
                item_policy_by_base=self._item_policy("alpha"),
            )

    def test_occurrence_rejects_registry_version_runtime_mismatch(
        self,
    ) -> None:
        self._seed_registry(targets=(("5Y", 1),))
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_versions "
                    "SET runtime_type = 'blackbox_v2' "
                    "WHERE scheme_id = 'alpha' "
                    "AND scheme_version = 'alpha-v1'"
                )
            )

        with self.assertRaisesRegex(ValueError, "runtime_type mismatch"):
            self._function("create_schedule_occurrence")(
                self.engine,
                schedule_key="runtime-mismatch",
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_dates=self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1),),
                ),
                item_policy_by_base=self._item_policy("alpha"),
            )

    def test_occurrence_policy_timestamps_are_normalized_to_utc(self) -> None:
        self._seed_registry(targets=(("5Y", 1),))
        policy = self._item_policy("alpha")
        policy["alpha"]["release_at"] = "2026-07-24T07:00:00+08:00"
        policy["alpha"]["deadline_at"] = "2026-07-24T08:30:00+08:00"
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="utc-normalization",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="alpha",
                targets=(("5Y", 1),),
            ),
            item_policy_by_base=policy,
        )

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT release_at, deadline_at FROM t_schedule_items "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
        self.assertEqual(str(row["release_at"]), "2026-07-23 23:00:00")
        self.assertEqual(str(row["deadline_at"]), "2026-07-24 00:30:00")

    def test_start_requires_release_time_and_approved_trigger_origin(self) -> None:
        _, item_id = self._create_occurrence()
        start = self._function("start_schedule_attempt")

        with self.assertRaisesRegex(RuntimeError, "release_at"):
            start(
                self.engine,
                item_id=item_id,
                started_at=datetime(2026, 7, 23, 22, 59),
                _clock=self._clock(datetime(2026, 7, 23, 22, 59)),
            )
        with self.assertRaisesRegex(ValueError, "trigger_origin"):
            start(
                self.engine,
                item_id=item_id,
                trigger_origin="scheduled",
                started_at=datetime(2026, 7, 23, 23, 30),
                _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
            )

    def test_same_occurrence_replay_rejects_frozen_policy_drift(self) -> None:
        occurrence_id, _item_id = self._create_occurrence()
        drifted_policy = self._item_policy("alpha")
        drifted_policy["alpha"]["cache_group"] = "drifted-cache"

        with self.assertRaisesRegex(
            RuntimeError,
            "occurrence immutable snapshot mismatch",
        ):
            self._function("create_schedule_occurrence")(
                self.engine,
                schedule_key="daily-production",
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                target_dates=self._target_dates(
                    base_scheme_id="alpha",
                    targets=(("5Y", 1), ("10Y", 1)),
                ),
                item_policy_by_base=drifted_policy,
            )
        with self.engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT occurrence_id, registry_digest "
                    "FROM t_schedule_occurrences"
                )
            ).mappings().one()
        self.assertEqual(int(stored["occurrence_id"]), occurrence_id)

    def test_schedule_time_override_requires_internal_clock_seam(self) -> None:
        _, item_id = self._create_occurrence()

        with self.assertRaisesRegex(ValueError, "internal _clock seam"):
            self._function("start_schedule_attempt")(
                self.engine,
                item_id=item_id,
                started_at=datetime(2026, 7, 23, 23, 30),
            )

    def test_completion_rejects_future_and_before_start_timestamps(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        complete = self._function("complete_scheduled_attempt")
        with self.assertRaisesRegex(RuntimeError, "before run started_at"):
            complete(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
                trusted_verifier=self._trusted_completion_verifier(),
                completed_at=datetime(2026, 7, 23, 23, 29),
                _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
            )
        with self.assertRaisesRegex(RuntimeError, "future"):
            complete(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
                trusted_verifier=self._trusted_completion_verifier(),
                completed_at=datetime(2026, 7, 23, 23, 41),
                _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
            )
        self._assert_no_predictions()

    def test_target_sla_rejects_future_and_before_release_evaluation(
        self,
    ) -> None:
        occurrence_id, _item_id = self._create_occurrence()
        evaluate = self._function("evaluate_schedule_occurrence_target_sla")
        with self.assertRaisesRegex(RuntimeError, "before release_at"):
            evaluate(
                self.engine,
                occurrence_id=occurrence_id,
                evaluated_at=datetime(2026, 7, 23, 22, 59),
                _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
            )
        with self.assertRaisesRegex(RuntimeError, "future"):
            evaluate(
                self.engine,
                occurrence_id=occurrence_id,
                evaluated_at=datetime(2026, 7, 24, 0, 6),
                _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
            )

    def test_start_locks_occurrence_siblings_generation_run_then_targets(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        first = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("abandon_current_schedule_attempt")(
            self.engine,
            item_id=item_id,
            orphan_cleanup_confirmed=True,
            abandoned_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )
        statements, listener = self._capture_sql()
        try:
            self._function("start_schedule_attempt")(
                self.engine,
                item_id=item_id,
                trigger_origin="operator_recovery",
                started_at=datetime(2026, 7, 23, 23, 40),
                _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )

        occurrence_index = self._sql_index(
            statements,
            "from t_schedule_occurrences",
            "where occurrence_id",
        )
        siblings_index = self._sql_index_after(
            statements,
            occurrence_index,
            "from t_schedule_items",
            "where occurrence_id",
            "order by item_id",
        )
        generation_index = self._sql_index_after(
            statements,
            siblings_index,
            "from t_input_generations",
            "where generation_id",
        )
        run_index = self._sql_index_after(
            statements,
            generation_index,
            "select run_id, scheme_id",
            "from t_scheme_runs",
            "where run_id",
        )
        target_index = self._sql_index_after(
            statements,
            run_index,
            "from t_schedule_item_targets",
            "where item_id",
            "order by target_id",
        )
        self.assertLess(occurrence_index, siblings_index)
        self.assertLess(siblings_index, generation_index)
        self.assertLess(generation_index, run_index)
        self.assertLess(run_index, target_index)
        self.assertGreater(first.run_id, 0)

    def test_running_item_requires_confirmed_abandon_before_recovery(self) -> None:
        _, item_id = self._create_occurrence(generation_status="SEALED")
        start_attempt = self._function("start_schedule_attempt")

        first = start_attempt(
            self.engine,
            item_id=item_id,
            trigger_origin="apscheduler",
            execution_token="first-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        with self.assertRaisesRegex(RuntimeError, "state=RUNNING"):
            start_attempt(
                self.engine,
                item_id=item_id,
                trigger_origin="operator_recovery",
                execution_token="forbidden-token",
                started_at=datetime(2026, 7, 24, 0, 0),
                _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
            )
        abandon = self._function("abandon_current_schedule_attempt")
        with self.assertRaisesRegex(RuntimeError, "orphan cleanup confirmation"):
            abandon(self.engine, item_id=item_id)
        abandon(
            self.engine,
            item_id=item_id,
            orphan_cleanup_confirmed=True,
            abandoned_at=datetime(2026, 7, 23, 23, 50),
            _clock=self._clock(datetime(2026, 7, 23, 23, 50)),
        )
        second = start_attempt(
            self.engine,
            item_id=item_id,
            trigger_origin="operator_recovery",
            execution_token="second-token",
            started_at=datetime(2026, 7, 24, 0, 0),
            _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
        )

        self.assertEqual((first.attempt_no, second.attempt_no), (1, 2))
        self.assertNotEqual(first.run_id, second.run_id)
        with self.engine.connect() as conn:
            item = conn.execute(
                text(
                    "SELECT attempt_no, current_run_id, state "
                    "FROM t_schedule_items WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).mappings().one()
            old_run = conn.execute(
                text(
                    "SELECT status, failure_code FROM t_scheme_runs "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": first.run_id},
            ).mappings().one()
        self.assertEqual(item["attempt_no"], 2)
        self.assertEqual(item["current_run_id"], second.run_id)
        self.assertEqual(item["state"], "RUNNING")
        self.assertEqual(dict(old_run), {
            "status": "failed",
            "failure_code": "ABANDONED_ORPHAN_CLEANUP",
        })

    def test_recovery_fences_completion_before_orphan_cleanup(
        self,
    ) -> None:
        _, item_id = self._create_occurrence(generation_status="SEALED")
        start = self._function("start_schedule_attempt")
        first = start(
            self.engine,
            item_id=item_id,
            trigger_origin="apscheduler",
            execution_token="first-token",
            process_id=9101,
            process_group_id=9101,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        fenced = self._function("fence_current_schedule_attempt")(
            self.engine,
            item_id=item_id,
            fenced_at=datetime(2026, 7, 23, 23, 40),
            _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
        )

        self.assertEqual(fenced.run_id, first.run_id)
        self.assertEqual(fenced.execution_token, "first-token")
        self.assertEqual(fenced.process_group_id, 9101)
        with self.assertRaisesRegex(
            RuntimeError,
            "stale scheduled run fence",
        ):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=first.run_id,
                records=self._records(),
                trusted_verifier=self._trusted_completion_verifier(),
                completed_at=datetime(2026, 7, 23, 23, 41),
                _clock=self._clock(datetime(2026, 7, 23, 23, 41)),
            )
        self._assert_no_predictions()
        with self.assertRaisesRegex(
            RuntimeError,
            "orphan cleanup confirmation",
        ):
            start(
                self.engine,
                item_id=item_id,
                trigger_origin="operator_recovery",
                execution_token="too-early",
                started_at=datetime(2026, 7, 23, 23, 42),
                _clock=self._clock(datetime(2026, 7, 23, 23, 42)),
            )

        self._function("confirm_schedule_attempt_orphan_cleanup")(
            self.engine,
            item_id=item_id,
            run_id=first.run_id,
            execution_token="first-token",
        )
        second = start(
            self.engine,
            item_id=item_id,
            trigger_origin="operator_recovery",
            execution_token="second-token",
            started_at=datetime(2026, 7, 23, 23, 45),
            _clock=self._clock(datetime(2026, 7, 23, 23, 45)),
        )

        self.assertEqual(second.attempt_no, 2)

    def test_process_identity_registration_is_fenced_and_idempotent(
        self,
    ) -> None:
        from scheduler.repository import start_schedule_attempt

        _, item_id = self._create_occurrence(generation_status="SEALED")
        attempt = start_schedule_attempt(
            self.engine,
            item_id=item_id,
            execution_token="tracked-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        register = self._function("register_schedule_attempt_process")

        first = register(
            self.engine,
            run_id=attempt.run_id,
            execution_token="tracked-token",
            process_id=9201,
            process_group_id=9201,
            started_at=datetime(2026, 7, 23, 23, 30, 1),
            _clock=self._clock(
                datetime(2026, 7, 23, 23, 30, 1)
            ),
        )
        second = register(
            self.engine,
            run_id=attempt.run_id,
            execution_token="tracked-token",
            process_id=9201,
            process_group_id=9201,
            started_at=datetime(2026, 7, 23, 23, 30, 1),
            _clock=self._clock(
                datetime(2026, 7, 23, 23, 30, 1)
            ),
        )

        self.assertEqual(first, (9201, 9201))
        self.assertEqual(second, first)
        with self.assertRaisesRegex(RuntimeError, "execution token"):
            register(
                self.engine,
                run_id=attempt.run_id,
                execution_token="wrong-token",
                process_id=9201,
                process_group_id=9201,
            )
        with self.assertRaisesRegex(RuntimeError, "process identity"):
            register(
                self.engine,
                run_id=attempt.run_id,
                execution_token="tracked-token",
                process_id=9202,
                process_group_id=9202,
            )

    def test_current_replay_process_query_requires_active_current_run(
        self,
    ) -> None:
        from scheduler.repository import (
            read_current_replay_attempt_processes,
            register_schedule_attempt_process,
            start_schedule_attempt,
        )

        occurrence_id, item_id = self._create_occurrence(
            targets=(("5Y", 1),),
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE t_schedule_occurrences "
                    "SET schedule_key = :schedule_key "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {
                    "schedule_key":
                        "isolated-real-replay-v1-test",
                    "occurrence_id": occurrence_id,
                },
            )
        attempt = start_schedule_attempt(
            self.engine,
            item_id=item_id,
            trigger_origin="operator_recovery",
            execution_token="replay-process-token",
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        register_schedule_attempt_process(
            self.engine,
            run_id=attempt.run_id,
            execution_token=attempt.execution_token,
            process_id=9500,
            process_group_id=9500,
            started_at=datetime(2026, 7, 23, 23, 30, 1),
            _clock=self._clock(
                datetime(2026, 7, 23, 23, 30, 1)
            ),
        )

        rows = read_current_replay_attempt_processes(
            self.engine,
            occurrence_id=occurrence_id,
            active_item_ids=(item_id,),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].item_id, item_id)
        self.assertEqual(rows[0].run_id, attempt.run_id)
        self.assertEqual(
            rows[0].execution_token,
            "replay-process-token",
        )
        self.assertEqual(rows[0].process_id, 9500)
        self.assertEqual(rows[0].process_group_id, 9500)
        self.assertEqual(
            read_current_replay_attempt_processes(
                self.engine,
                occurrence_id=occurrence_id,
                active_item_ids=(),
            ),
            (),
        )
        self.assertEqual(
            read_current_replay_attempt_processes(
                self.engine,
                occurrence_id=occurrence_id,
                active_item_ids=(item_id + 999,),
            ),
            (),
        )
        with self.engine.connect() as connection:
            stored_run = dict(
                connection.execute(
                    text(
                        """
                        SELECT scheme_id, scheme_version, runtime_type,
                               run_type, prediction_phase, predict_date,
                               trigger_origin, process_id, process_group_id
                        FROM t_scheme_runs
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": attempt.run_id},
                ).mappings().one()
            )
        unsafe_values = (
            ("scheme_id", "wrong-scheme"),
            ("scheme_version", "wrong-version"),
            ("runtime_type", "blackbox_v2"),
            ("run_type", "manual"),
            ("prediction_phase", "gray_live"),
            ("predict_date", "2026-07-22"),
            ("trigger_origin", "apscheduler"),
            ("process_id", 1),
            ("process_id", 9501),
            ("process_group_id", 1),
        )
        for field, unsafe_value in unsafe_values:
            with self.subTest(field=field, unsafe_value=unsafe_value):
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            f"""
                            UPDATE t_scheme_runs
                            SET {field} = :unsafe_value
                            WHERE run_id = :run_id
                            """
                        ),
                        {
                            "unsafe_value": unsafe_value,
                            "run_id": attempt.run_id,
                        },
                    )
                try:
                    self.assertEqual(
                        read_current_replay_attempt_processes(
                            self.engine,
                            occurrence_id=occurrence_id,
                            active_item_ids=(item_id,),
                        ),
                        (),
                    )
                finally:
                    with self.engine.begin() as connection:
                        connection.execute(
                            text(
                                f"""
                                UPDATE t_scheme_runs
                                SET {field} = :stored_value
                                WHERE run_id = :run_id
                                """
                            ),
                            {
                                "stored_value": stored_run[field],
                                "run_id": attempt.run_id,
                            },
                        )

        self._function("fence_current_schedule_attempt")(
            self.engine,
            item_id=item_id,
            fenced_at=datetime(2026, 7, 23, 23, 31),
            _clock=self._clock(datetime(2026, 7, 23, 23, 31)),
        )
        self.assertEqual(
            read_current_replay_attempt_processes(
                self.engine,
                occurrence_id=occurrence_id,
                active_item_ids=(item_id,),
            ),
            (),
        )

    def test_attempt_claim_does_not_fabricate_process_start_or_v2_guardrail(
        self,
    ) -> None:
        from scheduler.repository import start_schedule_attempt

        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        native_generation_id = self._create_native_calendar_generation()
        databridge = self._generation_params("databridge-process-start")
        databridge["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **databridge,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        self._function("seal_input_generation")(
            self.engine,
            generation_id="databridge-process-start",
            sealed_at=datetime(2026, 7, 23, 23, 0),
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=self._item_policy("v2"),
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )
        self._function("bind_schedule_item_input_generation")(
            self.engine,
            item_id=item_id,
            generation_id="databridge-process-start",
            expected_feature_date="2026-07-23",
        )
        attempt = start_schedule_attempt(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 44, 59),
            _clock=self._clock(datetime(2026, 7, 23, 23, 44, 59)),
        )
        with self.engine.connect() as conn:
            before = conn.execute(
                text(
                    "SELECT r.started_at AS run_started_at, "
                    "i.started_at AS item_started_at, i.sla_status "
                    "FROM t_scheme_runs r JOIN t_schedule_items i "
                    "ON i.item_id = r.schedule_item_id "
                    "WHERE r.run_id = :run_id"
                ),
                {"run_id": attempt.run_id},
            ).mappings().one()
        self.assertIsNone(before["run_started_at"])
        self.assertIsNone(before["item_started_at"])
        self.assertEqual(before["sla_status"], "PENDING")

        self._function("register_schedule_attempt_process")(
            self.engine,
            run_id=attempt.run_id,
            execution_token=attempt.execution_token,
            process_id=9301,
            process_group_id=9301,
            started_at=datetime(2026, 7, 23, 23, 45, 1),
            _clock=self._clock(datetime(2026, 7, 23, 23, 45, 1)),
        )
        with self.engine.connect() as conn:
            after = conn.execute(
                text(
                    "SELECT r.started_at AS run_started_at, "
                    "i.started_at AS item_started_at, i.sla_status "
                    "FROM t_scheme_runs r JOIN t_schedule_items i "
                    "ON i.item_id = r.schedule_item_id "
                    "WHERE r.run_id = :run_id"
                ),
                {"run_id": attempt.run_id},
            ).mappings().one()
        self.assertEqual(
            str(after["run_started_at"]),
            "2026-07-23 23:45:01",
        )
        self.assertEqual(
            str(after["item_started_at"]),
            "2026-07-23 23:45:01",
        )
        self.assertEqual(after["sla_status"], "LATE")

    def test_pre_popen_terminal_failure_keeps_started_at_null(self) -> None:
        from scheduler.repository import start_schedule_attempt

        _, item_id = self._create_occurrence()
        attempt = start_schedule_attempt(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("mark_schedule_attempt_terminal_failure")(
            self.engine,
            run_id=attempt.run_id,
            failure_code="CONTRACT",
            failed_at=datetime(2026, 7, 23, 23, 31),
            _clock=self._clock(datetime(2026, 7, 23, 23, 31)),
        )
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT r.started_at AS run_started_at, "
                    "i.started_at AS item_started_at, r.status, i.state "
                    "FROM t_scheme_runs r JOIN t_schedule_items i "
                    "ON i.item_id = r.schedule_item_id "
                    "WHERE r.run_id = :run_id"
                ),
                {"run_id": attempt.run_id},
            ).mappings().one()
        self.assertIsNone(row["run_started_at"])
        self.assertIsNone(row["item_started_at"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["state"], "FAILED_TERMINAL")

    def test_startup_catchup_is_legal_only_for_abandoned_attempt_two(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        start = self._function("start_schedule_attempt")
        first = start(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("abandon_current_schedule_attempt")(
            self.engine,
            item_id=item_id,
            orphan_cleanup_confirmed=True,
            abandoned_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )

        second = start(
            self.engine,
            item_id=item_id,
            trigger_origin="startup_catchup",
            started_at=datetime(2026, 7, 23, 23, 40),
            _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
        )

        self.assertEqual((first.attempt_no, second.attempt_no), (1, 2))

    def test_startup_catchup_remains_illegal_for_retry_wait_attempt_two(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        start = self._function("start_schedule_attempt")
        first = start(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("mark_schedule_attempt_retry_wait")(
            self.engine,
            run_id=first.run_id,
            failure_code="TRANSIENT_INFRA",
            failed_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )

        with self.assertRaisesRegex(ValueError, "attempt 2"):
            start(
                self.engine,
                item_id=item_id,
                trigger_origin="startup_catchup",
                started_at=datetime(2026, 7, 23, 23, 40),
                _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
            )

    def test_transient_infra_allows_one_retry_then_becomes_terminal(self) -> None:
        _, item_id = self._create_occurrence(generation_status="SEALED")
        start_attempt = self._function("start_schedule_attempt")
        mark_retry = self._function("mark_schedule_attempt_retry_wait")
        first = start_attempt(
            self.engine,
            item_id=item_id,
            execution_token="first-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        first_state = mark_retry(
            self.engine,
            run_id=first.run_id,
            failure_code="TRANSIENT_INFRA",
            error_message="source temporarily unavailable",
            failed_at=datetime(2026, 7, 23, 23, 40),
            _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
        )
        second = start_attempt(
            self.engine,
            item_id=item_id,
            trigger_origin="auto_retry",
            execution_token="second-token",
            started_at=datetime(2026, 7, 24, 0, 0),
            _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
        )
        second_state = mark_retry(
            self.engine,
            run_id=second.run_id,
            failure_code="TRANSIENT_INFRA",
            error_message="source still unavailable",
            failed_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )

        self.assertEqual(first_state, "RETRY_WAIT")
        self.assertEqual(second_state, "FAILED_TERMINAL")
        with self.engine.connect() as conn:
            terminal = conn.execute(
                text(
                    "SELECT i.state, o.completion_state, o.sla_outcome "
                    "FROM t_schedule_items i JOIN t_schedule_occurrences o "
                    "ON o.occurrence_id = i.occurrence_id "
                    "WHERE i.item_id = :item_id"
                ),
                {"item_id": item_id},
            ).mappings().one()
        self.assertEqual(terminal["state"], "FAILED_TERMINAL")
        self.assertEqual(terminal["completion_state"], "FAILED")
        self.assertEqual(terminal["sla_outcome"], "PENDING")

    def test_retry_api_rejects_non_transient_failure_and_terminal_api_closes(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        with self.assertRaisesRegex(ValueError, "TRANSIENT_INFRA"):
            self._function("mark_schedule_attempt_retry_wait")(
                self.engine,
                run_id=attempt.run_id,
                failure_code="DATA",
            )
        state = self._function("mark_schedule_attempt_terminal_failure")(
            self.engine,
            run_id=attempt.run_id,
            failure_code="DATA",
            error_message="input contract failed",
            failed_at=datetime(2026, 7, 23, 23, 40),
            _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
        )
        self.assertEqual(state, "FAILED_TERMINAL")

    def test_terminal_api_rejects_legacy_failure_code_aliases(self) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        with self.assertRaisesRegex(ValueError, "schedule failure_code"):
            self._function("mark_schedule_attempt_terminal_failure")(
                self.engine,
                run_id=attempt.run_id,
                failure_code="DATA_ERROR",
            )

    def test_ledger_bound_run_creation_rejects_unknown_failure_code(self) -> None:
        _, item_id = self._create_occurrence()

        with self.assertRaisesRegex(ValueError, "schedule failure_code"):
            self._function("create_scheme_run")(
                self.engine,
                scheme_id="alpha",
                predict_date="2026-07-24",
                schedule_item_id=item_id,
                attempt_no=1,
                trigger_origin="apscheduler",
                failure_code="DATA_ERROR",
            )

    def test_snapshot_fails_closed_on_legacy_stored_failure_code(self) -> None:
        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_schedule_items "
                    "SET failure_code = 'DATA_ERROR' "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "explicit audit migration required",
        ):
            self._function("read_schedule_occurrence_snapshot")(
                self.engine,
                occurrence_id=occurrence_id,
            )

    def test_nonexecuted_pending_item_failure_is_atomically_audited(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence(
            generation_status="BUILDING"
        )

        state = self._function("fail_schedule_item_without_attempt")(
            self.engine,
            item_id=item_id,
            failure_code="NATIVE_GENERATION_UNSUPPORTED",
            failure_message="native generator is not configured",
        )

        self.assertEqual(state, "FAILED_TERMINAL")
        with self.engine.connect() as conn:
            item = conn.execute(
                text(
                    "SELECT state, attempt_no, current_run_id, failure_code, "
                    "failure_message, completed_at FROM t_schedule_items "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).mappings().one()
            occurrence = conn.execute(
                text(
                    "SELECT completion_state, failure_code, failure_message, "
                    "completed_at, sla_outcome "
                    "FROM t_schedule_occurrences "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
            run_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_runs "
                    "WHERE schedule_item_id = :item_id"
                ),
                {"item_id": item_id},
            ).scalar_one()
        self.assertEqual(item["state"], "FAILED_TERMINAL")
        self.assertEqual(item["attempt_no"], 0)
        self.assertIsNone(item["current_run_id"])
        self.assertEqual(
            item["failure_code"],
            "NATIVE_GENERATION_UNSUPPORTED",
        )
        self.assertEqual(
            occurrence["failure_code"],
            "NATIVE_GENERATION_UNSUPPORTED",
        )
        self.assertEqual(
            occurrence["failure_message"],
            "native generator is not configured",
        )
        self.assertEqual(occurrence["completion_state"], "FAILED")
        self.assertEqual(occurrence["sla_outcome"], "PENDING")
        self.assertIsNotNone(item["completed_at"])
        self.assertIsNotNone(occurrence["completed_at"])
        self.assertEqual(run_count, 0)

    def test_nonexecuted_retry_wait_accepts_generation_hash_mismatch(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("mark_schedule_attempt_retry_wait")(
            self.engine,
            run_id=attempt.run_id,
            failure_code="TRANSIENT_INFRA",
            failed_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )

        state = self._function("fail_schedule_item_without_attempt")(
            self.engine,
            item_id=item_id,
            failure_code="GENERATION_HASH_MISMATCH",
            failure_message="sealed manifest hash changed",
        )

        self.assertEqual(state, "FAILED_TERMINAL")

    def test_nonexecuted_abandoned_accepts_generation_invalidated(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("abandon_current_schedule_attempt")(
            self.engine,
            item_id=item_id,
            orphan_cleanup_confirmed=True,
            abandoned_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )

        state = self._function("fail_schedule_item_without_attempt")(
            self.engine,
            item_id=item_id,
            failure_code="GENERATION_INVALIDATED",
        )

        self.assertEqual(state, "FAILED_TERMINAL")

    def test_nonexecuted_failure_accepts_build_failed_and_rejects_other_codes(
        self,
    ) -> None:
        _, item_id = self._create_occurrence(
            generation_status="BUILDING"
        )
        with self.assertRaisesRegex(ValueError, "non-executed failure_code"):
            self._function("fail_schedule_item_without_attempt")(
                self.engine,
                item_id=item_id,
                failure_code="DATA",
            )

        state = self._function("fail_schedule_item_without_attempt")(
            self.engine,
            item_id=item_id,
            failure_code="GENERATION_BUILD_FAILED",
        )

        self.assertEqual(state, "FAILED_TERMINAL")

    def test_nonexecuted_failure_replay_is_idempotent_and_audit_stable(
        self,
    ) -> None:
        _, item_id = self._create_occurrence(
            generation_status="BUILDING"
        )
        fail = self._function("fail_schedule_item_without_attempt")
        first = fail(
            self.engine,
            item_id=item_id,
            failure_code="GENERATION_BUILD_FAILED",
            failure_message="builder exited",
            _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
        )
        second = fail(
            self.engine,
            item_id=item_id,
            failure_code="GENERATION_BUILD_FAILED",
            failure_message="builder exited",
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )
        with self.engine.connect() as conn:
            completed_at = conn.execute(
                text(
                    "SELECT completed_at FROM t_schedule_items "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).scalar_one()

        self.assertEqual((first, second), ("FAILED_TERMINAL",) * 2)
        self.assertEqual(str(completed_at), "2026-07-24 00:00:00")
        with self.assertRaisesRegex(RuntimeError, "different failure audit"):
            fail(
                self.engine,
                item_id=item_id,
                failure_code="GENERATION_INVALIDATED",
            )
        with self.assertRaisesRegex(ValueError, "failure_message"):
            fail(
                self.engine,
                item_id=item_id,
                failure_code="GENERATION_BUILD_FAILED",
                failure_message="  ",
            )

    def test_terminal_occurrence_cannot_flip_back_when_sibling_runs(
        self,
    ) -> None:
        occurrence_id, item_ids = self._create_two_item_occurrence()
        start = self._function("start_schedule_attempt")
        alpha = start(
            self.engine,
            item_id=item_ids["alpha"],
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("mark_schedule_attempt_terminal_failure")(
            self.engine,
            run_id=alpha.run_id,
            failure_code="DATA",
            failed_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )

        beta = start(
            self.engine,
            item_id=item_ids["beta"],
            started_at=datetime(2026, 7, 23, 23, 40),
            _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
        )
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text(
                        "SELECT completion_state "
                        "FROM t_schedule_occurrences "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one(),
                "FAILED",
            )
        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=beta.run_id,
            records=self._records(
                base_scheme_id="beta",
                targets=(("10Y", 1),),
            ),
            trusted_verifier=self._trusted_completion_verifier(
                scheme_version="beta-v1"
            ),
        )

        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text(
                        "SELECT completion_state "
                        "FROM t_schedule_occurrences "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one(),
                "FAILED",
            )

    def test_second_attempt_waits_for_every_occurrence_item_first_attempt(
        self,
    ) -> None:
        _, item_ids = self._create_two_item_occurrence()
        start = self._function("start_schedule_attempt")
        alpha_first = start(
            self.engine,
            item_id=item_ids["alpha"],
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("mark_schedule_attempt_retry_wait")(
            self.engine,
            run_id=alpha_first.run_id,
            failure_code="TRANSIENT_INFRA",
            failed_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )

        with self.assertRaisesRegex(RuntimeError, "first-attempt barrier"):
            start(
                self.engine,
                item_id=item_ids["alpha"],
                trigger_origin="auto_retry",
                started_at=datetime(2026, 7, 23, 23, 40),
                _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
            )
        start(
            self.engine,
            item_id=item_ids["beta"],
            started_at=datetime(2026, 7, 23, 23, 45),
            _clock=self._clock(datetime(2026, 7, 23, 23, 45)),
        )
        with self.assertRaisesRegex(ValueError, "attempt 2"):
            start(
                self.engine,
                item_id=item_ids["alpha"],
                trigger_origin="apscheduler",
                started_at=datetime(2026, 7, 23, 23, 50),
                _clock=self._clock(datetime(2026, 7, 23, 23, 50)),
            )
        second = start(
            self.engine,
            item_id=item_ids["alpha"],
            trigger_origin="auto_retry",
            started_at=datetime(2026, 7, 23, 23, 55),
            _clock=self._clock(datetime(2026, 7, 23, 23, 55)),
        )

        self.assertEqual(second.attempt_no, 2)

    def test_terminal_sibling_without_attempt_does_not_block_second_attempt(
        self,
    ) -> None:
        _, item_ids = self._create_two_item_occurrence()
        start = self._function("start_schedule_attempt")
        alpha_first = start(
            self.engine,
            item_id=item_ids["alpha"],
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("mark_schedule_attempt_retry_wait")(
            self.engine,
            run_id=alpha_first.run_id,
            failure_code="TRANSIENT_INFRA",
            failed_at=datetime(2026, 7, 23, 23, 35),
            _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
        )
        self._function("fail_schedule_item_without_attempt")(
            self.engine,
            item_id=item_ids["beta"],
            failure_code="GENERATION_BUILD_FAILED",
            failure_message="beta generation cannot be built",
            _clock=self._clock(datetime(2026, 7, 23, 23, 36)),
        )

        second = start(
            self.engine,
            item_id=item_ids["alpha"],
            trigger_origin="auto_retry",
            started_at=datetime(2026, 7, 23, 23, 40),
            _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
        )

        self.assertEqual(second.attempt_no, 2)

    def test_start_rejects_recovery_cutoff_and_terminal_code_overflow(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        start = self._function("start_schedule_attempt")
        with self.assertRaisesRegex(RuntimeError, "recovery cutoff"):
            start(
                self.engine,
                item_id=item_id,
                started_at=datetime(2026, 7, 24, 0, 30),
                _clock=self._clock(datetime(2026, 7, 24, 0, 30)),
            )

        attempt = start(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        with self.assertRaisesRegex(ValueError, "64"):
            self._function("mark_schedule_attempt_terminal_failure")(
                self.engine,
                run_id=attempt.run_id,
                failure_code="X" * 65,
            )

    def test_attempt_claim_resamples_clock_after_occurrence_lock(
        self,
    ) -> None:
        from scheduler.repository import start_schedule_attempt

        _, item_id = self._create_occurrence()
        clock = _MutableLedgerClock(
            datetime(2026, 7, 24, 0, 29, 59)
        )

        def advance_after_lock(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            normalized = " ".join(str(statement).split()).lower()
            if "from t_schedule_occurrences" in normalized:
                clock.set(datetime(2026, 7, 24, 0, 30))

        event.listen(
            self.engine,
            "before_cursor_execute",
            advance_after_lock,
        )
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "recovery cutoff",
            ):
                start_schedule_attempt(
                    self.engine,
                    item_id=item_id,
                    _clock=clock,
                )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                advance_after_lock,
            )

    def test_real_process_start_is_rejected_at_recovery_cutoff(
        self,
    ) -> None:
        from scheduler.repository import (
            register_schedule_attempt_process,
            start_schedule_attempt,
        )

        _, item_id = self._create_occurrence()
        attempt = start_schedule_attempt(
            self.engine,
            item_id=item_id,
            execution_token="cutoff-fence-token",
            _clock=self._clock(datetime(2026, 7, 24, 0, 29, 59)),
        )

        with self.assertRaisesRegex(RuntimeError, "recovery cutoff"):
            register_schedule_attempt_process(
                self.engine,
                run_id=attempt.run_id,
                execution_token=attempt.execution_token,
                process_id=9401,
                process_group_id=9401,
                _clock=self._clock(datetime(2026, 7, 24, 0, 30)),
            )

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT r.started_at AS run_started_at, "
                    "i.started_at AS item_started_at "
                    "FROM t_scheme_runs r JOIN t_schedule_items i "
                    "ON i.item_id = r.schedule_item_id "
                    "WHERE r.run_id = :run_id"
                ),
                {"run_id": attempt.run_id},
            ).mappings().one()
        self.assertIsNone(row["run_started_at"])
        self.assertIsNone(row["item_started_at"])

    def test_cutoff_rejected_process_attempt_is_persisted_expired(
        self,
    ) -> None:
        from scheduler.repository import (
            mark_schedule_attempt_terminal_failure,
            start_schedule_attempt,
        )

        occurrence_id, item_id = self._create_occurrence()
        attempt = start_schedule_attempt(
            self.engine,
            item_id=item_id,
            execution_token="cutoff-expired-token",
            _clock=self._clock(datetime(2026, 7, 24, 0, 29, 59)),
        )
        state = mark_schedule_attempt_terminal_failure(
            self.engine,
            run_id=attempt.run_id,
            failure_code="RECOVERY_CUTOFF_EXPIRED",
            error_message="real process start crossed cutoff",
            _clock=self._clock(datetime(2026, 7, 24, 0, 30)),
        )

        self.assertEqual(state, "EXPIRED")
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT i.state, i.failure_code, r.status, "
                    "r.failure_code AS run_failure_code, "
                    "o.completion_state "
                    "FROM t_schedule_items i "
                    "JOIN t_scheme_runs r "
                    "ON r.run_id = i.current_run_id "
                    "JOIN t_schedule_occurrences o "
                    "ON o.occurrence_id = i.occurrence_id "
                    "WHERE i.item_id = :item_id "
                    "AND o.occurrence_id = :occurrence_id"
                ),
                {
                    "item_id": item_id,
                    "occurrence_id": occurrence_id,
                },
            ).mappings().one()
        self.assertEqual(row["state"], "EXPIRED")
        self.assertEqual(
            row["failure_code"],
            "RECOVERY_CUTOFF_EXPIRED",
        )
        self.assertEqual(row["status"], "failed")
        self.assertEqual(
            row["run_failure_code"],
            "RECOVERY_CUTOFF_EXPIRED",
        )
        self.assertEqual(row["completion_state"], "FAILED")

    def test_recovery_cutoff_expires_pending_items(self) -> None:
        occurrence_id, item_id = self._create_occurrence(
            generation_status="SEALED"
        )

        expired = self._function("expire_schedule_items")(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 30),
            _clock=self._clock(datetime(2026, 7, 24, 0, 30)),
        )

        self.assertEqual(expired, 1)
        with self.engine.connect() as conn:
            terminal = conn.execute(
                text(
                    "SELECT i.state, o.completion_state, o.sla_outcome "
                    "FROM t_schedule_items i JOIN t_schedule_occurrences o "
                    "ON o.occurrence_id = i.occurrence_id "
                    "WHERE i.item_id = :item_id"
                ),
                {"item_id": item_id},
            ).mappings().one()
        self.assertEqual(terminal["state"], "EXPIRED")
        self.assertEqual(terminal["completion_state"], "FAILED")
        self.assertEqual(terminal["sla_outcome"], "PENDING")

    def test_expiry_resamples_clock_after_occurrence_lock(self) -> None:
        occurrence_id, item_id = self._create_occurrence(
            generation_status="SEALED"
        )
        clock = _MutableLedgerClock(
            datetime(2026, 7, 24, 0, 29, 59)
        )

        def advance_after_lock(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            normalized = " ".join(str(statement).split()).lower()
            if "from t_schedule_occurrences" in normalized:
                clock.set(datetime(2026, 7, 24, 0, 30))

        event.listen(
            self.engine,
            "before_cursor_execute",
            advance_after_lock,
        )
        try:
            expired = self._function("expire_schedule_items")(
                self.engine,
                occurrence_id=occurrence_id,
                _clock=clock,
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                advance_after_lock,
            )

        self.assertEqual(expired, 1)
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text(
                        "SELECT state FROM t_schedule_items "
                        "WHERE item_id = :item_id"
                    ),
                    {"item_id": item_id},
                ).scalar_one(),
                "EXPIRED",
            )

    def test_v2_start_sla_is_write_once_after_0745(self) -> None:
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=self._item_policy("v2"),
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )
        evaluate = self._function("evaluate_schedule_item_start_sla")

        first = evaluate(
            self.engine,
            item_id=item_id,
            evaluated_at=datetime(2026, 7, 23, 23, 45),
            _clock=self._clock(datetime(2026, 7, 23, 23, 45)),
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_schedule_items SET started_at = :started_at "
                    "WHERE item_id = :item_id"
                ),
                {
                    "item_id": item_id,
                    "started_at": datetime(2026, 7, 23, 23, 50),
                },
            )
        second = evaluate(
            self.engine,
            item_id=item_id,
            evaluated_at=datetime(2026, 7, 24, 0, 0),
            _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
        )

        self.assertEqual(first.status, "LATE")
        self.assertEqual(first.reason, "V2_NOT_STARTED_BY_0745")
        self.assertTrue(first.newly_persisted)
        self.assertFalse(second.newly_persisted)
        self.assertEqual(second.status, first.status)
        self.assertEqual(second.evaluated_at, first.evaluated_at)
        self.assertEqual(second.deadline_at, first.deadline_at)
        self.assertEqual(second.reason, first.reason)

    def test_v2_guardrail_resamples_clock_after_item_context_lock(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=self._item_policy("v2"),
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )
        clock = _MutableLedgerClock(
            datetime(2026, 7, 23, 23, 44, 59)
        )

        def advance_after_lock(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            normalized = " ".join(str(statement).split()).lower()
            if "from t_schedule_occurrences" in normalized:
                clock.set(datetime(2026, 7, 23, 23, 45))

        event.listen(
            self.engine,
            "before_cursor_execute",
            advance_after_lock,
        )
        try:
            projection = self._function(
                "evaluate_schedule_item_start_sla"
            )(
                self.engine,
                item_id=item_id,
                _clock=clock,
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                advance_after_lock,
            )

        self.assertEqual(projection.status, "LATE")
        self.assertEqual(
            projection.reason,
            "V2_NOT_STARTED_BY_0745",
        )

    def test_target_sla_resamples_clock_after_occurrence_lock(
        self,
    ) -> None:
        occurrence_id, _item_id = self._create_occurrence()
        clock = _MutableLedgerClock(
            datetime(2026, 7, 23, 23, 59, 59)
        )

        def advance_after_lock(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            normalized = " ".join(str(statement).split()).lower()
            if "from t_schedule_occurrences" in normalized:
                clock.set(datetime(2026, 7, 24, 0, 0, 5))

        event.listen(
            self.engine,
            "before_cursor_execute",
            advance_after_lock,
        )
        try:
            projection = self._function(
                "evaluate_schedule_occurrence_target_sla"
            )(
                self.engine,
                occurrence_id=occurrence_id,
                _clock=clock,
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                advance_after_lock,
            )

        self.assertEqual(projection.status, "BREACHED")
        self.assertEqual(projection.accepted_by_deadline_count, 0)

    def test_v2_start_guardrail_is_not_blocked_by_late_dynamic_release(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        item_policy = self._item_policy("v2")
        item_policy["v2"]["release_offset_minutes"] = 6
        item_policy["v2"]["release_at"] = datetime(
            2026,
            7,
            23,
            23,
            50,
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="daily-production",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=item_policy,
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )

        projection = self._function(
            "evaluate_schedule_item_start_sla"
        )(
            self.engine,
            item_id=item_id,
            evaluated_at=datetime(2026, 7, 23, 23, 45),
            _clock=self._clock(datetime(2026, 7, 23, 23, 45)),
        )

        self.assertEqual(projection.status, "LATE")
        self.assertEqual(
            projection.reason,
            "V2_NOT_STARTED_BY_0745",
        )

    def test_completion_uses_post_verification_time_and_occurrence_sla_deadline(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_schedule_items SET deadline_at = :deadline_at "
                    "WHERE item_id = :item_id"
                ),
                {
                    "item_id": item_id,
                    "deadline_at": datetime(2026, 7, 23, 23, 55),
                },
            )
            conn.execute(
                text(
                    "UPDATE t_input_generations SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        clock = _MutableLedgerClock(datetime(2026, 7, 23, 23, 54, 59))
        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(),
            trusted_verifier=_ClockAdvancingCompletionVerifier(
                clock,
                datetime(2026, 7, 23, 23, 56),
            ),
            _clock=clock,
        )
        projection = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 0),
            _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
        )

        self.assertEqual(projection.status, "MET")
        self.assertEqual(projection.accepted_target_count, 2)
        self.assertEqual(projection.accepted_by_deadline_count, 2)
        with self.engine.connect() as conn:
            accepted_at = conn.execute(
                text(
                    "SELECT MIN(accepted_at) "
                    "FROM t_schedule_item_targets "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        self.assertEqual(str(accepted_at), "2026-07-23 23:56:00")

    def test_sla_uses_post_commit_visibility_receipt_not_precommit_acceptance(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_input_generations SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        completion_clock = _SequenceLedgerClock(
            datetime(2026, 7, 23, 23, 59, 59, 900_000),
            datetime(2026, 7, 24, 0, 0, 0, 100_000),
        )

        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(),
            trusted_verifier=self._trusted_completion_verifier(),
            _clock=completion_clock,
        )
        projection = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 0, 1),
            _clock=self._clock(datetime(2026, 7, 24, 0, 0, 1)),
        )

        self.assertEqual(projection.status, "BREACHED")
        self.assertEqual(projection.accepted_target_count, 2)
        self.assertEqual(projection.accepted_by_deadline_count, 0)
        with self.engine.connect() as conn:
            receipt = conn.execute(
                text(
                    "SELECT MIN(accepted_at) AS accepted_at, "
                    "MIN(visible_at) AS visible_at "
                    "FROM t_schedule_item_targets "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
        self.assertEqual(
            str(receipt["accepted_at"]),
            "2026-07-23 23:59:59.900000",
        )
        self.assertEqual(
            str(receipt["visible_at"]),
            "2026-07-24 00:00:00.100000",
        )

    def test_attempt_started_before_cutoff_may_finish_late_without_rewriting_sla(
        self,
    ) -> None:
        """08:30 只阻止新启动；在跑任务可 late commit，但不能修复 08:00 SLA。"""
        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_input_generations SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 24, 0, 29, 59),
            _clock=self._clock(datetime(2026, 7, 24, 0, 29, 59)),
        )

        breached = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 30),
            _clock=self._clock(datetime(2026, 7, 24, 0, 30)),
        )
        self.assertEqual(breached.status, "BREACHED")
        self.assertEqual(breached.accepted_target_count, 0)

        written = self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(),
            trusted_verifier=self._trusted_completion_verifier(),
            completed_at=datetime(2026, 7, 24, 0, 31),
            _clock=self._clock(datetime(2026, 7, 24, 0, 31)),
        )
        replay = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 32),
            _clock=self._clock(datetime(2026, 7, 24, 0, 32)),
        )

        self.assertEqual(written, 2)
        self.assertEqual(replay.status, "BREACHED")
        self.assertEqual(replay.evaluated_at, breached.evaluated_at)
        self.assertEqual(replay.accepted_target_count, 2)
        self.assertEqual(replay.accepted_by_deadline_count, 0)
        with self.engine.connect() as conn:
            occurrence = conn.execute(
                text(
                    "SELECT completion_state, accepted_target_count, "
                    "sla_outcome, sla_accepted_target_count "
                    "FROM t_schedule_occurrences "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
        self.assertEqual(
            dict(occurrence),
            {
                "completion_state": "SUCCESS",
                "accepted_target_count": 2,
                "sla_outcome": "BREACHED",
                "sla_accepted_target_count": 0,
            },
        )

    def test_missing_receipt_is_conservative_and_replay_uses_replay_time(
        self,
    ) -> None:
        from scheduler import repository

        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_input_generations SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        with self.assertLogs("scheduler.repository", level="ERROR"):
            with patch.object(
                repository,
                "record_schedule_attempt_visibility",
                side_effect=RuntimeError("receipt transaction unavailable"),
            ):
                written = self._function("complete_scheduled_attempt")(
                    self.engine,
                    run_id=attempt.run_id,
                    records=self._records(),
                    trusted_verifier=self._trusted_completion_verifier(),
                    _clock=self._clock(
                        datetime(2026, 7, 23, 23, 59, 59, 900_000)
                    ),
                )

        self.assertEqual(written, 2)
        with self.engine.connect() as conn:
            committed = conn.execute(
                text(
                    "SELECT r.status AS run_status, i.state AS item_state, "
                    "COUNT(t.visible_at) AS receipt_count "
                    "FROM t_scheme_runs r "
                    "JOIN t_schedule_items i "
                    "ON i.current_run_id = r.run_id "
                    "JOIN t_schedule_item_targets t "
                    "ON t.item_id = i.item_id "
                    "WHERE r.run_id = :run_id "
                    "GROUP BY r.status, i.state"
                ),
                {"run_id": attempt.run_id},
            ).mappings().one()
        self.assertEqual(dict(committed), {
            "run_status": "success",
            "item_state": "SUCCESS",
            "receipt_count": 0,
        })

        evaluate = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )
        predeadline = evaluate(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 23, 23, 59, 59, 950_000),
            _clock=self._clock(
                datetime(2026, 7, 23, 23, 59, 59, 950_000)
            ),
        )
        first = evaluate(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 1),
            _clock=self._clock(datetime(2026, 7, 24, 0, 1)),
        )
        self.assertEqual(predeadline.status, "PENDING")
        self.assertEqual(predeadline.accepted_target_count, 0)
        self.assertEqual(first.status, "BREACHED")
        self.assertEqual(first.accepted_target_count, 0)
        self.assertEqual(first.accepted_by_deadline_count, 0)

        reconciled = self._function(
            "reconcile_schedule_occurrence_visibility_receipts"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            _clock=self._clock(datetime(2026, 7, 24, 0, 3)),
        )
        replayed_again = self._function(
            "reconcile_schedule_occurrence_visibility_receipts"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )
        second = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )

        self.assertEqual(reconciled, 1)
        self.assertEqual(replayed_again, 0)
        self.assertEqual(second.status, "BREACHED")
        self.assertEqual(second.accepted_target_count, 2)
        self.assertEqual(second.evaluated_at, first.evaluated_at)
        with self.engine.connect() as conn:
            visible_at = conn.execute(
                text(
                    "SELECT MIN(visible_at) "
                    "FROM t_schedule_item_targets "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        self.assertEqual(str(visible_at), "2026-07-24 00:03:00")

    def test_visibility_receipt_bridges_subsecond_storage_rounding(
        self,
    ) -> None:
        (
            _occurrence_id,
            item_id,
            run_id,
            _prediction_id,
            _registry_id,
        ) = self._create_completed_single_target()
        rounded_finished_at = datetime(2026, 7, 23, 23, 40, 1)
        observed_at = datetime(
            2026,
            7,
            23,
            23,
            40,
            0,
            500_000,
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_runs SET finished_at = :finished_at "
                    "WHERE run_id = :run_id"
                ),
                {
                    "finished_at": rounded_finished_at,
                    "run_id": run_id,
                },
            )
            conn.execute(
                text(
                    "UPDATE t_schedule_item_targets SET visible_at = NULL "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )

        visible_at = self._function(
            "record_schedule_attempt_visibility"
        )(
            self.engine,
            run_id=run_id,
            _clock=self._clock(observed_at),
        )

        self.assertEqual(visible_at, rounded_finished_at)
        with self.engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT visible_at FROM t_schedule_item_targets "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).scalar_one()
        self.assertEqual(
            str(stored),
            rounded_finished_at.isoformat(sep=" "),
        )

    def test_visibility_receipt_rejects_material_future_finished_at(
        self,
    ) -> None:
        (
            _occurrence_id,
            item_id,
            run_id,
            _prediction_id,
            _registry_id,
        ) = self._create_completed_single_target()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_runs SET finished_at = :finished_at "
                    "WHERE run_id = :run_id"
                ),
                {
                    "finished_at": datetime(
                        2026,
                        7,
                        23,
                        23,
                        40,
                        1,
                    ),
                    "run_id": run_id,
                },
            )
            conn.execute(
                text(
                    "UPDATE t_schedule_item_targets SET visible_at = NULL "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "visible_at cannot be before run finished_at",
        ):
            self._function("record_schedule_attempt_visibility")(
                self.engine,
                run_id=run_id,
                _clock=self._clock(
                    datetime(
                        2026,
                        7,
                        23,
                        23,
                        40,
                        0,
                        499_999,
                    )
                ),
            )

    def test_visibility_receipt_does_not_bridge_precise_future_time(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "visible_at cannot be before run finished_at",
        ):
            self._function("_visibility_time_after_run_finish")(
                datetime(
                    2026,
                    7,
                    23,
                    23,
                    40,
                    0,
                    500_001,
                ),
                datetime(
                    2026,
                    7,
                    23,
                    23,
                    40,
                    1,
                    1,
                ),
            )

    def test_multi_target_item_missing_one_record_cannot_succeed(self) -> None:
        occurrence_id, item_id = self._create_occurrence(generation_status="SEALED")
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            execution_token="attempt-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        with self.assertRaisesRegex(RuntimeError, "target multiset mismatch"):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(targets=(("5Y", 1),)),
                trusted_verifier=self._trusted_completion_verifier(),
            )

        self._assert_no_predictions()
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text(
                        "SELECT state FROM t_schedule_items "
                        "WHERE item_id = :item_id"
                    ),
                    {"item_id": item_id},
                ).scalar_one(),
                "RUNNING",
            )
            self.assertNotEqual(
                conn.execute(
                    text(
                        "SELECT completion_state FROM t_schedule_occurrences "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one(),
                "SUCCESS",
            )

    def test_stale_run_is_rejected_before_any_prediction_insert(self) -> None:
        _, item_id = self._create_occurrence(generation_status="SEALED")
        start_attempt = self._function("start_schedule_attempt")
        first = start_attempt(
            self.engine,
            item_id=item_id,
            execution_token="first-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("abandon_current_schedule_attempt")(
            self.engine,
            item_id=item_id,
            orphan_cleanup_confirmed=True,
            abandoned_at=datetime(2026, 7, 23, 23, 50),
            _clock=self._clock(datetime(2026, 7, 23, 23, 50)),
        )
        start_attempt(
            self.engine,
            item_id=item_id,
            trigger_origin="operator_recovery",
            execution_token="second-token",
            started_at=datetime(2026, 7, 24, 0, 0),
            _clock=self._clock(datetime(2026, 7, 24, 0, 0)),
        )

        with self.assertRaisesRegex(RuntimeError, "stale scheduled run fence"):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=first.run_id,
                records=self._records(),
                trusted_verifier=self._trusted_completion_verifier(),
            )

        self._assert_no_predictions()

    def test_binding_and_start_both_reject_non_sealed_generation(self) -> None:
        _, item_id = self._create_occurrence(generation_status="BUILDING")
        with self.assertRaisesRegex(RuntimeError, "not SEALED"):
            self._function("bind_schedule_item_input_generation")(
                self.engine,
                item_id=item_id,
                generation_id="generation-20260724",
                expected_feature_date="2026-07-23",
            )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_schedule_items SET input_generation_id = "
                    "'generation-20260724' WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )
        with self.assertRaisesRegex(RuntimeError, "not SEALED"):
            self._function("start_schedule_attempt")(
                self.engine,
                item_id=item_id,
                started_at=datetime(2026, 7, 23, 23, 30),
                _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
            )
        self._assert_no_predictions()

    def test_completion_rechecks_generation_is_still_sealed(self) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("invalidate_input_generation")(
            self.engine,
            generation_id="generation-20260724",
            reason="manifest revoked",
        )

        with self.assertRaisesRegex(RuntimeError, "not SEALED"):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
                trusted_verifier=self._trusted_completion_verifier(),
            )
        self._assert_no_predictions()

    def test_trusted_completion_verifier_is_required_not_optional(self) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        with self.assertRaises(TypeError):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
            )

    def test_completion_verifier_receives_expectation_not_connection(
        self,
    ) -> None:
        from scheduler.repository import ScheduledCompletionExpectation

        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        verifier = self._trusted_completion_verifier()

        written = self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(),
            trusted_verifier=verifier,
        )

        self.assertEqual(written, 2)
        self.assertIsInstance(
            verifier.expectation,
            ScheduledCompletionExpectation,
        )
        self.assertFalse(hasattr(verifier.expectation, "execute"))

    def _assert_completion_evidence_rejected(
        self,
        *,
        expected_field: str,
        verifier: object,
    ) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        with self.assertRaisesRegex(RuntimeError, expected_field):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
                trusted_verifier=verifier,
            )
        self._assert_no_predictions()

    def test_completion_rejects_observed_generation_id_mismatch(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="observed_generation_id",
            verifier=self._trusted_completion_verifier(
                observed_generation_id="generation-wrong"
            ),
        )

    def test_completion_rejects_manifest_sha256_mismatch(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="manifest_sha256",
            verifier=self._trusted_completion_verifier(
                manifest_sha256="f" * 64
            ),
        )

    def test_completion_rejects_generation_content_id_drift(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="generation_dataset_content_id",
            verifier=self._trusted_completion_verifier(
                generation_dataset_content_id="f" * 64,
            ),
        )

    def test_completion_rejects_generation_schema_drift(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="generation_schema_version",
            verifier=self._trusted_completion_verifier(
                generation_schema_version="wrong-schema",
            ),
        )

    def test_completion_rejects_generation_exporter_drift(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="generation_exporter_version",
            verifier=self._trusted_completion_verifier(
                generation_exporter_version="wrong-exporter",
            ),
        )

    def test_completion_rejects_evidence_feature_date_mismatch(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="feature_date",
            verifier=self._trusted_completion_verifier(
                feature_date="2026-07-22"
            ),
        )

    def test_completion_rejects_evidence_scheme_version_mismatch(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="scheme_version",
            verifier=self._trusted_completion_verifier(
                scheme_version="alpha-v2"
            ),
        )

    def test_completion_rejects_evidence_code_sha256_mismatch(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="code_sha256",
            verifier=self._trusted_completion_verifier(
                code_sha256="c" * 64
            ),
        )

    def test_completion_rejects_evidence_config_sha256_mismatch(self) -> None:
        self._assert_completion_evidence_rejected(
            expected_field="config_sha256",
            verifier=self._trusted_completion_verifier(
                config_sha256="c" * 64
            ),
        )

    def test_completion_rejects_mapping_evidence_even_with_all_fields(
        self,
    ) -> None:
        mapping = {
            "observed_generation_id": "generation-20260724",
            "manifest_sha256": "e" * 64,
            "feature_date": "2026-07-23",
            "scheme_version": "alpha-v1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
        }
        self._assert_completion_evidence_rejected(
            expected_field="ScheduledCompletionEvidence",
            verifier=_ReturningCompletionVerifier(mapping),
        )

    def test_completion_rejects_malformed_validator_result(self) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "ScheduledCompletionEvidence",
        ):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
                trusted_verifier=_ReturningCompletionVerifier(None),
            )
        self._assert_no_predictions()

    def test_completion_rejects_record_feature_date_drift(self) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        records = [
            PredictionRecord(
                **{
                    **record.__dict__,
                    "feature_date": "2026-07-22",
                    "extra": {"feature_date": "2026-07-22"},
                }
            )
            for record in self._records()
        ]

        with self.assertRaisesRegex(RuntimeError, "record feature_date"):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=records,
                trusted_verifier=self._trusted_completion_verifier(),
            )
        self._assert_no_predictions()

    def test_completion_failure_rolls_back_predictions_and_all_ledger_states(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence(generation_status="SEALED")
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            execution_token="attempt-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        with self.assertRaisesRegex(RuntimeError, "injected precommit failure"):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
                trusted_verifier=_RaisingCompletionVerifier(
                    RuntimeError("injected precommit failure")
                ),
            )

        self._assert_no_predictions()
        with self.engine.connect() as conn:
            run_status = conn.execute(
                text("SELECT status FROM t_scheme_runs WHERE run_id = :run_id"),
                {"run_id": attempt.run_id},
            ).scalar_one()
            item_status = conn.execute(
                text("SELECT state FROM t_schedule_items WHERE item_id = :item_id"),
                {"item_id": item_id},
            ).scalar_one()
            occurrence_status = conn.execute(
                text(
                    "SELECT completion_state FROM t_schedule_occurrences "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
            accepted = conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_schedule_item_targets "
                    "WHERE occurrence_id = :occurrence_id AND status = 'ACCEPTED'"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        self.assertEqual(run_status, "running")
        self.assertEqual(item_status, "RUNNING")
        self.assertEqual(occurrence_status, "RUNNING")
        self.assertEqual(accepted, 0)

    def test_completion_uses_canonical_ledger_lock_order(self) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        statements, listener = self._capture_sql()
        try:
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(),
                trusted_verifier=self._trusted_completion_verifier(),
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )

        occurrence_index = self._sql_index(
            statements,
            "from t_schedule_occurrences",
            "where occurrence_id",
        )
        siblings_index = self._sql_index_after(
            statements,
            occurrence_index,
            "from t_schedule_items",
            "where occurrence_id",
            "order by item_id",
        )
        generation_index = self._sql_index_after(
            statements,
            siblings_index,
            "from t_input_generations",
            "where generation_id",
        )
        run_index = self._sql_index_after(
            statements,
            generation_index,
            "select run_id, scheme_id",
            "from t_scheme_runs",
            "where run_id",
        )
        target_index = self._sql_index_after(
            statements,
            run_index,
            "from t_schedule_item_targets",
            "where item_id",
            "order by target_id",
        )
        self.assertLess(occurrence_index, siblings_index)
        self.assertLess(siblings_index, generation_index)
        self.assertLess(generation_index, run_index)
        self.assertLess(run_index, target_index)

    def test_terminal_failure_locks_occurrence_siblings_before_run(
        self,
    ) -> None:
        _, item_id = self._create_occurrence()
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        statements, listener = self._capture_sql()
        try:
            self._function("mark_schedule_attempt_terminal_failure")(
                self.engine,
                run_id=attempt.run_id,
                failure_code="DATA",
                failed_at=datetime(2026, 7, 23, 23, 35),
                _clock=self._clock(datetime(2026, 7, 23, 23, 35)),
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )

        occurrence_index = self._sql_index(
            statements,
            "from t_schedule_occurrences",
            "where occurrence_id",
        )
        siblings_index = self._sql_index(
            statements,
            "from t_schedule_items",
            "where occurrence_id",
            "order by item_id",
        )
        run_index = self._sql_index(
            statements,
            "select run_id, scheme_id",
            "from t_scheme_runs",
            "where run_id",
        )
        self.assertLess(occurrence_index, siblings_index)
        self.assertLess(siblings_index, run_index)

    def test_scheduled_completion_is_insert_only_and_cannot_replace_old_winner(
        self,
    ) -> None:
        _, item_id = self._create_occurrence(
            generation_status="SEALED",
            targets=(("5Y", 1),),
        )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            execution_token="attempt-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon, predict_date,
                         feature_date, target_date, prediction_phase,
                         predicted_direction, extra)
                    VALUES
                        (999, 'alpha', '5Y', 1, '2026-07-23', '2026-07-22',
                         '2026-07-25', 'scheduled_live', -1, '{}')
                    """
                )
            )

        with self.assertRaises(IntegrityError):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(targets=(("5Y", 1),)),
                trusted_verifier=self._trusted_completion_verifier(),
            )

        with self.engine.connect() as conn:
            winner = conn.execute(
                text(
                    "SELECT run_id, predicted_direction "
                    "FROM t_scheme_predictions WHERE scheme_id = 'alpha' "
                    "AND target_tenor = '5Y' AND horizon = 1 "
                    "AND target_date = '2026-07-25'"
                )
            ).mappings().one()
        self.assertEqual(dict(winner), {
            "run_id": 999,
            "predicted_direction": -1,
        })

    def test_successful_completion_accepts_targets_and_finishes_occurrence(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence(generation_status="SEALED")
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            execution_token="attempt-token",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )

        written = self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(),
            trusted_verifier=self._trusted_completion_verifier(),
        )

        self.assertEqual(written, 2)
        with self.engine.connect() as conn:
            run_status = conn.execute(
                text("SELECT status FROM t_scheme_runs WHERE run_id = :run_id"),
                {"run_id": attempt.run_id},
            ).scalar_one()
            item_status = conn.execute(
                text("SELECT state FROM t_schedule_items WHERE item_id = :item_id"),
                {"item_id": item_id},
            ).scalar_one()
            occurrence = conn.execute(
                text(
                    "SELECT completion_state, accepted_target_count "
                    "FROM t_schedule_occurrences WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
            accepted = conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_schedule_item_targets t "
                    "JOIN t_scheme_predictions p "
                    "  ON p.id = t.accepted_prediction_id "
                    "WHERE t.occurrence_id = :occurrence_id "
                    "  AND t.status = 'ACCEPTED' "
                    "  AND t.accepted_run_id = :run_id "
                    "  AND p.run_id = :run_id"
                ),
                {
                    "occurrence_id": occurrence_id,
                    "run_id": attempt.run_id,
                },
            ).scalar_one()
        self.assertEqual(run_status, "success")
        self.assertEqual(item_status, "SUCCESS")
        self.assertEqual(dict(occurrence), {
            "completion_state": "SUCCESS",
            "accepted_target_count": 2,
        })
        self.assertEqual(accepted, 2)

    def test_cross_occurrence_target_cannot_receive_visibility_receipt(
        self,
    ) -> None:
        (
            occurrence_id,
            item_id,
            run_id,
            _prediction_id,
            registry_id,
        ) = self._create_completed_single_target()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_schedule_item_targets "
                    "SET occurrence_id = :foreign_occurrence_id, "
                    "visible_at = NULL "
                    "WHERE item_id = :item_id"
                ),
                {
                    "foreign_occurrence_id": occurrence_id + 10_000,
                    "item_id": item_id,
                },
            )

        probe = self._function("read_schedule_api_visibility_probe")(
            self.engine,
            occurrence_id=occurrence_id,
        )
        projection = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "target linkage invalid",
        ):
            self._function("record_schedule_attempt_visibility")(
                self.engine,
                run_id=run_id,
                _clock=self._clock(datetime(2026, 7, 23, 23, 45)),
            )

        with self.engine.connect() as conn:
            visible_at = conn.execute(
                text(
                    "SELECT visible_at FROM t_schedule_item_targets "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            ).scalar_one()
        self.assertIsNone(visible_at)
        self.assertEqual(probe.committed_registry_ids, ())
        self.assertEqual(probe.missing_registry_ids, (registry_id,))
        self.assertEqual(projection.status, "BREACHED")
        self.assertEqual(projection.accepted_target_count, 0)
        health = self._function("read_schedule_health_envelope")(
            self.engine,
            item_id=item_id,
        )
        self.assertFalse(health.targets[0].accepted_linkage_valid)

    def test_accepted_old_run_is_missing_and_cannot_remain_success(
        self,
    ) -> None:
        (
            occurrence_id,
            item_id,
            old_run_id,
            _prediction_id,
            registry_id,
        ) = self._create_completed_single_target()
        with self.engine.begin() as conn:
            replacement = conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_runs
                        (scheme_id, scheme_version, runtime_type, run_type,
                         prediction_phase, predict_date, status,
                         schedule_item_id, attempt_no, trigger_origin,
                         execution_token, started_at, finished_at)
                    VALUES
                        ('alpha', 'alpha-v1', 'native_adapter', 'active',
                         'scheduled_live', '2026-07-24', 'success',
                         :item_id, 2, 'operator_recovery',
                         'replacement-winning-run', :started_at, :finished_at)
                    """
                ),
                {
                    "item_id": item_id,
                    "started_at": datetime(2026, 7, 23, 23, 41),
                    "finished_at": datetime(2026, 7, 23, 23, 42),
                },
            )
            replacement_run_id = int(replacement.lastrowid)
            conn.execute(
                text(
                    "UPDATE t_schedule_items "
                    "SET current_run_id = :run_id, attempt_no = 2 "
                    "WHERE item_id = :item_id"
                ),
                {
                    "run_id": replacement_run_id,
                    "item_id": item_id,
                },
            )

        probe = self._function("read_schedule_api_visibility_probe")(
            self.engine,
            occurrence_id=occurrence_id,
        )
        snapshot = self._function("read_schedule_occurrence_snapshot")(
            self.engine,
            occurrence_id=occurrence_id,
        )
        health = self._function("read_schedule_health_envelope")(
            self.engine,
            item_id=item_id,
        )
        projection = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )

        self.assertNotEqual(old_run_id, replacement_run_id)
        self.assertEqual(probe.committed_registry_ids, ())
        self.assertEqual(probe.linked_registry_ids, ())
        self.assertEqual(probe.db_visible_registry_ids, ())
        self.assertEqual(probe.missing_registry_ids, (registry_id,))
        self.assertEqual(snapshot.actual_accepted_target_count, 0)
        self.assertFalse(health.targets[0].accepted_linkage_valid)
        self.assertEqual(projection.status, "BREACHED")
        self.assertEqual(projection.accepted_target_count, 0)
        with self.engine.connect() as conn:
            completion_state = conn.execute(
                text(
                    "SELECT completion_state "
                    "FROM t_schedule_occurrences "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        self.assertNotEqual(completion_state, "SUCCESS")

    def test_run_owned_by_another_item_is_not_accepted_evidence(self) -> None:
        occurrence_id, item_ids = self._create_two_item_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_input_generations "
                    "SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        run_ids: dict[str, int] = {}
        for base_scheme_id, tenor in (("alpha", "5Y"), ("beta", "10Y")):
            attempt = self._function("start_schedule_attempt")(
                self.engine,
                item_id=item_ids[base_scheme_id],
                started_at=datetime(2026, 7, 23, 23, 30),
                _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
            )
            run_ids[base_scheme_id] = int(attempt.run_id)
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(
                    base_scheme_id=base_scheme_id,
                    targets=((tenor, 1),),
                ),
                trusted_verifier=self._trusted_completion_verifier(
                    scheme_version=f"{base_scheme_id}-v1",
                ),
                completed_at=datetime(2026, 7, 23, 23, 40),
                _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
            )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_runs "
                    "SET schedule_item_id = :foreign_item_id, attempt_no = 2 "
                    "WHERE run_id = :run_id"
                ),
                {
                    "foreign_item_id": item_ids["beta"],
                    "run_id": run_ids["alpha"],
                },
            )

        snapshot = self._function("read_schedule_occurrence_snapshot")(
            self.engine,
            occurrence_id=occurrence_id,
        )
        alpha_health = self._function(
            "read_schedule_health_envelope"
        )(
            self.engine,
            item_id=item_ids["alpha"],
        )
        projection = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )

        self.assertEqual(snapshot.actual_accepted_target_count, 1)
        self.assertFalse(
            alpha_health.targets[0].accepted_linkage_valid
        )
        self.assertEqual(projection.status, "BREACHED")
        self.assertEqual(projection.accepted_target_count, 1)
        self.assertEqual(projection.accepted_by_deadline_count, 1)

    def test_prediction_wrong_run_is_not_reconciled_or_api_ready(
        self,
    ) -> None:
        (
            occurrence_id,
            item_id,
            _run_id,
            prediction_id,
            registry_id,
        ) = self._create_completed_single_target()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_predictions "
                    "SET run_id = 999999 "
                    "WHERE id = :prediction_id"
                ),
                {"prediction_id": prediction_id},
            )
            conn.execute(
                text(
                    "UPDATE t_schedule_item_targets SET visible_at = NULL "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )

        reconciled = self._function(
            "reconcile_schedule_occurrence_visibility_receipts"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            _clock=self._clock(datetime(2026, 7, 23, 23, 45)),
        )
        probe = self._function("read_schedule_api_visibility_probe")(
            self.engine,
            occurrence_id=occurrence_id,
        )
        health = self._function("read_schedule_health_envelope")(
            self.engine,
            item_id=item_id,
        )

        self.assertEqual(reconciled, 0)
        self.assertEqual(probe.committed_registry_ids, ())
        self.assertEqual(probe.linked_registry_ids, ())
        self.assertEqual(probe.db_visible_registry_ids, ())
        self.assertEqual(probe.missing_registry_ids, (registry_id,))
        self.assertFalse(health.targets[0].accepted_linkage_valid)

    def test_generic_upsert_cannot_overwrite_accepted_ledger_winner(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence(
            generation_status="SEALED",
            targets=(("5Y", 1),),
        )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            execution_token="ledger-winner",
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(targets=(("5Y", 1),)),
            trusted_verifier=self._trusted_completion_verifier(),
        )
        generic_run_id = self._function("create_scheme_run")(
            self.engine,
            scheme_id="alpha",
            predict_date="2026-07-24",
            prediction_phase="scheduled_live",
        )
        replacement = PredictionRecord(
            scheme_id="alpha",
            target_tenor="5Y",
            horizon=1,
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_date="2026-07-25",
            prediction_phase="scheduled_live",
            predicted_direction=-1,
            extra={"feature_date": "2026-07-23"},
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "frozen daily ledger target",
        ):
            self._function("insert_run_predictions")(
                self.engine,
                generic_run_id,
                [replacement],
                scheme_version="alpha-v1",
            )

        with self.engine.connect() as conn:
            winner = conn.execute(
                text(
                    "SELECT p.id, p.run_id, p.predicted_direction, "
                    "t.accepted_prediction_id, t.accepted_run_id "
                    "FROM t_scheme_predictions p "
                    "JOIN t_schedule_item_targets t "
                    "ON t.accepted_prediction_id = p.id "
                    "WHERE t.occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
        self.assertEqual(winner["run_id"], attempt.run_id)
        self.assertEqual(winner["predicted_direction"], 1)
        self.assertEqual(winner["accepted_prediction_id"], winner["id"])
        self.assertEqual(winner["accepted_run_id"], attempt.run_id)

    def test_generic_prediction_guard_uses_canonical_ledger_lock_order(
        self,
    ) -> None:
        self._create_occurrence(
            generation_status="SEALED",
            targets=(("5Y", 1),),
        )
        generic_run_id = self._function("create_scheme_run")(
            self.engine,
            scheme_id="alpha",
            predict_date="2026-07-24",
            prediction_phase="gray_live",
        )
        statements, listener = self._capture_sql()
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "frozen daily ledger target",
            ):
                self._function("insert_run_predictions")(
                    self.engine,
                    generic_run_id,
                    self._records(targets=(("5Y", 1),)),
                    scheme_version="alpha-v1",
                )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                listener,
            )

        occurrence_index = self._sql_index(
            statements,
            "from t_schedule_occurrences",
            "where occurrence_id",
        )
        siblings_index = self._sql_index_after(
            statements,
            occurrence_index,
            "from t_schedule_items",
            "where occurrence_id",
            "order by item_id",
        )
        targets_index = self._sql_index_after(
            statements,
            siblings_index,
            "from t_schedule_item_targets",
            "where item_id",
            "order by target_id",
        )
        self.assertLess(occurrence_index, siblings_index)
        self.assertLess(siblings_index, targets_index)

    def test_v2_completion_rejects_invalidated_native_generation(
        self,
    ) -> None:
        self._seed_registry(
            base_scheme_id="v2",
            runtime_type="blackbox_v2",
            targets=(("1Y", 5),),
        )
        native_generation_id = self._create_native_calendar_generation()
        databridge = self._generation_params("databridge-invalidation")
        databridge["generation_type"] = "databridge_v1"
        self._function("create_input_generation")(
            self.engine,
            **databridge,
            native_generation_id=native_generation_id,
            native_manifest_sha256="f" * 64,
        )
        self._function("seal_input_generation")(
            self.engine,
            generation_id="databridge-invalidation",
            sealed_at=datetime(2026, 7, 23, 23, 10),
        )
        occurrence_id = self._function("create_schedule_occurrence")(
            self.engine,
            schedule_key="v2-native-invalidation",
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_dates=self._target_dates(
                base_scheme_id="v2",
                targets=(("1Y", 5),),
            ),
            item_policy_by_base=self._item_policy("v2"),
        )
        with self.engine.connect() as conn:
            item_id = int(
                conn.execute(
                    text(
                        "SELECT item_id FROM t_schedule_items "
                        "WHERE occurrence_id = :occurrence_id"
                    ),
                    {"occurrence_id": occurrence_id},
                ).scalar_one()
            )
        self._function("bind_schedule_item_input_generation")(
            self.engine,
            item_id=item_id,
            generation_id="databridge-invalidation",
            expected_feature_date="2026-07-23",
        )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("invalidate_input_generation")(
            self.engine,
            generation_id=native_generation_id,
            reason="calendar manifest drift",
            invalidated_at=datetime(2026, 7, 23, 23, 35),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "native generation relation",
        ):
            self._function("complete_scheduled_attempt")(
                self.engine,
                run_id=attempt.run_id,
                records=self._records(
                    base_scheme_id="v2",
                    targets=(("1Y", 5),),
                ),
                trusted_verifier=self._trusted_completion_verifier(
                    observed_generation_id="databridge-invalidation",
                    scheme_version="v2-v1",
                ),
                completed_at=datetime(2026, 7, 23, 23, 40),
                _clock=self._clock(datetime(2026, 7, 23, 23, 40)),
            )
        self._assert_no_predictions()

    def test_persisted_target_sla_breach_does_not_flip_after_late_fill(self) -> None:
        targets = tuple((f"T{index}", 1) for index in range(25))
        occurrence_id, item_id = self._create_occurrence(
            generation_status="SEALED",
            targets=targets,
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_input_generations "
                    "SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(targets=targets),
            trusted_verifier=self._trusted_completion_verifier(),
            completed_at=datetime(2026, 7, 23, 23, 59),
            _clock=self._clock(datetime(2026, 7, 23, 23, 59)),
        )
        with self.engine.begin() as conn:
            target_ids = conn.execute(
                text(
                    "SELECT target_id FROM t_schedule_item_targets "
                    "WHERE occurrence_id = :occurrence_id ORDER BY target_id"
                ),
                {"occurrence_id": occurrence_id},
            ).scalars().all()
            conn.execute(
                text(
                    "UPDATE t_schedule_item_targets "
                    "SET visible_at = :visible_at "
                    "WHERE target_id = :target_id"
                ),
                {
                    "target_id": target_ids[-1],
                    "visible_at": datetime(2026, 7, 24, 0, 3),
                },
            )

        evaluate = self._function("evaluate_schedule_occurrence_target_sla")
        first = evaluate(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )
        second = evaluate(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 10),
            _clock=self._clock(datetime(2026, 7, 24, 0, 10)),
        )

        self.assertEqual(first.status, "BREACHED")
        self.assertEqual(first.accepted_target_count, 25)
        self.assertEqual(first.accepted_by_deadline_count, 24)
        self.assertTrue(first.newly_persisted)
        self.assertEqual(second.status, "BREACHED")
        self.assertFalse(second.newly_persisted)
        self.assertEqual(second.evaluated_at, first.evaluated_at)
        with self.engine.connect() as conn:
            counts = conn.execute(
                text(
                    "SELECT accepted_target_count, sla_accepted_target_count "
                    "FROM t_schedule_occurrences "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().one()
        self.assertEqual(dict(counts), {
            "accepted_target_count": 25,
            "sla_accepted_target_count": 24,
        })

    def test_delayed_watchdog_marks_met_when_every_target_was_on_time(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_input_generations "
                    "SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(),
            trusted_verifier=self._trusted_completion_verifier(),
            completed_at=datetime(2026, 7, 23, 23, 59),
            _clock=self._clock(datetime(2026, 7, 23, 23, 59)),
        )

        projection = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )

        self.assertEqual(projection.status, "MET")
        self.assertEqual(projection.accepted_target_count, 2)
        self.assertEqual(projection.accepted_by_deadline_count, 2)

    def test_occurrence_sla_uses_0800_not_earlier_item_deadline(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_schedule_items SET deadline_at = :deadline_at "
                    "WHERE item_id = :item_id"
                ),
                {
                    "deadline_at": datetime(2026, 7, 23, 23, 55),
                    "item_id": item_id,
                },
            )
            conn.execute(
                text(
                    "UPDATE t_input_generations "
                    "SET sealed_at = :sealed_at "
                    "WHERE generation_id = 'generation-20260724'"
                ),
                {"sealed_at": datetime(2026, 7, 23, 23, 5)},
            )
        attempt = self._function("start_schedule_attempt")(
            self.engine,
            item_id=item_id,
            started_at=datetime(2026, 7, 23, 23, 30),
            _clock=self._clock(datetime(2026, 7, 23, 23, 30)),
        )
        self._function("complete_scheduled_attempt")(
            self.engine,
            run_id=attempt.run_id,
            records=self._records(),
            trusted_verifier=self._trusted_completion_verifier(),
            completed_at=datetime(2026, 7, 23, 23, 59),
            _clock=self._clock(datetime(2026, 7, 23, 23, 59)),
        )

        projection = self._function(
            "evaluate_schedule_occurrence_target_sla"
        )(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=datetime(2026, 7, 24, 0, 5),
            _clock=self._clock(datetime(2026, 7, 24, 0, 5)),
        )

        self.assertEqual(projection.status, "MET")
        self.assertEqual(projection.accepted_target_count, 2)
        self.assertEqual(projection.accepted_by_deadline_count, 2)

    def test_health_envelope_observes_unbound_and_building_without_weakening_execution(
        self,
    ) -> None:
        _, item_id = self._create_occurrence(
            generation_status="BUILDING",
        )
        read_health = self._function("read_schedule_health_envelope")
        read_execution = self._function("read_schedule_execution_envelope")

        unbound = read_health(self.engine, item_id=item_id)

        self.assertIsNone(unbound.generation)
        self.assertIsNone(unbound.calendar_generation)
        self.assertEqual(
            unbound.generation_issue,
            "UNBOUND_INPUT_GENERATION",
        )
        self.assertEqual(len(unbound.targets), 2)
        with self.assertRaisesRegex(RuntimeError, "no bound input generation"):
            read_execution(self.engine, item_id=item_id)

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_schedule_items "
                    "SET input_generation_id = 'generation-20260724' "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )

        building = read_health(self.engine, item_id=item_id)

        self.assertEqual(building.generation.state, "BUILDING")
        self.assertEqual(
            building.calendar_generation.state,
            "BUILDING",
        )
        self.assertIsNone(building.generation_issue)
        with self.assertRaisesRegex(RuntimeError, "not SEALED"):
            read_execution(self.engine, item_id=item_id)

    def test_health_envelope_observes_invalidated_and_unsupported_items(
        self,
    ) -> None:
        _, invalidated_item_id = self._create_occurrence()
        self._function("invalidate_input_generation")(
            self.engine,
            generation_id="generation-20260724",
            reason="manifest revoked",
        )

        invalidated = self._function(
            "read_schedule_health_envelope"
        )(
            self.engine,
            item_id=invalidated_item_id,
        )

        self.assertEqual(invalidated.generation.state, "INVALIDATED")
        self.assertEqual(
            invalidated.calendar_generation.state,
            "INVALIDATED",
        )
        with self.assertRaisesRegex(RuntimeError, "not SEALED"):
            self._function("read_schedule_execution_envelope")(
                self.engine,
                item_id=invalidated_item_id,
            )

        other_engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            future=True,
        )
        try:
            with other_engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys = ON")
                for statement in _SQLITE_SCHEMA:
                    conn.exec_driver_sql(statement)
            original_engine = self.engine
            self.engine = other_engine
            _, unsupported_item_id = self._create_occurrence(
                generation_status="BUILDING",
            )
            self._function("fail_schedule_item_without_attempt")(
                self.engine,
                item_id=unsupported_item_id,
                failure_code="NATIVE_GENERATION_UNSUPPORTED",
            )
            unsupported = self._function(
                "read_schedule_health_envelope"
            )(
                self.engine,
                item_id=unsupported_item_id,
            )
        finally:
            self.engine = original_engine
            other_engine.dispose()

        self.assertEqual(unsupported.item.state, "FAILED_TERMINAL")
        self.assertEqual(
            unsupported.item.failure_code,
            "NATIVE_GENERATION_UNSUPPORTED",
        )
        self.assertEqual(
            unsupported.generation_issue,
            "UNBOUND_INPUT_GENERATION",
        )

    def test_dashboard_source_generation_changes_with_cross_process_receipts(
        self,
    ) -> None:
        occurrence_id, item_id = self._create_occurrence()
        read_generation = self._function(
            "read_dashboard_source_generation"
        )
        before = read_generation(
            self.engine,
            schedule_key="daily-production",
        )
        with self.engine.begin() as conn:
            target_id = conn.execute(
                text(
                    "SELECT target_id FROM t_schedule_item_targets "
                    "WHERE item_id = :item_id ORDER BY target_id LIMIT 1"
                ),
                {"item_id": item_id},
            ).scalar_one()
            conn.execute(
                text(
                    "UPDATE t_schedule_item_targets "
                    "SET status = 'ACCEPTED', "
                    "accepted_at = :accepted_at, "
                    "visible_at = :visible_at "
                    "WHERE target_id = :target_id"
                ),
                {
                    "accepted_at": datetime(2026, 7, 23, 23, 30),
                    "visible_at": datetime(2026, 7, 23, 23, 31),
                    "target_id": target_id,
                },
            )

        after = read_generation(
            self.engine,
            schedule_key="daily-production",
        )

        self.assertNotEqual(before, after)
        self.assertEqual(
            after,
            read_generation(
                self.engine,
                schedule_key="daily-production",
                occurrence_id=occurrence_id,
            ),
        )

    def test_api_visibility_probe_distinguishes_commit_link_and_db_receipt(
        self,
    ) -> None:
        (
            occurrence_id,
            item_id,
            run_id,
            _prediction_id,
            registry_id,
        ) = self._create_completed_single_target()
        with self.engine.begin() as conn:
            target = conn.execute(
                text(
                    "SELECT target_id, registry_scheme_id, target_tenor, "
                    "horizon, target_date "
                    "FROM t_schedule_item_targets "
                    "WHERE item_id = :item_id ORDER BY target_id LIMIT 1"
                ),
                {"item_id": item_id},
            ).mappings().one()
            conn.execute(
                text(
                    "UPDATE t_schedule_item_targets "
                    "SET visible_at = NULL "
                    "WHERE target_id = :target_id"
                ),
                {
                    "target_id": target["target_id"],
                },
            )
        read_probe = self._function(
            "read_schedule_api_visibility_probe"
        )

        missing_receipt = read_probe(
            self.engine,
            occurrence_id=occurrence_id,
            _clock=self._clock(datetime(2026, 7, 23, 23, 41)),
        )

        self.assertEqual(
            missing_receipt.committed_registry_ids,
            (registry_id,),
        )
        self.assertEqual(
            missing_receipt.linked_registry_ids,
            (registry_id,),
        )
        self.assertEqual(missing_receipt.db_visible_registry_ids, ())
        self.assertEqual(
            missing_receipt.receipt_missing_registry_ids,
            (registry_id,),
        )
        self.assertIn(
            registry_id,
            missing_receipt.missing_registry_ids,
        )

        self._function("record_schedule_attempt_visibility")(
            self.engine,
            run_id=run_id,
            _clock=self._clock(datetime(2026, 7, 23, 23, 42)),
        )

        visible = read_probe(
            self.engine,
            occurrence_id=occurrence_id,
            _clock=self._clock(datetime(2026, 7, 23, 23, 43)),
        )

        self.assertEqual(
            visible.db_visible_registry_ids,
            (registry_id,),
        )
        self.assertEqual(visible.receipt_missing_registry_ids, ())
        self.assertEqual(
            visible.observed_at,
            datetime(2026, 7, 23, 23, 43),
        )

    def _assert_no_predictions(self) -> None:
        with self.engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM t_scheme_predictions")
            ).scalar_one()
        self.assertEqual(count, 0)


_SQLITE_SCHEMA = (
    """
    CREATE TABLE t_scheme_registry (
        scheme_id TEXT PRIMARY KEY,
        base_scheme_id TEXT NOT NULL,
        runtime_type TEXT NOT NULL,
        status TEXT NOT NULL,
        frequency TEXT NOT NULL,
        task_type TEXT NOT NULL,
        target_tenor TEXT NOT NULL,
        horizon INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE t_scheme_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_id TEXT NOT NULL,
        scheme_version TEXT NOT NULL,
        runtime_type TEXT NOT NULL DEFAULT 'native_adapter',
        code_hash TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (scheme_id, scheme_version)
    )
    """,
    """
    CREATE TABLE t_input_generations (
        generation_id TEXT PRIMARY KEY,
        generation_type TEXT NOT NULL,
        business_date TEXT NOT NULL,
        feature_date TEXT NOT NULL,
        readiness_basis TEXT NOT NULL,
        source_commit_token TEXT NOT NULL,
        dataset_content_id TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        exporter_version TEXT NOT NULL,
        manifest_uri TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        native_generation_id TEXT,
        native_manifest_sha256 TEXT,
        state TEXT NOT NULL DEFAULT 'BUILDING',
        sealed_at DATETIME,
        invalidated_at DATETIME,
        invalid_reason TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE t_schedule_occurrences (
        occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_key TEXT NOT NULL,
        predict_date TEXT NOT NULL,
        feature_date TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL,
        policy_json TEXT NOT NULL,
        registry_digest TEXT NOT NULL,
        completion_state TEXT NOT NULL DEFAULT 'PENDING',
        expected_item_count INTEGER NOT NULL,
        expected_target_count INTEGER NOT NULL,
        accepted_target_count INTEGER NOT NULL DEFAULT 0,
        sla_accepted_target_count INTEGER,
        sla_deadline_at DATETIME NOT NULL,
        recovery_cutoff_at DATETIME NOT NULL,
        sla_outcome TEXT NOT NULL DEFAULT 'PENDING',
        sla_evaluated_at DATETIME,
        sla_reason TEXT,
        failure_code TEXT,
        failure_message TEXT,
        started_at DATETIME,
        completed_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (schedule_key, predict_date)
    )
    """,
    """
    CREATE TABLE t_schedule_items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id INTEGER NOT NULL,
        base_scheme_id TEXT NOT NULL,
        runtime_type TEXT NOT NULL,
        scheme_version TEXT NOT NULL,
        code_sha256 TEXT NOT NULL,
        config_sha256 TEXT NOT NULL,
        cache_group TEXT NOT NULL,
        input_generation_id TEXT,
        resource_class TEXT,
        internal_workers INTEGER,
        release_offset_minutes INTEGER NOT NULL DEFAULT 0,
        release_at DATETIME,
        deadline_at DATETIME,
        state TEXT NOT NULL DEFAULT 'PENDING',
        sla_status TEXT NOT NULL DEFAULT 'PENDING',
        late_reason TEXT,
        sla_evaluated_at DATETIME,
        attempt_no INTEGER NOT NULL DEFAULT 0,
        current_run_id INTEGER,
        started_at DATETIME,
        completed_at DATETIME,
        failure_code TEXT,
        failure_message TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (occurrence_id, base_scheme_id)
    )
    """,
    """
    CREATE TABLE t_schedule_item_targets (
        target_id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        registry_scheme_id TEXT NOT NULL,
        base_scheme_id TEXT NOT NULL,
        runtime_type TEXT NOT NULL,
        task_type TEXT NOT NULL,
        target_tenor TEXT NOT NULL,
        horizon INTEGER NOT NULL,
        target_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        accepted_run_id INTEGER,
        accepted_prediction_id INTEGER,
        accepted_at DATETIME,
        visible_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (occurrence_id, registry_scheme_id),
        UNIQUE (item_id, target_tenor, horizon)
    )
    """,
    """
    CREATE TABLE t_scheme_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_id TEXT NOT NULL,
        scheme_version TEXT,
        runtime_type TEXT NOT NULL DEFAULT 'native_adapter',
        run_type TEXT NOT NULL DEFAULT 'active',
        prediction_phase TEXT,
        predict_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        harness_run_id TEXT,
        input_artifact_id TEXT,
        data_snapshot_id TEXT,
        records_expected INTEGER,
        records_returned INTEGER,
        records_written INTEGER,
        error_message TEXT,
        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        finished_at DATETIME,
        schedule_item_id INTEGER,
        attempt_no INTEGER,
        trigger_origin TEXT,
        queued_at DATETIME,
        failure_code TEXT,
        execution_token TEXT UNIQUE,
        process_id INTEGER,
        process_group_id INTEGER,
        UNIQUE (schedule_item_id, attempt_no)
    )
    """,
    """
    CREATE TABLE t_scheme_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        scheme_version TEXT,
        scheme_id TEXT NOT NULL,
        target_tenor TEXT NOT NULL,
        horizon INTEGER NOT NULL,
        predict_date TEXT NOT NULL,
        feature_date TEXT NOT NULL,
        target_date TEXT NOT NULL,
        prediction_phase TEXT NOT NULL,
        predicted_direction INTEGER NOT NULL,
        confidence REAL,
        model_version TEXT,
        extra TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (scheme_id, target_tenor, horizon, target_date)
    )
    """,
    """
    CREATE TABLE t_scheduler_heartbeat (
        service_name TEXT PRIMARY KEY,
        process_id INTEGER NOT NULL,
        host_name TEXT NOT NULL,
        state TEXT NOT NULL,
        occurrence_id INTEGER,
        heartbeat_at DATETIME NOT NULL,
        details_json TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
)


if __name__ == "__main__":
    unittest.main()
