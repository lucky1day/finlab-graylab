"""W4 存量 Native 输入 artifact 构建与作业内复用。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import (
    Any,
    Callable,
    Mapping,
)
import pandas as pd

from shared import data_service as _data_service
from shared._input_artifacts.contract import (
    BLACKBOX_SCHEMA_PATH,
    DAILY_DATA_VERSION,
    DEFAULT_OUTPUT_ROOT,
    EPHEMERAL_NATIVE_INPUT_ROOT_ENV,
    InputArtifact,
    MONTHLY_DATA_VERSION,
    WEEKLY_DATA_VERSION,
    _load_blackbox_schema,
)
from shared._input_artifacts.files import _fsync_directory


_FREQUENCY_FILE_PREFIXES = {
    "daily": "daily_output",
    "weekly": "weekly_output",
    "monthly": "monthly_output",
}


def create_input_engine(*, database_config: Any | None = None):
    """创建当前权威数据库输入源。"""
    if database_config is None:
        return _data_service.create_sqlalchemy_engine()
    return _data_service.create_sqlalchemy_engine(
        db_config=database_config,
    )


def safe_path_part(value: str) -> str:
    """Return a filesystem-safe path segment for scheme and benchmark ids."""
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value))


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
    filename = f"{prefix}_{safe_predict_date}.csv"
    ephemeral_root = os.environ.get(EPHEMERAL_NATIVE_INPUT_ROOT_ENV)
    if ephemeral_root is not None:
        root = Path(ephemeral_root)
        if not root.is_absolute():
            raise ValueError(
                f"{EPHEMERAL_NATIVE_INPUT_ROOT_ENV} must be absolute"
            )
        return root / "views" / safe_scheme_id / filename
    return Path(output_root) / safe_scheme_id / filename


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
    schema_columns = _legacy_native_schema_columns("daily_output.csv")
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        output_root=output_root,
    )
    metadata = {
        "start_date": start_date,
        "end_date": end_date,
        "predict_date": predict_date,
        "schema_columns": schema_columns,
    }
    read_back = _build_or_reuse_native_csv(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        data_version=DAILY_DATA_VERSION,
        metadata=metadata,
        view_path=path,
        build=lambda: _data_service.build_daily_output_from_db(
            start_date=start_date,
            end_date=end_date,
            engine=engine,
            schema_columns=schema_columns,
        ),
        save=_data_service.save_daily_output,
        read=_read_daily_output_csv,
    )
    artifact = InputArtifact(
        scheme_id=scheme_id,
        frequency="daily",
        path=path,
        dataframe=read_back,
        source="shared_data_service_daily",
        data_version=DAILY_DATA_VERSION,
    )
    return artifact


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
    effective_schema_columns = (
        list(schema_columns)
        if schema_columns is not None
        else _legacy_native_schema_columns("weekly_output.csv")
    )
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="weekly",
        predict_date=predict_date,
        output_root=output_root,
    )
    metadata = {
        "schema_columns": effective_schema_columns,
        "preserve_metadata_lags": schema_columns is None,
        "start_week": start_week,
        "end_week": end_week,
        "as_of_date": as_of_date,
        "predict_date": predict_date,
    }
    read_back = _build_or_reuse_native_csv(
        scheme_id=scheme_id,
        frequency="weekly",
        predict_date=predict_date,
        data_version=WEEKLY_DATA_VERSION,
        metadata=metadata,
        view_path=path,
        build=lambda: _data_service.build_weekly_output_from_db(
            schema_columns=effective_schema_columns,
            start_week=start_week,
            end_week=end_week,
            as_of_date=as_of_date,
            engine=engine,
            preserve_metadata_lags=schema_columns is None,
        ),
        save=_data_service.save_weekly_output,
        read=_read_weekly_output_csv,
    )
    artifact = InputArtifact(
        scheme_id=scheme_id,
        frequency="weekly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_weekly",
        data_version=WEEKLY_DATA_VERSION,
    )
    return artifact


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
    schema_columns = _legacy_native_schema_columns("monthly_output.csv")
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="monthly",
        predict_date=predict_date,
        output_root=output_root,
    )
    metadata = {
        "start_date": start_date,
        "end_date": end_date,
        "predict_date": predict_date,
        "schema_columns": schema_columns,
    }
    read_back = _build_or_reuse_native_csv(
        scheme_id=scheme_id,
        frequency="monthly",
        predict_date=predict_date,
        data_version=MONTHLY_DATA_VERSION,
        metadata=metadata,
        view_path=path,
        build=lambda: _data_service.build_monthly_output_from_db(
            start_date=start_date,
            end_date=end_date,
            engine=engine,
            schema_columns=schema_columns,
        ),
        save=_data_service.save_monthly_output,
        read=_read_monthly_output_csv,
    )
    artifact = InputArtifact(
        scheme_id=scheme_id,
        frequency="monthly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_monthly",
        data_version=MONTHLY_DATA_VERSION,
    )
    return artifact


def _legacy_native_schema_columns(filename: str) -> list[str]:
    _, columns = _load_blackbox_schema(BLACKBOX_SCHEMA_PATH)
    selected = list(columns[filename])
    if filename == "monthly_output.csv":
        additions = set(_data_service.DATA_BRIDGE_V1_ADDITIVE_MONTHLY_CODES)
        selected = [
            column
            for column in selected
            if column == "month_id" or column not in additions
        ]
    return selected


def _build_or_reuse_native_csv(
    *,
    scheme_id: str,
    frequency: str,
    predict_date: str,
    data_version: str,
    metadata: Mapping[str, Any],
    view_path: Path,
    build: Callable[[], pd.DataFrame],
    save: Callable[[pd.DataFrame, str | Path], None],
    read: Callable[[str | Path], pd.DataFrame],
) -> pd.DataFrame:
    """同一临时作业内按完整 builder 参数复用只读 CSV。"""
    configured = str(os.environ.get(EPHEMERAL_NATIVE_INPUT_ROOT_ENV) or "").strip()
    if not configured:
        frame = build()
        _atomic_save_output(save, frame, view_path)
        return read(view_path)

    root = Path(configured)
    if not root.is_absolute():
        raise ValueError(f"{EPHEMERAL_NATIVE_INPUT_ROOT_ENV} must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    source_scope = (
        f"isolated:{scheme_id}"
        if str(os.environ.get("BFL_SOURCE_DB_CONFIG_PATH") or "").strip()
        else "service-db"
    )
    identity_payload = {
        "frequency": frequency,
        "predict_date": predict_date,
        "data_version": data_version,
        "source_scope": source_scope,
        "metadata": dict(metadata),
    }
    identity = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    objects = root / "objects"
    locks = root / "locks"
    objects.mkdir(mode=0o700, parents=True, exist_ok=True)
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    object_path = objects / f"{identity}.csv"
    lock_path = locks / f"{identity}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if not object_path.exists():
                frame = build()
                _atomic_save_output(save, frame, object_path)
                object_path.chmod(0o444)
            _link_native_input_view(object_path, view_path)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return read(view_path)


def _link_native_input_view(object_path: Path, view_path: Path) -> None:
    """为单方案建立同一只读 inode 的路径，不复制输入内容。"""
    view_path.parent.mkdir(parents=True, exist_ok=True)
    if view_path.exists():
        if os.path.samefile(object_path, view_path):
            return
        raise OSError(f"native input view already exists: {view_path}")
    os.link(object_path, view_path)


def _atomic_save_output(save_output, frame: pd.DataFrame, path: Path) -> None:
    """同目录写临时文件，fsync 后原子替换，失败时保留旧完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        save_output(frame, temporary)
        if temporary.is_symlink() or not temporary.is_file():
            raise OSError(
                f"input artifact writer did not create a regular file: "
                f"{temporary}"
            )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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


def _read_weekly_output_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "week_id" in df.columns:
        df["week_id"] = pd.to_numeric(
            df["week_id"], errors="coerce"
        ).astype("Int64")
    for col in df.columns:
        if col != "week_id":
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
