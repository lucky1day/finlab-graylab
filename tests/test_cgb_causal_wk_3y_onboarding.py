"""cgb_causal_wk_3y 交付身份与平台配置回归测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from scheduler.discovery import load_scheme_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "schemes/cgb_causal_wk_3y/config.yaml"
SCRIPT_SHA256 = (
    "849c2fe6e24343230ddcc17212979f981144cb03f44d0ff9b1457f96fa7d0383"
)
METADATA_SHA256 = (
    "84b09972a583314c401228957a83d9195821ed0fbab93040cfb76e2e3c4bbb38"
)


def _sha256(path: Path | None) -> str:
    assert path is not None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_delivery_bytes_and_exact_version_are_frozen() -> None:
    """交付字节或 canonical 平台配置漂移必须使 exact version 回归失败。"""
    config = load_scheme_config(CONFIG_PATH)

    assert config.scheme_version == "4b8db29b2f74"
    assert _sha256(config.delivery_script) == SCRIPT_SHA256
    assert _sha256(config.delivery_metadata) == METADATA_SHA256


def test_weekly_platform_contract_uses_authoritative_calendar() -> None:
    """冻结权威周历、周度任务契约和初始生命周期状态。"""
    config = load_scheme_config(CONFIG_PATH)

    assert config.runtime_type == "blackbox_v2"
    assert config.input_source == "data_bridge_current"
    assert config.runtime_profile == "blackbox-v2-v1"
    assert config.data_schema_version == "data-bridge-v1"
    assert config.platform_inputs == ("api-wind-date-v1",)
    assert config.schedule.cron == "30 11 * * 6"
    assert config.schedule.timezone == "Asia/Shanghai"
    assert config.schedule.timeout_sec == 3600
    assert config.task_type == "weekly_point"
    assert config.horizon == 1
    assert config.tenors == ["3Y"]
    assert config.target_rule == (
        "target_week_end_yield_vs_feature_week_end_yield"
    )
    assert config.algorithm_version == "2.0.0-rc1"
