"""平台批准的封装交付：把存量 W4 `daily_0629` 二进制算法包装成 Blackbox V2 交付（内嵌运行时外壳）。

算法本体是上游以编译产物交付的二进制包（已嵌入本文件，生成时校验哈希；唯一改动是把含明文口令的
`db_config.py` 换成不含任何凭据的占位模块）。外壳只做四件事：
1. 校验并解压内嵌二进制包到工作目录；
2. 从 `--data-dir` 读取平台导出的原始源表（`raw_wind__*.csv` 与 manifest），校验哈希并还原为与
   `pandas.read_sql` 同 dtype 的 DataFrame；
3. 用 `sys.modules` 里的严格白名单假模块替换 `sqlalchemy`/`pymysql`，并让 `pandas.read_sql*` 只从这些
   DataFrame 作答（未知查询一律报错），使二进制在没有数据库的情况下得到与原来逐位相同的输入；
4. 以请求的 `target_date` 为运行日进程内运行 `daily.run_daily --dry-run --frequencies <频率>`，读取选中行，
   校验日期上下文后映射为 V2 输出；运行产物每次用完即删。

本文件刻意只依赖标准库与 numpy/pandas，且不导入 subprocess/sqlalchemy/pymysql 等静态策略禁止的模块。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import hashlib
import importlib.abc
import importlib.machinery
import io
import json
import lzma
import os
import re
import runpy
import shutil
import sys
import tarfile
import traceback
import types
from pathlib import Path

SCHEME_ID = "__SCHEME_ID__"
FREQUENCY = "__FREQUENCY__"
PACKAGE_SHA256 = "__PACKAGE_SHA256__"
SOURCE_PACKAGE_IDENTITY = "__SOURCE_PACKAGE_IDENTITY__"
PAYLOAD_B64 = """__PAYLOAD_B64__"""

NULL_TOKEN = "\\N"
MANIFEST_NAME = "raw_wind_manifest.json"
DIRECTION_FIELDS = ("pred_label", "candidate_pred", "vote_pred", "model_pred")
RESULT_FIELDS = ("request_id", "predict_date", "feature_date", "target_date", "predicted_direction")
REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_WORKERS = 6


class ShellError(RuntimeError):
    """外壳或封装算法无法为某个请求给出可信结果；以非 0 退出，不写部分结果。"""


# ---------------------------------------------------------------- 内嵌二进制包

def unpack_package(destination: Path) -> Path:
    """校验并解压内嵌二进制包，返回 `forecast_project` 根目录。"""
    compressed = base64.b64decode("".join(PAYLOAD_B64.split()))
    archive = lzma.decompress(compressed)
    if hashlib.sha256(archive).hexdigest() != PACKAGE_SHA256:
        raise ShellError("embedded package does not match its recorded sha256")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        handle.extractall(destination, filter="data")
    root = destination / "forecast_project"
    if not (root / "daily_project" / "src").is_dir():
        raise ShellError("embedded package layout is invalid")
    return root


# ---------------------------------------------------------------- 原始源表

def load_raw_tables(data_dir: Path) -> dict:
    """读取平台导出的原始源表；逐文件校验 sha256/字节数，并还原为与 pandas.read_sql 同 dtype 的 DataFrame。"""
    import pandas as pd

    manifest = json.loads((data_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "raw-wind-v1":
        raise ShellError("unsupported raw wind manifest")
    tables = {}
    for name, info in manifest["tables"].items():
        path = data_dir / info["file"]
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
                size += len(block)
        if digest.hexdigest() != info["sha256"] or size != info["bytes"]:
            raise ShellError(f"raw wind file does not match its manifest: {info['file']}")
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
        if list(raw.columns) != info["columns"] or len(raw) != info["rows"]:
            raise ShellError(f"raw wind table shape does not match its manifest: {name}")
        columns = {}
        for column in info["columns"]:
            kind = info["kinds"][column]
            cells = raw[column]
            is_null = cells.eq(NULL_TOKEN)
            if kind == "float":
                columns[column] = cells.mask(is_null, "nan").to_numpy().astype("float64")
            elif kind == "int":
                values = [None if cell == NULL_TOKEN else int(cell) for cell in cells.tolist()]
                columns[column] = (
                    pd.Series(values, dtype="float64") if any(v is None for v in values) else pd.Series(values, dtype="int64")
                )
            elif kind == "datetime":
                columns[column] = pd.to_datetime(pd.Series([None if cell == NULL_TOKEN else cell for cell in cells.tolist()]))
            else:
                columns[column] = pd.Series([_decode_text(cell) for cell in cells.tolist()], dtype=object)
        tables[name] = pd.DataFrame(columns, columns=info["columns"])
    return tables


def _decode_text(cell: str):
    if cell == NULL_TOKEN:
        return None
    return cell[1:] if cell.startswith("\\\\") else cell


# ---------------------------------------------------------------- 无数据库层

_SELECT = re.compile(
    r"SELECT\s+(.+?)\s+FROM\s+(\w+)(?:\s+WHERE\s+(.+?))?(?:\s+ORDER\s+BY\s+(\w+(?:\s*,\s*\w+)*))?\s*$", re.I | re.S
)


class _Answerer:
    """只回答二进制实际使用的查询形态（选列、按指标代码/日期/非空过滤、排序）；其余一律报错。"""

    def __init__(self, tables: dict) -> None:
        self.tables = tables

    def __call__(self, sql, params=None):
        text = re.sub(r"\s+", " ", self._bind(sql, params)).strip()
        match = _SELECT.match(text)
        if not match or match.group(2) not in self.tables:
            raise ShellError(f"unsupported query (database access is disabled): {text[:100]}")
        columns, table, where, order = match.groups()
        frame = self.tables[table]
        if where:
            frame = self._filter(frame, re.sub(r"(\w+)\s+between\s+'([^']*)'\s+and\s+'([^']*)'", r"\1 >= '\2' AND \1 <= '\3'", where, flags=re.I))
        if order:
            frame = frame.sort_values([item.strip() for item in order.split(",")], kind="stable")
        return (frame if columns.strip() == "*" else frame[[item.strip() for item in columns.split(",")]]).copy()

    @staticmethod
    def _bind(sql, params) -> str:
        text = str(sql)
        if params is None:
            return text
        values = list(params)
        parts = text.split("%s")
        if len(parts) - 1 != len(values):
            raise ShellError("query placeholder and parameter counts differ")
        return parts[0] + "".join("'" + str(value) + "'" + tail for value, tail in zip(values, parts[1:]))

    @staticmethod
    def _filter(frame, where: str):
        rest = where
        in_match = re.match(r"indicators_code\s+IN\s*\(([^)]*)\)\s*(.*)$", rest, re.I)
        if in_match:
            frame = frame[frame.indicators_code.isin(set(re.findall(r"'([^']+)'", in_match.group(1))))]
            rest = in_match.group(2)
        for condition in [item for item in re.split(r"\s*AND\s+", rest, flags=re.I) if item]:
            not_null = re.fullmatch(r"(\w+)\s+IS\s+NOT\s+NULL", condition, re.I)
            compare = re.fullmatch(r"(\w+)\s*(<=|>=|=|<|>)\s*'?([^']*?)'?", condition, re.I)
            if not_null:
                frame = frame[frame[not_null.group(1)].notna()]
            elif compare:
                column, operator, value = compare.groups()
                series = frame[column].astype(str)
                frame = frame[{"<=": series <= value, ">=": series >= value, "=": series == value, "<": series < value, ">": series > value}[operator]]
            else:
                raise ShellError(f"unsupported WHERE term: {condition[:100]}")
        return frame


class _DisabledEngine:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self):
        raise ShellError("database access is disabled")

    begin = connect

    def dispose(self):
        return None


class _DisabledModule(types.ModuleType):
    """严格白名单假模块：只放行二进制已知会取用的名字，其余明确报错。"""

    ALLOWED = {"sqlalchemy": {"create_engine"}, "sqlalchemy.engine": {"URL"}}

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name not in self.ALLOWED.get(self.__name__, set()):
            raise AttributeError(f"database access is disabled: {self.__name__}.{name}")
        if name == "create_engine":
            return lambda *args, **kwargs: _DisabledEngine()
        return types.SimpleNamespace(create=lambda *args, **kwargs: None)


class _DisabledFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    ROOTS = ("sqlalchemy", "pymysql")

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.ROOTS:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        module = _DisabledModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        return None


def install_database_shim(tables: dict) -> None:
    """使 pandas.read_sql* 只从导出表作答，并让 sqlalchemy/pymysql 成为严格白名单假模块。"""
    import pandas as pd

    answer = _Answerer(tables)

    def read(sql, con=None, *args, **kwargs):
        return answer(sql, kwargs.get("params"))

    for name in ("read_sql", "read_sql_query"):
        setattr(pd, name, read)
        setattr(pd.io.sql, name, read)
    for name in [m for m in sys.modules if m.split(".")[0] in _DisabledFinder.ROOTS]:
        del sys.modules[name]
    if not any(isinstance(finder, _DisabledFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _DisabledFinder())


# ---------------------------------------------------------------- 运行二进制

class Runner:
    """一个进程内的二进制运行器：解压一次、装载数据一次，之后逐请求运行。"""

    def __init__(self, work_dir: Path, data_dir: Path) -> None:
        self.root = unpack_package(work_dir)
        self.tables = load_raw_tables(data_dir)
        self.trade_days = set(
            self.tables["t_trade_calendar"].loc[self.tables["t_trade_calendar"]["trade_flag"].astype(str).str.strip() == "1", "rdate"]
        )
        install_database_shim(self.tables)
        for entry in (self.root / "daily_project" / "src", self.root):
            if str(entry) not in sys.path:
                sys.path.insert(0, str(entry))
        os.environ["DRY_RUN"] = "1"
        self._baseline = set(sys.modules)

    def run(self, request: dict) -> int:
        """以 target_date 为运行日运行二进制，返回 -1/0/1；之后清除产物与二进制模块状态。"""
        run_date = request["target_date"]
        if run_date not in self.trade_days:
            raise ShellError(f"target_date is not a trading day: {run_date}")
        output_root = self.root / "daily_project" / "output"
        shutil.rmtree(output_root, ignore_errors=True)
        argv = ["daily.run_daily", run_date, "--project-root", str(self.root), "--n-jobs", "1", "--dry-run", "--frequencies", FREQUENCY]
        saved = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    runpy.run_module("daily.run_daily", run_name="__main__")
                except SystemExit as exit_request:
                    if exit_request.code not in (0, None):
                        raise ShellError(f"daily runner exited with {exit_request.code}") from None
            return select_direction(output_root / run_date / "prediction" / "daily_selected_prediction_rows.csv", request)
        finally:
            sys.argv = saved
            shutil.rmtree(output_root, ignore_errors=True)
            self._forget_package_modules()

    def _forget_package_modules(self) -> None:
        """二进制可能有模块级缓存；每个请求之后清除其模块，保证结果与请求顺序无关。"""
        prefix = str(self.root)
        for name in [m for m in sys.modules if m not in self._baseline]:
            origin = getattr(sys.modules[name], "__file__", None) or ""
            if origin.startswith(prefix):
                del sys.modules[name]


def select_direction(rows_path: Path, request: dict) -> int:
    """读取选中行并按原生适配器同一规则映射方向；日期上下文必须与请求一致。"""
    if not rows_path.is_file():
        raise ShellError("daily runner produced no selected prediction rows")
    with rows_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("frequency") == FREQUENCY]
    if len(rows) != 1:
        raise ShellError(f"expected exactly one {FREQUENCY} row, got {len(rows)}")
    row = rows[0]
    source_feature = (row.get("prediction_date") or row.get("expected_prediction_date") or "").strip()
    if source_feature != request["feature_date"]:
        raise ShellError(f"source prediction_date {source_feature!r} differs from feature_date {request['feature_date']!r}")
    if (row.get("rdate") or "").strip() != request["target_date"]:
        raise ShellError("source run date differs from target_date")
    for field in DIRECTION_FIELDS:
        value = row.get(field)
        if value is None or not str(value).strip():
            continue
        try:
            direction = int(float(str(value).strip()))
        except ValueError as exc:
            raise ShellError(f"direction field {field} is not numeric") from exc
        if direction not in (-1, 0, 1):
            raise ShellError(f"direction field {field} is outside -1/0/1")
        return direction
    raise ShellError("source row has no final prediction direction")


# ---------------------------------------------------------------- 请求与输出

def validate_request(raw: dict) -> dict:
    if set(raw) != set(REQUEST_FIELDS):
        raise ShellError("request fields mismatch")
    request = {key: str(raw[key]).strip() for key in REQUEST_FIELDS}
    if not request["request_id"]:
        raise ShellError("request_id is empty")
    for key in ("predict_date", "feature_date", "target_date"):
        if not _DATE.fullmatch(request[key]):
            raise ShellError(f"{key} must be YYYY-MM-DD")
    if not (request["feature_date"] <= request["predict_date"] <= request["target_date"] and request["feature_date"] < request["target_date"]):
        raise ShellError("request dates violate feature <= predict <= target")
    return request


def write_atomic(path: Path, payload: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="")
    os.replace(temporary, path)


def _worker_init(data_dir: str, work_root: str) -> None:
    global _WORKER
    _WORKER = Runner(Path(work_root) / f"worker-{os.getpid()}", Path(data_dir))


def _worker_run(task):
    index, request = task
    return index, _WORKER.run(request)


_WORKER = None


def predict(arguments, work_dir: Path) -> None:
    raw = json.loads(Path(arguments.request).read_text(encoding="utf-8"))
    request = validate_request(raw)
    runner = Runner(work_dir / "w4", Path(arguments.data_dir))
    direction = runner.run(request)
    result = {**{key: request[key] for key in RESULT_FIELDS[:-1]}, "predicted_direction": direction}
    write_atomic(Path(arguments.output), json.dumps(result, ensure_ascii=True))


def backtest(arguments, work_dir: Path) -> None:
    import concurrent.futures
    import multiprocessing

    with Path(arguments.requests).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        requests = [validate_request(row) for row in reader]
    if not requests:
        raise ShellError("backtest requires at least one request")
    ids = [request["request_id"] for request in requests]
    if len(set(ids)) != len(ids):
        raise ShellError("request_id values must be unique")
    workers = max(1, min(_MAX_WORKERS, os.cpu_count() or 1, len(requests)))
    results = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init,
        initargs=(arguments.data_dir, str(work_dir / "w4")),
    ) as pool:
        for index, direction in pool.map(_worker_run, list(enumerate(requests)), chunksize=1):
            results[index] = direction
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(RESULT_FIELDS)
    for index, request in enumerate(requests):
        writer.writerow([request[key] for key in RESULT_FIELDS[:-1]] + [results[index]])
    write_atomic(Path(arguments.output), buffer.getvalue())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"Blackbox V2 wrapped binary delivery: {SCHEME_ID}")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("predict")
    one.add_argument("--request", required=True)
    one.add_argument("--data-dir", required=True)
    one.add_argument("--output", required=True)
    many = sub.add_parser("backtest")
    many.add_argument("--requests", required=True)
    many.add_argument("--data-dir", required=True)
    many.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    work_dir = Path(arguments.output).resolve().parent
    try:
        (predict if arguments.command == "predict" else backtest)(arguments, work_dir)
    except BaseException as exc:  # noqa: BLE001 — 任何失败都以非 0 退出且不写部分结果
        Path(arguments.output).unlink(missing_ok=True)
        print(f"{type(exc).__name__}: {exc}"[:2000], file=sys.stderr)
        if not isinstance(exc, ShellError):
            traceback.print_exc(limit=3, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work_dir / "w4", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
