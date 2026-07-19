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

    def test_old_native_entry_paths_are_redirect_only(self) -> None:
        redirects = (
            DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_T0.md",
            DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_SOP.md",
            DOCS_ROOT / "sop" / "SCHEME_POST_ONBOARDING_TEST_SOP.md",
            DOCS_ROOT / "SCHEME_PARADIGM.md",
        )
        for path in redirects:
            text = path.read_text(encoding="utf-8")
            self.assertIn("HISTORICAL", text)
            self.assertIn("禁止用于新增方案", text)
            self.assertLess(len(text.splitlines()), 30)

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
        self.assertIn("所有后续新增", (DOCS_ROOT / "onboarding" / "README.md").read_text(encoding="utf-8"))

    def test_blackbox_production_boundary_is_explicit(self) -> None:
        readiness = (DOCS_ROOT / "blackbox_v2" / "PRODUCTION_READINESS.md").read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        self.assertIn("BLOCKED_DRAFT", readiness)
        for item in ("PR-01", "PR-02", "PR-03", "PR-04", "PR-05", "PR-06", "PR-07", "PR-08"):
            self.assertIn(item, readiness)
        self.assertIn("shadow + paused", platform)
        self.assertIn("不得执行 `activate` 或 `live`", platform)

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
