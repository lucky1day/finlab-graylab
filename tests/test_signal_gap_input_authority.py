from __future__ import annotations

import hashlib
import inspect
import json
import unittest
from dataclasses import replace
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


def _fixture_rows():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


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


def _case(row, targets):
    from harness.signal_gap_plan import ExpectedSignalCase

    target = targets[
        (
            row["base_scheme_id"],
            row["target_tenor"],
            row["horizon"],
        )
    ]
    return ExpectedSignalCase(
        registry_scheme_id=target.registry_scheme_id,
        base_scheme_id=target.base_scheme_id,
        runtime_type=target.runtime_type,
        frequency=target.frequency,
        task_type=target.task_type,
        target_tenor=target.target_tenor,
        horizon=target.horizon,
        predict_date=row["predict_date"],
        feature_date=row["feature_date"],
        target_date=row["target_date"],
        segment="live",
        data_contract_error=(
            "WEEK_CALENDAR_CONTRACT:202625"
            if target.base_scheme_id
            == "weekly_10y_d_overlay_0529"
            else None
        ),
    )


def _current_authority(
    *,
    refresh_date="2026-07-22",
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
            _case(row, targets) for row in rows
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
        "generation_id": "native-20260727",
        "generation_type": "native_source",
        "business_date": "2026-07-27",
        "feature_date": "2026-07-24",
        "readiness_basis": "CLOCK_CONTRACT",
        "source_commit_token": _sha("native-source"),
        "dataset_content_id": _sha("native-dataset"),
        "schema_version": "native-generation-v1",
        "exporter_version": "native-exporter-v1",
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

    def test_newer_current_unlocks_all_blackbox_gaps_only(self):
        plan = self._plan(
            _fixture_snapshot(
                authority=_current_authority(
                    refresh_date="2026-07-24"
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
        snapshot = replace(
            _fixture_snapshot(
                authority=None,
                authority_error="MISSING",
            ),
            input_generations=(old_row,),
        )

        plan = self._plan(snapshot)

        self.assertEqual(plan["counts"]["GRAY_LIVE_GAP"], 0)
        self.assertEqual(plan["counts"]["BLOCKED_NO_GENERATION"], 25)

    def test_native_exact_fence_validation_fails_closed(self):
        valid = self._plan(
            _native_snapshot(generations=(_native_generation(),))
        )
        self.assertEqual(valid["actions"][0]["action"], "GRAY_LIVE_GAP")
        self.assertEqual(
            set(valid["actions"][0]["input_authority"]),
            {
                "generation_id",
                "generation_type",
                "business_date",
                "feature_date",
                "readiness_basis",
                "source_commit_token",
                "dataset_content_id",
                "schema_version",
                "exporter_version",
                "manifest_uri",
                "manifest_sha256",
                "native_generation_id",
                "native_manifest_sha256",
                "state",
                "sealed_at",
            },
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

    def test_live_source_0629_is_never_automatically_actionable(self):
        from harness.signal_gap_plan import ObservedSignal

        snapshot = _native_snapshot(
            generations=(_native_generation(),),
            input_mode="live_source_0629",
        )
        plan = self._plan(
            snapshot
        )

        self.assertEqual(
            plan["actions"][0]["action"],
            "BLOCKED_DATA_CONTRACT",
        )
        self.assertEqual(
            plan["actions"][0]["reason"],
            "LIVE_SOURCE_0629_ATTESTATION_REQUIRED",
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
        native_baseline = self._plan(
            _native_snapshot(generations=(_native_generation(),))
        )
        native_changed = self._plan(
            _native_snapshot(
                generations=(
                    _native_generation(
                        source_commit_token=_sha("changed-source"),
                    ),
                )
            )
        )
        self.assertNotEqual(
            native_baseline["plan_sha256"],
            native_changed["plan_sha256"],
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
        ):
            snapshot = read_signal_gap_snapshot(
                connection,
                start_date="2025-01-01",
                as_of_date="2026-07-27",
                execution_authority=(),
                discovery_identity_sha256=_sha("discovery"),
            )

        reader.assert_called_once()
        self.assertIs(
            reader.call_args.kwargs["connection"],
            connection,
        )
        self.assertEqual(
            reader.call_args.kwargs["feature_dates"],
            tuple(sorted({case.feature_date for case in cases})),
        )
        self.assertEqual(snapshot.databridge_authority, authority)
        source = inspect.getsource(read_signal_gap_snapshot)
        self.assertNotIn("create_engine", source)
        self.assertNotIn("tempfile", source)
        self.assertNotIn(".mkdir(", source)

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
        ):
            snapshot = read_signal_gap_snapshot(
                connection,
                start_date="2025-01-01",
                as_of_date="2026-07-27",
                execution_authority=(),
                discovery_identity_sha256=_sha("discovery"),
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
