from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from shared.blackbox_v2.contracts import BlackboxMetadata, load_metadata
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES


SCHEDULES = {
    "daily": "3 7 * * 1-5",
    "weekly": "30 11 * * 6",
    "monthly": "0 18 15 * *",
}
PERIOD_AVERAGE_SCHEDULE = "0 18 * * 1-5"
RUNTIME_PROFILE = "blackbox-v2-v1"
DATA_SCHEMA_VERSION = "data-bridge-v1"
FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "ftplib",
    "http",
    "mysql",
    "pymysql",
    "psycopg",
    "requests",
    "socket",
    "sqlalchemy",
    "sqlite3",
    "subprocess",
    "urllib",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_QUALIFIED_CALLS = {"os.system", "os.popen", "os.spawnl", "os.spawnv"}
ABSOLUTE_PATH_PATTERN = re.compile(r"^(?:/Users/|/home/|[A-Za-z]:[\\/])")
RELATIVE_TRAVERSAL_PATTERN = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
SCRIPT_VALIDATOR_POLICY_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def intake_delivery(
    delivery_dir: str | Path,
    *,
    schemes_root: str | Path,
    incremental_state: bool = False,
) -> Path:
    """原子保留两文件交付并生成平台配置，可显式启用私有增量状态。"""
    if type(incremental_state) is not bool:
        raise ValueError("incremental_state intake option must be a boolean")
    metadata, script, metadata_file = validate_delivery(delivery_dir)

    destination_root = Path(schemes_root).resolve()
    destination = destination_root / metadata.scheme_id
    if destination.exists():
        raise FileExistsError(f"scheme already exists: {metadata.scheme_id}")
    destination_root.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{metadata.scheme_id}.",
            dir=destination_root,
        )
    )
    try:
        staged_delivery = staging / "delivery"
        staged_delivery.mkdir()
        shutil.copyfile(script, staged_delivery / script.name)
        shutil.copyfile(
            metadata_file,
            staged_delivery / metadata_file.name,
        )
        (staging / "config.yaml").write_text(
            _config_text(metadata, incremental_state=incremental_state),
            encoding="utf-8",
        )
        for path in staged_delivery.iterdir():
            path.chmod(0o444)
        os.replace(staging, destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_delivery(
    delivery_dir: str | Path,
    *,
    expected_scheme_id: str | None = None,
) -> tuple[BlackboxMetadata, Path, Path]:
    """校验 Blackbox canonical 两文件交付，不产生任何副作用。"""
    source = Path(delivery_dir).resolve()
    if not source.is_dir():
        raise ValueError(f"delivery directory does not exist: {source}")
    entries = sorted(source.iterdir())
    if len(entries) != 2 or any(not item.is_file() or item.is_symlink() for item in entries):
        raise ValueError("Blackbox V2 delivery must contain exactly two regular files")
    scripts = [item for item in entries if item.suffix == ".py"]
    metadata_files = [item for item in entries if item.suffix == ".json"]
    if len(scripts) != 1 or len(metadata_files) != 1:
        raise ValueError("Blackbox V2 delivery must contain one .py and one .json")
    metadata = load_metadata(metadata_files[0])
    if expected_scheme_id is not None and metadata.scheme_id != expected_scheme_id:
        raise ValueError(
            "delivery scheme_id does not match canonical scheme: "
            f"expected={expected_scheme_id}, actual={metadata.scheme_id}"
        )
    if metadata.description is None:
        raise ValueError("description is required for a new Blackbox V2 Intake")
    if metadata.owner is None:
        raise ValueError("owner is required for a new Blackbox V2 Intake")
    expected_names = {f"{metadata.scheme_id}.py", f"{metadata.scheme_id}.json"}
    if {item.name for item in entries} != expected_names:
        raise ValueError("delivery filenames must match metadata scheme_id")
    validate_delivery_script(scripts[0])
    return metadata, scripts[0], metadata_files[0]


def validate_delivery_script(script_path: str | Path) -> None:
    """校验一份已定位的 Blackbox 交付脚本。"""
    script = Path(script_path)
    if not script.is_file() or script.is_symlink() or script.suffix != ".py":
        raise ValueError("Blackbox V2 delivery script must be one regular .py file")
    try:
        tree = ast.parse(
            script.read_text(encoding="utf-8"),
            filename=str(script),
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f"delivery script syntax error: {exc}") from exc
    violations = _script_violations(tree)
    if violations:
        raise ValueError("unsafe Blackbox V2 delivery script: " + "; ".join(violations))


def validate_canonical_layout(
    scheme_dir: str | Path,
) -> tuple[Path, Path, Path]:
    """校验 canonical 仅包含配置及独立两文件交付目录。"""
    scheme = Path(scheme_dir)
    if not scheme.is_dir() or scheme.is_symlink():
        raise ValueError(f"Blackbox V2 scheme must be a regular directory: {scheme}")
    entries = sorted(scheme.iterdir())
    if {item.name for item in entries} != {"config.yaml", "delivery"}:
        raise ValueError(
            "Blackbox V2 scheme must contain exactly config.yaml and delivery"
        )
    config_path = scheme / "config.yaml"
    delivery_dir = scheme / "delivery"
    if not config_path.is_file() or config_path.is_symlink():
        raise ValueError("Blackbox V2 config.yaml must be a regular file")
    if not delivery_dir.is_dir() or delivery_dir.is_symlink():
        raise ValueError("Blackbox V2 delivery must be a regular directory")
    return scheme.resolve(), config_path.resolve(), delivery_dir.resolve()


def _script_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"line {node.lineno}: forbidden import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                violations.append(f"line {node.lineno}: forbidden import {node.module}")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_CALLS or name in FORBIDDEN_QUALIFIED_CALLS:
                violations.append(f"line {node.lineno}: forbidden call {name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value.strip()
            if ABSOLUTE_PATH_PATTERN.match(literal):
                violations.append(f"line {node.lineno}: hard-coded absolute path is forbidden")
            if RELATIVE_TRAVERSAL_PATTERN.search(literal):
                violations.append(
                    f"line {node.lineno}: relative path traversal literal is forbidden"
                )
    return sorted(set(violations))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _config_text(
    metadata: BlackboxMetadata,
    *,
    incremental_state: bool = False,
) -> str:
    cron = (
        PERIOD_AVERAGE_SCHEDULE
        if metadata.task_type in PERIOD_AVERAGE_TASK_TYPES
        else SCHEDULES[metadata.frequency]
    )
    state_config = "incremental_state: true\n" if incremental_state else ""
    return (
        f"scheme_id: {metadata.scheme_id}\n"
        "runtime_type: blackbox_v2\n"
        "input_source: data_bridge_current\n"
        "factor_input_mode: algorithm_managed\n"
        f"{state_config}"
        f"runtime_profile: {RUNTIME_PROFILE}\n"
        f"data_schema_version: {DATA_SCHEMA_VERSION}\n"
        "status: paused\n"
        "version_status: draft\n"
        "schedule:\n"
        f"  cron: '{cron}'\n"
        "  timezone: Asia/Shanghai\n"
        "  timeout_sec: 3600\n"
        "delivery:\n"
        f"  script: delivery/{metadata.scheme_id}.py\n"
        f"  metadata: delivery/{metadata.scheme_id}.json\n"
    )
