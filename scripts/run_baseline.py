from __future__ import annotations

import argparse
import csv
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.postonboard_common import (
    PROJECT_ROOT,
    clean_json,
    default_output_dir,
    exit_code,
    finish,
    load_scheme_config,
    make_payload,
    parse_json_document,
    write_json,
)


DEFAULT_BENCHMARK_ID = "model_muti_0529"
OUTPUT_FILE = "baseline_original.json"


def run_baseline(
    scheme_id: str,
    predict_date: str | None = None,
    output_dir: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
    algo_env: str = "forecast_env",
) -> tuple[dict[str, Any], int]:
    """运行或读取原始基准，输出 AI 可消费的 baseline_original.json。"""
    errors: list[str] = []
    try:
        config = load_scheme_config(scheme_id, project_root)
    except Exception as exc:
        payload = make_payload("blocked", {"scheme_id": scheme_id}, [str(exc)])
        return payload, exit_code(payload["status"])

    resolved_output_dir = Path(output_dir) if output_dir else default_output_dir(scheme_id, project_root)
    benchmark_id = _benchmark_id(config)
    legacy_scripts = sorted((project_root / "schemes" / scheme_id / "core").glob("legacy_*.py"))
    if legacy_scripts:
        payload = _run_legacy_script(
            scheme_id=scheme_id,
            legacy_script=legacy_scripts[0],
            predict_date=predict_date,
            output_dir=resolved_output_dir,
            algo_env=algo_env,
            project_root=project_root,
        )
        return payload, exit_code(payload["status"])

    try:
        rows, source_path = _read_static_benchmark(
            benchmark_id=benchmark_id,
            predict_date=predict_date,
            project_root=project_root,
        )
    except Exception as exc:
        errors.append(str(exc))
        evidence = {
            "scheme_id": scheme_id,
            "benchmark_id": benchmark_id,
            "morphology": "static_benchmark_csv",
        }
        payload = make_payload("blocked", evidence, errors)
        return payload, exit_code(payload["status"])

    output_path = write_json(resolved_output_dir / OUTPUT_FILE, rows)
    dates = [row["predict_date"] for row in rows if row.get("predict_date")]
    evidence = {
        "scheme_id": scheme_id,
        "benchmark_id": benchmark_id,
        "morphology": "static_benchmark_csv",
        "sample_count": len(rows),
        "date_range": _date_range(dates),
        "source_path": str(source_path),
        "output_path": str(output_path),
    }
    status = "pass" if rows else "blocked"
    if not rows:
        errors.append("static benchmark CSV produced zero rows")
    payload = make_payload(status, evidence, errors)
    return payload, exit_code(payload["status"])


def _benchmark_id(config: dict[str, Any]) -> str:
    value = config.get("benchmark_id")
    if value:
        return str(value)
    backtest = config.get("backtest") if isinstance(config.get("backtest"), dict) else {}
    return str(backtest.get("benchmark_id") or DEFAULT_BENCHMARK_ID)


def _read_static_benchmark(
    benchmark_id: str,
    predict_date: str | None,
    project_root: Path,
) -> tuple[list[dict[str, Any]], Path]:
    benchmark_dir = project_root / "source_evidence" / "benchmark_batches" / benchmark_id
    if not benchmark_dir.exists():
        raise FileNotFoundError(f"missing source evidence directory: {benchmark_dir}")

    source_path = _select_benchmark_csv(benchmark_dir)
    rows: list[dict[str, Any]] = []
    with source_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"benchmark CSV has no header: {source_path}")
        fieldnames = [str(name).strip() for name in reader.fieldnames]
        for index, raw in enumerate(reader):
            normalized = _normalize_static_row(raw, index=index, source_columns=fieldnames)
            if predict_date and normalized.get("predict_date") != predict_date:
                continue
            rows.append(normalized)
    return rows, source_path


def _select_benchmark_csv(benchmark_dir: Path) -> Path:
    preferred = benchmark_dir / "daily_output.csv"
    if preferred.exists():
        return preferred
    csv_files = sorted(benchmark_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"no source evidence CSV found in {benchmark_dir}")
    return csv_files[0]


def _normalize_static_row(raw: dict[str, Any], index: int, source_columns: list[str]) -> dict[str, Any]:
    predict_date = _first_present(raw, ("predict_date", "date", "rdate", "feature_date"))
    record = {
        "predict_date": _date_string_or_none(predict_date),
        "target_tenor": _string_or_none(_first_present(raw, ("target_tenor", "tenor", "bond_tenor"))),
        "predicted_direction": _int_or_none(_first_present(raw, ("predicted_direction", "prediction", "label_pred"))),
        "confidence": _float_or_none(_first_present(raw, ("confidence", "probability", "score"))),
        "source_row_index": index,
        "extra": {
            "source_columns_count": len(source_columns),
            "source_columns_sample": source_columns[:20],
        },
    }
    return record


def _run_legacy_script(
    scheme_id: str,
    legacy_script: Path,
    predict_date: str | None,
    output_dir: Path,
    algo_env: str,
    project_root: Path,
) -> dict[str, Any]:
    command = ["conda", "run", "-n", algo_env, "python", str(legacy_script)]
    if predict_date:
        command.extend(["--predict-date", predict_date])
    completed = subprocess.run(command, cwd=project_root, capture_output=True, text=True, check=False)
    evidence: dict[str, Any] = {
        "scheme_id": scheme_id,
        "morphology": "legacy_script",
        "legacy_script": str(legacy_script),
        "returncode": completed.returncode,
    }
    if completed.returncode != 0:
        return make_payload("blocked", evidence, [_stderr_or_stdout(completed)])
    try:
        parsed = parse_json_document(completed.stdout)
        rows = _extract_rows(parsed)
    except Exception as exc:
        return make_payload("blocked", evidence, [str(exc)])
    rows = clean_json(rows)
    output_path = write_json(output_dir / OUTPUT_FILE, rows)
    dates = [row.get("predict_date") for row in rows if isinstance(row, dict) and row.get("predict_date")]
    evidence.update(
        {
            "sample_count": len(rows),
            "date_range": _date_range(dates),
            "output_path": str(output_path),
        }
    )
    status = "pass" if rows else "blocked"
    errors = [] if rows else ["legacy script produced zero rows"]
    return make_payload(status, evidence, errors)


def _extract_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("predictions", "rows", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("legacy script stdout JSON did not contain a row list")


def _date_range(values: list[Any]) -> dict[str, str | None]:
    dates = sorted(_date_string_or_none(value) for value in values if _date_string_or_none(value))
    return {"min": dates[0] if dates else None, "max": dates[-1] if dates else None}


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip()


def _date_string_or_none(value: Any) -> str | None:
    text_value = _string_or_none(value)
    if text_value is None:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text_value[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return text_value[:10]


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _stderr_or_stdout(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stderr or completed.stdout or "legacy script failed").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or read the original baseline for a scheme.")
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--predict-date")
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    payload, _ = run_baseline(
        args.scheme_id,
        predict_date=args.predict_date,
        output_dir=args.output_dir,
    )
    return finish(payload)


if __name__ == "__main__":
    raise SystemExit(main())
