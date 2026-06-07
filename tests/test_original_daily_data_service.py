from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pandas as pd


class OriginalDailyDataServiceTests(unittest.TestCase):
    def test_build_original_daily_output_writes_and_reads_csv_boundary(self) -> None:
        from shared.original_daily_data_service import build_original_daily_output_from_db

        class FakeOriginalService:
            DAILY_TARGETS = ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C")

            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            def build_daily_output_from_db(self, *, start_date: str, end_date: str, engine: object) -> pd.DataFrame:
                self.calls.append(("build", (start_date, end_date, engine)))
                return pd.DataFrame(
                    {
                        "date": pd.to_datetime(["2026-01-02"]),
                        "TB0YWI0C": [2.2345],
                    }
                )

            def save_daily_output(self, df: pd.DataFrame, path: str | Path) -> Path:
                self.calls.append(("save", Path(path)))
                output_path = Path(path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(output_path, index=False)
                return output_path

        service = FakeOriginalService()
        engine = object()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "daily_output.csv"
            with patch("shared.original_daily_data_service._load_original_data_service", return_value=service):
                result = build_original_daily_output_from_db(
                    start_date="2026-01-01",
                    end_date="2026-01-03",
                    engine=engine,
                    output_path=output_path,
                )

        self.assertEqual(service.calls[0], ("build", ("2026-01-01", "2026-01-03", engine)))
        self.assertEqual(service.calls[1], ("save", output_path))
        self.assertEqual(result["date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-01-02"])
        self.assertEqual(result["TB0YWI0C"].tolist(), [2.2345])

    def test_original_daily_targets_are_upstream_targets(self) -> None:
        from shared.original_daily_data_service import load_original_daily_targets

        self.assertEqual(load_original_daily_targets(), ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C"))


class DailyPredictAdapterDataServiceTests(unittest.TestCase):
    def test_t1_daily_predict_uses_original_daily_output_bridge(self) -> None:
        from schemes.t1_daily import predict

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-04"]), "TB0YWI0C": [2.0]})
        fake_config = SimpleNamespace(tenor="10Y", window=240)
        fake_result = SimpleNamespace(
            rdate="2026-06-05",
            target_date="2026-06-05",
            feature_date="2026-06-04",
            pred_label=1,
            prob_up=0.6,
            base_pred=1,
            base_decision="model",
            vote_sum=1,
            decision="test",
            threshold_used=0.5,
            train_start="2025-01-01",
            train_end="2026-06-03",
        )

        artifact = SimpleNamespace(dataframe=daily_df, path=Path("/tmp/t1_daily_output.csv"), source="test")
        with patch.object(predict, "build_daily_input_artifact", return_value=artifact) as build:
            with patch.object(predict, "TENOR_CONFIGS", {"daily_10y": fake_config}):
                with patch.object(predict, "_configured_tenors", return_value={"10Y"}):
                    with patch.object(predict, "predict_latest_for_config", return_value=fake_result) as predict_latest:
                        records = predict.run("2026-06-05")

        self.assertEqual(len(records), 1)
        self.assertIs(predict_latest.call_args.args[0], daily_df)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["end_date"], "2026-06-05")
        self.assertEqual(kwargs["scheme_id"], "t1_daily")
        self.assertEqual(kwargs["predict_date"], "2026-06-05")

    def test_t5_daily_predict_uses_original_daily_output_bridge(self) -> None:
        from schemes.t5_daily import predict

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-04"]), "TB0YWI0C": [2.0]})
        fake_result = SimpleNamespace(
            feature_date="2026-06-04",
            vote_pred=-1,
            confidence=0.4,
            model_version="test",
            model_pred=-1,
            vote_sum=-1,
            signal_sum=0,
            threshold=0.5,
            decision="test",
            vote_signals={},
        )

        artifact = SimpleNamespace(dataframe=daily_df, path=Path("/tmp/t5_daily_output.csv"), source="test")
        with patch.object(predict, "build_daily_input_artifact", return_value=artifact) as build:
            with patch.object(predict, "predict_latest_for_module", return_value=fake_result) as predict_latest:
                with patch.object(predict, "_target_date_from_feature_date", return_value="2026-06-12"):
                    records = predict.run("2026-06-05")

        self.assertEqual(len(records), 4)
        self.assertIs(predict_latest.call_args.args[1], daily_df)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["end_date"], "2026-06-05")
        self.assertEqual(kwargs["scheme_id"], "t5_daily")
        self.assertEqual(kwargs["predict_date"], "2026-06-05")


class ReproductionDailyDataServiceTests(unittest.TestCase):
    def test_db_aligned_daily_uses_original_data_service_when_upstream_mode(self) -> None:
        from backtests import reproduction

        canonical = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.2345],
            }
        )
        generated = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.2345],
            }
        )
        engine = object()

        artifact = SimpleNamespace(dataframe=generated, path=Path("/tmp/reproduction_daily_output.csv"), source="test")
        with patch.object(reproduction, "build_daily_input_artifact", return_value=artifact) as build:
            _, aligned = reproduction.build_db_aligned_daily(csv_df=canonical, engine=engine, upstream_mode=True)

        self.assertEqual(aligned["TB0YWI0C"].tolist(), [2.2345])
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "2026-01-02")
        self.assertEqual(kwargs["end_date"], "2026-01-02")
        self.assertIs(kwargs["engine"], engine)
        self.assertEqual(kwargs["scheme_id"], "model_muti_0529_daily")


if __name__ == "__main__":
    unittest.main()
