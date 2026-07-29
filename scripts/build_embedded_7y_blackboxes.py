#!/usr/bin/env python3
"""Build exactly the two approved standalone embedded 7Y deliveries."""

from __future__ import annotations

import argparse
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


def _reject_symlink_ancestors(
    path: Path,
    *,
    trusted_root: Path | None = None,
) -> None:
    absolute = Path(os.path.abspath(path))
    if trusted_root is None:
        current = Path(absolute.anchor)
        parts = absolute.parts[1:]
    else:
        current = Path(os.path.abspath(trusted_root))
        parts = absolute.relative_to(current).parts
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"output root has a symlink ancestor: {current}")
        if not current.exists():
            break


def _validate_output_root(output_root: Path) -> Path:
    output_root = Path(output_root)
    absolute = Path(os.path.abspath(output_root))
    resolved = output_root.resolve()
    approved = Path(os.path.abspath(APPROVED_OUTPUT_ROOT))
    approved_resolved = approved.resolve()
    temporary_lexical = Path(os.path.abspath(tempfile.gettempdir()))
    temporary = temporary_lexical.resolve()
    is_temporary = resolved == temporary or temporary in resolved.parents
    if absolute == temporary_lexical or temporary_lexical in absolute.parents:
        _reject_symlink_ancestors(absolute, trusted_root=temporary_lexical)
    elif absolute == temporary or temporary in absolute.parents:
        _reject_symlink_ancestors(absolute, trusted_root=temporary)
    else:
        _reject_symlink_ancestors(absolute)
    is_approved = absolute == approved and resolved == approved_resolved
    if not is_approved and not is_temporary:
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
        if not is_approved:
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


def _open_directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )


def _rename_known_path(source: Path, destination: Path) -> None:
    source_parent = _open_directory(source.parent)
    destination_parent = _open_directory(destination.parent)
    try:
        os.rename(
            source.name,
            destination.name,
            src_dir_fd=source_parent,
            dst_dir_fd=destination_parent,
        )
    finally:
        os.close(destination_parent)
        os.close(source_parent)


def _remove_known_path(path: Path, *, directory: bool) -> None:
    parent = _open_directory(path.parent)
    try:
        if directory:
            os.rmdir(path.name, dir_fd=parent)
        else:
            os.unlink(path.name, dir_fd=parent)
    finally:
        os.close(parent)


def _remove_validated_delivery(delivery: Path, scheme_id: str) -> None:
    _validate_existing_delivery(delivery, scheme_id)
    for filename in sorted(_expected_names(scheme_id)):
        _remove_known_path(delivery / filename, directory=False)
    _remove_known_path(delivery, directory=True)


def _snapshot_delivery(
    delivery: Path,
    scheme_id: str,
) -> dict[str, tuple[bytes, int]]:
    _validate_existing_delivery(delivery, scheme_id)
    return {
        filename: (
            (delivery / filename).read_bytes(),
            stat.S_IMODE((delivery / filename).lstat().st_mode),
        )
        for filename in sorted(_expected_names(scheme_id))
    }


def _restore_snapshot(
    delivery: Path,
    snapshot: dict[str, tuple[bytes, int]],
) -> None:
    delivery.mkdir(mode=0o700, exist_ok=True)
    entries = {path.name: path for path in delivery.iterdir()}
    if not set(entries).issubset(snapshot):
        raise RuntimeError(f"unexpected rollback contents: {delivery}")
    directory = _open_directory(delivery)
    try:
        for filename, (contents, mode) in snapshot.items():
            path = delivery / filename
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise RuntimeError(f"unsafe rollback target: {path}")
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                mode,
                dir_fd=directory,
            )
            try:
                remaining = memoryview(contents)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written == 0:
                        raise OSError("rollback snapshot write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
                os.fchmod(descriptor, mode)
            finally:
                os.close(descriptor)
    finally:
        os.close(directory)


def _install_staged_delivery(
    staged_delivery: Path,
    final_delivery: Path,
    scheme_id: str,
) -> None:
    backup = final_delivery.parent / f".{scheme_id}.previous"
    if backup.exists() or backup.is_symlink():
        raise ValueError(f"unexpected build backup path exists: {backup}")
    if not final_delivery.exists():
        _rename_known_path(staged_delivery, final_delivery)
        _validate_existing_delivery(final_delivery, scheme_id)
        return
    _validate_existing_delivery(final_delivery, scheme_id)
    snapshot = _snapshot_delivery(final_delivery, scheme_id)
    _rename_known_path(final_delivery, backup)
    try:
        _rename_known_path(staged_delivery, final_delivery)
    except BaseException:
        _rename_known_path(backup, final_delivery)
        raise
    try:
        _remove_validated_delivery(backup, scheme_id)
    except BaseException:
        _rename_known_path(final_delivery, staged_delivery)
        _restore_snapshot(backup, snapshot)
        _validate_existing_delivery(backup, scheme_id)
        _rename_known_path(backup, final_delivery)
        _validate_existing_delivery(final_delivery, scheme_id)
        raise
    _validate_existing_delivery(final_delivery, scheme_id)
    if backup.exists() or backup.is_symlink():
        raise RuntimeError(f"delivery backup cleanup incomplete: {backup}")


def _cleanup_staging_root(staging_root: Path, scheme_id: str) -> None:
    if not staging_root.exists():
        return
    entries = list(staging_root.iterdir())
    if not entries:
        _remove_known_path(staging_root, directory=True)
        return
    expected_delivery = staging_root / scheme_id
    if entries != [expected_delivery]:
        raise RuntimeError(
            f"refusing to remove unexpected staging contents: {staging_root}"
        )
    if expected_delivery.is_dir() and not any(expected_delivery.iterdir()):
        _remove_known_path(expected_delivery, directory=True)
    else:
        _remove_validated_delivery(expected_delivery, scheme_id)
    _remove_known_path(staging_root, directory=True)


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
    for delivery in deliveries:
        print(delivery, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
