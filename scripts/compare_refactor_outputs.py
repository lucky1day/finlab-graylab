from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JsonDiff:
    """单个 JSON 字段或文件差异。"""

    kind: str
    path: str
    baseline: Any = None
    current: Any = None


@dataclass(frozen=True)
class CompareReport:
    """重构输出比对结果。"""

    baseline: str
    current: str
    file_count: int
    diff_count: int
    diffs: list[JsonDiff]
    float_tolerance: float
    ignored_paths: list[str]


def compare_paths(
    baseline: str | Path,
    current: str | Path,
    float_tolerance: float = 1e-9,
    ignored_paths: set[str] | None = None,
) -> CompareReport:
    """逐字段比较两个 JSON 文件或两个 JSON 目录。"""
    baseline_path = Path(baseline)
    current_path = Path(current)
    ignored = ignored_paths or set()
    diffs: list[JsonDiff] = []

    if baseline_path.is_file() and current_path.is_file():
        _compare_json_files(baseline_path, current_path, "$", diffs, float_tolerance, ignored)
        file_count = 1
    elif baseline_path.is_dir() and current_path.is_dir():
        baseline_files = _json_files(baseline_path)
        current_files = _json_files(current_path)
        baseline_names = set(baseline_files)
        current_names = set(current_files)
        for relative in sorted(baseline_names - current_names):
            diffs.append(JsonDiff("missing_file", relative))
        for relative in sorted(current_names - baseline_names):
            diffs.append(JsonDiff("extra_file", relative))
        for relative in sorted(baseline_names & current_names):
            _compare_json_files(
                baseline_files[relative],
                current_files[relative],
                "$",
                diffs,
                float_tolerance,
                ignored,
            )
        file_count = len(baseline_names & current_names)
    else:
        raise ValueError(f"baseline/current must both be files or both be directories: {baseline_path} vs {current_path}")

    return CompareReport(
        baseline=str(baseline_path),
        current=str(current_path),
        file_count=file_count,
        diff_count=len(diffs),
        diffs=diffs,
        float_tolerance=float_tolerance,
        ignored_paths=sorted(ignored),
    )


def _json_files(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in sorted(root.rglob("*.json"))}


def _compare_json_files(
    baseline_path: Path,
    current_path: Path,
    path: str,
    diffs: list[JsonDiff],
    float_tolerance: float,
    ignored_paths: set[str],
) -> None:
    baseline_payload = _read_json(baseline_path)
    current_payload = _read_json(current_path)
    _compare_values(baseline_payload, current_payload, path, diffs, float_tolerance, ignored_paths)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _compare_values(
    baseline: Any,
    current: Any,
    path: str,
    diffs: list[JsonDiff],
    float_tolerance: float,
    ignored_paths: set[str],
) -> None:
    if path in ignored_paths:
        return

    if isinstance(baseline, dict) and isinstance(current, dict):
        baseline_keys = set(baseline)
        current_keys = set(current)
        for key in sorted(baseline_keys - current_keys):
            diffs.append(JsonDiff("missing_key", _child_path(path, key), baseline[key], None))
        for key in sorted(current_keys - baseline_keys):
            diffs.append(JsonDiff("extra_key", _child_path(path, key), None, current[key]))
        for key in sorted(baseline_keys & current_keys):
            _compare_values(baseline[key], current[key], _child_path(path, key), diffs, float_tolerance, ignored_paths)
        return

    if isinstance(baseline, list) and isinstance(current, list):
        if len(baseline) != len(current):
            diffs.append(JsonDiff("list_length", path, len(baseline), len(current)))
        for index, (left, right) in enumerate(zip(baseline, current)):
            _compare_values(left, right, f"{path}[{index}]", diffs, float_tolerance, ignored_paths)
        return

    if _both_numbers(baseline, current):
        if not _numbers_equal(float(baseline), float(current), float_tolerance):
            diffs.append(JsonDiff("number", path, baseline, current))
        return

    if baseline != current:
        diffs.append(JsonDiff("value", path, baseline, current))


def _both_numbers(left: Any, right: Any) -> bool:
    return (
        isinstance(left, int | float)
        and isinstance(right, int | float)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    )


def _numbers_equal(left: float, right: float, tolerance: float) -> bool:
    if math.isnan(left) and math.isnan(right):
        return True
    if math.isinf(left) or math.isinf(right):
        return left == right
    return abs(left - right) <= tolerance


def _child_path(parent: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare refactor current outputs against golden baseline JSON.")
    parser.add_argument("baseline", help="Baseline JSON file or directory.")
    parser.add_argument("current", help="Current JSON file or directory.")
    parser.add_argument("--float-tolerance", type=float, default=1e-9)
    parser.add_argument("--max-diffs", type=int, default=50)
    parser.add_argument("--ignore-path", action="append", default=[], help="JSON path to ignore, e.g. $.elapsed_sec")
    args = parser.parse_args(argv)

    report = compare_paths(
        args.baseline,
        args.current,
        float_tolerance=args.float_tolerance,
        ignored_paths=set(args.ignore_path),
    )
    payload = asdict(report)
    payload["diffs"] = payload["diffs"][: args.max_diffs]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return report.diff_count


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)
