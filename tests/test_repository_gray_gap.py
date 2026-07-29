from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, event, text

import shared.liwei_0616_phase_a_cache as cache_module
from shared.daily_coordinator_mode import DailyCoordinatorEpochIdentity
from shared.liwei_0616_cache_projection import (
    AuxiliaryDependencyProjection,
)
from shared.liwei_0616_phase_a_cache import (
    PhaseACacheSpec,
    prepare_phase_a_caches,
)
from shared.models import PredictionRecord


TENORS_T5 = ("3Y", "5Y", "7Y", "10Y")
TENORS_T1 = ("5Y", "10Y")
PREDICT_DATE = "2026-06-02"
FEATURE_DATE = "2026-06-01"
TARGET_DATE_T5 = "2026-06-08"
TARGET_DATE_T1 = "2026-06-02"
PLAN_SHA256 = "a" * 64
DIRECT_CACHE_SPEC = "d" * 64
DIRECT_CACHE_MANIFEST = "e" * 64
DIRECT_CACHE_PROJECTION = "f" * 64


def _cfg(
    scheme_id: str,
    *,
    horizon: int,
    task_type: str,
    tenors: tuple[str, ...],
    runtime_type: str = "native_adapter",
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=f"{scheme_id}-version",
        runtime_type=runtime_type,
        status="active",
        version_status="active",
        frequency="daily",
        horizon=horizon,
        task_type=task_type,
        tenors=list(tenors),
    )


def _target_keys(
    cfg: SimpleNamespace,
    *,
    target_date: str,
) -> list[dict[str, object]]:
    return [
        {
            "registry_scheme_id":
                f"{cfg.scheme_id}__h{cfg.horizon}__{tenor}",
            "base_scheme_id": cfg.scheme_id,
            "target_tenor": tenor,
            "horizon": cfg.horizon,
            "task_type": cfg.task_type,
            "predict_date": PREDICT_DATE,
            "feature_date": FEATURE_DATE,
            "target_date": target_date,
            "prediction_phase": "gray_live",
        }
        for tenor in cfg.tenors
    ]


def _records(
    cfg: SimpleNamespace,
    *,
    target_date: str,
) -> list[PredictionRecord]:
    return [
        PredictionRecord(
            scheme_id=cfg.scheme_id,
            target_tenor=tenor,
            horizon=cfg.horizon,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=target_date,
            prediction_phase="gray_live",
            predicted_direction=1,
            confidence=0.6,
            scheme_version=cfg.scheme_version,
            extra={"model_evidence": tenor},
        )
        for tenor in cfg.tenors
    ]


def _native_authority() -> dict[str, str]:
    return {
        "authority_type": "native_current_snapshot_artifact",
        "artifact_id": "native-0123456789abcdef01234567",
        "manifest_sha256": "b" * 64,
        "feature_date": FEATURE_DATE,
        "cutoff_date": FEATURE_DATE,
        "vintage_disclaimer":
            "current_snapshot_as_of_not_historical_vintage",
    }


def _databridge_authority() -> dict[str, str]:
    return {
        "authority_type": "databridge_current_generation",
        "generation_id": "full-20260730-083000-deadbeef",
        "manifest_sha256": "c" * 64,
        "refresh_date": "2026-07-30",
        "cutoff_date": FEATURE_DATE,
        "replay_mode": "historical_as_of_replay",
        "vintage_disclaimer":
            "current_snapshot_as_of_not_historical_vintage",
    }


class GrayGapRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "gray-gap.sqlite"
        self.engine = create_engine(
            f"sqlite+pysqlite:///{db_path}",
            future=True,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            dbapi_connection.execute("PRAGMA busy_timeout = 2000")

        with self.engine.begin() as conn:
            for statement in _SCHEMA:
                conn.exec_driver_sql(statement)
        self._epoch_identity = DailyCoordinatorEpochIdentity(
            epoch=1,
            mode="ledger",
            previous_record_sha256="0" * 64,
            record_sha256="1" * 64,
            source="gray-gap-test",
            transition_id="gray-gap-test-epoch",
        )
        self._identity_patcher = patch(
            "shared.daily_coordinator_mode."
            "require_current_daily_coordinator_identity",
            return_value=self._epoch_identity,
        )
        self._identity_patcher.start()

    def tearDown(self) -> None:
        self._identity_patcher.stop()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _function(self):
        from scheduler import repository

        function = getattr(repository, "complete_gray_gap_run", None)
        self.assertIsNotNone(
            function,
            "scheduler.repository.complete_gray_gap_run is missing",
        )
        return function

    def _seed(
        self,
        cfg: SimpleNamespace,
        *,
        run_id: int,
        status: str = "running",
        target_date: str,
        **run_overrides: object,
    ) -> None:
        run = {
            "run_id": run_id,
            "scheme_id": cfg.scheme_id,
            "scheme_version": cfg.scheme_version,
            "runtime_type": cfg.runtime_type,
            "run_type": "active",
            "prediction_phase": "gray_live",
            "predict_date": PREDICT_DATE,
            "status": status,
            "records_expected": len(cfg.tenors),
            "schedule_item_id": None,
            "attempt_no": None,
            "trigger_origin": None,
            "execution_token": None,
        }
        run.update(run_overrides)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_runs
                        (run_id, scheme_id, scheme_version, runtime_type,
                         run_type, prediction_phase, predict_date, status,
                         records_expected, schedule_item_id, attempt_no,
                         trigger_origin, execution_token)
                    VALUES
                        (:run_id, :scheme_id, :scheme_version, :runtime_type,
                         :run_type, :prediction_phase, :predict_date, :status,
                         :records_expected, :schedule_item_id, :attempt_no,
                         :trigger_origin, :execution_token)
                    """
                ),
                run,
            )
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO t_scheme_registry
                        (scheme_id, base_scheme_id, runtime_type, frequency,
                         task_type, target_tenor, horizon, status)
                    VALUES
                        (:scheme_id, :base_scheme_id, :runtime_type,
                         :frequency, :task_type, :target_tenor, :horizon,
                         'active')
                    """
                ),
                [
                    {
                        "scheme_id":
                            f"{cfg.scheme_id}__h{cfg.horizon}__{tenor}",
                        "base_scheme_id": cfg.scheme_id,
                        "runtime_type": cfg.runtime_type,
                        "frequency": cfg.frequency,
                        "task_type": cfg.task_type,
                        "target_tenor": tenor,
                        "horizon": cfg.horizon,
                    }
                    for tenor in cfg.tenors
                ],
            )

    def _complete(
        self,
        cfg: SimpleNamespace,
        *,
        run_id: int,
        target_date: str,
        records: list[PredictionRecord] | None = None,
        target_keys: list[dict[str, object]] | None = None,
        source_authority: dict[str, str] | None = None,
        run_date: str = PREDICT_DATE,
    ) -> int:
        record_list = (
            _records(cfg, target_date=target_date)
            if records is None
            else records
        )
        return self._function()(
            self.engine,
            cfg,
            run_id=run_id,
            records=record_list,
            expected_target_keys=(
                _target_keys(cfg, target_date=target_date)
                if target_keys is None
                else target_keys
            ),
            plan_sha256=PLAN_SHA256,
            source_authority=source_authority or _native_authority(),
            records_returned=len(record_list),
            run_date=run_date,
            duration_sec=1.25,
        )

    def _rows(self, table: str) -> list[dict[str, object]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(f"SELECT * FROM {table} ORDER BY 1")
                ).mappings().all()
            ]

    def _seed_ledger_target(
        self,
        *,
        base_scheme_id: str,
        target_tenor: str,
        horizon: int,
        target_date: str,
        task_type: str = "T+1",
    ) -> tuple[int, int, int]:
        with self.engine.begin() as conn:
            occurrence = conn.execute(
                text(
                    """
                    INSERT INTO t_schedule_occurrences
                        (schedule_key, predict_date, feature_date,
                         policy_version, policy_sha256, policy_json,
                         registry_digest, completion_state,
                         expected_item_count, expected_target_count,
                         accepted_target_count, sla_deadline_at,
                         recovery_cutoff_at)
                    VALUES
                        (:schedule_key, :predict_date, :feature_date,
                         'daily-schedule-policy-v1', :policy_sha256,
                         :policy_json, :registry_digest, 'RUNNING',
                         1, 1, 0, '2026-06-03 08:00:00',
                         '2026-06-03 12:00:00')
                    """
                ),
                {
                    "schedule_key":
                        f"gray-gap-owner-{base_scheme_id}-"
                        f"{target_tenor}-{target_date}",
                    "predict_date": PREDICT_DATE,
                    "feature_date": FEATURE_DATE,
                    "policy_sha256": "2" * 64,
                    "policy_json": json.dumps(
                        {
                            "daily_coordinator_epoch":
                                self._epoch_identity.policy_payload(),
                        },
                        sort_keys=True,
                    ),
                    "registry_digest": "3" * 64,
                },
            )
            occurrence_id = int(occurrence.lastrowid)
            item = conn.execute(
                text(
                    """
                    INSERT INTO t_schedule_items
                        (occurrence_id, base_scheme_id, runtime_type,
                         scheme_version, code_sha256, config_sha256,
                         cache_group, state, sla_status)
                    VALUES
                        (:occurrence_id, :base_scheme_id, 'native_adapter',
                         :scheme_version, :code_sha256, :config_sha256,
                         :cache_group, 'RUNNING', 'PENDING')
                    """
                ),
                {
                    "occurrence_id": occurrence_id,
                    "base_scheme_id": base_scheme_id,
                    "scheme_version": f"{base_scheme_id}-version",
                    "code_sha256": "4" * 64,
                    "config_sha256": "5" * 64,
                    "cache_group": f"{base_scheme_id}:daily",
                },
            )
            item_id = int(item.lastrowid)
            target = conn.execute(
                text(
                    """
                    INSERT INTO t_schedule_item_targets
                        (occurrence_id, item_id, registry_scheme_id,
                         base_scheme_id, runtime_type, task_type,
                         target_tenor, horizon, target_date, status)
                    VALUES
                        (:occurrence_id, :item_id, :registry_scheme_id,
                         :base_scheme_id, 'native_adapter', :task_type,
                         :target_tenor, :horizon, :target_date, 'PENDING')
                    """
                ),
                {
                    "occurrence_id": occurrence_id,
                    "item_id": item_id,
                    "registry_scheme_id":
                        f"{base_scheme_id}__h{horizon}__{target_tenor}",
                    "base_scheme_id": base_scheme_id,
                    "task_type": task_type,
                    "target_tenor": target_tenor,
                    "horizon": horizon,
                    "target_date": target_date,
                },
            )
            return occurrence_id, item_id, int(target.lastrowid)

    def test_t5_four_of_four_is_atomic_and_enriched(self) -> None:
        cfg = _cfg(
            "t5_daily",
            horizon=5,
            task_type="T+5",
            tenors=TENORS_T5,
        )
        self._seed(cfg, run_id=101, target_date=TARGET_DATE_T5)

        written = self._complete(
            cfg,
            run_id=101,
            target_date=TARGET_DATE_T5,
        )

        self.assertEqual(written, 4)
        predictions = self._rows("t_scheme_predictions")
        self.assertEqual(len(predictions), 4)
        run = self._rows("t_scheme_runs")[0]
        self.assertEqual(
            (run["status"], run["records_returned"], run["records_written"]),
            ("success", 4, 4),
        )
        logs = self._rows("t_scheme_run_log")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["run_id"], 101)
        for row in predictions:
            extra = json.loads(str(row["extra"]))
            self.assertEqual(extra["signal_gap_plan_sha256"], PLAN_SHA256)
            self.assertEqual(extra["backfill_mode"], "signal_gap_fill")
            self.assertTrue(str(extra["backfilled_at"]).endswith("+00:00"))
            self.assertEqual(extra["source_authority"], _native_authority())
            self.assertEqual(
                extra["source_artifact_id"],
                _native_authority()["artifact_id"],
            )
            self.assertEqual(
                extra["source_artifact_manifest_sha256"],
                _native_authority()["manifest_sha256"],
            )
            self.assertEqual(
                extra["replay_semantics"],
                "current_snapshot_as_of_not_historical_vintage",
            )
            group = extra["execution_group_identity"]
            self.assertEqual(group["base_scheme_id"], "t5_daily")
            self.assertEqual(group["record_count"], 4)
            self.assertEqual(len(group["targets"]), 4)
            self.assertRegex(group["identity_sha256"], r"^[0-9a-f]{64}$")

    def test_three_of_four_writes_nothing(self) -> None:
        cfg = _cfg(
            "t5_daily",
            horizon=5,
            task_type="T+5",
            tenors=TENORS_T5,
        )
        self._seed(cfg, run_id=102, target_date=TARGET_DATE_T5)

        with self.assertRaisesRegex(RuntimeError, "target multiset"):
            self._complete(
                cfg,
                run_id=102,
                target_date=TARGET_DATE_T5,
                records=_records(cfg, target_date=TARGET_DATE_T5)[:3],
            )

        self.assertEqual(self._rows("t_scheme_predictions"), [])
        self.assertEqual(self._rows("t_scheme_run_log"), [])
        self.assertEqual(self._rows("t_scheme_runs")[0]["status"], "running")

    def test_blackbox_two_of_two_databridge_authority(self) -> None:
        cfg = _cfg(
            "demo_blackbox",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
            runtime_type="blackbox_v2",
        )
        self._seed(cfg, run_id=103, target_date=TARGET_DATE_T1)

        written = self._complete(
            cfg,
            run_id=103,
            target_date=TARGET_DATE_T1,
            source_authority=_databridge_authority(),
        )

        self.assertEqual(written, 2)
        for row in self._rows("t_scheme_predictions"):
            extra = json.loads(str(row["extra"]))
            self.assertEqual(
                extra["source_generation_id"],
                _databridge_authority()["generation_id"],
            )
            self.assertEqual(
                extra["source_generation_manifest_sha256"],
                _databridge_authority()["manifest_sha256"],
            )
            self.assertEqual(
                extra["source_refresh_date"],
                _databridge_authority()["refresh_date"],
            )
            self.assertEqual(extra["source_cutoff_date"], FEATURE_DATE)

    def test_t1_two_of_two_native_authority(self) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        self._seed(cfg, run_id=113, target_date=TARGET_DATE_T1)

        self.assertEqual(
            self._complete(
                cfg,
                run_id=113,
                target_date=TARGET_DATE_T1,
            ),
            2,
        )

    def test_authority_type_must_match_runtime_type(self) -> None:
        cases = (
            (
                _cfg(
                    "native_trial",
                    horizon=1,
                    task_type="T+1",
                    tenors=("5Y",),
                ),
                _databridge_authority(),
            ),
            (
                _cfg(
                    "blackbox_trial",
                    horizon=1,
                    task_type="T+1",
                    tenors=("5Y",),
                    runtime_type="blackbox_v2",
                ),
                _native_authority(),
            ),
        )
        for run_id, (cfg, authority) in enumerate(cases, start=114):
            with self.subTest(runtime_type=cfg.runtime_type):
                self._seed(
                    cfg,
                    run_id=run_id,
                    target_date=TARGET_DATE_T1,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "authority_type.*runtime_type",
                ):
                    self._complete(
                        cfg,
                        run_id=run_id,
                        target_date=TARGET_DATE_T1,
                        source_authority=authority,
                    )
        self.assertEqual(self._rows("t_scheme_predictions"), [])
        self.assertEqual(self._rows("t_scheme_run_log"), [])

    def test_builtin_multi_target_groups_cannot_be_narrowed_by_cfg(self) -> None:
        cases = (
            _cfg(
                "t5_daily",
                horizon=5,
                task_type="T+5",
                tenors=("3Y", "5Y"),
            ),
            _cfg(
                "t1_daily",
                horizon=1,
                task_type="T+1",
                tenors=("5Y",),
            ),
        )
        for run_id, cfg in enumerate(cases, start=116):
            target_date = (
                TARGET_DATE_T5 if cfg.scheme_id == "t5_daily"
                else TARGET_DATE_T1
            )
            with self.subTest(scheme_id=cfg.scheme_id):
                self._seed(
                    cfg,
                    run_id=run_id,
                    target_date=target_date,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "fixed atomic target multiset",
                ):
                    self._complete(
                        cfg,
                        run_id=run_id,
                        target_date=target_date,
                    )
        self.assertEqual(self._rows("t_scheme_predictions"), [])
        self.assertEqual(self._rows("t_scheme_run_log"), [])

    def test_run_date_must_equal_execution_predict_date(self) -> None:
        cfg = _cfg(
            "native_trial",
            horizon=1,
            task_type="T+1",
            tenors=("5Y",),
        )
        self._seed(cfg, run_id=118, target_date=TARGET_DATE_T1)

        with self.assertRaisesRegex(
            ValueError,
            "run_date.*predict_date",
        ):
            self._complete(
                cfg,
                run_id=118,
                target_date=TARGET_DATE_T1,
                run_date="2026-06-03",
            )

        self.assertEqual(self._rows("t_scheme_predictions"), [])
        self.assertEqual(self._rows("t_scheme_run_log"), [])
        self.assertEqual(self._rows("t_scheme_runs")[0]["status"], "running")

    def test_duplicate_extra_and_wrong_dates_write_nothing(self) -> None:
        cases: list[tuple[str, list[PredictionRecord]]] = []
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        base = _records(cfg, target_date=TARGET_DATE_T1)
        cases.append(("duplicate", [base[0], base[0]]))
        cases.append(("extra", [*base, base[0]]))
        cases.append(
            (
                "wrong predict_date",
                [
                    base[0],
                    PredictionRecord(
                        **{
                            **base[1].__dict__,
                            "predict_date": "2026-06-03",
                        }
                    ),
                ],
            )
        )
        cases.append(
            (
                "wrong feature_date",
                [
                    base[0],
                    PredictionRecord(
                        **{
                            **base[1].__dict__,
                            "feature_date": "2026-05-29",
                        }
                    ),
                ],
            )
        )
        cases.append(
            (
                "wrong target_date",
                [
                    base[0],
                    PredictionRecord(
                        **{
                            **base[1].__dict__,
                            "target_date": "2026-06-03",
                        }
                    ),
                ],
            )
        )
        for index, (label, invalid_records) in enumerate(cases, start=200):
            with self.subTest(label=label):
                self._seed(
                    cfg,
                    run_id=index,
                    target_date=TARGET_DATE_T1,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "target multiset|identity mismatch",
                ):
                    self._complete(
                        cfg,
                        run_id=index,
                        target_date=TARGET_DATE_T1,
                        records=invalid_records,
                    )
                self.assertEqual(
                    [
                        row
                        for row in self._rows("t_scheme_predictions")
                        if row["run_id"] == index
                    ],
                    [],
                )
                self.assertEqual(
                    next(
                        row
                        for row in self._rows("t_scheme_runs")
                        if row["run_id"] == index
                    )["status"],
                    "running",
                )

    def test_third_prediction_failure_rolls_back_entire_group(self) -> None:
        cfg = _cfg(
            "t5_daily",
            horizon=5,
            task_type="T+5",
            tenors=TENORS_T5,
        )
        self._seed(cfg, run_id=104, target_date=TARGET_DATE_T5)
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TRIGGER fail_third_prediction
                BEFORE INSERT ON t_scheme_predictions
                WHEN NEW.target_tenor = '7Y'
                BEGIN
                    SELECT RAISE(ABORT, 'injected third prediction failure');
                END
                """
            )

        with self.assertRaisesRegex(Exception, "third prediction failure"):
            self._complete(
                cfg,
                run_id=104,
                target_date=TARGET_DATE_T5,
            )

        self.assertEqual(self._rows("t_scheme_predictions"), [])
        self.assertEqual(self._rows("t_scheme_run_log"), [])
        self.assertEqual(self._rows("t_scheme_runs")[0]["status"], "running")

    def test_finish_or_log_failure_rolls_back_predictions(self) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        for run_id, failure_target, ddl in (
            (
                105,
                "finish",
                """
                CREATE TRIGGER fail_run_finish
                BEFORE UPDATE OF status ON t_scheme_runs
                WHEN NEW.run_id = 105 AND NEW.status = 'success'
                BEGIN
                    SELECT RAISE(ABORT, 'injected finish failure');
                END
                """,
            ),
            (
                106,
                "log",
                """
                CREATE TRIGGER fail_run_log
                BEFORE INSERT ON t_scheme_run_log
                WHEN NEW.run_id = 106
                BEGIN
                    SELECT RAISE(ABORT, 'injected log failure');
                END
                """,
            ),
        ):
            with self.subTest(failure_target=failure_target):
                self._seed(
                    cfg,
                    run_id=run_id,
                    target_date=TARGET_DATE_T1,
                )
                with self.engine.begin() as conn:
                    conn.exec_driver_sql(ddl)
                with self.assertRaisesRegex(
                    Exception,
                    f"injected {failure_target} failure",
                ):
                    self._complete(
                        cfg,
                        run_id=run_id,
                        target_date=TARGET_DATE_T1,
                    )
                self.assertEqual(
                    [
                        row
                        for row in self._rows("t_scheme_predictions")
                        if row["run_id"] == run_id
                    ],
                    [],
                )
                self.assertEqual(
                    next(
                        row
                        for row in self._rows("t_scheme_runs")
                        if row["run_id"] == run_id
                    )["status"],
                    "running",
                )

    def test_existing_business_key_is_not_overwritten(self) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        self._seed(cfg, run_id=107, target_date=TARGET_DATE_T1)
        existing = _records(cfg, target_date=TARGET_DATE_T1)[0]
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_version, scheme_id, target_tenor,
                         horizon, predict_date, feature_date, target_date,
                         prediction_phase, predicted_direction, confidence,
                         model_version, extra)
                    VALUES
                        (900, :scheme_version, :scheme_id, :target_tenor,
                         :horizon, :predict_date, :feature_date, :target_date,
                         'gray_live', -1, 0.9, NULL, '{"original": true}')
                    """
                ),
                existing.__dict__,
            )

        with self.assertRaisesRegex(RuntimeError, "business key already exists"):
            self._complete(
                cfg,
                run_id=107,
                target_date=TARGET_DATE_T1,
            )

        predictions = self._rows("t_scheme_predictions")
        self.assertEqual(len(predictions), 1)
        self.assertEqual(predictions[0]["run_id"], 900)
        self.assertEqual(predictions[0]["predicted_direction"], -1)
        self.assertEqual(self._rows("t_scheme_runs")[0]["status"], "running")
        self.assertEqual(self._rows("t_scheme_run_log"), [])

    def test_migration_018_frozen_target_rejects_whole_gray_group(
        self,
    ) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        self._seed(cfg, run_id=119, target_date=TARGET_DATE_T1)
        _occurrence_id, _item_id, target_id = self._seed_ledger_target(
            base_scheme_id=cfg.scheme_id,
            target_tenor="5Y",
            horizon=cfg.horizon,
            target_date=TARGET_DATE_T1,
        )
        statements: list[str] = []

        def capture_sql(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(" ".join(statement.lower().split()))

        event.listen(self.engine, "before_cursor_execute", capture_sql)
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "frozen daily ledger target",
            ):
                self._complete(
                    cfg,
                    run_id=119,
                    target_date=TARGET_DATE_T1,
                )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                capture_sql,
            )

        self.assertEqual(self._rows("t_scheme_predictions"), [])
        self.assertEqual(self._rows("t_scheme_run_log"), [])
        self.assertEqual(self._rows("t_scheme_runs")[0]["status"], "running")
        target = self._rows("t_schedule_item_targets")[0]
        self.assertEqual(target["target_id"], target_id)
        self.assertEqual(target["status"], "PENDING")
        self.assertIsNone(target["accepted_run_id"])
        occurrence_index = next(
            index
            for index, statement in enumerate(statements)
            if (
                "from t_schedule_occurrences" in statement
                and "where occurrence_id" in statement
            )
        )
        item_index = next(
            index
            for index, statement in enumerate(statements)
            if (
                index > occurrence_index
                and "from t_schedule_items" in statement
                and "where occurrence_id" in statement
            )
        )
        target_index = next(
            index
            for index, statement in enumerate(statements)
            if (
                index > item_index
                and "from t_schedule_item_targets" in statement
                and "where item_id" in statement
            )
        )
        self.assertLess(occurrence_index, item_index)
        self.assertLess(item_index, target_index)
        self.assertFalse(
            any(
                "from t_scheme_runs" in statement
                and "where run_id" in statement
                for statement in statements
            ),
            "gray run must not lock before the ledger guard",
        )

    def test_scheduled_ledger_owner_wins_gray_gap_competition(self) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        self._seed(cfg, run_id=120, target_date=TARGET_DATE_T1)
        _occurrence_id, _item_id, target_id = self._seed_ledger_target(
            base_scheme_id=cfg.scheme_id,
            target_tenor="5Y",
            horizon=cfg.horizon,
            target_date=TARGET_DATE_T1,
        )
        barrier = threading.Barrier(2)

        def scheduled_owner() -> tuple[str, object]:
            barrier.wait()
            try:
                with self.engine.begin() as conn:
                    result = conn.execute(
                        text(
                            """
                            INSERT INTO t_scheme_predictions
                                (run_id, scheme_version, scheme_id,
                                 target_tenor, horizon, predict_date,
                                 feature_date, target_date,
                                 prediction_phase, predicted_direction,
                                 confidence, model_version, extra)
                            VALUES
                                (900, :scheme_version, :scheme_id, '5Y', 1,
                                 :predict_date, :feature_date, :target_date,
                                 'scheduled_live', -1, 0.9, NULL,
                                 '{"scheduled_owner": true}')
                            """
                        ),
                        {
                            "scheme_version": cfg.scheme_version,
                            "scheme_id": cfg.scheme_id,
                            "predict_date": PREDICT_DATE,
                            "feature_date": FEATURE_DATE,
                            "target_date": TARGET_DATE_T1,
                        },
                    )
                    prediction_id = int(result.lastrowid)
                    conn.execute(
                        text(
                            """
                            UPDATE t_schedule_item_targets
                            SET status = 'ACCEPTED',
                                accepted_run_id = 900,
                                accepted_prediction_id = :prediction_id
                            WHERE target_id = :target_id
                            """
                        ),
                        {
                            "prediction_id": prediction_id,
                            "target_id": target_id,
                        },
                    )
                return "success", prediction_id
            except Exception as exc:  # noqa: BLE001 - concurrency evidence
                return "failed", exc

        def gray_gap() -> tuple[str, object]:
            barrier.wait()
            try:
                return (
                    "success",
                    self._complete(
                        cfg,
                        run_id=120,
                        target_date=TARGET_DATE_T1,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - expected loser
                return "failed", exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            scheduled_result = pool.submit(scheduled_owner)
            gray_result = pool.submit(gray_gap)
            scheduled_status, scheduled_value = scheduled_result.result()
            gray_status, gray_value = gray_result.result()

        self.assertEqual(
            (scheduled_status, gray_status),
            ("success", "failed"),
        )
        self.assertIsInstance(gray_value, RuntimeError)
        self.assertIn("frozen daily ledger target", str(gray_value))
        predictions = self._rows("t_scheme_predictions")
        self.assertEqual(len(predictions), 1)
        self.assertEqual(predictions[0]["run_id"], 900)
        self.assertEqual(
            predictions[0]["id"],
            scheduled_value,
        )
        target = self._rows("t_schedule_item_targets")[0]
        self.assertEqual(target["accepted_run_id"], 900)
        self.assertEqual(
            target["accepted_prediction_id"],
            predictions[0]["id"],
        )
        self.assertEqual(self._rows("t_scheme_run_log"), [])
        self.assertEqual(self._rows("t_scheme_runs")[0]["status"], "running")

    def test_competing_business_key_allows_exactly_one_group(self) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        self._seed(cfg, run_id=108, target_date=TARGET_DATE_T1)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_runs
                        (run_id, scheme_id, scheme_version, runtime_type,
                         run_type, prediction_phase, predict_date, status,
                         records_expected)
                    SELECT 109, scheme_id, scheme_version, runtime_type,
                           run_type, prediction_phase, predict_date, status,
                           records_expected
                    FROM t_scheme_runs WHERE run_id = 108
                    """
                )
            )
        barrier = threading.Barrier(2)

        def compete(run_id: int) -> tuple[str, object]:
            barrier.wait()
            try:
                return (
                    "success",
                    self._complete(
                        cfg,
                        run_id=run_id,
                        target_date=TARGET_DATE_T1,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - precise loser varies by DB
                return ("failed", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, (108, 109)))

        self.assertEqual(
            [status for status, _ in results].count("success"),
            1,
        )
        self.assertEqual(len(self._rows("t_scheme_predictions")), 2)
        self.assertEqual(len(self._rows("t_scheme_run_log")), 1)
        self.assertEqual(
            [row["status"] for row in self._rows("t_scheme_runs")].count(
                "success"
            ),
            1,
        )

    def test_rejects_nonordinary_run_and_preserves_historical_failure(self) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        self._seed(
            cfg,
            run_id=110,
            target_date=TARGET_DATE_T1,
            schedule_item_id=88,
            attempt_no=1,
            trigger_origin="apscheduler",
            execution_token="token",
        )
        self._seed(
            cfg,
            run_id=111,
            target_date=TARGET_DATE_T1,
            status="failed",
        )

        for run_id in (110, 111):
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "ordinary gray_live running run",
                ):
                    self._complete(
                        cfg,
                        run_id=run_id,
                        target_date=TARGET_DATE_T1,
                    )
        runs = {row["run_id"]: row for row in self._rows("t_scheme_runs")}
        self.assertEqual(runs[110]["status"], "running")
        self.assertEqual(runs[111]["status"], "failed")
        self.assertEqual(self._rows("t_scheme_predictions"), [])
        self.assertEqual(self._rows("t_scheme_run_log"), [])

    def test_registry_drift_authority_errors_and_ledger_tables_are_untouched(
        self,
    ) -> None:
        cfg = _cfg(
            "t1_daily",
            horizon=1,
            task_type="T+1",
            tenors=TENORS_T1,
        )
        self._seed(cfg, run_id=112, target_date=TARGET_DATE_T1)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE t_scheme_registry
                    SET status = 'paused'
                    WHERE target_tenor = '10Y'
                    """
                )
            )
        self._seed_ledger_target(
            base_scheme_id="unrelated",
            target_tenor="3Y",
            horizon=1,
            target_date=TARGET_DATE_T1,
        )
        ledger_before = {
            table: self._rows(table)
            for table in (
                "t_schedule_occurrences",
                "t_schedule_items",
                "t_schedule_item_targets",
            )
        }

        with self.assertRaisesRegex(RuntimeError, "active Registry target multiset"):
            self._complete(
                cfg,
                run_id=112,
                target_date=TARGET_DATE_T1,
            )
        for table, rows in ledger_before.items():
            self.assertEqual(self._rows(table), rows)

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE t_scheme_registry SET status = 'active'"
                )
            )
        bad_authority = _native_authority()
        bad_authority["manifest_sha256"] = "B" * 64
        with self.assertRaisesRegex(ValueError, "source_authority"):
            self._complete(
                cfg,
                run_id=112,
                target_date=TARGET_DATE_T1,
                source_authority=bad_authority,
            )
        self.assertEqual(self._rows("t_scheme_predictions"), [])

    def test_direct_cache_authority_reopens_without_legacy_qualification(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture()
        loaded = SimpleNamespace(
            generation_id=fixture["generation_id"],
            path=Path(fixture["audit"]["generation_path"]),
            manifest=fixture["manifest"],
            manifest_sha256=DIRECT_CACHE_MANIFEST,
            caches={
                "STD": {
                    "test_dates": ["2026-05-29", "2026-06-01"],
                }
            },
        )
        record = PredictionRecord(
            scheme_id=fixture["scheme_id"],
            target_tenor="5Y",
            horizon=5,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE_T5,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            extra={"phase_a_cache": fixture["audit"]},
        )

        with (
            patch.object(
                repository,
                "validate_trusted_cache_use_qualification",
                side_effect=AssertionError(
                    "legacy qualification must not be used"
                ),
            ),
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_load_generation_directory",
                return_value=loaded,
            ) as loader,
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_verify_generation_acceptance_lineage",
            ) as lineage,
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_validate_generation_acceptance_for_use",
            ) as acceptance,
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[record],
            )

        loader.assert_called_once_with(
            Path(fixture["audit"]["generation_path"]),
            expected_generation_id=fixture["generation_id"],
            expected_manifest_sha256=DIRECT_CACHE_MANIFEST,
            secure=True,
        )
        lineage.assert_called_once()
        qualification = lineage.call_args.kwargs[
            "trusted_qualification"
        ]["qualification"]
        self.assertEqual(
            {
                key: qualification[key]
                for key in (
                    "cache_abi_version",
                    "cache_family",
                    "tenor",
                    "spec_fingerprint",
                )
            },
            {
                "cache_abi_version": "liwei_0616.phase_a.v1",
                "cache_family": "liwei_test_family",
                "tenor": "5Y",
                "spec_fingerprint": DIRECT_CACHE_SPEC,
            },
        )
        self.assertEqual(
            qualification["daily_dependency_lookback_rows"],
            21,
        )
        self.assertEqual(
            qualification["daily_dependency_proof"],
            "daily_window_is_bounded_by_21_rows",
        )
        acceptance.assert_called_once_with(
            loaded,
            native_generation_binding=fixture["native_binding"],
        )

    def test_direct_cache_completion_is_single_record_by_contract(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture()
        record = PredictionRecord(
            scheme_id=fixture["scheme_id"],
            target_tenor="5Y",
            horizon=5,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE_T5,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            extra={"phase_a_cache": fixture["audit"]},
        )

        with (
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_load_generation_directory",
                side_effect=AssertionError(
                    "multi-record cache completion must not perform I/O"
                ),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "exactly one record",
            ),
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[record, record],
            )

    def test_direct_cache_real_generation_accepts_and_tampering_fails(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._real_direct_cache_fixture("positive")
        repository._validate_cache_qualified_completion(
            occurrence={"policy_json": fixture["policy"]},
            item=fixture["item"],
            generation=fixture["generation"],
            records=[fixture["record"]],
        )

        for tamper_target in ("manifest", "baseline"):
            with self.subTest(tamper_target=tamper_target):
                tampered = self._real_direct_cache_fixture(
                    f"tamper-{tamper_target}"
                )
                generation_path = Path(
                    tampered["audit"]["generation_path"]
                )
                path = (
                    generation_path / "manifest.json"
                    if tamper_target == "manifest"
                    else generation_path / "baselines" / "STD.pkl"
                )
                path.write_bytes(path.read_bytes() + b"\nforged")

                with self.assertRaisesRegex(
                    RuntimeError,
                    "direct cache completion audit verification failed",
                ):
                    repository._validate_cache_qualified_completion(
                        occurrence={
                            "policy_json": tampered["policy"]
                        },
                        item=tampered["item"],
                        generation=tampered["generation"],
                        records=[tampered["record"]],
                    )

    def test_direct_cache_read_only_requires_validated_published_hit(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture(read_only=True)
        record = PredictionRecord(
            scheme_id=fixture["scheme_id"],
            target_tenor="5Y",
            horizon=5,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE_T5,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            extra={
                "phase_a_cache": {
                    **fixture["audit"],
                    "status": "cold_build",
                    "build_mode": "full",
                    "build_reason": "no_current_generation",
                }
            },
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "read_only.*consumer_validated_hit",
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[record],
            )

    def test_direct_cache_real_hit_audits_close_to_reopened_generation(
        self,
    ) -> None:
        from scheduler import repository

        for read_only, build_reason in (
            (False, "cache_complete"),
            (True, "consumer_validated_hit"),
        ):
            with self.subTest(read_only=read_only):
                fixture = self._direct_cache_fixture(
                    read_only=read_only
                )
                fixture["audit"].update(
                    {
                        "status": "hit",
                        "build_mode": "hit",
                        "build_reason": build_reason,
                    }
                )
                loaded = SimpleNamespace(
                    generation_id=fixture["generation_id"],
                    path=Path(fixture["audit"]["generation_path"]),
                    manifest=fixture["manifest"],
                    manifest_sha256=DIRECT_CACHE_MANIFEST,
                    caches={"STD": {"test_dates": ["2026-06-01"]}},
                )
                record = PredictionRecord(
                    scheme_id=fixture["scheme_id"],
                    target_tenor="5Y",
                    horizon=5,
                    predict_date=PREDICT_DATE,
                    feature_date=FEATURE_DATE,
                    target_date=TARGET_DATE_T5,
                    predicted_direction=1,
                    prediction_phase="scheduled_live",
                    extra={"phase_a_cache": fixture["audit"]},
                )

                with (
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_load_generation_directory",
                        return_value=loaded,
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_verify_generation_acceptance_lineage",
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_validate_generation_acceptance_for_use",
                    ),
                ):
                    repository._validate_cache_qualified_completion(
                        occurrence={
                            "policy_json": fixture["policy"]
                        },
                        item=fixture["item"],
                        generation=fixture["generation"],
                        records=[record],
                    )

    def test_direct_cache_read_only_requires_publisher_lineage_contract(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture(read_only=True)
        publisher = fixture["policy"]["direct_cache_authorities"][
            "consumers"
        ]["cache_publisher"]
        publisher["daily_dependency_lookback_rows"] = 20

        with self.assertRaisesRegex(
            RuntimeError,
            "publisher graph",
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[],
            )

    def test_direct_cache_group_requires_exactly_one_self_publisher(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture()
        authority = fixture["policy"]["direct_cache_authorities"]
        second = dict(authority["consumers"][fixture["scheme_id"]])
        second.update(
            {
                "base_scheme_id": "second_publisher",
                "cache_consumer_id": "second_publisher",
                "scheme_version": "second-publisher-version",
                "publisher_consumer_id": "second_publisher",
            }
        )
        authority["consumers"]["second_publisher"] = second
        fixture["policy"]["schemes"].append(
            {
                "scheme_id": "second_publisher",
                "cache_group": "liwei_test_family:5Y",
                "cache_spec_fingerprint": DIRECT_CACHE_SPEC,
                "cache_adapter_sha256": "5" * 64,
                "cache_core_sha256": "6" * 64,
            }
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "exactly one self-publisher",
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[],
            )

    def test_direct_cache_non_full_lineage_receives_dependency_contract(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture()
        fixture["manifest"]["build_mode"] = "suffix"
        fixture["manifest"]["generation_acceptance_evidence"][
            "build_mode"
        ] = "suffix"
        fixture["audit"]["generation_acceptance"][
            "build_mode"
        ] = "suffix"
        fixture["audit"].update(
            {
                "status": "extended",
                "build_mode": "suffix",
                "build_reason": "proven_daily_input_revision",
            }
        )
        loaded = SimpleNamespace(
            generation_id=fixture["generation_id"],
            path=Path(fixture["audit"]["generation_path"]),
            manifest=fixture["manifest"],
            manifest_sha256=DIRECT_CACHE_MANIFEST,
            caches={
                "STD": {
                    "test_dates": ["2026-05-29", "2026-06-01"],
                }
            },
        )
        record = PredictionRecord(
            scheme_id=fixture["scheme_id"],
            target_tenor="5Y",
            horizon=5,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE_T5,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            extra={"phase_a_cache": fixture["audit"]},
        )
        observed: dict[str, object] = {}

        def verify_lineage(
            generation,
            *,
            trusted_qualification,
        ) -> None:
            observed["build_mode"] = generation.manifest["build_mode"]
            observed["qualification"] = trusted_qualification[
                "qualification"
            ]

        with (
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_load_generation_directory",
                return_value=loaded,
            ),
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_verify_generation_acceptance_lineage",
                side_effect=verify_lineage,
            ),
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_validate_generation_acceptance_for_use",
            ),
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[record],
            )

        self.assertEqual(observed["build_mode"], "suffix")
        qualification = observed["qualification"]
        self.assertEqual(
            qualification["daily_dependency_lookback_rows"],
            21,
        )
        self.assertEqual(
            qualification["daily_dependency_proof"],
            "daily_window_is_bounded_by_21_rows",
        )

    def test_direct_cache_authority_allows_absent_dependency_proof(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture()
        consumer = fixture["policy"]["direct_cache_authorities"][
            "consumers"
        ][fixture["scheme_id"]]
        consumer["daily_dependency_lookback_rows"] = None
        consumer["daily_dependency_proof"] = None
        loaded = SimpleNamespace(
            generation_id=fixture["generation_id"],
            path=Path(fixture["audit"]["generation_path"]),
            manifest=fixture["manifest"],
            manifest_sha256=DIRECT_CACHE_MANIFEST,
            caches={"STD": {"test_dates": ["2026-06-01"]}},
        )
        record = PredictionRecord(
            scheme_id=fixture["scheme_id"],
            target_tenor="5Y",
            horizon=5,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE_T5,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            extra={"phase_a_cache": fixture["audit"]},
        )

        with (
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_load_generation_directory",
                return_value=loaded,
            ),
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_verify_generation_acceptance_lineage",
            ) as lineage,
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_validate_generation_acceptance_for_use",
            ),
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[record],
            )

        qualification = lineage.call_args.kwargs[
            "trusted_qualification"
        ]["qualification"]
        self.assertIsNone(
            qualification["daily_dependency_lookback_rows"]
        )
        self.assertIsNone(
            qualification["daily_dependency_proof"]
        )

    def test_direct_cache_authority_fails_closed_on_dynamic_drift(self) -> None:
        from scheduler import repository

        mutations = {
            "projection": lambda fixture: fixture["manifest"][
                "input_state"
            ]["effective_auxiliary"].update(
                {"proof_identity_sha256": "0" * 64}
            ),
            "native": lambda fixture: fixture["manifest"][
                "generation_acceptance_evidence"
            ]["native_generation"].update(
                {"generation_id": "native-drift"}
            ),
            "path": lambda fixture: fixture["audit"].update(
                {
                    "generation_path": str(
                        Path(fixture["storage_root"])
                        / "wrong-family"
                        / "5y"
                        / "generations"
                        / fixture["generation_id"]
                    )
                }
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                fixture = self._direct_cache_fixture()
                mutate(fixture)
                record = PredictionRecord(
                    scheme_id=fixture["scheme_id"],
                    target_tenor="5Y",
                    horizon=5,
                    predict_date=PREDICT_DATE,
                    feature_date=FEATURE_DATE,
                    target_date=TARGET_DATE_T5,
                    predicted_direction=1,
                    prediction_phase="scheduled_live",
                    extra={"phase_a_cache": fixture["audit"]},
                )
                loaded = SimpleNamespace(
                    generation_id=fixture["generation_id"],
                    path=Path(fixture["audit"]["generation_path"]),
                    manifest=fixture["manifest"],
                    manifest_sha256=DIRECT_CACHE_MANIFEST,
                    caches={
                        "STD": {
                            "test_dates": [
                                "2026-05-29",
                                "2026-06-01",
                            ],
                        }
                    },
                )
                with (
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_load_generation_directory",
                        return_value=loaded,
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_verify_generation_acceptance_lineage",
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_validate_generation_acceptance_for_use",
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "direct cache",
                    ),
                ):
                    repository._validate_cache_qualified_completion(
                        occurrence={
                            "policy_json": fixture["policy"]
                        },
                        item=fixture["item"],
                        generation=fixture["generation"],
                        records=[record],
                    )

    def test_direct_cache_authority_rejects_policy_cache_group_drift(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture()
        fixture["policy"]["schemes"][0][
            "cache_group"
        ] = "other_family:5Y"

        with self.assertRaisesRegex(
            RuntimeError,
            "consumer identity mismatch.*cache_group",
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[],
            )

    def test_direct_cache_authority_rejects_unbound_code_artifact_hashes(
        self,
    ) -> None:
        from scheduler import repository

        for field in (
            "cache_adapter_sha256",
            "cache_core_sha256",
        ):
            with self.subTest(field=field):
                fixture = self._direct_cache_fixture()
                consumer = fixture["policy"][
                    "direct_cache_authorities"
                ]["consumers"][fixture["scheme_id"]]
                consumer[field] = "0" * 64

                with self.assertRaisesRegex(
                    RuntimeError,
                    f"consumer identity mismatch.*{field}",
                ):
                    repository._validate_cache_qualified_completion(
                        occurrence={
                            "policy_json": fixture["policy"]
                        },
                        item=fixture["item"],
                        generation=fixture["generation"],
                        records=[],
                    )

    def test_direct_cache_authority_rejects_audit_identity_drift(
        self,
    ) -> None:
        from scheduler import repository

        mutations = {
            "version": "wrong.abi",
            "cache_family": "wrong_family",
            "tenor": "10Y",
            "input_content_id": "0" * 64,
            "input_change": {"change_type": "revision"},
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                fixture = self._direct_cache_fixture()
                fixture["audit"][field] = value
                loaded = SimpleNamespace(
                    generation_id=fixture["generation_id"],
                    path=Path(fixture["audit"]["generation_path"]),
                    manifest=fixture["manifest"],
                    manifest_sha256=DIRECT_CACHE_MANIFEST,
                    caches={"STD": {"test_dates": ["2026-06-01"]}},
                )
                record = PredictionRecord(
                    scheme_id=fixture["scheme_id"],
                    target_tenor="5Y",
                    horizon=5,
                    predict_date=PREDICT_DATE,
                    feature_date=FEATURE_DATE,
                    target_date=TARGET_DATE_T5,
                    predicted_direction=1,
                    prediction_phase="scheduled_live",
                    extra={"phase_a_cache": fixture["audit"]},
                )
                with (
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_load_generation_directory",
                        return_value=loaded,
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_verify_generation_acceptance_lineage",
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_validate_generation_acceptance_for_use",
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "audit identity",
                    ),
                ):
                    repository._validate_cache_qualified_completion(
                        occurrence={
                            "policy_json": fixture["policy"]
                        },
                        item=fixture["item"],
                        generation=fixture["generation"],
                        records=[record],
                    )

    def test_direct_cache_authority_rejects_audit_state_lies(
        self,
    ) -> None:
        from scheduler import repository

        mutations = {
            "status": "extended",
            "build_mode": "suffix",
            "build_reason": "native_generation_rebound",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                fixture = self._direct_cache_fixture()
                fixture["audit"][field] = value
                loaded = SimpleNamespace(
                    generation_id=fixture["generation_id"],
                    path=Path(fixture["audit"]["generation_path"]),
                    manifest=fixture["manifest"],
                    manifest_sha256=DIRECT_CACHE_MANIFEST,
                    caches={"STD": {"test_dates": ["2026-06-01"]}},
                )
                record = PredictionRecord(
                    scheme_id=fixture["scheme_id"],
                    target_tenor="5Y",
                    horizon=5,
                    predict_date=PREDICT_DATE,
                    feature_date=FEATURE_DATE,
                    target_date=TARGET_DATE_T5,
                    predicted_direction=1,
                    prediction_phase="scheduled_live",
                    extra={"phase_a_cache": fixture["audit"]},
                )
                with (
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_load_generation_directory",
                        return_value=loaded,
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_verify_generation_acceptance_lineage",
                    ),
                    patch(
                        "shared.liwei_0616_phase_a_cache."
                        "_validate_generation_acceptance_for_use",
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "audit state",
                    ),
                ):
                    repository._validate_cache_qualified_completion(
                        occurrence={
                            "policy_json": fixture["policy"]
                        },
                        item=fixture["item"],
                        generation=fixture["generation"],
                        records=[record],
                    )

    def test_direct_cache_authority_rejects_compare_gate_audit_drift(
        self,
    ) -> None:
        from scheduler import repository

        fixture = self._direct_cache_fixture()
        fixture["audit"]["compare_gate_evidence"]["status"] = "forged"
        loaded = SimpleNamespace(
            generation_id=fixture["generation_id"],
            path=Path(fixture["audit"]["generation_path"]),
            manifest=fixture["manifest"],
            manifest_sha256=DIRECT_CACHE_MANIFEST,
            caches={"STD": {"test_dates": ["2026-06-01"]}},
        )
        record = PredictionRecord(
            scheme_id=fixture["scheme_id"],
            target_tenor="5Y",
            horizon=5,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE_T5,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            extra={"phase_a_cache": fixture["audit"]},
        )

        with (
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_load_generation_directory",
                return_value=loaded,
            ),
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_verify_generation_acceptance_lineage",
            ),
            patch(
                "shared.liwei_0616_phase_a_cache."
                "_validate_generation_acceptance_for_use",
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "compare-gate audit",
            ),
        ):
            repository._validate_cache_qualified_completion(
                occurrence={"policy_json": fixture["policy"]},
                item=fixture["item"],
                generation=fixture["generation"],
                records=[record],
            )

    def test_direct_cache_monotonic_coverage_reopens_parent_securely(
        self,
    ) -> None:
        from scheduler import repository

        generation_path = (
            Path(self._tmpdir.name)
            / "cache-root"
            / "family"
            / "5y"
            / "generations"
            / "current"
        )
        current = SimpleNamespace(
            path=generation_path,
            manifest={
                "parent_generation_id": "parent",
                "generation_acceptance_evidence": {
                    "parent": {
                        "generation_id": "parent",
                        "manifest_sha256": "1" * 64,
                    }
                },
            },
            caches={
                "STD": {
                    "test_dates": [
                        "2026-05-29",
                        "2026-06-01",
                    ]
                }
            },
        )
        parent = SimpleNamespace(
            caches={"STD": {"test_dates": ["2026-05-29"]}},
        )
        calls: list[tuple[Path, dict[str, object]]] = []

        def loader(path: Path, **kwargs):
            calls.append((path, kwargs))
            return parent

        repository._validate_direct_cache_monotonic_coverage(
            current,
            loader=loader,
        )
        self.assertEqual(
            calls,
            [
                (
                    generation_path.parent / "parent",
                    {
                        "expected_generation_id": "parent",
                        "expected_manifest_sha256": "1" * 64,
                        "secure": True,
                    },
                )
            ],
        )

        current.caches["STD"]["test_dates"] = ["2026-06-01"]
        with self.assertRaisesRegex(ValueError, "not monotonic"):
            repository._validate_direct_cache_monotonic_coverage(
                current,
                loader=loader,
            )

    def _real_direct_cache_fixture(
        self,
        suffix: str,
    ) -> dict[str, object]:
        scheme_id = f"real_cache_consumer_{suffix}"
        cache_family = f"real_direct_cache_{suffix}"
        storage_root = (
            Path(self._tmpdir.name) / f"real-cache-root-{suffix}"
        ).absolute()
        spec = PhaseACacheSpec(
            cache_family=cache_family,
            tenor="5Y",
            baselines=("STD",),
            baseline_configs={
                "STD": {"close": "TB5YWI0C", "window": 2}
            },
            source_ic_screen_start="2024-01-01",
            horizon=5,
            purge_gap=5,
            publisher_consumer_id=scheme_id,
        )
        dates = ["2026-05-29", FEATURE_DATE]
        daily = pd.DataFrame(
            {
                "date": pd.to_datetime(dates),
                "TB5YWI0C": [1.60, 1.61],
            }
        )
        weekly = pd.DataFrame(
            {"week_id": [202622, 202623], "weekly_x": [0.9, 1.0]}
        )
        monthly = pd.DataFrame(
            {"month_id": ["202605", "202606"], "monthly_x": [1.9, 2.0]}
        )
        projection_frame = pd.DataFrame(
            {
                "date": dates,
                "effective_aux": [0.1, 0.2],
            }
        )
        mapping_entries = [
            {"date": dates[0], "week_id": 202622},
            {"date": dates[1], "week_id": 202623},
        ]
        mapping_payload = [
            [entry["date"], entry["week_id"]]
            for entry in mapping_entries
        ]
        projection = AuxiliaryDependencyProjection(
            frame=projection_frame,
            proof={
                "schema_version":
                    "liwei-0616-auxiliary-dependency-projection-v1",
                "date_to_week_mode": "explicit",
                "date_to_week_sha256": hashlib.sha256(
                    json.dumps(
                        mapping_payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
                "date_to_week_entries": mapping_entries,
                "proof_files": [
                    {
                        "name": "data_alignment.py",
                        "sha256": "9" * 64,
                    }
                ],
                "columns": list(projection_frame.columns),
                "dtypes": [
                    str(projection_frame[column].dtype)
                    for column in projection_frame.columns
                ],
                "daily_grid_sha256": hashlib.sha256(
                    "|".join(dates).encode("utf-8")
                ).hexdigest(),
                "feature_cutoff": FEATURE_DATE,
            },
            content_sha256=hashlib.sha256(
                projection_frame.to_json(
                    orient="split"
                ).encode("utf-8")
            ).hexdigest(),
        )
        native_binding = {
            "generation_id": f"native-{suffix}",
            "manifest_sha256": "1" * 64,
            "dataset_content_id": "2" * 64,
            "business_date": PREDICT_DATE,
            "feature_date": FEATURE_DATE,
            "schema_version": "native-generation-v1",
            "exporter_version": "native-generation-exporter-v1",
        }

        def train_missing(
            baseline: str,
            ranges: tuple[tuple[str, str], ...],
        ) -> dict[str, object]:
            test_dates = [
                start
                for start, end in ranges
                if start == end
            ]
            values = np.asarray(
                [index + 1 for index in range(len(test_dates))],
                dtype=np.int32,
            )
            return {
                "test_dates": test_dates,
                "results": [
                    {
                        "config": {
                            "baseline": baseline,
                            "window": 2,
                        },
                        "preds": values,
                        "probs": values.astype(np.float64) / 10.0,
                    }
                ],
            }

        _caches, audit = prepare_phase_a_caches(
            spec=spec,
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=((FEATURE_DATE, FEATURE_DATE),),
            train_missing=train_missing,
            cache_consumer_id=scheme_id,
            native_generation=native_binding,
            auxiliary_dependency_projection=projection,
            cache_root=storage_root,
        )
        generation_path = Path(audit["generation_path"])
        manifest = json.loads(
            (generation_path / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        spec_fingerprint = cache_module._spec_fingerprint(spec)
        projection_sha256 = manifest["input_state"][
            "effective_auxiliary"
        ]["proof_identity_sha256"]
        consumer = {
            "base_scheme_id": scheme_id,
            "cache_consumer_id": scheme_id,
            "scheme_version": f"{scheme_id}-version",
            "code_sha256": "3" * 64,
            "config_sha256": "4" * 64,
            "cache_group": f"{cache_family}:5Y",
            "spec_fingerprint": spec_fingerprint,
            "publisher_consumer_id": scheme_id,
            "cache_family": cache_family,
            "tenor": "5Y",
            "access_mode": "publisher",
            "cache_adapter_sha256": "5" * 64,
            "cache_core_sha256": "6" * 64,
            "projection_proof_identity_sha256": projection_sha256,
            "daily_dependency_lookback_rows": None,
            "daily_dependency_proof": None,
        }
        policy = {
            "schemes": [
                {
                    "scheme_id": scheme_id,
                    "cache_group": f"{cache_family}:5Y",
                    "cache_spec_fingerprint": spec_fingerprint,
                    "cache_adapter_sha256": "5" * 64,
                    "cache_core_sha256": "6" * 64,
                }
            ],
            "direct_cache_authorities": {
                "schema_version":
                    "daily-direct-cache-authorities-v1",
                "storage_root": str(storage_root),
                "contract": {
                    "manifest_schema_version": 3,
                    "input_state_schema_version": 3,
                    "cache_abi_version":
                        "liwei_0616.phase_a.v1",
                    "projection_schema_version":
                        "liwei-0616-auxiliary-dependency-projection-v1",
                    "native_generation_type": "native_source",
                    "native_generation_schema_version":
                        "native-generation-v1",
                    "native_exporter_version":
                        "native-generation-exporter-v1",
                },
                "consumers": {scheme_id: consumer},
            },
        }
        item = {
            "base_scheme_id": scheme_id,
            "scheme_version": f"{scheme_id}-version",
            "code_sha256": "3" * 64,
            "config_sha256": "4" * 64,
            "cache_group": f"{cache_family}:5Y",
        }
        generation = {
            **native_binding,
            "generation_type": "native_source",
            "state": "SEALED",
        }
        record = PredictionRecord(
            scheme_id=scheme_id,
            target_tenor="5Y",
            horizon=5,
            predict_date=PREDICT_DATE,
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE_T5,
            predicted_direction=1,
            prediction_phase="scheduled_live",
            extra={"phase_a_cache": audit},
        )
        return {
            "policy": policy,
            "item": item,
            "generation": generation,
            "record": record,
            "audit": audit,
        }

    def _direct_cache_fixture(
        self,
        *,
        read_only: bool = False,
    ) -> dict[str, object]:
        scheme_id = "cache_consumer"
        publisher_id = "cache_publisher" if read_only else scheme_id
        storage_root = str(
            (Path(self._tmpdir.name) / "cache-root").absolute()
        )
        generation_id = "generation-direct-cache-test"
        native_binding = {
            "generation_id": "native-0123456789abcdef01234567",
            "manifest_sha256": "1" * 64,
            "dataset_content_id": "2" * 64,
            "business_date": PREDICT_DATE,
            "feature_date": FEATURE_DATE,
            "schema_version": "native-generation-v1",
            "exporter_version": "native-generation-exporter-v1",
        }

        def consumer(
            base_scheme_id: str,
            *,
            access_mode: str,
        ) -> dict[str, object]:
            return {
                "base_scheme_id": base_scheme_id,
                "cache_consumer_id": base_scheme_id,
                "scheme_version": f"{base_scheme_id}-version",
                "code_sha256": "3" * 64,
                "config_sha256": "4" * 64,
                "cache_group": "liwei_test_family:5Y",
                "spec_fingerprint": DIRECT_CACHE_SPEC,
                "publisher_consumer_id": publisher_id,
                "cache_family": "liwei_test_family",
                "tenor": "5Y",
                "access_mode": access_mode,
                "cache_adapter_sha256": "5" * 64,
                "cache_core_sha256": "6" * 64,
                "projection_proof_identity_sha256":
                    DIRECT_CACHE_PROJECTION,
                "daily_dependency_lookback_rows": 21,
                "daily_dependency_proof":
                    "daily_window_is_bounded_by_21_rows",
            }

        consumers = {
            scheme_id: consumer(
                scheme_id,
                access_mode="read_only" if read_only else "publisher",
            )
        }
        schemes = [
            {
                "scheme_id": scheme_id,
                "cache_group": "liwei_test_family:5Y",
                "cache_spec_fingerprint": DIRECT_CACHE_SPEC,
                "cache_adapter_sha256": "5" * 64,
                "cache_core_sha256": "6" * 64,
            }
        ]
        if read_only:
            consumers[publisher_id] = consumer(
                publisher_id,
                access_mode="publisher",
            )
            schemes.append(
                {
                    "scheme_id": publisher_id,
                    "cache_group": "liwei_test_family:5Y",
                    "cache_spec_fingerprint": DIRECT_CACHE_SPEC,
                    "cache_adapter_sha256": "5" * 64,
                    "cache_core_sha256": "6" * 64,
                }
            )
        authority = {
            "schema_version": "daily-direct-cache-authorities-v1",
            "storage_root": storage_root,
            "contract": {
                "manifest_schema_version": 3,
                "input_state_schema_version": 3,
                "cache_abi_version": "liwei_0616.phase_a.v1",
                "projection_schema_version":
                    "liwei-0616-auxiliary-dependency-projection-v1",
                "native_generation_type": "native_source",
                "native_generation_schema_version":
                    "native-generation-v1",
                "native_exporter_version":
                    "native-generation-exporter-v1",
            },
            "consumers": consumers,
        }
        acceptance = {
            "native_generation": dict(native_binding),
            "parent": None,
            "build_mode": "full",
        }
        family_root = (
            Path(storage_root) / "liwei_test_family" / "5y"
        )
        generation_path = (
            family_root / "generations" / generation_id
        )
        audit = {
            "status": "cold_build",
            "build_mode": "full",
            "build_reason": "no_current_generation",
            "version": "liwei_0616.phase_a.v1",
            "cache_family": "liwei_test_family",
            "tenor": "5Y",
            "input_content_id": "7" * 64,
            "input_change": {"change_type": "initial"},
            "compare_gate_evidence": {
                "schema_version": "phase-a-compare-gate-v1",
                "status": "passed",
                "generation_id": generation_id,
                "generation_manifest_sha256": DIRECT_CACHE_MANIFEST,
            },
            "generation_id": generation_id,
            "generation_path": str(generation_path),
            "generation_manifest_sha256": DIRECT_CACHE_MANIFEST,
            "family_root": str(family_root),
            "current_pointer": str(family_root / "current.json"),
            "published": True,
            "generation_acceptance": dict(acceptance),
        }
        manifest = {
            "schema_version": 3,
            "generation_id": generation_id,
            "abi_version": "liwei_0616.phase_a.v1",
            "cache_family": "liwei_test_family",
            "tenor": "5Y",
            "spec_fingerprint": DIRECT_CACHE_SPEC,
            "build_mode": "full",
            "input_change": {"change_type": "initial"},
            "compare_gate_evidence": {
                "schema_version": "phase-a-compare-gate-v1",
                "status": "passed",
            },
            "input_state": {
                "schema_version": 3,
                "content_id": "7" * 64,
                "native_generation": dict(native_binding),
                "effective_auxiliary": {
                    "schema_version":
                        "liwei-0616-auxiliary-dependency-projection-v1",
                    "proof_identity_sha256": DIRECT_CACHE_PROJECTION,
                },
            },
            "parent_generation_id": None,
            "generation_acceptance_evidence": dict(acceptance),
        }
        return {
            "scheme_id": scheme_id,
            "storage_root": storage_root,
            "generation_id": generation_id,
            "native_binding": native_binding,
            "policy": {
                "schemes": schemes,
                "direct_cache_authorities": authority,
            },
            "item": {
                "base_scheme_id": scheme_id,
                "scheme_version": f"{scheme_id}-version",
                "code_sha256": "3" * 64,
                "config_sha256": "4" * 64,
                "cache_group": "liwei_test_family:5Y",
            },
            "generation": {
                "generation_id": native_binding["generation_id"],
                "generation_type": "native_source",
                "manifest_sha256": native_binding["manifest_sha256"],
                "dataset_content_id":
                    native_binding["dataset_content_id"],
                "business_date": native_binding["business_date"],
                "feature_date": native_binding["feature_date"],
                "schema_version": native_binding["schema_version"],
                "exporter_version": native_binding["exporter_version"],
                "state": "SEALED",
            },
            "audit": audit,
            "manifest": manifest,
        }


_SCHEMA = (
    """
    CREATE TABLE t_scheme_runs (
        run_id INTEGER PRIMARY KEY,
        scheme_id TEXT NOT NULL,
        scheme_version TEXT,
        runtime_type TEXT NOT NULL,
        run_type TEXT NOT NULL,
        prediction_phase TEXT,
        predict_date TEXT NOT NULL,
        status TEXT NOT NULL,
        records_expected INTEGER,
        records_returned INTEGER,
        records_written INTEGER,
        schedule_item_id INTEGER,
        attempt_no INTEGER,
        trigger_origin TEXT,
        queued_at TEXT,
        failure_code TEXT,
        execution_token TEXT,
        process_id INTEGER,
        process_group_id INTEGER,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT,
        error_message TEXT
    )
    """,
    """
    CREATE TABLE t_scheme_registry (
        scheme_id TEXT PRIMARY KEY,
        base_scheme_id TEXT NOT NULL,
        runtime_type TEXT NOT NULL,
        frequency TEXT NOT NULL,
        task_type TEXT NOT NULL,
        target_tenor TEXT NOT NULL,
        horizon INTEGER NOT NULL,
        status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE t_scheme_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
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
        extra TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (scheme_id, target_tenor, horizon, target_date)
    )
    """,
    """
    CREATE TABLE t_scheme_run_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        scheme_id TEXT NOT NULL,
        run_date TEXT NOT NULL,
        status TEXT NOT NULL,
        duration_sec REAL,
        error_msg TEXT
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
)


if __name__ == "__main__":
    unittest.main()
