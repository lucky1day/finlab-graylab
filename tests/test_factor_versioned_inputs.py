from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest


def _frames(*, include_v2: bool) -> dict[str, pd.DataFrame]:
    # 使用真正的冻结字段合同，只缩小行数；不替换生产 schema 校验。
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "shared/blackbox_v2/data_bridge_v1_schema.json").read_text()
    )["files"]
    frames = {}
    catalog = []
    for filename, frequency, key in (
        ("daily_output.csv", "daily", "2026-08-27"),
        ("weekly_output.csv", "weekly", "202635"),
        ("monthly_output.csv", "monthly", "202608"),
    ):
        columns = schema[filename]["columns"]
        frames[filename] = pd.DataFrame([{column: key if index == 0 else 1.0
            for index, column in enumerate(columns)}])
        catalog.extend({"indicators_code": column, "frequency": frequency, "factor_version": "V1.0"}
                       for column in columns[1:])
        if frequency == "daily" and include_v2:
            frames[filename]["D2"] = 2.0
            catalog.append({"indicators_code": "D2", "frequency": "daily", "factor_version": "V2.0"})
    frames["api_wind_date.csv"] = pd.DataFrame({"rdate": ["2026-08-27"], "week_id": ["202635"]})
    frames["factor_catalog.csv"] = pd.DataFrame(catalog)
    return frames


def _dataset_and_state(frames: dict[str, pd.DataFrame]):
    profiles = {}
    state_files = {}
    for index, (filename, frame) in enumerate(frames.items(), start=1):
        sha256 = hashlib.sha256(
            frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        ).hexdigest()
        profile = SimpleNamespace(
            sha256=sha256,
            business_hash=f"{index:064x}",
            rows=len(frame),
            columns=len(frame.columns),
            min_key=str(frame.iloc[0, 0]),
            max_key=str(frame.iloc[-1, 0]),
        )
        profiles[filename] = profile
        state_files[filename] = {
            "sha256": profile.sha256,
            "business_hash": profile.business_hash,
            "rows": profile.rows,
            "columns": profile.columns,
            "min_key": profile.min_key,
            "max_key": profile.max_key,
        }
    return (
        SimpleNamespace(
            schema_version="data-bridge-v1",
            business_digest="b" * 64,
            frames=frames,
            files=profiles,
        ),
        {
            "generation_id": "generation-factor-version-test",
            "refresh_date": "2026-08-28",
            "business_digest": "b" * 64,
            "schema_version": "data-bridge-v1",
            "files": state_files,
        },
    )


def test_generation_builds_one_shared_legacy_view_only_when_needed() -> None:
    from shared.input_artifacts import (
        get_ready_blackbox_snapshot,
        prepare_blackbox_generation_snapshot,
    )

    frames = _frames(include_v2=True)
    dataset, state = _dataset_and_state(frames)
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_root = Path(tmpdir)
        prepare_blackbox_generation_snapshot(
            state=state,
            dataset=dataset,
            cache_root=cache_root,
        )
        full = get_ready_blackbox_snapshot(
            snapshot_date="2026-08-28",
            cache_root=cache_root,
            factor_input_mode="algorithm_managed",
        )
        legacy = get_ready_blackbox_snapshot(
            snapshot_date="2026-08-28",
            cache_root=cache_root,
            factor_input_mode="legacy_v1",
        )

        assert full.snapshot_id != legacy.snapshot_id
        assert list(pd.read_csv(full.data_dir / "daily_output.csv").columns) == list(frames["daily_output.csv"].columns)
        assert list(pd.read_csv(legacy.data_dir / "daily_output.csv").columns) == list(frames["daily_output.csv"].columns[:-1])
        assert (full.data_dir / "factor_catalog.csv").is_file()
        assert not (legacy.data_dir / "factor_catalog.csv").exists()

        receipt_path = next((cache_root / "receipts").glob("*.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        downgraded = dict(receipt)
        downgraded.pop("legacy_snapshot_id")
        downgraded.pop("legacy_sealed_file_fingerprints")
        receipt_path.chmod(0o644)
        receipt_path.write_text(
            json.dumps(
                downgraded,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        receipt_path.chmod(0o444)
        with pytest.raises(ValueError, match="receipt contract mismatch"):
            get_ready_blackbox_snapshot(
                snapshot_date="2026-08-28",
                cache_root=cache_root,
                factor_input_mode="legacy_v1",
            )

        receipt["legacy_snapshot_id"] = receipt["snapshot_id"]
        receipt["legacy_sealed_file_fingerprints"] = receipt[
            "sealed_file_fingerprints"
        ]
        receipt_path.chmod(0o644)
        receipt_path.write_text(
            json.dumps(
                receipt,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        receipt_path.chmod(0o444)
        with pytest.raises(ValueError, match="frozen V1 columns"):
            get_ready_blackbox_snapshot(
                snapshot_date="2026-08-28",
                cache_root=cache_root,
                factor_input_mode="legacy_v1",
            )


def test_cached_generation_requires_intact_legacy_snapshot_before_ready() -> None:
    from shared.input_artifacts import (
        get_ready_blackbox_snapshot,
        invalidate_ready_blackbox_snapshot,
        prepare_blackbox_generation_snapshot,
    )

    frames = _frames(include_v2=True)
    dataset, state = _dataset_and_state(frames)
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_root = Path(tmpdir)
        prepare_blackbox_generation_snapshot(
            state=state,
            dataset=dataset,
            cache_root=cache_root,
        )
        legacy = get_ready_blackbox_snapshot(
            snapshot_date="2026-08-28",
            cache_root=cache_root,
            factor_input_mode="legacy_v1",
        )
        legacy.data_dir.chmod(0o755)
        (legacy.data_dir / "daily_output.csv").unlink()
        invalidate_ready_blackbox_snapshot(cache_root=cache_root)

        with pytest.raises(ValueError):
            prepare_blackbox_generation_snapshot(
                state=state,
                dataset=dataset,
                cache_root=cache_root,
            )
        assert not (cache_root / "ready-generation.json").exists()


def test_published_factor_version_membership_is_sealed() -> None:
    from shared.data_bridge.refresh import (
        DataBridgeRefreshError,
        _assert_factor_catalog_continuity,
    )

    previous = SimpleNamespace(
        dataset=SimpleNamespace(
            frames={"factor_catalog.csv": _frames(include_v2=False)["factor_catalog.csv"]}
        )
    )
    candidate = _frames(include_v2=True)["factor_catalog.csv"]
    _assert_factor_catalog_continuity(
        previous,
        SimpleNamespace(frames={"factor_catalog.csv": candidate}),
    )

    expanded_v1 = pd.concat(
        [
            previous.dataset.frames["factor_catalog.csv"],
            pd.DataFrame(
                [
                    {
                        "indicators_code": "D3",
                        "frequency": "daily",
                        "factor_version": "V1.0",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(DataBridgeRefreshError, match="membership changed"):
        _assert_factor_catalog_continuity(
            previous,
            SimpleNamespace(frames={"factor_catalog.csv": expanded_v1}),
        )


def test_legacy_four_file_receipt_is_readable_only_by_legacy_mode() -> None:
    from shared.blackbox_v2.snapshot import create_snapshot_from_frames
    from shared.data_bridge.validation import LEGACY_FOUR_FILENAMES
    from shared.input_artifacts import (
        LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
        LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256,
        _generation_snapshot_cache_key,
        _snapshot_file_fingerprints,
        get_ready_blackbox_snapshot,
    )

    frames = {
        name: frame
        for name, frame in _frames(include_v2=False).items()
        if name in LEGACY_FOUR_FILENAMES
    }
    files = {}
    for filename, frame in frames.items():
        rendered = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        files[filename] = {
            "sha256": hashlib.sha256(rendered).hexdigest(),
            "business_hash": hashlib.sha256(filename.encode("utf-8")).hexdigest(),
            "rows": len(frame),
            "columns": len(frame.columns),
            "min_key": str(frame.iloc[0, 0]),
            "max_key": str(frame.iloc[-1, 0]),
        }
    identity = {
        "cache_schema_version": LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
        "schema_contract_sha256": LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256,
        "generation_id": "legacy-four-generation",
        "refresh_date": "2026-08-28",
        "business_digest": "c" * 64,
        "schema_version": "data-bridge-v1",
        "files": files,
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_root = Path(tmpdir)
        key = _generation_snapshot_cache_key(identity)
        snapshot = create_snapshot_from_frames(
            frames,
            output_root=cache_root / "snapshots" / key,
            expected_columns={name: list(frame.columns) for name, frame in frames.items()},
            schema_version="data-bridge-v1",
        )
        seals = _snapshot_file_fingerprints(snapshot)
        receipt = {
            "identity": identity,
            "snapshot_id": snapshot.snapshot_id,
            "sealed_file_fingerprints": {
                name: list(value) for name, value in seals.items()
            },
            "cutoff_keys": {
                "date": ["2026-08-27"],
                "week_id": ["202635"],
                "month_id": ["202608"],
                "calendar_week_ids_by_date": [["2026-08-27", "202635"]],
            },
        }
        receipts = cache_root / "receipts"
        receipts.mkdir()
        canonical = lambda value: json.dumps(  # noqa: E731
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        (receipts / f"{key}.json").write_text(
            canonical(receipt), encoding="utf-8"
        )
        (cache_root / "ready-generation.json").write_text(
            canonical(identity), encoding="utf-8"
        )

        legacy = get_ready_blackbox_snapshot(
            snapshot_date="2026-08-28",
            cache_root=cache_root,
            factor_input_mode="legacy_v1",
        )
        assert legacy.snapshot_id == snapshot.snapshot_id
        with pytest.raises(ValueError, match="five-file generation"):
            get_ready_blackbox_snapshot(
                snapshot_date="2026-08-28",
                cache_root=cache_root,
                factor_input_mode="algorithm_managed",
            )


def test_factor_catalog_must_match_wide_columns_and_order() -> None:
    from shared.data_bridge.validation import (
        DataBridgeValidationError,
        _validate_factor_catalog_file,
        _validate_factor_catalog_matches_outputs,
    )

    frames = _frames(include_v2=True)
    _validate_factor_catalog_matches_outputs(frames)
    reordered = dict(frames)
    reordered["factor_catalog.csv"] = frames["factor_catalog.csv"].iloc[::-1]
    with pytest.raises(DataBridgeValidationError, match="columns and order"):
        _validate_factor_catalog_matches_outputs(reordered)
    invalid = frames["factor_catalog.csv"].copy()
    invalid.loc[0, "factor_version"] = "v1"
    with pytest.raises(DataBridgeValidationError, match="factor_version"):
        _validate_factor_catalog_file(
            invalid,
            baseline_columns=[
                "indicators_code",
                "frequency",
                "factor_version",
            ],
        )


def test_native_frozen_weekly_input_preserves_metadata_lag() -> None:
    from shared import data_service

    metadata = pd.DataFrame(
        {
            "indicators_code": ["W1"],
            "frequency": ["weekly"],
            "lag_length": [1],
        }
    )
    raw = pd.DataFrame(
        {
            "rdate": ["2026-08-14", "2026-08-21"],
            "week_id": [202633, 202634],
            "indicators_code": ["W1", "W1"],
            "indicators_value": [1.0, 2.0],
        }
    )
    empty = raw.iloc[0:0].copy()
    with patch.object(
        data_service,
        "read_factor_metadata_from_db",
        return_value=metadata,
    ), patch.object(
        data_service,
        "read_weekly_long_from_db",
        side_effect=[raw, empty, raw, empty],
    ):
        original = data_service.build_weekly_output_from_db(engine=object())
        frozen = data_service.build_weekly_output_from_db(
            schema_columns=["week_id", "W1"],
            engine=object(),
            preserve_metadata_lags=True,
        )

    pd.testing.assert_frame_equal(frozen, original)
    assert pd.isna(frozen.loc[0, "W1"])
    assert frozen.loc[1, "W1"] == 1.0


@pytest.mark.parametrize("failure", ["prepare", "execution", "cleanup"])
def test_runtime_view_cleanup_failure_records_owned_debris(tmp_path, monkeypatch, caplog, failure):
    from shared.input_artifacts import (
        cleanup_blackbox_runtime_debris, compose_blackbox_input_bundle,
        get_ready_blackbox_snapshot, open_blackbox_runtime_view,
        prepare_blackbox_generation_snapshot,
    )

    dataset, state = _dataset_and_state(_frames(include_v2=True))
    cache = tmp_path / "cache"
    prepare_blackbox_generation_snapshot(state=state, dataset=dataset, cache_root=cache)
    snapshot = get_ready_blackbox_snapshot(
        snapshot_date="2026-08-28", cache_root=cache, factor_input_mode="algorithm_managed",
    )
    bundle = compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed")
    root = tmp_path / "views"
    unrelated = root / "active" / "blackbox-runtime-unrelated"
    unrelated.mkdir(parents=True)
    original = RuntimeError("original materialization or algorithm failure")
    cleanup_failure = PermissionError("simulated runtime view removal failure")
    open_file, remove_tree = os.open, shutil.rmtree

    def open_path(path, flags, *args, **kwargs):
        if failure == "prepare" and flags & os.O_CREAT and Path(path).parent.parent == unrelated.parent:
            raise original
        return open_file(path, flags, *args, **kwargs)

    def remove_path(path, *args, **kwargs):
        if Path(path).parent == unrelated.parent:
            raise cleanup_failure
        return remove_tree(path, *args, **kwargs)

    with monkeypatch.context() as filesystem:
        filesystem.setattr(os, "open", open_path)
        filesystem.setattr(shutil, "rmtree", remove_path)
        with pytest.raises(RuntimeError if failure != "cleanup" else PermissionError) as caught:
            with open_blackbox_runtime_view(bundle, runtime_root=root):
                assert failure != "prepare"
                if failure == "execution":
                    raise original

    assert caught.value is (cleanup_failure if failure == "cleanup" else original)
    if failure != "cleanup":
        assert any("cleanup failed" in note for note in caught.value.__notes__)
    assert "runtime view cleanup failed" in caplog.text
    debris, = [path for path in unrelated.parent.iterdir() if path != unrelated]
    cleanup_blackbox_runtime_debris(debris, runtime_root=root)
    assert not debris.exists() and unrelated.is_dir()


def _fifo_snapshot_read(root: str, location: str) -> None:
    """隔离进程中的真实管道；只调用公开输入接口。"""
    from shared.input_artifacts import (
        compose_blackbox_input_bundle, get_ready_blackbox_snapshot,
        open_blackbox_runtime_view, prepare_blackbox_generation_snapshot,
    )

    cache = Path(root) / 'cache'
    dataset, state = _dataset_and_state(_frames(include_v2=True))
    prepare_blackbox_generation_snapshot(state=state, dataset=dataset, cache_root=cache)
    snapshot = get_ready_blackbox_snapshot(snapshot_date='2026-08-28', cache_root=cache, factor_input_mode='algorithm_managed')
    target = {
        'ready': cache/'ready-generation.json',
        'receipt': next((cache/'receipts').glob('*.json')),
        'manifest': snapshot.manifest_path,
        'runtime-copy': snapshot.data_dir/'daily_output.csv',
        'lock': cache/'generation-cache.lock',
    }[location]
    target.parent.chmod(0o700)
    target.unlink()
    os.mkfifo(target, 0o600)
    inode = target.lstat().st_ino
    open_descriptors = len(list(Path('/dev/fd').iterdir()))
    try:
        if location == 'runtime-copy':
            with open_blackbox_runtime_view(compose_blackbox_input_bundle(snapshot, factor_input_mode='algorithm_managed'), runtime_root=Path(root)/'views'):
                raise AssertionError('FIFO input must not reach the algorithm')
        else:
            get_ready_blackbox_snapshot(snapshot_date='2026-08-28', cache_root=cache, factor_input_mode='algorithm_managed')
    except ValueError as error:
        if location == 'lock':
            assert str(error) == 'Blackbox generation snapshot lock must be a private regular file'
        assert stat.S_ISFIFO(target.lstat().st_mode) and target.lstat().st_ino == inode
        assert len(list(Path('/dev/fd').iterdir())) == open_descriptors
    else:
        raise AssertionError('FIFO must be rejected')


@pytest.mark.parametrize('location', ['ready', 'receipt', 'manifest', 'runtime-copy', 'lock'])
def test_snapshot_read_rejects_fifo_without_waiting_or_removing_it(tmp_path, location):
    command = "import runpy,sys; runpy.run_path(sys.argv[1])['_fifo_snapshot_read'](sys.argv[2],sys.argv[3])"
    subprocess.run(
        [sys.executable, '-B', '-c', command, __file__, str(tmp_path), location],
        check=True, capture_output=True, text=True, timeout=5,
    )
