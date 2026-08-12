from __future__ import annotations

import json
import plistlib
from pathlib import Path

from scripts.audit_launchd_config_drift import (
    audit_installed_launchd,
    audit_plist_pair,
    main,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_ROOT = PROJECT_ROOT / "deploy" / "launchd"
LEGACY_VARIABLE = "BOND_DAILY_COORDINATOR_MODE"


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


def test_formal_launchd_templates_forbid_retired_coordinator_mode() -> None:
    templates = sorted(LAUNCHD_ROOT.glob("*.plist"))
    assert templates
    for template in templates:
        with template.open("rb") as handle:
            payload = plistlib.load(handle)
        assert LEGACY_VARIABLE not in payload.get("EnvironmentVariables", {}), template


def test_data_bridge_template_requires_controlled_producer_identity() -> None:
    path = LAUNCHD_ROOT / "com.bond-factor-lab.data-bridge-refresh.plist"
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["EnvironmentVariables"]["BFL_DATABRIDGE_PRODUCER"] == (
        "launchd-one-shot"
    )


def test_audit_reports_legacy_drift_without_exposing_environment_values(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.daily-predictions"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    _write_plist(template, _plist(label, environment={"SAFE_KEY": "template-value"}))
    _write_plist(
        installed,
        _plist(
            label,
            environment={
                "SAFE_KEY": "installed-secret-value",
                LEGACY_VARIABLE: "legacy",
            },
        ),
    )

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            _plist(
                label,
                environment={
                    "SAFE_KEY": "installed-secret-value",
                    LEGACY_VARIABLE: "legacy",
                },
            ),
            state="not running",
            installed_path=installed,
        ),
    )
    serialized = json.dumps(result, sort_keys=True)

    assert result["forbidden_variables_present"] == [LEGACY_VARIABLE]
    assert result["loaded_forbidden_variables_present"] == [LEGACY_VARIABLE]
    assert result["unexpected_drift_paths"] == [
        "EnvironmentVariables.BOND_DAILY_COORDINATOR_MODE",
        "EnvironmentVariables.SAFE_KEY",
    ]
    assert "installed-secret-value" not in serialized
    assert "template-value" not in serialized
    assert "=> legacy" not in serialized


def test_backend_allows_local_token_and_log_paths_but_not_legacy_variable(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.backend"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    _write_plist(
        template,
        _plist(
            label,
            environment={"BOND_ADMIN_TOKEN": "__SET_REAL_TOKEN__"},
        ),
    )
    _write_plist(
        installed,
        _plist(
            label,
            environment={},
            stdout="/tmp/backend.log",
            stderr="/tmp/backend.err",
        ),
    )

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            _plist(
                label,
                environment={},
                stdout="/tmp/backend.log",
                stderr="/tmp/backend.err",
            ),
            state="running",
            installed_path=installed,
        ),
    )

    assert result["unexpected_drift_paths"] == []
    assert result["approved_local_difference_paths"] == [
        "EnvironmentVariables.BOND_ADMIN_TOKEN",
        "StandardErrorPath",
        "StandardOutPath",
    ]
    assert result["loaded_state"] == "running"


def test_data_bridge_audit_fails_closed_when_producer_identity_is_missing(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.data-bridge-refresh"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    _write_plist(
        template,
        _plist(
            label,
            environment={"BFL_DATABRIDGE_PRODUCER": "launchd-one-shot"},
        ),
    )
    _write_plist(installed, _plist(label, environment={}))

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            _plist(label, environment={}),
            state="not running",
            installed_path=installed,
        ),
    )

    assert result["required_environment_missing"] == ["BFL_DATABRIDGE_PRODUCER"]
    assert "launchd-one-shot" not in json.dumps(result, sort_keys=True)
    assert result["ok"] is False


def test_audit_fails_closed_when_service_is_not_loaded(tmp_path: Path) -> None:
    label = "com.bond-factor-lab.daily-predictions"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(label, environment={})
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=None,
    )

    assert result["loaded"] is False
    assert result["ok"] is False


def test_data_bridge_requires_producer_identity_in_loaded_environment(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.data-bridge-refresh"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(
        label,
        environment={"BFL_DATABRIDGE_PRODUCER": "launchd-one-shot"},
    )
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            payload,
            state="not running",
            installed_path=installed,
            environment={
                "BFL_DATABRIDGE_PRODUCER": "wrong",
                "OTHER": "launchd-one-shot",
            },
        ),
    )

    assert result["loaded_required_environment_missing"] == [
        "BFL_DATABRIDGE_PRODUCER"
    ]
    assert "launchd-one-shot" not in json.dumps(result, sort_keys=True)
    assert result["ok"] is False


def test_ssh_tunnel_allows_only_declared_local_placeholders(tmp_path: Path) -> None:
    label = "com.bond-factor-lab.ssh-tunnel"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    template_payload = _plist(label, environment={})
    template_payload["ProgramArguments"] = [
        "/usr/bin/ssh",
        "-N",
        "-i",
        "/Users/macstudio0/.ssh/<TUNNEL_KEY>",
        "-R",
        "127.0.0.1:18100:127.0.0.1:8100",
        "<SSH_USER>@bond.finailab.cn",
    ]
    installed_payload = dict(template_payload)
    installed_payload["ProgramArguments"] = [
        "/usr/bin/ssh",
        "-N",
        "-i",
        "/Users/macstudio0/.ssh/real-key",
        "-R",
        "127.0.0.1:18100:127.0.0.1:8100",
        "real-user@bond.finailab.cn",
    ]
    installed_payload["StandardOutPath"] = "/tmp/ssh-tunnel.log"
    installed_payload["StandardErrorPath"] = "/tmp/ssh-tunnel.err"
    _write_plist(template, template_payload)
    _write_plist(installed, installed_payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            installed_payload,
            state="running",
            installed_path=installed,
        ),
    )

    assert result["unexpected_drift_paths"] == []
    assert result["approved_local_difference_paths"] == [
        "ProgramArguments[3]",
        "ProgramArguments[6]",
        "StandardErrorPath",
        "StandardOutPath",
    ]
    assert result["ok"] is True

    installed_payload["ProgramArguments"][5] = "0.0.0.0:18100:127.0.0.1:8100"
    _write_plist(installed, installed_payload)
    changed = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            installed_payload,
            state="running",
            installed_path=installed,
        ),
    )

    assert changed["unexpected_drift_paths"] == ["ProgramArguments[5]"]
    assert changed["ok"] is False


def test_ssh_tunnel_does_not_approve_fixed_indexes_without_placeholders(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.ssh-tunnel"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    template_payload = _plist(label, environment={})
    template_payload["ProgramArguments"] = [f"fixed-{index}" for index in range(18)]
    installed_payload = dict(template_payload)
    installed_payload["ProgramArguments"] = list(template_payload["ProgramArguments"])
    installed_payload["ProgramArguments"][14] = "changed-sensitive-option"
    _write_plist(template, template_payload)
    _write_plist(installed, installed_payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            installed_payload,
            state="running",
            installed_path=installed,
        ),
    )

    assert result["unexpected_drift_paths"] == ["ProgramArguments[14]"]
    assert result["ok"] is False


def test_empty_template_directory_fails_closed(
    tmp_path: Path,
    capsys,
) -> None:
    report = audit_installed_launchd(
        project_root=tmp_path,
        installed_root=tmp_path,
        domain="gui/0",
    )

    assert report["services"] == []
    assert report["error"] == "launchd_templates_missing"
    assert report["ok"] is False
    assert main(
        [
            "--project-root",
            str(tmp_path),
            "--installed-root",
            str(tmp_path),
            "--domain",
            "gui/0",
        ]
    ) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


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


def test_loaded_calendar_interval_order_is_not_drift(tmp_path: Path) -> None:
    label = "com.bond-factor-lab.actuals"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(label, environment={})
    payload["StartCalendarInterval"] = [
        {"Hour": 8, "Minute": 30},
        {"Hour": 19, "Minute": 0},
        {"Hour": 23, "Minute": 45},
    ]
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            payload,
            state="not running",
            installed_path=installed,
            trigger=list(reversed(payload["StartCalendarInterval"])),
        ),
    )

    assert result["loaded_configuration_mismatch_fields"] == []
    assert result["ok"] is True


def test_loaded_forbidden_name_in_unrelated_value_is_not_a_false_positive(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.daily-predictions"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(label, environment={"SAFE_KEY": LEGACY_VARIABLE})
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            payload,
            state="not running",
            installed_path=installed,
        ),
    )

    assert result["loaded_forbidden_variables_present"] == []
    assert result["ok"] is True


def test_loaded_environment_rejects_non_launchd_extra_key(tmp_path: Path) -> None:
    label = "com.bond-factor-lab.daily-predictions"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(label, environment={"SAFE_KEY": "same-value"})
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            payload,
            state="not running",
            installed_path=installed,
            environment={
                "SAFE_KEY": "same-value",
                "UNEXPECTED_EXTRA_KEY": "stale-value",
            },
        ),
    )

    assert result["loaded_configuration_mismatch_fields"] == [
        "EnvironmentVariables"
    ]
    assert "UNEXPECTED_EXTRA_KEY" not in json.dumps(result, sort_keys=True)
    assert "stale-value" not in json.dumps(result, sort_keys=True)
    assert result["ok"] is False


def test_loaded_environment_allows_only_known_launchd_injected_keys(
    tmp_path: Path,
) -> None:
    label = "com.bond-factor-lab.daily-predictions"
    template = tmp_path / "template.plist"
    installed = tmp_path / "installed.plist"
    payload = _plist(label, environment={"SAFE_KEY": "same-value"})
    _write_plist(template, payload)
    _write_plist(installed, payload)

    result = audit_plist_pair(
        template_path=template,
        installed_path=installed,
        launchctl_output=_launchctl(
            payload,
            state="not running",
            installed_path=installed,
            environment={
                "SAFE_KEY": "same-value",
                "OSLogRateLimit": "64",
                "XPC_SERVICE_NAME": label,
            },
        ),
    )

    assert result["loaded_configuration_mismatch_fields"] == []
    assert result["ok"] is True
