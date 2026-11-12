# 附件：W4 daily_0629 二进制无数据库原型（2026-11-13）。仅用于评估，不是生产代码。
# 用法见 docs/plans/FRAMEWORK_AUDIT_AND_OPTIMIZATION_PLAN.md §13.2：W4_RAW 指向 9 张源表的 pickle 导出，
# 以 forecast_env 的 python 在源码包副本的 daily_project 目录运行：proto_run.py <日期> <源码包根目录>。
"""原型：在没有数据库的情况下进程内运行 W4 daily_0629 二进制，用原始数据回答它的四类查询（未知查询一律报错）。"""
import os, re, sys, runpy, types
import pandas as pd
RAW = os.environ["W4_RAW"]; LOG = []
daily = pd.read_pickle(f"{RAW}/api_wind_daily.pkl"); deriv = pd.read_pickle(f"{RAW}/api_wind_derivative_daily.pkl")
calendar = pd.read_pickle(f"{RAW}/t_trade_calendar.pkl"); catalog = pd.read_pickle(f"{RAW}/api_wind_indicators_all.pkl")
TABLES = {name: pd.read_pickle(f"{RAW}/{name}.pkl") for name in (
    "api_wind_daily", "api_wind_derivative_daily", "api_wind_weekly", "api_wind_derivative_weekly",
    "api_wind_monthly", "api_wind_derivative_monthly", "api_wind_date", "t_trade_calendar", "api_wind_indicators_all")}
_SELECT = re.compile(r"SELECT\s+(.+?)\s+FROM\s+(\w+)(?:\s+WHERE\s+(.+?))?(?:\s+ORDER\s+BY\s+(\w+(?:\s*,\s*\w+)*))?\s*$", re.I | re.S)
def answer(sql):
    text = re.sub(r"\s+", " ", str(sql)).strip()
    LOG.append(text[:140])
    m = _SELECT.match(text)
    if not m or m.group(2) not in TABLES: raise RuntimeError(f"W4 prototype: unexpected SQL: {text[:80]} ... {text[-120:]}")
    cols, table, where, order = m.groups(); df = TABLES[table]
    if where:
        rest = where
        in_m = re.match(r"indicators_code\s+IN\s*\(([^)]*)\)\s*(.*)$", rest, re.I)
        if in_m:
            df = df[df.indicators_code.isin(set(re.findall(r"'([^']+)'", in_m.group(1))))]; rest = in_m.group(2)
        for cond in [c for c in re.split(r"\s*AND\s+", rest, flags=re.I) if c]:
            if (nn := re.fullmatch(r"(\w+)\s+IS\s+NOT\s+NULL", cond, re.I)): df = df[df[nn.group(1)].notna()]
            elif (d := re.fullmatch(r"rdate\s*<=\s*'([^']+)'", cond, re.I)): df = df[df.rdate <= d.group(1)]
            else: raise RuntimeError(f"W4 prototype: unsupported WHERE term: {cond[:80]}")
    if order: df = df.sort_values([c.strip() for c in order.split(",")], kind="stable")
    return (df if cols.strip() == "*" else df[[c.strip() for c in cols.split(",")]]).copy()
class _Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass
    def execute(self, *a, **k): raise RuntimeError("W4 prototype: direct execute is not supported")
class _Engine(_Conn):
    def connect(self): return _Conn()
    def dispose(self): pass
    def begin(self): return _Conn()
def _bind(sql, params):
    text = str(sql)
    if params is None: return text
    values = list(params)
    if text.count("%s") != len(values): raise RuntimeError("W4 prototype: placeholder/param count mismatch")
    parts = text.split("%s"); out = parts[0]
    for value, tail in zip(values, parts[1:]): out += "'" + str(value) + "'" + tail
    return out
def _read(sql, con=None, *a, **k): return answer(_bind(sql, k.get("params")))
for name in ("read_sql", "read_sql_query"):
    setattr(pd, name, _read); setattr(pd.io.sql, name, _read)
import sqlalchemy
sqlalchemy.create_engine = lambda *a, **k: _Engine()
fake = types.ModuleType("pymysql")
def _deny(*a, **k): raise RuntimeError("W4 prototype: pymysql.connect is not available")
fake.connect = _deny; fake.cursors = types.SimpleNamespace(); fake.install_as_MySQLdb = lambda: None
sys.modules["pymysql"] = fake
sys.argv = ["daily.run_daily", sys.argv[1], "--project-root", sys.argv[2], "--n-jobs", "1", "--dry-run"]
try:
    runpy.run_module("daily.run_daily", run_name="__main__")
finally:
    print("SQL answered:", len(LOG), "distinct:", len(set(LOG)), file=sys.stderr)
