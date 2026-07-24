#!/usr/bin/env python
"""刷新或检查平台统一 DataBridge 三频 current 文件。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.main import _previous_trading_day  # noqa: E402
from shared.data_bridge.client import (  # noqa: E402
    DataBridgeClient,
    DataBridgeClientConfig,
    DataBridgeConfigurationError,
    DataBridgeRequestError,
)
from shared.data_bridge.refresh import (  # noqa: E402
    DataBridgeRefreshConfig,
    DataBridgeRefreshError,
    check_current_dataset,
    run_full_refresh,
)
from shared.data_bridge.validation import DataBridgeValidationError  # noqa: E402
from shared.daily_coordinator_mode import (  # noqa: E402
    DAILY_COORDINATOR_MODE_ENV,
    require_daily_coordinator_mode,
)

Mode = Literal["dry-run", "publish", "check-only"]


def _daily_coordinator_mode() -> str:
    return require_daily_coordinator_mode()


def refresh_current(*, refresh_date: str, publish: bool):
    expected_daily_date = _previous_trading_day(refresh_date)
    client = DataBridgeClient(DataBridgeClientConfig.from_env())
    return run_full_refresh(
        client=client,
        config=DataBridgeRefreshConfig.from_env(),
        expected_daily_date=expected_daily_date,
        refresh_date=refresh_date,
        publish=publish,
    )


def check_current(*, refresh_date: str):
    return check_current_dataset(
        DataBridgeRefreshConfig.from_env(),
        required_refresh_date=refresh_date,
        expected_daily_date=_previous_trading_day(refresh_date),
    )


def run_command(mode: Mode, *, refresh_date: str) -> tuple[int, dict[str, object]]:
    try:
        if (
            mode in {"publish", "dry-run"}
            and _daily_coordinator_mode() == "ledger"
        ):
            return 2, {
                "status": "configuration_error",
                "mode": mode,
                "error": (
                    "standalone DataBridge refresh is disabled in ledger "
                    "mode; use the daily coordinator, and allow only "
                    "--check-only from standalone callers"
                ),
            }
        if mode == "check-only":
            current = check_current(refresh_date=refresh_date)
            return 0, {"status": "ok", "mode": mode, "state": current.state}
        result = refresh_current(refresh_date=refresh_date, publish=mode == "publish")
        return 0, {
            "status": "ok",
            "mode": mode,
            "published": result.published,
            "rounds_completed": result.rounds_completed,
            "duration_sec": round(result.duration_sec, 3),
            "state": result.state,
        }
    except DataBridgeConfigurationError:
        return 2, {
            "status": "configuration_error",
            "mode": mode,
            "error": "DataBridge platform configuration is invalid or incomplete",
        }
    except (DataBridgeRequestError, DataBridgeRefreshError, DataBridgeValidationError) as exc:
        return 1, {"status": "failed", "mode": mode, "error": str(exc)}
    except (OSError, ValueError) as exc:
        return 2, {"status": "configuration_error", "mode": mode, "error": str(exc)}


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--publish", action="store_true")
    modes.add_argument("--check-only", action="store_true")
    parser.add_argument("--date", default=_today(), help="刷新日期，格式 YYYY-MM-DD")
    args = parser.parse_args()
    mode: Mode = "publish" if args.publish else "check-only" if args.check_only else "dry-run"
    exit_code, payload = run_command(mode, refresh_date=args.date)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
