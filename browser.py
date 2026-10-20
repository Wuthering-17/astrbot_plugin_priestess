"""Launch a headless browser for imports and image rendering."""

from __future__ import annotations

from astrbot.api import logger
import sys

from playwright.async_api import Browser, Error, Playwright



class BrowserUnavailableError(RuntimeError):
    """No browser supported by Playwright is available on this host."""


async def launch_browser(playwright: Playwright) -> Browser:
    try:
        return await playwright.chromium.launch(headless=True)
    except Error as error:
        if "Executable doesn't exist" not in str(error):
            raise
        missing_browser = error

    if sys.platform == "win32":
        for channel in ("msedge", "chrome"):
            try:
                browser = await playwright.chromium.launch(channel=channel, headless=True)
            except Error:
                continue
            logger.info("[Priestess] Using installed browser channel: %s", channel)
            return browser

    raise BrowserUnavailableError(
        "浏览器组件不可用，请管理员运行 python -m playwright install chromium；"
        "Windows 也可安装 Edge 或 Chrome 后重试。"
    ) from missing_browser
