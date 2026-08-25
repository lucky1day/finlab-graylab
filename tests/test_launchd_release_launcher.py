from __future__ import annotations

import os
import plistlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.run_launchd_release import (
    LaunchdReleaseError,
    load_release_environment,
    load_service_environment,
    main,
    prepare_exec_environment,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_ROOT = PROJECT_ROOT / "deploy" / "launchd"
PRODUCTION_CURRENT = "/Users/macstudio0/bond-factor-lab-production/current"
COMMIT = "a" * 40
APPLICATION_TEMPLATES = (
    "com.bond-factor-lab.backend.plist",
    "com.bond-factor-lab.data-bridge-refresh.plist",
    "com.bond-factor-lab.daily-predictions.plist",
    "com.bond-factor-lab.weekly-predictions.plist",
    "com.bond-factor-lab.monthly-predictions.plist",
    "com.bond-factor-lab.actuals.plist",
)


@pytest.fixture(autouse=True)
def _restore_release_directory_permissions(tmp_path: Path) -> Iterator[None]:
    """允许 pytest 清理由安全性测试创建的只读 release 目录。"""
    yield
    for root, _, _ in os.walk(tmp_path):
        Path(root).chmod(0o755)


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "deploy" / "releases" / COMMIT
    release.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config = runtime / "config"
    config.mkdir(mode=0o700)
    service_environment = config / "service.env"
    service_environment.write_text(
        "\n".join(
            (
                "BOND_ADMIN_TOKEN=local-admin-token",
                "BOND_DB_USER=bond_user",
                'BOND_DB_PASSWORD="database secret"',
                "BOND_DB_HOST=127.0.0.1",
                "BOND_DB_PORT=3306",
                "BOND_DB_NAME=bond_db",
                "BOND_DB_CHARSET=utf8mb4",
                "BOND_FACTOR_LAB_INSTANCE_NONCE='mac3-instance'",
                "DATABRIDGE_API_BASE_URL=https://example.invalid",
                "DATABRIDGE_API_USERNAME=bridge_user",
                "DATABRIDGE_API_PASSWORD='bridge$(literal)${HOME}`value`'",
                "",
            )
        ),
        encoding="utf-8",
    )
    service_environment.chmod(0o600)
    native = runtime / "cache" / "native" / COMMIT
    environment = release / ".bfl-release.env"
    environment.write_text(
        "\n".join(
            (
                f"BFL_RELEASE_COMMIT={COMMIT}",
                f'BFL_RUNTIME_ROOT="{runtime}"',
                f'NUMBA_CACHE_DIR="{native / "numba"}"',
                f'MPLCONFIGDIR="{native / "matplotlib"}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    environment.chmod(0o444)
    release.chmod(0o555)
    return release


def _rewrite_service_environment(
    release: Path,
    transform,
) -> Path:
    runtime = Path(load_release_environment(release)["BFL_RUNTIME_ROOT"])
    path = runtime / "config" / "service.env"
    original = path.read_text(encoding="utf-8")
    path.write_text(transform(original), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_launchd_templates_use_immutable_current_release() -> None:
    for name in APPLICATION_TEMPLATES:
        with (LAUNCHD_ROOT / name).open("rb") as handle:
            payload = plistlib.load(handle)

        assert payload["WorkingDirectory"] == PRODUCTION_CURRENT, name
        arguments = payload["ProgramArguments"]
        assert arguments[:5] == [
            "/usr/bin/python3",
            "-I",
            "scripts/run_launchd_release.py",
            "--",
            "/Users/macstudio0/miniconda3/bin/conda",
        ], name
        assert "BFL_RELEASE_COMMIT" not in payload["EnvironmentVariables"], name
        assert "BFL_RUNTIME_ROOT" not in payload["EnvironmentVariables"], name
        assert payload["StandardOutPath"].startswith(
            "/Users/macstudio0/bond-factor-lab-runtime/logs/"
        ), name
        assert payload["StandardErrorPath"].startswith(
            "/Users/macstudio0/bond-factor-lab-runtime/logs/"
        ), name


def test_monthly_template_uses_explicit_refresh_window_arguments() -> None:
    path = LAUNCHD_ROOT / "com.bond-factor-lab.monthly-predictions.plist"
    with path.open("rb") as handle:
        payload = plistlib.load(handle)

    environment = payload["EnvironmentVariables"]
    assert "DATABRIDGE_REFRESH_START" not in environment
    assert "DATABRIDGE_REFRESH_DEADLINE" not in environment
    arguments = payload["ProgramArguments"]
    assert arguments[-4:] == [
        "--refresh-start",
        "18:00",
        "--refresh-deadline",
        "18:55",
    ]


def test_loader_accepts_exact_installed_release_environment(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)

    values = load_release_environment(release)

    assert values["BFL_RELEASE_COMMIT"] == COMMIT
    assert values["BFL_RUNTIME_ROOT"] == str(tmp_path / "runtime")
    assert values["NUMBA_CACHE_DIR"].endswith(f"/{COMMIT}/numba")
    assert values["MPLCONFIGDIR"].endswith(f"/{COMMIT}/matplotlib")


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_service_environment_loads_required_values_without_shell_expansion(
    tmp_path: Path,
    mode: int,
) -> None:
    release = _release(tmp_path)
    runtime = load_release_environment(release)["BFL_RUNTIME_ROOT"]
    (Path(runtime) / "config" / "service.env").chmod(mode)

    values = load_service_environment(runtime)

    assert values["BOND_ADMIN_TOKEN"] == "local-admin-token"
    assert values["BOND_DB_PASSWORD"] == "database secret"
    assert values["BOND_FACTOR_LAB_INSTANCE_NONCE"] == "mac3-instance"
    assert values["DATABRIDGE_API_PASSWORD"] == (
        "bridge$(literal)${HOME}`value`"
    )


@pytest.mark.parametrize("mode", [0o000, 0o200, 0o640, 0o644])
def test_service_environment_rejects_non_private_file_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    release = _release(tmp_path)
    runtime = Path(load_release_environment(release)["BFL_RUNTIME_ROOT"])
    service_environment = runtime / "config" / "service.env"
    service_environment.chmod(mode)

    with pytest.raises(LaunchdReleaseError, match="private regular file"):
        load_service_environment(runtime)


def test_service_environment_rejects_insecure_or_linked_config(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    runtime = Path(load_release_environment(release)["BFL_RUNTIME_ROOT"])
    config = runtime / "config"

    config.chmod(0o755)
    with pytest.raises(LaunchdReleaseError, match="config directory is insecure"):
        load_service_environment(runtime)

    config.chmod(0o700)
    service_environment = config / "service.env"
    outside = tmp_path / "outside.env"
    service_environment.replace(outside)
    service_environment.symlink_to(outside)
    with pytest.raises(LaunchdReleaseError, match="private regular file"):
        load_service_environment(runtime)


@pytest.mark.parametrize(
    "invalid_line",
    ("export EXTRA=value", "INVALID KEY=value", "BROKEN", 'UNCLOSED="value'),
)
def test_service_environment_rejects_invalid_syntax(
    tmp_path: Path,
    invalid_line: str,
) -> None:
    release = _release(tmp_path)
    path = _rewrite_service_environment(
        release,
        lambda value: value + invalid_line + "\n",
    )

    with pytest.raises(LaunchdReleaseError, match="invalid syntax"):
        load_service_environment(path.parents[1])


def test_service_environment_rejects_duplicate_reserved_or_missing_keys(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    runtime = Path(load_release_environment(release)["BFL_RUNTIME_ROOT"])
    path = runtime / "config" / "service.env"
    original = path.read_text(encoding="utf-8")

    cases = (
        (original + "BOND_DB_NAME=other\n", "duplicate keys"),
        (original + "BFL_RELEASE_COMMIT=bad\n", "reserved keys"),
        (
            original + "BFL_DATABASE_ENV_FILE=/outside/runtime/database.env\n",
            "reserved keys",
        ),
        (
            original.replace("BOND_ADMIN_TOKEN=local-admin-token\n", ""),
            "missing required keys",
        ),
        (
            original.replace(
                "BOND_ADMIN_TOKEN=local-admin-token",
                "BOND_ADMIN_TOKEN='   '",
            ),
            "BOND_ADMIN_TOKEN",
        ),
    )
    for content, message in cases:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(LaunchdReleaseError, match=message):
            load_service_environment(runtime)

def test_loader_rejects_missing_or_untrusted_release_environment(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    environment = release / ".bfl-release.env"

    release.chmod(0o755)
    environment.chmod(0o644)
    release.chmod(0o555)
    with pytest.raises(LaunchdReleaseError, match="read-only regular file"):
        load_release_environment(release)

    release.chmod(0o755)
    environment.unlink()
    environment.symlink_to(tmp_path / "outside.env")
    release.chmod(0o555)
    with pytest.raises(LaunchdReleaseError, match="read-only regular file"):
        load_release_environment(release)


def test_loader_rejects_writable_release_root(tmp_path: Path) -> None:
    release = _release(tmp_path)
    release.chmod(0o755)

    with pytest.raises(LaunchdReleaseError, match="root identity is invalid"):
        load_release_environment(release)


def test_loader_rejects_release_identity_or_cache_drift(tmp_path: Path) -> None:
    release = _release(tmp_path)
    environment = release / ".bfl-release.env"
    original = environment.read_text(encoding="utf-8")

    for old, new, message in (
        (COMMIT, "b" * 40, "commit does not match"),
        ("/numba", "/other", "NUMBA_CACHE_DIR does not match"),
    ):
        release.chmod(0o755)
        environment.chmod(0o644)
        environment.write_text(original.replace(old, new, 1), encoding="utf-8")
        environment.chmod(0o444)
        release.chmod(0o555)
        with pytest.raises(LaunchdReleaseError, match=message):
            load_release_environment(release)


def test_loader_rejects_insecure_runtime_root(tmp_path: Path) -> None:
    release = _release(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.chmod(0o777)

    with pytest.raises(LaunchdReleaseError, match="runtime root is insecure"):
        load_release_environment(release)


def test_prepare_environment_merges_service_values_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    runtime = Path(load_release_environment(release)["BFL_RUNTIME_ROOT"])

    merged = prepare_exec_environment(
        release,
        {"BOND_DB_HOST": "127.0.0.1", "AMBIENT_SAFE": "kept"},
    )

    assert merged["BOND_DB_PASSWORD"] == "database secret"
    assert merged["BOND_ADMIN_TOKEN"] == "local-admin-token"
    assert merged["BFL_DATABASE_ENV_FILE"] == str(
        runtime / "config" / "service.env"
    )
    assert merged["AMBIENT_SAFE"] == "kept"

    with pytest.raises(LaunchdReleaseError, match="service key"):
        prepare_exec_environment(release, {"BOND_DB_HOST": "wrong-host"})

    with pytest.raises(LaunchdReleaseError, match="ambient environment conflicts"):
        prepare_exec_environment(
            release,
            {"BFL_DATABASE_ENV_FILE": "/outside/runtime/database.env"},
        )


def test_prepare_environment_keeps_global_morning_refresh_window(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    _rewrite_service_environment(
        release,
        lambda value: value
        + "DATABRIDGE_REFRESH_START=05:30\n"
        + "DATABRIDGE_REFRESH_DEADLINE=06:45\n",
    )

    merged = prepare_exec_environment(release, {})

    assert merged["DATABRIDGE_REFRESH_START"] == "05:30"
    assert merged["DATABRIDGE_REFRESH_DEADLINE"] == "06:45"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("BFL_RELEASE_COMMIT", "b" * 40, "ambient environment conflicts"),
        ("PYTHONPATH", "/outside/release", "ambient Python environment"),
        ("PYTHONHOME", "/outside/release", "ambient Python environment"),
    ],
)
def test_prepare_environment_rejects_ambient_control_overrides(
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    release = _release(tmp_path)

    with pytest.raises(LaunchdReleaseError, match=message):
        prepare_exec_environment(release, {name: value})


def test_main_executes_command_with_release_identity(tmp_path: Path) -> None:
    release = _release(tmp_path)
    captured: dict[str, object] = {}

    def fake_exec(
        executable: str,
        command: list[str],
        environment: dict[str, str],
    ) -> None:
        captured.update(
            executable=executable,
            command=command,
            environment=environment,
        )

    with (
        patch("scripts.run_launchd_release.Path.cwd", return_value=release),
        patch("scripts.run_launchd_release.os.execvpe", side_effect=fake_exec),
        patch.dict(os.environ, {}, clear=True),
    ):
        assert main(["--", "/usr/bin/true", "example"]) == 0

    assert captured["executable"] == "/usr/bin/true"
    assert captured["command"] == ["/usr/bin/true", "example"]
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["BFL_RELEASE_COMMIT"] == COMMIT
    assert environment["BOND_FACTOR_LAB_CONTROL_PLANE"] == "launchd_one_shot"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_main_rejects_relative_or_missing_command(tmp_path: Path) -> None:
    release = _release(tmp_path)
    with patch("scripts.run_launchd_release.Path.cwd", return_value=release):
        assert main(["--", "python"]) == 2
        assert main([]) == 2
