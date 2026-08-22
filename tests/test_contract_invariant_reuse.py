"""契约不变量的复用查找：命中条件与 fail-closed 语义。

上游交付契约明文要求的不变量是交付代码的结构性质。交付字节与平台代码都未变时，
它们不可能改变，因此按指纹复用既有判定；任何不确定都必须回到完整套件。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from harness.context import GateContext
from harness.persistence import find_passed_gate_run

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


def _engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE t_harness_runs ("
                "harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, "
                "git_commit TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE t_harness_gate_results ("
                "harness_run_id TEXT, gate_name TEXT, status TEXT, "
                "finished_at TEXT)"
            )
        )
    return engine


def _seed(engine, *, run_id, scheme_version, commit, gate_status):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO t_harness_runs VALUES (:r, 'demo', :v, :c)"),
            {"r": run_id, "v": scheme_version, "c": commit},
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "VALUES (:r, 'compare', :s, '2026-08-22 10:00:00')"
            ),
            {"r": run_id, "s": gate_status},
        )


def _ctx(tmp_path: Path, engine=None) -> GateContext:
    return GateContext(
        scheme_id="demo",
        predict_date="2026-08-22",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        engine_factory=(lambda: engine) if engine is not None else None,
    )


def _released(commit: str = COMMIT):
    """模拟 release 注入的 commit。"""
    return patch.dict(os.environ, {"BFL_RELEASE_COMMIT": commit}, clear=False)


def test_matching_fingerprint_returns_the_prior_run(tmp_path: Path) -> None:
    engine = _engine()
    _seed(engine, run_id="hr_ok", scheme_version="v1", commit=COMMIT, gate_status="passed")
    with _released():
        found = find_passed_gate_run(
            _ctx(tmp_path, engine), gate_name="compare", scheme_version="v1"
        )
    assert found == "hr_ok"


@pytest.mark.parametrize(
    "seeded_version, seeded_commit, seeded_status, reason",
    [
        pytest.param("v2", COMMIT, "passed", "交付字节变了", id="scheme_version_changed"),
        pytest.param("v1", OTHER_COMMIT, "passed", "平台代码变了", id="platform_code_changed"),
        pytest.param("v1", COMMIT, "failed", "此前未通过", id="prior_run_failed"),
    ],
)
def test_no_reuse_when_anything_differs(
    tmp_path: Path, seeded_version, seeded_commit, seeded_status, reason
) -> None:
    engine = _engine()
    _seed(
        engine,
        run_id="hr_other",
        scheme_version=seeded_version,
        commit=seeded_commit,
        gate_status=seeded_status,
    )
    with _released():
        found = find_passed_gate_run(
            _ctx(tmp_path, engine), gate_name="compare", scheme_version="v1"
        )
    assert found is None, reason


def test_empty_history_runs_the_full_suite(tmp_path: Path) -> None:
    with _released():
        found = find_passed_gate_run(
            _ctx(tmp_path, _engine()), gate_name="compare", scheme_version="v1"
        )
    assert found is None


def test_missing_engine_fails_closed(tmp_path: Path) -> None:
    with _released():
        assert (
            find_passed_gate_run(_ctx(tmp_path), gate_name="compare", scheme_version="v1")
            is None
        )


def test_database_error_fails_closed(tmp_path: Path) -> None:
    def boom():
        raise RuntimeError("database unavailable")

    ctx = GateContext(
        scheme_id="demo",
        predict_date="2026-08-22",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        engine_factory=boom,
    )
    with _released():
        assert find_passed_gate_run(ctx, gate_name="compare", scheme_version="v1") is None


def test_blank_scheme_version_fails_closed(tmp_path: Path) -> None:
    engine = _engine()
    _seed(engine, run_id="hr_ok", scheme_version="", commit=COMMIT, gate_status="passed")
    with _released():
        assert (
            find_passed_gate_run(
                _ctx(tmp_path, engine), gate_name="compare", scheme_version="  "
            )
            is None
        )


def test_unresolvable_commit_fails_closed(tmp_path: Path) -> None:
    """生产目标下缺 BFL_RELEASE_COMMIT 时不得复用。"""
    engine = _engine()
    _seed(engine, run_id="hr_ok", scheme_version="v1", commit=COMMIT, gate_status="passed")
    environ = {k: v for k, v in os.environ.items() if k != "BFL_RELEASE_COMMIT"}
    environ["BFL_DEPLOYMENT_TARGET"] = "aliyun-gray"
    with patch.dict(os.environ, environ, clear=True):
        assert (
            find_passed_gate_run(
                _ctx(tmp_path, engine), gate_name="compare", scheme_version="v1"
            )
            is None
        )


def test_gate_name_is_scoped(tmp_path: Path) -> None:
    """compare 的复用不得被别的 gate 的通过记录满足。"""
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO t_harness_runs VALUES ('hr_u', 'demo', 'v1', :c)"),
            {"c": COMMIT},
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "VALUES ('hr_u', 'unit', 'passed', '2026-08-22 10:00:00')"
            )
        )
    with _released():
        assert (
            find_passed_gate_run(
                _ctx(tmp_path, engine), gate_name="compare", scheme_version="v1"
            )
            is None
        )


def test_other_scheme_does_not_satisfy_reuse(tmp_path: Path) -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO t_harness_runs VALUES ('hr_x', 'another', 'v1', :c)"),
            {"c": COMMIT},
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "VALUES ('hr_x', 'compare', 'passed', '2026-08-22 10:00:00')"
            )
        )
    with _released():
        assert (
            find_passed_gate_run(
                _ctx(tmp_path, engine), gate_name="compare", scheme_version="v1"
            )
            is None
        )


# --------------------------------------------------------------------------
# Gate 结构：哪些必跑、哪些可复用
# --------------------------------------------------------------------------


def _compare_source() -> tuple[str, str]:
    import inspect

    from harness.blackbox_v2.gates import BlackboxCompareGate

    whole = inspect.getsource(BlackboxCompareGate)
    invariants = inspect.getsource(
        BlackboxCompareGate._verify_contract_invariants
    )
    return whole, invariants


def test_the_nine_contract_fits_all_live_behind_the_reuse_check() -> None:
    """契约不变量的全量拟合必须都在可复用的 helper 里，不得留在必跑路径上。"""
    whole, invariants = _compare_source()
    always_run = whole.replace(invariants, "")

    # baseline 冒烟是唯一留在必跑路径上的拟合
    assert always_run.count("run_blackbox_predict(") == 1
    assert "run_blackbox_backtest(" not in always_run

    # 契约不变量的拟合全在 helper 内：3 次 predict + 3 次 backtest
    assert "run_blackbox_predict(" not in invariants
    assert invariants.count("run_blackbox_backtest(") == 3


def test_every_contract_assertion_stays_inside_the_reusable_helper() -> None:
    import re

    _, invariants = _compare_source()
    # Evidence 在 helper 里是多行书写，先归一化空白再比对
    flat = re.sub(r"\s+", "", invariants)
    for key in (
        "batch_split_invariant",
        "request_order_invariant",
    ):
        assert f'Evidence("{key}"' in flat, key


def test_platform_input_verification_is_never_skipped() -> None:
    """平台输入哈希校验不含拟合，必须每轮执行，不参与复用。"""
    whole, invariants = _compare_source()
    always_run = whole.replace(invariants, "")
    assert "_verify_runtime_platform_files(" in always_run


def test_reused_run_id_is_recorded_in_evidence() -> None:
    whole, _ = _compare_source()
    assert 'Evidence("contract_invariants_reused_from"' in whole


def test_reuse_defaults_to_running_the_full_suite() -> None:
    """`reused_from is None` 才是默认分支——查不到就跑完整套件。"""
    whole, _ = _compare_source()
    assert "if reused_from is not None" in whole
    assert "_verify_contract_invariants(" in whole
