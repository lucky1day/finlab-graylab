from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


class InputArtifactTests(unittest.TestCase):
    def test_input_artifact_path_sanitizes_scheme_and_predict_date(self) -> None:
        from shared.input_artifacts import input_artifact_path

        with tempfile.TemporaryDirectory() as tmpdir:
            path = input_artifact_path(
                scheme_id="daily/scheme:bad",
                frequency="daily",
                predict_date="2026/06/05",
                output_root=Path(tmpdir),
            )

        self.assertEqual(path.name, "daily_output_2026-06-05.csv")
        self.assertEqual(path.parent.name, "daily_scheme_bad")

    def test_daily_input_artifact_delegates_to_unified_data_service_file(self) -> None:
        from shared.input_artifacts import build_daily_input_artifact

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-05"]), "TB0YWI0C": [2.1]})
        engine = object()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("shared.input_artifacts.data_service", create=True) as daily_service:
                daily_service.build_daily_output_from_db.return_value = daily_df
                daily_service.save_daily_output.side_effect = lambda df, path: df.to_csv(path, index=False)
                artifact = build_daily_input_artifact(
                    scheme_id="t1_daily",
                    predict_date="2026-06-05",
                    start_date="2019-06-10",
                    end_date="2026-06-05",
                    engine=engine,
                    output_root=root,
                )
                artifact_exists = artifact.path.exists()

        self.assertEqual(artifact.scheme_id, "t1_daily")
        self.assertEqual(artifact.frequency, "daily")
        self.assertEqual(artifact.source, "shared_data_service_daily")
        self.assertTrue(str(artifact.path).endswith("t1_daily/daily_output_2026-06-05.csv"))
        self.assertEqual(artifact.dataframe["date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-06-05"])
        self.assertEqual(artifact.dataframe["TB0YWI0C"].tolist(), [2.1])
        kwargs = daily_service.build_daily_output_from_db.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "2019-06-10")
        self.assertEqual(kwargs["end_date"], "2026-06-05")
        self.assertIs(kwargs["engine"], engine)
        self.assertIs(daily_service.save_daily_output.call_args.args[0], daily_df)
        self.assertEqual(daily_service.save_daily_output.call_args.args[1], artifact.path)
        self.assertTrue(artifact_exists)

    def test_weekly_input_artifact_delegates_to_unified_data_service_file(self) -> None:
        from shared.input_artifacts import build_weekly_input_artifact

        weekly_df = pd.DataFrame({"week_id": [202621], "TB0YWI3C": [1.7]})
        engine = object()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("shared.input_artifacts.data_service", create=True) as data_service:
                data_service.build_weekly_output_from_db.return_value = weekly_df
                data_service.save_weekly_output.side_effect = lambda df, path: df.to_csv(path, index=False)
                artifact = build_weekly_input_artifact(
                    scheme_id="weekly_10y_d_overlay",
                    predict_date="2026-06-06",
                    end_week=202621,
                    engine=engine,
                    output_root=root,
                )
                self.assertTrue(artifact.path.exists())

        self.assertEqual(artifact.scheme_id, "weekly_10y_d_overlay")
        self.assertEqual(artifact.frequency, "weekly")
        self.assertEqual(artifact.source, "shared_data_service_weekly")
        self.assertTrue(str(artifact.path).endswith("weekly_10y_d_overlay/weekly_output_2026-06-06.csv"))
        self.assertEqual(artifact.dataframe["week_id"].tolist(), [202621])
        self.assertEqual(artifact.dataframe["TB0YWI3C"].tolist(), [1.7])
        kwargs = data_service.build_weekly_output_from_db.call_args.kwargs
        self.assertEqual(kwargs["end_week"], 202621)
        self.assertIs(kwargs["engine"], engine)
        self.assertIs(data_service.save_weekly_output.call_args.args[0], weekly_df)
        self.assertEqual(data_service.save_weekly_output.call_args.args[1], artifact.path)

    def test_weekly_input_artifact_rejects_legacy_daily_close_fallback_flag(self) -> None:
        from shared.input_artifacts import build_weekly_input_artifact

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "include_daily_weekly_close_fallback"):
                build_weekly_input_artifact(
                    scheme_id="weekly_10y_d_overlay",
                    predict_date="2026-06-06",
                    include_daily_weekly_close_fallback=True,
                    output_root=Path(tmpdir),
                )

    def test_weekly_input_artifact_rejects_legacy_end_date_filter(self) -> None:
        from shared.input_artifacts import build_weekly_input_artifact

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "end_date"):
                build_weekly_input_artifact(
                    scheme_id="weekly_10y_d_overlay",
                    predict_date="2026-06-06",
                    end_date="2026-06-05",
                    output_root=Path(tmpdir),
                )


class WeeklyPredictInputArtifactTests(unittest.TestCase):
    def test_weekly_predict_uses_common_input_artifact_layer(self) -> None:
        from schemes.weekly_10y_d_overlay import predict

        weekly_df = pd.DataFrame(
            [
                {
                    "week_id": 202621,
                    "TB0YWI3C": 1.7,
                    "TB1YWI3C": 1.1,
                    "TB5YWI3C": 1.4,
                }
            ]
        )
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly_output.csv"), source="test")
        engine = SimpleNamespace(dispose=lambda: None)
        model_result = SimpleNamespace(
            week_id=202621,
            frequency="weekly",
            prediction_column="d_pred_label",
            probability_column="d_prob_up",
            pred_label=-1,
            prob_up=0.28,
            source="weekly_test_model",
        )

        with patch.object(predict, "create_sqlalchemy_engine", return_value=engine):
            with patch.object(predict, "read_source_week_id_for_date", return_value=202621):
                with patch.object(predict, "build_weekly_input_artifact", return_value=artifact) as build:
                    with patch.object(predict, "predict_w10y", return_value=model_result):
                        records = predict.run("2026-06-06")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].scheme_id, "weekly_10y_d_overlay")
        self.assertEqual(records[0].predicted_direction, -1)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["scheme_id"], "weekly_10y_d_overlay")
        self.assertEqual(kwargs["predict_date"], "2026-06-06")
        self.assertEqual(kwargs["end_week"], 202621)
        self.assertNotIn("end_date", kwargs)
        self.assertNotIn("include_daily_weekly_close_fallback", kwargs)
        self.assertIs(kwargs["engine"], engine)


if __name__ == "__main__":
    unittest.main()
