"""Scratch cleanup must never change rendered output or remove retained assets."""
import asyncio
import importlib
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from test_upstream_sync import TEMP, package, renderer

theme = importlib.import_module(package.__name__ + ".priestess_features.ef_theme")
config = importlib.import_module(package.__name__ + ".config")


class RenderCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(TEMP.name) / self._testMethodName
        self.cache = self.root / "cache"
        self.cache.mkdir(parents=True)
        self.patches = [patch.object(config, "CACHE_DIR", self.cache), patch.object(renderer, "_inline_resources", side_effect=lambda html, base: html), patch.object(renderer, "_inline_remote_images", AsyncMock(side_effect=lambda html: html))]
        for p in self.patches: p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()

    def png(self, name="skland_card_20261027000000000001.png", *, outside=False):
        path = (self.root if outside else self.cache) / name
        Image.new("RGB", (30, 20), "white").save(path)
        return path

    def assert_webp(self, content):
        with Image.open(BytesIO(content)) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(image.size, (30, 20))

    async def test_success_discards_only_its_scratch_and_preserves_output(self):
        own = self.png()
        old = self.png("skland_card_previous.png")
        icon = self.png("character-icon.png")
        with patch.object(renderer, "_html_to_pic", AsyncMock(return_value=str(own))):
            result = await theme.render_page("<p>fixture</p>", 30)
        self.assert_webp(result)
        self.assertFalse(own.exists())
        self.assertTrue(old.exists())
        self.assertTrue(icon.exists())

    async def test_conversion_failure_preserves_png_and_original_error(self):
        own = self.png()
        with patch.object(renderer, "_html_to_pic", AsyncMock(return_value=str(own))), patch.object(theme, "to_webp", side_effect=ValueError("fixture conversion failed")):
            with self.assertRaisesRegex(ValueError, "fixture conversion failed"):
                await theme.render_page("fixture", 30)
        self.assertTrue(own.exists())

    async def test_read_failure_preserves_scratch(self):
        own = self.png()
        with patch.object(renderer, "_html_to_pic", AsyncMock(return_value=str(own))), patch.object(Path, "read_bytes", side_effect=PermissionError("fixture read lock")):
            with self.assertRaises(PermissionError):
                await theme.render_page("fixture", 30)
        self.assertTrue(own.exists())

    async def test_cleanup_failure_still_returns_valid_image(self):
        own = self.png()
        with patch.object(renderer, "_html_to_pic", AsyncMock(return_value=str(own))), patch.object(Path, "unlink", side_effect=PermissionError("fixture delete lock")):
            self.assert_webp(await theme.render_page("fixture", 30))
        self.assertTrue(own.exists())

    async def test_outside_cache_asset_names_and_links_are_not_deleted(self):
        outside = self.png(outside=True)
        asset = self.png("character-icon.png")
        for path in (outside, asset):
            with self.subTest(path=path.name), patch.object(renderer, "_html_to_pic", AsyncMock(return_value=str(path))):
                self.assert_webp(await theme.render_page("fixture", 30))
                self.assertTrue(path.exists())
        own = self.png()
        with patch.object(renderer, "_html_to_pic", AsyncMock(return_value=str(own))), patch.object(Path, "is_symlink", return_value=True):
            self.assert_webp(await theme.render_page("fixture", 30))
        self.assertTrue(own.exists())

    async def test_concurrent_render_calls_keep_their_outputs(self):
        paths = [self.png(f"skland_card_{i}.png") for i in range(2)]
        with patch.object(renderer, "_html_to_pic", AsyncMock(side_effect=[str(p) for p in paths])):
            results = await asyncio.gather(theme.render_page("one", 30), theme.render_page("two", 30))
        for result in results: self.assert_webp(result)
        self.assertTrue(all(not p.exists() for p in paths))


if __name__ == "__main__":
    unittest.main(verbosity=2)
