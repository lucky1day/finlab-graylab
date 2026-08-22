from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from harness.authorization import issue_token, used_tokens_path
from harness.context import GateContext
from harness.result import GateStatus
from scheduler.discovery import load_scheme_config
from shared.scheme_lifecycle_state import (
    create_lifecycle_state,
    lifecycle_state_path,
    read_lifecycle_state,
)


SCHEME_ID = "weekly_1y_causal_v1_31_0_standalone"


class _Engine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _DisposeFailingEngine(_Engine):
    def dispose(self) -> None:
        self.disposed = True
        raise RuntimeError("forced dispose failure")


@pytest.fixture(autouse=True)
def _runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HARNESS_AUTH_SECRET", "bootstrap-test-secret")
    monkeypatch.setenv("BFL_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.delenv("BFL_DEPLOYMENT_TARGET", raising=False)


def _config(
    tmp_path: Path,
    *,
    status: str = "active",
    version_status: str = "active",
):
    source = Path(__file__).resolve().parents[1] / "schemes" / SCHEME_ID
    target = tmp_path / "schemes" / SCHEME_ID
    shutil.copytree(source, target)
    config_path = target / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["status"] = status
    raw["version_status"] = version_status
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return load_scheme_config(config_path)


def _passed_run(harness_run_id: str = "hr_exact") -> SimpleNamespace:
    return SimpleNamespace(harness_run_id=harness_run_id)


def _db_state(cfg, *, version_status: str = "active", registry_status: str = "active"):
    return SimpleNamespace(
        scheme_id=cfg.scheme_id,
        scheme_version=cfg.scheme_version,
        runtime_type="blackbox_v2",
        version_status=version_status,
        registry_status=registry_status,
        registry_scheme_ids=(f"{cfg.scheme_id}__h1__1Y",),
    )


def _context(tmp_path: Path, cfg, token: str, engine: _Engine) -> GateContext:
    return GateContext(
        scheme_id=cfg.scheme_id,
        predict_date="bootstrap",
        project_root=tmp_path,
        report_dir=tmp_path / "reports" / "bootstrap",
        config=cfg,
        authorization=token,
        engine_factory=lambda: engine,
    )


def _token(cfg, *, harness_run_id: str = "hr_exact") -> str:
    return issue_token(
        cfg.scheme_id,
        "blackbox_lifecycle_bootstrap",
        scheme_version=cfg.scheme_version,
        harness_run_id=harness_run_id,
        issued_by="migration-owner",
    )


def test_create_lifecycle_state_is_insert_only(tmp_path: Path) -> None:
    first = create_lifecycle_state(
        tmp_path,
        scheme_id="demo",
        scheme_version="v1",
        status="active",
        version_status="active",
        harness_run_id="hr_first",
    )
    original = first.read_bytes()

    with pytest.raises(FileExistsError):
        create_lifecycle_state(
            tmp_path,
            scheme_id="demo",
            scheme_version="v1",
            status="paused",
            version_status="draft",
            harness_run_id="hr_second",
        )

    assert first.read_bytes() == original


def test_bootstrap_writes_only_exact_active_overlay_after_read_only_preflight(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    token = _token(cfg)
    ctx = _context(tmp_path, cfg, token, engine)

    with (
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
            return_value=_passed_run(),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_blackbox_lifecycle_state",
            return_value=_db_state(cfg),
        ) as read_db,
    ):
        result = BlackboxLifecycleBootstrapGate().run(ctx)

    assert result.status == GateStatus.PASSED
    assert result.passed
    assert engine.disposed
    read_db.assert_called_once()
    record = read_lifecycle_state(tmp_path, cfg.scheme_id, cfg.scheme_version)
    assert record is not None
    assert (record.status, record.version_status) == ("active", "active")
    payload = json.loads(
        lifecycle_state_path(tmp_path, cfg.scheme_id).read_text(encoding="utf-8")
    )
    assert payload["harness_run_id"] == "hr_exact"
    assert used_tokens_path(tmp_path).is_file()
    journal_root = (
        tmp_path
        / "runtime"
        / "artifacts"
        / "blackbox-v2-lifecycle"
        / cfg.scheme_id
    )
    assert not list(journal_root.glob("*.json"))


@pytest.mark.parametrize(
    ("status", "version_status"),
    [("paused", "active"), ("active", "draft"), ("paused", "draft")],
)
def test_bootstrap_rejects_non_active_canonical_without_consuming_token(
    tmp_path: Path,
    status: str,
    version_status: str,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path, status=status, version_status=version_status)
    engine = _Engine()
    result = BlackboxLifecycleBootstrapGate().run(
        _context(tmp_path, cfg, _token(cfg), engine)
    )

    assert result.status == GateStatus.BLOCKED
    assert "canonical config active+active" in "\n".join(result.errors)
    assert not used_tokens_path(tmp_path).exists()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert not engine.disposed


def test_bootstrap_rejects_existing_overlay_without_overwrite_or_token_use(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    path = create_lifecycle_state(
        tmp_path,
        scheme_id=cfg.scheme_id,
        scheme_version=cfg.scheme_version,
        status="active",
        version_status="active",
        harness_run_id="hr_existing",
    )
    before = path.read_bytes()
    engine = _Engine()

    result = BlackboxLifecycleBootstrapGate().run(
        _context(tmp_path, cfg, _token(cfg), engine)
    )

    assert result.status == GateStatus.BLOCKED
    assert "overlay already exists" in "\n".join(result.errors)
    assert path.read_bytes() == before
    assert not used_tokens_path(tmp_path).exists()
    assert not engine.disposed


def test_bootstrap_treats_dangling_overlay_symlink_as_existing_before_token_use(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    path = lifecycle_state_path(tmp_path, cfg.scheme_id)
    path.parent.mkdir(parents=True)
    path.symlink_to(path.parent / "missing.json")
    engine = _Engine()

    result = BlackboxLifecycleBootstrapGate().run(
        _context(tmp_path, cfg, _token(cfg), engine)
    )

    assert result.status == GateStatus.BLOCKED
    assert "overlay already exists" in "\n".join(result.errors)
    assert path.is_symlink()
    assert not used_tokens_path(tmp_path).exists()
    assert not engine.disposed


@pytest.mark.parametrize(
    ("version_status", "registry_status"),
    [("shadow", "active"), ("active", "paused")],
)
def test_bootstrap_rejects_non_active_database_without_consuming_token(
    tmp_path: Path,
    version_status: str,
    registry_status: str,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    with (
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
            return_value=_passed_run(),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_blackbox_lifecycle_state",
            return_value=_db_state(
                cfg,
                version_status=version_status,
                registry_status=registry_status,
            ),
        ),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    assert result.status == GateStatus.BLOCKED
    assert "database active+active" in "\n".join(result.errors)
    assert not used_tokens_path(tmp_path).exists()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert engine.disposed


def test_bootstrap_pending_journal_blocks_before_database_or_token_use(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    with patch(
        "harness.blackbox_v2.lifecycle_bootstrap.assert_lifecycle_clear",
        side_effect=RuntimeError("pending lifecycle journal"),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    assert result.status == GateStatus.BLOCKED
    assert "pending lifecycle journal" in "\n".join(result.errors)
    assert not used_tokens_path(tmp_path).exists()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert not engine.disposed


def test_bootstrap_passed_all_failure_does_not_consume_token(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    with patch(
        "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
        side_effect=ValueError("no exact passed all run"),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    assert result.status == GateStatus.FAILED
    assert "no exact passed all run" in "\n".join(result.errors)
    assert not used_tokens_path(tmp_path).exists()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert engine.disposed


def test_bootstrap_token_must_match_exact_passed_all_run_without_consumption(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    with (
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
            return_value=_passed_run(),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_blackbox_lifecycle_state",
            return_value=_db_state(cfg),
        ),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(
                tmp_path,
                cfg,
                _token(cfg, harness_run_id="hr_other"),
                engine,
            )
        )

    assert result.status == GateStatus.BLOCKED
    assert "harness_run_id mismatch" in "\n".join(result.errors)
    assert not used_tokens_path(tmp_path).exists()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert engine.disposed


def test_bootstrap_commit_failure_consumes_token_but_never_writes_database(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    with (
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
            return_value=_passed_run(),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_blackbox_lifecycle_state",
            return_value=_db_state(cfg),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.create_lifecycle_state",
            side_effect=OSError("forced overlay commit failure"),
        ),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    assert result.status == GateStatus.FAILED
    assert "overlay commit failed" in "\n".join(result.errors)
    assert used_tokens_path(tmp_path).is_file()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert engine.disposed


def test_bootstrap_audit_failure_reports_consumed_token_as_commit_failure(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    with (
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
            return_value=_passed_run(),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_blackbox_lifecycle_state",
            return_value=_db_state(cfg),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.write_authorization_audit",
            side_effect=OSError("forced audit failure"),
        ),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    evidence = {item.key: item.value for item in result.evidence}
    assert result.status == GateStatus.FAILED
    assert "authorization audit failed" in "\n".join(result.errors)
    assert "preflight" not in "\n".join(result.errors)
    assert evidence["authorization_consumed"] is True
    assert "overlay_path" not in evidence
    assert used_tokens_path(tmp_path).is_file()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert engine.disposed


def test_bootstrap_dispose_failure_preserves_success_and_reports_committed_effects(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _DisposeFailingEngine()
    with (
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
            return_value=_passed_run(),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_blackbox_lifecycle_state",
            return_value=_db_state(cfg),
        ),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    evidence = {item.key: item.value for item in result.evidence}
    assert result.status == GateStatus.PASSED
    assert result.passed
    assert result.errors == []
    assert evidence["authorization_consumed"] is True
    assert evidence["overlay_path"] == str(
        lifecycle_state_path(tmp_path, cfg.scheme_id)
    )
    assert evidence["engine_dispose_error"] == "forced dispose failure"
    assert "preflight" not in "\n".join(result.errors)
    assert engine.disposed


def test_bootstrap_unexpected_readback_error_is_reported_as_post_commit(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )

    cfg = _config(tmp_path)
    engine = _Engine()
    with (
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap._verify_passed_all",
            return_value=_passed_run(),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_blackbox_lifecycle_state",
            return_value=_db_state(cfg),
        ),
        patch(
            "harness.blackbox_v2.lifecycle_bootstrap.read_lifecycle_state",
            side_effect=OSError("forced readback failure"),
        ),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    evidence = {item.key: item.value for item in result.evidence}
    assert result.status == GateStatus.FAILED
    assert "commit failed" in "\n".join(result.errors)
    assert "preflight" not in "\n".join(result.errors)
    assert evidence["authorization_consumed"] is True
    assert evidence["overlay_path"] == str(
        lifecycle_state_path(tmp_path, cfg.scheme_id)
    )
    assert lifecycle_state_path(tmp_path, cfg.scheme_id).is_file()
    assert engine.disposed


def test_bootstrap_lock_timeout_is_blocked_without_consuming_token(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )
    from shared.blackbox_v2.lifecycle import LifecycleLockTimeout

    cfg = _config(tmp_path)
    engine = _Engine()
    with patch(
        "harness.blackbox_v2.lifecycle_bootstrap.lifecycle_operation_lock",
        side_effect=LifecycleLockTimeout("forced lifecycle lock timeout"),
    ):
        result = BlackboxLifecycleBootstrapGate().run(
            _context(tmp_path, cfg, _token(cfg), engine)
        )

    assert result.status == GateStatus.BLOCKED
    assert "lifecycle lock timeout" in "\n".join(result.errors)
    assert not used_tokens_path(tmp_path).exists()
    assert not lifecycle_state_path(tmp_path, cfg.scheme_id).exists()
    assert not engine.disposed


def test_bootstrap_is_registered_as_blackbox_gate_and_cli_subcommand(
    tmp_path: Path,
) -> None:
    from harness.blackbox_v2.lifecycle_bootstrap import (
        BlackboxLifecycleBootstrapGate,
    )
    from harness.cli import _build_parser
    from harness.registry import gate_for_name

    cfg = SimpleNamespace(runtime_type="blackbox_v2")
    ctx = GateContext(
        scheme_id="demo",
        predict_date="bootstrap",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        config=cfg,
    )
    assert isinstance(
        gate_for_name("lifecycle-bootstrap", ctx=ctx),
        BlackboxLifecycleBootstrapGate,
    )

    parser = _build_parser()
    gate_parser = next(
        action for action in parser._actions if action.dest == "command"
    ).choices["gate"]
    gate_choices = next(
        action for action in gate_parser._actions if action.dest == "gate_name"
    ).choices
    assert "lifecycle-bootstrap" in gate_choices
