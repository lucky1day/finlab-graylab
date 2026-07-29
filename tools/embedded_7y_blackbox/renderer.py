"""Render deterministic, standalone files for approved embedded 7Y schemes."""

from __future__ import annotations

import ast
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from tools.embedded_7y_blackbox.frozen_schemes import FrozenScheme
from tools.embedded_7y_blackbox.payload import PayloadEntry


_TEMPLATE_PATH = Path(__file__).with_name("templates") / "runner.py.tmpl"


def render_metadata(scheme: FrozenScheme) -> dict[str, object]:
    """Return the exact SOP metadata for one frozen scheme."""
    return {
        "schema_version": "1.0",
        "scheme_id": scheme.scheme_id,
        "name": (
            "CFC0084_EMBEDDED_ANCHOR_CONSENSUS"
            if scheme.candidate_id == "7y-cfc-0084"
            else "CFC0156_EMBEDDED_ANCHOR_CONSENSUS"
        ),
        "algorithm_version": "1.0.0",
        "target_tenor": "7Y",
        "task_type": "T+1",
        "horizon": 1,
        "target_rule": "target_date_yield_vs_feature_date_yield",
        "description": (
            f"Standalone embedded delivery preserving {scheme.candidate_id} "
            f"({scheme.candidate_hash}) with private 5Y10 and 10Y04 anchors."
        ),
    }


def _json_literal(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def render_runner(scheme: FrozenScheme, payload: tuple[PayloadEntry, ...]) -> str:
    """Render a Python runner containing only frozen JSON-compatible literals."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = template.replace(
        "__FROZEN_SCHEME_JSON__", _json_literal(asdict(scheme))
    ).replace(
        "__PAYLOAD_JSON__", _json_literal([asdict(entry) for entry in payload])
    )
    ast.parse(rendered, filename=f"{scheme.scheme_id}.py")
    return rendered


def _write_temp(directory: Path, filename: str, contents: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=directory, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def write_delivery(
    output_root: Path, scheme: FrozenScheme, payload: tuple[PayloadEntry, ...]
) -> Path:
    """Atomically replace the two files in one scheme-specific delivery directory."""
    delivery = output_root / scheme.scheme_id
    expected_names = {f"{scheme.scheme_id}.json", f"{scheme.scheme_id}.py"}
    if delivery.exists():
        if not delivery.is_dir() or {path.name for path in delivery.iterdir()} != expected_names:
            raise ValueError(f"unexpected existing delivery contents: {delivery}")
    else:
        delivery.mkdir(parents=True)

    runner_name = f"{scheme.scheme_id}.py"
    metadata_name = f"{scheme.scheme_id}.json"
    runner_temporary: Path | None = None
    metadata_temporary: Path | None = None
    try:
        runner_temporary = _write_temp(
            delivery, runner_name, render_runner(scheme, payload)
        )
        metadata_temporary = _write_temp(
            delivery,
            metadata_name,
            json.dumps(render_metadata(scheme), sort_keys=True, indent=2, ensure_ascii=True)
            + "\n",
        )
        os.replace(runner_temporary, delivery / runner_name)
        os.replace(metadata_temporary, delivery / metadata_name)
    finally:
        if runner_temporary is not None:
            runner_temporary.unlink(missing_ok=True)
        if metadata_temporary is not None:
            metadata_temporary.unlink(missing_ok=True)
    return delivery
