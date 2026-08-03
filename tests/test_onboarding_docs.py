from __future__ import annotations

import hashlib
import json
import plistlib
import re
import unittest
from pathlib import Path

from scheduler.discovery import load_scheme_config
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
TEN_Y_T5_RECORD = (
    DOCS_ROOT
    / "blackbox_v2"
    / "records"
    / "GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md"
)
BLACKBOX_RECORDS = DOCS_ROOT / "blackbox_v2" / "records"
TEN_Y_T5_GRAY_EVIDENCE = (
    BLACKBOX_RECORDS
    / "GRAY_ACCEPTANCE_10Y_T5_4SCHEMES_20260726.evidence.json"
)
BLACKBOX_TRIAL_LEDGER = BLACKBOX_RECORDS / "ONBOARDING_TRIAL_LEDGER.md"
BLACKBOX_RECORDS_INDEX = BLACKBOX_RECORDS / "README.md"
TEN_Y_T5_SCHEME_IDS = (
    "ten_y_t5_maj3_k3_ic_static_v1",
    "ten_y_t5_maj4_k3_ic_static_v1",
    "ten_y_t5_maj4_k3_ic_yearly_v1",
    "ten_y_t5_say_k5_sharpe_static_v1",
)
FENGRL_MONTHLY_SCHEME_IDS = (
    "cgb_a4_fundseason_1y",
    "cgb_a4_fundseason_3y",
    "cgb_a4_fundseason_5y",
    "cgb_a4_fundseason_7y",
    "cgb_a4_fundseason_10y",
)
FENGRL_MONTHLY_RECORD = (
    BLACKBOX_RECORDS
    / "MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md"
)
FENGRL_MONTHLY_EVIDENCE = (
    BLACKBOX_RECORDS
    / "MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.evidence.json"
)
FENGRL_MONTHLY_PREFLIGHT_EVIDENCE = (
    BLACKBOX_RECORDS
    / "FENGRL_MONTHLY_GRAY_PREFLIGHT_20260727.evidence.json"
)
FENGRL_MONTHLY_ACCEPTANCE_EVIDENCE = (
    BLACKBOX_RECORDS
    / "FENGRL_MONTHLY_GRAY_ACCEPTANCE_20260727.evidence.json"
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
DAILY_POLICY = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v2.json"

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
        self.assertIn("daily-gray", governance)
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
        self.assertIn("bootstrap/bootout/kickstart", deploy)
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
        self.assertIn("`scheduler.main`/APScheduler", code_architecture)
        self.assertIn("bootstrap/bootout/kickstart", root_policy)
        self.assertIn("一个 cadence 只能有一个生产 writer", governance)
        self.assertIn("daily predictions 约 07:03", governance)
        self.assertIn("daily-gray", architecture)
        self.assertIn("不属于新的或过渡生产方案", architecture)
        self.assertNotIn("2026-08-02", architecture)
        self.assertIn("不再要求更新 frozen daily-gray policy", code_architecture)

    def test_native_daily_activation_coordinates_frozen_launchd_policy(self) -> None:
        sop = NATIVE_MAINTENANCE_SOP.read_text(encoding="utf-8")

        for marker in (
            "daily_gray_launchd_policy_v1.json",
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

        self.assertIn("HISTORICAL / 不可执行", deploy)
        self.assertIn("文档状态**：`HISTORICAL`", sla)
        for text in (governance, platform):
            self.assertIn("launchd + installed plist", text)
            self.assertIn("一个", text)
            self.assertIn("writer", text)
        self.assertIn("不得新增、扩容、迁移或补建", governance)
        self.assertIn("daily-gray", governance)
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
            "必需源表",
            "不得执行生产 DDL/DML",
            "安全存储与 cache",
            "backtest_artifacts/runtime_cache/liwei_0616",
            "runtime 不静默 `chmod`",
            "manifest/payload SHA-256",
            "owner、mode、inode、symlink",
            "不得修改或重启 BondProjectPro",
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

    def test_retired_document_paths_are_absent(self) -> None:
        retired = (
            DOCS_ROOT / "archive" / "README.md",
            DOCS_ROOT / "archive" / "SCHEME_PARADIGM.md",
            DOCS_ROOT / "blackbox_v2" / "archive" / "README.md",
            DOCS_ROOT
            / "blackbox_v2"
            / "archive"
            / "UPSTREAM_DELIVERY_SOP_EXCEL_DRAFT.md",
            DOCS_ROOT / "native_v1" / "archive" / "README.md",
            DOCS_ROOT
            / "native_v1"
            / "archive"
            / "SCHEME_ONBOARDING_SOP_PRE_FREEZE.md",
            DOCS_ROOT
            / "native_v1"
            / "archive"
            / "SCHEME_ONBOARDING_T0_PRE_FREEZE.md",
            DOCS_ROOT
            / "native_v1"
            / "archive"
            / "SCHEME_PARADIGM_DRAFT.md",
            DOCS_ROOT
            / "native_v1"
            / "archive"
            / "SCHEME_POST_ONBOARDING_TEST_SOP_PRE_FREEZE.md",
            DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_SOP.md",
            DOCS_ROOT / "sop" / "SCHEME_ONBOARDING_T0.md",
            DOCS_ROOT / "sop" / "SCHEME_POST_ONBOARDING_TEST_SOP.md",
        )
        self.assertTrue(all(not path.exists() for path in retired))

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

    def test_legacy_daily_policy_is_not_the_current_production_contract(self) -> None:
        policy = json.loads(DAILY_POLICY.read_text(encoding="utf-8"))
        sla = DAILY_SIGNAL_SLA.read_text(encoding="utf-8")
        current = CURRENT_STATUS.read_text(encoding="utf-8")
        todo = TODO.read_text(encoding="utf-8")
        governance = PRODUCTION_SCHEDULING_GOVERNANCE.read_text(encoding="utf-8")

        self.assertEqual(policy["version"], "daily-scheduler-policy-v2")
        self.assertGreater(len(policy["schemes"]), 0)
        self.assertIn("文档状态**：`HISTORICAL`", sla)
        for text in (current, todo):
            self.assertIn("PRODUCTION_SCHEDULING_GOVERNANCE.md", text)
            self.assertNotIn("1 个 `daily-signals` occurrence", text)
        self.assertIn("一个 cadence 只能有一个生产 writer", governance)
        self.assertIn("daily prediction", governance)
        self.assertRegex(
            governance,
            r"不得让常驻\s+APScheduler、daily-gray",
        )

    def test_daily_runtime_uses_direct_authority_without_legacy_admission(
        self,
    ) -> None:
        deploy = DEPLOY_README.read_text(encoding="utf-8")
        sla = DAILY_SIGNAL_SLA.read_text(encoding="utf-8")
        current = CURRENT_STATUS.read_text(encoding="utf-8")
        todo = TODO.read_text(encoding="utf-8")
        runtime = (
            PROJECT_ROOT / "scheduler" / "daily_runtime.py"
        ).read_text(encoding="utf-8")
        operator = (
            PROJECT_ROOT / "scripts" / "daily_coordinator_epoch_operator.py"
        ).read_text(encoding="utf-8")
        admission_path = (
            PROJECT_ROOT / "deploy" / "daily_capacity_admission_v2.json"
        )

        self.assertFalse(admission_path.exists())
        self.assertNotIn("bind_capacity_admission", runtime)
        self.assertNotIn("require_current_capacity_admission", runtime)
        self.assertNotIn("require_trusted_current_capacity_admission", operator)
        self.assertNotIn("_validate_capacity_admission", operator)
        self.assertIn("bind_direct_cache_authorities", runtime)
        self.assertIn("revalidate_direct_authority", runtime)

        self.assertIn("PRODUCTION_SCHEDULING_GOVERNANCE.md", current)
        self.assertIn("PRODUCTION_SCHEDULING_GOVERNANCE.md", todo)
        self.assertIn("独立生产操作", deploy)
        self.assertIn("必须 fail-closed", deploy)
        self.assertNotIn("capacity admission", sla)
        self.assertIn("文档状态**：`HISTORICAL`", sla)

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
            "## 当前状态权威与 operator guard",
            "[当前状态](../docs/CURRENT_STATUS.md)",
            "本\nrunbook 不复制任何动态值",
            "operator 每次执行前必须读取两页",
            "必须 fail-closed",
        ):
            self.assertIn(marker, deploy)
        for duplicated_dynamic_fact in (
            "2026-07-28 为 12/29",
            "2026-07-29 为 0/29",
            "尚无真实 ledger occurrence",
        ):
            self.assertNotIn(duplicated_dynamic_fact, deploy)

        self.assertIn("G0 — 文档与口径", todo)
        self.assertIn("G1 — DataBridge refresh", todo)
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
        self.assertIn("HISTORICAL / 不可执行", deploy)
        self.assertNotIn("真实 ledger provenance", platform)
        self.assertNotIn("ledger provenance", governance)

    def test_legacy_sla_does_not_bind_current_liwei_policy(self) -> None:
        policy = json.loads(DAILY_POLICY.read_text(encoding="utf-8"))
        sla = DAILY_SIGNAL_SLA.read_text(encoding="utf-8")
        liwei_items = [
            item
            for item in policy["schemes"]
            if item["scheme_id"].startswith("liwei_0616_")
        ]
        self.assertEqual(len(liwei_items), 10)
        for item in liwei_items:
            self.assertIn("cache_group", item)
            self.assertIn("cache_spec_fingerprint", item)
        self.assertIn("文档状态**：`HISTORICAL`", sla)
        self.assertIn("不能作为安装、迁移、调度、验收或回滚操作说明", sla)
        self.assertIn(
            "PRODUCTION_SCHEDULING_GOVERNANCE.md",
            sla,
        )

    def test_historical_daily_gaps_have_gray_only_phase(self) -> None:
        historical = (
            DOCS_ROOT / "records" / "status"
            / "DAILY_SIGNAL_RECOVERY_HANDOFF_20260729.md"
        ).read_text(encoding="utf-8")
        governance = PRODUCTION_SCHEDULING_GOVERNANCE.read_text(encoding="utf-8")

        self.assertIn("文档状态**：`HISTORICAL`", historical)
        self.assertIn("2026-07-28", historical)
        self.assertIn("gray_live", historical)
        self.assertIn("不要写 `scheduled_live`", historical)
        self.assertIn("历史缺口", governance)
        self.assertIn("insert-only", governance)
        self.assertIn("不能互相伪装、覆盖", governance)

    def test_todo_records_completed_manual_gray_batches(self) -> None:
        todo = TODO.read_text(encoding="utf-8")
        monthly = FENGRL_MONTHLY_RECORD.read_text(encoding="utf-8")
        ten_y = TEN_Y_T5_RECORD.read_text(encoding="utf-8")

        self.assertIn("G0 — 文档与口径", todo)
        self.assertIn("G4 — weekly 10Y D-overlay", todo)
        self.assertIn("MANUAL_GRAY_ACCEPTED_5_OF_5", monthly)
        self.assertIn("80 + 15 = 95", monthly)
        self.assertIn("10Y T+5 四方案", ten_y)
        self.assertIn("manual gray_live", ten_y)

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

        self.assertIn("HISTORICAL / 不可执行", deploy)
        for marker in (
            "--expected-database-name <database-name>",
            "--expected-server-uuid <server-uuid>",
            "--inspect-applying-017",
            "--inspect-applying-018",
            "--recover-applying-017 --apply",
            "--recover-applying-018 --apply",
        ):
            self.assertIn(marker, deploy)
        self.assertRegex(
            deploy,
            r"恢复 017 后必须另行执行普通\s+`--apply`",
        )

    def test_10y_gray_onboarding_record_binds_the_exact_batch(self) -> None:
        self.assertTrue(TEN_Y_T5_RECORD.exists())
        if not TEN_Y_T5_RECORD.exists():
            return

        text = TEN_Y_T5_RECORD.read_text(encoding="utf-8")
        self.assertIn("authorized/manual-onboarding-pending-revalidation", text)
        for scheme_id in TEN_Y_T5_SCHEME_IDS:
            self.assertIn(scheme_id, text)

    def test_fengrl_monthly_record_binds_each_delivery_digest_to_its_section(
        self,
    ) -> None:
        """每个 FengRL delivery 摘要必须在其自身技术证据小节可审计。"""
        record = FENGRL_MONTHLY_RECORD.read_text(encoding="utf-8")
        evidence = json.loads(FENGRL_MONTHLY_EVIDENCE.read_text(encoding="utf-8"))

        self.assertEqual(len(evidence["schemes"]), 5)
        for scheme in evidence["schemes"]:
            with self.subTest(scheme_id=scheme["scheme_id"]):
                expected_keys = {
                    f"{scheme['scheme_id']}.py",
                    f"{scheme['scheme_id']}.json",
                }
                self.assertEqual(set(scheme["source_sha256"]), expected_keys)
                self.assertEqual(len(scheme["source_sha256"]), 2)
                self.assertEqual(set(scheme["delivery_sha256"]), expected_keys)
                self.assertEqual(len(scheme["delivery_sha256"]), 2)
                section_start = record.index(
                    f"## {scheme['target_tenor']} 技术证据"
                )
                section_end = record.find("\n## ", section_start + 1)
                section = record[section_start:]
                if section_end != -1:
                    section = record[section_start:section_end]
                for filename in expected_keys:
                    self.assertEqual(
                        scheme["source_sha256"][filename],
                        scheme["delivery_sha256"][filename],
                    )
                    self.assertIn(scheme["delivery_sha256"][filename], section)

    def test_fengrl_monthly_gray_preflight_is_explicitly_no_write_and_complete(
        self,
    ) -> None:
        """月度批次预检必须冻结身份、日期计划与严格零写入边界。"""
        self.assertTrue(FENGRL_MONTHLY_PREFLIGHT_EVIDENCE.exists())
        evidence = json.loads(
            FENGRL_MONTHLY_PREFLIGHT_EVIDENCE.read_text(encoding="utf-8")
        )

        self.assertEqual(
            evidence["candidate_head"],
            "2ba93bbf94b0f07e2c1559cfc45afba59d905997",
        )
        self.assertEqual(
            evidence["source"]["branch"],
            "codex/blackbox-v2-monthly-fengrl-review-20260726",
        )
        self.assertEqual(evidence["source"]["head"], "48de613")
        self.assertEqual(len(evidence["schemes"]), 5)
        self.assertEqual(
            [item["scheme_id"] for item in evidence["schemes"]],
            list(FENGRL_MONTHLY_SCHEME_IDS),
        )
        expected_identities = (
            ("cgb_a4_fundseason_1y", "cgb_a4_fundseason_1y__h1__1Y", "04e7af163fb0"),
            ("cgb_a4_fundseason_3y", "cgb_a4_fundseason_3y__h1__3Y", "89d31f8bcb95"),
            ("cgb_a4_fundseason_5y", "cgb_a4_fundseason_5y__h1__5Y", "7d47e0328532"),
            ("cgb_a4_fundseason_7y", "cgb_a4_fundseason_7y__h1__7Y", "ddba87ece7ae"),
            ("cgb_a4_fundseason_10y", "cgb_a4_fundseason_10y__h1__10Y", "85a65700499b"),
        )
        self.assertEqual(
            [
                (item["scheme_id"], item["composite_id"], item["scheme_version"])
                for item in evidence["schemes"]
            ],
            list(expected_identities),
        )
        for item in evidence["schemes"]:
            scheme_root = PROJECT_ROOT / "schemes" / item["scheme_id"]
            config = load_scheme_config(scheme_root / "config.yaml")
            self.assertEqual(item["runtime_type"], "blackbox_v2")
            self.assertEqual(item["task_type"], "monthly")
            self.assertEqual(item["horizon"], 1)
            self.assertEqual(config.scheme_id, item["scheme_id"])
            self.assertEqual(config.scheme_version, item["scheme_version"])
            self.assertEqual(config.runtime_type, item["runtime_type"])
            self.assertEqual(config.task_type, item["task_type"])
            self.assertEqual(config.horizon, item["horizon"])
            self.assertEqual(config.tenors, [item["target_tenor"]])
            expected_filenames = {
                f"{item['scheme_id']}.py",
                f"{item['scheme_id']}.json",
            }
            self.assertEqual(set(item["delivery_sha256"]), expected_filenames)
            for filename, expected_sha256 in item["delivery_sha256"].items():
                actual_sha256 = hashlib.sha256(
                    (scheme_root / "delivery" / filename).read_bytes()
                ).hexdigest()
                self.assertEqual(actual_sha256, expected_sha256)
        self.assertTrue(evidence["date_plan"]["zero_overlap"])
        self.assertTrue(evidence["date_plan"]["zero_gap"])
        self.assertEqual(evidence["date_plan"]["per_scheme"], {
            "history": 16,
            "gray_live": 3,
            "total": 19,
        })
        self.assertEqual(evidence["date_plan"]["batch"], {
            "history": 80,
            "gray_live": 15,
            "total": 95,
        })
        self.assertEqual(
            evidence["date_plan"]["batch"],
            {
                key: value * len(FENGRL_MONTHLY_SCHEME_IDS)
                for key, value in evidence["date_plan"]["per_scheme"].items()
            },
        )
        self.assertEqual(
            evidence["date_plan"]["gray_predict_dates"],
            ["2026-05-15", "2026-06-15", "2026-07-15"],
        )
        dates = evidence["date_plan"]["dates"]
        expected_dates = [
            ("history", "2025-01-15", "2025-01-15", "2025-02-14"),
            ("history", "2025-02-15", "2025-02-14", "2025-03-14"),
            ("history", "2025-03-15", "2025-03-14", "2025-04-15"),
            ("history", "2025-04-15", "2025-04-15", "2025-05-15"),
            ("history", "2025-05-15", "2025-05-15", "2025-06-13"),
            ("history", "2025-06-15", "2025-06-13", "2025-07-15"),
            ("history", "2025-07-15", "2025-07-15", "2025-08-15"),
            ("history", "2025-08-15", "2025-08-15", "2025-09-15"),
            ("history", "2025-09-15", "2025-09-15", "2025-10-15"),
            ("history", "2025-10-15", "2025-10-15", "2025-11-14"),
            ("history", "2025-11-15", "2025-11-14", "2025-12-15"),
            ("history", "2025-12-15", "2025-12-15", "2026-01-15"),
            ("history", "2026-01-15", "2026-01-15", "2026-02-13"),
            ("history", "2026-02-15", "2026-02-13", "2026-03-13"),
            ("history", "2026-03-15", "2026-03-13", "2026-04-15"),
            ("history", "2026-04-15", "2026-04-15", "2026-05-15"),
            ("gray_live", "2026-05-15", "2026-05-15", "2026-06-15"),
            ("gray_live", "2026-06-15", "2026-06-15", "2026-07-15"),
            ("gray_live", "2026-07-15", "2026-07-15", "2026-08-14"),
        ]
        actual_dates = [
            (
                item["phase"],
                item["predict_date"],
                item["feature_date"],
                item["target_date"],
            )
            for item in dates
        ]
        self.assertEqual(actual_dates, expected_dates)
        self.assertEqual(len(actual_dates), len(set(actual_dates)))
        self.assertEqual(len(dates), evidence["date_plan"]["per_scheme"]["total"])
        self.assertEqual(sum(item["phase"] == "history" for item in dates), 16)
        self.assertEqual(sum(item["phase"] == "gray_live" for item in dates), 3)
        self.assertTrue(
            all(item["target_date"] < "2026-06-01" for item in dates[:16])
        )
        self.assertTrue(
            all(item["target_date"] >= "2026-06-01" for item in dates[16:])
        )
        self.assertTrue(evidence["production_snapshot"]["identity_conflict_free"])
        per_scheme_counts = evidence["production_snapshot"][
            "per_scheme_related_table_counts"
        ]
        self.assertEqual(set(per_scheme_counts), set(FENGRL_MONTHLY_SCHEME_IDS))
        for counts in per_scheme_counts.values():
            self.assertEqual(
                set(counts), set(evidence["production_snapshot"]["related_tables"])
            )
            self.assertTrue(all(value == 0 for value in counts.values()))
        self.assertFalse(evidence["authorization"]["production_write_authorized"])
        self.assertFalse(evidence["authorization"]["writes_performed"])
        self.assertFalse(evidence["authorization"]["scheduler_or_scheduled_live"])
        self.assertFalse(evidence["private_publication"]["main_checkout_root_permission_valid"])
        self.assertFalse(
            evidence["private_publication"]["db_generation_row_registered"]
        )
        self.assertEqual(evidence["private_publication"]["fresh_live_count"], 0)
        self.assertEqual(
            evidence["verification"]["controller_combined"],
            {"passed": 280, "subtests": 145},
        )
        self.assertEqual(
            evidence["verification"]["parallel_groups"],
            {"passed": 278, "subtests": 140},
        )
        record = FENGRL_MONTHLY_RECORD.read_text(encoding="utf-8")
        self.assertIn("INTEGRATION_PREFLIGHT_READY_NO_WRITE", record)
        self.assertIn("FENGRL_MONTHLY_GRAY_PREFLIGHT_20260727.evidence.json", record)
        self.assertIn("不是数据库 `t_input_generations` 的 `SEALED` 记录", record)

    def test_fengrl_monthly_gray_acceptance_records_exact_terminal_state(
        self,
    ) -> None:
        """终验证据必须锁定 80+15=95 及零 scheduled 边界。"""
        self.assertTrue(FENGRL_MONTHLY_ACCEPTANCE_EVIDENCE.exists())
        evidence = json.loads(
            FENGRL_MONTHLY_ACCEPTANCE_EVIDENCE.read_text(encoding="utf-8")
        )

        self.assertEqual(evidence["status"], "MANUAL_GRAY_ACCEPTED_5_OF_5")
        self.assertEqual(
            [item["scheme_id"] for item in evidence["schemes"]],
            list(FENGRL_MONTHLY_SCHEME_IDS),
        )
        self.assertEqual(
            [item["scheme_version"] for item in evidence["schemes"]],
            [
                "04e7af163fb0",
                "89d31f8bcb95",
                "7d47e0328532",
                "ddba87ece7ae",
                "85a65700499b",
            ],
        )
        for item in evidence["schemes"]:
            self.assertEqual(item["persisted_all_stage"]["passed_gates"], 7)
            self.assertEqual(item["persisted_all_stage"]["expected_gates"], 7)
            self.assertEqual(item["history"]["predictions"], 16)
            self.assertEqual(item["history"]["monthly_metrics"], 16)
            self.assertEqual(item["gray"], {
                "runs": 3,
                "predictions": 3,
                "logs": 3,
            })
            self.assertEqual(item["frontend_samples"], 19)

        self.assertEqual(evidence["batch_totals"], {
            "persisted_all_stage_passed": 5,
            "history_runs": 5,
            "history_predictions": 80,
            "history_monthly_metrics": 80,
            "gray_runs": 15,
            "gray_predictions": 15,
            "gray_logs": 15,
            "frontend_samples": 95,
        })
        self.assertEqual(evidence["date_contract"]["history_per_scheme"], 16)
        self.assertEqual(evidence["date_contract"]["gray_per_scheme"], 3)
        self.assertEqual(evidence["date_contract"]["total_per_scheme"], 19)
        self.assertEqual(
            evidence["input_provenance"]["databridge_generation_id"],
            "full-20260724-062251-4977e502dadf",
        )
        self.assertEqual(evidence["acceptance"]["registry_active"], 5)
        self.assertEqual(evidence["acceptance"]["versions_active"], 5)
        self.assertEqual(evidence["acceptance"]["database"], "passed")
        self.assertEqual(evidence["acceptance"]["api"], "passed")
        self.assertEqual(evidence["acceptance"]["frontend"], "passed")
        boundaries = evidence["production_boundaries"]
        self.assertEqual(boundaries["scheduled_live_per_scheme"], 0)
        self.assertEqual(boundaries["rollout"], "legacy")
        self.assertEqual(boundaries["admission"], "BLOCKED")
        self.assertFalse(boundaries["scheduler_restarted"])
        self.assertFalse(boundaries["backend_restarted"])
        self.assertFalse(boundaries["bondprojectpro_modified"])
        self.assertFalse(boundaries["merged"])
        self.assertFalse(boundaries["pushed"])
        self.assertFalse(boundaries["deployed"])
        self.assertFalse(boundaries["automatic_scheduler_authorized"])

    def test_10y_batch_scope_records_manual_history_and_new_scheduler_decision(self) -> None:
        record = TEN_Y_T5_RECORD.read_text(encoding="utf-8")
        index = BLACKBOX_RECORDS_INDEX.read_text(encoding="utf-8")

        self.assertIn("文档状态**：`HISTORICAL`", record)
        for marker in (
            "controlled activate",
            "persistent backtest",
            "manual gray_live",
        ):
            self.assertIn(marker, record)

        self.assertIn("四个方案均已完成 39 条", record)
        self.assertIn("本批总计 156 条", record)
        self.assertIn("合计 372 条", record)
        self.assertEqual(
            record.count("GRAY_LIVE_WAITING_FOR_MANUAL_EXECUTION"),
            0,
        )
        self.assertIn("本批不存在 `scheduled_live`", record)
        self.assertIn("scheduled_live", record)
        self.assertIn("旧 generation fallback", record)
        self.assertIn("GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md", index)

    def test_10y_batch_current_docs_record_manual_gray_completion(self) -> None:
        todo = TODO.read_text(encoding="utf-8")
        current = CURRENT_STATUS.read_text(encoding="utf-8")
        record = TEN_Y_T5_RECORD.read_text(encoding="utf-8")
        ledger = BLACKBOX_TRIAL_LEDGER.read_text(encoding="utf-8")
        index = BLACKBOX_RECORDS_INDEX.read_text(encoding="utf-8")

        for text in (todo, current, record):
            self.assertNotIn(
                "当前状态是 `authorized/manual-onboarding-pending-revalidation`",
                text,
            )
            self.assertNotIn("GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION", text)

        self.assertIn("状态记录", current)
        self.assertIn("7Y", current)
        self.assertNotIn("10Y T+5", current)
        self.assertNotIn("尚无真实 ledger occurrence", current)

        self.assertIn(
            "**当前状态**：`four-schemes-gray-live-accepted`",
            record,
        )
        for marker in (
            "full-20260724-062251-4977e502dadf",
            "snapshot-46ff3231de2c4a080c46ba56",
            "1194..1232",
            "1233..1271",
            "1272..1310",
            "1311..1349",
            "25 item/29 target",
            "21 item/25 target",
            "gray_live",
            "scheduled_live",
            "rollout=`legacy`",
            "admission=`BLOCKED`",
            "schedule_cron",
            "Python 3.12",
            "report_uri",
            "code_hash/config_hash/input_artifact_hash",
        ):
            self.assertIn(marker, record + ledger)

        self.assertIn("### 4.16 记录 003 终态", ledger)
        self.assertIn("### 4.17 记录 003E", ledger)
        self.assertIn("手工灰度验收", index)

        evidence = json.loads(TEN_Y_T5_GRAY_EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(evidence["batch_totals"]["gray_predictions"], 156)
        self.assertEqual(evidence["batch_totals"]["gray_runs"], 156)
        self.assertEqual(evidence["batch_totals"]["gray_run_logs"], 156)
        self.assertEqual(evidence["batch_totals"]["scheduled_live"], 0)
        boundary = evidence["scheduler_boundary"]
        self.assertEqual(boundary["rollout"], "legacy")
        self.assertEqual(boundary["admission"], "BLOCKED")
        self.assertEqual(boundary["formal_policy"], "21-item-25-target")
        self.assertEqual(boundary["integration_discovery"], "25-item-29-target")
        self.assertFalse(boundary["scheduler_restarted"])
        self.assertTrue(boundary["active_configs_have_schedule_cron"])
        self.assertFalse(boundary["integration_merge_authorized"])
        self.assertFalse(boundary["automatic_gray_authorized"])
        self.assertEqual(len(evidence["schemes"]), 4)
        expected_run_start = 1194
        gray_total = 0
        for scheme in evidence["schemes"]:
            self.assertEqual(scheme["backtest_rows"], 333)
            self.assertEqual(scheme["gray_predictions"], 39)
            self.assertEqual(
                scheme["backtest_rows"] + scheme["gray_predictions"],
                scheme["frontend_samples"],
            )
            self.assertEqual(scheme["frontend_samples"], 372)
            run_start, run_end = scheme["gray_run_range"]
            self.assertEqual(run_start, expected_run_start)
            self.assertEqual(run_end - run_start + 1, 39)
            expected_run_start = run_end + 1
            gray_total += scheme["gray_predictions"]
        self.assertEqual(expected_run_start, 1350)
        self.assertEqual(gray_total, evidence["batch_totals"]["gray_predictions"])

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
