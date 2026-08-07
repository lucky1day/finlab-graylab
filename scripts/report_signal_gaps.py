#!/usr/bin/env python
"""在单一只读一致性快照中输出 active 方案的 live 信号缺口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env  # noqa: E402
from shared.signal_gap_report import (  # noqa: E402
    SignalGapReportError,
    load_signal_gap_report,
    serialize_signal_gap_report,
)


DEFAULT_HISTORY_START_DATE = "2025-01-01"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析显式报告日期范围。"""

    parser = argparse.ArgumentParser(
        description=(
            "只读枚举 active 方案的应有、已有和缺失 live 信号；"
            "上线当天不计入应有集合。"
        )
    )
    parser.add_argument(
        "--as-of",
        required=True,
        help="报告截止 predict_date，格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--start-date",
        default=DEFAULT_HISTORY_START_DATE,
        help=(
            "报告起始 predict_date，默认 2025-01-01；"
            "各方案仍从自身 onboarding 日之后开始计入"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """运行只读报告；发现缺口本身不视为 CLI 执行失败。"""

    args = parse_args(argv)
    engine = create_engine_from_env()
    try:
        report = load_signal_gap_report(
            engine,
            start_date=args.start_date,
            end_date=args.as_of,
        )
    except SignalGapReportError as exc:
        print(
            json.dumps(
                {"status": "error", "code": exc.code},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"status": "error", "code": "report_unavailable"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    finally:
        engine.dispose()
    print(
        json.dumps(
            serialize_signal_gap_report(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
