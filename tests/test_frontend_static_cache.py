from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend import main


NoCacheFrontendStaticFiles = main.NoCacheFrontendStaticFiles


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_INDEX = PROJECT_ROOT / "frontend" / "index.html"


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
        html = FRONTEND_INDEX.read_text(encoding="utf-8")

        self.assertIn('href="aifin-shell.css?v=20260705a"', html)
        self.assertIn('src="aifin-shell.js?v=20260721a"', html)
