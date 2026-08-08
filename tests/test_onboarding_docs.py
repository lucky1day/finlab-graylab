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
PRODUCTION_SCHEDULING_GOVERNANCE = (
    DOCS_ROOT / "architecture" / "PRODUCTION_SCHEDULING_GOVERNANCE.md"
)
UPSTREAM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md"
PLATFORM_SOP = DOCS_ROOT / "sop" / "BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md"
BLACKBOX_ARCHITECTURE = (
    DOCS_ROOT / "architecture" / "BLACKBOX_V2_PLATFORM.md"
)
SCHEME_CONTRACT = DOCS_ROOT / "architecture" / "SCHEME_CONTRACT.md"
HARNESS_ARCHITECTURE = (
    DOCS_ROOT / "architecture" / "HARNESS_ARCHITECTURE.md"
)
SOP_INDEX = DOCS_ROOT / "sop" / "README.md"
DEPLOY_README = PROJECT_ROOT / "deploy" / "README.md"
DAILY_SIGNAL_SLA = DOCS_ROOT / "architecture" / "DAILY_SIGNAL_SLA.md"
TODO = DOCS_ROOT / "TODO.md"
CURRENT_STATUS = DOCS_ROOT / "CURRENT_STATUS.md"
ARCHITECTURE = DOCS_ROOT / "architecture" / "ARCHITECTURE.md"
CODE_ARCHITECTURE = DOCS_ROOT / "architecture" / "CODE_ARCHITECTURE.md"
NATIVE_MAINTENANCE_SOP = DOCS_ROOT / "sop" / "NATIVE_V1_MAINTENANCE_SOP.md"
BLACKBOX_RECORDS = DOCS_ROOT / "blackbox_v2" / "records"
FENGRL_MONTHLY_RECORD = (
    BLACKBOX_RECORDS
    / "MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md"
)
HISTORICAL_DOCUMENTS = (
    FENGRL_MONTHLY_RECORD,
    DOCS_ROOT
    / "internal"
    / "specs"
    / "2026-07-21-t5-no-foreign-lgbm-ablation-design.md",
    DOCS_ROOT
    / "records"
    / "system-checks"
    / "bond_factor_lab_all_schemes_system_check_20260628.md",
    DOCS_ROOT
    / "records"
    / "system-checks"
    / "bond_factor_lab_system_check_against_old_runbook_20260628.md",
)
T5_NO_FOREIGN_ABLATION_DESIGN = HISTORICAL_DOCUMENTS[1]
class OnboardingDocumentationTests(unittest.TestCase):
    def test_current_governance_contract_exists_and_is_indexed(self) -> None:
        self.assertTrue(PRODUCTION_SCHEDULING_GOVERNANCE.exists())
        governance = PRODUCTION_SCHEDULING_GOVERNANCE.read_text(
            encoding="utf-8"
        )
        for marker in (
            "launchd + installed plist",
            "唯一生产调度控制面",
            "一个 cadence 只能有一个生产 writer",
            "gray_live",
            "scheduled_live",
        ):
            self.assertIn(marker, governance)
        self.assertIn("不得新增、扩容、迁移或补建", governance)
        self.assertIn(
            "仓库已移除 `com.bond-factor-lab.scheduler`",
            governance,
        )
        self.assertRegex(
            governance,
            r"也不得作为新的或过渡生产调度\s*路径",
        )

        for path in (
            DOCS_ROOT / "README.md",
            DOCS_ROOT / "CURRENT_STATUS.md",
            DOCS_ROOT / "TODO.md",
            DOCS_ROOT / "architecture" / "README.md",
        ):
            with self.subTest(path=path.relative_to(PROJECT_ROOT)):
                self.assertIn(
                    "PRODUCTION_SCHEDULING_GOVERNANCE.md",
                    path.read_text(encoding="utf-8"),
                )
        self.assertIn(
            "WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md",
            (DOCS_ROOT / "records" / "status" / "README.md").read_text(
                encoding="utf-8"
            ),
        )
        native_sop = NATIVE_MAINTENANCE_SOP.read_text(encoding="utf-8")
        self.assertIn("不再要求维护", native_sop)
        self.assertNotIn("作为**同一受控发布单元**", native_sop)
        deploy = DEPLOY_README.read_text(encoding="utf-8")
        self.assertIn("不提供 bootstrap、bootout 或 kickstart 的可执行指令", deploy)
        self.assertIn("独立生产操作", deploy)

    def test_point_in_time_records_use_standard_historical_status(self) -> None:
        for path in HISTORICAL_DOCUMENTS:
            with self.subTest(path=path.relative_to(PROJECT_ROOT)):
                text = path.read_text(encoding="utf-8")
                self.assertIn("**文档状态**：`HISTORICAL`", text)

        ablation_design = T5_NO_FOREIGN_ABLATION_DESIGN.read_text(
            encoding="utf-8"
        )
        self.assertNotIn("APPROVED_DESIGN", ablation_design)
        self.assertNotIn("待实施计划", ablation_design)

    def test_root_agent_instructions_are_byte_identical(self) -> None:
        self.assertEqual(
            (PROJECT_ROOT / "AGENTS.md").read_bytes(),
            (PROJECT_ROOT / "CLAUDE.md").read_bytes(),
        )

    def test_launchd_plist_is_the_documented_production_scheduler_control_plane(self) -> None:
        root_policy = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        current = CURRENT_STATUS.read_text(encoding="utf-8")
        architecture = ARCHITECTURE.read_text(encoding="utf-8")
        code_architecture = CODE_ARCHITECTURE.read_text(encoding="utf-8")
        governance = PRODUCTION_SCHEDULING_GOVERNANCE.read_text(encoding="utf-8")

        self.assertIn("launchd + plist", root_policy)
        self.assertIn("真实生产调度控制面", root_policy)
        for text in (architecture, governance):
            self.assertIn("launchd + installed plist", text)
            self.assertIn("生产调度控制面", text)
        self.assertIn("launchd + installed plist", current)
        self.assertIn("唯一生产调度控制面", current)
        self.assertIn("installed plist", current)
        self.assertIn("`scheduler.direct_prediction`", code_architecture)
        self.assertIn("bootstrap/bootout/kickstart", root_policy)
        self.assertIn("一个 cadence 只能有一个生产 writer", governance)
        self.assertIn("daily predictions 约 07:03", governance)
        self.assertIn("仓库模板已移除", architecture)
        self.assertIn("不属于新的或过渡生产方案", architecture)
        self.assertNotIn("2026-08-02", architecture)
        self.assertIn(
            "不再存在需要维护的 frozen daily-gray policy",
            code_architecture,
        )

    def test_native_daily_activation_uses_exact_validation_version(self) -> None:
        sop = NATIVE_MAINTENANCE_SOP.read_text(encoding="utf-8")

        for marker in (
            "不再要求维护",
            "不能作为新版本发布单元",
            "validation_scheme_version",
            "生产信号与调度治理",
            "bootout/bootstrap/kickstart",
            "另取明确生产",
        ):
            self.assertIn(marker, sop)
        self.assertNotIn("作为**同一受控发布单元**", sop)

    def test_policy_declares_blackbox_as_only_new_scheme_runtime(self) -> None:
        raw = json.loads((PROJECT_ROOT / "deploy" / "onboarding_policy_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["policy_version"], "1.0")
        self.assertEqual(raw["new_scheme_runtime_type"], "blackbox_v2")
        self.assertEqual(raw["native_v1_mode"], "maintenance_only")
        self.assertEqual(len(raw["legacy_native_scheme_ids"]), 26)

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
            "launchd_one_shot",
            "legacy_automatic",
            "daily_ledger",
            "direct_scheduled",
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

    def test_legacy_daily_rollout_docs_are_explicitly_retired(self) -> None:
        deploy = DEPLOY_README.read_text(encoding="utf-8")
        sla = DAILY_SIGNAL_SLA.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        governance = PRODUCTION_SCHEDULING_GOVERNANCE.read_text(encoding="utf-8")

        self.assertIn(
            "仓库已移除 `com.bond-factor-lab.scheduler`",
            deploy,
        )
        self.assertIn("文档状态**：`HISTORICAL`", sla)
        for text in (governance, platform):
            self.assertIn("launchd + installed plist", text)
            self.assertIn("一个", text)
            self.assertIn("writer", text)
        self.assertIn("不得新增、扩容、迁移或补建", governance)
        self.assertIn(
            "仓库已移除 `com.bond-factor-lab.scheduler`",
            governance,
        )
        self.assertIn("不能作为新的或过渡调度路径", platform)
        self.assertNotIn("BOND_DAILY_COORDINATOR_MODE=ledger", governance)

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

    def test_blackbox_platform_sop_defines_insert_only_draft_registration(self) -> None:
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        for marker in (
            "draft-register",
            "draft_register",
            "HARNESS_AUTH_SECRET",
            "scheme-scoped MySQL advisory lock",
            "insert-only",
            "latest persisted all-stage",
            "不改 config",
            "不写 run/prediction/backtest",
        ):
            self.assertIn(marker, platform)

    def test_blackbox_formal_delivery_requires_description(self) -> None:
        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            '"schema_version": "1.0"',
            '"description":',
            "正式交付必填",
            "主要输入",
            "窗口或规则",
            "模型类型",
            "方向形成方式",
            "不得进入平台 Gate",
        ):
            self.assertIn(marker, upstream)

        for marker in (
            "description",
            "warnings",
            "机器兼容 Intake",
            "不等于正式收包通过",
            "正式新交付",
            "Gate 前 fail-closed",
            "t_scheme_registry.description",
            "/api/schemes",
            "/api/backtests/factor-lab",
            "已有方案",
            "不修改",
            "前端备注",
        ):
            self.assertIn(marker, platform)

    def test_blackbox_platform_input_delivery_and_check_only_contract(self) -> None:
        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            "正式交付目录仍然只能包含",
            "`api_wind_date.csv` 只允许作为上游自验材料",
            "不得进入正式两文件交付目录",
            "不得在 Metadata 中增加 `platform_inputs`",
            "--platform-input api-wind-date-v1",
            "export/tables/api_wind_date/csv/",
            "自测与平台验收必须同代",
            "data_vintage_mismatch",
            "daily_cutoff_key -> week_id",
        ):
            self.assertIn(marker, upstream)

        for marker in (
            "--platform-input api-wind-date-v1",
            "platform_inputs",
            "api-wind-date-provider-v1",
            "api_wind_date.csv",
            "DataBridge 父快照仍然严格只有三份业务文件",
            "combined_snapshot_id",
            "parent_snapshot_id",
            "identity_manifest",
            "audit_manifest",
            "Harness/check-only",
            "只读 DB capture",
            "scheduled",
            "Native generation",
            "source provenance",
            "--check-only",
            "check_only=true",
            "control_plane_persisted=false",
            "business_tables_written=false",
            "persist_backtest=false",
            "100/100",
            "结构验证",
            "对齐上游自测与平台验收输入",
            "self_test_alignment",
            "data_vintage_mismatch",
            "生产永久冻结",
        ):
            self.assertIn(marker, platform)

    def test_blackbox_architecture_defines_composed_runtime_inputs(self) -> None:
        blackbox = BLACKBOX_ARCHITECTURE.read_text(encoding="utf-8")
        contract = SCHEME_CONTRACT.read_text(encoding="utf-8")
        harness = HARNESS_ARCHITECTURE.read_text(encoding="utf-8")

        for marker in (
            "DataBridge 父快照",
            "严格保持三个文件",
            "三频父快照 + 显式声明的平台制品",
            "combined_snapshot_id",
            "parent_snapshot_id",
            "identity schema version",
            "provider version",
            "source provenance",
            "不进入组合内容身份",
            "普通文件",
            "0444",
            "0555",
            "debris cleanup",
            "PredictionRecord.extra",
            "input_identity_manifest",
            "input_audit_manifest",
        ):
            self.assertIn(marker, blackbox)

        for marker in (
            "三频父快照 + 显式声明的平台制品",
            "platform_inputs",
            "api-wind-date-v1",
            "组合输入身份",
            "父快照身份",
        ):
            self.assertIn(marker, contract)

        for marker in (
            "--check-only",
            "七个自动 Gate",
            "控制面零持久化",
            "业务表零写入",
            "Static 只记录声明的 provider",
            "Input 创建并记录组合输入身份",
            "Unit/Dry-run/Compare/Backtest/API readiness 共享同一组合输入身份",
            "`api_wind_date`",
            "仅由平台输入 provider 通过只读连接捕获",
        ):
            self.assertIn(marker, harness)

    def test_launchd_templates_define_the_one_shot_desired_state(self) -> None:
        launchd_root = PROJECT_ROOT / "deploy" / "launchd"
        expected_one_shots = (
            (
                "com.bond-factor-lab.data-bridge-refresh.plist",
                "com.bond-factor-lab.data-bridge-refresh",
                {"Hour": 6, "Minute": 30},
            ),
            (
                "com.bond-factor-lab.daily-predictions.plist",
                "com.bond-factor-lab.daily-predictions",
                [
                    {"Weekday": weekday, "Hour": 7, "Minute": 3}
                    for weekday in range(1, 6)
                ],
            ),
            (
                "com.bond-factor-lab.weekly-predictions.plist",
                "com.bond-factor-lab.weekly-predictions",
                {"Weekday": 6, "Hour": 11, "Minute": 30},
            ),
            (
                "com.bond-factor-lab.monthly-predictions.plist",
                "com.bond-factor-lab.monthly-predictions",
                {"Day": 15, "Hour": 18, "Minute": 0},
            ),
        )
        for filename, label, calendar in expected_one_shots:
            with self.subTest(label=label):
                with (launchd_root / filename).open("rb") as handle:
                    config = plistlib.load(handle)
                self.assertEqual(config["Label"], label)
                self.assertEqual(config["StartCalendarInterval"], calendar)
                self.assertFalse(config["RunAtLoad"])
                self.assertNotIn("KeepAlive", config)

    def test_scheduler_source_binding_runbook_keeps_secrets_out_of_plist(
        self,
    ) -> None:
        deploy = DEPLOY_README.read_text(encoding="utf-8")
        for marker in (
            "期望配置",
            "只读核对",
            "DSN",
            "凭证",
            "admin token",
            "实例 nonce",
        ):
            self.assertIn(marker, deploy)

    def test_platform_sop_defines_launchd_only_timing_boundary(self) -> None:
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        for marker in (
            "launchd + installed plist",
            "各有一个 writer",
            "当天新鲜",
            "feature_date",
            "gray_live",
            "scheduled_live",
        ):
            self.assertIn(marker, platform)
        self.assertIn("不能作为新的或过渡调度路径", platform)
        self.assertNotIn("当天 occurrence 已冻结", platform)
        self.assertNotIn("真实 ledger provenance", platform)

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
        for status in ("CURRENT", "LEGACY_MAINTENANCE"):
            self.assertIn(f"`{status}`", text)
        self.assertNotIn("`HISTORICAL`", text)

    def test_docs_root_contains_navigation_current_status_and_todo(self) -> None:
        self.assertEqual(
            {path.name for path in DOCS_ROOT.glob("*.md")},
            {"README.md", "CURRENT_STATUS.md", "TODO.md"},
        )

    def test_current_status_separates_target_from_installed_facts(self) -> None:
        current = CURRENT_STATUS.read_text(encoding="utf-8")
        todo = TODO.read_text(encoding="utf-8")
        deploy = DEPLOY_README.read_text(encoding="utf-8")

        self.assertIn("## 当前政策", current)
        self.assertIn("## 已验证的 7Y 灰度闭环", current)
        self.assertIn("## 未完成的生产治理", current)
        for marker in (
            "launchd + installed plist",
            "gray_live",
            "scheduled_live",
            "G1",
            "G4",
            "不授予 scheduler admission",
        ):
            self.assertIn(marker, current)

        for marker in (
            "## 生产操作边界",
            "仓库模板",
            "installed plist",
            "loaded state",
            "fail-closed",
        ):
            self.assertIn(marker, deploy)
        for duplicated_dynamic_fact in (
            "2026-07-28 为 12/29",
            "2026-07-29 为 0/29",
            "尚无真实 ledger occurrence",
        ):
            self.assertNotIn(duplicated_dynamic_fact, deploy)

        self.assertIn("R0：生产修复发布", todo)
        self.assertIn("D1：Native 日频缺口闭环", todo)
        self.assertNotIn("WAITING_EXPLICIT_RELEASE_DECISION", todo)
        self.assertNotIn(
            "REPOSITORY_REMEDIATION_VERIFIED_DATA_SCOPE_UNRESOLVED",
            todo,
        )
        self.assertNotIn("ledger 已启用", current)
        self.assertNotIn("2026-07-30 日频 ledger 本地运行基线", deploy)


    def test_scheduled_live_requires_future_real_occurrence_evidence(
        self,
    ) -> None:
        current = CURRENT_STATUS.read_text(encoding="utf-8")
        deploy = DEPLOY_README.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")
        governance = PRODUCTION_SCHEDULING_GOVERNANCE.read_text(encoding="utf-8")

        for text in (current, governance, platform):
            self.assertIn("gray_live", text)
            self.assertIn("scheduled_live", text)
        self.assertIn("自然时钟触发", governance)
        self.assertIn("insert-only", governance)
        self.assertIn("不授予 scheduler admission", current)
        self.assertIn("合格自然时钟触发", platform)
        self.assertIn("期望配置", deploy)
        self.assertNotIn("真实 ledger provenance", platform)
        self.assertNotIn("ledger provenance", governance)

    def test_current_governance_separates_historical_and_natural_phases(self) -> None:
        governance = PRODUCTION_SCHEDULING_GOVERNANCE.read_text(encoding="utf-8")

        self.assertIn("历史缺口", governance)
        self.assertIn("gray_live", governance)
        self.assertIn("scheduled_live", governance)
        self.assertIn("insert-only", governance)
        self.assertIn("不能互相伪装、覆盖", governance)


    def test_canonical_migration_runner_boundary_is_documented(self) -> None:
        """迁移 CLI 的写库身份围栏与运维边界必须由当前文档锁定。"""
        root_policy = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        architecture = CODE_ARCHITECTURE.read_text(encoding="utf-8")
        deploy = DEPLOY_README.read_text(encoding="utf-8")

        self.assertIn("`migrations.runner` 是迁移行为的唯一实现", root_policy)
        self.assertIn("caller-supplied `Engine`", root_policy)
        self.assertIn("`scripts/apply_migrations.py` 是唯一受控运维包装器", root_policy)
        self.assertIn("## Canonical migration runner", readme)
        self.assertIn("`migrations.runner`", architecture)
        self.assertIn("caller-supplied `Engine`", architecture)
        self.assertIn("`scripts/apply_migrations.py`", architecture)

        self.assertIn("期望配置", deploy)
        self.assertIn("不提供 bootstrap、bootout 或 kickstart 的可执行指令", deploy)


    def test_each_document_directory_has_a_complete_index(self) -> None:
        missing_indexes: list[str] = []
        incomplete_indexes: list[str] = []

        documentation_paths = (
            path
            for path in DOCS_ROOT.rglob("*.md")
            if "superpowers" not in path.relative_to(DOCS_ROOT).parts
        )
        directories = {DOCS_ROOT}
        directories.update(path.parent for path in documentation_paths)
        for directory in sorted(directories):
            markdown_files = {
                path.name
                for path in directory.glob("*.md")
                if path.name != "README.md"
            }
            child_doc_dirs = {
                child.name
                for child in directory.iterdir()
                if child.name != "superpowers"
                and child.is_dir()
                and any(child.rglob("*.md"))
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
        self.assertLessEqual(len(text.splitlines()), 140)
        self.assertIn("状态记录", text)
        self.assertIn("生产信号与调度治理", text)

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

    def test_blackbox_sops_separate_delivery_from_exact_scheduler_admission(self) -> None:
        upstream = UPSTREAM_SOP.read_text(encoding="utf-8")
        platform = PLATFORM_SOP.read_text(encoding="utf-8")

        for marker in (
            "交付不授予平台控制面权限",
            "不授予 activation、灰度写入或平台准入",
            "不得把两文件交付当作平台审批",
        ):
            self.assertIn(marker, upstream)

        for marker in (
            "精确 scheduler admission 是独立的仓库策略",
            "scheduler/blackbox_scheduler_admission.py",
            "deploy/blackbox_scheduler_admission_v1.json",
            "`launchd_one_shot`",
            "`legacy_automatic`、`daily_ledger`、`direct_scheduled`",
            "不安装 plist、不运行 `launchctl`、不重启服务",
        ):
            self.assertIn(marker, platform)

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
