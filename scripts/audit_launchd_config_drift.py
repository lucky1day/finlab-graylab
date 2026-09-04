#!/usr/bin/env python
"""只读审计仓库 launchd 模板与本机 installed plist 的语义漂移。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Sequence

if __package__:
    from scripts.run_launchd_release import (
        LaunchdReleaseError,
        load_service_environment,
    )
else:
    from run_launchd_release import (  # type: ignore[no-redef]
        LaunchdReleaseError,
        load_service_environment,
    )


FORBIDDEN_ENVIRONMENT_VARIABLES = frozenset({"BOND_DAILY_COORDINATOR_MODE"})
LAUNCHD_INJECTED_ENVIRONMENT_VARIABLES = frozenset(
    {"OSLogRateLimit", "XPC_SERVICE_NAME"}
)
DATA_BRIDGE_LABEL = "com.bond-factor-lab.data-bridge-refresh"
DATA_BRIDGE_PRODUCER = ("BFL_DATABRIDGE_PRODUCER", "launchd-one-shot")
BACKEND_LABEL = "com.bond-factor-lab.backend"
APPROVED_LOCAL_DIFFERENCES: dict[str, frozenset[str]] = {}
DEFAULT_RUNTIME_ROOT = Path("/Users/macstudio0/bond-factor-lab-runtime")

SSH_TUNNEL_LOCAL_ARGUMENT_PLACEHOLDERS = {
    "/Users/macstudio0/.ssh/<TUNNEL_KEY>",
    "<SSH_USER>@101.132.143.185",
}
SSH_TUNNEL_KEY_PLACEHOLDER = "/Users/macstudio0/.ssh/<TUNNEL_KEY>"
SSH_TUNNEL_REMOTE = re.compile(r"^[^@<>\s]+@101\.132\.143\.185$")
ALWAYS_RUNNING_LABELS = frozenset(
    {BACKEND_LABEL, "com.bond-factor-lab.ssh-tunnel"}
)


def _valid_ssh_local_argument(template_value: object, value: object) -> bool:
    candidate = str(value).strip()
    if template_value != SSH_TUNNEL_KEY_PLACEHOLDER:
        return SSH_TUNNEL_REMOTE.fullmatch(candidate) is not None
    key_path = Path(candidate)
    if not key_path.is_absolute() or key_path.is_symlink():
        return False
    try:
        metadata = key_path.stat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) in {0o400, 0o600}
    )


def _load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"plist root must be a dictionary: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _value_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _different_paths(left: object, right: object, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "<root>"]
    if isinstance(left, dict):
        result: list[str] = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                result.append(path)
            else:
                result.extend(_different_paths(left[key], right[key], path))
        return result
    if isinstance(left, list):
        if len(left) != len(right):
            return [prefix]
        result = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            result.extend(
                _different_paths(left_item, right_item, f"{prefix}[{index}]")
            )
        return result
    return [] if left == right else [prefix]


def _loaded_state(launchctl_output: str) -> str | None:
    match = re.search(r"^\s*state = (.+)$", launchctl_output, re.MULTILINE)
    return match.group(1).strip() if match else None


def _loaded_block(launchctl_output: str, name: str) -> list[str] | None:
    lines = launchctl_output.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("\t")
            and not line.startswith("\t\t")
            and line.strip() == f"{name} = {{"
        ),
        None,
    )
    if start is None:
        return None
    result: list[str] = []
    for line in lines[start + 1 :]:
        if line == "\t}":
            return result
        result.append(line)
    return None


def _loaded_arguments(launchctl_output: str) -> list[str] | None:
    block = _loaded_block(launchctl_output, "arguments")
    if block is None:
        return None
    return [line.strip() for line in block if line.startswith("\t\t")]


def _loaded_environment(launchctl_output: str) -> dict[str, str] | None:
    block = _loaded_block(launchctl_output, "environment")
    if block is None:
        return None
    result: dict[str, str] = {}
    for line in block:
        stripped = line.strip()
        if " => " not in stripped:
            continue
        key, value = stripped.split(" => ", 1)
        result[key] = value
    return result


def _loaded_calendar_intervals(
    launchctl_output: str,
) -> list[dict[str, int]] | None:
    block = _loaded_block(launchctl_output, "event triggers")
    if block is None:
        return None
    result: list[dict[str, int]] = []
    current: dict[str, int] | None = None
    in_descriptor = False
    for line in block:
        stripped = line.strip()
        if stripped == "descriptor = {":
            current = {}
            in_descriptor = True
            continue
        if in_descriptor and stripped == "}":
            if current is not None:
                result.append(current)
            current = None
            in_descriptor = False
            continue
        if in_descriptor and current is not None and " => " in stripped:
            key, value = stripped.split(" => ", 1)
            key = key.strip('"')
            try:
                current[key] = int(value)
            except ValueError:
                return None
    return result or None


def _normalized_intervals(value: object) -> list[dict[str, int]] | None:
    if isinstance(value, dict):
        intervals = [value]
    elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
        intervals = value
    else:
        return None
    return sorted(
        ({str(key): int(item[key]) for key in sorted(item)} for item in intervals),
        key=lambda item: json.dumps(item, sort_keys=True),
    )


def audit_plist_pair(
    *,
    template_path: Path,
    installed_path: Path,
    launchctl_output: str | None,
) -> dict[str, object]:
    """返回脱敏语义差异；绝不返回 plist 中的环境变量值。"""

    template = _load_plist(template_path)
    installed = _load_plist(installed_path)
    label = str(template.get("Label") or installed.get("Label") or "")
    approved = set(APPROVED_LOCAL_DIFFERENCES.get(label, frozenset()))
    all_drift = _different_paths(template, installed)
    if label == "com.bond-factor-lab.ssh-tunnel":
        template_arguments = template.get("ProgramArguments") or []
        for index, value in enumerate(template_arguments):
            if value in SSH_TUNNEL_LOCAL_ARGUMENT_PLACEHOLDERS:
                approved.add(f"ProgramArguments[{index}]")
    approved_drift = sorted(path for path in all_drift if path in approved)
    unexpected_drift = sorted(path for path in all_drift if path not in approved)
    installed_environment = installed.get("EnvironmentVariables") or {}
    if not isinstance(installed_environment, dict):
        installed_environment = {}
    forbidden = sorted(
        FORBIDDEN_ENVIRONMENT_VARIABLES.intersection(installed_environment)
    )
    loaded_text = launchctl_output or ""
    loaded_environment = _loaded_environment(loaded_text) if launchctl_output else None
    loaded_forbidden = sorted(
        name
        for name in FORBIDDEN_ENVIRONMENT_VARIABLES
        if loaded_environment is not None and name in loaded_environment
    )
    required_missing: list[str] = []
    loaded_required_missing: list[str] = []
    if label == DATA_BRIDGE_LABEL:
        name, expected_value = DATA_BRIDGE_PRODUCER
        if installed_environment.get(name) != expected_value:
            required_missing.append(name)
        if loaded_environment is None or loaded_environment.get(name) != expected_value:
            loaded_required_missing.append(name)
    required_local_missing: list[str] = []
    loaded_required_local_missing: list[str] = []
    if label == "com.bond-factor-lab.ssh-tunnel":
        template_arguments = template.get("ProgramArguments") or []
        installed_arguments = installed.get("ProgramArguments") or []
        loaded_arguments = _loaded_arguments(loaded_text) if launchctl_output else None
        for index, value in enumerate(template_arguments):
            if value not in SSH_TUNNEL_LOCAL_ARGUMENT_PLACEHOLDERS:
                continue
            path = f"ProgramArguments[{index}]"
            if (
                index >= len(installed_arguments)
                or not _valid_ssh_local_argument(
                    value,
                    installed_arguments[index],
                )
            ):
                required_local_missing.append(path)
            if (
                loaded_arguments is None
                or index >= len(loaded_arguments)
                or not _valid_ssh_local_argument(
                    value,
                    loaded_arguments[index],
                )
            ):
                loaded_required_local_missing.append(path)
    loaded = launchctl_output is not None
    loaded_mismatch: list[str] = []
    if loaded:
        path_match = re.search(r"^\s*path = (.+)$", loaded_text, re.MULTILINE)
        if (
            path_match is None
            or Path(path_match.group(1).strip()).resolve() != installed_path.resolve()
        ):
            loaded_mismatch.append("Path")
        if label in ALWAYS_RUNNING_LABELS and _loaded_state(loaded_text) != "running":
            loaded_mismatch.append("State")
        if _loaded_arguments(loaded_text) != installed.get("ProgramArguments"):
            loaded_mismatch.append("ProgramArguments")
        working_match = re.search(
            r"^\s*working directory = (.+)$", loaded_text, re.MULTILINE
        )
        if (
            working_match is None
            or working_match.group(1).strip() != installed.get("WorkingDirectory")
        ):
            loaded_mismatch.append("WorkingDirectory")
        installed_trigger = installed.get("StartCalendarInterval")
        if installed_trigger is not None:
            loaded_intervals = _loaded_calendar_intervals(loaded_text)
            if (
                loaded_intervals is None
                or _normalized_intervals(loaded_intervals)
                != _normalized_intervals(installed_trigger)
            ):
                loaded_mismatch.append("StartCalendarInterval")
        for field, launchd_name in (
            ("StandardOutPath", "stdout path"),
            ("StandardErrorPath", "stderr path"),
        ):
            path_match = re.search(
                rf"^\s*{re.escape(launchd_name)} = (.+)$",
                loaded_text,
                re.MULTILINE,
            )
            if (
                path_match is None
                or path_match.group(1).strip() != installed.get(field)
            ):
                loaded_mismatch.append(field)
        loaded_explicit_environment = (
            None
            if loaded_environment is None
            else {
                key: value
                for key, value in loaded_environment.items()
                if key not in LAUNCHD_INJECTED_ENVIRONMENT_VARIABLES
                or key in installed_environment
            }
        )
        expected_environment = {
            str(key): str(value) for key, value in installed_environment.items()
        }
        if loaded_explicit_environment != expected_environment:
            loaded_mismatch.append("EnvironmentVariables")
    result: dict[str, object] = {
        "label": label,
        "template_path": str(template_path),
        "installed_path": str(installed_path),
        "template_sha256": _sha256(template_path),
        "installed_sha256": _sha256(installed_path),
        "program_arguments_sha256": _value_hash(installed.get("ProgramArguments")),
        "working_directory_sha256": _value_hash(installed.get("WorkingDirectory")),
        "calendar_trigger_sha256": _value_hash(
            installed.get("StartCalendarInterval")
        ),
        "environment_variable_names": sorted(str(key) for key in installed_environment),
        "forbidden_variables_present": forbidden,
        "loaded": loaded,
        "loaded_state": _loaded_state(loaded_text),
        "loaded_forbidden_variables_present": loaded_forbidden,
        "required_environment_missing": required_missing,
        "loaded_required_environment_missing": loaded_required_missing,
        "required_local_configuration_missing": required_local_missing,
        "loaded_required_local_configuration_missing": (
            loaded_required_local_missing
        ),
        "loaded_configuration_mismatch_fields": sorted(loaded_mismatch),
        "approved_local_difference_paths": approved_drift,
        "unexpected_drift_paths": unexpected_drift,
    }
    result["ok"] = not (
        not loaded
        or forbidden
        or loaded_forbidden
        or required_missing
        or loaded_required_missing
        or required_local_missing
        or loaded_required_local_missing
        or loaded_mismatch
        or unexpected_drift
    )
    return result


def audit_service_environment(runtime_root: Path) -> dict[str, object]:
    """只报告外置生产配置的变量名和校验类别，不返回变量值。"""
    path = runtime_root / "config" / "service.env"
    try:
        values = load_service_environment(runtime_root)
    except LaunchdReleaseError as exc:
        return {
            "path": str(path),
            "environment_variable_names": [],
            "ok": False,
            "error": str(exc),
        }
    return {
        "path": str(path),
        "environment_variable_names": sorted(values),
        "ok": True,
    }


def _launchctl_print(label: str, *, domain: str) -> str | None:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"{domain}/{label}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def audit_installed_launchd(
    *,
    project_root: Path,
    installed_root: Path,
    domain: str,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> dict[str, object]:
    """扫描正式仓库模板；缺失或无效 installed plist 均 fail-closed。"""

    template_root = project_root / "deploy" / "launchd"
    results: list[dict[str, object]] = []
    template_paths = sorted(template_root.glob("*.plist"))
    for template_path in template_paths:
        template = _load_plist(template_path)
        label = str(template.get("Label") or "")
        installed_path = installed_root / f"{label}.plist"
        if not installed_path.is_file():
            results.append(
                {
                    "label": label,
                    "template_path": str(template_path),
                    "installed_path": str(installed_path),
                    "loaded": _launchctl_print(label, domain=domain) is not None,
                    "ok": False,
                    "error": "installed_plist_missing",
                }
            )
            continue
        try:
            results.append(
                audit_plist_pair(
                    template_path=template_path,
                    installed_path=installed_path,
                    launchctl_output=_launchctl_print(label, domain=domain),
                )
            )
        except (OSError, ValueError, plistlib.InvalidFileException):
            results.append(
                {
                    "label": label,
                    "template_path": str(template_path),
                    "installed_path": str(installed_path),
                    "ok": False,
                    "error": "installed_plist_unreadable",
                }
            )
    service_environment = audit_service_environment(runtime_root)
    report: dict[str, object] = {
        "schema_version": "bfl-launchd-config-drift-v1",
        "read_only": True,
        "project_root": str(project_root),
        "installed_root": str(installed_root),
        "runtime_root": str(runtime_root),
        "domain": domain,
        "ok": bool(template_paths)
        and bool(service_environment["ok"])
        and all(bool(item.get("ok")) for item in results),
        "service_environment": service_environment,
        "services": results,
    }
    if not template_paths:
        report["error"] = "launchd_templates_missing"
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--installed-root",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents",
    )
    parser.add_argument("--domain", default=f"gui/{os.getuid()}")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = audit_installed_launchd(
        project_root=args.project_root.resolve(),
        installed_root=args.installed_root.resolve(),
        domain=str(args.domain),
        runtime_root=args.runtime_root,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
