#!/usr/bin/env python3
"""Build exactly the two approved standalone embedded 7Y deliveries."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.embedded_7y_blackbox.frozen_schemes import SCHEMES
from tools.embedded_7y_blackbox.payload import collect_payload
from tools.embedded_7y_blackbox.renderer import write_delivery
from tools.embedded_7y_blackbox.verifier import verify_structure


APPROVED_OUTPUT_ROOT = Path(
    "/Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries"
)


def _validate_output_root(output_root: Path) -> Path:
    output_root = Path(output_root)
    absolute = Path(os.path.abspath(output_root))
    resolved = output_root.resolve()
    approved = Path(os.path.abspath(APPROVED_OUTPUT_ROOT))
    temporary = Path(tempfile.gettempdir()).resolve()
    is_temporary = resolved == temporary or temporary in resolved.parents
    if absolute != approved and not is_temporary:
        raise ValueError(
            "output root must be the approved output root or an explicit "
            "temporary test path"
        )
    if output_root.is_symlink():
        raise ValueError("output root must not be a symlink")
    if output_root.exists():
        if not output_root.is_dir():
            raise ValueError("output root must be a directory")
    else:
        if absolute != approved:
            raise ValueError("temporary output root must already exist")
        if not output_root.parent.is_dir() or output_root.parent.is_symlink():
            raise ValueError("approved output parent must be an existing directory")
        output_root.mkdir(mode=0o700)
    return output_root.resolve()


def _expected_names(scheme_id: str) -> set[str]:
    return {f"{scheme_id}.py", f"{scheme_id}.json"}


def _validate_existing_delivery(delivery: Path, scheme_id: str) -> None:
    if delivery.is_symlink() or not delivery.is_dir():
        raise ValueError(f"unexpected existing delivery contents: {delivery}")
    entries = {path.name: path for path in delivery.iterdir()}
    if set(entries) != _expected_names(scheme_id):
        raise ValueError(f"unexpected existing delivery contents: {delivery}")
    for path in entries.values():
        mode = path.lstat().st_mode
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise ValueError(f"unexpected existing delivery contents: {delivery}")


def _remove_validated_delivery(delivery: Path, scheme_id: str) -> None:
    _validate_existing_delivery(delivery, scheme_id)
    for filename in sorted(_expected_names(scheme_id)):
        (delivery / filename).unlink()
    delivery.rmdir()


def _install_staged_delivery(
    staged_delivery: Path,
    final_delivery: Path,
    scheme_id: str,
) -> None:
    backup = final_delivery.parent / f".{scheme_id}.previous"
    if backup.exists() or backup.is_symlink():
        raise ValueError(f"unexpected build backup path exists: {backup}")
    if not final_delivery.exists():
        os.rename(staged_delivery, final_delivery)
        return
    _validate_existing_delivery(final_delivery, scheme_id)
    os.rename(final_delivery, backup)
    try:
        os.rename(staged_delivery, final_delivery)
    except BaseException:
        os.rename(backup, final_delivery)
        raise
    _remove_validated_delivery(backup, scheme_id)


def _cleanup_staging_root(staging_root: Path, scheme_id: str) -> None:
    if not staging_root.exists():
        return
    entries = list(staging_root.iterdir())
    if not entries:
        staging_root.rmdir()
        return
    expected_delivery = staging_root / scheme_id
    if entries != [expected_delivery]:
        raise RuntimeError(
            f"refusing to remove unexpected staging contents: {staging_root}"
        )
    if expected_delivery.is_dir() and not any(expected_delivery.iterdir()):
        expected_delivery.rmdir()
    else:
        _remove_validated_delivery(expected_delivery, scheme_id)
    staging_root.rmdir()


def build_all(source_root: Path, output_root: Path) -> tuple[Path, Path]:
    """Deterministically build only the two frozen scheme directories."""
    output_root = _validate_output_root(output_root)
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(f"source root must be an existing directory: {source_root}")
    for scheme_id in SCHEMES:
        delivery = output_root / scheme_id
        if delivery.exists() or delivery.is_symlink():
            _validate_existing_delivery(delivery, scheme_id)

    payload = collect_payload(source_root)
    built: list[Path] = []
    for scheme_id in sorted(SCHEMES):
        scheme = SCHEMES[scheme_id]
        staging_root = Path(
            tempfile.mkdtemp(prefix=f".{scheme_id}.build-", dir=output_root)
        )
        try:
            staged_delivery = write_delivery(staging_root, scheme, payload)
            staged_delivery.chmod(0o700)
            for path in staged_delivery.iterdir():
                path.chmod(0o600)
            verify_structure(staged_delivery)
            final_delivery = output_root / scheme_id
            _install_staged_delivery(
                staged_delivery,
                final_delivery,
                scheme_id,
            )
            built.append(final_delivery)
        finally:
            if staging_root.exists():
                _cleanup_staging_root(staging_root, scheme_id)
    if len(built) != 2:
        raise AssertionError("build did not produce exactly two deliveries")
    return built[0], built[1]


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    parsed = _argument_parser().parse_args(arguments)
    try:
        deliveries = build_all(parsed.source_root, parsed.output_root)
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "deliveries": [str(path) for path in deliveries],
                "status": "built",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
