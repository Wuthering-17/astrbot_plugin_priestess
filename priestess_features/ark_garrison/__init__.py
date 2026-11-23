"""AstrBot integration for Stronghold Protocol history and trophy standings."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from sqlalchemy import select

from ...api import SklandAPI
from ...config import CACHE_DIR
from ...db import Character, SkUser, get_ark_characters, get_session, get_user_by_platform
from ...schemas import CRED
from ...schemas.arknights.garrison import GarrisonResources, GarrisonView
from ...utils import call_api_with_refresh
from .. import ef_theme
from ..ark_roster import _avatar, _fill_avatars
from . import render
from .stats import Standing, ranked

RESOURCE_TTL = 86400
REFRESH_COOLDOWN = 300
WORKERS = 5
MENU_RECORDS = 16
USAGE = "用法：/卫 [@群友] [全部｜收藏｜详情｜第2页｜第3局] [-r 序号]"


def parse_choice(argument: str) -> tuple[bool, bool, bool, int | None, int | None]:
    argument = re.sub(r"(?:^|\s)-r\s+\d+(?=\s|$)", " ", argument)
    argument = re.sub(r"详情\s+(?=\d)", "详情", argument)
    words = argument.split()
    favored = "收藏" in words
    menu = "详情" in words
    show_all = "全部" in words
    record, page = None, None
    for word in words:
        if word in {"全部", "收藏", "详情"}:
            continue
        if match := re.fullmatch(r"第(\d+)页", word):
            if page is None:
                page = int(match[1])
                continue
        if match := re.fullmatch(r"(?:详情|第)?(\d+)局?", word):
            if record is None:
                record = int(match[1])
                continue
        raise ValueError(USAGE)
    if (record is not None and (show_all or page is not None)) or (menu and show_all):
        raise ValueError(USAGE)
    return favored, show_all, menu, record, page


async def _picture(html: str, width: int) -> str:
    image = await ef_theme.render_page(html, width, height=1, scale=render.SCALE, quality=render.QUALITY)
    path = CACHE_DIR / f"ark_garrison_{time.time_ns()}.webp"
    path.write_bytes(image)
    return str(path)


class ArkGarrison:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "ark_garrison.json"
        self._resource_lock = asyncio.Lock()
        self._resource: tuple[float, GarrisonResources] | None = None
        self._board_lock = asyncio.Lock()
        self._refresh_until: dict[str, float] = {}

    async def resources_for(self, user: SkUser) -> GarrisonResources:
        async with self._resource_lock:
            now = time.monotonic()
            if self._resource and now - self._resource[0] < RESOURCE_TTL:
                return self._resource[1]
            try:
                resources = await call_api_with_refresh(user, SklandAPI.ark_garrison_resources, self._cred(user))
                resources.current_resource()
            except Exception:
                if self._resource:
                    logger.warning("[Priestess] Garrison resource refresh failed; using cached resources")
                    return self._resource[1]
                raise
            self._resource = (now, resources)
            return resources

    @staticmethod
    def _cred(user: SkUser) -> CRED:
        return CRED(cred=user.cred, token=user.cred_token)

    async def _data(self, user: SkUser, char: Character, resources: GarrisonResources):
        return await call_api_with_refresh(user, SklandAPI.ark_garrison, self._cred(user), char.uid, resources.current_activity_id)

    async def view(self, user: SkUser, char: Character, favored: bool, record: int | None) -> GarrisonView:
        resources = await self.resources_for(user)
        data = await self._data(user, char, resources)
        view = GarrisonView.from_data(
            data, resources, uid=char.uid, server_name=f"渠道 {char.channel_master_id}",
            channel_master_id=char.channel_master_id, favored=favored, record_index=record,
        )
        if not data.gameUserInfo.name:
            view.nickname = char.nickname
        return view

    async def history(self, view: GarrisonView, show_all: bool, menu: bool, page: int | None) -> tuple[list[str], str]:
        if menu and not view.is_detail:
            if not view.records:
                return [], "暂无对局。"
            number = 1 if page is None else page
            total = (len(view.records) + MENU_RECORDS - 1) // MENU_RECORDS
            if not 1 <= number <= total:
                return [], f"页码超出范围，共 {total} 页。"
            records = view.records[(number - 1) * MENU_RECORDS:number * MENU_RECORDS]
            options = [f"{record.index}. {record.difficulty_label} · {record.result_label}" for record in records]
            command = "/卫 收藏" if view.favored else "/卫"
            return [], "\n".join([f"第 {number}/{total} 页 · {command} 详情 第N页 翻页", *options, f"发送 {command} 编号 查看单局。"])
        total = max(1, (len(view.records) + render.PAGE_RECORDS - 1) // render.PAGE_RECORDS)
        if page is not None and not 1 <= page <= total:
            return [], f"页码超出范围，共 {total} 页。"
        if (show_all or page is not None) and not view.records:
            return [], "暂无对局。"
        if view.is_detail:
            jobs = [(0, view.records)]
        elif page is not None or show_all:
            numbers = [page] if page is not None else range(1, total + 1)
            jobs = [(n, view.records[(n - 1) * render.PAGE_RECORDS:n * render.PAGE_RECORDS]) for n in numbers]
        else:
            jobs = [(0, view.records[:render.SUMMARY_RECORDS])]
        ids = [operator.char_id for _, records in jobs for record in records for operator in record.operators]
        await _fill_avatars(ids)
        avatars = {char_id: _avatar(char_id) for char_id in set(ids)}
        images = []
        for number, records in jobs:
            if view.is_detail:
                html = render.detail(view, avatars)
            elif number:
                html = render.records_page(view, records, avatars, page=number, page_total=total)
            else:
                html = render.summary(view, avatars, page_total=total)
            images.append(await _picture(html, render.WIDTH))
        return images, ""

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {"activity": "", "season": "", "counted_at": 0, "daily_at": 0, "rows": {}, "optout": []}

    def _save(self, state: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def optout(self, qq: str, enabled: bool) -> str:
        state = self._load()
        excluded = set(state["optout"])
        if enabled:
            excluded.add(qq)
            state["rows"].pop(qq, None)
            self._refresh_until.pop(qq, None)
        else:
            excluded.discard(qq)
        state["optout"] = sorted(excluded)
        self._save(state)
        return "已退出卫戍榜。" if enabled else "已加入卫戍榜，可发送 /卫戍榜 更新 提交战绩。"

    async def _bindings(self) -> dict[str, set[str]]:
        async with await get_session() as session:
            result = await session.execute(
                select(SkUser.platform_user_id, Character.uid).join(Character, Character.id == SkUser.id)
                .where(Character.app_code == "arknights")
            )
            bindings: dict[str, set[str]] = {}
            for qq, uid in result:
                bindings.setdefault(qq, set()).add(uid)
            return bindings

    async def _best(self, qq: str) -> tuple[GarrisonResources | None, Standing | None, bool]:
        async with await get_session() as session:
            user = await get_user_by_platform(session, qq)
            if user is None:
                return None, None, False
            characters = await get_ark_characters(session, user)
            if not characters:
                return None, None, False
            resources = None
            rows = []
            succeeded = False
            try:
                resources = await self.resources_for(user)
                bands = resources.current_resource().bandInfo
                for char in characters:
                    try:
                        data = await self._data(user, char, resources)
                    except Exception as error:
                        logger.debug("[Priestess] Garrison role read failed: %s", type(error).__name__)
                        continue
                    succeeded = True
                    career = data.career
                    total = (career.totalSingleBattleCnt or 0) + (career.totalMultiBattleCnt or 0)
                    wins = (career.totalSingleBattleWinCnt or 0) + (career.totalMultiBattleWinCnt or 0)
                    if career.totalSingleBattleCnt is None and career.totalMultiBattleCnt is None and data.history:
                        records = {record.id: record for record in data.history.records if record.activityId == resources.current_activity_id}
                        total = len(records)
                        wins = sum(record.completeState == 1 for record in records.values())
                    if total:
                        band = bands.get(career.top1BandUseId)
                        rows.append(Standing(qq, char.uid, data.gameUserInfo.name or char.nickname, career.totalTrophyCnt or 0, total, wins, band.name if band else ""))
            except Exception as error:
                logger.debug("[Priestess] Garrison standings read failed: %s", type(error).__name__)
            await session.commit()
            return resources, ranked(rows)[0] if rows else None, succeeded

    def _merge(self, results: dict[str, tuple[GarrisonResources | None, Standing | None, bool]], bindings: dict[str, set[str]], *, daily: bool = False) -> int:
        state = self._load()
        resources = next((resources for resources, _, ok in results.values() if resources and ok), None)
        if resources:
            version = resources.current_resource().versionInfo
            if state["activity"] != version.activityId or state["season"] != version.seasonName:
                state.update(activity=version.activityId, season=version.seasonName, rows={})
        excluded = set(state["optout"])
        rows = {qq: row for qq, row in state["rows"].items() if qq not in excluded and row["uid"] in bindings.get(qq, ())}
        for qq, (source, row, ok) in results.items():
            if not ok or qq in excluded or not source:
                continue
            season = source.current_resource().versionInfo
            if (season.activityId, season.seasonName) != (state["activity"], state["season"]):
                continue
            if row and row.uid in bindings.get(qq, ()):
                rows[qq] = asdict(row)
            else:
                rows.pop(qq, None)
        state["rows"] = rows
        if resources:
            state["counted_at"] = time.time()
        if daily and (resources or not results):
            state["daily_at"] = time.time()
        self._save(state)
        return len(rows)

    async def collect(self) -> str:
        if self._board_lock.locked():
            return "卫戍榜正在刷新，请稍后再试。"
        async with self._board_lock:
            bindings = await self._bindings()
            excluded = set(self._load()["optout"])
            candidates = list(bindings.keys() - excluded)
            queue = iter(candidates)
            results = {}

            async def worker() -> None:
                for qq in queue:
                    try:
                        if qq in self._load()["optout"]:
                            continue
                        results[qq] = await self._best(qq)
                    except Exception as error:
                        logger.warning("[Priestess] Garrison standings refresh failed for one member: %s", type(error).__name__)
                        results[qq] = (None, None, False)
                    await asyncio.sleep(1)

            async with asyncio.TaskGroup() as group:
                for _ in range(min(WORKERS, len(candidates))):
                    group.create_task(worker())
            rows = self._merge(results, await self._bindings(), daily=True)
            succeeded = sum(ok for _, _, ok in results.values())
            return f"卫戍榜已处理 {len(candidates)} 位成员，成功读取 {succeeded} 位，当前 {rows} 位上榜。"

    async def refresh(self, qq: str) -> str:
        if qq in self._load()["optout"]:
            return "你已退出卫戍榜。发送 /卫戍榜 加入 后可更新。"
        now = time.monotonic()
        self._refresh_until = {user: until for user, until in self._refresh_until.items() if until > now}
        if qq in self._refresh_until:
            return f"请 {int(self._refresh_until[qq] - now) + 1} 秒后再更新。"
        if self._board_lock.locked():
            return "卫戍榜正在刷新，请稍后再试。"
        async with self._board_lock:
            bindings = await self._bindings()
            if qq not in bindings:
                return "请先绑定有明日方舟角色的森空岛账号。"
            resources, row, ok = await self._best(qq)
            if qq in self._load()["optout"]:
                return "你已退出卫戍榜。"
            self._merge({qq: (resources, row, ok)}, await self._bindings())
            self._refresh_until[qq] = time.monotonic() + (REFRESH_COOLDOWN if ok else 60)
            if not ok:
                return "读取卫戍协议战绩失败，已保留此前的榜单记录。请 1 分钟后重试。"
            return f"卫戍榜已更新，当前 {row.trophies} 个奖杯。" if row else "当前赛季暂无可上榜的卫戍协议战绩。"

    async def board(self, event) -> str:
        bindings = await self._bindings()
        state = self._load()
        excluded = set(state["optout"])
        rows = ranked([Standing(**row) for qq, row in state["rows"].items() if qq not in excluded and row["uid"] in bindings.get(qq, ())])
        names = {}
        if event.get_group_id() and event.get_platform_name() == "aiocqhttp":
            try:
                members = await event.bot.call_action("get_group_member_list", group_id=int(event.get_group_id()))
                names = {str(member["user_id"]): (str(member.get("card") or member.get("nickname") or ""), True) for member in members}
            except Exception as error:
                logger.debug("[Priestess] Garrison board could not read group names: %s", type(error).__name__)
        season = "卫戍协议·" + state["season"] if state["season"] else "卫戍协议"
        return await _picture(render.board(rows, names, str(event.get_sender_id()), state["counted_at"], season=season), render.BOARD_WIDTH)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
                return
            except TimeoutError:
                pass
            now = datetime.now(render.CN)
            due = now.replace(hour=10, minute=30, second=0, microsecond=0)
            try:
                if now >= due and self._load()["daily_at"] < due.timestamp():
                    logger.info("[Priestess] %s", await self.collect())
                    await asyncio.wait_for(stop.wait(), timeout=900)
            except TimeoutError:
                pass
            except Exception:
                logger.exception("[Priestess] Scheduled garrison board refresh failed")
