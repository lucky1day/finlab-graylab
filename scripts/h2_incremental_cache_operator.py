"""一次性 H2 Liwei 缓存单日追加与历史补缺 operator。"""

from __future__ import annotations

import argparse
from dataclasses import replace
import importlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pandas as pd

import shared.liwei_0616_phase_a_cache as cache
from harness.authorization import issue_signal_gap_fill_token
from harness.gates.signal_gap_fill_gate import (
    _build_groups,
    _load_frozen_plan,
    run_signal_gap_fill,
)
from scheduler.executor import _record_from_payload, run_configured_scheme
from scheduler.repository import create_engine_from_env
from scheduler.scheme_runner import run_scheme
from scripts.prewarm_liwei_0616_phase_a_cache import PREWARM_SCHEMES
from shared.data_bridge.refresh import DataBridgeRefreshConfig
from shared.input_artifacts import (
    _read_daily_output_csv,
    _read_monthly_output_csv,
)
from shared.models import PredictionRecord


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = (
    PROJECT_ROOT
    / "reports"
    / "harness"
    / "signal-gap-plan"
    / "20260730-h2-fast"
    / "plan-20260729.json"
)
INCREMENTAL_DATE = "2026-07-28"
INCREMENTAL_SUFFIX_DATES = frozenset(
    {"2026-07-24", "2026-07-27", "2026-07-28"}
)
PREDICT_DATE = "2026-07-29"
PARENT_FEATURE_DATE = "2026-07-27"
PARENT_NATIVE_GENERATION_ID = "native-8ad794ac7bfc9505f9a498f1"


def _artifact() -> dict[str, object]:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    return next(
        action["input_authority"]["artifact"]
        for action in plan["actions"]
        if action.get("action") == "GRAY_LIVE_GAP"
        and action["runtime_type"] == "native_adapter"
    )


def _configure_environment(artifact: dict[str, object]) -> None:
    os.environ.update(
        {
            "BOND_NATIVE_INPUT_MODE": "native_generation_v1",
            "BOND_NATIVE_GENERATION_MANIFEST": str(
                artifact["manifest_uri"]
            ),
            "BOND_NATIVE_GENERATION_ID": str(
                artifact["generation_id"]
            ),
            "BOND_NATIVE_GENERATION_MANIFEST_SHA256": str(
                artifact["manifest_sha256"]
            ),
            "BOND_NATIVE_GENERATION_BUSINESS_DATE": str(
                artifact["business_date"]
            ),
            "BOND_NATIVE_GENERATION_FEATURE_DATE": str(
                artifact["feature_date"]
            ),
        }
    )
    for name in (
        "BOND_LIWEI_0616_CACHE_MUTATION_POLICY",
        "BOND_LIWEI_0616_CACHE_USE_QUALIFICATION",
        "BOND_LIWEI_0616_DIRECT_CACHE_RUNTIME_CONTEXT",
    ):
        os.environ.pop(name, None)


def _rollback_invalid_current(
    cache_family: str,
    tenor: str,
) -> None:
    family_root = (
        PROJECT_ROOT
        / "backtest_artifacts"
        / "runtime_cache"
        / "liwei_0616"
        / cache_family
        / tenor.lower()
    )
    current, error = cache._load_current_generation(
        family_root,
        secure=True,
    )
    if current is None:
        raise RuntimeError(f"cannot load cache current: {error}")
    current_watermark = max(
        max(item["test_dates"]) for item in current.caches.values()
    )
    if current_watermark == PARENT_FEATURE_DATE:
        return
    parent_id = current.manifest.get("parent_generation_id")
    if not isinstance(parent_id, str) or not parent_id:
        raise RuntimeError("incremental cache candidate has no parent")
    parent = cache._load_generation_directory(
        family_root / "generations" / parent_id,
        expected_generation_id=parent_id,
        secure=True,
    )
    if max(
        max(item["test_dates"]) for item in parent.caches.values()
    ) != PARENT_FEATURE_DATE:
        raise RuntimeError("cache parent does not end on 2026-07-27")
    cache._switch_current_generation(
        family_root,
        parent,
        secure=True,
    )


def _hybrid_inputs(
    *,
    scheme_id: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, int],
]:
    del weekly_df, monthly_df
    module = importlib.import_module(f"schemes.{scheme_id}.inference")
    tenor = next(
        value
        for value in ("5Y", "7Y", "10Y")
        if value.lower() in str(module.CACHE_FAMILY).lower()
    )
    family_root = (
        PROJECT_ROOT
        / "backtest_artifacts"
        / "runtime_cache"
        / "liwei_0616"
        / str(module.CACHE_FAMILY)
        / tenor.lower()
    )
    current, error = cache._load_current_generation(
        family_root,
        secure=True,
    )
    if current is None:
        raise RuntimeError(f"cannot load parent cache: {error}")
    parent_id = current.manifest.get("parent_generation_id")
    parent = (
        cache._load_generation_directory(
            current.path.parent / parent_id,
            expected_generation_id=parent_id,
            secure=True,
        )
        if current.manifest["input_state"]["native_generation"][
            "feature_date"
        ] == INCREMENTAL_DATE
        else current
    )
    expected_frames = parent.manifest["input_state"]["frames"]
    candidate_root = (
        PROJECT_ROOT
        / "backtest_artifacts"
        / "runtime_inputs"
        / str(module.CACHE_PUBLISHER_CONSUMER_ID)
        / "_scheduled"
        / PARENT_NATIVE_GENERATION_ID
    )
    selected = None
    for directory in sorted(candidate_root.iterdir()):
        if not directory.is_dir():
            continue
        paths = {
            "daily": directory / "daily_output_2026-07-28.csv",
            "weekly": directory / "weekly_output_2026-07-28.csv",
            "monthly": directory / "monthly_output_2026-07-28.csv",
        }
        if not all(path.is_file() for path in paths.values()):
            continue
        parent_daily = _read_daily_output_csv(paths["daily"])
        parent_weekly = pd.read_csv(paths["weekly"])
        parent_weekly["week_id"] = pd.to_numeric(
            parent_weekly["week_id"],
            errors="coerce",
        ).astype("Int64")
        for column in parent_weekly.columns:
            if column != "week_id":
                parent_weekly[column] = pd.to_numeric(
                    parent_weekly[column],
                    errors="coerce",
                )
        parent_monthly = _read_monthly_output_csv(paths["monthly"])
        actual_frames = {
            "daily": cache._frame_generation_state(
                parent_daily,
                "date",
            ),
            "weekly": cache._frame_generation_state(
                parent_weekly,
                "week_id",
            ),
            "monthly": cache._frame_generation_state(
                parent_monthly,
                "month_id",
            ),
        }
        if actual_frames == expected_frames:
            selected = (
                parent_daily,
                parent_weekly,
                parent_monthly,
            )
            break
    if selected is None:
        raise RuntimeError(
            "no exact saved parent runtime input matches cache manifest"
        )
    parent_daily, parent_weekly, parent_monthly = selected
    new_daily = daily_df.loc[
        pd.to_datetime(
            daily_df["date"],
            errors="raise",
        ).dt.strftime("%Y-%m-%d")
        == INCREMENTAL_DATE
    ]
    if len(new_daily) != 1:
        raise RuntimeError(
            "special snapshot must contribute exactly one daily row"
        )
    if list(parent_daily.columns) != list(new_daily.columns):
        raise RuntimeError("daily input schema changed")
    hybrid_daily = pd.concat(
        [parent_daily, new_daily[parent_daily.columns]],
        ignore_index=True,
    )
    proof = parent.manifest["input_state"][
        "effective_auxiliary"
    ]["proof"]
    date_to_week = {
        str(item["date"]): int(item["week_id"])
        for item in proof["date_to_week_entries"]
    }
    return (
        hybrid_daily,
        parent_weekly,
        parent_monthly,
        date_to_week,
    )


def _enable_incremental_only(scheme_id: str) -> None:
    original_resolve = cache._resolve_native_generation_binding
    original_validate = cache._validate_native_generation_binding

    def validate_special(
        binding: object,
        *,
        allow_signal_gap_snapshot: bool = False,
    ) -> object:
        del allow_signal_gap_snapshot
        return original_validate(
            binding,
            allow_signal_gap_snapshot=True,
        )

    def resolve_special(
        binding: object,
        *,
        allow_signal_gap_snapshot: bool = False,
    ) -> object:
        del allow_signal_gap_snapshot
        return original_resolve(
            binding,
            allow_signal_gap_snapshot=True,
        )

    cache._validate_native_generation_binding = validate_special
    cache._resolve_native_generation_binding = resolve_special
    original_projection_decision = cache._projection_build_decision

    def projection_decision(**kwargs: object) -> object:
        decision = original_projection_decision(**kwargs)
        print(
            json.dumps(
                {
                    "event": "H2_INPUT_CHANGE",
                    "decision": decision,
                    "change": cache._public_input_change(
                        kwargs["input_change"]
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        return decision

    cache._projection_build_decision = projection_decision

    module = importlib.import_module(f"schemes.{scheme_id}.inference")
    original_prepare = module.prepare_phase_a_caches

    def guarded_prepare(*args: object, **kwargs: object) -> object:
        special_projection = kwargs[
            "auxiliary_dependency_projection"
        ]
        (
            hybrid_daily,
            hybrid_weekly,
            hybrid_monthly,
            hybrid_date_to_week,
        ) = _hybrid_inputs(
            scheme_id=scheme_id,
            daily_df=kwargs["daily_df"],
            weekly_df=kwargs["weekly_df"],
            monthly_df=kwargs["monthly_df"],
        )
        for item in special_projection.proof[
            "date_to_week_entries"
        ]:
            if item["date"] == INCREMENTAL_DATE:
                hybrid_date_to_week[INCREMENTAL_DATE] = int(
                    item["week_id"]
                )
        projection = module.build_auxiliary_dependency_projection(
            daily_df=hybrid_daily,
            weekly_df=hybrid_weekly,
            monthly_df=hybrid_monthly,
            date_to_week=hybrid_date_to_week,
            prepare_model_frames=module.v31_common.prepare_model_frames,
            build_wkmo_features=module.v31_common.build_wkmo_features,
            proof_files=(
                Path(module.v31_common.__file__),
                Path(module.data_alignment.__file__),
            ),
        )
        kwargs["daily_df"] = hybrid_daily
        kwargs["weekly_df"] = hybrid_weekly
        kwargs["monthly_df"] = hybrid_monthly
        kwargs["auxiliary_dependency_projection"] = projection
        trainer = kwargs["train_missing"]

        def guarded_train(
            baseline: str,
            missing_ranges: tuple[tuple[str, str], ...],
        ) -> object:
            if not missing_ranges or any(
                start != end
                or start not in INCREMENTAL_SUFFIX_DATES
                for start, end in missing_ranges
            ):
                raise RuntimeError(
                    "INCREMENTAL_ONLY_GUARD: "
                    f"{baseline} requested {missing_ranges!r}"
                )
            return trainer(baseline, missing_ranges)

        kwargs["train_missing"] = guarded_train
        kwargs["compare_cold"] = None
        kwargs["qualify_compare_gate"] = None
        kwargs["compare_full_output"] = None
        kwargs["require_compare_gate"] = False
        kwargs["cache_use_qualification"] = None
        return original_prepare(*args, **kwargs)

    module.prepare_phase_a_caches = guarded_prepare


def _enable_hybrid_predict_hit(scheme_id: str) -> None:
    predict_module = importlib.import_module(
        f"schemes.{scheme_id}.predict"
    )
    original_daily = predict_module.build_daily_input_artifact
    original_weekly = predict_module.build_weekly_input_artifact
    original_monthly = predict_module.build_monthly_input_artifact
    prepared: dict[str, object] = {}

    def daily_artifact(**kwargs: object) -> object:
        artifact = original_daily(**kwargs)
        (
            hybrid_daily,
            parent_weekly,
            parent_monthly,
            date_to_week,
        ) = _hybrid_inputs(
            scheme_id=scheme_id,
            daily_df=artifact.dataframe,
            weekly_df=pd.DataFrame({"week_id": []}),
            monthly_df=pd.DataFrame({"month_id": []}),
        )
        prepared.update(
            {
                "daily": hybrid_daily,
                "weekly": parent_weekly,
                "monthly": parent_monthly,
                "date_to_week": date_to_week,
            }
        )
        return replace(artifact, dataframe=hybrid_daily)

    def weekly_artifact(**kwargs: object) -> object:
        artifact = original_weekly(**kwargs)
        return replace(
            artifact,
            dataframe=prepared["weekly"],
        )

    def monthly_artifact(**kwargs: object) -> object:
        artifact = original_monthly(**kwargs)
        return replace(
            artifact,
            dataframe=prepared["monthly"],
        )

    def date_to_week_map(
        daily_df: pd.DataFrame,
        calendar: object,
    ) -> dict[str, int]:
        del daily_df
        mapping = dict(prepared["date_to_week"])
        week_id = calendar.week_id_for_date(INCREMENTAL_DATE)
        if week_id is None:
            raise RuntimeError("cannot map 2026-07-28 to a week")
        mapping[INCREMENTAL_DATE] = int(week_id)
        return mapping

    predict_module.build_daily_input_artifact = daily_artifact
    predict_module.build_weekly_input_artifact = weekly_artifact
    predict_module.build_monthly_input_artifact = monthly_artifact
    predict_module._date_to_week_map = date_to_week_map
    os.environ[
        "BOND_LIWEI_0616_CACHE_MUTATION_POLICY"
    ] = "hit_only"


def _manual_h2_algorithm_runner(
    cfg: object,
    predict_date: str,
    **kwargs: object,
) -> list[PredictionRecord]:
    scheme_id = str(cfg.scheme_id)
    if not scheme_id.startswith("liwei_0616_"):
        return run_configured_scheme(
            cfg,
            predict_date,
            **kwargs,
        )
    completed = subprocess.run(
        [
            "conda",
            "run",
            "--no-capture-output",
            "-n",
            "forecast_env",
            "python",
            "-m",
            "scripts.h2_incremental_cache_operator",
            "--mode",
            "predict",
            "--scheme-id",
            scheme_id,
        ],
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=int(kwargs.get("timeout_sec", 600)),
        check=False,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.splitlines()[-20:])
        raise RuntimeError(
            f"manual predict failed for {scheme_id}: {tail}"
        )
    lines = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip()
    ]
    if not lines:
        raise RuntimeError(
            f"manual predict returned no payload for {scheme_id}"
        )
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"manual predict returned invalid JSON for {scheme_id}"
        ) from exc
    if not isinstance(payload, list):
        raise RuntimeError(
            f"manual predict payload is not a list for {scheme_id}"
        )
    return [_record_from_payload(item) for item in payload]


def _fill_h2() -> None:
    frozen = _load_frozen_plan(PLAN_PATH)
    groups = _build_groups(frozen)
    os.environ["HARNESS_AUTH_SECRET"] = secrets.token_hex(48)
    tokens = [
        issue_signal_gap_fill_token(
            plan_sha256=str(frozen["plan_sha256"]),
            base_scheme_id=group.base_scheme_id,
            predict_date=group.predict_date,
            target_keys=group.expected_target_keys,
            scheme_version=group.scheme_version,
            source_authority=group.source_authority,
            ttl_seconds=900,
            issued_by="h2-manual-incremental-operator",
        )
        for group in groups
    ]
    report = run_signal_gap_fill(
        plan_path=PLAN_PATH,
        authorizations=tokens,
        project_root=PROJECT_ROOT,
        engine_factory=create_engine_from_env,
        databridge_config=DataBridgeRefreshConfig.from_env(),
        algo_env="forecast_env",
        timeout_sec=600,
        algorithm_runner=_manual_h2_algorithm_runner,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("status") not in {"PASSED", "SKIP_PRESENT"}:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheme-id")
    parser.add_argument(
        "--mode",
        choices=("prewarm", "predict", "fill"),
        default="prewarm",
    )
    args = parser.parse_args()
    if args.mode == "fill":
        if args.scheme_id is not None:
            raise ValueError("--scheme-id is invalid with --mode fill")
        _fill_h2()
        return
    if not args.scheme_id:
        raise ValueError("--scheme-id is required")
    if (
        args.mode == "prewarm"
        and args.scheme_id not in PREWARM_SCHEMES
    ):
        raise ValueError("scheme is not an approved cache publisher")
    _configure_environment(_artifact())
    if args.mode == "predict":
        _enable_hybrid_predict_hit(args.scheme_id)
        payload = run_scheme(args.scheme_id, PREDICT_DATE)
        for record in payload:
            extra = dict(record.get("extra") or {})
            extra.update(
                {
                    "historical_input_composition":
                        "parent_generation_plus_current_snapshot_daily_delta",
                    "historical_parent_generation_id":
                        PARENT_NATIVE_GENERATION_ID,
                    "historical_delta_generation_id": str(
                        _artifact()["generation_id"]
                    ),
                    "historical_delta_feature_date":
                        INCREMENTAL_DATE,
                    "vintage_disclaimer":
                        "current_snapshot_as_of_not_historical_vintage",
                }
            )
            record["extra"] = extra
        print(json.dumps(payload, ensure_ascii=False))
        return
    _enable_incremental_only(args.scheme_id)
    module = importlib.import_module(f"schemes.{args.scheme_id}.inference")
    _rollback_invalid_current(
        str(module.CACHE_FAMILY),
        next(
            value
            for value in ("5Y", "7Y", "10Y")
            if value.lower() in str(module.CACHE_FAMILY).lower()
        ),
    )
    records = run_scheme(args.scheme_id, PREDICT_DATE)
    audit = (records[0].get("extra") or {}).get("phase_a_cache") or {}
    print(
        json.dumps(
            {
                "scheme_id": args.scheme_id,
                "status": "OK",
                "build_mode": audit.get("build_mode"),
                "build_reason": audit.get("build_reason"),
                "cache_status": audit.get("status"),
                "watermark": audit.get("watermark"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
