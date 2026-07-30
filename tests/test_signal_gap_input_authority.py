from __future__ import annotations

import hashlib
import inspect
import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "signal_gap_missing_20260727.json"
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def one(self):
        if len(self._rows) != 1:
            raise AssertionError("expected exactly one row")
        return self._rows[0]


class _Connection:
    def __init__(self, responses=()):
        self.responses = tuple(responses)
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        del params
        sql = str(statement)
        self.statements.append(sql)
        for marker, rows in self.responses:
            if marker in sql:
                return _Rows(rows)
        raise AssertionError(f"unexpected SQL: {sql}")


class _PlanConnection(_Connection):
    def __init__(self):
        super().__init__()
        self.driver_sql: list[str] = []
        self.rollback_count = 0

    def exec_driver_sql(self, statement):
        self.driver_sql.append(statement)

    def rollback(self):
        self.rollback_count += 1


class _PlanEngine:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection


def _fixture_rows():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _readonly_databridge_config():
    from shared.data_bridge.refresh import DataBridgeRefreshConfig

    project_root = Path("/trusted/bond-factor-lab")
    return DataBridgeRefreshConfig(
        data_root=project_root / "data" / "data_bridge",
        runtime_root=(
            project_root
            / "backtest_artifacts"
            / "data_bridge_refresh"
        ),
        schema_path=(
            project_root
            / "shared"
            / "blackbox_v2"
            / "data_bridge_v1_schema.json"
        ),
    )


def _target(row):
    from harness.signal_gap_plan import RegistryTarget

    is_overlay = row["base_scheme_id"] == "weekly_10y_d_overlay_0529"
    is_weekly_blackbox = (
        row["base_scheme_id"] == "weekly_10y_lgbm_point_v1"
    )
    runtime_type = "native_adapter" if is_overlay else "blackbox_v2"
    return RegistryTarget(
        registry_scheme_id=(
            f"{row['base_scheme_id']}__h{row['horizon']}__"
            f"{row['target_tenor']}"
        ),
        base_scheme_id=row["base_scheme_id"],
        runtime_type=runtime_type,
        frequency=(
            "weekly"
            if is_overlay or is_weekly_blackbox
            else "daily"
        ),
        task_type=(
            "weekly_point"
            if is_overlay or is_weekly_blackbox
            else "T+5"
        ),
        target_tenor=row["target_tenor"],
        horizon=row["horizon"],
        scheme_version=f"{row['base_scheme_id']}-version",
        live_target_start_date="2026-06-01",
        live_boundary_source="platform_live_boundary_v1",
        input_mode=(
            "generation_v1" if is_overlay else "databridge_v1"
        ),
        code_sha256=_sha(f"{row['base_scheme_id']}:code"),
        config_sha256=_sha(f"{row['base_scheme_id']}:config"),
    )


def _fixture_calendar():
    from harness.signal_gap_plan import _FrozenCalendar

    start = date(2026, 6, 22)
    end = date(2026, 8, 7)
    calendar_days = tuple(
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    )
    trade_rows = [
        {
            "rdate": day.isoformat(),
            "trade_flag": "1" if day.weekday() < 5 else "0",
        }
        for day in calendar_days
    ]
    week_rows = [
        {
            "rdate": day.isoformat(),
            "trade_flag": "1" if day.weekday() < 5 else "0",
            "week_id": int(
                f"{day.isocalendar().year}"
                f"{day.isocalendar().week:02d}"
            ),
        }
        for day in calendar_days
    ]
    return _FrozenCalendar(
        trade_calendar_rows=trade_rows,
        week_calendar_rows=week_rows,
        start_date="2026-07-01",
        as_of_date="2026-07-27",
    )


def _fixture_cases(rows, targets):
    from harness.signal_gap_plan import build_expected_live_cases

    if len(rows) != 29:
        raise AssertionError("fixture must define exactly 29 deleted keys")
    expected_counts = {
        "GRAY_LIVE_GAP": 12,
        "BLOCKED_NO_GENERATION": 13,
        "BLOCKED_DATA_CONTRACT": 4,
    }
    actual_counts = {
        action: sum(row["expected_action"] == action for row in rows)
        for action in expected_counts
    }
    if actual_counts != expected_counts:
        raise AssertionError("fixture action matrix drift")
    fixture_keys = {
        (
            row["base_scheme_id"],
            row["target_tenor"],
            row["horizon"],
            row["target_date"],
        )
        for row in rows
    }
    if len(fixture_keys) != 29:
        raise AssertionError("fixture contains duplicate business keys")
    generated = build_expected_live_cases(
        tuple(targets.values()),
        calendar=_fixture_calendar(),
        as_of_date="2026-07-27",
    )
    by_key = {
        case.business_key: case
        for case in generated
        if case.business_key in fixture_keys
    }
    missing = fixture_keys - set(by_key)
    if missing:
        raise AssertionError(
            f"fixture key is not generated by live contexts: {sorted(missing)}"
        )
    result = []
    for row in rows:
        key = (
            row["base_scheme_id"],
            row["target_tenor"],
            row["horizon"],
            row["target_date"],
        )
        case = by_key[key]
        if case.base_scheme_id == "weekly_10y_d_overlay_0529":
            case = replace(
                case,
                data_contract_error="WEEK_CALENDAR_CONTRACT:202625",
            )
        result.append(case)
    return tuple(result)


def _current_authority(
    *,
    refresh_date="2026-07-24",
    reverse=False,
    changed_cutoff=False,
    changed_file=False,
    with_publication=False,
):
    from shared.data_bridge.authority import (
        StableDataBridgeCurrentAuthority,
        StableDataBridgeCutoff,
        StableDataBridgeFileIdentity,
        StablePublicationCapability,
    )

    files = [
        StableDataBridgeFileIdentity(
            filename=filename,
            rows=100,
            columns=10,
            min_key=min_key,
            max_key=max_key,
            sha256=_sha(
                f"{filename}:sha"
                + (
                    ":changed"
                    if changed_file
                    and filename == "daily_output.csv"
                    else ""
                )
            ),
            business_hash=_sha(f"{filename}:business"),
        )
        for filename, min_key, max_key in (
            ("daily_output.csv", "2025-01-01", refresh_date),
            ("weekly_output.csv", "202501", "202630"),
            ("monthly_output.csv", "202501", "202607"),
        )
    ]
    cutoffs = [
        StableDataBridgeCutoff(
            feature_date=feature_date,
            daily_cutoff_key=(
                "2026-07-19"
                if changed_cutoff and feature_date == "2026-07-20"
                else min(feature_date, refresh_date)
            ),
            weekly_cutoff_key="202629",
            monthly_cutoff_key="202607",
        )
        for feature_date in (
            "2026-07-20",
            "2026-07-21",
            "2026-07-22",
            "2026-07-23",
            "2026-07-24",
        )
    ]
    if reverse:
        files.reverse()
        cutoffs.reverse()
    return StableDataBridgeCurrentAuthority(
        authority_schema_version=(
            "stable-databridge-current-authority-v1"
        ),
        generation_id=f"current-{refresh_date}",
        refresh_date=refresh_date,
        schema_version="data-bridge-v1",
        business_digest=_sha(f"business:{refresh_date}"),
        publication_capability=(
            StablePublicationCapability(
                occurrence_id=42,
                business_date="2026-07-27",
                epoch=3,
                mode="ledger",
                record_sha256=_sha("publication"),
            )
            if with_publication
            else None
        ),
        files=tuple(files),
        cutoffs=tuple(cutoffs),
        publication_identity_sha256=_sha(
            f"publication-identity:{refresh_date}:{changed_cutoff}:"
            f"{changed_file}"
        ),
        stable_identity_sha256=_sha(
            f"authority:{refresh_date}:{changed_cutoff}:"
            f"{changed_file}:{with_publication}"
        ),
    )


def _fixture_snapshot(*, authority=None, authority_error=None):
    from harness.signal_gap_plan import SignalGapSnapshot

    rows = _fixture_rows()
    targets = {
        (
            row["base_scheme_id"],
            row["target_tenor"],
            row["horizon"],
        ): _target(row)
        for row in rows
    }
    return SignalGapSnapshot(
        registry_targets=tuple(
            sorted(
                targets.values(),
                key=lambda item: item.registry_scheme_id,
            )
        ),
        expected_cases=tuple(
            _fixture_cases(rows, targets)
        ),
        canonical_signals=(),
        live_signals=(),
        input_generations=(),
        input_watermarks={"trade_calendar_max": "2026-07-31"},
        source_identity_sha256=_sha("source"),
        discovery_identity_sha256=_sha("discovery"),
        active_version_identity_sha256=_sha("versions"),
        databridge_authority=(
            authority
            if authority is not None
            else _current_authority()
        ),
        databridge_authority_error=authority_error,
    )


def _native_generation(**changes):
    from harness.signal_gap_plan import InputGeneration

    values = {
        "generation_id": "native-0123456789abcdef01234567",
        "generation_type": "native_source",
        "business_date": "2026-07-30",
        "feature_date": "2026-07-24",
        "readiness_basis": "CLOCK_CONTRACT",
        "source_commit_token": _sha("native-source"),
        "dataset_content_id": _sha("native-dataset"),
        "schema_version": "native-generation-v1",
        "exporter_version":
            "native-signal-gap-current-snapshot-v1",
        "manifest_uri": "/private/native/manifest.json",
        "manifest_sha256": _sha("native-manifest"),
        "native_generation_id": None,
        "native_manifest_sha256": None,
        "state": "SEALED",
        "sealed_at": "2026-07-27T00:01:00.000000",
    }
    values.update(changes)
    return InputGeneration(**values)


def _native_snapshot(*, generations, input_mode="generation_v1"):
    from harness.signal_gap_plan import (
        ExpectedSignalCase,
        RegistryTarget,
        SignalGapSnapshot,
    )

    target = RegistryTarget(
        registry_scheme_id="native_demo__h5__1Y",
        base_scheme_id="native_demo",
        runtime_type="native_adapter",
        frequency="daily",
        task_type="T+5",
        target_tenor="1Y",
        horizon=5,
        scheme_version="native-version",
        live_target_start_date="2026-06-01",
        live_boundary_source="platform_live_boundary_v1",
        input_mode=input_mode,
        code_sha256=_sha("native-code"),
        config_sha256=_sha("native-config"),
        source_package_sha256=(
            _sha("source-package")
            if input_mode == "live_source_0629"
            else None
        ),
    )
    case = ExpectedSignalCase(
        registry_scheme_id=target.registry_scheme_id,
        base_scheme_id=target.base_scheme_id,
        runtime_type=target.runtime_type,
        frequency=target.frequency,
        task_type=target.task_type,
        target_tenor=target.target_tenor,
        horizon=target.horizon,
        predict_date="2026-07-27",
        feature_date="2026-07-24",
        target_date="2026-07-31",
        segment="live",
    )
    return SignalGapSnapshot(
        registry_targets=(target,),
        expected_cases=(case,),
        canonical_signals=(),
        live_signals=(),
        input_generations=tuple(generations),
        input_watermarks={},
        source_identity_sha256=_sha("source"),
        discovery_identity_sha256=_sha("discovery"),
        active_version_identity_sha256=_sha("versions"),
    )


class SignalGapInputAuthorityTests(unittest.TestCase):
    def _plan(self, snapshot):
        from harness.signal_gap_plan import build_signal_gap_plan

        return build_signal_gap_plan(
            snapshot,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

    def test_databridge_config_is_required_at_both_planner_layers(self):
        from harness.signal_gap_plan import (
            plan_signal_gaps,
            read_signal_gap_snapshot,
        )

        for function in (
            plan_signal_gaps,
            read_signal_gap_snapshot,
        ):
            with self.subTest(function=function.__name__):
                parameter = inspect.signature(function).parameters[
                    "databridge_config"
                ]
                self.assertIs(
                    parameter.default,
                    inspect.Parameter.empty,
                )

    def test_independent_fixture_has_exact_12_13_4_matrix(self):
        rows = _fixture_rows()
        self.assertEqual(len(rows), 29)
        self.assertEqual(
            len(
                {
                    (
                        row["base_scheme_id"],
                        row["target_tenor"],
                        row["horizon"],
                        row["target_date"],
                    )
                    for row in rows
                }
            ),
            29,
        )

        plan = self._plan(_fixture_snapshot())

        self.assertEqual(
            {
                action: plan["counts"][action]
                for action in (
                    "GRAY_LIVE_GAP",
                    "BLOCKED_NO_GENERATION",
                    "BLOCKED_DATA_CONTRACT",
                )
            },
            {
                "GRAY_LIVE_GAP": 12,
                "BLOCKED_NO_GENERATION": 13,
                "BLOCKED_DATA_CONTRACT": 4,
            },
        )
        actual = {
            tuple(item["business_key"]): item["action"]
            for item in plan["actions"]
        }
        expected = {
            (
                row["base_scheme_id"],
                row["target_tenor"],
                row["horizon"],
                row["target_date"],
            ): row["expected_action"]
            for row in rows
        }
        self.assertEqual(actual, expected)
        self.assertEqual(
            {
                row["reason"]
                for row in plan["actions"]
                if row["action"] == "BLOCKED_NO_GENERATION"
            },
            {"DATABRIDGE_CURRENT_REFRESH_REQUIRED"},
        )

    def test_fixture_keys_come_from_real_live_context_builders(self):
        rows = _fixture_rows()
        targets = {
            (
                row["base_scheme_id"],
                row["target_tenor"],
                row["horizon"],
            ): _target(row)
            for row in rows
        }

        cases = _fixture_cases(rows, targets)

        self.assertEqual(
            {case.business_key for case in cases},
            {
                (
                    row["base_scheme_id"],
                    row["target_tenor"],
                    row["horizon"],
                    row["target_date"],
                )
                for row in rows
            },
        )
        wrong_date = [
            {
                **row,
                "target_date": (
                    "2026-07-26"
                    if index == 0
                    else row["target_date"]
                ),
            }
            for index, row in enumerate(rows)
        ]
        with self.assertRaisesRegex(
            AssertionError,
            "not generated by live contexts",
        ):
            _fixture_cases(wrong_date, targets)
        with self.assertRaisesRegex(
            AssertionError,
            "exactly 29 deleted keys",
        ):
            _fixture_cases(rows[:-1], targets)

    def test_databridge_refresh_fence_uses_predict_date(self):
        plan = self._plan(_fixture_snapshot())
        rows = {
            (row["base_scheme_id"], row["predict_date"]): row
            for row in plan["actions"]
        }
        base = "ten_y_t5_maj3_k3_ic_static_v1"

        self.assertEqual(
            rows[(base, "2026-07-23")]["action"],
            "GRAY_LIVE_GAP",
        )
        self.assertEqual(
            rows[(base, "2026-07-24")]["action"],
            "BLOCKED_NO_GENERATION",
        )
        self.assertEqual(
            rows[(base, "2026-07-27")]["feature_date"],
            "2026-07-24",
        )
        self.assertEqual(
            rows[(base, "2026-07-27")]["action"],
            "BLOCKED_NO_GENERATION",
        )

    def test_newer_current_unlocks_all_blackbox_gaps_only(self):
        plan = self._plan(
            _fixture_snapshot(
                authority=_current_authority(
                    refresh_date="2026-07-28"
                )
            )
        )

        self.assertEqual(plan["counts"]["GRAY_LIVE_GAP"], 25)
        self.assertEqual(plan["counts"]["BLOCKED_NO_GENERATION"], 0)
        self.assertEqual(plan["counts"]["BLOCKED_DATA_CONTRACT"], 4)

    def test_missing_and_invalid_current_are_distinguished(self):
        missing = self._plan(
            _fixture_snapshot(
                authority=None,
                authority_error="MISSING",
            )
        )
        invalid = self._plan(
            _fixture_snapshot(
                authority=None,
                authority_error="INVALID",
            )
        )

        missing_row = next(
            row
            for row in missing["actions"]
            if row["base_scheme_id"].startswith("ten_y_")
        )
        invalid_row = next(
            row
            for row in invalid["actions"]
            if row["base_scheme_id"].startswith("ten_y_")
        )
        self.assertEqual(
            (
                missing_row["action"],
                invalid_row["action"],
            ),
            (
                "BLOCKED_NO_GENERATION",
                "BLOCKED_DATA_CONTRACT",
            ),
        )

    def test_old_sealed_databridge_row_never_unlocks_current_gap(self):
        old_row = replace(
            _native_generation(),
            generation_id="old-databridge",
            generation_type="databridge_v1",
            business_date="2026-07-24",
            native_generation_id="native-parent",
            native_manifest_sha256=_sha("native-parent"),
        )
        for error, expected_action in (
            ("MISSING", "BLOCKED_NO_GENERATION"),
            ("INVALID", "BLOCKED_DATA_CONTRACT"),
        ):
            with self.subTest(error=error):
                snapshot = replace(
                    _fixture_snapshot(
                        authority=None,
                        authority_error=error,
                    ),
                    input_generations=(old_row,),
                )

                plan = self._plan(snapshot)

                blackbox = [
                    row
                    for row in plan["actions"]
                    if row["runtime_type"] == "blackbox_v2"
                ]
                self.assertEqual(len(blackbox), 25)
                self.assertTrue(
                    all(
                        row["action"] == expected_action
                        for row in blackbox
                    )
                )

    def test_native_exact_fence_validation_fails_closed(self):
        missing_artifact = self._plan(
            _native_snapshot(generations=(_native_generation(),))
        )
        self.assertEqual(
            (
                missing_artifact["actions"][0]["action"],
                missing_artifact["actions"][0]["reason"],
            ),
            (
                "BLOCKED_DATA_CONTRACT",
                "NATIVE_GENERATION_ARTIFACT_INVALID",
            ),
        )
        for label, generations, expected_action in (
            (
                "wrong generation type",
                (
                    _native_generation(
                        generation_type="databridge_v1",
                    ),
                ),
                "BLOCKED_NO_GENERATION",
            ),
            (
                "wrong business date",
                (_native_generation(business_date="2026-07-26"),),
                "BLOCKED_NO_GENERATION",
            ),
            (
                "wrong feature date",
                (_native_generation(feature_date="2026-07-23"),),
                "BLOCKED_NO_GENERATION",
            ),
            (
                "duplicate",
                (
                    _native_generation(),
                    _native_generation(
                        generation_id="native-duplicate"
                    ),
                ),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "empty generation id",
                (_native_generation(generation_id=""),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "bad readiness basis",
                (_native_generation(readiness_basis="UNPROVEN"),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "empty source commit token",
                (_native_generation(source_commit_token=""),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "empty dataset content id",
                (_native_generation(dataset_content_id=""),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "empty schema version",
                (_native_generation(schema_version=""),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "empty exporter version",
                (_native_generation(exporter_version=""),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "relative manifest uri",
                (_native_generation(manifest_uri="manifest.json"),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "invalid manifest sha",
                (_native_generation(manifest_sha256="bad"),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "unexpected native parent id",
                (_native_generation(native_generation_id="parent"),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "unexpected native parent sha",
                (
                    _native_generation(
                        native_manifest_sha256=_sha("parent"),
                    ),
                ),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "invalid state",
                (_native_generation(state="BUILDING"),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "missing sealed timestamp",
                (_native_generation(sealed_at=None),),
                "BLOCKED_DATA_CONTRACT",
            ),
            (
                "noncanonical sealed timestamp",
                (
                    _native_generation(
                        sealed_at="2026-07-27T00:01:00",
                    ),
                ),
                "BLOCKED_DATA_CONTRACT",
            ),
        ):
            with self.subTest(label=label):
                action = self._plan(
                    _native_snapshot(generations=generations)
                )["actions"][0]
                self.assertEqual(action["action"], expected_action)

    def test_live_source_0629_is_actionable_only_with_frozen_package(self):
        from harness.signal_gap_plan import ObservedSignal

        snapshot = _native_snapshot(
            generations=(_native_generation(),),
            input_mode="live_source_0629",
        )
        with patch(
            "harness.signal_gap_plan._NativeArtifactVerifier.verify",
            return_value=({"artifact": "verified"}, None),
        ):
            plan = self._plan(snapshot)

        self.assertEqual(
            plan["actions"][0]["action"],
            "GRAY_LIVE_GAP",
        )
        self.assertEqual(
            plan["actions"][0]["source_package_sha256"],
            _sha("source-package"),
        )
        case = snapshot.expected_cases[0]
        target = snapshot.registry_targets[0]
        present = self._plan(
            replace(
                snapshot,
                live_signals=(
                    ObservedSignal(
                        base_scheme_id=case.base_scheme_id,
                        target_tenor=case.target_tenor,
                        horizon=case.horizon,
                        target_date=case.target_date,
                        predict_date=case.predict_date,
                        feature_date=case.feature_date,
                        phase="gray_live",
                        scheme_version=target.scheme_version,
                        run_status="success",
                    ),
                ),
            )
        )
        self.assertEqual(
            present["actions"][0]["action"],
            "SKIP_PRESENT",
        )

        changed_target = replace(
            snapshot.registry_targets[0],
            source_package_sha256=_sha("changed-source-package"),
        )
        with patch(
            "harness.signal_gap_plan._NativeArtifactVerifier.verify",
            return_value=({"artifact": "verified"}, None),
        ):
            changed = self._plan(
                replace(
                    snapshot,
                    registry_targets=(changed_target,),
                )
            )
        self.assertNotEqual(
            plan["plan_sha256"],
            changed["plan_sha256"],
        )

    def test_action_authority_is_order_invariant_and_digest_sensitive(self):
        baseline = self._plan(_fixture_snapshot())
        reordered = self._plan(
            _fixture_snapshot(
                authority=_current_authority(reverse=True)
            )
        )
        variants = {
            "cutoff": _current_authority(changed_cutoff=True),
            "file": _current_authority(changed_file=True),
            "publication": _current_authority(with_publication=True),
        }

        self.assertEqual(baseline, reordered)
        for label, authority in variants.items():
            with self.subTest(label=label):
                changed = self._plan(
                    _fixture_snapshot(authority=authority)
                )
                self.assertNotEqual(
                    baseline["plan_sha256"],
                    changed["plan_sha256"],
                )
        action = next(
            row
            for row in baseline["actions"]
            if row["action"] == "GRAY_LIVE_GAP"
        )
        self.assertNotIn("generation", action)
        self.assertEqual(
            action["input_authority"]["cutoff"]["feature_date"],
            action["feature_date"],
        )
        self.assertEqual(
            baseline["schema_version"],
            "active-signal-gap-plan-v2",
        )

    def test_generation_reader_maps_the_full_migration_017_fence(self):
        from harness.signal_gap_plan import _read_input_generations

        expected = _native_generation()
        row = {
            field: getattr(expected, field)
            for field in expected.__dataclass_fields__
        }
        row["sealed_at"] = expected.sealed_at.replace("T", " ")
        connection = _Connection(
            (("FROM t_input_generations", [row]),)
        )

        generations = _read_input_generations(connection)

        self.assertEqual(generations, (expected,))
        sql = connection.statements[0]
        self.assertIn("readiness_basis", sql)
        self.assertIn("sealed_at", sql)
        self.assertNotIn("cutoff_feature_dates", sql)

    def test_top_level_plan_passes_one_explicit_readonly_config(self):
        from harness.signal_gap_plan import plan_signal_gaps

        config = _readonly_databridge_config()
        snapshot = _fixture_snapshot()
        connection = _PlanConnection()
        seen = []

        def reader(
            received_connection,
            *,
            start_date,
            as_of_date,
            execution_authority,
            discovery_identity_sha256,
            databridge_config,
        ):
            del (
                start_date,
                as_of_date,
                execution_authority,
                discovery_identity_sha256,
            )
            seen.append((received_connection, databridge_config))
            return snapshot

        with patch(
            "harness.signal_gap_plan.DataBridgeRefreshConfig.from_env",
            side_effect=AssertionError("unexpected config fallback"),
        ):
            plan = plan_signal_gaps(
                _PlanEngine(connection),
                start_date="2025-01-01",
                as_of_date="2026-07-27",
                snapshot_reader=reader,
                execution_authority=(),
                databridge_config=config,
            )

        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], connection)
        self.assertIs(seen[0][1], config)
        self.assertEqual(plan["counts"]["open_gap"], 29)
        self.assertNotIn(
            "/trusted/bond-factor-lab",
            json.dumps(plan, sort_keys=True),
        )
        self.assertEqual(connection.rollback_count, 1)

    def test_reader_maps_typed_current_errors_and_ignores_old_ledger(self):
        from harness.signal_gap_plan import read_signal_gap_snapshot
        from shared.data_bridge.refresh import (
            DataBridgeCurrentInvalidError,
            DataBridgeCurrentMissingError,
        )

        fixture = _fixture_snapshot()
        target = next(
            item
            for item in fixture.registry_targets
            if item.runtime_type == "blackbox_v2"
        )
        case = next(
            item
            for item in fixture.expected_cases
            if item.base_scheme_id == target.base_scheme_id
        )
        old_ledger = replace(
            _native_generation(),
            generation_id="old-databridge",
            generation_type="databridge_v1",
            native_generation_id="native-parent",
            native_manifest_sha256=_sha("native-parent"),
        )
        rows = [{"rdate": "2026-07-24", "trade_flag": "1"}]
        calendar_snapshot = {
            "t_trade_calendar.csv": _Frame(rows),
            "api_wind_date.csv": _Frame(
                [{**rows[0], "week_id": 202630}]
            ),
        }
        config = _readonly_databridge_config()

        for exception, error, expected_action in (
            (
                DataBridgeCurrentMissingError("missing"),
                "MISSING",
                "BLOCKED_NO_GENERATION",
            ),
            (
                DataBridgeCurrentInvalidError("invalid"),
                "INVALID",
                "BLOCKED_DATA_CONTRACT",
            ),
        ):
            with self.subTest(error=error):
                connection = _Connection(
                    (
                        (
                            "SELECT DATABASE()",
                            [
                                {
                                    "database_name": "fixture",
                                    "server_uuid": "fixture-server",
                                    "server_port": 3306,
                                }
                            ],
                        ),
                    )
                )
                with (
                    patch(
                        "harness.signal_gap_plan._read_registry_versions",
                        return_value=(
                            (
                                {
                                    "scheme_id":
                                        target.registry_scheme_id,
                                    "base_scheme_id":
                                        target.base_scheme_id,
                                },
                            ),
                            (target,),
                            (),
                            _sha("versions"),
                        ),
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "read_calendar_snapshot_from_connection",
                        return_value=calendar_snapshot,
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "build_expected_canonical_cases",
                        return_value=(),
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "_read_canonical_actual_facts",
                        return_value=((), {}),
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "_read_persisted_canonical_observations",
                        return_value=((), (), {}, (), ()),
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "build_expected_live_cases",
                        return_value=(case,),
                    ),
                    patch(
                        "harness.signal_gap_plan._read_live_signals",
                        return_value=([], ()),
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "_read_input_generations",
                        return_value=(old_ledger,),
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "resolve_stable_databridge_current_authority",
                        side_effect=exception,
                    ),
                    patch(
                        "harness.signal_gap_plan."
                        "DataBridgeRefreshConfig.from_env",
                        side_effect=AssertionError(
                            "unexpected config fallback"
                        ),
                    ),
                ):
                    snapshot = read_signal_gap_snapshot(
                        connection,
                        start_date="2025-01-01",
                        as_of_date="2026-07-27",
                        execution_authority=(),
                        discovery_identity_sha256=_sha("discovery"),
                        databridge_config=config,
                    )

                self.assertEqual(
                    snapshot.databridge_authority_error,
                    error,
                )
                self.assertEqual(
                    self._plan(snapshot)["actions"][0]["action"],
                    expected_action,
                )

    def test_snapshot_reader_calls_stable_current_once_on_connection(self):
        from harness.signal_gap_plan import read_signal_gap_snapshot

        fixture = _fixture_snapshot()
        target = next(
            item
            for item in fixture.registry_targets
            if item.base_scheme_id
            == "ten_y_t5_maj3_k3_ic_static_v1"
        )
        cases = tuple(
            item
            for item in fixture.expected_cases
            if item.base_scheme_id == target.base_scheme_id
        )[:3]
        self.assertEqual(len(cases), 3)
        connection = _Connection(
            (
                (
                    "SELECT DATABASE()",
                    [
                        {
                            "database_name": "fixture",
                            "server_uuid": "fixture-server",
                            "server_port": 3306,
                        }
                    ],
                ),
            )
        )
        rows = [
            {"rdate": "2026-07-24", "trade_flag": "1"}
        ]
        calendar_snapshot = {
            "t_trade_calendar.csv": _Frame(rows),
            "api_wind_date.csv": _Frame(
                [{**rows[0], "week_id": 202630}]
            ),
        }
        authority = _current_authority(refresh_date="2026-07-24")
        config = _readonly_databridge_config()
        with (
            patch(
                "harness.signal_gap_plan._read_registry_versions",
                return_value=(
                    (
                        {
                            "scheme_id": target.registry_scheme_id,
                            "base_scheme_id": target.base_scheme_id,
                        },
                    ),
                    (target,),
                    (),
                    _sha("versions"),
                ),
            ),
            patch(
                "harness.signal_gap_plan."
                "read_calendar_snapshot_from_connection",
                return_value=calendar_snapshot,
            ),
            patch(
                "harness.signal_gap_plan.build_expected_canonical_cases",
                return_value=(),
            ),
            patch(
                "harness.signal_gap_plan._read_canonical_actual_facts",
                return_value=((), {}),
            ),
            patch(
                "harness.signal_gap_plan."
                "_read_persisted_canonical_observations",
                return_value=((), (), {}, (), ()),
            ),
            patch(
                "harness.signal_gap_plan.build_expected_live_cases",
                return_value=cases,
            ),
            patch(
                "harness.signal_gap_plan._read_live_signals",
                return_value=([], ()),
            ),
            patch(
                "harness.signal_gap_plan._read_input_generations",
                return_value=(),
            ),
            patch(
                "harness.signal_gap_plan."
                "resolve_stable_databridge_current_authority",
                return_value=authority,
            ) as reader,
            patch(
                "harness.signal_gap_plan."
                "DataBridgeRefreshConfig.from_env",
                side_effect=AssertionError("unexpected config fallback"),
            ),
            patch(
                "scheduler.repository.create_engine_from_env",
                side_effect=AssertionError("unexpected scheduler engine"),
            ),
            patch(
                "backtests.repository.create_engine_from_env",
                side_effect=AssertionError("unexpected backtest engine"),
            ),
            patch(
                "sqlalchemy.engine.base.Engine.__init__",
                side_effect=AssertionError("unexpected SQLAlchemy engine"),
            ),
        ):
            snapshot = read_signal_gap_snapshot(
                connection,
                start_date="2025-01-01",
                as_of_date="2026-07-27",
                execution_authority=(),
                discovery_identity_sha256=_sha("discovery"),
                databridge_config=config,
            )

        reader.assert_called_once()
        self.assertIs(reader.call_args.args[0], config)
        self.assertIs(
            reader.call_args.kwargs["connection"],
            connection,
        )
        self.assertEqual(
            reader.call_args.kwargs["feature_dates"],
            tuple(sorted({case.feature_date for case in cases})),
        )
        self.assertEqual(snapshot.databridge_authority, authority)

    def test_snapshot_reader_skips_current_when_all_blackbox_are_present(
        self,
    ):
        from harness.signal_gap_plan import (
            ObservedSignal,
            read_signal_gap_snapshot,
        )

        fixture = _fixture_snapshot()
        target = next(
            item
            for item in fixture.registry_targets
            if item.base_scheme_id
            == "ten_y_t5_maj3_k3_ic_static_v1"
        )
        cases = tuple(
            item
            for item in fixture.expected_cases
            if item.base_scheme_id == target.base_scheme_id
        )[:3]
        signals = tuple(
            ObservedSignal(
                base_scheme_id=case.base_scheme_id,
                target_tenor=case.target_tenor,
                horizon=case.horizon,
                target_date=case.target_date,
                predict_date=case.predict_date,
                feature_date=case.feature_date,
                phase="gray_live",
                scheme_version=target.scheme_version,
                run_status="success",
            )
            for case in cases
        )
        connection = _Connection(
            (
                (
                    "SELECT DATABASE()",
                    [
                        {
                            "database_name": "fixture",
                            "server_uuid": "fixture-server",
                            "server_port": 3306,
                        }
                    ],
                ),
            )
        )
        rows = [{"rdate": "2026-07-24", "trade_flag": "1"}]
        calendar_snapshot = {
            "t_trade_calendar.csv": _Frame(rows),
            "api_wind_date.csv": _Frame(
                [{**rows[0], "week_id": 202630}]
            ),
        }
        config = _readonly_databridge_config()
        with (
            patch(
                "harness.signal_gap_plan._read_registry_versions",
                return_value=(
                    (
                        {
                            "scheme_id": target.registry_scheme_id,
                            "base_scheme_id": target.base_scheme_id,
                        },
                    ),
                    (target,),
                    (),
                    _sha("versions"),
                ),
            ),
            patch(
                "harness.signal_gap_plan."
                "read_calendar_snapshot_from_connection",
                return_value=calendar_snapshot,
            ),
            patch(
                "harness.signal_gap_plan.build_expected_canonical_cases",
                return_value=(),
            ),
            patch(
                "harness.signal_gap_plan._read_canonical_actual_facts",
                return_value=((), {}),
            ),
            patch(
                "harness.signal_gap_plan."
                "_read_persisted_canonical_observations",
                return_value=((), (), {}, (), ()),
            ),
            patch(
                "harness.signal_gap_plan.build_expected_live_cases",
                return_value=cases,
            ),
            patch(
                "harness.signal_gap_plan._read_live_signals",
                return_value=(list(signals), signals),
            ),
            patch(
                "harness.signal_gap_plan._read_input_generations",
                return_value=(),
            ),
            patch(
                "harness.signal_gap_plan."
                "resolve_stable_databridge_current_authority",
            ) as reader,
            patch(
                "harness.signal_gap_plan."
                "DataBridgeRefreshConfig.from_env",
                side_effect=AssertionError("unexpected config fallback"),
            ),
            patch(
                "scheduler.repository.create_engine_from_env",
                side_effect=AssertionError("unexpected scheduler engine"),
            ),
            patch(
                "backtests.repository.create_engine_from_env",
                side_effect=AssertionError("unexpected backtest engine"),
            ),
            patch(
                "sqlalchemy.engine.base.Engine.__init__",
                side_effect=AssertionError("unexpected SQLAlchemy engine"),
            ),
        ):
            snapshot = read_signal_gap_snapshot(
                connection,
                start_date="2025-01-01",
                as_of_date="2026-07-27",
                execution_authority=(),
                discovery_identity_sha256=_sha("discovery"),
                databridge_config=config,
            )

        reader.assert_not_called()
        self.assertIsNone(snapshot.databridge_authority)
        self.assertIsNone(snapshot.databridge_authority_error)


class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError(orient)
        return list(self.rows)


if __name__ == "__main__":
    unittest.main()
