from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHORITATIVE_DOCS = [
    PROJECT_ROOT / "docs" / "architecture" / "ARCHITECTURE.md",
    PROJECT_ROOT / "docs" / "CURRENT_STATUS.md",
    PROJECT_ROOT / "docs" / "architecture" / "SCHEME_CONTRACT.md",
    PROJECT_ROOT / "docs" / "sop" / "BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md",
]


class OneYearTargetDocsTests(unittest.TestCase):
    def test_authoritative_docs_show_1y_as_business_visible_target(self) -> None:
        for path in AUTHORITATIVE_DOCS:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("1Y", text)
                self.assertNotIn("1Y` 可作为因子输入，但不作为当前展示目标", text)
                self.assertNotIn("DB 中 `1Y` 回测格被前端目标注册表隐藏", text)
                self.assertNotIn("当前展示 `3Y/5Y/7Y/10Y` 四个国债活跃目标", text)

    def test_scheme_contract_tenor_set_includes_1y(self) -> None:
        text = (
            PROJECT_ROOT / "docs" / "architecture" / "SCHEME_CONTRACT.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`1Y/3Y/5Y/7Y/10Y`", text)
        self.assertNotIn("`3Y/5Y/7Y/10Y` ...", text)


if __name__ == "__main__":
    unittest.main()
