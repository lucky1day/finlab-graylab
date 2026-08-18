from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from shared.blackbox_v2.environment_manifest import (
    environment_manifest_path,
    runtime_environment_platform,
)


@pytest.mark.parametrize(
    ("system_name", "machine_name", "platform_name", "filename"),
    [
        ("Linux", "x86_64", "linux-64", "environment_manifest.json"),
        (
            "Darwin",
            "arm64",
            "osx-arm64",
            "environment_manifest.osx-arm64.json",
        ),
    ],
)
def test_supported_runtime_selects_matching_manifest(
    system_name: str,
    machine_name: str,
    platform_name: str,
    filename: str,
) -> None:
    project_root = Path(__file__).resolve().parents[1]

    selected_platform = runtime_environment_platform(
        system_name=system_name,
        machine_name=machine_name,
    )
    selected_path = environment_manifest_path(
        project_root,
        system_name=system_name,
        machine_name=machine_name,
    )
    manifest = json.loads(selected_path.read_text(encoding="utf-8"))

    assert selected_platform == platform_name
    assert selected_path.name == filename
    assert manifest["platform"] == platform_name


def test_unsupported_runtime_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="unsupported Blackbox"):
        runtime_environment_platform(
            system_name="Linux",
            machine_name="aarch64",
        )


def test_verification_cli_direct_script_entrypoint_is_available() -> None:
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(
                project_root
                / "scripts"
                / "verify_blackbox_v2_environment.py"
            ),
            "--help",
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--manifest" in completed.stdout
