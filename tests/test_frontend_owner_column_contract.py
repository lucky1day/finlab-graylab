"""排行表「来源」列的结构契约。

表头列数、数据行 <td> 数、空态 colspan 三者必须一致，否则表格会错位。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "frontend"


def _ranking_header_cells() -> list[str]:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    table = html.split('class="factor-ranking-table"', 1)[1]
    thead = table.split("</thead>", 1)[0]
    return re.findall(r"<th[^>]*>(.*?)</th>", thead, re.S)


def test_header_declares_owner_column() -> None:
    cells = _ranking_header_cells()
    assert any("来源" in cell for cell in cells), cells


def test_row_cell_count_matches_header() -> None:
    js = (FRONTEND / "aifin-shell.js").read_text(encoding="utf-8")
    body = js.split("function renderSchemeRankingRow", 1)[1].split(
        "\n  function ", 1
    )[0]
    assert body.count("'<td") + body.count('"<td') == len(_ranking_header_cells())


def test_empty_state_colspan_matches_header() -> None:
    js = (FRONTEND / "aifin-shell.js").read_text(encoding="utf-8")
    body = js.split("function renderSchemeRanking(", 1)[1].split(
        "\n  function ", 1
    )[0]
    match = re.search(r'colspan="(\d+)" class="factor-empty-cell"', body)
    assert match is not None
    assert int(match.group(1)) == len(_ranking_header_cells())
