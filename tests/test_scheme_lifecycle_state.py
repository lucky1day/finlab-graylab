"""方案生命周期状态覆盖层：版本绑定与 fail-closed 语义。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.scheme_lifecycle_state import (
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


def _real_scheme() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    return root, "weekly_1y_causal_v1_31_0_standalone"


def test_discovery_applies_overlay_for_matching_version(tmp_path: Path) -> None:
    from scheduler.discovery import load_scheme_config

    root, scheme_id = _real_scheme()
    config_path = root / "schemes" / scheme_id / "config.yaml"
    with patch.dict(os.environ, {"BFL_RUNTIME_ROOT": str(tmp_path / "state")}, clear=False):
        declared = load_scheme_config(config_path)
        write_lifecycle_state(
            root,
            scheme_id=scheme_id,
            scheme_version=declared.scheme_version,
            status="active",
            version_status="active",
        )
        effective = load_scheme_config(config_path)

    assert (effective.status, effective.version_status) == ("active", "active")
    assert effective.scheme_version == declared.scheme_version


def test_lifecycle_transition_leaves_config_yaml_byte_identical(tmp_path: Path) -> None:
    """apply_lifecycle_state 只写覆盖层；config.yaml 必须逐字节不变。"""
    import hashlib

    from shared.blackbox_v2.lifecycle import LifecycleState, apply_lifecycle_state

    root, scheme_id = _real_scheme()
    config_path = root / "schemes" / scheme_id / "config.yaml"
    before = hashlib.sha256(config_path.read_bytes()).hexdigest()

    with patch.dict(os.environ, {"BFL_RUNTIME_ROOT": str(tmp_path / "state")}, clear=False):
        apply_lifecycle_state(
            root,
            scheme_id=scheme_id,
            scheme_version="v-under-test",
            state=LifecycleState("paused", "shadow", "paused"),
            harness_run_id="hr_test",
        )
        record = read_lifecycle_state(root, scheme_id, "v-under-test")

    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == before
    assert record is not None
    assert (record.status, record.version_status) == ("paused", "shadow")
