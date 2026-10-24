"""A plugin-local clock anchored to credential-free, verified HTTPS responses."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import secrets
import time

from astrbot.api import logger
import httpx

SOURCES = ("https://zonai.skland.com/", "https://as.hypergryph.com/")
REFRESH_SECONDS = 3600
RETRY_SECONDS = 300
MAX_RTT_SECONDS = 10


class NetworkClock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._epoch: float | None = None
        self._anchor = 0.0
        self._last_attempt: float | None = None

    def timestamp(self) -> float:
        if self._epoch is None:
            raise RuntimeError("尚未取得可信网络时间，请等待自动校时后重试。")
        return self._epoch + time.monotonic() - self._anchor

    def now(self, tz=timezone.utc) -> datetime:
        return datetime.fromtimestamp(self.timestamp(), tz)

    async def ensure_synced(self, *, force: bool = False) -> bool:
        async with self._lock:
            current = time.monotonic()
            if not force and self._epoch is not None and current - self._anchor < REFRESH_SECONDS:
                return True
            if self._last_attempt is not None and current - self._last_attempt < RETRY_SECONDS:
                return self._epoch is not None
            self._last_attempt = current
            async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), follow_redirects=False, trust_env=False) as client:
                for source in SOURCES:
                    try:
                        before = time.monotonic()
                        response = await client.get(source, params={"_priestess_clock": secrets.token_hex(8)}, headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"})
                        after = time.monotonic()
                        rtt = after - before
                        # A 404/401 still carries server time; reject failures and stale cache responses.
                        if response.status_code >= 500 or rtt < 0 or rtt > MAX_RTT_SECONDS:
                            continue
                        if float(response.headers.get("age", "0")) > 1:
                            continue
                        server = parsedate_to_datetime(response.headers["date"])
                        if server.tzinfo is None:
                            continue
                        epoch = server.timestamp() + rtt / 2
                        datetime.fromtimestamp(epoch, timezone.utc)
                        self._epoch, self._anchor = epoch, after
                        logger.info("[Priestess] 网络校时成功：%s；与系统时间偏差 %+.1f 秒（不修改系统时间）", source, epoch - time.time())
                        return True
                    except (httpx.HTTPError, KeyError, ValueError, TypeError, OverflowError, OSError) as error:
                        logger.debug("[Priestess] 校时源不可用：%s（%s）", source, type(error).__name__)
            logger.warning("[Priestess] 网络校时失败，5 分钟后重试；%s", "继续使用上次网络时间和单调时钟" if self._epoch is not None else "暂缓签到，避免用错误的电脑时间签名")
            return self._epoch is not None


network_clock = NetworkClock()
