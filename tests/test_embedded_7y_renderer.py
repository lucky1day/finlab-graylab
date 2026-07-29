from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import tools.embedded_7y_blackbox.renderer as renderer
from tools.embedded_7y_blackbox.frozen_schemes import SCHEMES, get_scheme
from tools.embedded_7y_blackbox.payload import PayloadEntry
from tools.embedded_7y_blackbox.renderer import render_runner, write_delivery


def tiny_payload() -> tuple[PayloadEntry, ...]:
    return (
        PayloadEntry(
            relative_path="daily_project/src/daily/__init__.py",
            raw_size=0,
            raw_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            compressed_sha256="5b3a6a13e0a0fa488ef65cc93f24e2b998a60af5a64c28e2d32d0462dc59f9e6",
            encoded_chunks=("",),
        ),
    )


class EmbeddedRendererTests(unittest.TestCase):
    def test_writes_exactly_two_same_id_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
            delivery = write_delivery(root, scheme, tiny_payload())
            self.assertEqual(
                sorted(path.name for path in delivery.iterdir()),
                [f"{scheme.scheme_id}.json", f"{scheme.scheme_id}.py"],
            )

    def test_schemes_do_not_reference_each_other(self) -> None:
        text_0084 = render_runner(SCHEMES["seven_y_t1_cfc_0084_embedded_v1"], tiny_payload())
        text_0156 = render_runner(SCHEMES["seven_y_t1_cfc_0156_embedded_v1"], tiny_payload())
        self.assertNotIn("seven_y_t1_cfc_0156_embedded_v1", text_0084)
        self.assertNotIn("seven_y_t1_cfc_0084_embedded_v1", text_0156)

    def test_render_is_deterministic(self) -> None:
        scheme = SCHEMES["seven_y_t1_cfc_0156_embedded_v1"]
        self.assertEqual(
            render_runner(scheme, tiny_payload()),
            render_runner(scheme, tiny_payload()),
        )

    def test_cleans_first_temp_file_if_second_temp_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
            original_write_temp = renderer._write_temp
            calls = 0

            def fail_metadata_temp(*args: object, **kwargs: object) -> Path:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("metadata temporary write failed")
                return original_write_temp(*args, **kwargs)

            with patch.object(renderer, "_write_temp", side_effect=fail_metadata_temp):
                with self.assertRaisesRegex(OSError, "metadata temporary write failed"):
                    write_delivery(root, scheme, tiny_payload())

            self.assertEqual(list((root / scheme.scheme_id).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
