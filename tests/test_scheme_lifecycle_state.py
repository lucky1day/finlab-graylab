"""方案生命周期状态覆盖层：版本绑定与 fail-closed 语义。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.scheme_lifecycle_state import (
    SCHEMA_VERSION,
    lifecycle_state_path,
    read_lifecycle_state,
    write_lifecycle_state,
)


def _runtime(tmp_path: Path) -> dict[str, str]:
    return {"BFL_RUNTIME_ROOT": str(tmp_path / "state")}


def test_written_state_is_read_back_for_the_same_version(tmp_path: Path) -> None:
    with patch.dict(os.environ, _runtime(tmp_path), clear=False):
        write_lifecycle_state(
            tmp_path,
            scheme_id="demo",
            scheme_version="v1",
            status="active",
            version_status="active",
            harness_run_id="hr_x",
        )
        record = read_lifecycle_state(tmp_path, "demo", "v1")

    assert record is not None
    assert (record.status, record.version_status) == ("active", "active")


def test_state_is_ignored_when_scheme_version_changed(tmp_path: Path) -> None:
    """代码或平台配置一变，scheme_version 随之改变，旧状态必须失效。"""
    with patch.dict(os.environ, _runtime(tmp_path), clear=False):
        write_lifecycle_state(
            tmp_path,
            scheme_id="demo",
            scheme_version="v1",
            status="active",
            version_status="active",
        )
        assert read_lifecycle_state(tmp_path, "demo", "v2") is None


def test_missing_state_returns_none_for_fallback(tmp_path: Path) -> None:
    with patch.dict(os.environ, _runtime(tmp_path), clear=False):
        assert read_lifecycle_state(tmp_path, "never-written", "v1") is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.pop("status"), id="missing_field"),
        pytest.param(lambda d: d.update(status="bogus"), id="invalid_status"),
        pytest.param(lambda d: d.update(version_status="bogus"), id="invalid_version_status"),
        pytest.param(lambda d: d.update(schema_version="other"), id="wrong_schema"),
        pytest.param(lambda d: d.update(scheme_id="other"), id="wrong_scheme_id"),
    ],
)
def test_malformed_state_fails_closed(tmp_path: Path, mutate) -> None:
    with patch.dict(os.environ, _runtime(tmp_path), clear=False):
        path = write_lifecycle_state(
            tmp_path,
            scheme_id="demo",
            scheme_version="v1",
            status="active",
            version_status="active",
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert read_lifecycle_state(tmp_path, "demo", "v1") is None


def test_unreadable_state_fails_closed(tmp_path: Path) -> None:
    with patch.dict(os.environ, _runtime(tmp_path), clear=False):
        path = write_lifecycle_state(
            tmp_path,
            scheme_id="demo",
            scheme_version="v1",
            status="active",
            version_status="active",
        )
        path.write_text("{ not json", encoding="utf-8")
        assert read_lifecycle_state(tmp_path, "demo", "v1") is None


def test_invalid_values_are_rejected_on_write(tmp_path: Path) -> None:
    with patch.dict(os.environ, _runtime(tmp_path), clear=False):
        for kwargs in (
            {"status": "bogus", "version_status": "active"},
            {"status": "active", "version_status": "bogus"},
        ):
            with pytest.raises(ValueError):
                write_lifecycle_state(
                    tmp_path, scheme_id="demo", scheme_version="v1", **kwargs
                )


def test_scheme_id_cannot_escape_the_lifecycle_root(tmp_path: Path) -> None:
    with patch.dict(os.environ, _runtime(tmp_path), clear=False):
        for bad in ("../escape", "nested/id", "", "  ", ".hidden"):
            with pytest.raises(ValueError):
                lifecycle_state_path(tmp_path, bad)


def test_state_lives_under_runtime_root_not_the_release_tree(tmp_path: Path) -> None:
    runtime_root = tmp_path / "state"
    project_root = tmp_path / "release"
    with patch.dict(os.environ, {"BFL_RUNTIME_ROOT": str(runtime_root)}, clear=False):
        path = lifecycle_state_path(project_root, "demo")

    assert path.is_relative_to(runtime_root.resolve())
    assert not path.is_relative_to(project_root)


def test_production_target_without_runtime_root_fails_closed(tmp_path: Path) -> None:
    environ = dict(os.environ)
    environ.pop("BFL_RUNTIME_ROOT", None)
    environ["BFL_DEPLOYMENT_TARGET"] = "aliyun-gray"
    with patch.dict(os.environ, environ, clear=True):
        with pytest.raises(RuntimeError):
            lifecycle_state_path(tmp_path, "demo")
        # 读路径把该失败视作「无状态」，交由调用方回落到 config.yaml
        assert read_lifecycle_state(tmp_path, "demo", "v1") is None


def test_development_default_stays_inside_the_worktree(tmp_path: Path) -> None:
    environ = dict(os.environ)
    environ.pop("BFL_RUNTIME_ROOT", None)
    environ.pop("BFL_DEPLOYMENT_TARGET", None)
    with patch.dict(os.environ, environ, clear=True):
        path = lifecycle_state_path(tmp_path, "demo")

    assert path == tmp_path / "backtest_artifacts" / "lifecycle" / "demo.json"


def test_schema_version_is_pinned() -> None:
    assert SCHEMA_VERSION == "scheme-lifecycle-state-v1"
