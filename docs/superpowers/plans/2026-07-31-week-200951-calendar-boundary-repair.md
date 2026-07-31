# week_id=200951 Calendar Boundary Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在上游日期生产链中精确补齐 `2009-12-28` 至 `2009-12-31` 的双表权威日历，关闭 `week_id=200951` 历史边界故障，同时保持算法和 benchmark gate 不变。

**Architecture:** `bond-factor-lab` 继续只读 `api_wind_date` 和 `t_trade_calendar`。一次性 operator 从 `/Users/macstudio0/bondprojectpro/BondPrediction/cn_stock_calendar.py` 生成四条期望记录，通过数据库身份围栏、命名锁、冲突检测和单事务只插缺失行；apply 后以平台 `CalendarService` 和只读回测诊断验收。

**Tech Stack:** Python 3.12、PyMySQL、SQLAlchemy、MySQL 8、unittest、Native weekly backtest runner。

---

### Task 1: 冻结 precheck 证据

**Files:**
- Read: `/Users/macstudio0/bondprojectpro/BondPrediction/cn_stock_calendar.py`
- Read: `shared/calendar_service.py`
- Read: `backtests/weekly_10y_d_overlay_0529_reproduction.py`

- [ ] **Step 1: 核对生成器输出**

Run:

```bash
PYTHONPATH=/Users/macstudio0/bondprojectpro/BondPrediction \
  conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
from cn_stock_calendar import build_cn_stock_calendar

rows = build_cn_stock_calendar("2009-12-28", "2009-12-31")
assert [row["rdate"] for row in rows] == [
    "2009-12-28",
    "2009-12-29",
    "2009-12-30",
    "2009-12-31",
]
assert {str(row["week_id"]) for row in rows} == {"200951"}
assert {str(row["trade_flag"]) for row in rows} == {"1"}
for row in rows:
    print(row)
PY
```

Expected: 恰好四行，全部为 `week_id=200951`、`trade_flag=1`。

- [ ] **Step 2: 核对生产现状和数据库身份**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
from sqlalchemy import text
from shared.data_service import create_sqlalchemy_engine

engine = create_sqlalchemy_engine()
with engine.connect() as conn:
    identity = conn.execute(
        text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")
    ).mappings().one()
    assert identity["database_name"] == "bond_db"
    print({"database_name": identity["database_name"], "server_uuid_present": bool(identity["server_uuid"])})
    for table in ("api_wind_date", "t_trade_calendar"):
        rows = conn.execute(
            text(
                f"SELECT * FROM {table} "
                "WHERE rdate BETWEEN '2009-12-28' AND '2010-01-03' "
                "ORDER BY rdate"
            )
        ).mappings().all()
        print(table, [dict(row) for row in rows])
engine.dispose()
PY
```

Expected:

- `api_wind_date` 仅 `2009-12-31` 已存在于四日修复范围；
- `t_trade_calendar` 四日均缺失；
- `2010-01-01` 至 `2010-01-03` 均为非交易日且属于 `200951`；
- 不输出真实 server UUID。

### Task 2: 执行受控双表事务

**Files:**
- No repository file changes
- Runtime source: `/Users/macstudio0/bondprojectpro/BondPrediction/cn_stock_calendar.py`

- [ ] **Step 1: 从只读 identity query 取得本次会话 UUID**

Run:

```bash
export BFL_CALENDAR_EXPECTED_DB="bond_db"
export BFL_CALENDAR_EXPECTED_UUID="$(
  conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
from sqlalchemy import text
from shared.data_service import create_sqlalchemy_engine

engine = create_sqlalchemy_engine()
with engine.connect() as conn:
    value = conn.execute(text("SELECT @@server_uuid")).scalar_one()
print(str(value).strip().lower())
engine.dispose()
PY
)"
test -n "$BFL_CALENDAR_EXPECTED_UUID"
```

Expected: shell 变量非空；不打印 UUID。

- [ ] **Step 2: 在命名锁和单事务中只插入缺失行**

Run:

```bash
PYTHONPATH=/Users/macstudio0/bondprojectpro/BondPrediction \
  conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
import json
import os

import pymysql

from cn_stock_calendar import build_cn_stock_calendar
from config.config import db_config

DATES = ("2009-12-28", "2009-12-29", "2009-12-30", "2009-12-31")
LOCK_NAME = "bfl:calendar-repair:200951"
expected_db = os.environ["BFL_CALENDAR_EXPECTED_DB"].strip()
expected_uuid = os.environ["BFL_CALENDAR_EXPECTED_UUID"].strip().lower()
expected_rows = build_cn_stock_calendar(DATES[0], DATES[-1])

assert tuple(row["rdate"] for row in expected_rows) == DATES
assert all(str(row["week_id"]) == "200951" for row in expected_rows)
assert all(str(row["trade_flag"]) == "1" for row in expected_rows)
expected = {row["rdate"]: row for row in expected_rows}

conn = pymysql.connect(**db_config, autocommit=True)
lock_acquired = False
try:
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")
        identity = cursor.fetchone()
        if identity["database_name"] != expected_db:
            raise RuntimeError("database identity mismatch")
        if str(identity["server_uuid"]).strip().lower() != expected_uuid:
            raise RuntimeError("server UUID mismatch")
        cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (LOCK_NAME,))
        lock_acquired = cursor.fetchone()["acquired"] == 1
        if not lock_acquired:
            raise RuntimeError("calendar repair lock unavailable")

    conn.autocommit(False)
    conn.begin()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        placeholders = ",".join(["%s"] * len(DATES))
        cursor.execute(
            f"SELECT rdate, week_id FROM api_wind_date "
            f"WHERE rdate IN ({placeholders}) ORDER BY rdate FOR UPDATE",
            DATES,
        )
        wind_rows = cursor.fetchall()
        cursor.execute(
            f"SELECT id, rdate, trade_flag, week_id, week_name "
            f"FROM t_trade_calendar WHERE rdate IN ({placeholders}) "
            f"ORDER BY rdate FOR UPDATE",
            DATES,
        )
        trade_rows = cursor.fetchall()

        wind_by_date = {}
        for row in wind_rows:
            day = str(row["rdate"])
            if day in wind_by_date:
                raise RuntimeError(f"duplicate api_wind_date rdate={day}")
            if str(row["week_id"]) != "200951":
                raise RuntimeError(f"api_wind_date conflict rdate={day}")
            wind_by_date[day] = row

        trade_by_date = {}
        for row in trade_rows:
            day = str(row["rdate"])
            if day in trade_by_date:
                raise RuntimeError(f"duplicate t_trade_calendar rdate={day}")
            expected_row = expected[day]
            actual = (
                str(row["id"]),
                day,
                str(row["trade_flag"]),
                str(row["week_id"]),
                str(row["week_name"]),
            )
            wanted = (
                day,
                day,
                "1",
                "200951",
                str(expected_row["week_name"]),
            )
            if actual != wanted:
                raise RuntimeError(f"t_trade_calendar conflict rdate={day}")
            trade_by_date[day] = row

        missing_wind = [day for day in DATES if day not in wind_by_date]
        missing_trade = [day for day in DATES if day not in trade_by_date]
        if len(missing_wind) not in (0, 3) or len(missing_trade) not in (0, 4):
            raise RuntimeError(
                f"unexpected repair cardinality wind={len(missing_wind)} "
                f"trade={len(missing_trade)}"
            )

        if missing_wind:
            cursor.executemany(
                "INSERT INTO api_wind_date (rdate, week_id) VALUES (%s, %s)",
                [(day, "200951") for day in missing_wind],
            )
        if missing_trade:
            cursor.executemany(
                "INSERT INTO t_trade_calendar "
                "(id, rdate, trade_flag, week_id, week_name) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    (
                        day,
                        day,
                        "1",
                        "200951",
                        str(expected[day]["week_name"]),
                    )
                    for day in missing_trade
                ],
            )

        cursor.execute(
            f"SELECT rdate, week_id FROM api_wind_date "
            f"WHERE rdate IN ({placeholders}) ORDER BY rdate",
            DATES,
        )
        wind_after = cursor.fetchall()
        cursor.execute(
            f"SELECT id, rdate, trade_flag, week_id, week_name "
            f"FROM t_trade_calendar WHERE rdate IN ({placeholders}) ORDER BY rdate",
            DATES,
        )
        trade_after = cursor.fetchall()
        if len(wind_after) != 4 or len(trade_after) != 4:
            raise RuntimeError("calendar postcheck row count mismatch")
        if any(str(row["week_id"]) != "200951" for row in wind_after):
            raise RuntimeError("api_wind_date postcheck mismatch")
        for row in trade_after:
            day = str(row["rdate"])
            if (
                str(row["id"]) != day
                or str(row["trade_flag"]) != "1"
                or str(row["week_id"]) != "200951"
                or str(row["week_name"]) != str(expected[day]["week_name"])
            ):
                raise RuntimeError(f"t_trade_calendar postcheck mismatch rdate={day}")

        cursor.execute(
            "SELECT MAX(wd.rdate) AS last_trade "
            "FROM api_wind_date wd "
            "JOIN t_trade_calendar tc "
            "ON tc.rdate = wd.rdate AND tc.trade_flag = '1' "
            "WHERE wd.week_id = '200951'"
        )
        last_trade = str(cursor.fetchone()["last_trade"])
        if last_trade != "2009-12-31":
            raise RuntimeError(f"unexpected last trade {last_trade}")

    conn.commit()
    print(
        json.dumps(
            {
                "status": "applied",
                "week_id": "200951",
                "inserted_api_wind_date": len(missing_wind),
                "inserted_t_trade_calendar": len(missing_trade),
                "last_trading_day": last_trade,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
except Exception:
    conn.rollback()
    raise
finally:
    conn.autocommit(True)
    if lock_acquired:
        with conn.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
    conn.close()
PY
```

Expected on first apply:

```json
{"inserted_api_wind_date": 3, "inserted_t_trade_calendar": 4, "last_trading_day": "2009-12-31", "status": "applied", "week_id": "200951"}
```

- [ ] **Step 3: 清除会话 UUID**

Run:

```bash
unset BFL_CALENDAR_EXPECTED_UUID BFL_CALENDAR_EXPECTED_DB
```

Expected: 两个变量从当前 shell 会话移除。

### Task 3: 并行执行只读验收

**Files:**
- Read: `shared/calendar_service.py`
- Read: `backtests/weekly_10y_d_overlay_0529_reproduction.py`
- Read: `shared/native_input_generation.py`

- [ ] **Step 1: 验收权威日历**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
from shared.calendar_service import get_calendar
from shared.data_service import create_sqlalchemy_engine

engine = create_sqlalchemy_engine()
calendar = get_calendar(engine)
assert calendar.week_id_to_last_trading_day(200951) == "2009-12-31"
assert calendar.week_id_to_last_trading_day(202625) == "2026-07-03"
print({"200951": "2009-12-31", "202625": "2026-07-03"})
engine.dispose()
PY
```

Expected: 两条断言通过。

- [ ] **Step 2: 运行真实 no-persist 诊断**

Run:

```bash
conda run --no-capture-output -n forecast_env \
  python -m backtests.weekly_10y_d_overlay_0529_reproduction --no-persist
```

Expected:

- 不再出现 `no trading day found for week_id=200951`；
- runner 完成 72 行历史结果构造；
- 当前仍在 original benchmark CompareGate 因输入 vintage 漂移 fail-closed；
- 首个已知差异为 `feature_week_id=202533` confidence；
- 不写入 `t_backtest_*`。

- [ ] **Step 3: 核对快照影响**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
from sqlalchemy import text
from shared.data_service import create_sqlalchemy_engine

engine = create_sqlalchemy_engine()
with engine.connect() as conn:
    wind_count = conn.execute(text("SELECT COUNT(*) FROM api_wind_date")).scalar_one()
    trade_count = conn.execute(text("SELECT COUNT(*) FROM t_trade_calendar")).scalar_one()
    print({"api_wind_date_rows": int(wind_count), "t_trade_calendar_rows": int(trade_count)})
engine.dispose()
PY
```

Expected: 分别较 precheck 增加 3 和 4；后续新 Native generation 与
`api-wind-date-v1` 快照摘要会变化，既有冻结 generation 文件不被原地修改。

### Task 4: 更新问题台账与当前状态

**Files:**
- Modify: `docs/records/SCHEME_ISSUE_LEDGER.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/superpowers/specs/2026-07-31-week-200951-calendar-boundary-repair-design.md`

- [ ] **Step 1: 拆分三个事实**

Document:

```text
202625 生产冲突：RESOLVED；live 9、actual-valid 8，合计 81/80。
200951 历史日历边界：RESOLVED；双表补齐 3+4 行，CalendarService=2009-12-31。
当前输入 vintage/benchmark 漂移：OPEN；14 个 feature week/20 个字段差异，
方向翻转 202538、202548、202602；禁止调算法或改 benchmark 贴合。
```

- [ ] **Step 2: 修正过期待办**

Remove:

```text
202625 仍 fail-closed
weekly_10y_d_overlay_0529=77/77
等待补回 7/04、7/11、7/18、7/25
```

Replace with:

```text
生产侧已为 81/80；历史 no-persist 的剩余阻塞仅记录为当前输入
vintage/benchmark 漂移。
```

- [ ] **Step 3: 运行文档回归**

Run:

```bash
python -m unittest tests.test_onboarding_docs
git diff --check
```

Expected: tests pass，`git diff --check` 无输出。

### Task 5: 最终核验与提交

**Files:**
- Review all modified documentation files

- [ ] **Step 1: 核对无算法改动**

Run:

```bash
git status --short
git diff --stat
git diff -- schemes/weekly_10y_d_overlay_0529 backtests/weekly_10y_d_overlay_0529_reproduction.py
```

Expected: 最后一条命令无输出；现有其他文档改动保持原样。

- [ ] **Step 2: 提交本次文档状态更新**

Run:

```bash
git add \
  docs/records/SCHEME_ISSUE_LEDGER.md \
  docs/CURRENT_STATUS.md \
  docs/TODO.md \
  docs/superpowers/specs/2026-07-31-week-200951-calendar-boundary-repair-design.md \
  docs/superpowers/plans/2026-07-31-week-200951-calendar-boundary-repair.md
git diff --cached --check
git commit -m "fix: close week 200951 calendar boundary"
```

Expected: commit 只包含本次计划、设计修订和状态文档；不包含算法、输出产物或其他未跟踪文件。
