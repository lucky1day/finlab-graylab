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
    main,
    prepare_exec_environment,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_ROOT = PROJECT_ROOT / "deploy" / "launchd"
PRODUCTION_CURRENT = "/Users/macstudio0/bond-factor-lab-production/current"
GIT_WORKTREE = "/Users/macstudio0/bond-factor-lab"
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


def test_no_launchd_template_depends_on_git_worktree() -> None:
    log_paths: set[str] = set()
    for path in LAUNCHD_ROOT.glob("*.plist"):
        text = path.read_text(encoding="utf-8")
        assert f"{GIT_WORKTREE}/" not in text, path.name
        assert f">{GIT_WORKTREE}<" not in text, path.name
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        label = payload["Label"]
        assert payload["StandardOutPath"] == (
            f"/Users/macstudio0/bond-factor-lab-runtime/logs/{label}.log"
        )
        assert payload["StandardErrorPath"] == (
            f"/Users/macstudio0/bond-factor-lab-runtime/logs/{label}.err"
        )
        log_paths.update(
            (payload["StandardOutPath"], payload["StandardErrorPath"])
        )

    assert len(log_paths) == 2 * len(tuple(LAUNCHD_ROOT.glob("*.plist")))

    tunnel = LAUNCHD_ROOT / "com.bond-factor-lab.ssh-tunnel.plist"
    with tunnel.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["WorkingDirectory"] == "/Users/macstudio0"


def test_loader_accepts_exact_installed_release_environment(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)

    values = load_release_environment(release)

    assert values["BFL_RELEASE_COMMIT"] == COMMIT
    assert values["BFL_RUNTIME_ROOT"] == str(tmp_path / "runtime")
    assert values["NUMBA_CACHE_DIR"].endswith(f"/{COMMIT}/numba")
    assert values["MPLCONFIGDIR"].endswith(f"/{COMMIT}/matplotlib")


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


def test_prepare_environment_rejects_ambient_release_override(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)

    with pytest.raises(LaunchdReleaseError, match="ambient environment conflicts"):
        prepare_exec_environment(
            release,
            {"BFL_RELEASE_COMMIT": "b" * 40},
        )


@pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONHOME"])
def test_prepare_environment_rejects_ambient_python_path_override(
    tmp_path: Path,
    name: str,
) -> None:
    release = _release(tmp_path)

    with pytest.raises(LaunchdReleaseError, match="ambient Python environment"):
        prepare_exec_environment(release, {name: "/outside/release"})


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
