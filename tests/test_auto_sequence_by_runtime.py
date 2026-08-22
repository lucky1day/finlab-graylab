"""`all` 自动段按 runtime_type 分开：Blackbox 精简，Native 保持不变。"""

from __future__ import annotations

import pytest

from harness.registry import (
    AUTO_SEQUENCE,
    BLACKBOX_AUTO_SEQUENCE,
    sequence_for_stage,
)


def test_native_sequence_is_unchanged() -> None:
    """Native 的 CompareGate 是 benchmark 对比，与 Blackbox 无关，其序列不得改动。"""
    assert AUTO_SEQUENCE == ["static", "input", "unit", "dry-run", "compare", "backtest"]
    assert sequence_for_stage("all", runtime_type="native_adapter") == AUTO_SEQUENCE


def test_blackbox_sequence_drops_the_duplicated_gates() -> None:
    """dry-run 与 no-persist backtest 的断言已被 Blackbox CompareGate 更强地覆盖。"""
    assert BLACKBOX_AUTO_SEQUENCE == ["static", "input", "unit", "compare"]
    assert sequence_for_stage("all", runtime_type="blackbox_v2") == BLACKBOX_AUTO_SEQUENCE
    assert "dry-run" not in BLACKBOX_AUTO_SEQUENCE
    assert "backtest" not in BLACKBOX_AUTO_SEQUENCE


def test_prefix_stages_follow_the_runtime_sequence() -> None:
    assert sequence_for_stage("unit", runtime_type="blackbox_v2") == ["static", "input", "unit"]
    assert sequence_for_stage("dry-run", runtime_type="native_adapter") == [
        "static",
        "input",
        "unit",
        "dry-run",
    ]


def test_stage_outside_the_runtime_sequence_is_rejected() -> None:
    """Blackbox 不再把 dry-run / backtest 作为 onboard 阶段；它们仍是独立 gate 命令。"""
    for stage in ("dry-run", "backtest"):
        with pytest.raises(ValueError, match="unsupported onboard stage"):
            sequence_for_stage(stage, runtime_type="blackbox_v2")


def test_default_runtime_type_is_native_for_backward_compatibility() -> None:
    assert sequence_for_stage("all") == AUTO_SEQUENCE


def test_blackbox_compare_carries_the_former_dry_run_evidence() -> None:
    """dry-run 并入 compare 后，其证据与结果文件必须由 compare 原样产出。"""
    import inspect

    from harness.blackbox_v2.gates import BlackboxCompareGate

    source = inspect.getsource(BlackboxCompareGate)
    for key in ("prediction_record", "result_path", "business_tables_written"):
        assert f'Evidence("{key}"' in source, key
    assert "dry_run_prediction_record.json" in source


def test_passed_all_verification_derives_its_gate_set_from_the_sequence() -> None:
    """写死过一次就出过事：自动段缩到四段后，依赖 _verify_passed_all 的三条副作用
    路径（shadow-register / backtest --persist / activate）全部被
    「missing=['backtest','dry-run']」阻断。此处钉死它必须派生而非复制。"""
    import inspect

    from harness.blackbox_v2.gates import _verify_passed_all

    source = inspect.getsource(_verify_passed_all)
    assert "BLACKBOX_AUTO_SEQUENCE" in source
    for stale in ('"dry-run"', '"backtest"'):
        assert stale not in source, f"{stale} 不得再出现在期望集合里"


def test_blackbox_sequence_has_no_algorithm_only_gates() -> None:
    """Blackbox 自动段只保留平台自己要验的东西。"""
    from harness.registry import BLACKBOX_AUTO_SEQUENCE

    assert BLACKBOX_AUTO_SEQUENCE == ["static", "input", "unit", "compare"]
