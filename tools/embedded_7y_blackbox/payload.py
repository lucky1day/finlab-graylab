"""Create a deterministic, narrowly allowlisted payload from audited source."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import lzma
from pathlib import Path, PurePosixPath


PAYLOAD_PATTERNS = (
    "audit_logging.cpython-313-darwin.so",
    "data_service.cpython-313-darwin.so",
    "runtime_common.cpython-313-darwin.so",
    "shap_policy.cpython-313-darwin.so",
    "daily_project/src/daily/__init__.py",
    "daily_project/src/daily/selected_models/5y10/__init__.py",
    "daily_project/src/daily/selected_models/5y10/run.py",
    "daily_project/src/daily/selected_models/5y10/_run_impl.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/5y10/combo_overlay.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/5y10/fixed_lgbm.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/5y10/strategy_signals.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/10y04/run.py",
    "daily_project/src/daily/selected_models/10y04/_run_impl.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/__init__.py",
    "daily_project/src/daily/selected_models/common/build_weekmap_features.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/daily_utils.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/pandas_compat.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/week_map_alignment.cpython-313-darwin.so",
)

CHUNK_SIZE = 16_384


@dataclass(frozen=True)
class PayloadEntry:
    relative_path: str
    raw_size: int
    raw_sha256: str
    compressed_sha256: str
    encoded_chunks: tuple[str, ...]


def validate_relative_path(relative_path: str) -> None:
    """Reject paths that could escape the audited source root."""
    path = PurePosixPath(relative_path)
    if (
        not relative_path
        or path.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe payload path: {relative_path!r}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_entry(source_root: Path, relative_path: str) -> PayloadEntry:
    validate_relative_path(relative_path)
    source_path = source_root / relative_path
    raw = source_path.read_bytes()
    compressed = lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME)
    encoded = base64.b85encode(compressed).decode("ascii")
    return PayloadEntry(
        relative_path=relative_path,
        raw_size=len(raw),
        raw_sha256=_sha256(raw),
        compressed_sha256=_sha256(compressed),
        encoded_chunks=tuple(
            encoded[index : index + CHUNK_SIZE]
            for index in range(0, len(encoded), CHUNK_SIZE)
        ),
    )


def collect_payload(source_root: Path) -> tuple[PayloadEntry, ...]:
    """Collect the exact audited native modules required by both anchors."""
    return tuple(
        _make_entry(source_root, relative_path)
        for relative_path in sorted(PAYLOAD_PATTERNS)
    )


def decode_entry(entry: PayloadEntry) -> bytes:
    """Decode and integrity-check one embedded payload entry."""
    validate_relative_path(entry.relative_path)
    compressed = base64.b85decode("".join(entry.encoded_chunks).encode("ascii"))
    if _sha256(compressed) != entry.compressed_sha256:
        raise ValueError(f"compressed payload hash mismatch: {entry.relative_path}")
    raw = lzma.decompress(compressed)
    if len(raw) != entry.raw_size or _sha256(raw) != entry.raw_sha256:
        raise ValueError(f"raw payload hash mismatch: {entry.relative_path}")
    return raw
