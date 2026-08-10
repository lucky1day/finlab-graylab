from __future__ import annotations

import re
import unittest
from pathlib import Path

from shared.blackbox_v2.contracts import (
    OPTIONAL_METADATA_FIELDS,
    REQUEST_FIELDS,
    REQUIRED_METADATA_FIELDS,
    RESULT_FIELDS,
    TASK_COMBINATIONS,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PROJECT_ROOT / "docs"
ONBOARDING_README = DOCS_ROOT / "onboarding" / "README.md"
UPSTREAM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md"
PLATFORM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md"
SOP_INDEX = DOCS_ROOT / "sop" / "README.md"


class OnboardingDocumentationTests(unittest.TestCase):
    def test_root_agent_instructions_are_byte_identical(self) -> None:
        self.assertEqual(
            (PROJECT_ROOT / "AGENTS.md").read_bytes(),
            (PROJECT_ROOT / "CLAUDE.md").read_bytes(),
        )

    def test_new_scheme_entry_points_select_blackbox_v2(self) -> None:
        for path in (
            PROJECT_ROOT / "README.md",
            DOCS_ROOT / "README.md",
            PROJECT_ROOT / "AGENTS.md",
            PROJECT_ROOT / "CLAUDE.md",
            ONBOARDING_README,
        ):
            with self.subTest(path=path.relative_to(PROJECT_ROOT)):
                text = path.read_text(encoding="utf-8")
                self.assertIn("Blackbox V2", text)
                self.assertNotRegex(text, r"新增(?:原生|Native).*SCHEME_ONBOARDING")

    def test_blackbox_sops_match_machine_contract(self) -> None:
        for path in (UPSTREAM_SOP, PLATFORM_SOP):
            text = path.read_text(encoding="utf-8")
            for field in sorted(REQUIRED_METADATA_FIELDS | OPTIONAL_METADATA_FIELDS):
                self.assertRegex(text, rf"(?<![a-z_]){re.escape(field)}(?![a-z_])")
            for field in (*REQUEST_FIELDS, *RESULT_FIELDS):
                self.assertRegex(text, rf"(?<![a-z_]){re.escape(field)}(?![a-z_])")
            for task_type, (horizon, target_rule, _frequency) in TASK_COMBINATIONS.items():
                self.assertRegex(
                    text,
                    rf"\| `{re.escape(task_type)}` \| {horizon} \| "
                    rf"`{re.escape(target_rule)}` \|",
                )

    def test_upstream_sop_excludes_platform_state_and_local_paths(self) -> None:
        text = UPSTREAM_SOP.read_text(encoding="utf-8")
        for marker in (
            "/Users/",
            "HARNESS_AUTH_SECRET",
            "t_scheme_registry",
            "launchctl",
            "launchd_one_shot",
            "daily_ledger",
        ):
            self.assertNotIn(marker, text)

    def test_onboarding_navigation_lists_reusable_test_suites(self) -> None:
        text = ONBOARDING_README.read_text(encoding="utf-8")
        for marker in (
            "## 可复用测试矩阵",
            "test_active_scheme_contracts.py",
            "test_active_blackbox_conformance.py",
            "test_blackbox_v2_harness_gates.py",
            "test_factor_lab_dashboard_api.py",
            "python -m pytest -q",
        ):
            self.assertIn(marker, text)

        referenced_tests = set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", text))
        self.assertTrue(referenced_tests)
        missing = [
            name
            for name in sorted(referenced_tests)
            if not (PROJECT_ROOT / "tests" / name).is_file()
        ]
        self.assertEqual(missing, [])

    def test_sop_index_covers_the_entire_directory(self) -> None:
        text = SOP_INDEX.read_text(encoding="utf-8")
        actual = {
            path.name
            for path in SOP_INDEX.parent.glob("*.md")
            if path.name != SOP_INDEX.name
        }
        indexed = set(re.findall(r"\]\(([^)/#]+\.md)(?:#[^)]+)?\)", text))
        self.assertEqual(indexed, actual)

    def test_docs_root_contains_only_current_entry_points(self) -> None:
        self.assertEqual(
            {path.name for path in DOCS_ROOT.glob("*.md")},
            {"README.md", "CURRENT_STATUS.md", "TODO.md"},
        )

    def test_current_documents_only_link_to_current_authority(self) -> None:
        violations: list[str] = []
        for path in DOCS_ROOT.rglob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "**文档状态**：`CURRENT`" not in text:
                continue
            for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = raw.strip().split("#", 1)[0].strip("<>")
                if not target or target.startswith(
                    ("http://", "https://", "mailto:", "/", "#")
                ):
                    continue
                resolved = (path.parent / target).resolve()
                try:
                    relative = resolved.relative_to(DOCS_ROOT)
                except ValueError:
                    continue
                if relative.parts[:1] == ("superpowers",) or (
                    relative.parts[:2] == ("records", "status")
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)} -> {relative}"
                    )
                    continue
                if resolved.suffix != ".md" or not resolved.is_file():
                    continue
                target_text = resolved.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                if any(
                    marker in target_text
                    for marker in (
                        "**文档状态**：`BLOCKED_DRAFT`",
                        "**文档状态**：`HISTORICAL`",
                    )
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)} -> {relative}"
                    )
        self.assertEqual(violations, [])

    def test_each_document_directory_has_a_complete_index(self) -> None:
        missing: list[str] = []
        documentation_paths = tuple(
            path
            for path in DOCS_ROOT.rglob("*.md")
            if "superpowers" not in path.relative_to(DOCS_ROOT).parts
        )
        directories = {DOCS_ROOT, *(path.parent for path in documentation_paths)}
        for directory in sorted(directories):
            index = directory / "README.md"
            if not index.is_file():
                missing.append(str(directory.relative_to(PROJECT_ROOT)))
                continue
            text = index.read_text(encoding="utf-8")
            targets = {
                raw.split("#", 1)[0].strip("<>")
                for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)
            }
            expected = {
                path.name
                for path in directory.glob("*.md")
                if path.name != "README.md"
            }
            expected.update(
                f"{child.name}/README.md"
                for child in directory.iterdir()
                if child.name != "superpowers"
                and child.is_dir()
                and any(child.rglob("*.md"))
            )
            for target in sorted(expected - targets):
                missing.append(f"{index.relative_to(PROJECT_ROOT)} -> {target}")
        self.assertEqual(missing, [])

    def test_all_markdown_relative_links_resolve(self) -> None:
        broken: list[str] = []
        for path in PROJECT_ROOT.rglob("*.md"):
            if ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = raw.strip().split("#", 1)[0].strip("<>")
                if not target or target.startswith(
                    ("http://", "https://", "mailto:", "/", "#")
                ):
                    continue
                if not (path.parent / target).resolve().exists():
                    broken.append(f"{path.relative_to(PROJECT_ROOT)} -> {target}")
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
