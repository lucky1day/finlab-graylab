"""原始源表旁路产物 `raw_wind_v1`：把声明需要原始数据库表内容的存量二进制算法所读的 9 张表导出为只读文件。

DataBridge 的 `current` 目录必须正好是五个数据文件，所以这份产物独立于 DataBridge，按内容寻址：
`<root>/<artifact_id>/` 内含 9 个 `raw_wind__<表名>.csv` 与 `raw_wind_manifest.json`。

文件格式（交付外壳是单文件、不能导入平台代码，须自行实现同一读取规则，故刻意保持简单）：
UTF-8、LF、标准 CSV、固定列序、确定性排序；`\\N` 表示 NULL（以区分空字符串）；以反斜杠开头的字符串
在前面再加一个反斜杠；浮点写 `repr(float(x))`；日期时间写 `str(value)`。manifest 记录每列的类型
（`str`/`float`/`int`/`datetime`）、行数、字节数与 sha256。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text

RAW_WIND_SCHEMA_VERSION = "raw-wind-v1"
MANIFEST_FILENAME = "raw_wind_manifest.json"
NULL_TOKEN = "\\N"
_PARTITION_ROWS = 50_000
_KINDS = ("str", "float", "int", "datetime")


@dataclass(frozen=True)
class RawWindTable:
    """一张源表的导出规格：列、非空过滤与确定性排序。"""

    name: str
    columns: tuple[str, ...] | None  # None 表示 SELECT *
    non_null: tuple[str, ...]
    order_by: tuple[str, ...]


_LONG = ("rdate", "indicators_code", "indicators_value")
_ORDER = ("rdate", "indicators_code", "id")
RAW_WIND_TABLES: tuple[RawWindTable, ...] = (
    RawWindTable("api_wind_daily", _LONG, ("indicators_value",), _ORDER),
    RawWindTable("api_wind_derivative_daily", _LONG, ("indicators_value",), _ORDER),
    RawWindTable("api_wind_weekly", ("rdate", "week_id", "indicators_code", "indicators_value"), ("indicators_value",), _ORDER),
    RawWindTable("api_wind_derivative_weekly", ("rdate", "week_id", "indicators_code", "indicators_value"), ("indicators_value",), _ORDER),
    RawWindTable("api_wind_monthly", _LONG, ("indicators_value",), _ORDER),
    RawWindTable("api_wind_derivative_monthly", ("rdate", "month_id", "indicators_code", "indicators_value"), ("indicators_value",), _ORDER),
    RawWindTable("api_wind_date", ("rdate", "week_id"), ("rdate", "week_id"), ("rdate",)),
    RawWindTable("t_trade_calendar", ("rdate", "trade_flag"), (), ("rdate",)),
    RawWindTable("api_wind_indicators_all", None, (), ("id",)),
)
RAW_WIND_FILENAMES: tuple[str, ...] = tuple(f"raw_wind__{table.name}.csv" for table in RAW_WIND_TABLES)
# 进入沙箱数据目录的文件：9 个数据文件加 manifest（读取方据此还原列类型并校验哈希）
RAW_WIND_DATA_FILENAMES: tuple[str, ...] = (*RAW_WIND_FILENAMES, MANIFEST_FILENAME)


class RawWindError(ValueError):
    """原始源表旁路产物导出或校验失败。"""


@dataclass(frozen=True)
class RawWindArtifact:
    artifact_id: str
    path: Path
    manifest: Mapping[str, Any]

    @property
    def manifest_sha256(self) -> str:
        return _manifest_sha256(self.manifest)


def raw_wind_filename(table_name: str) -> str:
    return f"raw_wind__{table_name}.csv"


def encode_cell(value: Any) -> tuple[str, str | None]:
    """返回 (CSV 单元格文本, 该值的类型)；NULL 的类型为 None。"""
    if value is None:
        return NULL_TOKEN, None
    if isinstance(value, bool):
        return str(int(value)), "int"
    if isinstance(value, (Decimal, float)):
        return repr(float(value)), "float"
    if isinstance(value, int):
        return str(value), "int"
    if isinstance(value, (datetime, date)):
        return str(value), "datetime"
    if isinstance(value, (bytes, bytearray)):
        raise RawWindError("binary columns are not supported in raw wind export")
    text_value = str(value)
    return ("\\" + text_value if text_value.startswith("\\") else text_value), "str"


def decode_cell(cell: str) -> str | None:
    """`encode_cell` 的字符串层逆运算：`\\N` → None；开头 `\\\\` → 去掉一个反斜杠。"""
    if cell == NULL_TOKEN:
        return None
    if cell.startswith("\\\\"):
        return cell[1:]
    return cell


def export_raw_wind(
    connection: Any,
    output_root: str | Path,
    *,
    generation_id: str,
    refresh_date: str,
) -> RawWindArtifact:
    """在调用方已建立的一致性快照连接上导出 9 张表，并以内容寻址目录原子发布。"""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RawWindError("raw wind artifact root must be a real directory")
    staging = Path(tempfile.mkdtemp(prefix=".raw-wind-staging-", dir=root))
    try:
        tables: dict[str, Any] = {}
        for table in RAW_WIND_TABLES:
            tables[table.name] = _export_table(connection, table, staging / raw_wind_filename(table.name))
        manifest = {
            "schema_version": RAW_WIND_SCHEMA_VERSION,
            "generation_id": str(generation_id),
            "refresh_date": str(refresh_date),
            "tables": tables,
        }
        artifact_id = f"raw-wind-{_manifest_sha256(manifest)[:24]}"
        manifest["artifact_id"] = artifact_id
        (staging / MANIFEST_FILENAME).write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        destination = root / artifact_id
        if destination.exists():
            verify_raw_wind_artifact(destination)
            _remove_tree(staging)
        else:
            os.rename(staging, destination)
            _seal(destination)  # 目录只读后无法再改名，所以发布之后再封存；使用方总会 verify
        return RawWindArtifact(artifact_id=artifact_id, path=destination, manifest=manifest)
    except BaseException:
        _remove_tree(staging)
        raise


def read_raw_wind_manifest(directory: str | Path) -> dict[str, Any]:
    """读取并校验 manifest 的自洽性（artifact_id 与内容哈希一致、表集合与文件名固定）。"""
    path = Path(directory) / MANIFEST_FILENAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RawWindError(f"raw wind manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != RAW_WIND_SCHEMA_VERSION:
        raise RawWindError("raw wind manifest schema_version is invalid")
    declared = manifest.get("artifact_id")
    if declared != f"raw-wind-{_manifest_sha256(manifest)[:24]}" or Path(directory).name != declared:
        raise RawWindError("raw wind artifact identity does not match its manifest")
    tables = manifest.get("tables")
    if not isinstance(tables, dict) or sorted(tables) != sorted(table.name for table in RAW_WIND_TABLES):
        raise RawWindError("raw wind manifest table set is invalid")
    return manifest


def verify_raw_wind_artifact(directory: str | Path) -> dict[str, Any]:
    """逐文件重算 sha256/字节数，并要求目录内容正好是 9 个数据文件加 manifest。"""
    root = Path(directory)
    manifest = read_raw_wind_manifest(root)
    expected = {*RAW_WIND_FILENAMES, MANIFEST_FILENAME}
    entries = {entry.name for entry in root.iterdir()}
    if entries != expected:
        raise RawWindError(f"raw wind artifact files mismatch: missing={sorted(expected - entries)} extra={sorted(entries - expected)}")
    for table in RAW_WIND_TABLES:
        info = manifest["tables"][table.name]
        path = root / raw_wind_filename(table.name)
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise RawWindError(f"raw wind file is not a regular file: {path.name}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != info["sha256"] or details.st_size != info["bytes"]:
            raise RawWindError(f"raw wind file does not match its manifest: {path.name}")
    return manifest


def read_raw_wind_frame(directory: str | Path, table_name: str) -> Any:
    """参考读取实现：还原为与 `pandas.read_sql` 同 dtype 的 DataFrame（NULL→None/NaN/NaT）。

    浮点必须按精确解析（逐值 `float()`），不能用 pandas 默认的快速解析器，否则末位可能不同。
    交付外壳需自行实现等价逻辑，并以本函数作为验证基准。
    """
    import numpy as np
    import pandas as pd

    manifest = read_raw_wind_manifest(directory)
    info = manifest["tables"].get(table_name)
    if info is None:
        raise RawWindError(f"unknown raw wind table: {table_name}")
    raw = pd.read_csv(Path(directory) / info["file"], dtype=str, keep_default_na=False, na_filter=False)
    if list(raw.columns) != info["columns"] or len(raw) != info["rows"]:
        raise RawWindError(f"raw wind table shape does not match its manifest: {table_name}")
    columns: dict[str, Any] = {}
    for name in info["columns"]:
        cells = raw[name].tolist()
        kind = info["kinds"][name]
        if kind == "float":
            columns[name] = np.array([np.nan if cell == NULL_TOKEN else float(cell) for cell in cells], dtype="float64")
        elif kind == "int":
            values = [None if cell == NULL_TOKEN else int(cell) for cell in cells]
            columns[name] = (
                pd.Series(values, dtype="float64") if any(value is None for value in values) else pd.Series(values, dtype="int64")
            )
        elif kind == "datetime":
            columns[name] = pd.to_datetime(pd.Series([None if cell == NULL_TOKEN else cell for cell in cells]))
        else:
            columns[name] = pd.Series([decode_cell(cell) for cell in cells], dtype=object)
    return pd.DataFrame(columns, columns=info["columns"])


def _export_table(connection: Any, table: RawWindTable, destination: Path) -> dict[str, Any]:
    columns_sql = "*" if table.columns is None else ", ".join(table.columns)
    where = " AND ".join(f"{column} IS NOT NULL" for column in table.non_null)
    sql = f"SELECT {columns_sql} FROM {table.name}" + (f" WHERE {where}" if where else "") + f" ORDER BY {', '.join(table.order_by)}"
    kinds: dict[str, str | None] = {}
    rows = 0
    digest = hashlib.sha256()
    size = 0
    result = connection.execute(text(sql).execution_options(stream_results=True))
    names = list(result.keys())
    with destination.open("wb") as raw:
        buffer = _Sink(raw, digest)
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(names)
        for block in result.partitions(_PARTITION_ROWS):
            for row in block:
                cells = []
                for name, value in zip(names, row, strict=True):
                    cell, kind = encode_cell(value)
                    cells.append(cell)
                    if kind is not None:
                        if kinds.setdefault(name, kind) != kind and {kinds[name], kind} != {"int", "float"}:
                            raise RawWindError(f"column {table.name}.{name} mixes {kinds[name]} and {kind}")
                        if kind == "float":
                            kinds[name] = "float"
                writer.writerow(cells)
                rows += 1
        size = buffer.bytes_written
    return {
        "file": destination.name,
        "columns": names,
        "kinds": {name: kinds.get(name, "str") for name in names},
        "rows": rows,
        "bytes": size,
        "sha256": digest.hexdigest(),
    }


class _Sink:
    """把 csv.writer 的文本写成 UTF-8 字节并同步累计哈希与字节数。"""

    def __init__(self, raw: Any, digest: Any) -> None:
        self._raw, self._digest, self.bytes_written = raw, digest, 0

    def write(self, data: str) -> int:
        encoded = data.encode("utf-8")
        self._raw.write(encoded)
        self._digest.update(encoded)
        self.bytes_written += len(encoded)
        return len(data)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _manifest_sha256(manifest: Mapping[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "artifact_id"}
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _seal(directory: Path) -> None:
    for entry in directory.iterdir():
        entry.chmod(0o444)
    directory.chmod(0o555)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for entry in [path, *path.rglob("*")]:
        try:
            entry.chmod(0o755 if entry.is_dir() else 0o644)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)
