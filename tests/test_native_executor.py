from __future__ import annotations

import os
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.run_launchd_release import prepare_exec_environment


_RELEASE_COMMIT = "a" * 40


def _trusted_release(tmp_path: Path) -> tuple[Path, Path]:
    release = tmp_path / "deploy" / "releases" / _RELEASE_COMMIT
    release.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config = runtime / "config"
    config.mkdir(mode=0o700)
    service_environment = config / "service.env"
    service_environment.write_text(
        "\n".join(
            (
                "BOND_DB_USER=bond_user",
                "BOND_DB_PASSWORD=database-secret",
                "BOND_DB_HOST=127.0.0.1",
                "BOND_DB_PORT=3306",
                "BOND_DB_NAME=bond_db",
                "BOND_DB_CHARSET=utf8mb4",
                "DATABRIDGE_API_BASE_URL=https://example.invalid",
                "DATABRIDGE_API_USERNAME=bridge_user",
                "DATABRIDGE_API_PASSWORD=bridge-secret",
                "",
            )
        ),
        encoding="utf-8",
    )
    service_environment.chmod(0o600)
    native_cache = runtime / "cache" / "native" / _RELEASE_COMMIT
    release_environment = release / ".bfl-release.env"
    release_environment.write_text(
        "\n".join(
            (
                f"BFL_RELEASE_COMMIT={_RELEASE_COMMIT}",
                f"BFL_RUNTIME_ROOT={runtime}",
                f"NUMBA_CACHE_DIR={native_cache / 'numba'}",
                f"MPLCONFIGDIR={native_cache / 'matplotlib'}",
                "",
            )
        ),
        encoding="utf-8",
    )
    release_environment.chmod(0o444)
    release.chmod(0o555)
    return release, runtime


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_called_process_error_does_not_persist_unrecognized_output(
    stream: str,
) -> None:
    import subprocess

    from scheduler.executor import _execution_error_message

    payload = (
        "DATABASE_URL=mysql://writer:s3cr3t@db/bond "
        "password=hunter2 token=secret-token"
    )
    kwargs = {"output": "", "stderr": ""}
    kwargs["stderr" if stream == "stderr" else "output"] = payload
    exc = subprocess.CalledProcessError(7, ["algorithm"], **kwargs)

    message = _execution_error_message(exc)

    assert message == "algorithm process exited with status 7"
    assert "s3cr3t" not in message
    assert "hunter2" not in message
    assert "secret-token" not in message


def test_native_execution_rejects_invalid_runtime_controls() -> None:
    from scheduler.executor import run_configured_scheme

    native = SimpleNamespace(
        runtime_type="native_adapter",
        scheme_id="daily_demo",
    )
    blackbox = SimpleNamespace(
        runtime_type="blackbox_v2",
        input_source="data_bridge_current",
        scheme_id="blackbox_demo",
    )
    common = {
        "engine": object(),
        "algo_env": "forecast_env",
        "timeout_sec": 600,
    }
    with pytest.raises(ValueError, match="absolute"):
        run_configured_scheme(
            native,
            "2026-07-24",
            ephemeral_native_runtime_root="relative/root",
            **common,
        )
    with pytest.raises(ValueError, match="native_adapter"):
        run_configured_scheme(
            blackbox,
            "2026-07-24",
            ephemeral_native_runtime_root=Path("/private/native-gap"),
            **common,
        )


@pytest.mark.parametrize("ephemeral_input", [False, True], ids=["default-input", "ephemeral-input"])
def test_native_subprocess_environment_is_allowlisted(
    tmp_path: Path, ephemeral_input: bool,
) -> None:
    from scheduler.executor import run_scheme_subprocess
    from shared.input_artifacts import EPHEMERAL_NATIVE_INPUT_ROOT_ENV

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    parent = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/Users/tester",
        "TMPDIR": "/tmp/tester/",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "OMP_NUM_THREADS": "2",
        "BFL_RUNTIME_ROOT": "/var/lib/bond-factor-lab/runtime",
        "NUMBA_CACHE_DIR": (
            "/var/lib/bond-factor-lab/runtime/cache/native/abc/numba"
        ),
        "MPLCONFIGDIR": (
            "/var/lib/bond-factor-lab/runtime/cache/native/abc/matplotlib"
        ),
        "BFL_DATABASE_ENV_FILE": "/etc/bond-factor-lab/bond-factor-lab.env",
        "BOND_DB_PASSWORD": "secret",
        "UNTRUSTED_PARENT_VALUE": "must-not-be-inherited",
    }
    with (
        patch.dict(os.environ, parent, clear=True),
        patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ),
    ):
        run_scheme_subprocess(
            "daily_demo", "2026-07-24",
            **({"ephemeral_native_runtime_root": tmp_path.resolve()} if ephemeral_input else {}),
        )

    assert captured["PATH"] == parent["PATH"]
    assert captured["BFL_RUNTIME_ROOT"] == (
        "/var/lib/bond-factor-lab/runtime"
    )
    assert captured["NUMBA_CACHE_DIR"] == parent["NUMBA_CACHE_DIR"]
    assert captured["MPLCONFIGDIR"] == parent["MPLCONFIGDIR"]
    assert captured["BFL_DATABASE_ENV_FILE"] == parent[
        "BFL_DATABASE_ENV_FILE"
    ]
    assert "BOND_DB_PASSWORD" not in captured
    assert "UNTRUSTED_PARENT_VALUE" not in captured

    if ephemeral_input:
        assert captured[EPHEMERAL_NATIVE_INPUT_ROOT_ENV] == str(tmp_path.resolve())
    else:
        assert EPHEMERAL_NATIVE_INPUT_ROOT_ENV not in captured


def test_launchd_environment_passes_only_trusted_database_file_to_native(
    tmp_path: Path,
) -> None:
    from scheduler.executor import run_scheme_subprocess

    captured: dict[str, str] = {}

    def fake_run(cmd, *, cwd, env, timeout):
        captured.update(env)
        return CompletedProcess(cmd, 0, "[]", "")

    release, runtime = _trusted_release(tmp_path)
    try:
        parent = prepare_exec_environment(release, {})
        with (
            patch.dict(os.environ, parent, clear=True),
            patch(
                "scheduler.executor._run_process_group",
                side_effect=fake_run,
            ),
        ):
            run_scheme_subprocess("daily_demo", "2026-07-24")
    finally:
        release.chmod(0o755)

    assert captured["BFL_DATABASE_ENV_FILE"] == str(
        runtime / "config" / "service.env"
    )
    assert "BOND_DB_USER" not in captured
    assert "BOND_DB_PASSWORD" not in captured
    assert "BOND_DB_HOST" not in captured


def test_v2_policy_timeout_is_a_hard_upper_bound() -> None:
    from scheduler.executor import _effective_timeout_sec

    cfg = SimpleNamespace(
        scheme_id="daily_v2",
        runtime_type="blackbox_v2",
        schedule=SimpleNamespace(timeout_sec=3600),
    )
    assert _effective_timeout_sec(cfg, 120) == 120


@pytest.mark.parametrize(
    ("configured_timeout", "operation_timeout", "expected_timeout"),
    [
        (None, 900, 900),
        (600, 900, 600),
        (3600, 900, 3600),
    ],
)
def test_native_operation_timeout_is_only_the_unconfigured_fallback(
    configured_timeout: int | None,
    operation_timeout: int,
    expected_timeout: int,
) -> None:
    from scheduler.executor import _effective_timeout_sec

    cfg = SimpleNamespace(
        scheme_id="native_publisher",
        runtime_type="native_adapter",
        schedule=SimpleNamespace(timeout_sec=configured_timeout),
    )
    assert _effective_timeout_sec(cfg, operation_timeout) == expected_timeout


def test_source_runtime_native_runs_one_process_with_full_budget(tmp_path):
    from contextlib import nullcontext
    from scheduler.executor import run_scheme_subprocess
    calls=[]
    def run(cmd,**kwargs):
        calls.append((cmd,kwargs))
        return CompletedProcess(cmd,0,'[]','')
    with (
        patch('scheduler.executor.frozen_source_runtime_database_config',return_value=nullcontext(tmp_path/'source-db.json')),
        patch('scheduler.executor._run_process_group',side_effect=run),
    ):
        result=run_scheme_subprocess('daily_1y_xgb_1y13_0629','2026-10-29',
                                     source_database_config=object(),timeout_sec=73)
    assert result==[] and len(calls)==1
    assert calls[0][1]['timeout']==73
    assert 'scheduler.scheme_runner' in calls[0][0]


@pytest.mark.parametrize("owns_engine", [True, False])
@pytest.mark.parametrize(
    ("runtime_type", "failing_read"),
    [
        ("blackbox_v2", "read_blackbox_execution_approval"),
        ("native_adapter", "_verify_scheme_activation"),
        ("blackbox_v2", "_active_registry_targets"),
    ],
)
def test_execution_preflight_failure_preserves_engine_ownership(
    owns_engine: bool, runtime_type: str, failing_read: str,
) -> None:
    """任何预检查询失败都释放本次创建的引擎，不接管调用者引擎。"""
    from scheduler import executor

    cfg = SimpleNamespace(
        scheme_id="demo", runtime_type=runtime_type, status="active",
        scheme_version="exact",
    )
    engine = Mock()
    with (
        patch.object(executor, "scheduled_live_execution_configuration_error", return_value=None),
        patch.object(executor, "create_engine_from_env", return_value=engine),
        patch.object(executor, "read_blackbox_execution_approval", return_value=SimpleNamespace(executable=True)),
        patch.object(executor, "_verify_scheme_activation", return_value=(True, "")),
        patch.object(executor, failing_read, side_effect=RuntimeError("preflight unavailable")),
        pytest.raises(RuntimeError, match="preflight unavailable"),
    ):
        executor.execute_scheme(
            cfg, "2026-11-06", prediction_phase="scheduled_live",
            **({} if owns_engine else {"engine": engine}),
        )
    assert engine.dispose.call_count == int(owns_engine)


@pytest.mark.parametrize("runtime_type", ["native_adapter", "blackbox_v2"])
def test_interrupted_execution_settles_its_run_and_preserves_interrupt(runtime_type):
    from scheduler import executor

    interrupted = KeyboardInterrupt("cancelled test execution")
    cfg = SimpleNamespace(
        scheme_id="demo", runtime_type=runtime_type, status="active",
        scheme_version="exact", execution_timeout_sec=10,
    )
    engine = Mock()
    with (
        patch.object(executor, "scheduled_live_execution_configuration_error", return_value=None),
        patch.object(executor, "create_engine_from_env", return_value=engine),
        patch.object(executor, "read_blackbox_execution_approval", return_value=SimpleNamespace(executable=True)),
        patch.object(executor, "_verify_scheme_activation", return_value=(True, "")),
        patch.object(executor, "_active_registry_targets", return_value={("5Y", 1)}),
        patch.object(executor, "create_scheme_run", return_value=101),
        patch.object(executor, "run_configured_scheme", side_effect=interrupted),
        patch.object(executor, "fail_scheme_run_atomic") as settle,
    ):
        with pytest.raises(KeyboardInterrupt) as caught:
            executor.execute_scheme(cfg, "2026-11-06", prediction_phase="scheduled_live")

    assert caught.value is interrupted
    settle.assert_called_once()
    assert settle.call_args.args == (engine,)
    assert settle.call_args.kwargs["run_id"] == 101
    assert settle.call_args.kwargs["scheme_id"] == "demo"
    assert settle.call_args.kwargs["run_date"] == "2026-11-06"
    assert settle.call_args.kwargs["error_message"] == "cancelled test execution"
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("runtime_type", ["native_adapter", "blackbox_v2"])
def test_model_failure_is_recorded_as_failed_run_with_its_error(runtime_type):
    from scheduler import executor

    cfg = SimpleNamespace(
        scheme_id="demo", runtime_type=runtime_type, status="active",
        scheme_version="exact", execution_timeout_sec=10,
    )
    engine = Mock()
    with (
        patch.object(executor, "scheduled_live_execution_configuration_error", return_value=None),
        patch.object(executor, "create_engine_from_env", return_value=engine),
        patch.object(executor, "read_blackbox_execution_approval", return_value=SimpleNamespace(executable=True)),
        patch.object(executor, "_verify_scheme_activation", return_value=(True, "")),
        patch.object(executor, "_active_registry_targets", return_value={("5Y", 1)}),
        patch.object(executor, "create_scheme_run", return_value=101),
        patch.object(executor, "run_configured_scheme", side_effect=ValueError("missing raw observation")),
        patch.object(executor, "fail_scheme_run_atomic") as settle,
    ):
        result = executor.execute_scheme(cfg, "2026-11-06", prediction_phase="scheduled_live")

    assert (result.status, result.records_written, result.run_id) == ("failed", 0, 101)
    assert result.error_msg == "missing raw observation"
    settle.assert_called_once()
    assert settle.call_args.kwargs["error_message"] == "missing raw observation"
    engine.dispose.assert_called_once()


@pytest.fixture
def run_native_stdout(monkeypatch: pytest.MonkeyPatch):
    """以受控真实子进程输出模拟 Native IPC，不加载方案或数据库。"""
    import json
    import subprocess
    import sys

    from scheduler.executor import run_scheme_subprocess

    system_popen = subprocess.Popen

    def run(**overrides):
        row = {
            "scheme_id": "ipc_contract_trial", "target_tenor": "10Y",
            "horizon": 1, "predicted_direction": 1,
            "predict_date": "2026-11-06", "feature_date": "2026-11-05",
            "target_date": "2026-11-06",
            **overrides,
        }
        payload = json.dumps([row])

        def launch(command, **options):
            assert "scheduler.scheme_runner" in command
            return system_popen(
                [sys.executable, "-c",
                 "import sys; sys.stdout.write(sys.argv[1]); "
                 "sys.stderr.write('unrecognized-private-model-stderr')",
                 payload],
                **options,
            )

        monkeypatch.setattr(subprocess, "Popen", launch)
        return run_scheme_subprocess("ipc_contract_trial", "2026-11-06")

    return run


@pytest.mark.parametrize("field,value", [
    ("predicted_direction", value)
    for value in (0.9, -0.9, 1.9, True, False, 2, -2, float("nan"),
                  float("inf"), -float("inf"), None, "bad", "1.0")
] + [
    ("horizon", value)
    for value in (1.9, True, False, 0, -1, float("nan"), float("inf"),
                  -float("inf"), None, "bad", "1.0")
])
def test_native_ipc_rejects_invalid_direction_and_horizon(
    run_native_stdout, capsys, field: str, value: object,
) -> None:
    """畸形方向或身份数值不得被截断、补平或当作布尔整数接收。"""
    with pytest.raises(ValueError, match=field) as error:
        run_native_stdout(**{field: value})
    assert "unrecognized-private-model-stderr" not in str(error.value)
    assert "unrecognized-private-model-stderr" not in capsys.readouterr().err


@pytest.mark.parametrize("field,value,expected", [
    ("predicted_direction", value, expected)
    for value, expected in ((-1, -1), (0, 0), (1, 1), (1.0, 1),
                            ("-1", -1), ("0", 0), (" +1 ", 1), ("01", 1))
] + [
    ("horizon", value, expected)
    for value, expected in ((1, 1), (6, 6), (1.0, 1),
                            ("1", 1), ("06", 6), (" +6 ", 6))
])
def test_native_ipc_preserves_compatible_integer_representations(
    run_native_stdout, field: str, value: object, expected: int,
) -> None:
    """保留既有整数数字和数字字符串表示，不改变合法 Native 输出。"""
    record, = run_native_stdout(**{field: value})
    assert getattr(record, field) == expected
    assert type(getattr(record, field)) is int


def test_read_only_state_seed_logs_unreadable_state_and_returns_none(
    tmp_path, monkeypatch, caplog
) -> None:
    from scheduler import executor

    monkeypatch.setenv("BFL_RUNTIME_ROOT", str(tmp_path))
    cfg = SimpleNamespace(
        incremental_state=True, scheme_id="s1", scheme_version="v1"
    )
    state_file = tmp_path / "blackbox-state" / "s1" / "v1.state"
    state_file.parent.mkdir(parents=True)

    assert executor._read_only_state_seed(cfg, []) is None
    assert not caplog.records

    state_file.write_bytes(b"corrupt")
    with caplog.at_level("WARNING", logger=executor.logger.name):
        assert executor._read_only_state_seed(cfg, []) is None
    assert "unreadable state file" in caplog.text
