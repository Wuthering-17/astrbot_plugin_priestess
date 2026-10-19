"""Safe Heybox history import and reconciliation for AstrBot's Endfield records."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from sqlalchemy import delete, func, select

from ..db import Character, GachaRecord, get_ef_characters, get_session, get_user_by_platform
from ..data_source import ef_gacha_pool_data
from .guide import CN
from .heybox_plan import Official, Plan, build_plan

TOOL_URL = "https://www.xiaoheihe.cn/game/endfield/card_statistic?user_id={heybox_id}"
OVERVIEW_PATH = "/game/endfield/player/overview"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
FETCH_TIMEOUT = 15000
FETCH_ATTEMPTS = 3
COOLDOWN = 300


class HeyboxImporter:
    def __init__(self, plugin, data_dir: Path) -> None:
        self.plugin = plugin
        self.data_dir = data_dir / "heybox_imports"
        self._lock = asyncio.Lock()
        self._last_run: dict[str, float] = {}

    async def _fetch_once(self, heybox_id: str) -> dict:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                context = await browser.new_context(
                    device_scale_factor=1,
                    viewport={"width": 430, "height": 900},
                    user_agent=USER_AGENT,
                )
                page = await context.new_page()
                async with page.expect_response(
                    lambda response: OVERVIEW_PATH in response.url,
                    timeout=FETCH_TIMEOUT,
                ) as pending:
                    await page.goto(
                        TOOL_URL.format(heybox_id=heybox_id),
                        wait_until="domcontentloaded",
                        timeout=FETCH_TIMEOUT,
                    )
                response = await pending.value
                return await response.json()
            finally:
                await browser.close()

    async def _fetch(self, heybox_id: str) -> dict:
        last_error = None
        for attempt in range(FETCH_ATTEMPTS):
            try:
                payload = await self._fetch_once(heybox_id)
            except Exception as error:
                last_error = error
                continue
            if payload.get("status") != "ok":
                raise RuntimeError(f"小黑盒返回状态：{payload.get('status')}")
            return payload.get("result") or {}
        raise RuntimeError(f"小黑盒读取失败：{type(last_error).__name__ if last_error else 'unknown'}")

    def _pool_windows(self) -> dict[str, tuple[int, int]]:
        loaded = self.plugin.wiki.resolver()
        if not loaded:
            return {}
        windows = {}
        for pool in (loaded[0].get("calendar") or {}).get("pools") or []:
            try:
                opened, closed = (
                    datetime.strptime(pool[key], "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN)
                    for key in ("open", "close")
                )
                windows[pool["id"]] = (int(opened.timestamp()), int(closed.timestamp()))
            except (KeyError, ValueError, TypeError):
                continue
        return windows

    async def _build(self, session, character: Character, result: dict) -> tuple[Plan, list[dict]]:
        official = [
            Official(pool_id, name, rarity, int(ts), pos, bool(is_free))
            for pool_id, name, rarity, ts, pos, is_free in await session.execute(
                select(
                    GachaRecord.pool_id,
                    GachaRecord.char_name,
                    GachaRecord.rarity,
                    GachaRecord.gacha_ts,
                    GachaRecord.pos,
                    GachaRecord.is_free,
                ).where(
                    GachaRecord.char_pk_id == character.id,
                    GachaRecord.char_uid == character.uid,
                    GachaRecord.app_code == "endfield",
                    GachaRecord.pos >= 0,
                )
            )
        ]
        plan = build_plan(result, str(character.role_id), official, self._pool_windows())
        if plan.error or not plan.rows:
            return plan, []
        id_by_name = {
            (item_type, name): item_id
            for item_type, name, item_id in await session.execute(
                select(GachaRecord.item_type, GachaRecord.char_name, GachaRecord.char_id)
                .where(GachaRecord.app_code == "endfield", GachaRecord.pos >= 0)
                .group_by(GachaRecord.item_type, GachaRecord.char_name, GachaRecord.char_id)
            )
            if item_id
        }
        pool_names = dict(
            (await session.execute(
                select(GachaRecord.pool_id, func.max(GachaRecord.pool_name))
                .where(GachaRecord.app_code == "endfield", GachaRecord.pos >= 0)
                .group_by(GachaRecord.pool_id)
            )).all()
        )
        wiki = self.plugin.wiki.resolver()
        index = wiki[0] if wiki else {}
        for item_type, section in (("char", "operators"), ("weapon", "weapons")):
            for item_id, item in (index.get(section) or {}).items():
                name = item.get("name")
                if name and not item_id.startswith("chr_9000"):
                    id_by_name.setdefault((item_type, name), item_id)
        rows = []
        for row in plan.rows:
            content = ef_gacha_pool_data.get_pool(row["pool_id"])
            rows.append({
                "uid": character.id,
                "char_pk_id": character.id,
                "char_uid": character.uid,
                "app_code": "endfield",
                "item_type": row["item_type"],
                "pool_id": row["pool_id"],
                "pool_name": pool_names.get(row["pool_id"]) or (content.pool_name if content else "") or row["pool_name"],
                "char_id": id_by_name.get((row["item_type"], row["name"]), ""),
                "char_name": row["name"],
                "rarity": row["rarity"],
                "is_new": False,
                "is_free": row["is_free"],
                "gacha_ts": row["ts"],
                "pos": row["pos"],
            })
        return plan, rows

    def _snapshot_path(self, user_id: int, role_id: str) -> Path:
        return self.data_dir / f"{user_id}-{role_id}.json"

    def _save_snapshot(self, user_id: int, role_id: str, heybox_id: str, result: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._snapshot_path(user_id, role_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"heybox_id": heybox_id, "fetched": time.time(), "result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    async def import_history(self, event: AstrMessageEvent, heybox_id: str) -> str:
        if not heybox_id.isdecimal() or len(heybox_id) > 24:
            return "小黑盒 ID 必须是个人主页里的数字 ID，不是游戏 UID。用法：/zmd导入小黑盒 小黑盒ID"
        qq = event.get_sender_id()
        if self._lock.locked():
            return "当前有其他人在导入，请稍后再试。"
        remaining = COOLDOWN - (time.time() - self._last_run.get(qq, 0))
        if remaining > 0:
            return f"刚导入过，请 {int(remaining // 60) + 1} 分钟后再试。"
        async with self._lock:
            async with await get_session() as session:
                user = await get_user_by_platform(session, qq)
                if user is None:
                    return "请先发送 /skl绑定 绑定森空岛账号。"
                characters = [char for char in await get_ef_characters(session, user) if char.role_id]
                if not characters:
                    return "账号下没有终末地角色，请先发送 /skl角色更新。"
            try:
                result = await self._fetch(heybox_id)
            except Exception as error:
                return f"读取小黑盒数据失败（{type(error).__name__}），请稍后再试。"
            role_id = str((result.get("user_info") or {}).get("uid") or "")
            character = next((char for char in characters if str(char.role_id) == role_id), None)
            if character is None:
                return "这个小黑盒账号绑定的终末地 UID 与你绑定的角色不一致，不能导入别人的记录。"
            async with await get_session() as session:
                current = await session.get(Character, (character.id, character.uid))
                if current is None:
                    return "终末地角色已变化，请重新同步角色后再试。"
                plan, rows = await self._build(session, current, result)
                if plan.error:
                    return plan.error
                if not rows:
                    return "没有发现需要补回的抽卡记录。"
                await session.execute(delete(GachaRecord).where(
                    GachaRecord.char_pk_id == current.id,
                    GachaRecord.char_uid == current.uid,
                    GachaRecord.app_code == "endfield",
                    GachaRecord.pos < 0,
                ))
                await asyncio.to_thread(self._save_snapshot, current.id, str(current.role_id), heybox_id, result)
                session.add_all(GachaRecord(**row) for row in rows)
                await session.commit()
            self._last_run[qq] = time.time()
            imported = len(plan.rows)
            return (
                f"小黑盒导入完成：补回 {imported} 条抽卡记录。\n"
                f"官方记录 {plan.official_pulls} 抽，小黑盒记录 {plan.heybox_pulls} 抽。\n"
                "官方记录未修改；发送 /zmd撤销小黑盒导入 可删除导入数据。"
            )

    async def undo(self, event: AstrMessageEvent) -> str:
        qq = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, qq)
            if user is None:
                return "请先发送 /skl绑定 绑定森空岛账号。"
            characters = [char for char in await get_ef_characters(session, user) if char.role_id]
            if not characters:
                return "账号下没有终末地角色。"
            removed = 0
            for char in characters:
                result = await session.execute(delete(GachaRecord).where(
                    GachaRecord.char_pk_id == char.id,
                    GachaRecord.char_uid == char.uid,
                    GachaRecord.app_code == "endfield",
                    GachaRecord.pos < 0,
                ))
                removed += max(result.rowcount or 0, 0)
                self._snapshot_path(char.id, str(char.role_id)).unlink(missing_ok=True)
            await session.commit()
        self._last_run.pop(qq, None)
        return f"已撤销小黑盒导入，删除 {removed} 条导入记录；官方记录不受影响。" if removed else "你没有导入过小黑盒抽卡记录。"

    async def reconcile(self, character: Character) -> None:
        path = self._snapshot_path(character.id, str(character.role_id))
        try:
            snapshot = json.loads(path.read_text("utf-8"))
            source = snapshot["result"]
        except (OSError, KeyError, json.JSONDecodeError):
            return
        async with await get_session() as session:
            current = await session.get(Character, (character.id, character.uid))
            if current is None or current.app_code != "endfield":
                return
            plan, rows = await self._build(session, current, source)
            if plan.error:
                return
            await session.execute(delete(GachaRecord).where(
                GachaRecord.char_pk_id == current.id,
                GachaRecord.char_uid == current.uid,
                GachaRecord.app_code == "endfield",
                GachaRecord.pos < 0,
            ))
            if rows:
                session.add_all(GachaRecord(**row) for row in rows)
            await session.commit()
