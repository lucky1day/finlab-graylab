from __future__ import annotations

import json
import plistlib
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
UPSTREAM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md"
PLATFORM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md"
SOP_INDEX = DOCS_ROOT / "sop" / "README.md"
DEPLOY_README = PROJECT_ROOT / "deploy" / "README.md"
DAILY_SIGNAL_SLA = DOCS_ROOT / "architecture" / "DAILY_SIGNAL_SLA.md"
TODO = DOCS_ROOT / "TODO.md"
TEN_Y_T5_RECORD = (
    DOCS_ROOT
    / "blackbox_v2"
    / "records"
    / "GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md"
)
TEN_Y_T5_SCHEME_IDS = (
    "ten_y_t5_maj3_k3_ic_static_v1",
    "ten_y_t5_maj4_k3_ic_static_v1",
    "ten_y_t5_maj4_k3_ic_yearly_v1",
    "ten_y_t5_say_k5_sharpe_static_v1",
)


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

            for field in sorted(REQUIRED_METADATA_FIELDS | OPTIONAL_METADATA_FIELDS):
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
            "t_scheme_registry",
            "gray_backfill_write",
            "phase_ranges",
            "launchctl",
        )
        for marker in banned:
            self.assertNotIn(marker, text)

    def test_upstream_sop_is_self_contained_and_explains_databridge_download(self) -> None:
        text = UPSTREAM_SOP.read_text(encoding="utf-8")

        for marker in (
            "唯一需要阅读的人类文档",
            "DATABRIDGE_API_BASE_URL",
            "DATABRIDGE_API_USERNAME",
            "DATABRIDGE_API_PASSWORD",
            "export/csv/",
            "frequency=日",
            "frequency=周",
            "frequency=月",
            "sample_data/daily_output.csv",
            "sample_data/weekly_output.csv",
            "sample_data/monthly_output.csv",
            "data_bridge_v1_schema.json",
            "f959777b7f251937b6364843a81d8eb696072ca7671b1306c368aa0f3cf735dc",
            "真实 DataBridge 数据",
            "接口烟雾测试",
            "最低兼容字段基线",
            "新增业务列",
            "按字段名",
            "忽略未使用",
            "Python 3.13.12",
            "numpy 2.3.5",
            "pandas 2.3.3",
            "scikit-learn 1.8.0",
            "lightgbm 4.6.0",
            "xgboost 3.1.3",
            "catboost 1.2.8",
            "import csv",
            "csv.reader",
            'encoding="utf-8-sig"',
        ):
            self.assertIn(marker, text)

        for marker in (
            "`data-bridge-v1` 固定列数",
            "columns=774",
            "columns=575",
            "columns=123",
            "完整检查保证",
            "Runtime Profile |",
            "Conda 环境 |",
            "`osx-arm64`",
            "720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd",
            "conda run --no-capture-output",
            "from importlib.metadata import version",
        ):
            self.assertNotIn(marker, text)

        self.assertNotRegex(
            text,
            r"\[[^\]]+\]\([^)]*\.md(?:#[^)]*)?\)",
        )

    def test_databridge_sample_docs_do_not_freeze_point_in_time_column_counts(self) -> None:
        readme = (
            DOCS_ROOT / "blackbox_v2" / "data_bridge_v1" / "README.md"
        ).read_text(encoding="utf-8")
        manifest = json.loads(
            (
                DOCS_ROOT / "blackbox_v2" / "data_bridge_v1" / "manifest.json"
            ).read_text(encoding="utf-8")
        )

        self.assertIn("最低兼容字段基线", readme)
        self.assertIn("新增业务列", readme)
        self.assertNotIn("V1 列数", readme)
        for item in manifest["files"].values():
            self.assertNotIn("columns", item)

    def test_platform_sop_treats_databridge_schema_as_additive_baseline(self) -> None:
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            "最低兼容字段基线",
            "新增业务列",
            "相对顺序",
            "实际列数",
            "Snapshot identity",
        ):
            self.assertIn(marker, platform)
        self.assertNotIn("按冻结 Schema 生成", platform)

    def test_daily_rollout_docs_define_monotonic_epoch_cutover(self) -> None:
        deploy = DEPLOY_README.read_text(encoding="utf-8")
        architecture = DAILY_SIGNAL_SLA.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            "bootout legacy V2 preflight",
            "root-owned append-only epoch chain",
            "只能追加更高 epoch",
            "daily_coordinator_epoch_operator.py",
            "hard-link no-clobber",
            "仓库 rollout",
            "candidate v2",
            "launchctl print",
            "scripts/apply_migrations.py --apply",
        ):
            self.assertIn(marker, deploy)

        for marker in (
            "bootout legacy preflight",
            "machine-global append-only chain",
            "允许受控回滚",
            "hard-link no-clobber",
            "仓库 rollout",
            "candidate v2",
            "scripts/apply_migrations.py --apply",
        ):
            self.assertIn(marker, architecture)

        for text in (deploy, architecture, platform):
            self.assertIn(
                "BOND_DAILY_COORDINATOR_MODE=ledger",
                text,
            )
            self.assertIn("--check-only", text)
            self.assertIn("--dry-run", text)

        self.assertIn(
            "BOND_DAILY_COORDINATOR_MODE=legacy",
            platform,
        )

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
            "实盘预测目标区间",
            "phase_ranges",
            "待验证",
            "不参与回测截断",
        ):
            self.assertIn(marker, platform)

        self.assertIn("scheduled_live.start_target_date", platform)
        self.assertNotIn("实盘预测目标区间", upstream)

        for platform_only_marker in ("gray_target_start", "phase_ranges", "deployed_at"):
            self.assertNotIn(platform_only_marker, upstream)

    def test_blackbox_platform_sop_defines_authorized_gray_backfill(self) -> None:
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        for marker in (
            "gray-backfill",
            "gray_backfill_write",
            "historical_as_of_replay",
            "current_snapshot_as_of_not_historical_vintage",
            "普通 `live` Gate",
            "fresh-only",
            "insert-only",
            "重复 target",
            "gray_target_start",
        ):
            self.assertIn(marker, platform)

    def test_blackbox_description_is_optional_and_recommended(self) -> None:
        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            '"schema_version": "1.0"',
            '"description":',
            "可选",
            "强烈建议",
            "主要输入",
            "窗口或规则",
            "模型类型",
            "方向形成方式",
            "不阻断",
        ):
            self.assertIn(marker, upstream)

        for marker in (
            "description",
            "warnings",
            "不阻断 Intake",
            "t_scheme_registry.description",
            "/api/schemes",
            "/api/backtests/factor-lab",
            "已有方案",
            "不修改",
            "前端备注",
        ):
            self.assertIn(marker, platform)

    def test_launchd_defaults_keep_one_coherent_legacy_control_plane(self) -> None:
        rollout = json.loads(
            (
                PROJECT_ROOT
                / "deploy"
                / "daily_coordinator_rollout_v1.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            rollout,
            {
                "schema_version": "daily-coordinator-rollout-v1",
                "mode": "legacy",
            },
        )

        preflight_path = (
            PROJECT_ROOT
            / "deploy"
            / "launchd"
            / "com.bond-factor-lab.v2-preflight.plist"
        )
        with preflight_path.open("rb") as handle:
            preflight = plistlib.load(handle)

        self.assertEqual(preflight["Label"], "com.bond-factor-lab.v2-preflight")
        self.assertNotIn("KeepAlive", preflight)
        self.assertNotIn("RunAtLoad", preflight)
        self.assertNotIn("Disabled", preflight)
        self.assertEqual(
            {
                (item["Hour"], item["Minute"])
                for item in preflight["StartCalendarInterval"]
            },
            {(6, 0), (6, 30), (6, 35), (7, 0)},
        )
        self.assertIn(
            "scheduler.v2_daily_preflight",
            preflight["ProgramArguments"],
        )
        self.assertEqual(
            preflight["EnvironmentVariables"][
                "BOND_DAILY_COORDINATOR_MODE"
            ],
            "legacy",
        )
        self.assertEqual(
            preflight["EnvironmentVariables"]["DATABRIDGE_REFRESH_START"],
            "06:00",
        )
        self.assertEqual(
            preflight["EnvironmentVariables"]["DATABRIDGE_REFRESH_DEADLINE"],
            "07:00",
        )

        scheduler_path = (
            PROJECT_ROOT
            / "deploy"
            / "launchd"
            / "com.bond-factor-lab.scheduler.plist"
        )
        with scheduler_path.open("rb") as handle:
            scheduler = plistlib.load(handle)
        scheduler_env = scheduler["EnvironmentVariables"]
        self.assertEqual(
            scheduler_env["BOND_DAILY_COORDINATOR_MODE"],
            "legacy",
        )
        self.assertIn("BFL_SOURCE_DB_CONFIG_PATH", scheduler_env)
        self.assertIn("BFL_SOURCE_DB_CONFIG_ROOT", scheduler_env)
        source_database_root = Path(
            scheduler_env["BFL_SOURCE_DB_CONFIG_ROOT"]
        )
        source_database_config = scheduler_env["BFL_SOURCE_DB_CONFIG_PATH"]
        self.assertTrue(source_database_root.is_absolute())
        self.assertTrue(Path(source_database_config).is_absolute())
        self.assertEqual(
            Path(source_database_config).parent,
            source_database_root,
        )
        self.assertEqual(
            source_database_root,
            Path("/Users/macstudio0/.config/bond-factor-lab"),
        )
        self.assertEqual(
            Path(source_database_config).name,
            "source-runtime-db.json",
        )
        self.assertTrue(
            {
                "BOND_DB_USER",
                "BOND_DB_PASSWORD",
                "BOND_DB_DSN",
                "SOURCE_DB_USER",
                "SOURCE_DB_PASSWORD",
                "SOURCE_DB_DSN",
            }.isdisjoint(scheduler_env),
        )
        self.assertEqual(scheduler_env["DATABRIDGE_REFRESH_START"], "06:30")
        self.assertEqual(scheduler_env["DATABRIDGE_REFRESH_DEADLINE"], "06:55")

        backend_path = (
            PROJECT_ROOT
            / "deploy"
            / "launchd"
            / "com.bond-factor-lab.backend.plist"
        )
        with backend_path.open("rb") as handle:
            backend = plistlib.load(handle)
        self.assertEqual(
            backend["EnvironmentVariables"]["BOND_DAILY_COORDINATOR_MODE"],
            "legacy",
        )

    def test_scheduler_source_binding_runbook_keeps_secrets_out_of_plist(
        self,
    ) -> None:
        deploy = DEPLOY_README.read_text(encoding="utf-8")
        for marker in (
            "BFL_SOURCE_DB_CONFIG_ROOT",
            "BFL_SOURCE_DB_CONFIG_PATH",
            "/Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json",
            "chmod 700 /Users/macstudio0/.config/bond-factor-lab",
            "chmod 600 /Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json",
            "不得把用户名、密码或 DSN 写入 plist",
            "SHOW GRANTS FOR CURRENT_USER()",
            "9 张必需源表",
            "生产预检禁止执行 DDL/DML",
            "7 个日频存储根",
            "backtest_artifacts/runtime_cache/liwei_0616",
            "不会静默 chmod",
            "certify_generation_native_daily.py",
            "certify_daily_0629_source.py",
            "BFL_DAILY_0629_CERTIFY_REAL=1",
            "runner_persistence_tables_full_content",
            "PYTHONPYCACHEPREFIX",
            "live_source_no_persist_observed_watermark",
            "提交后必须在 clean candidate",
            "--no-persist",
            "clean detached worktree",
            "POSIX semaphore",
            'pwd -P',
            "伪造历史 snapshot clock",
            "不得修改或重启 BondProjectPro",
        ):
            self.assertIn(marker, deploy)

    def test_platform_sop_defines_occurrence_timeline_and_isolation(self) -> None:
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        for marker in (
            "06:30",
            "07:00",
            "07:45",
            "08:00",
            "08:30",
            "+0/+2/+4/+6",
            "不重启 scheduler",
            "旧 generation",
            "scheduled_live",
        ):
            self.assertIn(marker, platform)

        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        for marker in (
            "06:00",
            "06:30",
            "06:35",
            "07:00",
        ):
            self.assertNotIn(marker, upstream)
        self.assertIn("统一 DataBridge 导出逻辑", upstream)
        self.assertIn("整体原子发布", upstream)
        self.assertNotIn("v2-scheduler-gate-v1", upstream)

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

    def test_docs_root_contains_navigation_current_status_and_todo(self) -> None:
        self.assertEqual(
            {path.name for path in DOCS_ROOT.glob("*.md")},
            {"README.md", "CURRENT_STATUS.md", "TODO.md"},
        )

    def test_todo_prioritizes_the_10y_batch_and_platform_dependencies(self) -> None:
        text = TODO.read_text(encoding="utf-8")
        p0, _ = text.split("## P1", maxsplit=1)

        for scheme_id in TEN_Y_T5_SCHEME_IDS:
            self.assertIn(scheme_id, text)
        self.assertNotIn("one_y_t5_", p0)

        dependencies = (
            "migration017 namespace digest",
            "migration017 real MySQL recovery",
            "canonical migration runner",
            "execute-only replay",
            "real 17 Native + 4 formal V2 21/25",
            "scheduler resource/recovery/atomic commit/capacity",
            "three 0629 generation adapters",
            "migrations018/019/020",
            "archive/disk",
            "exact 20 forced-cold +20 revision/suffix",
            "gray/formal admission",
            "automatic gray scheduling",
            "formal promotion",
        )
        positions = [text.index(marker) for marker in dependencies]
        self.assertEqual(positions, sorted(positions))

    def test_10y_gray_onboarding_record_binds_the_exact_batch(self) -> None:
        self.assertTrue(TEN_Y_T5_RECORD.exists())
        if not TEN_Y_T5_RECORD.exists():
            return

        text = TEN_Y_T5_RECORD.read_text(encoding="utf-8")
        self.assertIn("authorized/manual-onboarding-pending-revalidation", text)
        for scheme_id in TEN_Y_T5_SCHEME_IDS:
            self.assertIn(scheme_id, text)

    def test_10y_batch_scope_allows_manual_gray_phases_but_not_scheduler(self) -> None:
        todo = TODO.read_text(encoding="utf-8")
        p0, _ = todo.split("## P1", maxsplit=1)
        record = TEN_Y_T5_RECORD.read_text(encoding="utf-8")

        for text in (p0, record):
            for marker in (
                "controlled activate",
                "persistent backtest",
                "manual gray_live",
            ):
                self.assertIn(marker, text)

        self.assertIn("截至本记录尚未执行", record)
        self.assertIn("automatic gray scheduling", todo)
        self.assertIn("scheduled_live", record)
        self.assertIn("旧 generation fallback", record)

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
