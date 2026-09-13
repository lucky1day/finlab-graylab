from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from harness.context import GateContext


BASE_SCHEME_ID = "demo_daily"


def _write_config(
    project_root: Path,
    *,
    status: str = "active",
    version_status: str = "active",
    tenors: tuple[str, ...] = ("5Y", "10Y"),
) -> None:
    config_path = project_root / "schemes" / BASE_SCHEME_ID / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                f"scheme_id: {BASE_SCHEME_ID}",
                "runtime_type: native_adapter",
                'name: "Demo Daily"',
                'description: "Dashboard gate fixture"',
                "horizon: 1",
                "task_type: T+1",
                f"tenors: [{', '.join(tenors)}]",
                "frequency: daily",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "input_spec:",
                "  data_version: shared_data_service_daily.v1",
                '  required_columns: ["date", "TB0YWI0C"]',
                f"status: {status}",
                f"version_status: {version_status}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_blackbox_config(project_root: Path) -> None:
    scheme_dir = project_root / "schemes" / BASE_SCHEME_ID
    delivery_dir = scheme_dir / "delivery"
    delivery_dir.mkdir(parents=True)
    (delivery_dir / f"{BASE_SCHEME_ID}.py").write_text(
        "print('fixture')\n",
        encoding="utf-8",
    )
    (delivery_dir / f"{BASE_SCHEME_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scheme_id": BASE_SCHEME_ID,
                "name": "Demo Blackbox",
                "owner": "ALGO-A",
                "description": "Blackbox dashboard fixture",
                "algorithm_version": "1.0.0",
                "target_tenor": "5Y",
                "task_type": "T+1",
                "horizon": 1,
                "target_rule": "target_date_yield_vs_feature_date_yield",
            }
        ),
        encoding="utf-8",
    )
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {BASE_SCHEME_ID}",
                "runtime_type: blackbox_v2",
                "input_source: data_bridge_current",
                "runtime_profile: blackbox-v2-v1",
                "data_schema_version: data-bridge-v1",
                "status: active",
                "version_status: active",
                "schedule:",
                '  cron: "3 7 * * 1-5"',
                "  timeout_sec: 3600",
                "delivery:",
                f"  script: delivery/{BASE_SCHEME_ID}.py",
                f"  metadata: delivery/{BASE_SCHEME_ID}.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _scheme(target_tenor: str, *, with_live: bool) -> dict[str, Any]:
    return {
        "scheme_id": f"{BASE_SCHEME_ID}__h1__{target_tenor}",
        "base_scheme_id": BASE_SCHEME_ID,
        "is_production": False,
        "name": "Demo Daily",
        "owner": "ALGO-A",
        "description": "Dashboard gate fixture",
        "horizon": 1,
        "task_type": "T+1",
        "frequency": "daily",
        "target_tenor": target_tenor,
        "target_label": f"{target_tenor} target",
        "status": "active",
        "deployed_at": "2026-08-01",
        "monthly_rows": (
            [["2026-08", "live", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0]]
            if with_live
            else []
        ),
        "backtest": {
            "benchmark_id": "benchmark-1",
            "benchmark_label": "Benchmark 1",
            "data_source": "framework_db_aligned",
            "data_source_label": "Current DB aligned",
            "latest_run_date": "2026-07-31",
        },
    }


def _payload(
    *,
    tenors: tuple[str, ...] = ("5Y", "10Y"),
) -> dict[str, Any]:
    schemes = [
        _scheme(tenor, with_live=index == 0)
        for index, tenor in enumerate(tenors)
    ]
    schemes.sort(key=lambda row: row["scheme_id"])
    return {
        "schema_version": "factor-lab-dashboard-v6",
        "representation": "summary",
        "snapshot_id": "dashboard-snapshot-1",
        "generated_at": "2026-08-10T12:00:00+08:00",
        "display_until": "2026-08-10",
        "live_target_start_date": "2026-06-01",
        "monthly_row_fields": [
            "month",
            "source",
            "samples",
            "metric_samples",
            "correct",
            "predicted_up",
            "predicted_down",
            "predicted_flat",
            "actual_up",
            "actual_down",
            "actual_flat",
            "up_true_positive",
            "down_true_positive",
        ],
        "target_labels": {tenor: f"{tenor} target" for tenor in tenors},
        "schemes": schemes,
    }


def _context(project_root: Path) -> GateContext:
    return GateContext(
        scheme_id=BASE_SCHEME_ID,
        predict_date="dashboard",
        project_root=project_root,
        config=SimpleNamespace(
            runtime_type="native_adapter",
            status="stale-context-must-not-be-trusted",
        ),
        api_base_url="http://127.0.0.1:8100/",
    )


def _run_gate(
    project_root: Path,
    fetcher: Callable[..., Any],
):
    from harness.gates.dashboard_gate import DashboardGate

    return DashboardGate(fetcher=fetcher).run(_context(project_root))


def test_dashboard_gate_accepts_existing_and_empty_live_months(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    result = _run_gate(tmp_path, lambda _url, **_kwargs: (_payload(), 200))

    assert result.passed, result.errors


@pytest.mark.parametrize(
    ("case", "fetcher_factory"),
    [
        (
            "http_non_200",
            lambda payload: lambda _url, **_kwargs: (payload, 503),
        ),
        (
            "non_json",
            lambda _payload: lambda _url, **_kwargs: ("not-json", 200),
        ),
        (
            "v2_schema_rejected",
            lambda payload: lambda _url, **_kwargs: (
                {**payload, "schema_version": "factor-lab-dashboard-v2"},
                200,
            ),
        ),
        *[
            (
                f"invalid_production_flag_{value!r}",
                lambda payload, value=value: lambda _url, **_kwargs: (
                    {**payload, "schemes": [
                        {**scheme, "is_production": value}
                        for scheme in payload["schemes"]
                    ]},
                    200,
                ),
            )
            for value in (1, "true", None)
        ],
    ],
)
def test_dashboard_gate_fails_closed_on_probe_or_shared_validation_errors(
    tmp_path: Path,
    case: str,
    fetcher_factory: Callable[[dict[str, Any]], Callable[..., Any]],
) -> None:
    _write_config(tmp_path)

    result = _run_gate(tmp_path, fetcher_factory(_payload()))

    assert not result.passed, (case, result.errors)
    assert result.errors, case


def test_dashboard_gate_uses_dashboard_database_lifecycle_not_declared_status(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        status="paused",
        version_status="draft",
        tenors=("5Y",),
    )
    result = _run_gate(
        tmp_path,
        lambda _url, **_kwargs: (_payload(tenors=("5Y",)), 200),
    )

    assert result.passed


def test_dashboard_gate_requires_each_config_composite_id_exactly_once(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    payload = _payload()
    payload["schemes"] = [payload["schemes"][0], payload["schemes"][0]]

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed


def test_dashboard_gate_requires_backtest_partition(tmp_path: Path) -> None:
    _write_config(tmp_path, tenors=("5Y",))
    payload = _payload(tenors=("5Y",))
    payload["schemes"][0]["backtest"] = None

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed
    assert any("backtest" in error for error in result.errors)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (("name", ""), ("description", "different")),
)
def test_dashboard_gate_requires_exact_new_blackbox_display_identity(
    tmp_path: Path,
    field: str,
    invalid_value: str,
) -> None:
    _write_blackbox_config(tmp_path)
    payload = _payload(tenors=("5Y",))
    payload["schemes"][0].update(
        name="Demo Blackbox",
        description="Blackbox dashboard fixture",
    )
    payload["schemes"][0][field] = invalid_value

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed
    assert any(field in error for error in result.errors)
