from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from starlette.exceptions import HTTPException

from backend import main


NoCacheFrontendStaticFiles = main.NoCacheFrontendStaticFiles


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
FRONTEND_INDEX = FRONTEND_ROOT / "index.html"
FRONTEND_CSS = FRONTEND_ROOT / "aifin-shell.css"


class _FrontendIndexAssetParser(HTMLParser):
    """解析真实 index 中的 stylesheet 与脚本资源 URL。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href", ""))
        if tag == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"] or "")


def _scope(path: str, query_string: bytes = b"") -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/{path}",
        "raw_path": f"/{path}".encode("ascii"),
        "query_string": query_string,
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 43123),
        "server": ("127.0.0.1", 18101),
    }


class FrontendStaticCacheTests(unittest.TestCase):
    def test_frontend_static_files_apply_exact_versioned_asset_policy(self) -> None:
        """只有 index 当前精确引用的 CSS/JS token 可以 immutable。"""

        async def _fetch_headers(
            static: NoCacheFrontendStaticFiles,
            path: str,
            query_string: bytes = b"",
        ) -> dict[str, str]:
            response = await static.get_response(
                path,
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/{path}",
                    "query_string": query_string,
                    "headers": [],
                },
            )
            return dict(response.headers)

        async def _exercise() -> dict[str, dict[str, str]]:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "index.html").write_text(
                    '<link rel="stylesheet" href="app.css?v=current-css">'
                    '<script src="app.js?v=current-js"></script>'
                    '<script src="https://cdn.example/cdn.js?v=external"></script>',
                    encoding="utf-8",
                )
                for name in (
                    "app.css",
                    "app.js",
                    "icon.svg",
                    "cdn.js",
                    "unknown.txt",
                ):
                    Path(directory, name).write_text("x", encoding="utf-8")
                static = NoCacheFrontendStaticFiles(directory=directory, html=True)
                return {
                    "index": await _fetch_headers(static, "index.html"),
                    "versioned_css": await _fetch_headers(
                        static, "app.css", b"v=current-css"
                    ),
                    "versioned_js": await _fetch_headers(
                        static, "app.js", b"v=current-js"
                    ),
                    "unversioned_css": await _fetch_headers(static, "app.css"),
                    "old_css": await _fetch_headers(static, "app.css", b"v=old"),
                    "wrong_path": await _fetch_headers(
                        static, "app.js", b"v=current-css"
                    ),
                    "svg": await _fetch_headers(
                        static, "icon.svg", b"v=current-js"
                    ),
                    "external_ref": await _fetch_headers(
                        static, "cdn.js", b"v=external"
                    ),
                    "unknown": await _fetch_headers(static, "unknown.txt"),
                }

        headers = asyncio.run(_exercise())

        self.assertTrue(hasattr(main, "INDEX_CACHE_CONTROL"))
        self.assertTrue(hasattr(main, "VERSIONED_ASSET_CACHE_CONTROL"))
        self.assertTrue(hasattr(main, "UNVERSIONED_ASSET_CACHE_CONTROL"))
        self.assertEqual(
            headers["index"]["cache-control"], main.INDEX_CACHE_CONTROL
        )
        self.assertEqual(
            headers["versioned_css"]["cache-control"],
            main.VERSIONED_ASSET_CACHE_CONTROL,
        )
        self.assertEqual(
            headers["versioned_js"]["cache-control"],
            main.VERSIONED_ASSET_CACHE_CONTROL,
        )
        self.assertNotIn("pragma", headers["versioned_css"])
        self.assertNotIn("expires", headers["versioned_css"])
        for name in (
            "unversioned_css",
            "old_css",
            "wrong_path",
            "svg",
            "external_ref",
            "unknown",
        ):
            self.assertEqual(
                headers[name]["cache-control"],
                main.UNVERSIONED_ASSET_CACHE_CONTROL,
            )
            self.assertEqual(headers[name]["pragma"], "no-cache")
            self.assertEqual(headers[name]["expires"], "0")

    def test_index_uses_current_asset_cache_buster(self) -> None:
        css_token = hashlib.sha256(FRONTEND_CSS.read_bytes()).hexdigest()
        parser = _FrontendIndexAssetParser()
        parser.feed(FRONTEND_INDEX.read_text(encoding="utf-8"))

        css_version_tokens = {
            value
            for stylesheet in parser.stylesheets
            for key, value in parse_qsl(
                urlsplit(stylesheet).query,
                keep_blank_values=True,
            )
            if key == "v"
        }
        self.assertEqual(css_version_tokens, {css_token})
        self.assertEqual(
            parser.stylesheets,
            [f"aifin-shell.css?v={css_token}"],
        )
        self.assertEqual(parser.scripts, ["aifin-shell.js?v=20260806b"])

        async def _fetch_headers(
            static: NoCacheFrontendStaticFiles,
            query_string: bytes,
        ) -> dict[str, str]:
            response = await static.get_response(
                "aifin-shell.css",
                _scope("aifin-shell.css", query_string),
            )
            return dict(response.headers)

        async def _exercise() -> dict[str, dict[str, str]]:
            static = NoCacheFrontendStaticFiles(directory=FRONTEND_ROOT, html=True)
            return {
                "current_css": await _fetch_headers(
                    static,
                    f"v={css_token}".encode("ascii"),
                ),
                "stale_css": await _fetch_headers(static, b"v=20260806b"),
            }

        headers = asyncio.run(_exercise())
        self.assertEqual(
            headers["current_css"]["cache-control"],
            main.VERSIONED_ASSET_CACHE_CONTROL,
        )
        self.assertNotIn("pragma", headers["current_css"])
        self.assertNotIn("expires", headers["current_css"])
        self.assertEqual(
            headers["stale_css"]["cache-control"],
            main.UNVERSIONED_ASSET_CACHE_CONTROL,
        )
        self.assertEqual(headers["stale_css"]["pragma"], "no-cache")
        self.assertEqual(headers["stale_css"]["expires"], "0")

    def test_missing_versioned_asset_exception_is_explicitly_revalidated(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "index.html").write_text(
                    '<script src="current.js?v=current"></script>',
                    encoding="utf-8",
                )
                static = NoCacheFrontendStaticFiles(
                    directory=directory,
                    html=True,
                )
                await static.get_response(
                    "current.js",
                    _scope("current.js", b"v=current"),
                )

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(exercise())

        self.assertEqual(caught.exception.status_code, 404)
        headers = {
            key.casefold(): value
            for key, value in (caught.exception.headers or {}).items()
        }
        self.assertEqual(
            headers["cache-control"],
            main.UNVERSIONED_ASSET_CACHE_CONTROL,
        )
        self.assertEqual(headers["pragma"], "no-cache")
        self.assertEqual(headers["expires"], "0")

    def test_html_404_fallback_for_versioned_js_is_never_immutable(self) -> None:
        async def exercise() -> tuple[int, dict[str, str]]:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "index.html").write_text(
                    '<script src="current.js?v=current"></script>',
                    encoding="utf-8",
                )
                Path(directory, "404.html").write_text(
                    "<html>missing</html>",
                    encoding="utf-8",
                )
                static = NoCacheFrontendStaticFiles(
                    directory=directory,
                    html=True,
                )
                response = await static.get_response(
                    "current.js",
                    _scope("current.js", b"v=current"),
                )
                return response.status_code, dict(response.headers)

        status, headers = asyncio.run(exercise())

        self.assertEqual(status, 404)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertEqual(
            headers["cache-control"],
            main.UNVERSIONED_ASSET_CACHE_CONTROL,
        )
        self.assertEqual(headers["pragma"], "no-cache")
        self.assertEqual(headers["expires"], "0")

    def test_js_directory_redirect_is_never_immutable(self) -> None:
        async def exercise() -> tuple[int, dict[str, str]]:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "index.html").write_text(
                    '<script src="bundle.js?v=current"></script>',
                    encoding="utf-8",
                )
                Path(directory, "bundle.js").mkdir()
                Path(directory, "bundle.js", "index.html").write_text(
                    "<html>directory</html>",
                    encoding="utf-8",
                )
                static = NoCacheFrontendStaticFiles(
                    directory=directory,
                    html=True,
                )
                response = await static.get_response(
                    "bundle.js",
                    _scope("bundle.js", b"v=current"),
                )
                return response.status_code, dict(response.headers)

        status, headers = asyncio.run(exercise())

        self.assertEqual(status, 307)
        self.assertEqual(
            headers["cache-control"],
            main.UNVERSIONED_ASSET_CACHE_CONTROL,
        )
        self.assertEqual(headers["pragma"], "no-cache")
        self.assertEqual(headers["expires"], "0")

    def test_version_token_matching_allows_extras_but_rejects_ambiguity(self) -> None:
        async def exercise() -> dict[bytes, dict[str, str]]:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "index.html").write_text(
                    '<script src="app.js?v=one"></script>'
                    '<script src="app.js?version=two"></script>',
                    encoding="utf-8",
                )
                Path(directory, "app.js").write_text(
                    "console.log('ok')",
                    encoding="utf-8",
                )
                static = NoCacheFrontendStaticFiles(
                    directory=directory,
                    html=True,
                )
                queries = (
                    b"foo=x&v=one",
                    b"version=two&foo=x",
                    b"v=one&v=one",
                    b"v=old",
                    b"v=",
                    b"v=%6fne",
                    b"v=one%20",
                )
                results: dict[bytes, dict[str, str]] = {}
                for query in queries:
                    response = await static.get_response(
                        "app.js",
                        _scope("app.js", query),
                    )
                    results[query] = dict(response.headers)
                return results

        headers = asyncio.run(exercise())

        for query in (b"foo=x&v=one", b"version=two&foo=x"):
            self.assertEqual(
                headers[query]["cache-control"],
                main.VERSIONED_ASSET_CACHE_CONTROL,
            )
            self.assertNotIn("pragma", headers[query])
            self.assertNotIn("expires", headers[query])
        for query in (
            b"v=one&v=one",
            b"v=old",
            b"v=",
            b"v=%6fne",
            b"v=one%20",
        ):
            self.assertEqual(
                headers[query]["cache-control"],
                main.UNVERSIONED_ASSET_CACHE_CONTROL,
            )
            self.assertEqual(headers[query]["pragma"], "no-cache")
            self.assertEqual(headers[query]["expires"], "0")
