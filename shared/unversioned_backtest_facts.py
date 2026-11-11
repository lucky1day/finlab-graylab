"""早于 exact 版本记录发布、按决定不补写 exact 的回测产品事实冻结清单。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from shared.scheme_config_schema import SCHEME_ID_PATTERN


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SCHEMA_VERSION = "unversioned-backtest-facts-v1"
_ENTRY_FIELDS = {
    "scheme_id",
    "target_tenor",
    "horizon",
    "expected_fact_count",
    "facts_sha256",
    "reason",
}


@dataclass(frozen=True, slots=True)
class UnversionedBacktestFacts:
    """一个方案/期限/horizon 下 exact 为空的回测产品事实的条数与规范摘要。"""

    scheme_id: str
    target_tenor: str
    horizon: int
    expected_fact_count: int
    facts_sha256: str
    reason: str


def load_unversioned_backtest_facts(
    project_root: str | Path,
) -> frozenset[UnversionedBacktestFacts]:
    """严格读取冻结清单；清单之外出现的空 exact 事实一律由 Gate 判失败。"""
    path = Path(project_root) / "deploy" / "unversioned_backtest_facts_v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "entries"}
        or raw.get("schema_version") != _SCHEMA_VERSION
        or not isinstance(raw.get("entries"), list)
    ):
        raise ValueError("unversioned backtest facts schema is invalid")

    result: set[UnversionedBacktestFacts] = set()
    identities: set[tuple[str, str, int]] = set()
    for value in raw["entries"]:
        if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
            raise ValueError("unversioned backtest facts entry is invalid")
        entry = UnversionedBacktestFacts(**value)
        if not _valid_entry(entry):
            raise ValueError("unversioned backtest facts fields are invalid")
        identity = (entry.scheme_id, entry.target_tenor, entry.horizon)
        if identity in identities:
            raise ValueError("duplicate unversioned backtest facts entry")
        identities.add(identity)
        result.add(entry)
    return frozenset(result)


def _valid_entry(entry: UnversionedBacktestFacts) -> bool:
    return (
        isinstance(entry.scheme_id, str)
        and SCHEME_ID_PATTERN.fullmatch(entry.scheme_id) is not None
        and isinstance(entry.target_tenor, str)
        and re.fullmatch(r"[1-9][0-9]*Y", entry.target_tenor) is not None
        and isinstance(entry.horizon, int)
        and not isinstance(entry.horizon, bool)
        and entry.horizon > 0
        and isinstance(entry.expected_fact_count, int)
        and not isinstance(entry.expected_fact_count, bool)
        and entry.expected_fact_count > 0
        and isinstance(entry.facts_sha256, str)
        and _SHA256_PATTERN.fullmatch(entry.facts_sha256) is not None
        and isinstance(entry.reason, str)
        and bool(entry.reason.strip())
        and entry.reason == entry.reason.strip()
    )
