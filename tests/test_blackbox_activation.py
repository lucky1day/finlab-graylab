from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.blackbox_v2.activation import activate_blackbox
from harness.context import GateContext
from harness.operation import build_direct_operation
from harness.result import GateStatus


def test_activate_dispatches_initial_and_revision_through_one_entry(tmp_path) -> None:
    ctx = GateContext(
        scheme_id="trial_10y",
        predict_date="activate",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
    )
    initial = SimpleNamespace(status="paused", version_status="shadow")
    revision = SimpleNamespace(status="active", version_status="active")
    with (
        patch("harness.blackbox_v2.activation._config", return_value=initial),
        patch(
            "harness.blackbox_v2.activation._activate_initial",
            return_value="initial-result",
        ) as initial_call,
    ):
        assert activate_blackbox(ctx) == "initial-result"
        initial_call.assert_called_once()
    with (
        patch("harness.blackbox_v2.activation._config", return_value=revision),
        patch(
            "harness.blackbox_v2.activation._activate_revision",
            return_value="revision-result",
        ) as revision_call,
    ):
        assert activate_blackbox(ctx) == "revision-result"
        revision_call.assert_called_once()


def test_revision_activation_uses_direct_operation_and_atomic_repository(tmp_path) -> None:
    cfg = SimpleNamespace(
        scheme_id="trial_10y",
        scheme_version="version-2",
        status="active",
        version_status="active",
        runtime_type="blackbox_v2",
        runtime_profile="blackbox-v2-v1",
        path=Path(tmp_path),
    )
    passed_run = SimpleNamespace(
        harness_run_id="hr-passed",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        generation_id="generation-1",
        data_snapshot_id="snapshot-1",
    )
    preflight = SimpleNamespace(
        prior_scheme_version="version-1",
        pending_scheme_versions=("version-pending",),
    )
    state = SimpleNamespace(
        scheme_id=cfg.scheme_id,
        scheme_version=cfg.scheme_version,
        version_status="active",
        registry_status="active",
        registry_scheme_ids=("trial_10y__h1__10Y",),
        environment_fingerprint="e" * 64,
        data_snapshot_id="snapshot-1",
        approved_by="operator",
        approved_at=None,
    )
    engine = SimpleNamespace(dispose=lambda: None)
    operation = build_direct_operation(
        cfg.scheme_id,
        "blackbox_activate",
        scheme_version=cfg.scheme_version,
        issued_by="operator",
    )
    ctx = GateContext(
        scheme_id=cfg.scheme_id,
        predict_date="activate",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        config=cfg,
        operation=operation,
        engine_factory=lambda: engine,
    )

    with (
        patch(
            "harness.blackbox_v2.activation.replace",
            side_effect=lambda value, **updates: SimpleNamespace(
                **{**vars(value), **updates}
            ),
        ),
        patch("harness.blackbox_v2.activation.lifecycle_operation_lock", return_value=nullcontext()),
        patch("harness.blackbox_v2.activation.assert_lifecycle_clear"),
        patch("harness.blackbox_v2.activation._reload_pinned_canonical", return_value=cfg),
        patch("harness.blackbox_v2.activation._verify_passed_all", return_value=passed_run),
        patch("harness.blackbox_v2.activation._environment_fingerprint", return_value="e" * 64),
        patch(
            "harness.blackbox_v2.activation.read_blackbox_revision_activation_preflight",
            return_value=preflight,
        ),
        patch(
            "harness.blackbox_v2.activation.activate_blackbox_revision",
            return_value=state,
        ) as activate_revision,
    ):
        result = activate_blackbox(ctx)

    assert result.status == GateStatus.PASSED
    evidence = {item.key: item.value for item in result.evidence}
    assert evidence["activation_mode"] == "revision"
    assert evidence["prior_scheme_version"] == "version-1"
    activate_revision.assert_called_once()
