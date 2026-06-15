from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.main import FRONTEND_CACHE_CONTROL, NoCacheFrontendStaticFiles


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_INDEX = PROJECT_ROOT / "frontend" / "index.html"


class FrontendStaticCacheTests(unittest.TestCase):
    def test_frontend_static_files_force_browser_revalidation(self) -> None:
        """前端静态资源响应必须禁用缓存，避免 iframe 继续展示旧 JS/CSS。"""

        async def _fetch_headers() -> dict[str, str]:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "index.html").write_text("<html></html>", encoding="utf-8")
                static = NoCacheFrontendStaticFiles(directory=directory, html=True)
                response = await static.get_response(
                    "index.html",
                    {"type": "http", "method": "GET", "path": "/index.html", "headers": []},
                )
                return dict(response.headers)

        headers = asyncio.run(_fetch_headers())

        self.assertEqual(headers["cache-control"], FRONTEND_CACHE_CONTROL)
        self.assertEqual(headers["pragma"], "no-cache")
        self.assertEqual(headers["expires"], "0")

    def test_index_uses_current_asset_cache_buster(self) -> None:
        html = FRONTEND_INDEX.read_text(encoding="utf-8")

        self.assertIn('href="aifin-shell.css?v=20260615a"', html)
        self.assertIn('src="aifin-shell.js?v=20260615a"', html)
