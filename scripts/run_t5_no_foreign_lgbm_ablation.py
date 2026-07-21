#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from experiments.t5_no_foreign_lgbm.runner import ExperimentRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated daily T+5 no-foreign-factor LGBM comparison."
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--source-data-root", required=True, type=Path)
    parser.add_argument("--source-cache-root", required=True, type=Path)
    parser.add_argument("--calendar-path", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--report-path", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    runner = ExperimentRunner(
        project_root=arguments.project_root,
        source_data_root=arguments.source_data_root,
        source_cache_root=arguments.source_cache_root,
        calendar_path=arguments.calendar_path,
        output_root=arguments.output_root,
        report_path=arguments.report_path,
        workers=arguments.workers,
        resume=arguments.resume,
    )
    if arguments.validate_only:
        bundle = runner.validate_only()
    else:
        print("[experiment] starting isolated read-only comparison", file=sys.stderr)
        bundle = runner.run()
    statuses: dict[str, int] = {}
    for scheme in bundle["schemes"]:
        status = str(scheme["status"])
        statuses[status] = statuses.get(status, 0) + 1
    print(
        json.dumps(
            {
                "run_state": str(runner.output_root / "run_state.json"),
                "report": str(runner.report_path),
                "statuses": statuses,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
