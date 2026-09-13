from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

import pytest

from scripts.audit_launchd_config_drift import (
    audit_plist_pair,
)


def _write_plist(path: Path, payload: dict[str, object]) -> None:
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def _plist(
    label: str,
    *,
    environment: dict[str, str],
    stdout: str = "/repo/logs/stdout.log",
    stderr: str = "/repo/logs/stderr.log",
) -> dict[str, object]:
    return {
        "Label": label,
        "ProgramArguments": ["/usr/bin/python3", "runner.py", "--publish"],
        "WorkingDirectory": "/repo",
        "EnvironmentVariables": environment,
        "StartCalendarInterval": {"Hour": 6, "Minute": 30},
        "StandardOutPath": stdout,
        "StandardErrorPath": stderr,
    }


def _launchctl(
    payload: dict[str, object],
    *,
    state: str,
    installed_path: Path | None = None,
    environment: dict[str, str] | None = None,
    arguments: list[str] | None = None,
    working_directory: str | None = None,
    trigger: dict[str, int] | list[dict[str, int]] | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> str:
    loaded_environment = environment
    if loaded_environment is None:
        loaded_environment = dict(payload.get("EnvironmentVariables") or {})
    loaded_arguments = arguments
    if loaded_arguments is None:
        loaded_arguments = list(payload.get("ProgramArguments") or [])
    loaded_trigger = trigger
    if loaded_trigger is None:
        loaded_trigger = payload.get("StartCalendarInterval")  # type: ignore[assignment]
    triggers = loaded_trigger if isinstance(loaded_trigger, list) else [loaded_trigger]
    lines = [
        "gui/501/example = {",
        f"\tpath = {installed_path or '/tmp/installed.plist'}",
        f"\tstate = {state}",
        "\targuments = {",
        *(f"\t\t{value}" for value in loaded_arguments),
        "\t}",
        f"\tworking directory = {working_directory or payload['WorkingDirectory']}",
        f"\tstdout path = {stdout or payload['StandardOutPath']}",
        f"\tstderr path = {stderr or payload['StandardErrorPath']}",
        "\tenvironment = {",
        *(f"\t\t{key} => {value}" for key, value in loaded_environment.items()),
        "\t}",
    ]
    if all(item is not None for item in triggers):
        lines.extend(["\tevent triggers = {"])
        for index, item in enumerate(triggers):
            assert item is not None
            lines.extend(
                [
                    f"\t\texample-{index} => {{",
                    "\t\t\tdescriptor = {",
                    *(f'\t\t\t\t"{key}" => {value}' for key, value in item.items()),
                    "\t\t\t}",
                    "\t\t}",
                ]
            )
        lines.extend(["\t}"])
    lines.append("}")
    return "\n".join(lines) + "\n"


def _audit_payloads(
    tmp_path: Path,
    template_payload: dict[str, object],
    installed_payload: dict[str, object],
    *,
    state: str = "not running",
    **loaded_overrides: Any,
) -> dict[str, object]:
    """写入一对临时 plist，并返回其脱敏审计结果。"""
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    _write_plist(template, template_payload)
    _write_plist(installed, installed_payload)
    return audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            installed_payload,
            state=state,
            installed_path=installed,
            **loaded_overrides,
        ),
    )


@pytest.mark.parametrize(
    ("key_path", "destination"),
    (
        ("/Users/macstudio0/.ssh/<TUNNEL_KEY>", "<SSH_USER>@101.132.143.185"),
        (
            "  /Users/macstudio0/.ssh/<TUNNEL_KEY>  ",
            "  <SSH_USER>@101.132.143.185  ",
        ),
        ("/does/not/exist", "not-a-host"),
    ),
)
def test_ssh_tunnel_requires_real_local_key_and_user(
    tmp_path: Path,
    key_path: str,
    destination: str,
) -> None:
    label = "com.bond-factor-lab.ssh-tunnel"
    template_payload = _plist(label, environment={})
    template_payload["ProgramArguments"] = [
        "/usr/bin/ssh",
        "-N",
        "-i",
        "/Users/macstudio0/.ssh/<TUNNEL_KEY>",
        "-R",
        "127.0.0.1:18100:127.0.0.1:8100",
        "<SSH_USER>@101.132.143.185",
    ]
    installed_payload = dict(template_payload)
    installed_payload["ProgramArguments"] = list(
        template_payload["ProgramArguments"]
    )
    installed_payload["ProgramArguments"][3] = key_path
    installed_payload["ProgramArguments"][6] = destination

    result = _audit_payloads(
        tmp_path,
        template_payload,
        installed_payload,
        state="running",
    )

    assert result["required_local_configuration_missing"] == [
        "ProgramArguments[3]",
        "ProgramArguments[6]",
    ]
    assert result["loaded_required_local_configuration_missing"] == [
        "ProgramArguments[3]",
        "ProgramArguments[6]",
    ]
    assert result["ok"] is False


def test_loaded_program_workdir_and_trigger_must_match_installed_plist(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.daily-predictions"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(label, environment={})
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            payload,
            state="not running",
            installed_path=installed,
            arguments=["/usr/bin/python3", "old-runner.py"],
            working_directory="/old-repo",
            trigger={"Hour": 7, "Minute": 4},
            stdout="/old/stdout.log",
            stderr="/old/stderr.log",
        ),
    )

    assert result["loaded_configuration_mismatch_fields"] == [
        "ProgramArguments",
        "StandardErrorPath",
        "StandardOutPath",
        "StartCalendarInterval",
        "WorkingDirectory",
    ]
    assert result["ok"] is False


def test_loaded_job_must_come_from_audited_installed_plist(tmp_path: Path) -> None:
    label = "com.bond-factor-lab.daily-predictions"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(label, environment={})
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            payload,
            state="not running",
            installed_path=tmp_path / "different.plist",
        ),
    )

    assert result["loaded_configuration_mismatch_fields"] == ["Path"]
    assert result["ok"] is False
