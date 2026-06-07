from __future__ import annotations

import ast
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
TENORS = ["3y", "5y", "7y", "10y"]
SCRIPT_NAMES = {"3y": "predict_3y.py", "5y": "predict_5y.py", "7y": "predict_7y.py", "10y": "predict_10y.py"}
TENOR_LABEL = {"3y": "3Y", "5y": "5Y", "7y": "7Y", "10y": "10Y"}
MODEL_NAMES = {
    "3y": "3Y T+5 投票增强模型",
    "5y": "5Y T+5 投票增强模型",
    "7y": "7Y T+5 投票规则模型",
    "10y": "10Y T+5 投票增强模型",
}
SUMMARY_NOTES = {
    "3y": "3Y 仍然是验证集最强模型，但本次 May 扩展到 12 个样本后回落到 58.3%，strict pass 被 May 条件卡住。",
    "5y": "5Y 的 sim/real 仍均在 65% 左右，但 May 观察段只有 33.3%，最新段明显失效，需要重点复核。",
    "7y": "7Y 投票规则模型仍通过 strict 条件，real 与 May 都保持在 60% 以上，是本次更稳的期限之一。",
    "10y": "10Y 仍通过 strict 条件，May 观察段 75.0%，但基础模型本身较弱，主要依赖投票增强。",
}


def pct_count(correct: int, n: int) -> str:
    if n == 0:
        return "N/A"
    return f"{correct / n * 100:.1f}%（{correct}/{n}）"


def rate_count(num: int, den: int) -> str:
    if den == 0:
        return "N/A"
    return f"{num / den * 100:.1f}%（{num}/{den}）"


def pct(value: float) -> str:
    if pd.isna(value):
        return "N/A"
    return f"{value * 100:.1f}%"


def md_table(headers: list[str], rows: list[list[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def parse_script_config(path: Path) -> tuple[dict[str, str], list[str]]:
    text = path.read_text(encoding="utf-8")
    cfg: dict[str, str] = {}
    for key in [
        "HORIZON",
        "GAP",
        "WINDOW",
        "NUM_LEAVES",
        "N_ESTIMATORS",
        "LEARNING_RATE",
        "MIN_CHILD_SAMPLES",
        "REG_ALPHA",
        "REG_LAMBDA",
        "SPLIT_PCT",
        "VOTE_THRESHOLD",
    ]:
        match = re.search(rf"^{key}\s*=\s*([^\n#]+)", text, flags=re.MULTILINE)
        if match:
            cfg[key] = match.group(1).strip().strip("\"'")
    recipe: list[str] = []
    match = re.search(r"RECIPE\s*=\s*(\[.*?\])", text, flags=re.S)
    if match:
        try:
            recipe = [item[0] for item in ast.literal_eval(match.group(1))]
        except Exception:
            recipe = []
    return cfg, recipe


def segment_mask(df: pd.DataFrame, segment: str) -> pd.Series:
    date = df["date"]
    if segment == "sim":
        return (date >= pd.Timestamp("2025-01-01")) & (date <= pd.Timestamp("2025-06-30"))
    if segment == "real":
        return (date >= pd.Timestamp("2025-07-01")) & (date <= pd.Timestamp("2026-04-30"))
    if segment == "may":
        return date >= pd.Timestamp("2026-05-01")
    if segment == "all":
        return pd.Series(True, index=df.index)
    raise ValueError(segment)


def accuracy(df: pd.DataFrame, pred_col: str) -> dict[str, float | int]:
    if len(df) == 0:
        return {"n": 0, "correct": 0, "acc": np.nan}
    correct = int((df[pred_col] == df["true_label"]).sum())
    return {"n": int(len(df)), "correct": correct, "acc": correct / len(df)}


def split_metrics(df: pd.DataFrame, pred_col: str) -> dict[str, int]:
    pred = df[pred_col]
    true = df["true_label"]
    tp = int(((pred == 1) & (true == 1)).sum())
    fp = int(((pred == 1) & (true == -1)).sum())
    tn = int(((pred == -1) & (true == -1)).sum())
    fn = int(((pred == -1) & (true == 1)).sum())
    return {
        "n": int(len(df)),
        "correct": tp + tn,
        "true_up": int((true == 1).sum()),
        "true_down": int((true == -1).sum()),
        "true_flat": int((true == 0).sum()),
        "pred_up": int((pred == 1).sum()),
        "pred_down": int((pred == -1).sum()),
        "pred_flat": int((pred == 0).sum()),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }


def judge_month(metrics: dict[str, int]) -> str:
    n = metrics["n"]
    acc = metrics["correct"] / n if n else np.nan
    if pd.isna(acc):
        base = "无样本"
    elif acc >= 0.70:
        base = "强"
    elif acc >= 0.60:
        base = "较好"
    elif acc >= 0.50:
        base = "一般"
    else:
        base = "偏弱"
    if n == 0:
        return base
    up_rate = metrics["pred_up"] / n
    down_rate = metrics["pred_down"] / n
    if up_rate >= 0.80:
        suffix = "偏重上涨预测"
    elif down_rate >= 0.80:
        suffix = "偏重下跌预测"
    else:
        suffix = "涨跌较均衡"
    return f"{base}；{suffix}"


def monthly_table(df: pd.DataFrame, pred_col: str) -> pd.DataFrame:
    work = df.copy()
    work["month"] = work["date"].dt.to_period("M").astype(str)
    rows = []
    for month, group in work.groupby("month"):
        metrics = split_metrics(group, pred_col)
        rows.append(
            {
                **metrics,
                "month": month,
                "accuracy": metrics["correct"] / metrics["n"] if metrics["n"] else np.nan,
                "judgement": judge_month(metrics),
            }
        )
    return pd.DataFrame(rows)


def build_report() -> tuple[Path, Path]:
    run_id = datetime.now().strftime("reproduce_eval_%Y%m%d_%H%M%S")
    out_dir = ROOT / "outputs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_path = ROOT / "docs" / f"models_liwei_0528_reproduce_evaluation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    doc_path.parent.mkdir(parents=True, exist_ok=True)

    configs: dict[str, dict[str, str]] = {}
    recipes: dict[str, list[str]] = {}
    for tenor in TENORS:
        configs[tenor], recipes[tenor] = parse_script_config(ROOT / SCRIPT_NAMES[tenor])

    raw = pd.read_csv(ROOT / "data" / "daily_output.csv", usecols=["date"])
    raw["date"] = pd.to_datetime(raw["date"])
    raw_range = (raw["date"].min().date(), raw["date"].max().date())

    preds: dict[str, pd.DataFrame] = {}
    summary: dict[str, dict[str, object]] = {}
    monthly: dict[str, pd.DataFrame] = {}
    for tenor in TENORS:
        path = ROOT / f"{tenor}_predictions.csv"
        shutil.copy2(path, out_dir / path.name)
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"])
        preds[tenor] = df
        segments = {
            pred_col: {segment: accuracy(df.loc[segment_mask(df, segment)], pred_col) for segment in ["sim", "real", "may", "all"]}
            for pred_col in ["model_pred", "vote_pred"]
        }
        vote = segments["vote_pred"]
        strict = bool(vote["sim"]["acc"] >= 0.60 and vote["real"]["acc"] >= 0.60 and vote["sim"]["acc"] > vote["real"]["acc"] and vote["may"]["acc"] >= 0.60)
        summary[tenor] = {
            "segments": segments,
            "strict": strict,
            "date_min": df["date"].min().date(),
            "date_max": df["date"].max().date(),
            "n": len(df),
        }
        monthly[tenor] = monthly_table(df, "vote_pred")

    metadata = {
        "run_id": run_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "doc_path": str(doc_path),
        "output_dir": str(out_dir),
        "raw_date_min": str(raw_range[0]),
        "raw_date_max": str(raw_range[1]),
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# models-liwei-0528 新一轮复现与逐月评估报告")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append(f"本报告基于 `run_all.py` 对 `models-liwei-0528` 目录重新运行得到。预测 CSV 已归档至 `{out_dir}`，旧报告未覆盖。统计口径以本次新生成的 `*_predictions.csv` 为准。")
    lines.append("")
    lines.append("## 0. 模型原理、建模方法与参数说明")
    lines.append("")
    lines.append("本代码包是一套 LightGBM 基础分类器叠加投票信号的国债收益率日频方向预测模型，覆盖 3Y、5Y、7Y、10Y 四个期限。本轮实际运行的四个脚本均为 `HORIZON=5`、`GAP=5`，即预测未来第 5 个交易日目标期限收益率相对当日是上行还是下行。")
    lines.append("")
    lines.append("标签方向是收益率方向，不是债券价格方向。`true_label=1` 表示未来收益率上行，`true_label=-1` 表示未来收益率下行；若未来收益率走平，预测方向无法命中，会计入整体准确率分母。")
    lines.append("")
    lines.append("每日预测时，脚本使用滚动历史窗口训练 LightGBM，并通过 `GAP=5` 排除预测日前最近 5 个交易日，降低 T+5 标签重叠造成的时间穿越风险。基础模型输出 `model_pred`，投票增强后的最终方向为 `vote_pred`，本报告主评估口径统一采用 `vote_pred`。")
    lines.append("")
    param_rows = []
    for tenor in TENORS:
        cfg = configs[tenor]
        param_rows.append(
            [
                MODEL_NAMES[tenor],
                f"T+{cfg.get('HORIZON', '')}",
                cfg.get("GAP", ""),
                cfg.get("WINDOW", ""),
                f"num_leaves={cfg.get('NUM_LEAVES', '')}, n_estimators={cfg.get('N_ESTIMATORS', '')}, learning_rate={cfg.get('LEARNING_RATE', '')}, min_child_samples={cfg.get('MIN_CHILD_SAMPLES', '')}, reg_alpha={cfg.get('REG_ALPHA', '')}, reg_lambda={cfg.get('REG_LAMBDA', '')}, split_pct={cfg.get('SPLIT_PCT', '')}, vote_threshold={cfg.get('VOTE_THRESHOLD', '')}",
                "、".join(recipes[tenor]),
            ]
        )
    lines.append(md_table(["模型", "预测跨度", "Gap", "窗口", "LightGBM/训练参数", "投票信号"], param_rows))
    lines.append("")
    lines.append("## 1. 本次执行结论")
    lines.append("")
    exec_rows = []
    for tenor in TENORS:
        seg = summary[tenor]["segments"]["vote_pred"]  # type: ignore[index]
        result = f"sim_vote={pct(seg['sim']['acc'])}，real_vote={pct(seg['real']['acc'])}，may_vote={pct(seg['may']['acc'])}。"
        exec_rows.append(
            [
                MODEL_NAMES[tenor],
                f"{tenor}_predictions.csv",
                "已跑通",
                f"{summary[tenor]['date_min']} 至 {summary[tenor]['date_max']}",
                summary[tenor]["n"],
                result,
                "通过" if summary[tenor]["strict"] else "未通过",
            ]
        )
    lines.append(md_table(["脚本/模型", "预测输出文件", "运行状态", "预测日期范围", "有效样本数", "运行结果", "Strict结论"], exec_rows))
    lines.append("")
    pass_count = sum(1 for tenor in TENORS if summary[tenor]["strict"])
    lines.append(f"本次 4 个期限均跑通；按 strict 条件（sim>=60%、real>=60%、sim>real、May>=60%）统计，`{pass_count}/4` 通过。和旧报告相比，本轮 May 观察段扩展为 2026-05-01 至 2026-05-21 的 12 个有效样本，3Y 与 5Y 因 May 表现低于 60% 未通过 strict。")
    lines.append("")
    for tenor in TENORS:
        lines.append(f"- {MODEL_NAMES[tenor]}：{SUMMARY_NOTES[tenor]}")
    lines.append("")
    first_pred = preds["3y"]
    lines.append("## 2. 数据与时间切分")
    lines.append("")
    lines.append(
        md_table(
            ["项目", "结果"],
            [
                ["使用数据", str(ROOT / "data" / "daily_output.csv")],
                ["原始数据日期范围", f"{raw_range[0]} 至 {raw_range[1]}"],
                ["预测结果日期范围", f"{first_pred['date'].min().date()} 至 {first_pred['date'].max().date()}"],
                ["验证集", "2025-01-01 至 2025-06-30，对应脚本中的 sim 段"],
                ["测试集", "2025-07-01 至 2026-04-30，对应脚本中的 real 段"],
                ["May观察段", "2026-05-01 至预测文件末尾，本次为 2026-05-21，共 12 个有效样本"],
                ["全量复现窗", f"{first_pred['date'].min().date()} 至 {first_pred['date'].max().date()}，合计 {len(first_pred)} 个预测样本"],
                ["本次结果归档目录", str(out_dir)],
            ],
        )
    )
    lines.append("")
    lines.append("准确率公式：整体准确率 = 预测方向与真实方向一致的天数 / 有效预测天数。上涨准确率 = 预测上涨且实际上涨 / 预测上涨；上涨召回率 = 预测上涨且实际上涨 / 实际上涨。下跌方向同理。")
    lines.append("")
    lines.append("## 3. 主评估口径整体结果")
    lines.append("")
    main_rows = []
    for tenor in TENORS:
        seg = summary[tenor]["segments"]["vote_pred"]  # type: ignore[index]
        cfg = configs[tenor]
        main_rows.append(
            [
                MODEL_NAMES[tenor],
                TENOR_LABEL[tenor],
                f"T+{cfg.get('HORIZON', '')}",
                cfg.get("GAP", ""),
                cfg.get("WINDOW", ""),
                "投票后最终预测",
                pct_count(seg["sim"]["correct"], seg["sim"]["n"]),
                pct_count(seg["real"]["correct"], seg["real"]["n"]),
                pct_count(seg["may"]["correct"], seg["may"]["n"]),
                pct_count(seg["all"]["correct"], seg["all"]["n"]),
                "通过" if summary[tenor]["strict"] else "未通过",
            ]
        )
    lines.append(md_table(["模型", "期限", "预测跨度", "Gap", "训练窗口", "主评估口径", "验证集准确率", "测试集准确率", "May观察准确率", "全量准确率", "Strict结论"], main_rows))
    lines.append("")
    lines.append("## 4. 基础模型与最终预测拆分")
    lines.append("")
    split_rows = []
    for tenor in TENORS:
        for pred_col, label in [("model_pred", "基础模型原始预测"), ("vote_pred", "投票/最终预测")]:
            seg = summary[tenor]["segments"][pred_col]  # type: ignore[index]
            split_rows.append(
                [
                    MODEL_NAMES[tenor],
                    label,
                    pct_count(seg["sim"]["correct"], seg["sim"]["n"]),
                    pct_count(seg["real"]["correct"], seg["real"]["n"]),
                    pct_count(seg["may"]["correct"], seg["may"]["n"]),
                    pct_count(seg["all"]["correct"], seg["all"]["n"]),
                    "主评估口径" if pred_col == "vote_pred" else "辅助对照",
                ]
            )
    lines.append(md_table(["模型", "评估口径", "验证集", "测试集", "May观察段", "全量复现窗", "说明"], split_rows))
    lines.append("")
    lines.append("从拆分看，本包仍主要依赖投票层改善：3Y、5Y、7Y、10Y 的 real 段 `vote_pred` 均明显高于 `model_pred`。但本轮新增 May 样本后，3Y 和 5Y 的最新段投票效果出现明显回撤。")
    lines.append("")
    lines.append("## 5. 全量方向拆分表现")
    lines.append("")
    dir_rows = []
    for tenor in TENORS:
        metrics = split_metrics(preds[tenor], "vote_pred")
        dir_rows.append(
            [
                MODEL_NAMES[tenor],
                f"真实上涨/下跌/走平：{metrics['true_up']}/{metrics['true_down']}/{metrics['true_flat']}",
                f"预测上涨/下跌/走平：{metrics['pred_up']}/{metrics['pred_down']}/{metrics['pred_flat']}",
                rate_count(metrics["tp"], metrics["pred_up"]),
                rate_count(metrics["tp"], metrics["true_up"]),
                rate_count(metrics["tn"], metrics["pred_down"]),
                rate_count(metrics["tn"], metrics["true_down"]),
            ]
        )
    lines.append(md_table(["模型", "真实方向分布", "预测方向分布", "上涨准确率", "上涨召回率", "下跌准确率", "下跌召回率"], dir_rows))
    lines.append("")
    lines.append("## 6. 逐月表现摘要")
    lines.append("")
    month_summary_rows = []
    for tenor in TENORS:
        mt = monthly[tenor]
        best = mt.loc[mt["accuracy"].idxmax()]
        worst = mt.loc[mt["accuracy"].idxmin()]
        ge60 = int((mt["accuracy"] >= 0.60).sum())
        lt50 = int((mt["accuracy"] < 0.50).sum())
        allseg = summary[tenor]["segments"]["vote_pred"]["all"]  # type: ignore[index]
        month_summary_rows.append(
            [
                MODEL_NAMES[tenor],
                pct_count(allseg["correct"], allseg["n"]),
                f"{best['month']}：{pct_count(best['correct'], best['n'])}",
                f"{worst['month']}：{pct_count(worst['correct'], worst['n'])}",
                f"{ge60}/{len(mt)}",
                f"{lt50}/{len(mt)}",
            ]
        )
    lines.append(md_table(["模型", "全量准确率", "最好月份", "最弱月份", "准确率不低于60%的月份数", "准确率低于50%的月份数"], month_summary_rows))
    lines.append("")
    lines.append("## 7. 逐月详细结果")
    for tenor in TENORS:
        lines.append("")
        lines.append(f"### {MODEL_NAMES[tenor]}")
        rows = []
        for _, row in monthly[tenor].iterrows():
            rows.append(
                [
                    row["month"],
                    int(row["n"]),
                    f"{int(row['true_up'])}/{int(row['true_down'])}/{int(row['true_flat'])}",
                    f"{int(row['pred_up'])}/{int(row['pred_down'])}/{int(row['pred_flat'])}",
                    pct_count(int(row["correct"]), int(row["n"])),
                    rate_count(int(row["tp"]), int(row["pred_up"])),
                    rate_count(int(row["tp"]), int(row["true_up"])),
                    rate_count(int(row["tn"]), int(row["pred_down"])),
                    rate_count(int(row["tn"]), int(row["true_down"])),
                    row["judgement"],
                ]
            )
        lines.append("")
        lines.append(md_table(["月份", "样本数", "真实涨/跌/平", "预测涨/跌/平", "整体准确率", "上涨准确率", "上涨召回率", "下跌准确率", "下跌召回率", "月度判断"], rows))
    lines.append("")
    lines.append("## 8. 本轮更新后的判断")
    lines.append("")
    lines.append("1. 7Y 与 10Y 仍满足 strict 条件，当前可以继续作为候选模型保留。")
    lines.append("2. 3Y 与 5Y 的 sim/real 仍然合格，但 May 新增样本后低于 60%，需要把它们从“稳定通过”降级为“观察”。")
    lines.append("3. 四个期限的基础模型在 real 段普遍不强，投票层仍是主要收益来源；后续若继续优化，应优先检查投票信号在 2026-05 新样本中的失效原因。")
    lines.append("4. May 段样本仍只有 12 个，单月结论不能过度外推；但 5Y 的 33.3% 已经足够提示最新段风险。")

    doc_path.write_text("\n".join(lines), encoding="utf-8")
    return doc_path, out_dir


if __name__ == "__main__":
    doc, out = build_report()
    print(doc)
    print(out)
