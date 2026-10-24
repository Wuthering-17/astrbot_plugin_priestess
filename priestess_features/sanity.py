"""AstrBot implementation of sanity queries and group full-sanity reminders."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from ..api import SklandAPI
from ..db import (
    SkUser,
    get_default_ark_character,
    get_default_ef_character,
    get_session,
    get_user_by_platform,
)
from ..schemas import CRED
from ..utils import call_api_with_refresh
from ..notifications import notification_enabled

GAMES = {"arknights": "明日方舟", "endfield": "终末地"}


class SanityReminder:
    def __init__(self, plugin, data_dir: Path) -> None:
        self.plugin = plugin
        self.path = data_dir / "sanity_subscriptions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, subscriptions: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(subscriptions, ensure_ascii=False), "utf-8")
        temporary.replace(self.path)

    async def _fetch(self, user: SkUser, game: str) -> tuple[int, int, float] | None:
        async with await get_session() as session:
            user = await get_user_by_platform(session, user.platform_user_id)
            if user is None:
                return None
            char = (
                await get_default_ark_character(session, user)
                if game == "arknights"
                else await get_default_ef_character(session, user)
            )
            if char is None:
                return None
            cred = CRED(cred=user.cred, token=user.cred_token)
            if game == "arknights":
                card = await call_api_with_refresh(user, SklandAPI.ark_card, cred, str(char.uid))
                ap = card.status.ap
                result = int(ap.ap_now), int(ap.max), float(ap.completeRecoveryTime)
            else:
                card = await call_api_with_refresh(
                    user,
                    SklandAPI.endfield_card,
                    cred,
                    user.user_id or user.platform_user_id,
                    char,
                )
                dungeon = card.dungeon
                result = int(dungeon.curStamina or 0), int(dungeon.maxStamina or 0), float(dungeon.maxTs or time.time())
            await session.commit()
            return result

    async def status(self, event: AstrMessageEvent) -> str:
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
        if user is None:
            return "未绑定森空岛账号，请先发送 /skl绑定。"
        rows = []
        for game, label in GAMES.items():
            try:
                state = await self._fetch(user, game)
                if state is None:
                    continue
                current, maximum, full_ts = state
                note = "已回满" if current >= maximum or time.time() >= full_ts else f"约 {self._remaining(full_ts - time.time())}后回满"
                rows.append(f"{label}：理智 {min(current, maximum)}/{maximum}，{note}")
            except Exception as error:
                rows.append(f"{label}：查询失败（{type(error).__name__}）")
        return "\n".join(rows) or "没有找到已绑定的明日方舟或终末地角色。"

    async def configure(self, event: AstrMessageEvent, action: str) -> str:
        if action not in ("开", "关"):
            return "用法：/理智提醒 开 或 /理智提醒 关"
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        if action == "开" and not group_id:
            return "请在群里发送 /理智提醒 开，理智回满时会在该群 @你。"
        async with self._lock:
            subscriptions = self._load()
            if action == "关":
                removed = subscriptions.pop(sender_id, None)
                self._save(subscriptions)
                return "已关闭理智回满提醒。" if removed else "你还没有开启理智提醒。"
            async with await get_session() as session:
                user = await get_user_by_platform(session, sender_id)
            if user is None:
                return "未绑定森空岛账号，请先发送 /skl绑定。"
            games = {}
            for game, label in GAMES.items():
                try:
                    state = await self._fetch(user, game)
                except Exception:
                    state = None
                if state is not None:
                    current, maximum, full_ts = state
                    games[game] = {"full_ts": full_ts, "notified": current >= maximum or time.time() >= full_ts}
            if not games:
                return "没有找到已绑定的明日方舟或终末地角色。"
            subscriptions[sender_id] = {"group_id": group_id, "umo": event.unified_msg_origin, "games": games}
            self._save(subscriptions)
        labels = "、".join(GAMES[game] for game in games)
        return f"已开启理智回满提醒（{labels}），回满时会在本群 @你。关闭请发送 /理智提醒 关。"

    @staticmethod
    def _remaining(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 3600}小时{seconds % 3600 // 60}分"

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._check()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=600)
            except asyncio.TimeoutError:
                pass

    async def _check(self) -> None:
        async with self._lock:
            subscriptions = self._load()
            changed = False
            for qq, subscription in list(subscriptions.items()):
                async with await get_session() as session:
                    user = await get_user_by_platform(session, qq)
                if user is None:
                    del subscriptions[qq]
                    changed = True
                    continue
                for game, state in subscription.get("games", {}).items():
                    if not notification_enabled(self.plugin, f"sanity_{game}"):
                        continue
                    try:
                        current, maximum, full_ts = await self._fetch(user, game)
                    except Exception as error:
                        logger.warning("Sanity check failed for %s: %s", game, type(error).__name__)
                        continue
                    full = current >= maximum or time.time() >= full_ts
                    if not full:
                        state["full_ts"], state["notified"] = full_ts, False
                        changed = True
                        continue
                    if state.get("notified"):
                        continue
                    try:
                        chain = MessageChain()
                        chain.chain = [Comp.At(qq=qq), Comp.Plain(f" 你的{GAMES[game]}理智已回满（{maximum}/{maximum}），记得上线使用～")]
                        await self.plugin.context.send_message(subscription["umo"], chain)
                        state["full_ts"], state["notified"] = None, True
                        changed = True
                    except Exception as error:
                        logger.warning("Sanity reminder send failed: %s", type(error).__name__)
            if changed:
                self._save(subscriptions)
