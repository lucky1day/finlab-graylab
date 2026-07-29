"""Safety and reproducibility checks for the audited embedded payload."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from tools.embedded_7y_blackbox.payload import (
    collect_payload,
    decode_entry,
    validate_relative_path,
)


SOURCE_ROOT = Path(
    "source_evidence/benchmark_batches/daily_0629/source_package/forecast_project"
)


class EmbeddedPayloadTests(unittest.TestCase):
    def test_payload_contains_both_anchor_implementations(self) -> None:
        entries = collect_payload(SOURCE_ROOT)
        paths = {entry.relative_path for entry in entries}
        self.assertIn(
            "daily_project/src/daily/selected_models/5y10/_run_impl.cpython-313-darwin.so",
            paths,
        )
        self.assertIn(
            "daily_project/src/daily/selected_models/10y04/_run_impl.cpython-313-darwin.so",
            paths,
        )

    def test_payload_excludes_unrelated_and_database_modules(self) -> None:
        paths = {entry.relative_path for entry in collect_payload(SOURCE_ROOT)}
        self.assertFalse(any("1y13" in path for path in paths))
        self.assertFalse(any("db_writer" in path for path in paths))
        self.assertFalse(any("db_config" in path for path in paths))

    def test_entry_round_trip_matches_source_bytes(self) -> None:
        for entry in collect_payload(SOURCE_ROOT):
            self.assertEqual(
                decode_entry(entry),
                (SOURCE_ROOT / entry.relative_path).read_bytes(),
            )

    def test_rejects_unsafe_relative_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe payload path"):
            validate_relative_path("../escape.so")

    def test_payload_raw_size_is_below_eight_mebibytes(self) -> None:
        self.assertLess(
            sum(entry.raw_size for entry in collect_payload(SOURCE_ROOT)),
            8 * 1024 * 1024,
        )

    def test_every_native_payload_filename_is_cpython_313_darwin(self) -> None:
        for entry in collect_payload(SOURCE_ROOT):
            if entry.relative_path.endswith(".so"):
                self.assertIn("cpython-313-darwin", entry.relative_path)

    def test_collection_is_byte_for_byte_deterministic(self) -> None:
        first = json.dumps(
            [entry.__dict__ for entry in collect_payload(SOURCE_ROOT)],
            sort_keys=True,
            separators=(",", ":"),
        )
        second = json.dumps(
            [entry.__dict__ for entry in collect_payload(SOURCE_ROOT)],
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(first.encode(), second.encode())


if __name__ == "__main__":
    unittest.main()
