from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from shared.scheme_config_schema import SCHEME_ID_PATTERN


_EXACT_PATTERN = re.compile(r"[0-9a-f]{12}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_PATTERN = re.compile(r"snapshot-[0-9a-f]{24}")
_ENTRY_FIELDS = {
    "scheme_id",
    "scheme_version",
    "target_tenor",
    "horizon",
    "benchmark_id",
    "input_snapshot_id",
    "code_hash",
    "config_hash",
    "manifest_hash",
    "expected_fact_count",
    "backtest_summary_sha256",
    "backtest_facts_sha256",
    "product_facts_sha256",
    "reason",
}


@dataclass(frozen=True, slots=True)
class LegacyBacktestLineage:
    """由版本、摘要及逐事实摘要共同约束的旧回测血缘。"""

    scheme_id: str
    scheme_version: str
    target_tenor: str
    horizon: int
    benchmark_id: str
    input_snapshot_id: str
    code_hash: str
    config_hash: str
    manifest_hash: str
    expected_fact_count: int
    backtest_summary_sha256: str
    backtest_facts_sha256: str
    product_facts_sha256: str
    reason: str


def load_legacy_backtest_lineages(
    project_root: str | Path,
) -> frozenset[LegacyBacktestLineage]:
    """严格读取仅用于旧回测缺失冗余字段的精确兼容清单。"""
    path = (
        Path(project_root)
        / "deploy"
        / "legacy_backtest_lineage_compatibility_v1.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "entries"}
        or raw.get("schema_version")
        != "legacy-backtest-lineage-compatibility-v1"
        or not isinstance(raw.get("entries"), list)
    ):
        raise ValueError("legacy backtest lineage schema is invalid")

    result: set[LegacyBacktestLineage] = set()
    identities: set[tuple[str, str, str, int]] = set()
    for value in raw["entries"]:
        if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
            raise ValueError("legacy backtest lineage entry is invalid")
        entry = LegacyBacktestLineage(**value)
        if not _valid_entry(entry):
            raise ValueError("legacy backtest lineage fields are invalid")
        identity = (
            entry.scheme_id,
            entry.scheme_version,
            entry.target_tenor,
            entry.horizon,
        )
        if identity in identities:
            raise ValueError("duplicate legacy backtest lineage entry")
        identities.add(identity)
        result.add(entry)
    return frozenset(result)


def _valid_entry(entry: LegacyBacktestLineage) -> bool:
    hashes = (
        entry.code_hash,
        entry.config_hash,
        entry.manifest_hash,
        entry.backtest_summary_sha256,
        entry.backtest_facts_sha256,
        entry.product_facts_sha256,
    )
    return (
        isinstance(entry.scheme_id, str)
        and SCHEME_ID_PATTERN.fullmatch(entry.scheme_id) is not None
        and isinstance(entry.scheme_version, str)
        and _EXACT_PATTERN.fullmatch(entry.scheme_version) is not None
        and isinstance(entry.target_tenor, str)
        and re.fullmatch(r"[1-9][0-9]*Y", entry.target_tenor) is not None
        and isinstance(entry.horizon, int)
        and not isinstance(entry.horizon, bool)
        and entry.horizon > 0
        and isinstance(entry.benchmark_id, str)
        and bool(entry.benchmark_id.strip())
        and entry.benchmark_id == entry.benchmark_id.strip()
        and len(entry.benchmark_id) <= 255
        and isinstance(entry.input_snapshot_id, str)
        and _SNAPSHOT_PATTERN.fullmatch(entry.input_snapshot_id) is not None
        and all(
            isinstance(value, str) and _SHA256_PATTERN.fullmatch(value)
            for value in hashes
        )
        and isinstance(entry.expected_fact_count, int)
        and not isinstance(entry.expected_fact_count, bool)
        and entry.expected_fact_count > 0
        and isinstance(entry.reason, str)
        and bool(entry.reason.strip())
        and entry.reason == entry.reason.strip()
    )
