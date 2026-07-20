from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from shared.blackbox_v2.contracts import METADATA_FIELDS, REQUEST_FIELDS, RESULT_FIELDS, TASK_COMBINATIONS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PROJECT_ROOT / "docs"
UPSTREAM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md"
PLATFORM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md"
SOP_INDEX = DOCS_ROOT / "sop" / "README.md"


class OnboardingDocumentationTests(unittest.TestCase):
    def test_root_agent_instructions_are_byte_identical(self) -> None:
        self.assertEqual(
            (PROJECT_ROOT / "AGENTS.md").read_bytes(),
            (PROJECT_ROOT / "CLAUDE.md").read_bytes(),
        )

    def test_policy_declares_blackbox_as_only_new_scheme_runtime(self) -> None:
        raw = json.loads((PROJECT_ROOT / "deploy" / "onboarding_policy_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["policy_version"], "1.0")
        self.assertEqual(raw["new_scheme_runtime_type"], "blackbox_v2")
        self.assertEqual(raw["native_v1_mode"], "maintenance_only")
        self.assertEqual(len(raw["legacy_native_scheme_ids"]), 29)

    def test_blackbox_sops_match_machine_contract(self) -> None:
        for path in (UPSTREAM_SOP, PLATFORM_SOP):
            text = path.read_text(encoding="utf-8")

            for field in sorted(METADATA_FIELDS):
                self.assertRegex(text, rf"(?<![a-z_]){re.escape(field)}(?![a-z_])")
            for field in REQUEST_FIELDS:
                self.assertRegex(text, rf"(?<![a-z_]){re.escape(field)}(?![a-z_])")
            for field in RESULT_FIELDS:
                self.assertRegex(text, rf"(?<![a-z_]){re.escape(field)}(?![a-z_])")

            for task_type, (horizon, target_rule, _frequency) in TASK_COMBINATIONS.items():
                row = rf"\| `{re.escape(task_type)}` \| {horizon} \| `{re.escape(target_rule)}` \|"
                self.assertRegex(text, row)

    def test_upstream_sop_contains_no_platform_internal_state(self) -> None:
        text = UPSTREAM_SOP.read_text(encoding="utf-8")
        banned = (
            "/Users/",
            "05:30",
            "Registry",
            "shadow",
            "scheduler",
            "weekly_10y_lgbm_point_v1",
            "hr_",
            "snapshot-",
        )
        for marker in banned:
            self.assertNotIn(marker, text)

    def test_upstream_metadata_name_is_task_scoped_and_concise(self) -> None:
        text = UPSTREAM_SOP.read_text(encoding="utf-8")

        self.assertIn('"name": "LIQ_EXCESS_A_W252_L7"', text)
        self.assertIn("不得重复 `target_tenor`", text)
        self.assertIn("不得重复 `task_type`", text)
        self.assertIn("不得追加“方向预测”", text)
        self.assertNotIn('"name": "10年国债收益率周频点位方向预测"', text)

    def test_blackbox_full_range_backtest_contract(self) -> None:
        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        self.assertIn("单批上限不是完整回测总量上限", upstream)
        self.assertIn("--backtest-start-date", platform)
        self.assertIn("默认 `2025-01-01`", platform)
        self.assertIn("全部批次成功后", platform)
        self.assertIn("单一事务", platform)
        self.assertIn("current snapshot as-of replay", platform)
        self.assertIn("CLI 缺少 `--predict-date` 时拒绝签发", platform)
        self.assertIn("durable summary", platform)

    def test_blackbox_platform_sop_defines_live_boundary_and_frontend_acceptance(self) -> None:
        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            "gray_target_start",
            "2026-06-01",
            "target_date >= gray_target_start",
            "target_date < gray_target_start",
            "prediction_phase=gray_live",
            "prediction_phase=scheduled_live",
            "实盘发出起点",
            "phase_ranges",
            "待验证",
            "不参与回测截断",
        ):
            self.assertIn(marker, platform)

        for platform_only_marker in ("gray_target_start", "phase_ranges", "deployed_at"):
            self.assertNotIn(platform_only_marker, upstream)

    def test_old_native_entry_paths_are_redirect_only(self) -> None:
        redirects = (
            DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_T0.md",
            DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_SOP.md",
            DOCS_ROOT / "sop" / "SCHEME_POST_ONBOARDING_TEST_SOP.md",
            DOCS_ROOT / "archive" / "SCHEME_PARADIGM.md",
        )
        for path in redirects:
            text = path.read_text(encoding="utf-8")
            self.assertIn("HISTORICAL", text)
            self.assertIn("禁止用于新增方案", text)
            self.assertLess(len(text.splitlines()), 30)

    def test_sop_index_covers_the_entire_directory(self) -> None:
        text = SOP_INDEX.read_text(encoding="utf-8")
        actual = {
            path.name
            for path in SOP_INDEX.parent.glob("*.md")
            if path.name != SOP_INDEX.name
        }
        indexed = set(re.findall(r"\]\(([^)/#]+\.md)(?:#[^)]+)?\)", text))

        self.assertEqual(indexed, actual)
        self.assertIn("算法侧只需要阅读这一份", text)
        for status in ("CURRENT", "LEGACY_MAINTENANCE", "HISTORICAL"):
            self.assertIn(f"`{status}`", text)

    def test_docs_root_contains_only_navigation_and_current_status(self) -> None:
        self.assertEqual(
            {path.name for path in DOCS_ROOT.glob("*.md")},
            {"README.md", "CURRENT_STATUS.md"},
        )

    def test_each_document_directory_has_a_complete_index(self) -> None:
        missing_indexes: list[str] = []
        incomplete_indexes: list[str] = []

        directories = {DOCS_ROOT}
        directories.update(path.parent for path in DOCS_ROOT.rglob("*.md"))
        for directory in sorted(directories):
            markdown_files = {
                path.name
                for path in directory.glob("*.md")
                if path.name != "README.md"
            }
            child_doc_dirs = {
                child.name
                for child in directory.iterdir()
                if child.is_dir() and any(child.rglob("*.md"))
            }
            index = directory / "README.md"
            if not index.exists():
                missing_indexes.append(str(directory.relative_to(PROJECT_ROOT)))
                continue

            text = index.read_text(encoding="utf-8")
            for marker in ("**文档状态**", "**目标读者**", "**最后核验日期**"):
                if marker not in text:
                    incomplete_indexes.append(
                        f"{index.relative_to(PROJECT_ROOT)} missing metadata {marker}"
                    )

            targets = {
                raw.split("#", 1)[0].strip("<>")
                for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)
            }
            for filename in sorted(markdown_files):
                if filename not in targets:
                    incomplete_indexes.append(
                        f"{index.relative_to(PROJECT_ROOT)} missing {filename}"
                    )
            for child in sorted(child_doc_dirs):
                if f"{child}/README.md" not in targets:
                    incomplete_indexes.append(
                        f"{index.relative_to(PROJECT_ROOT)} missing {child}/README.md"
                    )

        self.assertEqual(missing_indexes, [])
        self.assertEqual(incomplete_indexes, [])

    def test_current_status_is_a_concise_snapshot(self) -> None:
        text = (DOCS_ROOT / "CURRENT_STATUS.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 120)
        self.assertIn("历史状态记录", text)

    def test_navigation_does_not_duplicate_dynamic_runtime_state(self) -> None:
        navigation = (
            (DOCS_ROOT / "README.md").read_text(encoding="utf-8")
            + (DOCS_ROOT / "onboarding" / "README.md").read_text(encoding="utf-8")
        )
        for marker in (
            "当前数量",
            "29 个",
            "1 个 shadow",
            "最多进入 `shadow + paused`",
            "当前生命周期上限",
            "hr_",
            "snapshot-",
        ):
            self.assertNotIn(marker, navigation)

    def test_current_docs_use_the_specific_authorization_boundary(self) -> None:
        current_docs = (
            DOCS_ROOT / "architecture" / "ARCHITECTURE.md",
            DOCS_ROOT / "architecture" / "SCHEME_CONTRACT.md",
            DOCS_ROOT / "architecture" / "HARNESS_ARCHITECTURE.md",
            DOCS_ROOT / "architecture" / "BLACKBOX_V2_PLATFORM.md",
            DOCS_ROOT / "product" / "GRAY_LAB_USER_MANUAL.md",
        )
        banned = (
            "当前 Blackbox V2 正式能力止于 `shadow + paused`",
            "当前禁止 activate/live",
            "JSON Result 解析器当前接受字符串方向",
            "Contract 1.0 禁止 activate/live 目前是操作政策",
            "Blackbox V2 当前最多进入 `shadow + paused`",
            "Blackbox V2 当前最多为 `shadow + paused`",
            "当前 Blackbox 平台能力止于 `shadow + paused`",
        )
        for path in current_docs:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                for marker in banned:
                    self.assertNotIn(marker, text)
                self.assertIn("专项授权", text)

    def test_current_entry_points_do_not_route_new_schemes_to_native(self) -> None:
        entry_points = (
            PROJECT_ROOT / "README.md",
            DOCS_ROOT / "README.md",
            PROJECT_ROOT / "AGENTS.md",
            PROJECT_ROOT / "CLAUDE.md",
            DOCS_ROOT / "onboarding" / "README.md",
        )
        for path in entry_points:
            text = path.read_text(encoding="utf-8")
            self.assertIn("Blackbox V2", text)
            self.assertNotRegex(text, r"新增(?:原生|Native).*SCHEME_ONBOARDING")
        self.assertIn("后续新增方案唯一入口", (DOCS_ROOT / "onboarding" / "README.md").read_text(encoding="utf-8"))

    def test_blackbox_production_boundary_is_explicit(self) -> None:
        readiness = (DOCS_ROOT / "blackbox_v2" / "PRODUCTION_READINESS.md").read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        self.assertIn("BLOCKED_DRAFT", readiness)
        for item in ("PR-01", "PR-02", "PR-03", "PR-04", "PR-05", "PR-06", "PR-07", "PR-08"):
            self.assertIn(item, readiness)
        self.assertIn("shadow + paused", platform)
        self.assertIn("不自动授予生产运行权限", platform)
        self.assertIn("具体方案专项授权", platform)
        self.assertIn("不得把某个试验方案的授权外推", platform)

    def test_all_markdown_relative_links_resolve(self) -> None:
        broken: list[str] = []
        link_pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
        for path in PROJECT_ROOT.rglob("*.md"):
            if ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for raw_target in link_pattern.findall(text):
                target = raw_target.strip().split("#", 1)[0].strip("<>")
                if not target or target.startswith(("http://", "https://", "mailto:", "/", "#")):
                    continue
                if not (path.parent / target).resolve().exists():
                    broken.append(f"{path.relative_to(PROJECT_ROOT)} -> {target}")
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
