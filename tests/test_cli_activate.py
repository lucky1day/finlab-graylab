from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from harness.authorization import issue_token
from harness.cli import main
from harness.config_loader import load_config_raw
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _scaffold_scheme(root: Path, status: str = "paused") -> Path:
    scheme_dir = root / "schemes" / "t5_daily"
    scheme_dir.mkdir(parents=True)
    src = PROJECT_ROOT / "schemes" / "t5_daily" / "config.yaml"
    text = src.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith("status:"):
            lines[i] = f"status: {status}\n"
    (scheme_dir / "config.yaml").write_text("".join(lines), encoding="utf-8")
    return scheme_dir / "config.yaml"


class CliActivateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = "activate-test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        if self._prev_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._prev_secret
        self._tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def test_activate_without_token_blocked(self) -> None:
        _scaffold_scheme(self.root, status="paused")
        code = self._run_cli(
            ["activate", "--scheme-id", "t5_daily", "--project-root", str(self.root)]
        )
        self.assertEqual(code, 2)

    def test_activate_with_valid_token_flips_status(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        token = issue_token("t5_daily", "activate")
        with patch("harness.gates.activate_gate._verify_gate_history", return_value=[]):
            with patch(
                "harness.gates.activate_gate._sync_registry_after_activation",
                return_value="activated-version",
            ) as sync:
                code = self._run_cli(
                    [
                        "activate",
                        "--scheme-id",
                        "t5_daily",
                        "--project-root",
                        str(self.root),
                        "--authorize",
                        token,
                    ]
                )
        self.assertEqual(code, 0)
        sync.assert_called_once()
        raw = load_config_raw(config_path)
        self.assertEqual(raw["status"], "active")

    def test_activate_invalid_token_blocked(self) -> None:
        _scaffold_scheme(self.root, status="paused")
        code = self._run_cli(
            [
                "activate",
                "--scheme-id",
                "t5_daily",
                "--project-root",
                str(self.root),
                "--authorize",
                "not-a-valid-token",
            ]
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
