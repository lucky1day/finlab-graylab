from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from shared import data_service as _data_service
from shared.artifact_paths import BACKTEST_ARTIFACT_ROOT, RUNTIME_INPUT_ROOT, safe_path_part
from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    BlackboxSnapshot,
    CutoffKeys,
    create_snapshot_from_frames,
    resolve_cutoffs,
)
from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    check_current_dataset,
)

DEFAULT_OUTPUT_ROOT = RUNTIME_INPUT_ROOT
BLACKBOX_SNAPSHOT_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "snapshots"
BLACKBOX_RUNTIME_SNAPSHOT_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "runtime_snapshots"
BLACKBOX_SCHEMA_PATH = Path(__file__).with_name("blackbox_v2") / "data_bridge_v1_schema.json"
DATA_BRIDGE_ROOT = Path(__file__).resolve().parents[1] / "data" / "data_bridge"
DATA_BRIDGE_REFRESH_RUNTIME_ROOT = BACKTEST_ARTIFACT_ROOT / "data_bridge_refresh"
DAILY_DATA_VERSION = "shared_data_service_daily.v1"
WEEKLY_DATA_VERSION = "shared_data_service_weekly.v1"
MONTHLY_DATA_VERSION = "shared_data_service_monthly.v1"
_FREQUENCY_FILE_PREFIXES = {
    "daily": "daily_output",
    "weekly": "weekly_output",
    "monthly": "monthly_output",
}


def create_input_engine():
    """创建输入 artifact 构建所需 DB engine，避免 adapter 直接依赖 data_service。"""
    return _data_service.create_sqlalchemy_engine()


@dataclass(frozen=True)
class InputArtifact:
    """预测算法输入文件及读回后的 DataFrame。"""

    scheme_id: str
    frequency: str
    path: Path
    dataframe: pd.DataFrame
    source: str
    generated_at: str
    data_version: str
    artifact_id: str
    content_hash: str
    schema_hash: str
    source_watermark: str | None
    row_count: int
    column_count: int
    columns: list[str]
    date_coverage: dict[str, Any]
    quality_flags: dict[str, Any]
    metadata: dict[str, Any]


def build_blackbox_input_snapshot(
    *,
    snapshot_date: str,
    engine=None,
    output_root: str | Path = BLACKBOX_SNAPSHOT_ROOT,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    data_root: str | Path = DATA_BRIDGE_ROOT,
    refresh_runtime_root: str | Path = DATA_BRIDGE_REFRESH_RUNTIME_ROOT,
    require_fresh: bool = False,
) -> BlackboxSnapshot:
    """从平台当前三频文件生成一份内容寻址的 Blackbox V2 快照。"""
    schema_version, expected_columns = _load_blackbox_schema(schema_path)
    dataset = _load_current_data_bridge_dataset(
        snapshot_date=snapshot_date,
        schema_path=Path(schema_path),
        data_root=Path(data_root),
        refresh_runtime_root=Path(refresh_runtime_root),
        require_fresh=require_fresh,
    )
    return create_snapshot_from_frames(
        dataset.frames,
        output_root=Path(output_root),
        expected_columns=expected_columns,
        schema_version=schema_version,
    )


@contextmanager
def open_blackbox_input_snapshot(
    *,
    snapshot_date: str,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    data_root: str | Path = DATA_BRIDGE_ROOT,
    refresh_runtime_root: str | Path = DATA_BRIDGE_REFRESH_RUNTIME_ROOT,
    snapshot_runtime_root: str | Path = BLACKBOX_RUNTIME_SNAPSHOT_ROOT,
    require_fresh: bool = False,
):
    """复制当前三频文件供一次算法运行使用，并在运行后删除副本。"""
    runtime_root = Path(snapshot_runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="blackbox-input-", dir=runtime_root))
    try:
        snapshot = build_blackbox_input_snapshot(
            snapshot_date=snapshot_date,
            output_root=temporary,
            schema_path=schema_path,
            data_root=data_root,
            refresh_runtime_root=refresh_runtime_root,
            require_fresh=require_fresh,
        )
        yield snapshot
    finally:
        _make_tree_writable(temporary)
        shutil.rmtree(temporary, ignore_errors=True)


def _load_current_data_bridge_dataset(
    *,
    snapshot_date: str,
    schema_path: Path,
    data_root: Path,
    refresh_runtime_root: Path,
    require_fresh: bool,
):
    current = check_current_dataset(
        DataBridgeRefreshConfig(
            data_root=data_root,
            runtime_root=refresh_runtime_root,
            schema_path=schema_path,
        ),
        required_refresh_date=snapshot_date if require_fresh else None,
    )
    return current.dataset


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            path.chmod(0o755 if path.is_dir() else 0o644)
        except OSError:
            pass
    try:
        root.chmod(0o755)
    except OSError:
        pass


def resolve_blackbox_input_cutoffs(
    snapshot: BlackboxSnapshot,
    *,
    feature_date: str,
    engine=None,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> CutoffKeys:
    """从数据桥 as-of 输出解析截止键，不在算法层推导周/月标识。"""
    _, expected_columns = _load_blackbox_schema(schema_path)
    own_engine = engine is None
    engine = engine or _data_service.create_sqlalchemy_engine()
    try:
        weekly_as_of = _data_service.build_weekly_output_from_db(
            schema_columns=expected_columns["weekly_output.csv"],
            as_of_date=feature_date,
            engine=engine,
        )
        monthly_as_of = _data_service.build_monthly_output_from_db(
            end_date=feature_date,
            engine=engine,
        )
        return resolve_cutoffs(
            snapshot,
            feature_date,
            weekly_as_of=weekly_as_of,
            monthly_as_of=monthly_as_of,
        )
    finally:
        if own_engine:
            engine.dispose()


def input_artifact_path(
    *,
    scheme_id: str,
    frequency: str,
    predict_date: str,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    """生成统一的输入文件路径。"""
    safe_scheme_id = safe_path_part(scheme_id)
    safe_predict_date = str(predict_date).replace("/", "-").replace(":", "-")
    prefix = _FREQUENCY_FILE_PREFIXES.get(str(frequency))
    if prefix is None:
        raise ValueError(f"unsupported frequency for input artifact path: {frequency}")
    return Path(output_root) / safe_scheme_id / f"{prefix}_{safe_predict_date}.csv"


def build_daily_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    start_date: str,
    end_date: str,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """通过日频 data_service 生成输入 CSV，再读回给算法。"""
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        output_root=output_root,
    )
    df = _data_service.build_daily_output_from_db(
        start_date=start_date,
        end_date=end_date,
        engine=engine,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _data_service.save_daily_output(df, path)
    read_back = _read_daily_output_csv(path)
    profile = _dataframe_profile(read_back, coverage_field="date", required_columns=("date",))
    content_hash = _file_sha256(path)
    schema_hash = _schema_hash(read_back)
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="daily",
        path=path,
        dataframe=read_back,
        source="shared_data_service_daily",
        generated_at=_utc_now(),
        data_version=DAILY_DATA_VERSION,
        artifact_id=_artifact_id(scheme_id, predict_date, content_hash),
        content_hash=content_hash,
        schema_hash=schema_hash,
        source_watermark=_source_watermark(profile),
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata={
            "start_date": start_date,
            "end_date": end_date,
            "predict_date": predict_date,
        },
    )


def build_weekly_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    schema_columns: list[str] | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    as_of_date: str | None = None,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """生成周频输入 CSV，再读回给算法。"""
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="weekly",
        predict_date=predict_date,
        output_root=output_root,
    )
    df = _data_service.build_weekly_output_from_db(
        schema_columns=schema_columns,
        start_week=start_week,
        end_week=end_week,
        as_of_date=as_of_date,
        engine=engine,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _data_service.save_weekly_output(df, path)
    read_back = pd.read_csv(path)
    if "week_id" in read_back.columns:
        read_back["week_id"] = pd.to_numeric(read_back["week_id"], errors="coerce").astype("Int64")
    for col in read_back.columns:
        if col != "week_id":
            read_back[col] = pd.to_numeric(read_back[col], errors="coerce")
    profile = _dataframe_profile(read_back, coverage_field="week_id", required_columns=("week_id",))
    content_hash = _file_sha256(path)
    schema_hash = _schema_hash(read_back)
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="weekly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_weekly",
        generated_at=_utc_now(),
        data_version=WEEKLY_DATA_VERSION,
        artifact_id=_artifact_id(scheme_id, predict_date, content_hash),
        content_hash=content_hash,
        schema_hash=schema_hash,
        source_watermark=_source_watermark(profile),
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata={
            "start_week": start_week,
            "end_week": end_week,
            "as_of_date": as_of_date,
            "predict_date": predict_date,
        },
    )


def build_monthly_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    start_date: str,
    end_date: str,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """通过月频 data_service 生成输入 CSV，再读回给算法。"""
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="monthly",
        predict_date=predict_date,
        output_root=output_root,
    )
    df = _data_service.build_monthly_output_from_db(
        start_date=start_date,
        end_date=end_date,
        engine=engine,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _data_service.save_monthly_output(df, path)
    read_back = _read_monthly_output_csv(path)
    profile = _dataframe_profile(read_back, coverage_field="month_id", required_columns=("month_id",))
    content_hash = _file_sha256(path)
    schema_hash = _schema_hash(read_back)
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="monthly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_monthly",
        generated_at=_utc_now(),
        data_version=MONTHLY_DATA_VERSION,
        artifact_id=_artifact_id(scheme_id, predict_date, content_hash),
        content_hash=content_hash,
        schema_hash=schema_hash,
        source_watermark=_source_watermark(profile),
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata={
            "start_date": start_date,
            "end_date": end_date,
            "predict_date": predict_date,
        },
    )


def _dataframe_profile(
    df: pd.DataFrame,
    *,
    coverage_field: str,
    required_columns: tuple[str, ...],
) -> dict[str, Any]:
    columns = [str(col) for col in df.columns]
    missing_required = [col for col in required_columns if col not in columns]
    date_coverage: dict[str, Any] = {"field": coverage_field, "start": None, "end": None}
    null_coverage_rows = 0
    duplicate_coverage_values = 0

    if coverage_field in df.columns:
        values = df[coverage_field]
        null_coverage_rows = int(values.isna().sum())
        non_null = values.dropna()
        duplicate_coverage_values = int(non_null.duplicated().sum())
        start, end = _coverage_bounds(non_null, coverage_field)
        date_coverage["start"] = start
        date_coverage["end"] = end

    return {
        "row_count": int(len(df)),
        "column_count": int(len(columns)),
        "columns": columns,
        "date_coverage": date_coverage,
        "quality_flags": {
            "missing_required_columns": missing_required,
            "empty_frame": bool(df.empty),
            "null_coverage_rows": null_coverage_rows,
            "duplicate_coverage_values": duplicate_coverage_values,
        },
    }


def _coverage_bounds(values: pd.Series, coverage_field: str) -> tuple[Any, Any]:
    if values.empty:
        return None, None
    if coverage_field == "date":
        parsed = pd.to_datetime(values, errors="coerce").dropna()
        if parsed.empty:
            return None, None
        return parsed.min().strftime("%Y-%m-%d"), parsed.max().strftime("%Y-%m-%d")
    if coverage_field == "week_id":
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        if numeric.empty:
            return None, None
        return int(numeric.min()), int(numeric.max())
    if coverage_field == "month_id":
        cleaned = values.dropna().astype(str).str.strip()
        cleaned = cleaned[cleaned.str.fullmatch(r"\d{6}", na=False)]
        if cleaned.empty:
            return None, None
        return str(cleaned.min()), str(cleaned.max())
    return values.min(), values.max()


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _schema_hash(df: pd.DataFrame) -> str:
    schema = sorted((str(column), str(df[column].dtype)) for column in df.columns)
    payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact_id(scheme_id: str, predict_date: str, content_hash: str) -> str:
    payload = json.dumps(
        {
            "scheme_id": scheme_id,
            "predict_date": predict_date,
            "content_hash": content_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_watermark(profile: dict[str, Any]) -> str | None:
    end = profile["date_coverage"].get("end")
    if end is None:
        return None
    return str(end)


def _read_daily_output_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"daily input artifact missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _read_monthly_output_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "month_id" not in df.columns:
        raise ValueError(f"monthly input artifact missing month_id column: {path}")
    df["month_id"] = df["month_id"].fillna("").astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    df = df[df["month_id"].str.fullmatch(r"\d{6}", na=False)].copy()
    df = df.sort_values("month_id").reset_index(drop=True)
    for col in df.columns:
        if col != "month_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


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
    return schema_version, columns


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
