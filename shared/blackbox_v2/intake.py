from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from shared.blackbox_v2.contracts import BlackboxMetadata, load_metadata
from shared.blackbox_v2.platform_input_registry import (
    normalize_platform_input_ids,
)


SCHEDULES = {
    "daily": "3 7 * * 1-5",
    "weekly": "30 11 * * 6",
    "monthly": "0 18 15 * *",
}
DESCRIPTION_RECOMMENDATION = (
    "Blackbox V2 Metadata 未提供 description；"
    "建议上游补充简短算法逻辑说明。"
)


def intake_warnings(metadata: BlackboxMetadata) -> list[str]:
    """返回不阻断 Intake 的交付质量建议。"""
    return [] if metadata.description else [DESCRIPTION_RECOMMENDATION]


def intake_delivery(
    delivery_dir: str | Path,
    *,
    schemes_root: str | Path,
    runtime_profile: str = "blackbox-v2-v1",
    data_schema_version: str = "data-bridge-v1",
    platform_inputs: Sequence[str] | None = None,
) -> Path:
    """Atomically preserve a two-file delivery and generate its platform-owned config."""
    normalized_platform_inputs = (
        ()
        if platform_inputs is None
        else normalize_platform_input_ids(platform_inputs)
    )
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
    expected_names = {f"{metadata.scheme_id}.py", f"{metadata.scheme_id}.json"}
    if {item.name for item in entries} != expected_names:
        raise ValueError("delivery filenames must match metadata scheme_id")

    destination_root = Path(schemes_root).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / metadata.scheme_id
    if destination.exists():
        raise FileExistsError(f"scheme already exists: {metadata.scheme_id}")

    staging = Path(tempfile.mkdtemp(prefix=f".{metadata.scheme_id}.", dir=destination_root))
    try:
        staged_delivery = staging / "delivery"
        staged_delivery.mkdir()
        shutil.copyfile(scripts[0], staged_delivery / scripts[0].name)
        shutil.copyfile(metadata_files[0], staged_delivery / metadata_files[0].name)
        (staging / "config.yaml").write_text(
            _config_text(
                metadata,
                runtime_profile=runtime_profile,
                data_schema_version=data_schema_version,
                platform_inputs=normalized_platform_inputs,
            ),
            encoding="utf-8",
        )
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    for path in (destination / "delivery").iterdir():
        path.chmod(0o444)
    return destination


def _config_text(
    metadata: BlackboxMetadata,
    *,
    runtime_profile: str,
    data_schema_version: str,
    platform_inputs: tuple[str, ...] = (),
) -> str:
    cron = SCHEDULES[metadata.frequency]
    platform_input_text = ""
    if platform_inputs:
        platform_input_text = "platform_inputs:\n" + "".join(
            f"  - {artifact_id}\n" for artifact_id in platform_inputs
        )
    return (
        f"scheme_id: {metadata.scheme_id}\n"
        "runtime_type: blackbox_v2\n"
        "input_source: data_bridge_current\n"
        f"runtime_profile: {runtime_profile}\n"
        f"data_schema_version: {data_schema_version}\n"
        f"{platform_input_text}"
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
