from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from harness.context import GateContext
from harness.probes.api_probe import ApiProbeError


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


def _scheme(target_tenor: str, *, signal_status: str) -> dict[str, Any]:
    return {
        "scheme_id": f"{BASE_SCHEME_ID}__h1__{target_tenor}",
        "base_scheme_id": BASE_SCHEME_ID,
        "name": "Demo Daily",
        "description": "Dashboard gate fixture",
        "horizon": 1,
        "task_type": "T+1",
        "frequency": "daily",
        "target_tenor": target_tenor,
        "target_label": f"{target_tenor} target",
        "status": "active",
        "deployed_at": "2026-08-01",
        "signal_status": signal_status,
        "signal_failure_category": None,
        "live_rows": (
            [
                [
                    "2026-08-10",
                    "2026-08-07",
                    "2026-08-11",
                    "scheduled_live",
                    1,
                    None,
                ]
            ]
            if signal_status == "present"
            else []
        ),
        "backtest": {
            "benchmark_id": "benchmark-1",
            "benchmark_label": "Benchmark 1",
            "data_source": "framework_db_aligned",
            "data_source_label": "Current DB aligned",
            "latest_run_date": "2026-07-31",
            "rows": [],
        },
    }


def _payload(
    *,
    tenors: tuple[str, ...] = ("5Y", "10Y"),
) -> dict[str, Any]:
    schemes = [
        _scheme(tenor, signal_status="present" if index == 0 else "not_due")
        for index, tenor in enumerate(tenors)
    ]
    schemes.sort(key=lambda row: row["scheme_id"])
    return {
        "schema_version": "factor-lab-dashboard-v1",
        "snapshot_id": "dashboard-snapshot-1",
        "generated_at": "2026-08-10T12:00:00+08:00",
        "display_until": "2026-08-10",
        "stale": False,
        "snapshot_age_ms": 0,
        "row_fields": [
            "predict_date",
            "feature_date",
            "target_date",
            "prediction_phase",
            "predicted_direction",
            "actual_direction",
        ],
        "target_labels": {tenor: f"{tenor} target" for tenor in tenors},
        "schemes": schemes,
    }


def _context(project_root: Path) -> GateContext:
    return GateContext(
        scheme_id=BASE_SCHEME_ID,
        predict_date="dashboard",
        project_root=project_root,
        report_dir=project_root / "reports",
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


def _evidence(result) -> dict[str, Any]:
    return {item.key: item.value for item in result.evidence}


def test_dashboard_gate_accepts_present_not_due_pending_actual_and_empty_backtest_rows(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    payload = _payload()
    calls: list[tuple[str, dict[str, Any]]] = []

    def fetcher(url: str, **kwargs: Any):
        calls.append((url, kwargs))
        return payload, 200

    result = _run_gate(tmp_path, fetcher)

    assert result.passed, result.errors
    assert calls == [
        (
            "http://127.0.0.1:8100/api/factor-lab/dashboard",
            {"timeout_sec": 30},
        )
    ]
    evidence = _evidence(result)
    assert evidence["config_status"] == "active"
    assert evidence["config_version_status"] == "active"
    assert evidence["signal_statuses"] == {
        "demo_daily__h1__5Y": "present",
        "demo_daily__h1__10Y": "not_due",
    }
    assert set(evidence["backtest_registry_ids"]) == {
        "demo_daily__h1__5Y",
        "demo_daily__h1__10Y",
    }
    assert "scheme_version" not in evidence


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
            "response_budget",
            lambda _payload: lambda _url, **_kwargs: (_raise_budget_error()),
        ),
        (
            "shared_validation",
            lambda payload: lambda _url, **_kwargs: (
                {**payload, "schema_version": "wrong"},
                200,
            ),
        ),
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


def _raise_budget_error() -> tuple[dict[str, Any], int]:
    raise ApiProbeError("API response exceeds 1048576 bytes")


@pytest.mark.parametrize(
    ("status", "version_status"),
    [("paused", "active"), ("active", "draft")],
)
def test_dashboard_gate_requires_canonical_config_active_plus_active(
    tmp_path: Path,
    status: str,
    version_status: str,
) -> None:
    _write_config(
        tmp_path,
        status=status,
        version_status=version_status,
        tenors=("5Y",),
    )
    calls = 0

    def fetcher(_url: str, **_kwargs: Any):
        nonlocal calls
        calls += 1
        return _payload(tenors=("5Y",)), 200

    result = _run_gate(tmp_path, fetcher)

    assert not result.passed
    assert calls == 0
    assert any("active+active" in error for error in result.errors)


def test_dashboard_gate_requires_each_config_composite_id_exactly_once(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    payload = _payload()
    payload["schemes"] = [payload["schemes"][0], payload["schemes"][0]]

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed


@pytest.mark.parametrize(
    "mutation",
    [
        {"base_scheme_id": "other", "scheme_id": "other__h1__5Y"},
        {"horizon": 2, "scheme_id": f"{BASE_SCHEME_ID}__h2__5Y"},
        {"task_type": "T+5"},
        {"frequency": "weekly"},
        {"target_tenor": "7Y", "scheme_id": f"{BASE_SCHEME_ID}__h1__7Y"},
        {"status": "paused"},
    ],
)
def test_dashboard_gate_rejects_public_identity_mismatches(
    tmp_path: Path,
    mutation: dict[str, Any],
) -> None:
    _write_config(tmp_path, tenors=("5Y",))
    payload = _payload(tenors=("5Y",))
    payload["schemes"][0].update(mutation)
    if mutation.get("target_tenor") == "7Y":
        payload["target_labels"]["7Y"] = "7Y target"
        payload["schemes"][0]["target_label"] = "7Y target"

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed, mutation


def test_dashboard_gate_fails_missing_signal_and_preserves_failure_category(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, tenors=("5Y",))
    payload = _payload(tenors=("5Y",))
    payload["schemes"][0].update(
        signal_status="missing",
        signal_failure_category="upstream_timeout",
        live_rows=[],
    )

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed
    assert "upstream_timeout" in "\n".join(result.errors)
    assert _evidence(result)["signal_failure_categories"] == {
        "demo_daily__h1__5Y": "upstream_timeout"
    }


def test_dashboard_gate_requires_backtest_partition(tmp_path: Path) -> None:
    _write_config(tmp_path, tenors=("5Y",))
    payload = _payload(tenors=("5Y",))
    payload["schemes"][0]["backtest"] = None

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed
    assert any("backtest" in error for error in result.errors)


def test_registry_exposes_only_dashboard_post_activation_gate() -> None:
    from harness.gates.dashboard_gate import DashboardGate
    from harness.registry import gate_for_name

    for runtime_type in ("native_adapter", "blackbox_v2"):
        ctx = GateContext(
            scheme_id=BASE_SCHEME_ID,
            predict_date="dashboard",
            project_root=Path("/tmp/dashboard-gate"),
            report_dir=Path("/tmp/dashboard-gate/reports"),
            config=SimpleNamespace(runtime_type=runtime_type),
        )
        assert isinstance(gate_for_name("dashboard", ctx=ctx), DashboardGate)
        for old_name in ("api", "api-readiness"):
            with pytest.raises(ValueError, match="unsupported"):
                gate_for_name(old_name, ctx=ctx)


def test_cli_exposes_dashboard_and_rejects_old_names_and_nonce() -> None:
    from harness.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "gate",
            "dashboard",
            "--scheme-id",
            BASE_SCHEME_ID,
            "--api-base-url",
            "http://127.0.0.1:8100",
        ]
    )

    assert args.gate_name == "dashboard"
    assert args.predict_date == "dashboard"
    assert not hasattr(args, "api_instance_nonce")
    for old_name in ("api", "api-readiness"):
        with pytest.raises(SystemExit):
            parser.parse_args(["gate", old_name, "--scheme-id", BASE_SCHEME_ID])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "gate",
                "dashboard",
                "--scheme-id",
                BASE_SCHEME_ID,
                "--api-instance-nonce",
                "legacy",
            ]
        )


class _FakeUrlResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body


def test_run_gate_wires_custom_dashboard_url_and_timeout_to_default_fetcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.cli import _build_parser, _run_gate
    from harness.probes import api_probe

    _write_config(tmp_path)
    response = _FakeUrlResponse(json.dumps(_payload()).encode("utf-8"))
    calls: list[tuple[Any, int]] = []

    def fake_urlopen(request: Any, *, timeout: int):
        calls.append((request, timeout))
        return response

    monkeypatch.setattr(api_probe, "urlopen", fake_urlopen)
    args = _build_parser().parse_args(
        [
            "gate",
            "dashboard",
            "--scheme-id",
            BASE_SCHEME_ID,
            "--project-root",
            str(tmp_path),
            "--api-base-url",
            "http://dashboard.internal:9123/",
            "--timeout-sec",
            "7",
        ]
    )

    result = _run_gate(args)

    assert result.passed, result.errors
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == (
        "http://dashboard.internal:9123/api/factor-lab/dashboard"
    )
    assert request.get_method() == "GET"
    assert timeout == 7


def test_fetch_json_uses_get_timeout_and_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.probes import api_probe

    response = _FakeUrlResponse(b'{"ok": true}')
    calls: list[tuple[Any, int]] = []

    def fake_urlopen(request: Any, *, timeout: int):
        calls.append((request, timeout))
        return response

    monkeypatch.setattr(api_probe, "urlopen", fake_urlopen)

    payload, status = api_probe.fetch_json(
        "http://dashboard.internal:9123/api/factor-lab/dashboard",
        timeout_sec=9,
    )

    assert payload == {"ok": True}
    assert status == 200
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url.endswith("/api/factor-lab/dashboard")
    assert request.get_method() == "GET"
    assert timeout == 9
    assert response.read_sizes == [api_probe.DEFAULT_MAX_RESPONSE_BYTES + 1]


def test_fetch_json_fails_when_bounded_read_exceeds_response_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.probes import api_probe

    response = _FakeUrlResponse(
        b"x" * (api_probe.DEFAULT_MAX_RESPONSE_BYTES + 1)
    )

    def fake_urlopen(_request: Any, *, timeout: int):
        assert timeout == 5
        return response

    monkeypatch.setattr(api_probe, "urlopen", fake_urlopen)

    with pytest.raises(
        ApiProbeError,
        match=f"exceeds {api_probe.DEFAULT_MAX_RESPONSE_BYTES} bytes",
    ) as exc_info:
        api_probe.fetch_json(
            "http://dashboard.internal:9123/api/factor-lab/dashboard",
            timeout_sec=5,
        )

    assert exc_info.value.status_code == 200
    assert response.read_sizes == [api_probe.DEFAULT_MAX_RESPONSE_BYTES + 1]
