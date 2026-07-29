from __future__ import annotations

import csv
from datetime import date, timedelta
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

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


def tiny_payload() -> tuple[PayloadEntry, ...]:
    return (
        PayloadEntry(
            relative_path="daily_project/src/daily/__init__.py",
            raw_size=0,
            raw_sha256=(
                "e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855"
            ),
            compressed_sha256=(
                "5b3a6a13e0a0fa488ef65cc93f24e2b"
                "998a60af5a64c28e2d32d0462dc59f9e6"
            ),
            encoded_chunks=("",),
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


def fake_sop_runner(scheme_id: str, *, forbidden_read: bool = False) -> str:
    forbidden = ""
    if forbidden_read:
        forbidden = """
    Path.home().joinpath(
        "bond-factor-lab",
        "source_evidence",
        "benchmark_batches",
        "daily_0629",
        "source_package",
        "forecast_project",
        "audit_logging.cpython-313-darwin.so",
    ).read_bytes()
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
            delivery = write_delivery(Path(tmp))
            runner = delivery / f"{delivery.name}.py"
            text = runner.read_text(encoding="utf-8")
            self.assertIn('"raw_size":0', text)
            runner.write_text(
                text.replace('"raw_size":0', f'"raw_size":{64 * 1024 * 1024}', 1),
                encoding="utf-8",
            )
            runner.chmod(0o600)

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
        self.capture_lock = threading.Lock()

    def fake_subprocess(
        self,
        arguments: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.assertEqual(Path(arguments[0]), BLACKBOX_PYTHON)
        self.assertEqual(arguments[2], "backtest")
        requests_path = Path(arguments[arguments.index("--requests") + 1])
        output_path = Path(arguments[arguments.index("--output") + 1])
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
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def test_parity_constructs_strict_requests_batches_and_frozen_metrics(
        self,
    ) -> None:
        with patch.object(
            verifier.subprocess,
            "run",
            side_effect=self.fake_subprocess,
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

    def test_parity_rejects_action_mismatch(self) -> None:
        original = self.actions_by_date["2024-01-02"]
        self.actions_by_date["2024-01-02"] = -original
        with (
            patch.object(
                verifier.subprocess,
                "run",
                side_effect=self.fake_subprocess,
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
                verifier.subprocess,
                "run",
                side_effect=self.fake_subprocess,
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
