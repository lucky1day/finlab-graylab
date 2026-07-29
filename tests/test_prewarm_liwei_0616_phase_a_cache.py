from __future__ import annotations

import ast
import unittest
from pathlib import Path

from scripts.prewarm_liwei_0616_phase_a_cache import PREWARM_SCHEMES, prewarm


SCHEMES_ROOT = Path(__file__).resolve().parents[1] / "schemes"


def _declared_cache_families() -> dict[str, str]:
    """静态解析各方案 inference.py 的模块级 CACHE_FAMILY，避免导入算法依赖。"""
    families: dict[str, str] = {}
    for inference_path in sorted(SCHEMES_ROOT.glob("*/inference.py")):
        module = ast.parse(
            inference_path.read_text(encoding="utf-8"),
            filename=str(inference_path),
        )
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "CACHE_FAMILY"
                for target in node.targets
            ):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                families[inference_path.parent.name] = node.value.value
    return families


class PrewarmLiwei0616PhaseACacheTests(unittest.TestCase):
    """liwei_0616 cache 预热入口测试。"""

    def test_prewarm_covers_every_declared_cache_family(self) -> None:
        """预热清单必须逐 family 覆盖，不能按 tenor 近似。

        同一 tenor 下可以有多个不共享 Phase-A cache 的 family；漏掉任何一个
        都会让该 family 只能在日频窗口内冷重建。
        """
        families = _declared_cache_families()
        self.assertTrue(families, "未发现任何声明 CACHE_FAMILY 的方案")

        unknown = sorted(set(PREWARM_SCHEMES) - set(families))
        self.assertEqual(
            unknown,
            [],
            f"PREWARM_SCHEMES 引用了不存在或未声明 CACHE_FAMILY 的方案: {unknown}",
        )

        covered = {families[scheme_id] for scheme_id in PREWARM_SCHEMES}
        missing = sorted(set(families.values()) - covered)
        self.assertEqual(
            missing,
            [],
            f"以下 cache family 没有预热代表，将只能冷重建: {missing}",
        )

        self.assertEqual(
            len(PREWARM_SCHEMES),
            len(covered),
            "每个 cache family 只应有一个预热代表，避免重复冷启动开销",
        )

    def test_prewarm_runs_representative_schemes_without_repository(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_run_scheme(scheme_id: str, predict_date: str) -> list[dict]:
            calls.append((scheme_id, predict_date))
            return [
                {
                    "extra": {
                        "phase_a_cache": {
                            "status": "hit",
                            "watermark": "2026-07-09",
                            "missing_dates": [],
                        }
                    }
                }
            ]

        result = prewarm("2026-07-10", run_scheme_fn=fake_run_scheme)

        self.assertEqual(calls, [(scheme_id, "2026-07-10") for scheme_id in PREWARM_SCHEMES])
        self.assertEqual(
            [item["status"] for item in result],
            ["hit"] * len(PREWARM_SCHEMES),
        )
        self.assertEqual([item["scheme_id"] for item in result], list(PREWARM_SCHEMES))

    def test_prewarm_rejects_missing_prediction_or_cache_audit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "returned no prediction records"):
            prewarm("2026-07-10", run_scheme_fn=lambda _scheme, _day: [])

        with self.assertRaisesRegex(RuntimeError, "missing phase_a_cache audit"):
            prewarm("2026-07-10", run_scheme_fn=lambda _scheme, _day: [{"extra": {}}])


if __name__ == "__main__":
    unittest.main()
