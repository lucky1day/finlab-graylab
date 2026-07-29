from __future__ import annotations

import base64
import csv
from datetime import date, timedelta
import hashlib
import json
import lzma
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import scripts.build_embedded_7y_blackboxes as builder
import tools.embedded_7y_blackbox.verifier as verifier
from scripts.build_embedded_7y_blackboxes import build_all
from tools.embedded_7y_blackbox.frozen_schemes import SCHEMES, get_scheme
from tools.embedded_7y_blackbox.payload import PayloadEntry
from tools.embedded_7y_blackbox.renderer import render_metadata, render_runner
from tools.embedded_7y_blackbox.verifier import (
    verify_independence,
    verify_no_forbidden_paths,
    verify_parity,
    verify_structure,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = (
    PROJECT_ROOT
    / "source_evidence/benchmark_batches/daily_0629/source_package/forecast_project"
)
BLACKBOX_PYTHON = Path(
    "/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python"
)
REQUEST_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self) -> tuple[str, str]:
        return self._stdout, self._stderr


def tiny_payload() -> tuple[PayloadEntry, ...]:
    raw = b""
    compressed = lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME)
    return (
        PayloadEntry(
            relative_path="daily_project/src/daily/__init__.py",
            raw_size=len(raw),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            compressed_sha256=hashlib.sha256(compressed).hexdigest(),
            encoded_chunks=(
                base64.b85encode(compressed).decode("ascii"),
            ),
        ),
    )


def write_delivery(
    root: Path,
    scheme_id: str = "seven_y_t1_cfc_0084_embedded_v1",
    *,
    runner: str | None = None,
) -> Path:
    scheme = get_scheme(scheme_id)
    delivery = root / scheme_id
    delivery.mkdir(mode=0o700)
    runner_path = delivery / f"{scheme_id}.py"
    metadata_path = delivery / f"{scheme_id}.json"
    runner_path.write_text(
        runner if runner is not None else render_runner(scheme, tiny_payload()),
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(render_metadata(scheme), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    delivery.chmod(0o700)
    runner_path.chmod(0o600)
    metadata_path.chmod(0o600)
    return delivery


def write_platform_fixtures(root: Path) -> Path:
    data_dir = root / "fixtures"
    data_dir.mkdir()
    (data_dir / "daily_output.csv").write_text(
        "date,TB5YWI0C,TB7YWI0C,TB0YWI0C\n"
        "2025-07-15,1.0,1.1,1.2\n",
        encoding="utf-8",
    )
    (data_dir / "weekly_output.csv").write_text(
        "week_id,value\n202529,1.0\n",
        encoding="utf-8",
    )
    (data_dir / "monthly_output.csv").write_text(
        "month_id,value\n202507,1.0\n",
        encoding="utf-8",
    )
    (data_dir / "api_wind_date.csv").write_text(
        "rdate,week_id\n"
        "2025-07-15,202529\n"
        "2025-07-16,202529\n",
        encoding="utf-8",
    )
    (data_dir / "unrelated-secret.txt").write_text("do not copy", encoding="utf-8")
    return data_dir


def fake_sop_runner(
    scheme_id: str,
    *,
    forbidden_read: bool = False,
    trace_tamper: bool = False,
    close_trace_before_read: bool = False,
    forbidden_write: Path | None = None,
) -> str:
    forbidden = ""
    if forbidden_read:
        read_expression = """Path.home().joinpath(
            "bond-factor-lab",
            "source_evidence",
            "benchmark_batches",
            "daily_0629",
            "source_package",
            "forecast_project",
            "audit_logging.cpython-313-darwin.so",
        ).read_bytes()"""
        if close_trace_before_read:
            forbidden = f"""
    inherited_trace = os.environ.get("EMBEDDED_7Y_TRACE_FD")
    if inherited_trace is not None:
        os.close(int(inherited_trace))
    try:
        {read_expression}
    except OSError:
        pass
"""
        elif trace_tamper:
            forbidden = f"""
    try:
        {read_expression}
    except OSError:
        pass
    if "EMBEDDED_7Y_TRACE_PATH" in os.environ:
        Path(os.environ["EMBEDDED_7Y_TRACE_PATH"]).write_text(
            "",
            encoding="utf-8",
        )
    elif "EMBEDDED_7Y_TRACE_FD" in os.environ:
        try:
            os.ftruncate(int(os.environ["EMBEDDED_7Y_TRACE_FD"]), 0)
        except OSError:
            pass
"""
        else:
            forbidden = f"""
    {read_expression}
"""
    writes = ""
    if forbidden_write is not None:
        writes = f"""
    for forbidden_target in (
        Path(args.data_dir) / "daily_output.csv",
        Path({str(forbidden_write)!r}),
    ):
        try:
            forbidden_target.write_text("tampered", encoding="utf-8")
        except OSError:
            pass
"""
    return f"""\
import argparse
import csv
import json
import os
from pathlib import Path

REQUEST_FIELDS = {REQUEST_FIELDS!r}
RESULT_FIELDS = {RESULT_FIELDS!r}
SCHEME_ID = {scheme_id!r}

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    predict = subparsers.add_parser("predict")
    predict.add_argument("--request", required=True)
    predict.add_argument("--data-dir", required=True)
    predict.add_argument("--output", required=True)
    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--requests", required=True)
    backtest.add_argument("--data-dir", required=True)
    backtest.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    if set(path.name for path in root.iterdir()) != {{SCHEME_ID, "data"}}:
        raise RuntimeError("independence root is not blank")
    if "source_package" in os.environ.get("PYTHONPATH", ""):
        raise RuntimeError("source package leaked through PYTHONPATH")
    if (root / "data/unrelated-secret.txt").exists():
        raise RuntimeError("non-platform fixture was copied")
{forbidden}
{writes}
    if args.command == "predict":
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = {{
            key: request[key]
            for key in ("request_id", "predict_date", "feature_date", "target_date")
        }}
        result["predicted_direction"] = 1
        Path(args.output).write_text(json.dumps(result), encoding="utf-8")
    else:
        with Path(args.requests).open(encoding="utf-8", newline="") as handle:
            requests = list(csv.DictReader(handle))
        with Path(args.output).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
            writer.writeheader()
            for request in requests:
                writer.writerow({{
                    "request_id": request["request_id"],
                    "predict_date": request["predict_date"],
                    "feature_date": request["feature_date"],
                    "target_date": request["target_date"],
                    "predicted_direction": 1,
                }})

if __name__ == "__main__":
    main()
"""


def _next_day(value: date) -> date:
    return value + timedelta(days=1)


def write_audit_fixture(
    root: Path,
    *,
    candidate_id: str = "7y-cfc-0084",
) -> tuple[Path, Path, dict[str, int]]:
    audit_root = root / "audit"
    data_dir = root / "data"
    audit_root.mkdir()
    data_dir.mkdir()
    scheme = next(
        item for item in SCHEMES.values() if item.candidate_id == candidate_id
    )
    feature_dates: list[date] = []
    cursor = date(2024, 1, 2)
    while len(feature_dates) < 320:
        if cursor.weekday() < 5:
            feature_dates.append(cursor)
        cursor += timedelta(days=1)

    actions_by_date: dict[str, int] = {}
    phases = (("SIM", 117, 99, 62), ("REAL", 203, 139, 84))
    offset = 0
    for phase, row_count, traded_count, correct_count in phases:
        rows: list[dict[str, object]] = []
        for index in range(row_count):
            feature = feature_dates[offset + index]
            target = _next_day(feature)
            action = 0 if index >= traded_count else (1 if index % 2 == 0 else -1)
            if action == 0:
                label = 1
            elif index < correct_count:
                label = action
            else:
                label = -action
            feature_text = feature.isoformat()
            actions_by_date[feature_text] = action
            rows.append(
                {
                    "phase": phase,
                    "candidate_id": candidate_id,
                    "config_hash": scheme.candidate_hash,
                    "feature_date": feature_text,
                    "target_date": target.isoformat(),
                    "label": label,
                    "action": action,
                }
            )
        with (audit_root / f"{phase.lower()}_predictions.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        offset += row_count

    for filename, contents in (
        ("daily_output.csv", "date,value\n2024-01-02,1\n"),
        ("weekly_output.csv", "week_id,value\n202401,1\n"),
        ("monthly_output.csv", "month_id,value\n202401,1\n"),
    ):
        (data_dir / filename).write_text(contents, encoding="utf-8")
    with (data_dir / "api_wind_date.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=("rdate", "week_id"))
        writer.writeheader()
        for feature in feature_dates:
            iso = feature.isocalendar()
            writer.writerow(
                {
                    "rdate": feature.isoformat(),
                    "week_id": f"{iso.year}{iso.week:02d}",
                }
            )
    return audit_root, data_dir, actions_by_date


class EmbeddedBuildTests(unittest.TestCase):
    def test_build_all_creates_only_two_delivery_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = build_all(SOURCE_ROOT, root)
            self.assertEqual({path.name for path in paths}, set(SCHEMES))
            self.assertEqual({path.name for path in root.iterdir()}, set(SCHEMES))
            first_bytes = {
                path.name: tuple(
                    child.read_bytes() for child in sorted(path.iterdir())
                )
                for path in paths
            }

            rebuilt = build_all(SOURCE_ROOT, root)

            self.assertEqual(
                {
                    path.name: tuple(
                        child.read_bytes() for child in sorted(path.iterdir())
                    )
                    for path in rebuilt
                },
                first_bytes,
            )
            for delivery in rebuilt:
                self.assertEqual(stat.S_IMODE(delivery.stat().st_mode), 0o700)
                for child in delivery.iterdir():
                    self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o600)
                verify_structure(delivery)

    def test_build_preserves_unrelated_output_root_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "unrelated.txt"
            marker.write_text("preserve", encoding="utf-8")

            build_all(SOURCE_ROOT, root)

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_build_rejects_unapproved_non_temporary_output_before_writing(
        self,
    ) -> None:
        output_root = PROJECT_ROOT / f"unapproved-output-{uuid.uuid4().hex}"
        with self.assertRaisesRegex(ValueError, "approved output root"):
            build_all(SOURCE_ROOT, output_root)
        self.assertFalse(output_root.exists())

    def test_output_root_rejects_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real"
            real_parent.mkdir()
            output_root = real_parent / "output"
            output_root.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink ancestor"):
                builder._validate_output_root(linked_parent / "output")

    def test_build_rejects_malformed_existing_scheme_without_touching_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            delivery = root / next(iter(SCHEMES))
            delivery.mkdir()
            marker = delivery / "unexpected.txt"
            marker.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unexpected existing delivery"):
                build_all(SOURCE_ROOT, root)

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_replacement_cleanup_failure_rolls_back_every_step(self) -> None:
        for failure_step in (1, 2, 3):
            with self.subTest(failure_step=failure_step):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    original = build_all(SOURCE_ROOT, root)
                    first_runner = (
                        original[0] / f"{original[0].name}.py"
                    )
                    first_runner.write_text(
                        first_runner.read_text(encoding="utf-8")
                        + "\n# preserve-old-generation\n",
                        encoding="utf-8",
                    )
                    first_runner.chmod(0o600)
                    original_bytes = {
                        delivery.name: {
                            child.name: child.read_bytes()
                            for child in delivery.iterdir()
                        }
                        for delivery in original
                    }
                    calls = 0
                    real_remove = builder._remove_known_path

                    def fail_one_cleanup(path: Path, *, directory: bool) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == failure_step:
                            raise OSError(f"cleanup step {failure_step}")
                        real_remove(path, directory=directory)

                    with (
                        patch.object(
                            builder,
                            "_remove_known_path",
                            side_effect=fail_one_cleanup,
                        ),
                        self.assertRaisesRegex(
                            OSError,
                            f"cleanup step {failure_step}",
                        ),
                    ):
                        build_all(SOURCE_ROOT, root)

                    self.assertEqual(
                        {path.name for path in root.iterdir()},
                        set(SCHEMES),
                    )
                    for scheme_id, expected in original_bytes.items():
                        delivery = root / scheme_id
                        verify_structure(delivery)
                        self.assertEqual(
                            {
                                child.name: child.read_bytes()
                                for child in delivery.iterdir()
                            },
                            expected,
                        )


class EmbeddedStructureTests(unittest.TestCase):
    def test_verifier_rejects_external_absolute_path(self) -> None:
        with self.assertRaisesRegex(AssertionError, "absolute dependency"):
            verify_no_forbidden_paths(
                "open('/Users/macstudio0/Desktop/方案/0629/x')"
            )

    def test_structure_rejects_forbidden_dynamic_execution_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delivery = write_delivery(Path(tmp))
            runner = delivery / f"{delivery.name}.py"
            runner.write_text(
                runner.read_text(encoding="utf-8") + "\neval('1')\n",
                encoding="utf-8",
            )
            runner.chmod(0o600)

            with self.assertRaisesRegex(AssertionError, "forbidden call"):
                verify_structure(delivery)

    def test_structure_rejects_forbidden_call_import_aliases(self) -> None:
        cases = {
            "module_alias": "import os as x\nx.system('id')\n",
            "symbol_alias": "from os import system as x\nx('id')\n",
        }
        for name, addition in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                delivery = write_delivery(Path(tmp))
                runner = delivery / f"{delivery.name}.py"
                runner.write_text(
                    runner.read_text(encoding="utf-8") + "\n" + addition,
                    encoding="utf-8",
                )
                runner.chmod(0o600)

                with self.assertRaisesRegex(AssertionError, "forbidden call"):
                    verify_structure(delivery)

    def test_structure_rejects_symlink_and_non_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            delivery = write_delivery(root)
            metadata = delivery / f"{delivery.name}.json"
            metadata.chmod(0o644)
            with self.assertRaisesRegex(AssertionError, "mode"):
                verify_structure(delivery)

            metadata.chmod(0o600)
            target = root / "metadata-target.json"
            target.write_bytes(metadata.read_bytes())
            metadata.unlink()
            metadata.symlink_to(target)
            with self.assertRaisesRegex(AssertionError, "symlink"):
                verify_structure(delivery)

    def test_structure_rejects_projected_extraction_over_64_mib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = b"\0" * (33 * 1024 * 1024)
            compressed = lzma.compress(raw)
            digest = hashlib.sha256(raw).hexdigest()
            compressed_digest = hashlib.sha256(compressed).hexdigest()
            payload = tuple(
                PayloadEntry(
                    relative_path=f"daily_project/src/daily/large_{index}.bin",
                    raw_size=len(raw),
                    raw_sha256=digest,
                    compressed_sha256=compressed_digest,
                    encoded_chunks=(
                        base64.b85encode(compressed).decode("ascii"),
                    ),
                )
                for index in range(2)
            )
            scheme_id = "seven_y_t1_cfc_0084_embedded_v1"
            runner = render_runner(get_scheme(scheme_id), payload)
            delivery = write_delivery(
                Path(tmp),
                scheme_id,
                runner=runner,
            )

            with self.assertRaisesRegex(AssertionError, "64 MiB"):
                verify_structure(delivery)

    def test_structure_rejects_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delivery = write_delivery(Path(tmp))
            metadata = delivery / f"{delivery.name}.json"
            document = json.loads(metadata.read_text(encoding="utf-8"))
            document["horizon"] = 5
            metadata.write_text(json.dumps(document), encoding="utf-8")
            metadata.chmod(0o600)

            with self.assertRaisesRegex(AssertionError, "metadata"):
                verify_structure(delivery)

    def test_structure_rejects_invalid_base85_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scheme = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
            invalid = PayloadEntry(
                relative_path="daily_project/src/daily/__init__.py",
                raw_size=0,
                raw_sha256=hashlib.sha256(b"").hexdigest(),
                compressed_sha256=hashlib.sha256(b"not-lzma").hexdigest(),
                encoded_chunks=("é",),
            )
            delivery = write_delivery(
                Path(tmp),
                runner=render_runner(scheme, (invalid,)),
            )

            with self.assertRaisesRegex(AssertionError, "Base85"):
                verify_structure(delivery)

    def test_structure_rejects_fabricated_payload_hash_and_size(self) -> None:
        raw = b"audited-payload"
        compressed = lzma.compress(raw)
        encoded = base64.b85encode(compressed).decode("ascii")
        scheme = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
        cases = {
            "compressed_hash": PayloadEntry(
                relative_path="daily_project/src/daily/__init__.py",
                raw_size=len(raw),
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                compressed_sha256="0" * 64,
                encoded_chunks=(encoded,),
            ),
            "raw_hash": PayloadEntry(
                relative_path="daily_project/src/daily/__init__.py",
                raw_size=len(raw),
                raw_sha256="0" * 64,
                compressed_sha256=hashlib.sha256(compressed).hexdigest(),
                encoded_chunks=(encoded,),
            ),
            "raw_size": PayloadEntry(
                relative_path="daily_project/src/daily/__init__.py",
                raw_size=len(raw) + 1,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                compressed_sha256=hashlib.sha256(compressed).hexdigest(),
                encoded_chunks=(encoded,),
            ),
        }
        for name, entry in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                delivery = write_delivery(
                    Path(tmp),
                    runner=render_runner(scheme, (entry,)),
                )
                with self.assertRaisesRegex(AssertionError, "payload integrity"):
                    verify_structure(delivery)

    def test_structure_rejects_lzma_bomb_before_claimed_size(self) -> None:
        raw = b"0" * (64 * 1024 * 1024 + 1)
        compressed = lzma.compress(raw, preset=0)
        bomb = PayloadEntry(
            relative_path="daily_project/src/daily/__init__.py",
            raw_size=1,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            compressed_sha256=hashlib.sha256(compressed).hexdigest(),
            encoded_chunks=(base64.b85encode(compressed).decode("ascii"),),
        )
        scheme = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
        with tempfile.TemporaryDirectory() as tmp:
            delivery = write_delivery(
                Path(tmp),
                runner=render_runner(scheme, (bomb,)),
            )

            with self.assertRaisesRegex(AssertionError, "64 MiB"):
                verify_structure(delivery)

    def test_structure_rejects_embedded_frozen_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delivery = write_delivery(Path(tmp))
            runner = delivery / f"{delivery.name}.py"
            source = runner.read_text(encoding="utf-8")
            self.assertIn('"candidate_id":"7y-cfc-0084"', source)
            runner.write_text(
                source.replace(
                    '"candidate_id":"7y-cfc-0084"',
                    '"candidate_id":"7y-cfc-0156"',
                    1,
                ),
                encoding="utf-8",
            )
            runner.chmod(0o600)

            with self.assertRaisesRegex(AssertionError, "frozen scheme"):
                verify_structure(delivery)


class EmbeddedTraceLifecycleTests(unittest.TestCase):
    def _run_with_trace_records(
        self,
        records: list[dict[str, object]],
    ) -> None:
        class FakePopen:
            returncode = 0

            def __init__(
                fake_self,
                arguments: list[str],
                **kwargs: object,
            ) -> None:
                del fake_self, arguments
                trace_fd = kwargs["pass_fds"]
                assert isinstance(trace_fd, tuple)
                for record in records:
                    os.write(
                        trace_fd[0],
                        json.dumps(
                            record,
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n",
                    )

            def communicate(fake_self) -> tuple[str, str]:
                del fake_self
                return "", ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch.object(subprocess, "Popen", FakePopen):
                verifier._run_confined_process(
                    [str(BLACKBOX_PYTHON), "-c", "pass"],
                    cwd=root,
                    write_root=root / "writable",
                )

    def test_confined_process_rejects_missing_trace_completion(self) -> None:
        with self.assertRaisesRegex(AssertionError, "trace completion"):
            self._run_with_trace_records(
                [{"event": "open", "path": "/tmp/benign", "write": False}]
            )

    def test_confined_process_rejects_failed_trace_completion(self) -> None:
        with self.assertRaisesRegex(AssertionError, "trace write failure"):
            self._run_with_trace_records(
                [
                    {"event": "open", "path": "/tmp/benign", "write": False},
                    {
                        "event": "embedded_7y_trace_complete",
                        "ok": False,
                    },
                ]
            )


class EmbeddedIndependenceTests(unittest.TestCase):
    def test_independence_uses_blank_root_clean_pythonpath_and_only_fixtures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme_id = "seven_y_t1_cfc_0084_embedded_v1"
            delivery = write_delivery(
                root,
                scheme_id,
                runner=fake_sop_runner(scheme_id),
            )
            fixtures = write_platform_fixtures(root)
            leaked_pythonpath = (
                str(SOURCE_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
            )
            with patch.dict(os.environ, {"PYTHONPATH": leaked_pythonpath}):
                verify_independence(delivery, fixtures)

    def test_independence_tracing_rejects_dynamic_forbidden_root_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme_id = "seven_y_t1_cfc_0084_embedded_v1"
            delivery = write_delivery(
                root,
                scheme_id,
                runner=fake_sop_runner(scheme_id, forbidden_read=True),
            )
            fixtures = write_platform_fixtures(root)

            with self.assertRaisesRegex(AssertionError, "forbidden root access"):
                verify_independence(delivery, fixtures)

    def test_independence_trace_cannot_be_erased_after_forbidden_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme_id = "seven_y_t1_cfc_0084_embedded_v1"
            delivery = write_delivery(
                root,
                scheme_id,
                runner=fake_sop_runner(
                    scheme_id,
                    forbidden_read=True,
                    trace_tamper=True,
                ),
            )
            fixtures = write_platform_fixtures(root)

            with self.assertRaisesRegex(AssertionError, "forbidden root access"):
                verify_independence(delivery, fixtures)

    def test_independence_closing_disclosed_trace_fd_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme_id = "seven_y_t1_cfc_0084_embedded_v1"
            delivery = write_delivery(
                root,
                scheme_id,
                runner=fake_sop_runner(
                    scheme_id,
                    forbidden_read=True,
                    close_trace_before_read=True,
                ),
            )
            fixtures = write_platform_fixtures(root)

            with self.assertRaisesRegex(AssertionError, "forbidden root access"):
                verify_independence(delivery, fixtures)

    def test_independence_denies_data_and_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme_id = "seven_y_t1_cfc_0084_embedded_v1"
            external = root / "external.txt"
            external.write_text("preserve", encoding="utf-8")
            delivery = write_delivery(
                root,
                scheme_id,
                runner=fake_sop_runner(
                    scheme_id,
                    forbidden_write=external,
                ),
            )
            fixtures = write_platform_fixtures(root)
            original_fixture = (fixtures / "daily_output.csv").read_bytes()

            with self.assertRaisesRegex(AssertionError, "forbidden write access"):
                verify_independence(delivery, fixtures)

            self.assertEqual(external.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(
                (fixtures / "daily_output.csv").read_bytes(),
                original_fixture,
            )


class EmbeddedParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.delivery = write_delivery(self.root)
        (
            self.audit_root,
            self.data_dir,
            self.actions_by_date,
        ) = write_audit_fixture(self.root)
        self.captured_batches: list[list[dict[str, str]]] = []
        self.captured_invocations: list[dict[str, object]] = []
        self.capture_lock = threading.Lock()

    def fake_popen(
        self,
        sandbox_arguments: list[str],
        **kwargs: object,
    ) -> FakeProcess:
        self.assertEqual(Path(sandbox_arguments[0]), verifier.SANDBOX_EXEC)
        self.assertEqual(sandbox_arguments[1], "-f")
        profile_path = Path(sandbox_arguments[2])
        profile = profile_path.read_text(encoding="utf-8")
        arguments = sandbox_arguments[3:]
        self.assertEqual(Path(arguments[0]), BLACKBOX_PYTHON)
        self.assertEqual(arguments[2], "backtest")
        requests_path = Path(arguments[arguments.index("--requests") + 1])
        output_path = Path(arguments[arguments.index("--output") + 1])
        env = kwargs["env"]
        self.assertIsInstance(env, dict)
        write_root = Path(env["TMPDIR"])
        self.assertEqual(output_path.parent, write_root)
        self.assertTrue(write_root.is_dir())
        self.assertEqual(
            Path(arguments[arguments.index("--data-dir") + 1]),
            self.data_dir,
        )
        with requests_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(tuple(reader.fieldnames or ()), REQUEST_FIELDS)
            requests = list(reader)
        with self.capture_lock:
            self.captured_batches.append(requests)
            self.captured_invocations.append(
                {
                    "sandbox_arguments": tuple(sandbox_arguments),
                    "profile": profile,
                    "write_root": write_root,
                    "output_path": output_path,
                    "cwd": Path(kwargs["cwd"]),
                    "trace_env": env["EMBEDDED_7Y_TRACE_FD"],
                    "pass_fds": kwargs["pass_fds"],
                }
            )
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
            writer.writeheader()
            for request in requests:
                writer.writerow(
                    {
                        "request_id": request["request_id"],
                        "predict_date": request["predict_date"],
                        "feature_date": request["feature_date"],
                        "target_date": request["target_date"],
                        "predicted_direction": self.actions_by_date[
                            request["feature_date"]
                        ],
                    }
                )
        pass_fds = kwargs["pass_fds"]
        self.assertIsInstance(pass_fds, tuple)
        trace_records = (
            {
                "event": "open",
                "path": str(requests_path),
                "write": False,
            },
            {
                "event": "open",
                "path": str(output_path),
                "write": True,
            },
            {
                "event": "embedded_7y_trace_complete",
                "ok": True,
            },
        )
        os.write(
            pass_fds[0],
            b"".join(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
                for record in trace_records
            ),
        )
        return FakeProcess()

    def test_parity_constructs_strict_requests_batches_and_frozen_metrics(
        self,
    ) -> None:
        with patch.object(
            subprocess,
            "Popen",
            new=self.fake_popen,
        ):
            metrics = verify_parity(
                self.delivery,
                "7y-cfc-0084",
                self.audit_root,
                self.data_dir,
                jobs=3,
            )

        self.assertEqual(
            sorted(len(batch) for batch in self.captured_batches),
            [20, 100, 100, 100],
        )
        requests = sorted(
            (request for batch in self.captured_batches for request in batch),
            key=lambda request: request["request_id"],
        )
        self.assertEqual(len(requests), 320)
        for request in requests:
            self.assertEqual(set(request), set(REQUEST_FIELDS))
            self.assertEqual(request["predict_date"], request["feature_date"])
            self.assertEqual(request["daily_cutoff_key"], request["feature_date"])
            self.assertEqual(
                request["monthly_cutoff_key"],
                request["feature_date"].replace("-", "")[:6],
            )
        first = next(
            request
            for request in requests
            if request["feature_date"] == "2024-01-02"
        )
        self.assertEqual(first["weekly_cutoff_key"], "202401")
        self.assertEqual(
            metrics,
            {
                "sim_accuracy": 0.6262626262626263,
                "sim_trade_rate": 0.8461538461538461,
                "real_accuracy": 0.60431654676259,
                "real_trade_rate": 0.6847290640394089,
                "combined_accuracy": 0.6134453781512605,
                "combined_trade_rate": 0.74375,
            },
        )

    def test_parity_exercises_sandbox_and_write_root_confinement(self) -> None:
        with patch.object(
            subprocess,
            "Popen",
            new=self.fake_popen,
        ):
            verify_parity(
                self.delivery,
                "7y-cfc-0084",
                self.audit_root,
                self.data_dir,
            )

        self.assertEqual(len(self.captured_invocations), 4)
        for invocation in self.captured_invocations:
            sandbox_arguments = invocation["sandbox_arguments"]
            self.assertEqual(
                sandbox_arguments[:2],
                (str(verifier.SANDBOX_EXEC), "-f"),
            )
            write_root = invocation["write_root"]
            self.assertEqual(invocation["output_path"].parent, write_root)
            self.assertEqual(write_root.parent, invocation["cwd"])
            self.assertEqual(
                invocation["trace_env"],
                str(invocation["pass_fds"][0]),
            )
            self.assertIn("(deny default)", invocation["profile"])
            self.assertIn(
                f"(allow file-write* (subpath {json.dumps(str(write_root))}))",
                invocation["profile"],
            )

    def test_parity_rejects_invalid_structure_before_subprocess(self) -> None:
        runner = self.delivery / f"{self.delivery.name}.py"
        runner.write_text(
            runner.read_text(encoding="utf-8") + "\neval('1')\n",
            encoding="utf-8",
        )
        runner.chmod(0o600)
        with (
            patch.object(
                verifier,
                "_run_backtest_batch",
                side_effect=AssertionError("execution reached"),
            ),
            self.assertRaisesRegex(AssertionError, "forbidden call"),
        ):
            verify_parity(
                self.delivery,
                "7y-cfc-0084",
                self.audit_root,
                self.data_dir,
            )

    def test_parity_rejects_action_mismatch(self) -> None:
        original = self.actions_by_date["2024-01-02"]
        self.actions_by_date["2024-01-02"] = -original
        with (
            patch.object(
                subprocess,
                "Popen",
                new=self.fake_popen,
            ),
            self.assertRaisesRegex(AssertionError, "action mismatch"),
        ):
            verify_parity(
                self.delivery,
                "7y-cfc-0084",
                self.audit_root,
                self.data_dir,
            )

    def test_parity_rejects_frozen_metric_drift(self) -> None:
        sim_path = self.audit_root / "sim_predictions.csv"
        with sim_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["label"] = str(-int(rows[0]["label"]))
        with sim_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        with (
            patch.object(
                subprocess,
                "Popen",
                new=self.fake_popen,
            ),
            self.assertRaisesRegex(AssertionError, "SIM accuracy"),
        ):
            verify_parity(
                self.delivery,
                "7y-cfc-0084",
                self.audit_root,
                self.data_dir,
                jobs=2,
            )


if __name__ == "__main__":
    unittest.main()
