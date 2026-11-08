from __future__ import annotations

import json
from pathlib import Path

import pytest

from scheduler.discovery import load_scheme_config
from shared.legacy_backtest_lineage import load_legacy_backtest_lineages
from shared.versioning import compute_scheme_version


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_repository_legacy_backtest_lineage_preserves_historical_exact() -> None:
    entries = load_legacy_backtest_lineages(PROJECT_ROOT)

    assert len(entries) == 1
    entry = next(iter(entries))
    config = load_scheme_config(
        PROJECT_ROOT / "schemes" / entry.scheme_id / "config.yaml"
    )
    # 血缘绑定历史版本；同ID当前运行时升级不得重写旧回测的exact/hash。
    assert config.scheme_id == entry.scheme_id
    assert entry.target_tenor in config.tenors
    assert compute_scheme_version(
        entry.code_hash, entry.config_hash, entry.manifest_hash,
    ) == entry.scheme_version


def test_legacy_backtest_lineage_loader_rejects_duplicate_scope(
    tmp_path: Path,
) -> None:
    source = json.loads(
        (
            PROJECT_ROOT
            / "deploy"
            / "legacy_backtest_lineage_compatibility_v1.json"
        ).read_text(encoding="utf-8")
    )
    source["entries"].append(dict(source["entries"][0]))
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "legacy_backtest_lineage_compatibility_v1.json").write_text(
        json.dumps(source),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_legacy_backtest_lineages(tmp_path)
