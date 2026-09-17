from __future__ import annotations

import json
from io import BytesIO
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.error import HTTPError

import pytest
from sqlalchemy import create_engine, text

from harness.context import GateContext


BASE_SCHEME_ID = "demo_daily"


def _write_config(
    project_root: Path,
    *,
    scheme_id: str = BASE_SCHEME_ID,
    status: str = "active",
    version_status: str = "active",
    tenors: tuple[str, ...] = ("5Y", "10Y"),
) -> None:
    config_path = project_root / "schemes" / scheme_id / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
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
                "owner": "NEW-OWNER",
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
        "range_rows": (
            [["live", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0]] if with_live else []
        ),
        "backtest": {
            "benchmark_id": "benchmark-1",
            "benchmark_label": "Benchmark 1",
            "data_source": "framework_db_aligned",
            "data_source_label": "Current DB aligned",
            "latest_run_date": "2026-07-31",
        },
    }


def _other_scheme(target_tenor: str = "5Y") -> dict[str, Any]:
    return {
        **_scheme(target_tenor, with_live=False),
        "scheme_id": f"other_daily__h1__{target_tenor}",
        "base_scheme_id": "other_daily",
        "name": "Other Daily",
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
        "schema_version": "factor-lab-dashboard-v7",
        "representation": "summary",
        "snapshot_id": "dashboard-snapshot-1",
        "generated_at": "2026-08-10T12:00:00+08:00",
        "display_until": "2026-08-10",
        "live_feature_start_date": "2026-06-01",
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
        "range_row_fields": [
            "source", "samples", "metric_samples", "correct", "predicted_up",
            "predicted_down", "predicted_flat", "actual_up", "actual_down",
            "actual_flat", "up_true_positive", "down_true_positive",
        ],
        "selected_feature_range": None,
        "feature_date_bounds": {
            "all": {"start_date": "2026-08-03", "end_date": "2026-08-03"},
            "live": {"start_date": "2026-08-03", "end_date": "2026-08-03"},
            "backtest": None,
        },
        "target_labels": {tenor: f"{tenor} target" for tenor in tenors},
        "schemes": schemes,
    }


def _context(project_root: Path) -> GateContext:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE t_scheme_registry (scheme_id TEXT PRIMARY KEY, "
            "name TEXT, description TEXT, owner TEXT, status TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO t_scheme_registry VALUES "
            "(:scheme_id, :name, :description, :owner, :status)"
        ), [_scheme(tenor, with_live=False) for tenor in ("5Y", "10Y")])
    return GateContext(
        scheme_id=BASE_SCHEME_ID,
        predict_date="dashboard",
        project_root=project_root,
        config=SimpleNamespace(
            runtime_type="native_adapter",
            status="stale-context-must-not-be-trusted",
        ),
        api_base_url="http://127.0.0.1:8100/",
        engine_factory=lambda: engine,
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


def test_dashboard_gate_builds_prefixed_endpoint(tmp_path: Path) -> None:
    _write_config(tmp_path)
    seen: list[str] = []

    def fetcher(url, **_kwargs):
        seen.append(url)
        return _payload(), 200

    context = replace(
        _context(tmp_path),
        api_base_url="https://factor.example.test",
        api_prefix="/bond-factor-lab",
    )
    from harness.gates.dashboard_gate import DashboardGate

    result = DashboardGate(fetcher=fetcher).run(context)

    assert result.passed, result.errors
    assert seen == [
        "https://factor.example.test/bond-factor-lab/api/factor-lab/dashboard"
    ]


@pytest.mark.parametrize(
    "prefix",
    ["bond-factor-lab", "/bond-factor-lab/", "/a//b", "/../b", "/%62"],
)
def test_dashboard_gate_rejects_ambiguous_api_prefix(
    tmp_path: Path,
    prefix: str,
) -> None:
    _write_config(tmp_path)
    context = replace(
        _context(tmp_path),
        api_base_url="https://factor.example.test",
        api_prefix=prefix,
    )
    from harness.gates.dashboard_gate import DashboardGate

    result = DashboardGate(fetcher=lambda *_args, **_kwargs: (_payload(), 200)).run(
        context
    )

    assert not result.passed
    assert any("api_prefix" in error for error in result.errors)


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


def test_dashboard_gate_accepts_only_registered_live_only_partition(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, tenors=("5Y",))
    source = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "legacy_prediction_migration_compatibility_v1.json"
    )
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["entries"][0]["prediction_scheme_id"] = BASE_SCHEME_ID
    manifest["entries"][0]["designated_source_scheme_id"] = "demo_daily_bbv2"
    manifest["entries"][0]["historical_source_scheme_id"] = BASE_SCHEME_ID
    manifest["entries"][0]["horizon"] = 1
    target = tmp_path / "deploy" / source.name
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    payload = _payload(tenors=("5Y",))
    payload["schemes"][0]["backtest"] = None
    assert _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200)).passed

    payload["schemes"][0]["monthly_rows"] = []
    payload["schemes"][0]["range_rows"] = []
    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))
    assert not result.passed
    assert any("backtest" in error for error in result.errors)

    payload["schemes"][0]["monthly_rows"] = [
        ["2026-08", "live", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    ]
    payload["schemes"][0]["range_rows"] = [
        payload["schemes"][0]["monthly_rows"][0][1:]
    ]
    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))
    assert not result.passed
    assert any("backtest" in error for error in result.errors)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (("name", "different"), ("description", "different"), ("owner", "OTHER")),
)
def test_dashboard_gate_requires_display_to_match_registry(
    tmp_path: Path,
    field: str,
    invalid_value: str,
) -> None:
    _write_blackbox_config(tmp_path)
    payload = _payload(tenors=("5Y",))
    payload["schemes"][0][field] = invalid_value

    result = _run_gate(tmp_path, lambda _url, **_kwargs: (payload, 200))

    assert not result.passed
    assert any(field in error for error in result.errors)


def test_dashboard_gate_preserves_registered_display_after_runtime_migration(
    tmp_path: Path,
) -> None:
    _write_blackbox_config(tmp_path)
    result = _run_gate(
        tmp_path, lambda _url, **_kwargs: (_payload(tenors=("5Y",)), 200)
    )
    assert result.passed, result.errors


@pytest.mark.parametrize("case", ("no_engine", "missing", "paused", "query_error"))
def test_dashboard_gate_requires_readable_active_registry(
    tmp_path: Path, case: str,
) -> None:
    from harness.gates.dashboard_gate import DashboardGate

    _write_config(tmp_path)
    ctx = _context(tmp_path)
    if case == "no_engine":
        ctx.engine_factory().dispose()
        ctx = replace(ctx, engine_factory=None)
    else:
        with ctx.engine_factory().begin() as conn:
            conn.execute(text({
                "missing": "DELETE FROM t_scheme_registry",
                "paused": "UPDATE t_scheme_registry SET status = 'paused'",
                "query_error": "DROP TABLE t_scheme_registry",
            }[case]))
    result = DashboardGate(
        fetcher=lambda _url, **_kwargs: (_payload(), 200)
    ).run(ctx)
    assert not result.passed


def test_fetch_json_requires_session_and_does_not_expose_it(monkeypatch) -> None:
    from harness.gates import dashboard_gate

    captured: dict[str, Any] = {}

    class Response:
        status = 200
        headers = {"X-Request-ID": "request-1"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(_payload()).encode("utf-8")

    class Opener:
        def open(self, request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(dashboard_gate, "build_opener", lambda *_args: Opener())

    with pytest.raises(ValueError, match="session token"):
        dashboard_gate.fetch_json("https://example.test/api")

    payload, status, metadata = dashboard_gate.fetch_json(
        "https://example.test/api",
        session_token="opaque_session_token",
    )

    assert payload["snapshot_id"] == "dashboard-snapshot-1"
    assert status == 200
    assert metadata["request_id"] == "request-1"
    assert metadata["fetched_at"]
    assert captured["request"].get_header("Cookie") == (
        "__Host-bfl-session=opaque_session_token"
    )
    assert "opaque_session_token" not in repr(metadata)


def test_fetch_json_reports_expired_session_without_leaking_cookie(
    monkeypatch,
) -> None:
    from harness.gates import dashboard_gate

    class Opener:
        def open(self, request, *, timeout):
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {"X-Request-ID": "expired-request"},
                BytesIO(b'{"echo":"expired_session_token"}'),
            )

    monkeypatch.setattr(dashboard_gate, "build_opener", lambda *_args: Opener())

    with pytest.raises(dashboard_gate.ApiProbeError) as raised:
        dashboard_gate.fetch_json(
            "https://example.test/api",
            session_token="expired_session_token",
        )

    assert raised.value.status_code == 401
    assert raised.value.request_id == "expired-request"
    assert "expired_session_token" not in str(raised.value)
    assert raised.value.error_summary == "http_status_401"


def test_dashboard_gate_reports_expired_session_per_scheme(
    tmp_path: Path,
) -> None:
    from harness.gates.dashboard_gate import ApiProbeError, DashboardGate

    _write_config(tmp_path, tenors=("5Y",))

    def expired_fetcher(_url: str, **_kwargs):
        raise ApiProbeError(
            "HTTP 401",
            status_code=401,
            error_summary="authentication_required",
            request_id="expired-request",
            fetched_at="2026-09-16T01:02:03+00:00",
        )

    result = DashboardGate(fetcher=expired_fetcher).run(_context(tmp_path))
    evidence = {item.key: item.value for item in result.evidence}

    assert not result.passed
    assert evidence["http_status"] == 401
    assert evidence["request_id"] == "expired-request"
    assert evidence["scheme_results"][0]["status"] == "failed"
    assert "authentication_required" in evidence["scheme_results"][0]["errors"][0]


def test_authenticated_redirect_rejects_cross_origin() -> None:
    from harness.gates.dashboard_gate import _SameOriginRedirectHandler

    handler = _SameOriginRedirectHandler("https://example.test")
    request = SimpleNamespace(full_url="https://example.test/dashboard")

    with pytest.raises(Exception, match="cross-origin redirect"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://other.test/dashboard",
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"x" * 33, "exceeds"),
        (b"not-json", "not valid UTF-8 JSON"),
    ],
)
def test_fetch_json_rejects_oversized_or_invalid_json(
    monkeypatch,
    raw: bytes,
    expected: str,
) -> None:
    from harness.gates import dashboard_gate

    class Response:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit: int) -> bytes:
            return raw[:limit]

    class Opener:
        def open(self, _request, *, timeout):
            return Response()

    monkeypatch.setattr(dashboard_gate, "build_opener", lambda *_args: Opener())

    with pytest.raises(dashboard_gate.ApiProbeError, match=expected):
        dashboard_gate.fetch_json(
            "https://example.test/api",
            session_token="opaque_session_token",
            max_response_bytes=32,
        )


def test_dashboard_gate_batch_fetches_once_and_reports_each_scheme(
    tmp_path: Path,
) -> None:
    from harness.gates.dashboard_gate import DashboardGate

    _write_config(tmp_path, tenors=("5Y",))
    _write_config(
        tmp_path,
        scheme_id="other_daily",
        tenors=("5Y",),
    )
    payload = _payload(tenors=("5Y",))
    payload["schemes"].append(_other_scheme())
    calls: list[str] = []

    def fetcher(url: str, **_kwargs):
        calls.append(url)
        return payload, 200, {
            "request_id": "shared-request",
            "fetched_at": "2026-09-16T01:02:03+00:00",
        }

    ctx = replace(
        _context(tmp_path),
        dashboard_scheme_ids=(BASE_SCHEME_ID, "other_daily"),
    )
    with ctx.engine_factory().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "(:scheme_id, :name, :description, :owner, :status)"
            ),
            {
                "scheme_id": "other_daily__h1__5Y",
                "name": "Other Daily",
                "description": "Dashboard gate fixture",
                "owner": "ALGO-A",
                "status": "active",
            },
        )

    result = DashboardGate(fetcher=fetcher).run(ctx)

    assert len(calls) == 1
    evidence = {item.key: item.value for item in result.evidence}
    assert [item["base_scheme_id"] for item in evidence["scheme_results"]] == [
        BASE_SCHEME_ID,
        "other_daily",
    ]
    assert {item["snapshot_id"] for item in evidence["scheme_results"]} == {
        "dashboard-snapshot-1"
    }
    assert {item["fetched_at"] for item in evidence["scheme_results"]} == {
        "2026-09-16T01:02:03+00:00"
    }
    assert {item["request_id"] for item in evidence["scheme_results"]} == {
        "shared-request"
    }
    assert result.passed


def test_dashboard_gate_batch_preserves_one_scheme_failure(
    tmp_path: Path,
) -> None:
    from harness.gates.dashboard_gate import DashboardGate

    _write_config(tmp_path, tenors=("5Y",))
    _write_config(
        tmp_path,
        scheme_id="other_daily",
        tenors=("5Y",),
    )
    payload = _payload(tenors=("5Y",))
    payload["schemes"].append({**_other_scheme(), "owner": "WRONG"})
    ctx = replace(
        _context(tmp_path),
        dashboard_scheme_ids=(BASE_SCHEME_ID, "other_daily"),
    )
    with ctx.engine_factory().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "(:scheme_id, :name, :description, :owner, :status)"
            ),
            {
                "scheme_id": "other_daily__h1__5Y",
                "name": "Other Daily",
                "description": "Dashboard gate fixture",
                "owner": "ALGO-A",
                "status": "active",
            },
        )

    result = DashboardGate(
        fetcher=lambda _url, **_kwargs: (
            payload,
            200,
            {"request_id": "request-1", "fetched_at": "fetched-once"},
        )
    ).run(ctx)

    evidence = {item.key: item.value for item in result.evidence}
    by_scheme = {
        item["base_scheme_id"]: item for item in evidence["scheme_results"]
    }
    assert by_scheme[BASE_SCHEME_ID]["status"] == "passed"
    assert by_scheme["other_daily"]["status"] == "failed"
    assert any("owner mismatch" in error for error in by_scheme["other_daily"]["errors"])
    assert not result.passed


def test_dashboard_gate_redacts_session_echoes_from_result(
    tmp_path: Path,
) -> None:
    from harness.gates.dashboard_gate import DashboardGate

    _write_config(tmp_path, tenors=("5Y",))
    token = "never_write_this_session_token"
    payload = _payload(tenors=("5Y",))
    payload["snapshot_id"] = token
    payload["schemes"][0]["owner"] = f"reflected-{token}"
    ctx = replace(
        _context(tmp_path),
        dashboard_session_token=token,
    )
    result = DashboardGate(
        fetcher=lambda _url, **_kwargs: (
            payload,
            200,
            {
                "request_id": token,
                "fetched_at": "2026-09-16T01:02:03+00:00",
            },
        )
    ).run(ctx)

    serialized = repr(result)
    assert token not in serialized
    assert "[redacted-session]" in serialized


def test_dashboard_gate_redacts_session_echo_from_probe_error(
    tmp_path: Path,
) -> None:
    from harness.gates.dashboard_gate import ApiProbeError, DashboardGate

    _write_config(tmp_path, tenors=("5Y",))
    token = "never_write_this_session_token"
    ctx = replace(
        _context(tmp_path),
        dashboard_session_token=token,
    )

    def fail(_url: str, **_kwargs):
        raise ApiProbeError(
            "unsafe server response",
            status_code=500,
            error_summary=f"reflected-{token}",
            request_id=token,
        )

    result = DashboardGate(fetcher=fail).run(ctx)

    serialized = repr(result)
    assert token not in serialized
    assert "[redacted-session]" in serialized


def test_dashboard_gate_redacts_session_from_unexpected_fetcher_error(
    tmp_path: Path,
) -> None:
    from harness.gates.dashboard_gate import DashboardGate

    _write_config(tmp_path, tenors=("5Y",))
    token = "never_write_this_session_token"
    ctx = replace(
        _context(tmp_path),
        dashboard_session_token=token,
    )

    def fail(_url: str, **_kwargs):
        raise RuntimeError(f"unexpected reflected value: {token}")

    result = DashboardGate(fetcher=fail).run(ctx)

    serialized = repr(result)
    assert token not in serialized
    assert "[redacted-session]" in serialized


def test_dashboard_cli_requires_secret_input_and_accepts_repeated_schemes() -> None:
    from harness.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["gate", "dashboard", "--scheme-id", BASE_SCHEME_ID]
        )

    parsed = parser.parse_args(
        [
            "gate",
            "dashboard",
            "--scheme-id",
            BASE_SCHEME_ID,
            "--scheme-id",
            "other_daily",
            "--session-fd",
            "7",
        ]
    )
    assert parsed.scheme_id == [BASE_SCHEME_ID, "other_daily"]
    assert parsed.session_fd == 7


def test_dashboard_session_can_be_read_from_private_file_or_fd(
    tmp_path: Path,
) -> None:
    from harness.cli import _read_dashboard_session

    session_path = tmp_path / "dashboard-session"
    session_path.write_text("opaque_session_token\n", encoding="utf-8")
    session_path.chmod(0o600)
    assert _read_dashboard_session(
        session_file=session_path.resolve(),
        session_fd=None,
    ) == "opaque_session_token"

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"fd_session_token\n")
        os.close(write_fd)
        write_fd = -1
        assert _read_dashboard_session(
            session_file=None,
            session_fd=read_fd,
        ) == "fd_session_token"
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_dashboard_session_fd_does_not_block_while_writer_remains_open() -> None:
    from harness.cli import _read_dashboard_session

    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(
            SystemExit,
            match="dashboard session fd is not ready or is incomplete",
        ):
            _read_dashboard_session(
                session_file=None,
                session_fd=read_fd,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_dashboard_session_file_rejects_public_permissions(
    tmp_path: Path,
) -> None:
    from harness.cli import _read_dashboard_session

    session_path = tmp_path / "dashboard-session"
    session_path.write_text("opaque_session_token\n", encoding="utf-8")
    session_path.chmod(0o644)

    with pytest.raises(SystemExit, match="group/world"):
        _read_dashboard_session(
            session_file=session_path.resolve(),
            session_fd=None,
        )


def test_dashboard_session_file_rejects_non_regular_path(
    tmp_path: Path,
) -> None:
    from harness.cli import _read_dashboard_session

    session_path = tmp_path / "dashboard-session-pipe"
    os.mkfifo(session_path, mode=0o600)

    with pytest.raises(SystemExit, match="owned regular file"):
        _read_dashboard_session(
            session_file=session_path.resolve(),
            session_fd=None,
        )


def test_dashboard_session_token_is_hidden_from_context_repr(
    tmp_path: Path,
) -> None:
    ctx = replace(
        _context(tmp_path),
        dashboard_session_token="never_write_this_token",
    )

    assert "never_write_this_token" not in repr(ctx)
