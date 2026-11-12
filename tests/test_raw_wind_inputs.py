"""raw_wind_v1 输入组装：bundle 组合身份、运行视图物化与篡改检测、就绪标记、保留策略与 producer 钩子隔离。"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES, compose_blackbox_input_bundle
from shared.data_bridge.raw_wind import RAW_WIND_FILENAMES, export_raw_wind
from shared.input_artifacts import (
    RawWindUnavailableError,
    get_ready_blackbox_snapshot,
    get_ready_raw_wind_artifact,
    invalidate_raw_wind_sidecar,
    open_blackbox_runtime_view,
    prepare_blackbox_generation_snapshot,
    raw_wind_for_profile,
)
from shared._input_artifacts.raw_wind import _prune, _write_ready
from tests.test_factor_versioned_inputs import _dataset_and_state, _frames
from tests.test_raw_wind_export import _engine


def _snapshot(tmp_path: Path):
    dataset, state = _dataset_and_state(_frames(include_v2=True))
    cache = tmp_path / "cache"
    prepare_blackbox_generation_snapshot(state=state, dataset=dataset, cache_root=cache)
    return get_ready_blackbox_snapshot(snapshot_date="2026-08-28", cache_root=cache, factor_input_mode="algorithm_managed")


def _artifact(root: Path, snapshot, *, generation_id: str | None = None):
    with _engine().connect() as connection:
        return export_raw_wind(
            connection, root,
            generation_id=generation_id or snapshot.generation_id, refresh_date=snapshot.refresh_date,
        )


def _compose(snapshot, artifact):
    return compose_blackbox_input_bundle(
        snapshot, factor_input_mode="algorithm_managed", input_profile="raw_wind_v1", raw_wind=artifact
    )


def test_raw_profile_bundle_extends_the_file_set_and_has_a_combined_identity(tmp_path):
    snapshot = _snapshot(tmp_path)
    artifact = _artifact(tmp_path / "raw", snapshot)

    bundle = _compose(snapshot, artifact)
    standard = compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed")

    assert bundle.expected_filenames == (*SNAPSHOT_FILENAMES, *RAW_WIND_FILENAMES)
    assert bundle.parent_snapshot_id == snapshot.snapshot_id == standard.combined_snapshot_id
    assert bundle.combined_snapshot_id.startswith("snapshot-combined-")
    assert bundle.combined_snapshot_id != snapshot.snapshot_id
    assert bundle.combined_snapshot_id == _compose(snapshot, artifact).combined_snapshot_id


def test_composition_rejects_inconsistent_profile_artifact_pairs(tmp_path):
    snapshot = _snapshot(tmp_path)
    artifact = _artifact(tmp_path / "raw", snapshot)
    other_generation = _artifact(tmp_path / "raw", snapshot, generation_id="another-generation")

    with pytest.raises(ValueError, match="only valid for input_profile raw_wind_v1"):
        compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed", raw_wind=artifact)
    with pytest.raises(ValueError, match="requires a raw wind artifact"):
        compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed", input_profile="raw_wind_v1")
    with pytest.raises(ValueError, match="does not belong to this snapshot generation"):
        _compose(snapshot, other_generation)
    with pytest.raises(ValueError, match="unsupported input_profile"):
        compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed", input_profile="raw_wind_v9")


def test_runtime_view_materializes_exactly_the_expected_independent_readonly_files(tmp_path):
    snapshot = _snapshot(tmp_path)
    artifact = _artifact(tmp_path / "raw", snapshot)

    with open_blackbox_runtime_view(_compose(snapshot, artifact), runtime_root=tmp_path / "views") as view:
        names = sorted(p.name for p in view.data_dir.iterdir())
        assert names == sorted((*SNAPSHOT_FILENAMES, *RAW_WIND_FILENAMES))
        for name in RAW_WIND_FILENAMES:
            copy, source = view.data_dir / name, artifact.path / name
            assert copy.read_bytes() == source.read_bytes()
            assert copy.stat().st_ino != source.stat().st_ino and copy.stat().st_nlink == 1
            assert copy.stat().st_mode & 0o222 == 0


def test_runtime_view_rejects_a_tampered_artifact_and_leaves_no_active_view(tmp_path):
    snapshot = _snapshot(tmp_path)
    artifact = _artifact(tmp_path / "raw", snapshot)
    bundle = _compose(snapshot, artifact)
    artifact.path.chmod(0o755)
    target = artifact.path / RAW_WIND_FILENAMES[0]
    target.chmod(0o644)
    target.write_bytes(target.read_bytes().replace(b"2.5", b"9.9"))
    views = tmp_path / "views"

    with pytest.raises(ValueError, match="does not match its manifest"):
        with open_blackbox_runtime_view(bundle, runtime_root=views):
            raise AssertionError("must not run")

    assert not [p for p in (views / "active").iterdir()]


def test_runtime_view_rejects_forged_bundles(tmp_path):
    snapshot = _snapshot(tmp_path)
    bundle = _compose(snapshot, _artifact(tmp_path / "raw", snapshot))

    forged_profile = replace(bundle, input_profile="standard")
    forged_files = replace(bundle, expected_filenames=SNAPSHOT_FILENAMES)
    forged_id = replace(bundle, combined_snapshot_id=snapshot.snapshot_id)
    for forged in (forged_profile, forged_files, forged_id):
        with pytest.raises(ValueError, match="trusted composition"):
            with open_blackbox_runtime_view(forged, runtime_root=tmp_path / "views"):
                raise AssertionError("must not run")


def test_ready_marker_pairs_the_artifact_with_the_snapshot_generation(tmp_path):
    snapshot = _snapshot(tmp_path)
    root = tmp_path / "raw"
    artifact = _artifact(root, snapshot)

    assert raw_wind_for_profile(snapshot, "standard", root=root) is None
    with pytest.raises(RawWindUnavailableError, match="not ready"):
        get_ready_raw_wind_artifact(snapshot=snapshot, root=root)

    _write_ready(root, artifact)
    assert get_ready_raw_wind_artifact(snapshot=snapshot, root=root).artifact_id == artifact.artifact_id
    assert raw_wind_for_profile(snapshot, "raw_wind_v1", root=root).artifact_id == artifact.artifact_id

    with pytest.raises(RawWindUnavailableError, match="does not match"):
        get_ready_raw_wind_artifact(snapshot=replace(snapshot, generation_id="newer-generation"), root=root)

    invalidate_raw_wind_sidecar(root=root)
    invalidate_raw_wind_sidecar(root=root)  # 幂等
    with pytest.raises(RawWindUnavailableError):
        raw_wind_for_profile(snapshot, "raw_wind_v1", root=root)


def test_ready_artifact_with_missing_or_resized_files_is_unavailable(tmp_path):
    snapshot = _snapshot(tmp_path)
    root = tmp_path / "raw"
    artifact = _artifact(root, snapshot)
    _write_ready(root, artifact)
    artifact.path.chmod(0o755)
    (artifact.path / RAW_WIND_FILENAMES[1]).chmod(0o644)
    with (artifact.path / RAW_WIND_FILENAMES[1]).open("ab") as handle:
        handle.write(b"x")

    with pytest.raises(RawWindUnavailableError, match="invalid"):
        get_ready_raw_wind_artifact(snapshot=snapshot, root=root)


def test_prune_keeps_only_the_newest_two_artifacts_including_current(tmp_path):
    snapshot = _snapshot(tmp_path)
    root = tmp_path / "raw"
    artifacts = []
    for index in range(4):
        artifact = _artifact(root, snapshot, generation_id=f"generation-{index}")
        os.utime(artifact.path, (1_000 + index, 1_000 + index))
        artifacts.append(artifact)

    _prune(root, keep=2, current=artifacts[3].artifact_id)

    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == sorted([artifacts[2].artifact_id, artifacts[3].artifact_id])


def test_deliveries_of_one_scheme_must_share_one_input_profile():
    from scheduler.discovery import blackbox_input_profile

    def delivery(profile):
        return SimpleNamespace(metadata=SimpleNamespace(input_profile=profile))

    assert blackbox_input_profile(SimpleNamespace(blackbox_deliveries=(delivery("raw_wind_v1"), delivery("raw_wind_v1")))) == "raw_wind_v1"
    with pytest.raises(ValueError, match="share one input_profile"):
        blackbox_input_profile(SimpleNamespace(blackbox_deliveries=(delivery("standard"), delivery("raw_wind_v1"))))


def test_producer_hook_is_conditional_and_never_blocks_the_standard_publication(monkeypatch):
    from scripts import refresh_data_bridge_current as refresh

    calls = []
    monkeypatch.setattr(refresh, "prepare_raw_wind_sidecar", lambda **kw: calls.append(kw) or SimpleNamespace(artifact_id="raw-wind-x"))

    monkeypatch.setattr(refresh, "deployment_requires_raw_wind", lambda: False)
    assert refresh._prepare_raw_wind_sidecar_best_effort(state={}, engine=None, schema_path="s") is None
    assert calls == []

    monkeypatch.setattr(refresh, "deployment_requires_raw_wind", lambda: True)
    assert refresh._prepare_raw_wind_sidecar_best_effort(state={}, engine=None, schema_path="s").artifact_id == "raw-wind-x"

    def boom(**_kw):
        raise RuntimeError("source database unavailable")

    monkeypatch.setattr(refresh, "prepare_raw_wind_sidecar", boom)
    assert refresh._prepare_raw_wind_sidecar_best_effort(state={}, engine=None, schema_path="s") is None
