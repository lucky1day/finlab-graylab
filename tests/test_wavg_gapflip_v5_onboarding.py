"""五个 WAVG GAPFLIP V5 交付身份与生产首次登记配置回归测试。"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from scheduler.discovery import load_scheme_config
from scheduler.repository import registry_scheme_id


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION = (
    "以本周周收与周均之差的符号为基准方向,并用元模型误判概率、"
    "曲线扭转、周内尾日反转、信噪比自适应四个分量识别基准失效周"
    "并翻转方向。"
)
SCHEMES = {
    "wavg_1y_gapflip_v5": {
        "tenor": "1Y",
        "scheme_version": "68999585142a",
        "script_sha256": (
            "52184cd05da6cf87d60bcfdf0487306a20f96249792a7d1969fc3d7e89742de5"
        ),
        "metadata_sha256": (
            "f92ad296693010ab61a4373a54d640206089d4d79b387a84d3bb35701bc333d4"
        ),
    },
    "wavg_3y_gapflip_v5": {
        "tenor": "3Y",
        "scheme_version": "faba245acaef",
        "script_sha256": (
            "f406f8630f3978763112f3bd0187162c807f0789fff33373b61ecb6393ab1d62"
        ),
        "metadata_sha256": (
            "bf2372e6728d05fe612e24a16d8b5a7c56d4b931cd7ec446b021f921976ca17d"
        ),
    },
    "wavg_5y_gapflip_v5": {
        "tenor": "5Y",
        "scheme_version": "63ed1291f9d4",
        "script_sha256": (
            "b01c3ea65654b0ad3ccb45529ed6ec704c9fe1aa69ff5a96d37bc8f1cb842ebb"
        ),
        "metadata_sha256": (
            "9c05e61f3f96d3bbef76df1a1d91e8a1a8e67e7b13d02d9bf42073f27899842b"
        ),
    },
    "wavg_7y_gapflip_v5": {
        "tenor": "7Y",
        "scheme_version": "21951d955f44",
        "script_sha256": (
            "65ab2d3539a8dda967a5d0dc909ece3a1d68aec989f25b44f460a74b7d4d04e0"
        ),
        "metadata_sha256": (
            "bcdf46a87712fd8b567daf9b35f317db253e6e8bcb95fca1b7a6b8df45259547"
        ),
    },
    "wavg_10y_gapflip_v5": {
        "tenor": "10Y",
        "scheme_version": "c1e5a9db6097",
        "script_sha256": (
            "a558589e5f2dc0a41bd335074068e6049911de59f515a26ae80312c6cc71d0b7"
        ),
        "metadata_sha256": (
            "35e264bfb6a819f8a7420cad98e1001487f1caa96313e63db7bcd90c6dea16fb"
        ),
    },
}


def _sha256(path: Path | None) -> str:
    if path is None:
        raise AssertionError("delivery path must be present")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WavgGapflipV5OnboardingTests(unittest.TestCase):
    def test_exact_delivery_identity_and_production_draft_are_frozen(
        self,
    ) -> None:
        """逐方案冻结交付字节、业务契约、复合身份与首次登记生命周期。"""
        for scheme_id, expected in SCHEMES.items():
            with self.subTest(scheme_id=scheme_id):
                config = load_scheme_config(
                    PROJECT_ROOT / "schemes" / scheme_id / "config.yaml"
                )
                tenor = str(expected["tenor"])

                self.assertEqual(config.scheme_id, scheme_id)
                self.assertEqual(
                    config.scheme_version,
                    expected["scheme_version"],
                )
                self.assertEqual(
                    _sha256(config.delivery_script),
                    expected["script_sha256"],
                )
                self.assertEqual(
                    _sha256(config.delivery_metadata),
                    expected["metadata_sha256"],
                )
                self.assertEqual(config.contract_version, "1.0")
                self.assertEqual(config.name, "GAPFLIP_V5")
                self.assertEqual(config.description, DESCRIPTION)
                self.assertEqual(config.algorithm_version, "5.0.0")
                self.assertEqual(config.runtime_type, "blackbox_v2")
                self.assertEqual(config.input_source, "data_bridge_current")
                self.assertEqual(config.runtime_profile, "blackbox-v2-v1")
                self.assertEqual(
                    config.data_schema_version,
                    "data-bridge-v1",
                )
                self.assertEqual(
                    config.platform_inputs,
                    ("api-wind-date-v1",),
                )
                self.assertEqual(config.frequency, "weekly")
                self.assertEqual(config.task_type, "weekly_average")
                self.assertEqual(config.horizon, 1)
                self.assertEqual(config.tenors, [tenor])
                self.assertEqual(
                    config.target_rule,
                    (
                        "target_week_average_yield_vs_"
                        "feature_week_average_yield"
                    ),
                )
                self.assertEqual(config.schedule.cron, "30 11 * * 6")
                self.assertEqual(
                    config.schedule.timezone,
                    "Asia/Shanghai",
                )
                self.assertEqual(config.schedule.timeout_sec, 3600)
                self.assertEqual(
                    registry_scheme_id(
                        config.scheme_id,
                        config.horizon,
                        tenor,
                    ),
                    f"{scheme_id}__h1__{tenor}",
                )
                self.assertEqual(config.status, "paused")
                self.assertEqual(config.version_status, "draft")


if __name__ == "__main__":
    unittest.main()
