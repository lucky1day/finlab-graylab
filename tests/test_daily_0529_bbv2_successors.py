from __future__ import annotations

import ast
import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from shared.blackbox_v2.intake import validate_delivery


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUCCESSORS = {
    "t1_daily_5y_bbv2": ("T+1", 1, "5Y"),
    "t1_daily_10y_bbv2": ("T+1", 1, "10Y"),
    "t5_daily_3y_bbv2": ("T+5", 5, "3Y"),
    "t5_daily_5y_bbv2": ("T+5", 5, "5Y"),
    "t5_daily_7y_bbv2": ("T+5", 5, "7Y"),
    "t5_daily_10y_bbv2": ("T+5", 5, "10Y"),
}
REQUEST = {
    "request_id": "request-1",
    "predict_date": "2026-07-31",
    "feature_date": "2026-07-31",
    "target_date": "2026-08-03",
    "daily_cutoff_key": "2026-07-31",
    "weekly_cutoff_key": "202631",
    "monthly_cutoff_key": "202607",
}
RESULT_FIELDS = {
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
}


def _delivery(scheme_id: str) -> Path:
    return PROJECT_ROOT / "schemes" / scheme_id / "delivery"


def _load_successor(scheme_id: str) -> ModuleType:
    path = _delivery(scheme_id) / f"{scheme_id}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{scheme_id}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    prior = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior
    return module


@pytest.mark.parametrize("scheme_id", SUCCESSORS)
def test_daily_successor_is_strict_self_contained_two_file_package(
    scheme_id: str,
) -> None:
    metadata, script, metadata_path = validate_delivery(_delivery(scheme_id))
    task_type, horizon, tenor = SUCCESSORS[scheme_id]

    assert metadata.scheme_id == scheme_id
    assert metadata.task_type == task_type
    assert metadata.horizon == horizon
    assert metadata.target_tenor == tenor
    assert {script.name, metadata_path.name} == {
        f"{scheme_id}.py",
        f"{scheme_id}.json",
    }

    tree = ast.parse(script.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".", 1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported_roots.add((node.module or "").split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {
            "shared",
            "schemes",
            "scheduler",
            "backtests",
            "subprocess",
            "multiprocessing",
        }
    )


@pytest.mark.parametrize("scheme_id", SUCCESSORS)
def test_daily_successor_request_and_result_fields_are_exact(
    scheme_id: str,
) -> None:
    module = _load_successor(scheme_id)

    assert module.validate_request(REQUEST) == REQUEST
    assert set(module.RESULT_FIELDS) == RESULT_FIELDS
    with pytest.raises(module.ContractError, match="exactly match"):
        module.validate_request(REQUEST | {"unexpected": "value"})


@pytest.mark.parametrize("scheme_id", SUCCESSORS)
def test_daily_successor_accepts_more_than_100_backtest_requests(
    scheme_id: str,
    tmp_path: Path,
) -> None:
    module = _load_successor(scheme_id)
    requests_path = tmp_path / f"{scheme_id}-101.csv"
    with requests_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=module.REQUEST_FIELDS)
        writer.writeheader()
        for index in range(101):
            writer.writerow(REQUEST | {"request_id": f"request-{index}"})

    requests = module.read_requests(
        SimpleNamespace(command="backtest", requests=requests_path)
    )

    assert len(requests) == 101
