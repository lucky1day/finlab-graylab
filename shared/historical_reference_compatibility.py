from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from shared.scheme_config_schema import SCHEME_ID_PATTERN


@dataclass(frozen=True, slots=True)
class HistoricalBacktestReference:
    """已批准的产品身份到只读历史回测来源关系。"""

    prediction_scheme_id: str
    backtest_source_scheme_id: str
    reason: str


def load_historical_backtest_references(
    project_root: str | Path,
) -> frozenset[HistoricalBacktestReference]:
    """严格读取版本化历史引用兼容清单。"""
    path = (
        Path(project_root)
        / "deploy"
        / "historical_backtest_reference_compatibility_v1.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "relationships"}
        or raw.get("schema_version")
        != "historical-backtest-reference-compatibility-v1"
        or not isinstance(raw.get("relationships"), list)
    ):
        raise ValueError("historical reference compatibility schema is invalid")
    result: set[HistoricalBacktestReference] = set()
    pairs: set[tuple[str, str]] = set()
    for value in raw["relationships"]:
        if not isinstance(value, dict) or set(value) != {
            "prediction_scheme_id",
            "backtest_source_scheme_id",
            "reason",
        }:
            raise ValueError("historical reference relationship is invalid")
        prediction = value["prediction_scheme_id"]
        source = value["backtest_source_scheme_id"]
        reason = value["reason"]
        if (
            not isinstance(prediction, str)
            or not SCHEME_ID_PATTERN.fullmatch(prediction)
            or not isinstance(source, str)
            or not SCHEME_ID_PATTERN.fullmatch(source)
            or prediction == source
            or not isinstance(reason, str)
            or not reason.strip()
            or reason != reason.strip()
        ):
            raise ValueError("historical reference relationship fields are invalid")
        pair = (prediction, source)
        if pair in pairs:
            raise ValueError("duplicate historical reference relationship")
        pairs.add(pair)
        result.add(HistoricalBacktestReference(prediction, source, reason))
    return frozenset(result)
