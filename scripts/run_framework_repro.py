from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

from scripts.postonboard_common import (
    PROJECT_ROOT,
    default_output_dir,
    exit_code,
    finish,
    load_scheme_config,
    make_payload,
    parse_json_document,
    write_json,
)


OUTPUT_FILE = "repro_framework.json"


def run_framework_repro(
    scheme_id: str,
    predict_date: str | None = None,
    output_dir: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
    algo_env: str = "forecast_env",
) -> tuple[dict[str, Any], int]:
    """调用框架历史复现 runner，并抽取 framework_db_aligned 结果。"""
    try:
        config = load_scheme_config(scheme_id, project_root)
    except Exception as exc:
        payload = make_payload("fail", {"scheme_id": scheme_id}, [str(exc)])
        return payload, exit_code(payload["status"])

    runner_module = _runner_module(config, scheme_id)
    runner_path = project_root / Path(*runner_module.split(".")).with_suffix(".py")
    if not runner_path.exists():
        evidence = {"scheme_id": scheme_id, "runner_module": runner_module}
        payload = make_payload("fail", evidence, [f"missing backtest runner: {runner_path}"])
        return payload, exit_code(payload["status"])

    try:
        runner_payload = _run_backtest_runner(
            runner_module=runner_module,
            scheme_id=scheme_id,
            algo_env=algo_env,
            project_root=project_root,
        )
    except Exception as exc:
        evidence = {"scheme_id": scheme_id, "runner_module": runner_module}
        payload = make_payload("fail", evidence, [str(exc)])
        return payload, exit_code(payload["status"])

    selected = _select_framework_run(runner_payload, scheme_id)
    if selected is None:
        evidence = {"scheme_id": scheme_id, "runner_module": runner_module}
        payload = make_payload("fail", evidence, ["runner output did not include framework_db_aligned run"])
        return payload, exit_code(payload["status"])

    sample_count = _sample_count(selected)
    summary = selected.get("summary") if isinstance(selected.get("summary"), dict) else {}
    resolved_output_dir = Path(output_dir) if output_dir else default_output_dir(scheme_id, project_root)
    output_path = write_json(resolved_output_dir / OUTPUT_FILE, selected)
    evidence = {
        "scheme_id": scheme_id,
        "runner_module": runner_module,
        "sample_count": sample_count,
        "date_range": _date_range_from_run(selected),
        "input_artifact_source": _input_artifact_source(config),
        "data_version": _data_version(config),
        "output_path": str(output_path),
        "warnings": _warnings(config, predict_date, summary),
    }
    status = "pass" if sample_count > 0 else "fail"
    errors = [] if sample_count > 0 else ["framework reproduction produced zero rows"]
    payload = make_payload(status, evidence, errors)
    return payload, exit_code(payload["status"])


def _runner_module(config: dict[str, Any], scheme_id: str) -> str:
    backtest = config.get("backtest") if isinstance(config.get("backtest"), dict) else {}
    return str(backtest.get("runner") or f"backtests.{scheme_id}_reproduction")


def _run_backtest_runner(
    runner_module: str,
    scheme_id: str,
    algo_env: str,
    project_root: Path,
) -> dict[str, Any]:
    command = [
        "conda",
        "run",
        "-n",
        algo_env,
        "python",
        "-m",
        runner_module,
        "--no-persist",
        *_runner_scheme_args(runner_module, scheme_id),
    ]
    completed = subprocess.run(command, cwd=project_root, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "backtest runner failed").strip()
        raise RuntimeError(detail)
    parsed = parse_json_document(completed.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("backtest runner stdout JSON must be an object")
    return parsed


def _runner_scheme_args(runner_module: str, scheme_id: str) -> list[str]:
    if runner_module == "backtests.daily_0529_reproduction":
        if scheme_id == "t1_daily":
            return ["--skip-t5"]
        if scheme_id == "t5_daily":
            return ["--skip-t1"]
    return []


def _select_framework_run(payload: dict[str, Any], scheme_id: str) -> dict[str, Any] | None:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return None
    candidates = [run for run in runs if isinstance(run, dict) and run.get("scheme_id") == scheme_id]
    for run in candidates:
        if run.get("data_source") == "framework_db_aligned":
            return run
    return candidates[0] if candidates else None


def _sample_count(run: dict[str, Any]) -> int:
    rows = run.get("rows")
    if isinstance(rows, list):
        return len(rows)
    if isinstance(rows, int):
        return rows
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    for key in ("row_count", "raw_row_count", "sample_count"):
        value = summary.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def _date_range_from_run(run: dict[str, Any]) -> dict[str, str | None]:
    direct = [run.get("start_date"), run.get("end_date")]
    direct_dates = [str(value)[:10] for value in direct if value]
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    by_tenor = summary.get("by_tenor") if isinstance(summary.get("by_tenor"), dict) else {}
    date_values: list[str] = []
    for tenor_summary in by_tenor.values():
        if not isinstance(tenor_summary, dict):
            continue
        for key in ("date_min", "date_max", "start_date", "end_date"):
            value = tenor_summary.get(key)
            if value:
                date_values.append(str(value)[:10])
    dates = sorted(direct_dates + date_values)
    return {"min": dates[0] if dates else None, "max": dates[-1] if dates else None}


def _input_artifact_source(config: dict[str, Any]) -> str:
    frequency = str(config.get("frequency") or "daily").strip().lower()
    return f"shared_data_service_{frequency}"


def _data_version(config: dict[str, Any]) -> str | None:
    input_spec = config.get("input_spec") if isinstance(config.get("input_spec"), dict) else {}
    value = input_spec.get("data_version")
    return str(value) if value is not None else None


def _warnings(config: dict[str, Any], predict_date: str | None, summary: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if predict_date:
        warnings.append("predict_date is accepted for SOP consistency; runner controls its own date range")
    reported_version = summary.get("data_version") or summary.get("input_data_version")
    expected_version = _data_version(config)
    if reported_version and expected_version and str(reported_version) != expected_version:
        warnings.append(f"runner data_version {reported_version} differs from config {expected_version}")
    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run framework reproduction through the configured backtest runner.")
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--predict-date")
    parser.add_argument("--output-dir")
    parser.add_argument("--algo-env", default="forecast_env")
    args = parser.parse_args(argv)

    payload, _ = run_framework_repro(
        args.scheme_id,
        predict_date=args.predict_date,
        output_dir=args.output_dir,
        algo_env=args.algo_env,
    )
    return finish(payload)


if __name__ == "__main__":
    raise SystemExit(main())
