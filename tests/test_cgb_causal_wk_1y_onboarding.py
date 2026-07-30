"""cgb_causal_wk_1y 交付身份与平台配置回归测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from scheduler.discovery import load_scheme_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "schemes/cgb_causal_wk_1y/config.yaml"
SCRIPT_SHA256 = (
    "90f3abcc1501eb7173c706fc2ad5d76fda89bf9004c88ee976b98378bbb6d450"
)
METADATA_SHA256 = (
    "efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b"
)


def _sha256(path: Path | None) -> str:
    assert path is not None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_delivery_bytes_and_exact_version_are_frozen() -> None:
    """交付字节或 canonical 平台配置漂移必须使 exact version 回归失败。"""
    config = load_scheme_config(CONFIG_PATH)

    assert config.scheme_version == "05022a0eeec7"
    assert _sha256(config.delivery_script) == SCRIPT_SHA256
    assert _sha256(config.delivery_metadata) == METADATA_SHA256


def test_weekly_platform_contract_uses_authoritative_calendar() -> None:
    """遗漏权威周历或改错周度任务契约必须被回归捕获。"""
    config = load_scheme_config(CONFIG_PATH)

    assert config.platform_inputs == ("api-wind-date-v1",)
    assert config.schedule.cron == "30 11 * * 6"
    assert config.schedule.timezone == "Asia/Shanghai"
    assert config.task_type == "weekly_point"
    assert config.horizon == 1
    assert config.tenors == ["1Y"]
    assert config.target_rule == (
        "target_week_end_yield_vs_feature_week_end_yield"
    )
