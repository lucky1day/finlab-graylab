from __future__ import annotations

import gc
import hashlib
import json
import shutil
import tempfile
import time
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from harness.signal_gap_plan import (
    ExpectedSignalCase,
    InputGeneration,
    ObservedSignal,
    RegistryTarget,
    SignalGapSnapshot,
    build_signal_gap_plan,
)
from shared import native_input_generation as native_module
from tests.test_native_input_generation import (
    _create_generation,
    _make_generation_writable,
    _rewrite_manifest,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _generation_row(context, **changes: object) -> InputGeneration:
    values: dict[str, object] = {
        "generation_id": context.generation_id,
        "generation_type": context.generation_type,
        "business_date": context.business_date,
        "feature_date": context.feature_date,
        "readiness_basis": context.readiness_basis,
        "source_commit_token": context.source_commit_token,
        "dataset_content_id": context.dataset_content_id,
        "schema_version": context.schema_version,
        "exporter_version": context.exporter_version,
        "manifest_uri": str(context.manifest_path.resolve()),
        "manifest_sha256": context.manifest_sha256,
        "native_generation_id": None,
        "native_manifest_sha256": None,
        "state": "SEALED",
        # 数据库使用 ledger trusted clock，不要求等于 manifest sealed_at。
        "sealed_at": "2026-07-24T07:00:00.000000",
    }
    values.update(changes)
    return InputGeneration(**values)


def _target(index: int, *, input_mode: str = "generation_v1"):
    tenor = ("1Y", "5Y", "10Y")[index]
    base = f"native_artifact_demo_{index}"
    return RegistryTarget(
        registry_scheme_id=f"{base}__h5__{tenor}",
        base_scheme_id=base,
        runtime_type="native_adapter",
        frequency="daily",
        task_type="T+5",
        target_tenor=tenor,
        horizon=5,
        scheme_version=f"{base}-v1",
        live_target_start_date="2026-06-01",
        live_boundary_source="platform_live_boundary_v1",
        input_mode=input_mode,
        code_sha256=_sha(f"{base}:code"),
        config_sha256=_sha(f"{base}:config"),
        source_package_sha256=(
            _sha(f"{base}:source-package")
            if input_mode == "live_source_0629"
            else None
        ),
    )


def _case(
    target: RegistryTarget,
    *,
    segment: str = "live",
    data_contract_error: str | None = None,
):
    return ExpectedSignalCase(
        registry_scheme_id=target.registry_scheme_id,
        base_scheme_id=target.base_scheme_id,
        runtime_type=target.runtime_type,
        frequency=target.frequency,
        task_type=target.task_type,
        target_tenor=target.target_tenor,
        horizon=target.horizon,
        predict_date="2026-07-24",
        feature_date="2026-07-24",
        target_date="2026-07-31",
        segment=segment,
        data_contract_error=data_contract_error,
    )


def _snapshot(
    generation_rows: tuple[InputGeneration, ...],
    *,
    target_count: int = 2,
    input_mode: str = "generation_v1",
    segment: str = "live",
    live_signals: tuple[ObservedSignal, ...] = (),
    control_plane_blockers: tuple[dict[str, str], ...] = (),
    data_contract_error: str | None = None,
) -> SignalGapSnapshot:
    targets = tuple(
        _target(index, input_mode=input_mode)
        for index in range(target_count)
    )
    return SignalGapSnapshot(
        registry_targets=targets,
        expected_cases=tuple(
            _case(
                target,
                segment=segment,
                data_contract_error=data_contract_error,
            )
            for target in targets
        ),
        canonical_signals=(),
        live_signals=live_signals,
        input_generations=generation_rows,
        input_watermarks={},
        source_identity_sha256=_sha("source"),
        discovery_identity_sha256=_sha("discovery"),
        active_version_identity_sha256=_sha("versions"),
        control_plane_blockers=control_plane_blockers,
    )


def _plan(snapshot: SignalGapSnapshot) -> dict[str, object]:
    return build_signal_gap_plan(
        snapshot,
        start_date="2025-01-01",
        as_of_date="2026-07-24",
    )


def _manifest_row(context) -> InputGeneration:
    raw = context.manifest_path.read_bytes()
    manifest = json.loads(raw)
    return _generation_row(
        context,
        generation_id=manifest["generation_id"],
        source_commit_token=manifest["source_commit_token"],
        dataset_content_id=manifest["dataset_content_id"],
        readiness_basis=manifest["readiness_basis"],
        schema_version=manifest["schema_version"],
        exporter_version=manifest["exporter_version"],
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


class SignalGapNativeArtifactAuthorityTests(unittest.TestCase):
    def test_real_artifact_is_opened_once_per_plan_and_not_retained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(native_module, tmpdir)
            row = _generation_row(context)
            opened_refs: list[weakref.ReferenceType[object]] = []
            real_open = native_module.open_native_generation

            def tracking_open(*args: object, **kwargs: object):
                opened = real_open(*args, **kwargs)
                opened_refs.append(weakref.ref(opened))
                return opened

            with patch(
                "harness.signal_gap_plan.open_native_generation",
                side_effect=tracking_open,
            ) as opener:
                started_at = time.perf_counter()
                first = _plan(_snapshot((row,)))
                validation_duration = time.perf_counter() - started_at
                validation_evidence = {
                    "distinct_artifact_open_count": opener.call_count,
                    "total_validation_duration_seconds":
                        validation_duration,
                }
                self.assertEqual(
                    validation_evidence[
                        "distinct_artifact_open_count"
                    ],
                    1,
                )
                self.assertGreaterEqual(
                    validation_evidence[
                        "total_validation_duration_seconds"
                    ],
                    0.0,
                )
                self.assertEqual(opener.call_count, 1)
                actions = first["actions"]
                self.assertEqual(
                    {action["action"] for action in actions},
                    {"GRAY_LIVE_GAP"},
                )
                self.assertEqual(
                    actions[0]["input_authority"],
                    actions[1]["input_authority"],
                )
                authority = actions[0]["input_authority"]
                self.assertEqual(
                    set(authority),
                    {"database", "artifact"},
                )
                self.assertEqual(
                    authority["database"]["sealed_at"],
                    row.sealed_at,
                )
                self.assertEqual(
                    authority["artifact"]["sealed_at"],
                    context.sealed_at,
                )
                self.assertEqual(
                    authority["database"],
                    {
                        field: getattr(row, field)
                        for field in row.__dataclass_fields__
                    },
                )
                self.assertEqual(
                    authority["artifact"],
                    {
                        "generation_id": context.generation_id,
                        "generation_type": context.generation_type,
                        "business_date": context.business_date,
                        "feature_date": context.feature_date,
                        "readiness_basis": context.readiness_basis,
                        "source_commit_token": context.source_commit_token,
                        "dataset_content_id": context.dataset_content_id,
                        "schema_version": context.schema_version,
                        "exporter_version": context.exporter_version,
                        "manifest_uri": str(
                            context.manifest_path.resolve()
                        ),
                        "manifest_sha256": context.manifest_sha256,
                        "sealed_at": context.sealed_at,
                    },
                )
                self.assertNotEqual(
                    row.sealed_at,
                    context.sealed_at,
                )
                self.assertEqual(
                    opener.call_args.kwargs,
                    {
                        "expected_generation_id": row.generation_id,
                        "expected_manifest_sha256":
                            row.manifest_sha256,
                        "expected_business_date": row.business_date,
                        "expected_feature_date": row.feature_date,
                    },
                )

                gc.collect()
                self.assertIsNone(opened_refs[0]())

                second = _plan(_snapshot((row,)))
                self.assertEqual(opener.call_count, 2)
                self.assertEqual(
                    first["plan_sha256"],
                    second["plan_sha256"],
                )
                gc.collect()
                self.assertIsNone(opened_refs[1]())

    def test_bad_artifact_failure_is_memoized_then_new_plan_reopens(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(native_module, tmpdir)
            row = _generation_row(context)
            _make_generation_writable(context.root_dir)
            csv_path = context.root_dir / "api_wind_daily.csv"
            original = csv_path.read_bytes()
            csv_path.write_bytes(original + b"\n")

            with patch(
                "harness.signal_gap_plan.open_native_generation",
                wraps=native_module.open_native_generation,
            ) as opener:
                failed = _plan(_snapshot((row,)))
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(
                    {
                        (
                            action["action"],
                            action["reason"],
                        )
                        for action in failed["actions"]
                    },
                    {
                        (
                            "BLOCKED_DATA_CONTRACT",
                            "NATIVE_GENERATION_ARTIFACT_INVALID",
                        )
                    },
                )

                csv_path.write_bytes(original)
                recovered = _plan(_snapshot((row,)))
                self.assertEqual(opener.call_count, 2)
                self.assertEqual(
                    {action["action"] for action in recovered["actions"]},
                    {"GRAY_LIVE_GAP"},
                )

    def test_removed_artifact_is_reopened_after_real_restoration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(native_module, tmpdir)
            row = _generation_row(context)
            _make_generation_writable(context.root_dir)
            csv_path = context.root_dir / "api_wind_daily.csv"
            original = csv_path.read_bytes()
            csv_path.unlink()

            with patch(
                "harness.signal_gap_plan.open_native_generation",
                wraps=native_module.open_native_generation,
            ) as opener:
                failed = _plan(_snapshot((row,), target_count=1))
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(
                    failed["actions"][0]["reason"],
                    "NATIVE_GENERATION_ARTIFACT_INVALID",
                )

                csv_path.write_bytes(original)
                recovered = _plan(
                    _snapshot((row,), target_count=1)
                )
                self.assertEqual(opener.call_count, 2)
                self.assertEqual(
                    recovered["actions"][0]["action"],
                    "GRAY_LIVE_GAP",
                )

    def test_different_real_artifact_authorities_change_plan_sha(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first_root = Path(tmpdir) / "first"
            second_root = Path(tmpdir) / "second"
            _, first_context = _create_generation(
                native_module,
                first_root,
                source_commit_token="a" * 64,
            )
            _, second_context = _create_generation(
                native_module,
                second_root,
                source_commit_token="b" * 64,
            )

            first = _plan(
                _snapshot(
                    (_generation_row(first_context),),
                    target_count=1,
                )
            )
            second = _plan(
                _snapshot(
                    (_generation_row(second_context),),
                    target_count=1,
                )
            )

            self.assertEqual(
                first["actions"][0]["action"],
                "GRAY_LIVE_GAP",
            )
            self.assertEqual(
                second["actions"][0]["action"],
                "GRAY_LIVE_GAP",
            )
            self.assertNotEqual(
                first["actions"][0]["input_authority"],
                second["actions"][0]["input_authority"],
            )
            self.assertNotEqual(
                first["plan_sha256"],
                second["plan_sha256"],
            )

    def test_real_artifact_tampering_is_never_actionable(self) -> None:
        def csv_byte(context, _tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            path = context.root_dir / "api_wind_daily.csv"
            path.write_bytes(path.read_bytes() + b"\n")
            return _generation_row(context)

        def manifest_byte(context, _tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            context.manifest_path.write_bytes(
                context.manifest_path.read_bytes() + b"\n"
            )
            return _generation_row(context)

        def manifest_symlink(context, tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            raw = context.manifest_path.read_bytes()
            outside = Path(tmpdir) / "manifest-outside.json"
            outside.write_bytes(raw)
            context.manifest_path.unlink()
            context.manifest_path.symlink_to(outside)
            return _generation_row(
                context,
                manifest_uri=str(context.manifest_path.absolute()),
            )

        def csv_symlink(context, tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            path = context.root_dir / "api_wind_daily.csv"
            outside = Path(tmpdir) / "daily-outside.csv"
            outside.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(outside)
            return _generation_row(context)

        def unexpected_entry(context, _tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            (context.root_dir / "unexpected.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )
            return _generation_row(context)

        def cutoff(context, _tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(
                context,
                lambda manifest: manifest["cutoffs"].__setitem__(
                    "weekly",
                    "202630",
                ),
            )
            return _manifest_row(context)

        def source_evidence(context, _tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(
                context,
                lambda manifest: manifest["source_evidence"][
                    "tables"
                ][0].__setitem__("row_count", 999),
            )
            return _manifest_row(context)

        def schema(context, _tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(
                context,
                lambda manifest: manifest.__setitem__(
                    "schema_version",
                    "native-generation-v2",
                ),
            )
            return _manifest_row(context)

        def row_metadata(context, _tmpdir: str) -> InputGeneration:
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(
                context,
                lambda manifest: manifest["files"][
                    "api_wind_daily.csv"
                ].__setitem__(
                    "row_count",
                    manifest["files"]["api_wind_daily.csv"][
                        "row_count"
                    ]
                    + 1,
                ),
            )
            return _manifest_row(context)

        for label, mutate in (
            ("csv byte", csv_byte),
            ("manifest byte", manifest_byte),
            ("manifest symlink", manifest_symlink),
            ("csv symlink", csv_symlink),
            ("unexpected entry", unexpected_entry),
            ("cutoff", cutoff),
            ("source evidence", source_evidence),
            ("schema", schema),
            ("row metadata", row_metadata),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmpdir:
                    _, context = _create_generation(
                        native_module,
                        tmpdir,
                    )
                    row = mutate(context, tmpdir)
                    with patch(
                        "harness.signal_gap_plan.open_native_generation",
                        wraps=native_module.open_native_generation,
                    ) as opener:
                        action = _plan(
                            _snapshot((row,), target_count=1)
                        )["actions"][0]
                    self.assertEqual(opener.call_count, 1)
                    self.assertEqual(
                        (
                            action["action"],
                            action["reason"],
                        ),
                        (
                            "BLOCKED_DATA_CONTRACT",
                            "NATIVE_GENERATION_ARTIFACT_INVALID",
                        ),
                    )

    def test_database_context_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(native_module, tmpdir)
            baseline = _generation_row(context)
            for field, value in (
                ("source_commit_token", _sha("different-source")),
                ("dataset_content_id", _sha("different-dataset")),
                ("readiness_basis", "UPSTREAM_SEAL"),
                ("schema_version", "native-generation-v9"),
                ("exporter_version", "native-exporter-v9"),
            ):
                with self.subTest(field=field):
                    action = _plan(
                        _snapshot(
                            (replace(baseline, **{field: value}),),
                            target_count=1,
                        )
                    )["actions"][0]
                    self.assertEqual(
                        (
                            action["action"],
                            action["reason"],
                        ),
                        (
                            "BLOCKED_DATA_CONTRACT",
                            "NATIVE_GENERATION_DB_CONTEXT_DRIFT",
                        ),
                    )

            changed_hash = replace(
                baseline,
                manifest_sha256=_sha("different-manifest"),
            )
            changed_hash_action = _plan(
                _snapshot((changed_hash,), target_count=1)
            )["actions"][0]
            self.assertEqual(
                (
                    changed_hash_action["action"],
                    changed_hash_action["reason"],
                ),
                (
                    "BLOCKED_DATA_CONTRACT",
                    "NATIVE_GENERATION_ARTIFACT_INVALID",
                ),
            )

            fake_uri = str(Path(tmpdir) / "different" / "manifest.json")
            Path(fake_uri).parent.mkdir()
            Path(fake_uri).write_text("placeholder", encoding="utf-8")

            def open_original(*_args: object, **_kwargs: object):
                return native_module.open_native_generation(
                    context.manifest_path,
                    expected_generation_id=context.generation_id,
                    expected_manifest_sha256=context.manifest_sha256,
                    expected_business_date=context.business_date,
                    expected_feature_date=context.feature_date,
                )

            with patch(
                "harness.signal_gap_plan.open_native_generation",
                side_effect=open_original,
            ):
                uri_action = _plan(
                    _snapshot(
                        (
                            replace(
                                baseline,
                                manifest_uri=fake_uri,
                            ),
                        ),
                        target_count=1,
                    )
                )["actions"][0]
            self.assertEqual(
                (
                    uri_action["action"],
                    uri_action["reason"],
                ),
                (
                    "BLOCKED_DATA_CONTRACT",
                    "NATIVE_GENERATION_DB_CONTEXT_DRIFT",
                ),
            )

    def test_invalid_database_fence_and_precedence_do_not_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(native_module, tmpdir)
            baseline = _generation_row(context)
            invalid_rows = (
                replace(baseline, source_commit_token="not-a-digest"),
                replace(baseline, dataset_content_id="not-a-digest"),
                replace(
                    baseline,
                    generation_id="native-ABCDEF0123456789ABCDEF01",
                ),
                replace(
                    baseline,
                    manifest_uri=str(
                        context.root_dir / "not-manifest.json"
                    ),
                ),
                replace(baseline, state="BUILDING"),
                replace(baseline, sealed_at=None),
                replace(baseline, native_generation_id="parent"),
            )
            for row in invalid_rows:
                with self.subTest(row=row):
                    with patch(
                        "harness.signal_gap_plan.open_native_generation"
                    ) as opener:
                        action = _plan(
                            _snapshot((row,), target_count=1)
                        )["actions"][0]
                    opener.assert_not_called()
                    self.assertEqual(
                        action["reason"],
                        "GENERATION_CONTRACT_INVALID",
                    )

            with patch(
                "harness.signal_gap_plan.open_native_generation"
            ) as opener:
                opener.return_value = context
                missing = _plan(_snapshot((), target_count=1))
                duplicate = _plan(
                    _snapshot(
                        (baseline, replace(baseline)),
                        target_count=1,
                    )
                )
                canonical_snapshot = _snapshot(
                    (baseline,),
                    target_count=1,
                    segment="canonical",
                )
                canonical = _plan(
                    replace(
                        canonical_snapshot,
                        expected_cases=(
                            replace(
                                canonical_snapshot.expected_cases[0],
                                predict_date="2025-05-20",
                                feature_date="2025-05-20",
                                target_date="2025-05-27",
                            ),
                        ),
                    )
                )
                blocked = _plan(
                    _snapshot(
                        (baseline,),
                        target_count=1,
                        control_plane_blockers=(
                            {
                                "base_scheme_id":
                                    "native_artifact_demo_0",
                                "code": "POLICY_DRIFT",
                            },
                        ),
                    )
                )
                contract = _plan(
                    _snapshot(
                        (baseline,),
                        target_count=1,
                        data_contract_error="SOURCE_CONTRACT_INVALID",
                    )
                )
                source_0629 = _plan(
                    _snapshot(
                        (baseline,),
                        target_count=1,
                        input_mode="live_source_0629",
                    )
                )
                target = _target(0)
                case = _case(target)
                present = ObservedSignal(
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
                present_plan = _plan(
                    _snapshot(
                        (baseline,),
                        target_count=1,
                        live_signals=(present,),
                    )
                )
                drifted_present = replace(
                    present,
                    scheme_version="wrong-version",
                )
                observation_drift = _plan(
                    _snapshot(
                        (baseline,),
                        target_count=1,
                        live_signals=(drifted_present,),
                    )
                )
            opener.assert_called_once()
            self.assertEqual(
                [
                    missing["actions"][0]["reason"],
                    duplicate["actions"][0]["reason"],
                    canonical["actions"][0]["reason"],
                    blocked["actions"][0]["reason"],
                    contract["actions"][0]["reason"],
                    source_0629["actions"][0]["reason"],
                    present_plan["actions"][0]["reason"],
                    observation_drift["actions"][0]["reason"],
                ],
                [
                    "NO_EXACT_NATIVE_GENERATION",
                    "DUPLICATE_EXACT_NATIVE_GENERATION",
                    "CANONICAL_BUSINESS_KEY_MISSING",
                    "CONTROL_PLANE_BLOCKER:POLICY_DRIFT",
                    "SOURCE_CONTRACT_INVALID",
                    "LIVE_BUSINESS_KEY_MISSING",
                    "BUSINESS_KEY_PRESENT",
                    "OBSERVED_SIGNAL_CONTRACT_DRIFT",
                ],
            )

    def test_same_generation_id_with_different_envelopes_never_opens(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as first_dir:
            with tempfile.TemporaryDirectory() as second_dir:
                _, context = _create_generation(native_module, first_dir)
                first = _generation_row(context)
                second_root = Path(second_dir) / context.generation_id
                shutil.copytree(context.root_dir, second_root)
                second = replace(
                    first,
                    business_date="2026-07-25",
                    feature_date="2026-07-25",
                    manifest_uri=str(second_root / "manifest.json"),
                )
                target_a = _target(0)
                target_b = _target(1)
                case_a = _case(target_a)
                case_b = replace(
                    _case(target_b),
                    predict_date="2026-07-25",
                    feature_date="2026-07-25",
                    target_date="2026-08-03",
                )
                snapshot = replace(
                    _snapshot((first, second)),
                    registry_targets=(target_a, target_b),
                    expected_cases=(case_a, case_b),
                )
                with patch(
                    "harness.signal_gap_plan.open_native_generation",
                    wraps=native_module.open_native_generation,
                ) as opener:
                    plan = build_signal_gap_plan(
                        snapshot,
                        start_date="2025-01-01",
                        as_of_date="2026-07-25",
                    )
                opener.assert_not_called()
                self.assertEqual(
                    {
                        (row["action"], row["reason"])
                        for row in plan["actions"]
                    },
                    {
                        (
                            "BLOCKED_DATA_CONTRACT",
                            "NATIVE_GENERATION_DB_CONTEXT_DRIFT",
                        )
                    },
                )

    def test_unexpected_opener_exceptions_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(native_module, tmpdir)
            row = _generation_row(context)
            for error in (
                RuntimeError("runtime"),
                TypeError("type"),
                KeyboardInterrupt(),
            ):
                with self.subTest(error=type(error).__name__):
                    with patch(
                        "harness.signal_gap_plan.open_native_generation",
                        side_effect=error,
                    ):
                        with self.assertRaises(type(error)):
                            _plan(_snapshot((row,), target_count=1))
