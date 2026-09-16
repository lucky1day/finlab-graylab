#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import (
    apply_replaced_prediction_history_cleanup,
    create_engine_from_env,
    plan_replaced_prediction_history_cleanup,
)


MAX_PLAN_BYTES = 16 * 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or apply one exact replaced-history cleanup."
    )
    parser.add_argument("--scheme-id", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan-out", type=Path)
    action.add_argument("--apply-plan", type=Path)
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args()
    engine = create_engine_from_env()
    try:
        if args.plan_out is not None:
            if args.expected_plan_sha256 is not None:
                parser.error("--expected-plan-sha256 is only valid with --apply-plan")
            plan = plan_replaced_prediction_history_cleanup(
                engine,
                prediction_scheme_id=args.scheme_id,
            )
            raw = _canonical_json(plan)
            _write_exclusive(args.plan_out, raw)
            print(
                json.dumps(
                    {
                        "status": "planned",
                        "plan_sha256": hashlib.sha256(raw).hexdigest(),
                        "scope_digest": plan["scope_digest"],
                        "product_delete_count": len(
                            plan["scope"]["product_ids"]
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.expected_plan_sha256 is None:
            parser.error("--apply-plan requires --expected-plan-sha256")
        raw = _read_bounded(args.apply_plan)
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != args.expected_plan_sha256:
            raise ValueError("cleanup plan sha256 does not match")
        plan: dict[str, Any] = json.loads(raw)
        if plan.get("scope", {}).get("prediction_scheme_id") != args.scheme_id:
            raise ValueError("cleanup plan scheme does not match")
        stats = apply_replaced_prediction_history_cleanup(engine, plan)
        print(
            json.dumps(
                {
                    "status": "applied",
                    "plan_sha256": actual_sha256,
                    "scope_digest": plan["scope_digest"],
                    "predictions_deleted": stats.predictions_deleted,
                    "live_runs_deleted": stats.live_runs_deleted,
                    "backtest_runs_deleted": stats.backtest_runs_deleted,
                    "versions_deleted": stats.versions_deleted,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        engine.dispose()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _read_bounded(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or details.st_size <= 0
            or details.st_size > MAX_PLAN_BYTES
        ):
            raise ValueError("cleanup plan size is invalid")
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_PLAN_BYTES + 1)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("cleanup plan is too large")
    return raw


if __name__ == "__main__":
    raise SystemExit(main())
