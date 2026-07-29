"""Runtime isolation checks for generated embedded 7Y runners."""

from __future__ import annotations

import builtins
from contextlib import chdir, redirect_stderr, redirect_stdout
import csv
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch
import uuid

import pandas as pd

from scheduler.blackbox_v2_runner import execute_blackbox_cli
from tools.embedded_7y_blackbox.frozen_schemes import get_scheme
from tools.embedded_7y_blackbox.payload import collect_payload
from tools.embedded_7y_blackbox.renderer import render_runner


BLACKBOX_PYTHON = Path(
    "/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python"
)
SOURCE_ROOT = Path(
    "source_evidence/benchmark_batches/daily_0629/source_package/forecast_project"
)
REPLAY_DATA_ROOT = Path(
    "/Users/macstudio0/Documents/liwei/outputs/"
    "gray_lab_transfer_3Y_20260725_round6_rank_fusion/"
    "source_replay_input_real/data"
)
REFERENCE_RESULTS_ROOT = Path(
    "/Users/macstudio0/Documents/liwei/outputs/"
    "gray_lab_t1_7y_cross_family_consensus_20260726"
)
REFERENCE_SIGNALS = Path(
    "/Users/macstudio0/Documents/liwei/src/gray_state_gate/signals.py"
)
PLATFORM_FILENAMES = (
    "daily_output.csv",
    "weekly_output.csv",
    "monthly_output.csv",
    "api_wind_date.csv",
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
FORBIDDEN_ROOTS = (
    "/Users/macstudio0/bond-factor-lab/schemes/daily_5y_lgbm_5y10_0629",
    "/Users/macstudio0/bond-factor-lab/schemes/daily_10y_lgbm_10y04_0629",
    "/Users/macstudio0/bond-factor-lab/source_evidence/benchmark_batches/daily_0629",
    "/Users/macstudio0/Desktop/方案/0629/forecast_project",
)
CUTOFF = pd.Timestamp("2025-07-15")
REFERENCE_PHASE_COUNTS = {
    "sim": 117,
    "real": 203,
}


def validated_reference_candidate(
    predictions: pd.DataFrame,
    *,
    phase: str,
    candidate_id: str,
    config_hash: str,
) -> pd.DataFrame:
    """Return one complete, identity-locked audited phase vector."""
    missing = {
        "candidate_id",
        "config_hash",
        "feature_date",
    }.difference(predictions.columns)
    if missing:
        raise AssertionError(
            f"reference candidate columns missing: {sorted(missing)}"
        )
    try:
        expected_count = REFERENCE_PHASE_COUNTS[phase]
    except KeyError as error:
        raise AssertionError(f"unknown reference phase: {phase}") from error
    selected = predictions.loc[
        predictions["candidate_id"] == candidate_id
    ].copy()
    if selected.empty or len(selected) != expected_count:
        raise AssertionError(
            f"{phase} {candidate_id}: expected {expected_count} rows, "
            f"found {len(selected)}"
        )
    if not selected["candidate_id"].eq(candidate_id).all():
        raise AssertionError(f"{phase} {candidate_id}: identity mismatch")
    if not selected["config_hash"].eq(config_hash).all():
        raise AssertionError(f"{phase} {candidate_id}: config hash mismatch")
    dates = pd.to_datetime(
        selected["feature_date"],
        errors="raise",
    )
    normalized = dates.dt.normalize()
    if not dates.equals(normalized):
        raise AssertionError(
            f"{phase} {candidate_id}: feature dates must be normalized"
        )
    if (
        normalized.duplicated().any()
        or not normalized.is_monotonic_increasing
        or normalized.nunique() != expected_count
    ):
        raise AssertionError(
            f"{phase} {candidate_id}: feature dates must be unique "
            "and strictly increasing"
        )
    selected["feature_date"] = normalized
    return selected.set_index("feature_date")


class EmbeddedRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = collect_payload(SOURCE_ROOT)
        cls.rendered_runners = {
            scheme_id: render_runner(get_scheme(scheme_id), payload)
            for scheme_id in (
                "seven_y_t1_cfc_0084_embedded_v1",
                "seven_y_t1_cfc_0156_embedded_v1",
            )
        }
        cls.rendered_runner = cls.rendered_runners[
            "seven_y_t1_cfc_0084_embedded_v1"
        ]
        cls.first_payload_path = payload[0].relative_path

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tempdir = Path(self.temporary.name).resolve()

    def load_generated_module(self, script: str | None = None) -> types.ModuleType:
        runner = self.tempdir / "runner.py"
        runner.write_text(script or self.rendered_runner, encoding="utf-8")
        module_name = f"_embedded_7y_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, runner)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(sys.modules.pop, module_name, None)
        spec.loader.exec_module(module)
        return module

    def write_replay_fixture(
        self,
        destination: Path,
        *,
        append_one_future_daily_row: bool,
    ) -> None:
        destination.mkdir(parents=True)
        daily = pd.read_csv(
            REPLAY_DATA_ROOT / "daily_output.csv",
            low_memory=False,
        )
        daily_dates = pd.to_datetime(daily["date"], errors="raise")
        direct_yields = daily.loc[
            :,
            ("TB5YWI0C", "TB7YWI0C", "TB0YWI0C"),
        ].apply(pd.to_numeric, errors="coerce")
        finite_rows = pd.Series(
            direct_yields.notna().all(axis=1)
            & direct_yields.apply(lambda values: values.map(math.isfinite)).all(axis=1),
            index=daily.index,
        )
        last_nonfinite = finite_rows.loc[~finite_rows].index[-1]
        history_start = daily_dates.iloc[last_nonfinite + 1]
        cutoff_mask = daily_dates.between(history_start, CUTOFF)
        if append_one_future_daily_row:
            first_future_index = daily_dates.loc[daily_dates > CUTOFF].index[0]
            cutoff_mask.loc[first_future_index] = True
        daily.loc[cutoff_mask].to_csv(
            destination / "daily_output.csv",
            index=False,
        )

        calendar = pd.read_csv(REPLAY_DATA_ROOT / "api_wind_date.csv")
        calendar_dates = pd.to_datetime(calendar["rdate"], errors="raise")
        calendar = calendar.loc[
            calendar_dates.between(history_start, CUTOFF)
        ].copy()
        calendar.to_csv(destination / "api_wind_date.csv", index=False)

        weekly = pd.read_csv(
            REPLAY_DATA_ROOT / "weekly_output.csv",
            low_memory=False,
        )
        weekly.loc[weekly["week_id"].isin(calendar["week_id"])].to_csv(
            destination / "weekly_output.csv",
            index=False,
        )

        monthly = pd.read_csv(
            REPLAY_DATA_ROOT / "monthly_output.csv",
            low_memory=False,
        )
        monthly.loc[
            monthly["month_id"].between(
                int(history_start.strftime("%Y%m")),
                202507,
            )
        ].to_csv(
            destination / "monthly_output.csv",
            index=False,
        )

    def write_minimal_platform_fixture(self, destination: Path) -> None:
        destination.mkdir(parents=True)
        daily_dates = pd.date_range(end=CUTOFF, periods=121, freq="D")
        pd.DataFrame(
            {
                "date": daily_dates,
                "TB5YWI0C": [float(value) for value in range(121)],
                "TB7YWI0C": [float(value) for value in range(121)],
                "TB0YWI0C": [float(value) for value in range(121)],
            }
        ).to_csv(destination / "daily_output.csv", index=False)
        pd.DataFrame(
            {
                "rdate": ["2025-07-14", "2025-07-15"],
                "week_id": [202529, 202529],
            }
        ).to_csv(destination / "api_wind_date.csv", index=False)
        pd.DataFrame(
            {"week_id": [202528, 202529], "factor": [1.0, 2.0]}
        ).to_csv(destination / "weekly_output.csv", index=False)
        pd.DataFrame(
            {"month_id": [202506, 202507], "factor": [1.0, 2.0]}
        ).to_csv(destination / "monthly_output.csv", index=False)

    def platform_request(
        self,
        request_id: str,
        *,
        feature_date: str = "2025-07-15",
    ) -> dict[str, str]:
        target_date = (
            "2025-07-16"
            if feature_date == "2025-07-15"
            else "2025-07-15"
        )
        return {
            "request_id": request_id,
            "predict_date": feature_date,
            "feature_date": feature_date,
            "target_date": target_date,
            "daily_cutoff_key": feature_date,
            "weekly_cutoff_key": "202529",
            "monthly_cutoff_key": "202507",
        }

    def write_cli_test_runner(self) -> Path:
        runner = self.tempdir / "generated_runner.py"
        override = textwrap.dedent(
            """
            _CLI_TEST_INFER_CALLS = 0


            def _cli_test_infer(
                data_dir,
                feature_date,
                daily_cutoff_key,
                weekly_cutoff_key,
                monthly_cutoff_key,
            ):
                global _CLI_TEST_INFER_CALLS
                _CLI_TEST_INFER_CALLS += 1
                print("native python diagnostic")
                os.write(1, b"native fd diagnostic\\n")
                return {
                    "action": 1 if feature_date.day % 2 else -1,
                    "score": 999,
                    "curve_action": -1,
                }


            _infer_one = _cli_test_infer
            """
        )
        marker = '\nif __name__ == "__main__":\n'
        if marker not in self.rendered_runner:
            self.fail("generated runner has no CLI entrypoint")
        runner.write_text(
            self.rendered_runner.replace(marker, f"\n{override}{marker}", 1),
            encoding="utf-8",
        )
        return runner

    def write_requests_csv(
        self,
        path: Path,
        requests: list[dict[str, str]],
    ) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REQUEST_FIELDS)
            writer.writeheader()
            writer.writerows(requests)

    def run_cli(
        self,
        runner: Path,
        *arguments: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BLACKBOX_PYTHON), str(runner), *(str(arg) for arg in arguments)],
            text=True,
            capture_output=True,
            check=False,
        )

    def replay_anchor_scores(
        self,
        data_dir: Path,
    ) -> tuple[pd.Series, pd.Series, list[str]]:
        module = self.load_generated_module()
        run_root = Path(f"run-{uuid.uuid4().hex}")
        read_paths: list[str] = []
        original_open = builtins.open

        def audited_open(file: object, *args: object, **kwargs: object):
            mode = args[0] if args else kwargs.get("mode", "r")
            if (
                isinstance(file, (str, os.PathLike))
                and isinstance(mode, str)
                and "r" in mode
            ):
                read_path = str(Path(file).resolve())
                read_paths.append(read_path)
                self.assertFalse(
                    any(
                        read_path.startswith(forbidden)
                        for forbidden in FORBIDDEN_ROOTS
                    ),
                    read_path,
                )
            return original_open(file, *args, **kwargs)

        diagnostics = io.StringIO()
        with chdir(self.tempdir):
            payload_root = module._extract_payload(run_root / "payload")
            five, ten = module._anchor_modules(payload_root)
            with (
                patch.object(module, "open", audited_open, create=True),
                redirect_stdout(diagnostics),
                redirect_stderr(diagnostics),
            ):
                protected_root = module._materialize_cutoff_data(
                    data_dir,
                    CUTOFF,
                    "2025-07-15",
                    "202529",
                    "202507",
                    run_root / "request",
                )
                five_frame = module._run_5y10(
                    five,
                    protected_root,
                    run_root / "five",
                )
                self.assertFalse((run_root / "five").exists())
                ten_frame = module._run_10y04(
                    ten,
                    protected_root,
                    run_root / "ten",
                    CUTOFF,
                )
                self.assertFalse((run_root / "ten").exists())
                five_scores = module._anchor_score(five_frame, "5Y10", CUTOFF)
                ten_scores = module._anchor_score(ten_frame, "10Y04", CUTOFF)
        return five_scores, ten_scores, read_paths

    def reference_phase(
        self,
        phase: str,
    ) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.DataFrame]:
        daily = pd.read_csv(
            REFERENCE_RESULTS_ROOT
            / f"protected_input_{phase}/data/daily_output.csv",
            low_memory=False,
        )

        def anchor_scores(anchor: str) -> pd.Series:
            frame = pd.read_csv(
                REFERENCE_RESULTS_ROOT
                / f"source_replay_{phase}/source_{anchor}/predictions.csv",
                usecols=["date", "prob_up"],
            )
            dates = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
            scores = 2.0 * pd.to_numeric(
                frame["prob_up"], errors="raise"
            ) - 1.0
            result = pd.Series(
                scores.to_numpy(dtype="float64"),
                index=dates,
                name=anchor,
            )
            result = result.groupby(level=0).last().sort_index()
            result.index.name = "feature_date"
            return result

        predictions = pd.read_csv(
            REFERENCE_RESULTS_ROOT / f"{phase}_predictions.csv",
            usecols=[
                "candidate_id",
                "config_hash",
                "feature_date",
                "action",
                "score",
                "front_z",
                "back_z",
            ],
        )
        predictions["feature_date"] = pd.to_datetime(
            predictions["feature_date"],
            errors="raise",
        )
        return (
            daily,
            anchor_scores("5y10"),
            anchor_scores("10y04"),
            predictions,
        )

    def test_weighted_vote_is_one_to_two_with_abstain_ties(self) -> None:
        module = self.load_generated_module()
        curve = pd.Series([1, -1, 1, 0])
        anchor = pd.Series([1, 1, -1, 0])
        expected = pd.Series([1, 1, -1, 0], dtype="int64")

        pd.testing.assert_series_equal(
            module._weighted_vote(curve, anchor),
            expected,
        )

    def test_reference_candidate_validation_rejects_invalid_or_incomplete_rows(
        self,
    ) -> None:
        dates = pd.bdate_range("2025-01-02", periods=117)
        complete = pd.DataFrame(
            {
                "candidate_id": ["7y-cfc-0084"] * 117,
                "config_hash": ["69bf3a432784639d"] * 117,
                "feature_date": dates,
            }
        )
        non_normalized = complete.copy()
        non_normalized.loc[0, "feature_date"] += pd.Timedelta(hours=1)
        non_increasing = complete.copy()
        non_increasing.loc[
            [115, 116],
            "feature_date",
        ] = non_increasing.loc[
            [116, 115],
            "feature_date",
        ].to_numpy()
        wrong_candidate = complete.copy()
        wrong_candidate.loc[0, "candidate_id"] = "7y-cfc-0156"
        wrong_hash = complete.copy()
        wrong_hash.loc[0, "config_hash"] = "ad0d94dc15febd8a"
        cases = {
            "empty": complete.iloc[0:0].copy(),
            "truncated": complete.iloc[:-1].copy(),
            "duplicate": pd.concat(
                [complete.iloc[:-1], complete.iloc[[0]]],
                ignore_index=True,
            ),
            "non_normalized": non_normalized,
            "non_increasing": non_increasing,
            "wrong_candidate": wrong_candidate,
            "wrong_hash": wrong_hash,
        }
        for name, invalid in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AssertionError):
                    validated_reference_candidate(
                        invalid,
                        phase="sim",
                        candidate_id="7y-cfc-0084",
                        config_hash="69bf3a432784639d",
                    )

    def test_reference_phase_does_not_repair_non_normalized_dates(self) -> None:
        root = self.tempdir / "audited-reference"
        (root / "protected_input_sim/data").mkdir(parents=True)
        pd.DataFrame({"date": ["2025-01-02"]}).to_csv(
            root / "protected_input_sim/data/daily_output.csv",
            index=False,
        )
        for anchor in ("5y10", "10y04"):
            anchor_root = root / f"source_replay_sim/source_{anchor}"
            anchor_root.mkdir(parents=True)
            pd.DataFrame(
                {"date": ["2025-01-02"], "prob_up": [0.5]}
            ).to_csv(anchor_root / "predictions.csv", index=False)
        dates = pd.bdate_range("2025-01-02", periods=117)
        dates = dates.to_series(index=range(117))
        dates.iloc[0] += pd.Timedelta(hours=12)
        pd.DataFrame(
            {
                "candidate_id": ["7y-cfc-0084"] * 117,
                "config_hash": ["69bf3a432784639d"] * 117,
                "feature_date": dates,
                "action": [0] * 117,
                "score": [0.0] * 117,
                "front_z": [0.0] * 117,
                "back_z": [0.0] * 117,
            }
        ).to_csv(root / "sim_predictions.csv", index=False)

        with patch(
            f"{__name__}.REFERENCE_RESULTS_ROOT",
            root,
        ):
            _, _, _, predictions = self.reference_phase("sim")

        with self.assertRaisesRegex(AssertionError, "normalized"):
            validated_reference_candidate(
                predictions,
                phase="sim",
                candidate_id="7y-cfc-0084",
                config_hash="69bf3a432784639d",
            )

    def test_scheme_curve_vectors_match_each_exact_audited_golden(
        self,
    ) -> None:
        modules = {
            scheme_id: self.load_generated_module(script)
            for scheme_id, script in self.rendered_runners.items()
        }
        expected_identities = {
            "seven_y_t1_cfc_0084_embedded_v1": (
                "7y-cfc-0084",
                "69bf3a432784639d",
            ),
            "seven_y_t1_cfc_0156_embedded_v1": (
                "7y-cfc-0156",
                "ad0d94dc15febd8a",
            ),
        }
        self.assertEqual(
            modules["seven_y_t1_cfc_0084_embedded_v1"].FROZEN_SCHEME[
                "edge_minimum_history"
            ],
            21,
        )
        self.assertEqual(
            modules["seven_y_t1_cfc_0156_embedded_v1"].FROZEN_SCHEME[
                "edge_minimum_history"
            ],
            14,
        )
        for phase in ("sim", "real"):
            daily, _, _, predictions = self.reference_phase(phase)
            phase_results: dict[str, pd.Series] = {}
            for scheme_id, module in modules.items():
                candidate_id, config_hash = expected_identities[scheme_id]
                expected_frame = validated_reference_candidate(
                    predictions,
                    phase=phase,
                    candidate_id=candidate_id,
                    config_hash=config_hash,
                )
                expected = expected_frame["front_z"].astype("int64")
                actual = module._curve_orientation_actions(
                    daily,
                    module.FROZEN_SCHEME,
                ).reindex(expected.index)
                pd.testing.assert_series_equal(
                    actual.astype("int64"),
                    expected,
                    check_names=False,
                )
                phase_results[scheme_id] = actual
            pd.testing.assert_series_equal(
                phase_results["seven_y_t1_cfc_0084_embedded_v1"],
                phase_results["seven_y_t1_cfc_0156_embedded_v1"],
            )

    def test_hfas_vote_and_action_vectors_match_audited_goldens(self) -> None:
        modules = {
            scheme_id: self.load_generated_module(script)
            for scheme_id, script in self.rendered_runners.items()
        }
        expected_identities = {
            "seven_y_t1_cfc_0084_embedded_v1": (
                "7y-cfc-0084",
                "69bf3a432784639d",
            ),
            "seven_y_t1_cfc_0156_embedded_v1": (
                "7y-cfc-0156",
                "ad0d94dc15febd8a",
            ),
        }
        for phase in ("sim", "real"):
            daily, five, ten, predictions = self.reference_phase(phase)
            for scheme_id, module in modules.items():
                candidate_id, config_hash = expected_identities[scheme_id]
                expected = validated_reference_candidate(
                    predictions,
                    phase=phase,
                    candidate_id=candidate_id,
                    config_hash=config_hash,
                )
                curve = module._curve_orientation_actions(
                    daily,
                    module.FROZEN_SCHEME,
                )
                anchor = module._historical_anchor_actions(daily, five, ten)
                vote = curve.astype("int64") + 2 * anchor.astype("int64")
                action = module._weighted_vote(curve, anchor)
                selected = expected.index
                pd.testing.assert_series_equal(
                    anchor.reindex(selected).astype("int64"),
                    expected["back_z"].astype("int64"),
                    check_names=False,
                )
                pd.testing.assert_series_equal(
                    vote.reindex(selected).astype("int64"),
                    expected["score"].astype("int64"),
                    check_names=False,
                )
                pd.testing.assert_series_equal(
                    action.reindex(selected).astype("int64"),
                    expected["action"].astype("int64"),
                    check_names=False,
                )

    def test_rendered_runner_contains_only_its_own_member_hashes(self) -> None:
        own = {
            "seven_y_t1_cfc_0084_embedded_v1": (
                "7y-cfc-0084",
                "69bf3a432784639d",
                "7y-fcco-08756",
                "e80445063865243d",
            ),
            "seven_y_t1_cfc_0156_embedded_v1": (
                "7y-cfc-0156",
                "ad0d94dc15febd8a",
                "7y-cco-03620",
                "182131092906070b",
            ),
        }
        for scheme_id, script in self.rendered_runners.items():
            other_id = next(value for value in own if value != scheme_id)
            for literal in own[scheme_id]:
                self.assertIn(literal, script)
            for literal in own[other_id]:
                self.assertNotIn(literal, script)
            self.assertIn("7y-hfas-10173", script)
            self.assertIn("a8cbf01f35327bb7", script)
            self.assertNotIn(str(REFERENCE_SIGNALS), script)

    def test_infer_one_matches_audited_cutoff_consensus(self) -> None:
        module = self.load_generated_module()
        source = self.tempdir / "infer-platform"
        self.write_replay_fixture(
            source,
            append_one_future_daily_row=False,
        )
        reference = pd.read_csv(
            REFERENCE_RESULTS_ROOT / "real_predictions.csv",
            usecols=[
                "candidate_id",
                "config_hash",
                "feature_date",
                "action",
                "score",
                "front_z",
                "back_z",
            ],
        )
        selected = validated_reference_candidate(
            reference,
            phase="real",
            candidate_id="7y-cfc-0084",
            config_hash="69bf3a432784639d",
        ).loc[CUTOFF]

        result = module._infer_one(
            source,
            CUTOFF,
            "2025-07-15",
            "202529",
            "202507",
        )

        self.assertEqual(
            result,
            {
                "scheme_id": "seven_y_t1_cfc_0084_embedded_v1",
                "feature_date": CUTOFF.strftime("%Y-%m-%d"),
                "curve_action": int(selected["front_z"]),
                "anchor_action": int(selected["back_z"]),
                "vote": int(selected["score"]),
                "action": int(selected["action"]),
            },
        )

    def test_full_platform_input_matches_pretrimmed_golden_inference(
        self,
    ) -> None:
        module = self.load_generated_module()
        pretrimmed = self.tempdir / "pretrimmed-platform"
        self.write_replay_fixture(
            pretrimmed,
            append_one_future_daily_row=False,
        )
        expected = module._infer_one(
            pretrimmed,
            CUTOFF,
            "2025-07-15",
            "202529",
            "202507",
        )

        actual = module._infer_one(
            REPLAY_DATA_ROOT,
            CUTOFF,
            "2025-07-15",
            "202529",
            "202507",
        )

        self.assertEqual(actual, expected)

    def test_predict_backtest_parity(self) -> None:
        runner = self.write_cli_test_runner()
        data_dir = self.tempdir / "platform"
        self.write_minimal_platform_fixture(data_dir)
        request = self.platform_request("parity-001")
        request_path = self.tempdir / "request.json"
        request_path.write_text(
            json.dumps(request),
            encoding="utf-8",
        )
        requests_path = self.tempdir / "requests.csv"
        self.write_requests_csv(requests_path, [request])
        prediction_path = self.tempdir / "prediction.json"
        backtest_path = self.tempdir / "backtest.csv"

        prediction = self.run_cli(
            runner,
            "predict",
            "--request",
            request_path,
            "--data-dir",
            data_dir,
            "--output",
            prediction_path,
        )
        backtest = self.run_cli(
            runner,
            "backtest",
            "--requests",
            requests_path,
            "--data-dir",
            data_dir,
            "--output",
            backtest_path,
        )

        self.assertEqual(prediction.returncode, 0, prediction.stderr)
        self.assertEqual(backtest.returncode, 0, backtest.stderr)
        self.assertEqual(prediction.stdout, "")
        self.assertEqual(backtest.stdout, "")
        result = json.loads(prediction_path.read_text(encoding="utf-8"))
        with backtest_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(set(result), set(RESULT_FIELDS))
        self.assertIn(result["predicted_direction"], (-1, 0, 1))
        self.assertEqual(list(rows[0]), list(RESULT_FIELDS))
        self.assertEqual(
            {
                **{key: rows[0][key] for key in RESULT_FIELDS[:-1]},
                "predicted_direction": int(rows[0]["predicted_direction"]),
            },
            result,
        )
        self.assertNotIn("score", result)
        self.assertNotIn("action", result)

    def test_backtest_supports_100_identical_cutoffs_with_one_inference(
        self,
    ) -> None:
        runner = self.write_cli_test_runner()
        data_dir = self.tempdir / "platform"
        self.write_minimal_platform_fixture(data_dir)
        requests = [
            self.platform_request(f"batch-{index:03d}")
            for index in range(100)
        ]
        requests_path = self.tempdir / "requests.csv"
        self.write_requests_csv(requests_path, requests)
        output_path = self.tempdir / "backtest.csv"

        completed = self.run_cli(
            runner,
            "backtest",
            "--requests",
            requests_path,
            "--data-dir",
            data_dir,
            "--output",
            output_path,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.count("native python diagnostic"), 1)
        self.assertEqual(completed.stderr.count("native fd diagnostic"), 1)
        with output_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 100)
        self.assertEqual(
            [row["request_id"] for row in rows],
            [request["request_id"] for request in requests],
        )
        self.assertTrue(all(list(row) == list(RESULT_FIELDS) for row in rows))

    def test_reordered_batches_preserve_per_request_results(self) -> None:
        runner = self.write_cli_test_runner()
        data_dir = self.tempdir / "platform"
        self.write_minimal_platform_fixture(data_dir)
        requests = [
            self.platform_request("later", feature_date="2025-07-15"),
            self.platform_request("earlier", feature_date="2025-07-14"),
            self.platform_request("later-copy", feature_date="2025-07-15"),
        ]
        observed: list[list[dict[str, str]]] = []
        for index, batch in enumerate((requests, list(reversed(requests)))):
            request_path = self.tempdir / f"requests-{index}.csv"
            output_path = self.tempdir / f"output-{index}.csv"
            self.write_requests_csv(request_path, batch)
            completed = self.run_cli(
                runner,
                "backtest",
                "--requests",
                request_path,
                "--data-dir",
                data_dir,
                "--output",
                output_path,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            with output_path.open(encoding="utf-8") as handle:
                observed.append(list(csv.DictReader(handle)))

        self.assertEqual(
            [row["request_id"] for row in observed[0]],
            [request["request_id"] for request in requests],
        )
        self.assertEqual(
            {
                row["request_id"]: row["predicted_direction"]
                for row in observed[0]
            },
            {
                row["request_id"]: row["predicted_direction"]
                for row in observed[1]
            },
        )

    def test_backtest_invalid_item_50_fails_without_output(self) -> None:
        runner = self.write_cli_test_runner()
        data_dir = self.tempdir / "platform"
        self.write_minimal_platform_fixture(data_dir)
        requests = [
            self.platform_request(f"atomic-{index:03d}")
            for index in range(100)
        ]
        requests[49]["predict_date"] = "2025-7-15"
        requests_path = self.tempdir / "requests.csv"
        self.write_requests_csv(requests_path, requests)
        output_path = self.tempdir / "must-not-exist.csv"

        completed = self.run_cli(
            runner,
            "backtest",
            "--requests",
            requests_path,
            "--data-dir",
            data_dir,
            "--output",
            output_path,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertFalse(output_path.exists())
        self.assertNotIn("native python diagnostic", completed.stderr)

    def test_predict_rejects_duplicate_json_member_without_output(self) -> None:
        runner = self.write_cli_test_runner()
        data_dir = self.tempdir / "platform"
        self.write_minimal_platform_fixture(data_dir)
        request_path = self.tempdir / "duplicate-member.json"
        request_path.write_text(
            (
                '{"request_id":"duplicate-json",'
                '"predict_date":"2025-07-15",'
                '"predict_date":"2025-07-15",'
                '"feature_date":"2025-07-15",'
                '"target_date":"2025-07-16",'
                '"daily_cutoff_key":"2025-07-15",'
                '"weekly_cutoff_key":"202529",'
                '"monthly_cutoff_key":"202507"}'
            ),
            encoding="utf-8",
        )
        output_path = self.tempdir / "must-not-exist.json"

        completed = self.run_cli(
            runner,
            "predict",
            "--request",
            request_path,
            "--data-dir",
            data_dir,
            "--output",
            output_path,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertFalse(output_path.exists())
        self.assertNotIn("native python diagnostic", completed.stderr)
        self.assertNotIn("native fd diagnostic", completed.stderr)

    def test_preexisting_output_is_rejected_before_inference_and_preserved(
        self,
    ) -> None:
        runner = self.write_cli_test_runner()
        data_dir = self.tempdir / "platform"
        self.write_minimal_platform_fixture(data_dir)
        request_path = self.tempdir / "request.json"
        request_path.write_text(
            json.dumps(self.platform_request("existing-output")),
            encoding="utf-8",
        )
        output_path = self.tempdir / "unrelated-existing.json"
        original = b"unrelated pre-existing data\x00\xff"
        output_path.write_bytes(original)

        completed = self.run_cli(
            runner,
            "predict",
            "--request",
            request_path,
            "--data-dir",
            data_dir,
            "--output",
            output_path,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(output_path.read_bytes(), original)
        self.assertNotIn("native python diagnostic", completed.stderr)
        self.assertNotIn("native fd diagnostic", completed.stderr)

    def test_request_validation_is_strict_and_fail_closed(self) -> None:
        module = self.load_generated_module()
        data_dir = self.tempdir / "platform"
        self.write_minimal_platform_fixture(data_dir)
        valid = self.platform_request("strict-001")
        cases: dict[str, list[dict[str, object]]] = {
            "empty": [],
            "too-many": [
                self.platform_request(f"many-{index:03d}")
                for index in range(101)
            ],
            "duplicate-id": [valid, dict(valid)],
            "empty-id": [{**valid, "request_id": ""}],
            "missing-field": [
                {
                    key: value
                    for key, value in valid.items()
                    if key != "target_date"
                }
            ],
            "extra-field": [{**valid, "action": 1}],
            "invalid-date": [{**valid, "predict_date": "2025-7-15"}],
            "date-order": [{**valid, "target_date": "2025-07-15"}],
            "daily-cutoff": [
                {**valid, "daily_cutoff_key": "2025-07-14"}
            ],
            "weekly-cutoff": [{**valid, "weekly_cutoff_key": "202528"}],
            "monthly-cutoff": [{**valid, "monthly_cutoff_key": "202506"}],
        }
        with patch.object(module, "_infer_one") as infer:
            for name, requests in cases.items():
                with self.subTest(name=name):
                    with self.assertRaises((TypeError, ValueError)):
                        module._execute_requests(requests, data_dir)
            infer.assert_not_called()

    def test_materializes_exact_causal_platform_cutoff(self) -> None:
        module = self.load_generated_module()
        source = self.tempdir / "platform"
        self.write_minimal_platform_fixture(source)
        future_daily = pd.DataFrame(
            {
                "date": ["2025-07-16"],
                "TB5YWI0C": [99.0],
                "TB7YWI0C": [math.inf],
                "TB0YWI0C": [99.0],
            }
        )
        future_daily.to_csv(
            source / "daily_output.csv",
            mode="a",
            header=False,
            index=False,
        )

        protected_root = module._materialize_cutoff_data(
            source,
            CUTOFF,
            "2025-07-15",
            "202529",
            "202507",
            self.tempdir / "request",
        )

        data_root = protected_root / "data"
        self.assertEqual(
            sorted(path.name for path in data_root.iterdir()),
            sorted(PLATFORM_FILENAMES),
        )
        daily = pd.read_csv(data_root / "daily_output.csv")
        calendar = pd.read_csv(data_root / "api_wind_date.csv")
        weekly = pd.read_csv(data_root / "weekly_output.csv")
        monthly = pd.read_csv(data_root / "monthly_output.csv")
        self.assertEqual(pd.to_datetime(daily["date"]).max(), CUTOFF)
        self.assertEqual(pd.to_datetime(calendar["rdate"]).max(), CUTOFF)
        self.assertEqual(weekly["week_id"].tolist(), [202529])
        self.assertEqual(monthly["month_id"].tolist(), [202506, 202507])

    def test_materialization_trims_only_causal_nonfinite_prefix(self) -> None:
        module = self.load_generated_module()
        source = self.tempdir / "finite-suffix"
        self.write_minimal_platform_fixture(source)
        finite_count = 120
        dates = pd.date_range(
            end=CUTOFF,
            periods=finite_count + 2,
            freq="D",
        )
        daily = pd.DataFrame(
            {
                "date": dates,
                "TB5YWI0C": [float(value) for value in range(len(dates))],
                "TB7YWI0C": [float(value) for value in range(len(dates))],
                "TB0YWI0C": [float(value) for value in range(len(dates))],
            }
        )
        daily.loc[0, "TB5YWI0C"] = math.nan
        daily.loc[1, "TB7YWI0C"] = math.inf
        daily = pd.concat(
            [
                daily,
                pd.DataFrame(
                    {
                        "date": [CUTOFF + pd.Timedelta(days=1)],
                        "TB5YWI0C": [math.nan],
                        "TB7YWI0C": [math.inf],
                        "TB0YWI0C": [math.nan],
                    }
                ),
            ],
            ignore_index=True,
        )
        daily.to_csv(source / "daily_output.csv", index=False)

        protected = module._materialize_cutoff_data(
            source,
            CUTOFF,
            "2025-07-15",
            "202529",
            "202507",
            self.tempdir / "finite-suffix-request",
        )

        retained = pd.read_csv(protected / "data" / "daily_output.csv")
        self.assertEqual(len(retained), finite_count)
        self.assertEqual(pd.Timestamp(retained["date"].iloc[0]), dates[2])
        self.assertEqual(pd.Timestamp(retained["date"].iloc[-1]), CUTOFF)
        retained_yields = retained.loc[
            :,
            ("TB5YWI0C", "TB7YWI0C", "TB0YWI0C"),
        ].apply(pd.to_numeric, errors="coerce")
        self.assertTrue(
            retained_yields.apply(
                lambda column: column.map(math.isfinite)
            ).all(axis=None)
        )

    def test_materialization_rejects_bad_cutoff_and_short_finite_suffix(
        self,
    ) -> None:
        module = self.load_generated_module()
        cases = (("bad-cutoff", 120, True), ("short-suffix", 119, False))
        for name, finite_count, cutoff_bad in cases:
            with self.subTest(name=name):
                source = self.tempdir / name
                self.write_minimal_platform_fixture(source)
                dates = pd.date_range(
                    end=CUTOFF,
                    periods=finite_count + 1,
                    freq="D",
                )
                daily = pd.DataFrame(
                    {
                        "date": dates,
                        "TB5YWI0C": [
                            float(value) for value in range(len(dates))
                        ],
                        "TB7YWI0C": [
                            float(value) for value in range(len(dates))
                        ],
                        "TB0YWI0C": [
                            float(value) for value in range(len(dates))
                        ],
                    }
                )
                daily.loc[0, "TB5YWI0C"] = math.nan
                if cutoff_bad:
                    daily.loc[daily.index[-1], "TB7YWI0C"] = math.inf
                daily.to_csv(source / "daily_output.csv", index=False)

                with self.assertRaisesRegex(ValueError, "finite|history"):
                    module._materialize_cutoff_data(
                        source,
                        CUTOFF,
                        "2025-07-15",
                        "202529",
                        "202507",
                        self.tempdir / f"{name}-request",
                    )

    def test_post_cutoff_disorder_in_each_stream_is_ignored(self) -> None:
        module = self.load_generated_module()
        baseline_source = self.tempdir / "baseline-platform"
        self.write_minimal_platform_fixture(baseline_source)
        baseline_root = module._materialize_cutoff_data(
            baseline_source,
            CUTOFF,
            "2025-07-15",
            "202529",
            "202507",
            self.tempdir / "baseline-request",
        )
        baseline = {
            filename: pd.read_csv(baseline_root / "data" / filename)
            for filename in PLATFORM_FILENAMES
        }
        future_disorder = {
            "daily_output.csv": pd.DataFrame(
                {
                    "date": ["2025-07-17", "2025-07-16", "2025-07-16"],
                    "TB5YWI0C": [9.0, 9.0, 9.0],
                    "TB7YWI0C": [math.inf, 9.0, 9.0],
                    "TB0YWI0C": [9.0, 9.0, 9.0],
                }
            ),
            "api_wind_date.csv": pd.DataFrame(
                {
                    "rdate": ["2025-07-17", "2025-07-16", "2025-07-16"],
                    "week_id": [202530, 202530, 202530],
                }
            ),
            "weekly_output.csv": pd.DataFrame(
                {
                    "week_id": [202531, 202530, 202530],
                    "factor": [9.0, 9.0, 9.0],
                }
            ),
            "monthly_output.csv": pd.DataFrame(
                {
                    "month_id": [202509, 202508, 202508],
                    "factor": [9.0, 9.0, 9.0],
                }
            ),
        }

        for index, (filename, future_rows) in enumerate(
            future_disorder.items()
        ):
            with self.subTest(filename=filename):
                source = self.tempdir / f"future-disorder-{index}"
                self.write_minimal_platform_fixture(source)
                future_rows.to_csv(
                    source / filename,
                    mode="a",
                    header=False,
                    index=False,
                )
                protected = module._materialize_cutoff_data(
                    source,
                    CUTOFF,
                    "2025-07-15",
                    "202529",
                    "202507",
                    self.tempdir / f"future-disorder-request-{index}",
                )
                for output_filename in PLATFORM_FILENAMES:
                    pd.testing.assert_frame_equal(
                        pd.read_csv(protected / "data" / output_filename),
                        baseline[output_filename],
                    )

    def test_materialization_fails_closed_on_invalid_inputs(self) -> None:
        module = self.load_generated_module()

        source = self.tempdir / "missing"
        self.write_minimal_platform_fixture(source)
        (source / "weekly_output.csv").unlink()
        with self.assertRaises((FileNotFoundError, ValueError)):
            module._materialize_cutoff_data(
                source,
                CUTOFF,
                "2025-07-15",
                "202529",
                "202507",
                self.tempdir / "missing-request",
            )

        source = self.tempdir / "nonmonotonic"
        self.write_minimal_platform_fixture(source)
        daily = pd.read_csv(source / "daily_output.csv").iloc[::-1]
        daily.to_csv(source / "daily_output.csv", index=False)
        with self.assertRaisesRegex(ValueError, "monotonic"):
            module._materialize_cutoff_data(
                source,
                CUTOFF,
                "2025-07-15",
                "202529",
                "202507",
                self.tempdir / "nonmonotonic-request",
            )

        source = self.tempdir / "nonfinite"
        self.write_minimal_platform_fixture(source)
        daily = pd.read_csv(source / "daily_output.csv")
        daily.loc[daily.index[-1], "TB7YWI0C"] = math.inf
        daily.to_csv(source / "daily_output.csv", index=False)
        with self.assertRaisesRegex(ValueError, "finite"):
            module._materialize_cutoff_data(
                source,
                CUTOFF,
                "2025-07-15",
                "202529",
                "202507",
                self.tempdir / "nonfinite-request",
            )

    def test_materialization_rejects_nonfinite_prefix_with_short_suffix(
        self,
    ) -> None:
        module = self.load_generated_module()
        invalid_values = (float("nan"), "not-a-number")
        for index, invalid in enumerate(invalid_values):
            with self.subTest(invalid=invalid):
                source = self.tempdir / f"retained-nonfinite-{index}"
                self.write_minimal_platform_fixture(source)
                daily = pd.read_csv(source / "daily_output.csv")
                if isinstance(invalid, str):
                    daily["TB5YWI0C"] = daily["TB5YWI0C"].astype(object)
                daily.loc[daily.index[1], "TB5YWI0C"] = invalid
                daily.to_csv(source / "daily_output.csv", index=False)
                with self.assertRaisesRegex(ValueError, "history"):
                    module._materialize_cutoff_data(
                        source,
                        CUTOFF,
                        "2025-07-15",
                        "202529",
                        "202507",
                        self.tempdir / f"retained-nonfinite-request-{index}",
                    )

    def test_future_rows_do_not_change_result(self) -> None:
        without_future = self.tempdir / "without-future"
        with_future = self.tempdir / "with-future"
        self.write_replay_fixture(
            without_future,
            append_one_future_daily_row=False,
        )
        self.write_replay_fixture(
            with_future,
            append_one_future_daily_row=True,
        )

        baseline_five, baseline_ten, baseline_reads = self.replay_anchor_scores(
            without_future
        )
        future_five, future_ten, future_reads = self.replay_anchor_scores(with_future)

        pd.testing.assert_series_equal(baseline_five, future_five)
        pd.testing.assert_series_equal(baseline_ten, future_ten)
        self.assertTrue(math.isfinite(float(baseline_five.loc[CUTOFF])))
        self.assertTrue(math.isfinite(float(baseline_ten.loc[CUTOFF])))
        self.assertTrue(baseline_reads)
        self.assertTrue(future_reads)

    def test_corrupted_payload_fails_before_extraction(self) -> None:
        script = self.rendered_runner.replace(
            '"compressed_sha256":"', '"compressed_sha256":"0', 1
        )
        runner = self.tempdir / "corrupt_runner.py"
        runner.write_text(script, encoding="utf-8")
        extraction_root = self.tempdir / "private"
        command = (
            "import runpy; from pathlib import Path; "
            f"m=runpy.run_path({str(runner)!r}); "
            f"m['_extract_payload'](Path({str(extraction_root)!r}))"
        )

        completed = subprocess.run(
            [str(BLACKBOX_PYTHON), "-c", command],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("compressed payload hash mismatch", completed.stderr)
        self.assertFalse(extraction_root.exists())

    def test_payload_path_escape_is_rejected(self) -> None:
        module = self.load_generated_module()
        for unsafe in (
            "../escape.so",
            "/tmp/escape.so",
            r"..\escape.so",
            "daily_project//escape.so",
            "daily_project/./escape.so",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "unsafe payload path"):
                    module._safe_payload_path(self.tempdir, unsafe)

    def test_runtime_rejects_wrong_python_abi(self) -> None:
        module = self.load_generated_module()
        with patch.object(module.sys, "version_info", (3, 12)):
            with self.assertRaisesRegex(RuntimeError, "requires CPython 3.13"):
                module._validate_runtime()

    def test_runtime_rejects_non_cpython_implementation(self) -> None:
        module = self.load_generated_module()
        implementation = types.SimpleNamespace(name="pypy")
        with patch.object(module.sys, "implementation", implementation):
            with self.assertRaisesRegex(RuntimeError, "requires CPython 3.13"):
                module._validate_runtime()

    def test_runtime_rejects_wrong_platform_abi(self) -> None:
        module = self.load_generated_module()
        for system_name, machine_name in (("Linux", "arm64"), ("Darwin", "x86_64")):
            with self.subTest(system=system_name, machine=machine_name):
                with (
                    patch.object(
                        module.platform, "system", return_value=system_name
                    ),
                    patch.object(
                        module.platform, "machine", return_value=machine_name
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "requires macOS arm64"):
                        module._validate_runtime()

    def test_extraction_rejects_symlink_root(self) -> None:
        module = self.load_generated_module()
        outside = self.tempdir / "outside"
        outside.mkdir()
        run_root = self.tempdir / "private"
        run_root.symlink_to(outside, target_is_directory=True)

        with chdir(self.tempdir):
            with self.assertRaisesRegex(ValueError, "unsafe payload extraction root"):
                module._extract_payload(Path("private"))

        self.assertEqual(list(outside.iterdir()), [])

    def test_extraction_rejects_symlink_root_ancestor(self) -> None:
        module = self.load_generated_module()
        outside = self.tempdir / "outside"
        outside.mkdir()
        link = self.tempdir / "linked-parent"
        link.symlink_to(outside, target_is_directory=True)
        run_root = link / "nested" / "private"

        with chdir(self.tempdir):
            with self.assertRaisesRegex(ValueError, "unsafe payload extraction root"):
                module._extract_payload(Path("linked-parent/nested/private"))

        self.assertEqual(list(outside.iterdir()), [])

    def test_generated_extraction_runs_in_platform_deny_default_sandbox(
        self,
    ) -> None:
        script = self.rendered_runner.replace(
            'if __name__ == "__main__":\n    raise SystemExit(main())\n',
            textwrap.dedent(
                """\
                if __name__ == "__main__":
                    output = Path(sys.argv[sys.argv.index("--output") + 1])
                    payload_root = _extract_payload(Path("payload"))
                    if not payload_root.joinpath(
                        "daily_project", "src", "daily", "__init__.py"
                    ).is_file():
                        raise RuntimeError("payload extraction probe failed")
                    output.write_text("{}", encoding="utf-8")
                """
            ),
        )
        control_root = self.tempdir / "platform-control"
        control_root.mkdir()
        runner = control_root / "probe.py"
        runner.write_text(script, encoding="utf-8")
        request = control_root / "request.json"
        request.write_text("{}", encoding="utf-8")
        data_dir = control_root / "data"
        data_dir.mkdir()
        for filename in (
            "daily_output.csv",
            "weekly_output.csv",
            "monthly_output.csv",
        ):
            (data_dir / filename).write_text("key,value\n1,1\n", encoding="utf-8")
        run_root = self.tempdir / "platform-run"
        run_root.mkdir()
        output = run_root / "result.json"

        completed = execute_blackbox_cli(
            script_path=runner,
            mode="predict",
            input_path=request,
            data_dir=data_dir,
            output_path=output,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(output.read_text(encoding="utf-8"), "{}")

    def test_unmodified_generated_predict_runs_in_platform_sandbox(
        self,
    ) -> None:
        control_root = self.tempdir / "real-platform-control"
        control_root.mkdir()
        runner = control_root / "generated_runner.py"
        runner.write_text(self.rendered_runner, encoding="utf-8")
        data_dir = control_root / "data"
        self.write_replay_fixture(
            data_dir,
            append_one_future_daily_row=False,
        )
        request = self.platform_request("real-sandbox-001")
        request_path = control_root / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        run_root = self.tempdir / "real-platform-run"
        run_root.mkdir()
        output = run_root / "prediction.json"

        completed = execute_blackbox_cli(
            script_path=runner,
            mode="predict",
            input_path=request_path,
            data_dir=data_dir,
            output_path=output,
            platform_input_ids=("api-wind-date-v1",),
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            {
                "request_id": "real-sandbox-001",
                "predict_date": "2025-07-15",
                "feature_date": "2025-07-15",
                "target_date": "2025-07-16",
                "predicted_direction": 0,
            },
        )
        self.assertFalse(
            any(path.name.startswith("embedded-7y-") for path in run_root.iterdir())
        )

    def test_extraction_rejects_existing_parent_symlink(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        run_root.mkdir(mode=0o700)
        outside = self.tempdir / "outside"
        outside.mkdir()
        (run_root / "daily_project").symlink_to(outside, target_is_directory=True)

        with chdir(self.tempdir):
            with self.assertRaisesRegex(ValueError, "unsafe payload path"):
                module._extract_payload(Path("private"))

        self.assertEqual(list(outside.iterdir()), [])

    def test_dirfd_parent_open_rejects_swapped_symlink(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        run_root.mkdir(mode=0o700)
        original = run_root / "daily_project"
        original.mkdir()
        displaced = run_root / "displaced"
        original.rename(displaced)
        outside = self.tempdir / "outside"
        outside.mkdir()
        original.symlink_to(outside, target_is_directory=True)
        root_fd = os.open(run_root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)

        with self.assertRaisesRegex(ValueError, "unsafe payload path"):
            module._open_payload_parent(
                root_fd,
                ("daily_project", "src"),
                "daily_project/src/escape.so",
            )

    def test_post_install_tamper_is_removed_and_fails_closed(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        original_rename = module.os.rename
        tampered = False

        def rename_then_tamper(
            source: str,
            target: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
        ) -> None:
            nonlocal tampered
            original_rename(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            if not tampered:
                tampered = True
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_TRUNC,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(descriptor, b"tampered")
                finally:
                    os.close(descriptor)

        with patch.object(module.os, "rename", side_effect=rename_then_tamper):
            with chdir(self.tempdir):
                with self.assertRaisesRegex(ValueError, "post-install mismatch"):
                    module._extract_payload(Path("private"))

        first_path = module._PAYLOAD_MANIFEST[0]["relative_path"]
        self.assertFalse((run_root / first_path).exists())

    def run_post_install_replacement(
        self, replacement: str
    ) -> subprocess.CompletedProcess[str]:
        runner = self.tempdir / f"{replacement}_runner.py"
        runner.write_text(self.rendered_runner, encoding="utf-8")
        run_root = self.tempdir / f"{replacement}_private"
        first_path = self.first_payload_path
        replacement_statement = (
            f"os.mkfifo({str(run_root / first_path)!r}, 0o600)"
            if replacement == "fifo"
            else "os.mkdir(target, 0o700, dir_fd=dst_dir_fd)"
        )
        command = textwrap.dedent(
            f"""
            import os
            from pathlib import Path
            import runpy
            namespace = runpy.run_path({str(runner)!r})
            original_rename = os.rename
            replaced = False
            def replace_after_rename(source, target, *, src_dir_fd, dst_dir_fd):
                global replaced
                original_rename(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                if not replaced:
                    replaced = True
                    os.unlink(target, dir_fd=dst_dir_fd)
                    {replacement_statement}
            os.rename = replace_after_rename
            namespace["_extract_payload"](Path({run_root.name!r}))
            """
        )
        try:
            completed = subprocess.run(
                [str(BLACKBOX_PYTHON), "-c", command],
                text=True,
                capture_output=True,
                check=False,
                cwd=self.tempdir,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"post-install {replacement} verification blocked")
        self.assertFalse((run_root / first_path).exists())
        return completed

    def test_post_install_fifo_replacement_is_nonblocking_and_removed(self) -> None:
        completed = self.run_post_install_replacement("fifo")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("payload post-install mismatch", completed.stderr)

    def test_post_install_directory_replacement_is_removed(self) -> None:
        completed = self.run_post_install_replacement("directory")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("payload post-install mismatch", completed.stderr)

    def test_extracted_tree_has_private_directory_and_file_modes(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "missing" / "nested" / "private"

        with chdir(self.tempdir):
            relative_payload_root = module._extract_payload(
                Path("missing/nested/private")
            )
        payload_root = self.tempdir / relative_payload_root

        created_roots = [
            self.tempdir / "missing",
            self.tempdir / "missing" / "nested",
            payload_root,
        ]
        for directory in created_roots:
            with self.subTest(directory=directory):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in payload_root.rglob("*"):
            with self.subTest(path=path):
                expected_mode = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)

    def test_real_anchor_modules_import_from_extracted_tree(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        prior_daily = {
            name: loaded
            for name, loaded in sys.modules.items()
            if name == "daily" or name.startswith("daily.")
        }
        with chdir(self.tempdir):
            payload_root = module._extract_payload(Path("private"))
            five, ten = module._anchor_modules(payload_root)

            self.assertIsInstance(
                five.__spec__.loader,
                module._VerifiedPythonLoader,
            )
            self.assertIsInstance(
                ten.__spec__.loader,
                module._VerifiedPythonLoader,
            )
            self.assertTrue(five.__file__.startswith("<embedded-payload:"))
            self.assertTrue(ten.__file__.startswith("<embedded-payload:"))
            self.assertFalse(
                any(
                    path.name.startswith(".embedded-extension-loader-")
                    for path in self.tempdir.iterdir()
                )
            )
        final_daily = {
            name: loaded
            for name, loaded in sys.modules.items()
            if name == "daily" or name.startswith("daily.")
        }
        self.assertEqual(
            set(final_daily),
            set(prior_daily),
        )
        for name, loaded in prior_daily.items():
            self.assertIs(final_daily[name], loaded)

    def test_anchor_import_rejects_payload_replaced_after_extraction(self) -> None:
        cases = (
            "daily_project/src/daily/selected_models/5y10/run.py",
            (
                "daily_project/src/daily/selected_models/5y10/"
                "_run_impl.cpython-313-darwin.so"
            ),
        )
        for index, relative_path in enumerate(cases):
            with self.subTest(relative_path=relative_path):
                module = self.load_generated_module()
                run_root = Path(f"tampered-import-{index}")
                with chdir(self.tempdir):
                    payload_root = module._extract_payload(run_root)
                    target = payload_root / relative_path
                    target.write_bytes(target.read_bytes() + b"\n# replaced\n")
                    target.chmod(0o600)

                    with self.assertRaisesRegex(
                        ValueError,
                        "payload load-time integrity mismatch",
                    ):
                        module._anchor_modules(payload_root)

    def test_anchor_import_rejects_inode_replaced_during_import(self) -> None:
        module = self.load_generated_module()
        relative_path = "daily_project/src/daily/selected_models/5y10/run.py"
        previous_path = list(sys.path)
        previous_daily = {
            name: loaded
            for name, loaded in sys.modules.items()
            if name == "daily" or name.startswith("daily.")
        }
        with chdir(self.tempdir):
            payload_root = module._extract_payload(Path("replaced-during-import"))
            target = payload_root / relative_path
            original_import = module.importlib.import_module

            def import_then_replace(
                name: str,
                package: str | None = None,
            ):
                imported = original_import(name, package)
                if name == "daily.selected_models.10y04.run":
                    replacement = target.with_name(".replacement.py")
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(0o600)
                    replacement.replace(target)
                return imported

            with (
                patch.object(
                    module.importlib,
                    "import_module",
                    side_effect=import_then_replace,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "payload load-time identity changed",
                ),
            ):
                module._anchor_modules(payload_root)
        self.assertEqual(sys.path, previous_path)
        self.assertEqual(
            {
                name: loaded
                for name, loaded in sys.modules.items()
                if name == "daily" or name.startswith("daily.")
            },
            previous_daily,
        )

    def test_anchor_import_never_executes_python_replaced_after_validation(
        self,
    ) -> None:
        module = self.load_generated_module()
        relative_path = "daily_project/src/daily/selected_models/5y10/run.py"
        marker = self.tempdir / "malicious-python-executed"
        with chdir(self.tempdir):
            payload_root = module._extract_payload(Path("fd-python-import"))
            target = payload_root / relative_path
            original_verify = module._verified_payload_identities
            validation_calls = 0

            def validate_then_replace(root_descriptor: int):
                nonlocal validation_calls
                identities = original_verify(root_descriptor)
                validation_calls += 1
                if validation_calls == 1:
                    malicious = (
                        "from pathlib import Path as _TamperPath\n"
                        f"_TamperPath({str(marker)!r}).write_text("
                        "'executed', encoding='utf-8')\n"
                    ).encode("utf-8")
                    replacement = target.with_name(".malicious-run.py")
                    replacement.write_bytes(malicious + target.read_bytes())
                    replacement.chmod(0o600)
                    replacement.replace(target)
                return identities

            with (
                patch.object(
                    module,
                    "_verified_payload_identities",
                    side_effect=validate_then_replace,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "payload load-time",
                ),
            ):
                module._anchor_modules(payload_root)

        self.assertFalse(marker.exists())

    def test_anchor_import_never_executes_python_truncated_after_validation(
        self,
    ) -> None:
        module = self.load_generated_module()
        relative_path = "daily_project/src/daily/selected_models/5y10/run.py"
        marker = self.tempdir / "malicious-python-truncate-executed"
        with chdir(self.tempdir):
            payload_root = module._extract_payload(Path("fd-python-truncate"))
            target = payload_root / relative_path
            original_verify = module._verified_payload_identities
            validation_calls = 0

            def validate_then_truncate(root_descriptor: int):
                nonlocal validation_calls
                identities = original_verify(root_descriptor)
                validation_calls += 1
                if validation_calls == 1:
                    malicious = (
                        "from pathlib import Path as _TamperPath\n"
                        f"_TamperPath({str(marker)!r}).write_text("
                        "'executed', encoding='utf-8')\n"
                    ).encode("utf-8")
                    with target.open("wb") as stream:
                        stream.write(malicious)
                return identities

            with (
                patch.object(
                    module,
                    "_verified_payload_identities",
                    side_effect=validate_then_truncate,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "payload load-time",
                ),
            ):
                module._anchor_modules(payload_root)

        self.assertFalse(marker.exists())

    def test_anchor_import_does_not_open_so_replaced_after_validation(
        self,
    ) -> None:
        module = self.load_generated_module()
        relative_path = (
            "daily_project/src/daily/selected_models/5y10/"
            "_run_impl.cpython-313-darwin.so"
        )
        with chdir(self.tempdir):
            payload_root = module._extract_payload(Path("fd-extension-import"))
            target = payload_root / relative_path
            original_verify = module._verified_payload_identities
            validation_calls = 0

            def validate_then_replace(root_descriptor: int):
                nonlocal validation_calls
                identities = original_verify(root_descriptor)
                validation_calls += 1
                if validation_calls == 1:
                    replacement = target.with_name(".untrusted-extension.so")
                    replacement.write_bytes(b"untrusted replacement")
                    replacement.chmod(0o600)
                    replacement.replace(target)
                return identities

            with (
                patch.object(
                    module,
                    "_verified_payload_identities",
                    side_effect=validate_then_replace,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "payload load-time",
                ),
            ):
                module._anchor_modules(payload_root)


if __name__ == "__main__":
    unittest.main()
