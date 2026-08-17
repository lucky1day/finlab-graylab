#!/usr/bin/env python
"""按精确 receipt 一次性重绑定 Liwei Phase A Linux input_state。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.scheme_runner import run_scheme  # noqa: E402
from shared.liwei_0616_cache_migration import (  # noqa: E402
    CacheMigrationRebindComplete,
    authorized_cache_rebind,
    validate_cache_rebind_receipt,
)


MAX_RECEIPT_BYTES = 8 * 1024 * 1024


def main(argv: Sequence[str] | None = None) -> int:
    """执行 receipt 中七个 publisher，并输出非秘密 JSON 摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--predict-date", required=True)
    args = parser.parse_args(argv)
    predict_date = _canonical_date(args.predict_date)
    receipt = _load_secure_receipt(args.receipt)

    started = time.monotonic()
    family_results: list[dict[str, Any]] = []
    with authorized_cache_rebind(receipt):
        for entry in receipt["entries"]:
            publisher = str(entry["publisher_consumer_id"])
            try:
                with redirect_stdout(sys.stderr):
                    run_scheme(publisher, predict_date)
            except CacheMigrationRebindComplete as completed:
                family_results.append(
                    _validated_completion(entry, completed.audit)
                )
            else:
                raise RuntimeError(
                    "cache rebind publisher did not publish: "
                    f"{publisher}"
                )

    payload = {
        "status": "completed",
        "migration_id": receipt["migration_id"],
        "receipt_sha256": receipt["receipt_sha256"],
        "predict_date": predict_date,
        "training_calls": 0,
        "families": family_results,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _validated_completion(
    entry: Mapping[str, object],
    audit: Mapping[str, object],
) -> dict[str, Any]:
    expected = {
        "status": "rebound",
        "build_mode": "migration_rebind",
        "cache_family": entry["cache_family"],
        "tenor": entry["tenor"],
        "input_content_id": entry["target_input_content_id"],
        "missing_dates": [],
    }
    if any(audit.get(key) != value for key, value in expected.items()):
        raise RuntimeError(
            "cache rebind completion audit mismatch: "
            f"{entry['cache_family']}"
        )
    generation_id = audit.get("generation_id")
    manifest_sha256 = audit.get("generation_manifest_sha256")
    if not isinstance(generation_id, str) or not generation_id:
        raise RuntimeError("cache rebind generation id is missing")
    if (
        not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in manifest_sha256
        )
    ):
        raise RuntimeError(
            "cache rebind generation manifest digest is invalid"
        )
    return {
        "cache_family": entry["cache_family"],
        "tenor": entry["tenor"],
        "publisher_consumer_id": entry[
            "publisher_consumer_id"
        ],
        "parent_generation_id": entry["parent_generation_id"],
        "generation_id": generation_id,
        "generation_manifest_sha256": manifest_sha256,
        "input_content_id": audit["input_content_id"],
        "build_mode": audit["build_mode"],
        "status": audit["status"],
    }


def _canonical_date(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("predict date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("predict date must be YYYY-MM-DD") from exc
    normalized = parsed.isoformat()
    if normalized != raw:
        raise ValueError("predict date must be YYYY-MM-DD")
    return normalized


def _load_secure_receipt(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise ValueError("cache rebind receipt path must be absolute")
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError("cache rebind receipt is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size < 1
        or before.st_size > MAX_RECEIPT_BYTES
    ):
        raise ValueError("cache rebind receipt is not a secure file")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or opened.st_uid != before.st_uid
        ):
            raise ValueError("cache rebind receipt changed while opening")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("cache rebind receipt was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("cache rebind receipt grew while reading")
    finally:
        os.close(descriptor)
    try:
        raw = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cache rebind receipt is invalid JSON") from exc
    return validate_cache_rebind_receipt(raw)


if __name__ == "__main__":
    raise SystemExit(main())
