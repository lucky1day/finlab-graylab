# 10Y T+5 四方案手工入库记录

**文档状态**：`IN_PROGRESS`

**执行日期**：2026-07-26，`Asia/Shanghai`

**当前状态**：`authorized/manual-onboarding-pending-revalidation`

本文是本批四个 10Y T+5 Blackbox V2 方案的时点记录。它授权逐方案 revalidation → `controlled activate` → `persistent backtest` → `manual gray_live` → `DB/API/frontend acceptance`；截至本记录尚未执行这些阶段。

## 1. 来源与范围

来源工作树为 `blackbox-v2-10y-t5-onboarding-20260726`，最终审计提交为 `30e2edc`；四个独立 Intake 提交依次为 `0c1daa2`、`90092d6`、`49e3e13`、`30e2edc`。本记录不修改交付 Metadata，也不将本批结论外推为其他方案或通用生产授权。

| base scheme ID | source commit | scheme version | Metadata SHA-256 | Delivery SHA-256 | description 专项状态 |
|---|---|---|---|---|---|
| `ten_y_t5_maj3_k3_ic_static_v1` | `0c1daa2` | `c54b90bcafa7` | `10c41c6d3e271e76c6c03afc4e9ff3ad998ffefb92b557d5bb868d69e077329d` | `75749f165e3ce2c5cb70f86fae1336e52e693655198b05c78e45422e165471de` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |
| `ten_y_t5_maj4_k3_ic_static_v1` | `90092d6` | `6bdabf86b4a6` | `eee777b89f0a9138dce0454044e0569f91459c28f0f5426d8a7d5c06202da005` | `64000f9a4521dfdf8da04792e8b083bf12cd0837870b7714b27725d4dcbc955b` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |
| `ten_y_t5_maj4_k3_ic_yearly_v1` | `49e3e13` | `af04567a19c3` | `be7b6950d329ce058726d7bff80e4069192d2724223a364b02549527ec9f720a` | `7e55fae577b3a14085fdba98af638e449c11b935db373a6176238d72afea3781` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |
| `ten_y_t5_say_k5_sharpe_static_v1` | `30e2edc` | `e8137af4b655` | `a6fd633a49cdb7e8e52d900cf1372fbdeccb61528426d3606d30f1a141b80311` | `960a058e9f98525022d21b19034e7055d49bb56e2bba54447dc0c79870d9010c` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |

## 2. 授权和 revalidation 边界

1. 每个方案只能使用上表 exact scheme version、Metadata SHA-256 与 Delivery SHA-256 进行手工入库和 revalidation；摘要漂移即停止并重新判定。
2. revalidation 通过后，可逐方案按顺序执行 `controlled activate`、`persistent backtest`、`manual gray_live` 和 `DB/API/frontend acceptance`；每阶段都须复核专项授权和前序证据。
3. 截至本记录尚未执行 activation、active Registry 登记、持久化回测、`manual gray_live`、业务表写入或 DB/API/frontend acceptance；该时点事实不取消前项已授权的受控阶段。
4. 自动 scheduler、`automatic gray scheduling` 和 `scheduled_live` 仍禁止并延后到 TODO 的独立 admission；任何阶段都不得使用 `旧 generation fallback`。
5. 缺少 `description` 的状态仅是这四个不可变既有交付的专项豁免；不得补写或重写 Metadata，不得推测算法逻辑。后续新交付仍由当前人工 fail-closed 流程要求提供 `description`，机器门禁另行 TDD。

## 3. 后续顺序

本批之后的自动灰度调度必须先完成[TODO](../../TODO.md)列出的 21/25、迁移、replay、容量、恢复和连续观察门禁，并另行获得独立 `scheduler_admission=gray|formal` 与逐方案授权。`formal` 晋级不由本记录或 `gray` admission 推导。
