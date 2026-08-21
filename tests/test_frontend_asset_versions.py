from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path


class _ScriptParser(HTMLParser):
    """收集首页中声明的脚本资源。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"] or "")
        if (
            tag == "link"
            and attributes.get("rel") == "stylesheet"
            and attributes.get("href")
        ):
            self.stylesheets.append(attributes["href"] or "")


def test_index_references_factor_lab_javascript_by_content_hash() -> None:
    project_root = Path(__file__).resolve().parents[1]
    frontend_root = project_root / "frontend"
    parser = _ScriptParser()
    parser.feed((frontend_root / "index.html").read_text(encoding="utf-8"))

    javascript_hash = hashlib.sha256(
        (frontend_root / "aifin-shell.js").read_bytes()
    ).hexdigest()
    assert parser.scripts == [f"aifin-shell.js?v={javascript_hash}"]


def test_index_references_factor_lab_stylesheet_by_content_hash() -> None:
    project_root = Path(__file__).resolve().parents[1]
    frontend_root = project_root / "frontend"
    parser = _ScriptParser()
    parser.feed((frontend_root / "index.html").read_text(encoding="utf-8"))

    stylesheet_hash = hashlib.sha256(
        (frontend_root / "aifin-shell.css").read_bytes()
    ).hexdigest()
    assert parser.stylesheets == [f"aifin-shell.css?v={stylesheet_hash}"]
