"""weekly_10y_d_overlay_0529 跨年同锚点行保持 week_id 稳定顺序。"""
from __future__ import annotations

import pandas as pd

from schemes.weekly_10y_d_overlay_0529.core import d_overlay


def _anchor(week_id: int) -> str:
    """复刻 schemes/weekly_10y_d_overlay_0529/predict.py:_legacy_segment_anchor_date。"""
    text = str(int(week_id))
    year = int(text[:4])
    ordinal = int(text[4:])
    jan1 = pd.Timestamp(f"{year}-01-01")
    first_thu = jan1 + pd.Timedelta(days=(3 - jan1.weekday()) % 7)
    first_anchor = first_thu - pd.Timedelta(days=3)
    return (first_anchor + pd.Timedelta(weeks=ordinal - 1)).strftime("%Y-%m-%d")


def _week_id_sequence() -> list[int]:
    """含跨年碰撞的 week_id 序列：每年 1..52，额外插入 202553（与 202601 同 anchor）。"""
    weeks: list[int] = []
    for year in range(2019, 2027):
        for week in range(1, 53):
            weeks.append(year * 100 + week)
    weeks.append(202553)
    return sorted(set(weeks))


def _make_weekly(window_size: int) -> pd.DataFrame:
    """构造能用 202553 标签暴露跨年排序漂移的周频输入。"""
    seq = [w for w in _week_id_sequence() if w <= 202628][-window_size:]
    assert {202552, 202553, 202601, 202602}.issubset(set(seq))
    rows = []
    for idx, week_id in enumerate(seq):
        model_date = _anchor(week_id)
        if week_id == 202601:
            rate = 101.0
        elif week_id == 202602:
            rate = 99.0
        else:
            rate = 100.0 + 0.01 * idx
        rows.append(
            {
                "week_id": week_id,
                "week_date": model_date,
                "model_date": model_date,
                "TB0YWI3C": rate,
                "TB1YWI3C": rate * 0.9,
                "TB5YWI3C": rate * 1.1,
            }
        )
    return pd.DataFrame(rows)


def _label_of(engineered: pd.DataFrame, week_id: int) -> int:
    return int(engineered.loc[engineered["week_id"] == week_id, "label_5d"].iloc[0])


def _position_of(engineered: pd.DataFrame, week_id: int) -> int:
    return int(engineered.index[engineered["week_id"] == week_id][0])


def test_cross_year_tied_date_orders_by_week_id_and_labels_correctly() -> None:
    """36 行翻转窗口：202553 必须排在 202601 之前，label_5d 接到 202601（+1）。"""
    engineered, _ = d_overlay.load_engineered_frame(_make_weekly(36))
    assert _position_of(engineered, 202553) < _position_of(engineered, 202601)
    assert _label_of(engineered, 202553) == 1


def test_cross_year_label_is_window_shift_invariant() -> None:
    """窗口再滚动一周也不能改变跨年周标签：多个尺寸下 202553 恒为 +1。"""
    labels = {
        size: _label_of(d_overlay.load_engineered_frame(_make_weekly(size))[0], 202553)
        for size in range(36, 61)
    }
    assert set(labels.values()) == {1}, f"跨年标签随窗口漂移: {labels}"
