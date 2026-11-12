"""promote.py 的调度窗口保护：触发点前 10 分钟到后 60 分钟内不得改变主机状态。"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import promote  # noqa: E402

TZ = promote.TZ


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


@pytest.mark.parametrize(
    "when, safe_at",
    [
        ("2026-11-11 07:30", "08:03"),  # 周三：日频 07:03 之后仍在保护期
        ("2026-11-11 06:25", "08:03"),  # data-bridge 前 10 分钟内，且与日频保护期相连
        ("2026-11-14 11:40", "12:30"),  # 周六：周频 11:30
        ("2026-11-14 07:10", "07:30"),  # 周六无日频，只有 data-bridge
        ("2026-11-12 18:55", "20:00"),  # 月度 18:00 与 actuals 19:00 连成一段
        ("2026-11-15 23:50", "00:45"),  # 跨午夜
    ],
)
def test_busy(when, safe_at):
    until = promote.busy_until(at(when))
    assert until is not None and until.strftime("%H:%M") == safe_at


@pytest.mark.parametrize("when", ["2026-11-11 10:00", "2026-11-15 11:40", "2026-11-15 07:40", "2026-11-12 16:30"])
def test_clear(when):
    assert promote.busy_until(at(when)) is None
