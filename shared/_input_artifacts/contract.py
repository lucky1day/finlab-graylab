"""输入文件位置、版本和纯 schema 合同。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import pandas as pd

from shared.artifact_paths import (
    BACKTEST_ARTIFACT_ROOT,
    RUNTIME_INPUT_ROOT,
)
from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES


DEFAULT_OUTPUT_ROOT = RUNTIME_INPUT_ROOT
BLACKBOX_SNAPSHOT_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "snapshots"
BLACKBOX_GENERATION_SNAPSHOT_ROOT = (
    BLACKBOX_SNAPSHOT_ROOT / "generation_cache"
)
BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION = "blackbox-generation-snapshot-v3"
LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION = (
    "blackbox-generation-snapshot-v2"
)
BLACKBOX_RUNTIME_VIEW_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "runtime_views"
BLACKBOX_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "blackbox_v2" / "data_bridge_v1_schema.json"
BLACKBOX_SCHEMA_VERSION = "data-bridge-v1"
BLACKBOX_SCHEMA_CONTRACT_SHA256 = (
    "130c1acbb1cd13d49155334d3bba57f43896bf26d1ec7983c188eb93a7d0cf28"
)
LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256 = (
    "addf732eb35071f89493073d22f4bf6ef79a55d7ceaf554dd9cf2c41e4dc6db3"
)
DAILY_DATA_VERSION = "shared_data_service_daily.v1"
WEEKLY_DATA_VERSION = "shared_data_service_weekly.v1"
MONTHLY_DATA_VERSION = "shared_data_service_monthly.v1"
EPHEMERAL_NATIVE_INPUT_ROOT_ENV = "BOND_NATIVE_EPHEMERAL_INPUT_ROOT"


@dataclass(frozen=True)
class InputArtifact:
    """预测算法输入文件及读回后的 DataFrame。"""

    scheme_id: str
    frequency: str
    path: Path
    dataframe: pd.DataFrame
    source: str
    data_version: str


def _load_blackbox_schema(path: str | Path) -> tuple[str, dict[str, list[str]]]:
    schema_path = Path(path)
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Blackbox V2 data schema {schema_path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "files"}:
        raise ValueError("Blackbox V2 data schema must contain only schema_version and files")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise ValueError("Blackbox V2 data schema_version must be a non-empty string")
    files = raw.get("files")
    if not isinstance(files, dict) or set(files) != set(SNAPSHOT_FILENAMES):
        raise ValueError(f"Blackbox V2 data schema files must be exactly {list(SNAPSHOT_FILENAMES)}")
    columns: dict[str, list[str]] = {}
    for filename in SNAPSHOT_FILENAMES:
        file_schema = files.get(filename)
        if not isinstance(file_schema, dict) or set(file_schema) != {"columns"}:
            raise ValueError(f"{filename} schema must contain only columns")
        values = file_schema.get("columns")
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"{filename} columns must be a non-empty string list")
        if len(values) != len(set(values)):
            raise ValueError(f"{filename} schema contains duplicate columns")
        columns[filename] = list(values)
    if columns["daily_output.csv"][0] != "date":
        raise ValueError("daily_output.csv schema must start with date")
    if columns["weekly_output.csv"][0] != "week_id":
        raise ValueError("weekly_output.csv schema must start with week_id")
    if columns["monthly_output.csv"][0] != "month_id":
        raise ValueError("monthly_output.csv schema must start with month_id")
    if columns["factor_catalog.csv"] != [
        "indicators_code",
        "frequency",
        "factor_version",
    ]:
        raise ValueError("factor_catalog.csv schema columns are invalid")
    return schema_version, columns
