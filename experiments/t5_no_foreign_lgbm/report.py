from __future__ import annotations

from typing import Any, Mapping

from .metrics import MetricCounts


AGGREGATE_PERIOD = "2026-04..06"


def render_report(bundle: Mapping[str, Any]) -> str:
    metadata = dict(bundle.get("metadata") or {})
    manifest_order = [str(value) for value in bundle.get("manifest_order") or []]
    schemes = {
        str(item["scheme_id"]): dict(item)
        for item in bundle.get("schemes") or []
    }
    if len(manifest_order) != 20 or len(set(manifest_order)) != 20:
        raise RuntimeError("report manifest must contain exactly 20 unique schemes")
    if set(manifest_order) != set(schemes):
        raise RuntimeError("report scheme results do not match manifest")

    status_counts: dict[str, int] = {}
    for scheme in schemes.values():
        status = str(scheme.get("status") or "failed")
        status_counts[status] = status_counts.get(status, 0) + 1

    lines = [
        "# 日频 T+5 LGBM 去国外因子对比实验",
        "",
        "> 本报告来自隔离、只读实验；未修改灰度实验室的配置、registry、数据库、现有缓存或现有结果。",
        "",
        "## 执行摘要",
        "",
        (
            f"- 成功：{status_counts.get('success', 0)}；"
            f"不适用：{status_counts.get('not_applicable', 0)}；"
            f"失败：{status_counts.get('failed', 0)}。"
        ),
        "- 月份按 target_date 归属；准确率为正确出手数/出手数，出手率为出手数/有效样本数。",
        "- 原方案 Phase A 复用至 2026-03-31 目标；从首个 2026 年 4 月目标样本开始重算去国外因子的 LightGBM。",
        "",
        "## 可复现信息",
        "",
        f"- 代码提交：{metadata.get('code_commit', 'unknown')}",
        f"- Python {metadata.get('python_version', 'unknown')}",
        f"- LightGBM {metadata.get('lightgbm_version', 'unknown')}",
        f"- 源文件运行前后未变化：{'是' if metadata.get('source_unchanged') else '否'}",
        "",
        "| 输入 | SHA-256 |",
        "| --- | --- |",
    ]
    for name, digest in sorted(dict(metadata.get("source_hashes") or {}).items()):
        lines.append(f"| {name} | {digest} |")

    lines.extend(
        [
            "",
            "## 配置总览",
            "",
            "| 配置 | 状态 | 原方案准确率 | 去国外准确率 | 准确率变化 | 原方案出手率 | 去国外出手率 | 出手率变化 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for scheme_id in manifest_order:
        scheme = schemes[scheme_id]
        status = str(scheme.get("status") or "failed")
        if status == "failed":
            lines.append(f"| {scheme_id} | 失败 | — | — | — | — | — | — |")
            continue
        aggregate = _comparison(scheme, AGGREGATE_PERIOD)
        baseline = _counts(aggregate["baseline"])
        ablation = _counts(aggregate["ablation"])
        status_label = "成功" if status == "success" else "不适用"
        lines.append(
            "| "
            + " | ".join(
                [
                    scheme_id,
                    status_label,
                    _rate(baseline.accuracy),
                    _rate(ablation.accuracy),
                    _delta(aggregate.get("accuracy_delta")),
                    _rate(baseline.trade_rate),
                    _rate(ablation.trade_rate),
                    _delta(aggregate.get("trade_rate_delta")),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 配置明细", ""])
    for scheme_id in manifest_order:
        scheme = schemes[scheme_id]
        status = str(scheme.get("status") or "failed")
        lines.extend([f"### {scheme_id}", ""])
        if status == "failed":
            lines.extend(
                [
                    f"- 状态：失败",
                    f"- 失败原因：{scheme.get('reason') or '未提供'}",
                    "",
                ]
            )
            continue

        audit = dict(scheme.get("factor_audit") or {})
        removed_sources = [str(value) for value in audit.get("removed_sources") or []]
        removed_columns = [str(value) for value in audit.get("removed_columns") or []]
        ust_sources = [str(value) for value in audit.get("ust_sources_used") or []]
        lines.extend(
            [
                f"- 状态：{'成功' if status == 'success' else '不适用'}",
                f"- 候选特征数：{int(audit.get('candidate_count') or 0)}",
                f"- 保留特征数：{int(audit.get('retained_count') or 0)}",
                f"- 实际移除国外来源：{', '.join(removed_sources) if removed_sources else '无'}",
                f"- 实际移除派生列数：{len(removed_columns)}",
                f"- 美国国债收益率实际使用数：{len(ust_sources)}"
                + (f"（{', '.join(ust_sources)}）" if ust_sources else ""),
                "",
                "| 月份 | 原方案正确/出手/有效 | 原方案准确率 | 原方案出手率 | 去国外正确/出手/有效 | 去国外准确率 | 去国外出手率 | 准确率变化 | 出手率变化 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for comparison in scheme.get("comparisons") or []:
            baseline = _counts(comparison["baseline"])
            ablation = _counts(comparison["ablation"])
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(comparison["period"]),
                        _count_triplet(baseline),
                        _rate(baseline.accuracy),
                        _rate(baseline.trade_rate),
                        _count_triplet(ablation),
                        _rate(ablation.accuracy),
                        _rate(ablation.trade_rate),
                        _delta(comparison.get("accuracy_delta")),
                        _delta(comparison.get("trade_rate_delta")),
                    ]
                )
                + " |"
            )
        changed_dates = [
            str(value) for value in scheme.get("changed_target_dates") or []
        ]
        lines.extend(
            [
                "",
                "- 预测变化 target_date："
                + (", ".join(changed_dates) if changed_dates else "无"),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _comparison(scheme: Mapping[str, Any], period: str) -> Mapping[str, Any]:
    matches = [
        value
        for value in scheme.get("comparisons") or []
        if str(value.get("period")) == period
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"{scheme.get('scheme_id')} must contain one {period} comparison"
        )
    return matches[0]


def _counts(value: MetricCounts | Mapping[str, Any]) -> MetricCounts:
    if isinstance(value, MetricCounts):
        return value
    return MetricCounts(
        eligible=int(value["eligible"]),
        trades=int(value["trades"]),
        correct=int(value["correct"]),
    )


def _rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _delta(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) * 100:+.2f} pp"


def _count_triplet(value: MetricCounts) -> str:
    return f"{value.correct}/{value.trades}/{value.eligible}"
