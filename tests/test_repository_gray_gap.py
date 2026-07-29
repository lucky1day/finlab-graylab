from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, event, text

from shared.models import PredictionRecord


TENORS_T5 = ("3Y", "5Y", "7Y", "10Y")
TENORS_T1 = ("5Y", "10Y")
PREDICT_DATE = "2026-06-02"
FEATURE_DATE = "2026-06-01"
TARGET_DATE_T5 = "2026-06-08"
TARGET_DATE_T1 = "2026-06-02"
PLAN_SHA256 = "a" * 64


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

    def tearDown(self) -> None:
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
            conn.exec_driver_sql(
                "INSERT INTO t_schedule_occurrences VALUES (1, 'PENDING')"
            )
            conn.exec_driver_sql(
                "INSERT INTO t_schedule_items VALUES (2, 'PENDING', 'PENDING')"
            )
            conn.exec_driver_sql(
                "INSERT INTO t_schedule_item_targets VALUES (3, 'PENDING', NULL)"
            )

        with self.assertRaisesRegex(RuntimeError, "active Registry target multiset"):
            self._complete(
                cfg,
                run_id=112,
                target_date=TARGET_DATE_T1,
            )
        self.assertEqual(
            self._rows("t_schedule_occurrences"),
            [{"occurrence_id": 1, "completion_state": "PENDING"}],
        )
        self.assertEqual(
            self._rows("t_schedule_items"),
            [{"item_id": 2, "state": "PENDING", "sla_status": "PENDING"}],
        )
        self.assertEqual(
            self._rows("t_schedule_item_targets"),
            [{"target_id": 3, "status": "PENDING", "accepted_run_id": None}],
        )

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
        occurrence_id INTEGER PRIMARY KEY,
        completion_state TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE t_schedule_items (
        item_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL,
        sla_status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE t_schedule_item_targets (
        target_id INTEGER PRIMARY KEY,
        status TEXT NOT NULL,
        accepted_run_id INTEGER
    )
    """,
)


if __name__ == "__main__":
    unittest.main()
