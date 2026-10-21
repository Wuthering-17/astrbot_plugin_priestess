"""AstrBot 插件入口：森空岛（Skland）

指令前缀改为 /sk，新增纯文本看板功能
"""

import asyncio
import json
import math
import re
from pathlib import Path
from datetime import datetime, timedelta

import qrcode
from sqlalchemy import select
import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger

from .db import (
    init_db,
    get_session,
    SkUser,
    Character,
    GachaRecord,
    StaminaAlert,
    get_user_by_platform,
    get_ark_characters,
    get_ef_characters,
    get_all_users_with_umos,
    get_or_create_stamina_alert,
    get_gacha_records,
    delete_gacha_records,
)
from .api import SklandAPI, SklandLoginAPI
from .data_source import ef_gacha_pool_data, gacha_table_data
from .schemas import (
    CRED,
    Topics,
    RogueData,
    Clue,
    GachaInfo,
    GachaCate,
    EfGachaInfo,
    GachaPool,
    GachaPull,
    GachaGroup,
    EfGachaPoolInfo,
    EfGachaGroup,
    EfGachaPull,
    GroupedGachaRecord,
    EfGroupedGachaRecord,
    EndfieldPoolType,
    EndfieldCharPoolType,
    EndfieldWeaponPoolType,
)
from .render_adapter import (
    render_ark_card,
    render_rogue_card,
    render_rogue_info,
    render_clue_board,
    render_gacha_history,
    render_ef_gacha_history,
)
from .utils import call_api_with_refresh
from .config import DATA_DIR, RES_DIR
from .skland_cos import fetch_cos_images, _download_image

# 公招模块
from .recruit_data import RECRUIT_TAG_COUNT
from .recruit_calc import normalize_tags, calculate_combinations, format_recommendation
from .recruit_vision import recognize_tags_from_image
from .priestess_features.wiki import EndfieldWiki
from .priestess_features.roster import render_ef_roster
from .priestess_features.guide import CN, EndfieldGuide
from .priestess_features.gacha_rank import GachaRank
from .priestess_features.ark_gacha_rank import ArkGachaRank
from .priestess_features.pool_compare import PoolCompare
from .priestess_features.heybox import HeyboxImporter
from .priestess_features.ark_heybox import ArkHeyboxImporter
from .priestess_features.sanity import SanityReminder
from .priestess_features.weekly_report import WeeklyReport
from .priestess_features.health import AccountHealth
from .priestess_features.game_calendar.calendar import AstrBotCalendar, ARKNIGHTS_WORDS, COMMAND as CALENDAR_COMMAND
from .priestess_features.skland_pools import RERUN


# ==================== 抽卡记录辅助函数 ====================


def _get_up_chars(pool_id: str) -> tuple[list[str], list[str]]:
    """获取卡池 UP 五星和六星角色列表"""
    up_five_chars, up_six_chars = [], []
    for gacha_detail in gacha_table_data.gacha_details:
        if gacha_detail.gachaPoolId != pool_id:
            continue
        up_char = gacha_detail.gachaPoolDetail.detailInfo.upCharInfo
        avail_char = gacha_detail.gachaPoolDetail.detailInfo.availCharInfo
        if up_char and hasattr(up_char, "perCharList") and up_char.perCharList:
            for up_char_item in up_char.perCharList:
                if up_char_item.rarityRank == 4:
                    up_five_chars = up_char_item.charIdList
                elif up_char_item.rarityRank == 5:
                    up_six_chars = up_char_item.charIdList
        elif avail_char and hasattr(avail_char, "perAvailList") and avail_char.perAvailList:
            for avail_char_item in avail_char.perAvailList:
                if avail_char_item.rarityRank == 4:
                    up_five_chars = avail_char_item.charIdList
                elif avail_char_item.rarityRank == 5:
                    up_six_chars = avail_char_item.charIdList
    return up_five_chars, up_six_chars


def _get_pool_info(pool_id: str) -> tuple[int, int, int]:
    """获取卡池开放时间、结束时间和规则类型"""
    for gacha_table in gacha_table_data.gacha_table:
        if gacha_table.gachaPoolId == pool_id:
            return gacha_table.openTime, gacha_table.endTime, gacha_table.gachaRuleType
    return 0, 0, 0


def _infer_pool_category(pool_id: str) -> str:
    """根据 pool_id 推导终末地卡池类别"""
    pid = pool_id.lower()
    if pid.startswith("special"):
        return "special"
    if pid.startswith("wepon") or pid.startswith("weapon"):
        return "weapon"
    if pid == "beginner":
        return "beginner"
    return "standard"


def group_gacha_records(records: list[GachaRecord]) -> GroupedGachaRecord:
    """将明日方舟抽卡记录按卡池分组"""
    from collections import defaultdict

    temp_grouped_records = defaultdict(lambda: defaultdict(list))
    for record in records:
        temp_grouped_records[record.pool_id][record.gacha_ts].append(record)

    final_pools_data: list[GachaPool] = []
    for pool_id, ts_dict in temp_grouped_records.items():
        up_five_chars, up_six_chars = _get_up_chars(pool_id)
        open_time, end_time, gacha_rule_type = _get_pool_info(pool_id)
        gacha_groups: list[GachaGroup] = [
            GachaGroup(
                gacha_ts=gacha_ts,
                pulls=[
                    GachaPull(
                        pool_name=p.pool_name,
                        char_id=p.char_id,
                        char_name=p.char_name,
                        rarity=p.rarity,
                        is_new=p.is_new,
                        pos=p.pos,
                    )
                    for p in pulls
                ],
            )
            for gacha_ts, pulls in ts_dict.items()
        ]
        # 取第一个非空记录作为卡池名称
        first_pull = next(
            (pull for group in gacha_groups for pull in group.pulls),
            None,
        )
        pool_name = first_pull.pool_name if first_pull else "未知寻访"
        gacha_pool = GachaPool(
            gachaPoolId=pool_id,
            gachaPoolName=pool_name,
            openTime=open_time,
            endTime=end_time,
            up_five_chars=up_five_chars,
            up_six_chars=up_six_chars,
            gachaRuleType=gacha_rule_type,
            records=gacha_groups,
        )
        final_pools_data.append(gacha_pool)

    return GroupedGachaRecord(pools=final_pools_data)


def group_ef_gacha_records(records: list[GachaRecord]) -> EfGroupedGachaRecord:
    """将终末地抽卡记录按卡池分组"""
    from collections import defaultdict

    temp_grouped_records = defaultdict(lambda: defaultdict(list))
    for record in records:
        temp_grouped_records[record.pool_id][record.gacha_ts].append(record)

    beginner_pools: list[EfGachaPoolInfo] = []
    standard_pools: list[EfGachaPoolInfo] = []
    special_pools: list[EfGachaPoolInfo] = []
    weapon_pools: list[EfGachaPoolInfo] = []

    for pool_id, ts_dict in temp_grouped_records.items():
        gacha_groups: list[EfGachaGroup] = [
            EfGachaGroup(
                gacha_ts=gacha_ts,
                pulls=[
                    EfGachaPull(
                        pool_name=p.pool_name,
                        item_id=p.char_id or ef_gacha_pool_data.resolve_item_id(p.item_type, p.char_name, p.pool_id),
                        item_name=p.char_name,
                        item_type=p.item_type,
                        rarity=p.rarity,
                        is_new=p.is_new,
                        is_free=p.is_free,
                        seq_id=p.pos,
                    )
                    for p in pulls
                ],
            )
            for gacha_ts, pulls in ts_dict.items()
        ]
        first_record = next(iter(next(iter(ts_dict.values()))))
        pool_type = first_record.item_type if first_record.item_type else "char"
        # 取第一个非空记录作为卡池名称
        first_pull = next(
            (pull for group in gacha_groups for pull in group.pulls),
            None,
        )
        pool_name = first_pull.pool_name if first_pull else "未知卡池"
        pool_content = ef_gacha_pool_data.get_pool(pool_id)
        pool_info = EfGachaPoolInfo(
            pool_id=pool_id,
            pool_name=pool_name,
            pool_type=pool_type,
            records=gacha_groups,
            up_six_chars=pool_content.up_six_char_ids if pool_content else [],
            up6_img=pool_content.up6_image if pool_content else "",
            up6_name=pool_content.up6_name if pool_content else "",
        )
        category = _infer_pool_category(pool_id)
        if category == "beginner":
            beginner_pools.append(pool_info)
        elif category == "special":
            special_pools.append(pool_info)
        elif category == "weapon":
            weapon_pools.append(pool_info)
        else:
            standard_pools.append(pool_info)

    return EfGroupedGachaRecord(
        beginner_pools=beginner_pools,
        standard_pools=standard_pools,
        special_pools=special_pools,
        weapon_pools=weapon_pools,
    )


async def get_all_gacha_records(char: Character, cate: GachaCate, access_token: str, role_token: str, ak_cookie: str):
    """异步生成器：获取指定分类下的所有明日方舟抽卡记录"""
    import httpx

    async with httpx.AsyncClient() as client:
        page = await SklandAPI.get_gacha_history(char.uid, role_token, access_token, ak_cookie, cate.id)
        prev_ts, prev_pos = None, None

        while page and page.gacha_list:
            for record in page.gacha_list:
                yield record
            if not page.hasMore:
                break
            if (page.next_ts, page.next_pos) == (prev_ts, prev_pos):
                break
            prev_ts, prev_pos = page.next_ts, page.next_pos
            page = await SklandAPI.get_gacha_history(
                char.uid,
                role_token,
                access_token,
                ak_cookie,
                cate.id,
                gachaTs=page.next_ts,
                pos=page.next_pos,
                client=client,
            )


async def get_all_ef_gacha_records(
    char: Character,
    pool_type: EndfieldPoolType,
    role_token: str,
    concurrency: int = 8,
):
    """获取指定卡池类型下的所有终末地抽卡记录

    自动处理分页，并发请求数据直到获取全部记录。
    """
    import itertools

    if concurrency <= 0:
        raise ValueError("concurrency must be greater than 0")

    server_id = char.channel_master_id
    first_page = await SklandAPI.get_ef_gacha_history(pool_type, server_id, role_token)
    if not first_page.gacha_list:
        return []
    if not first_page.hasMore:
        return first_page.gacha_list

    page_size = len(first_page.gacha_list)  # normally 5
    last_seq = first_page.gacha_list[-1].seq_id_int
    last_seq_lock = asyncio.Lock()

    async def fetch_page(client: httpx.AsyncClient) -> list[EfGachaInfo]:
        nonlocal last_seq
        records: list[EfGachaInfo] = []
        while True:
            async with last_seq_lock:
                seq_id, last_seq = last_seq, last_seq - page_size
            if seq_id <= 0:
                break
            seq_end = seq_id - page_size
            page = await SklandAPI.get_ef_gacha_history(pool_type, server_id, role_token, str(seq_id), client)
            gacha_infos = [i for i in page.gacha_list if seq_end <= i.seq_id_int < seq_id]
            records.extend(gacha_infos)
            if not page.hasMore:
                break
        return records

    import httpx
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(fetch_page(client) for _ in range(concurrency)))

    return sorted(itertools.chain(first_page.gacha_list, *results), key=lambda x: x.seq_id_int, reverse=True)


class SklandPlugin(Star):
    """森空岛插件：查询鹰角网络旗下游戏数据"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "skland.db"
        self._qrcode_tasks: dict[str, dict] = {}
        # 体力预警后台任务
        self._alert_task = None
        self._alert_stop_event = asyncio.Event()
        self.sanity = SanityReminder(self, self.data_dir)
        self.gacha_rank = GachaRank(self.data_dir)
        self.ark_gacha_rank = ArkGachaRank(self.data_dir)
        self.pool_compare = PoolCompare(self)
        self._sanity_task = None
        self._sanity_stop_event = asyncio.Event()
        self._auto_sign_task = None
        self._auto_sign_stop_event = asyncio.Event()
        self._group_sign_lock = asyncio.Lock()
        self.wiki = EndfieldWiki(self, self.data_dir)
        self._wiki_task = None
        self._wiki_stop_event = asyncio.Event()
        self.guide = EndfieldGuide(self, self.wiki, self.data_dir)
        self._guide_task = None
        self._guide_stop_event = asyncio.Event()
        self.calendar = AstrBotCalendar(self, self.wiki, self.data_dir)
        self.heybox = HeyboxImporter(self, self.data_dir)
        self.ark_heybox = ArkHeyboxImporter(self)
        self.weekly_report = WeeklyReport(self, self.data_dir)
        self._weekly_task = None
        self._weekly_stop_event = asyncio.Event()
        self.health = AccountHealth(self, self.data_dir)
        self._health_task = None
        self._health_stop_event = asyncio.Event()
        self._calendar_task = None
        self._calendar_stop_event = asyncio.Event()

    async def initialize(self):
        """AstrBot calls this for initial loading and individual plugin reloads."""
        await init_db(str(self.db_path))
        self._start_auto_sign_loop()

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        await init_db(str(self.db_path))
        logger.info("[Skland] 插件已加载，数据库就绪")
        try:
            repaired = await self.health.repair_defaults()
            if repaired:
                logger.info("[Priestess] Repaired %s missing default role selections", repaired)
        except Exception:
            logger.exception("[Priestess] Failed to repair default role selections")
        # 加载卡池数据（用于抽卡分析）
        try:
            downloaded = await gacha_table_data.load()
            if downloaded:
                logger.info("[Skland] 卡池数据已下载/更新")
            else:
                logger.info("[Skland] 卡池数据已是最新")
        except Exception as e:
            logger.warning(f"[Skland] 卡池数据加载失败: {e}")
        try:
            await ef_gacha_pool_data.load()
        except Exception as e:
            logger.warning("[Skland] 终末地卡池 UP 数据加载失败: %s", type(e).__name__)
        # 启动体力预警后台轮询
        self._start_stamina_alert_loop()
        self._start_sanity_reminder_loop()
        self._start_auto_sign_loop()
        self._start_wiki_sync_loop()
        self._start_guide_collection_loop()
        self._start_calendar_loop()
        self._start_weekly_report_loop()
        self._start_health_loop()

    @filter.event_message_type(filter.EventMessageType.OTHER_MESSAGE)
    async def on_onebot_group_lifecycle(self, event: AstrMessageEvent):
        """Handle OneBot invitation and bot membership events exposed by AstrBot."""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") == "request" and raw.get("request_type") == "group" and raw.get("sub_type") == "invite":
            platform = self.context.get_platform_inst(event.get_platform_id())
            client = platform.get_client() if platform and hasattr(platform, "get_client") else None
            if client is None:
                logger.warning("[Priestess] Cannot accept group invitation: OneBot client is unavailable")
                return
            try:
                await client.call_action(
                    "set_group_add_request",
                    flag=raw["flag"],
                    sub_type="invite",
                    approve=True,
                )
                logger.info("[Priestess] Accepted OneBot group invitation for %s", raw.get("group_id", "unknown"))
                event.stop_event()
            except Exception as error:
                logger.warning("[Priestess] Group invitation approval failed: %s", type(error).__name__)
        elif raw.get("post_type") == "notice" and raw.get("notice_type") in ("group_increase", "group_decrease"):
            if str(raw.get("user_id")) == str(raw.get("self_id")):
                logger.info(
                    "[Priestess] Bot group membership changed: %s (%s)",
                    raw.get("group_id", "unknown"),
                    raw.get("notice_type"),
                )

    async def terminate(self):
        """Stop plugin background loops during unload or reload."""
        self._alert_stop_event.set()
        self._sanity_stop_event.set()
        self._auto_sign_stop_event.set()
        self._wiki_stop_event.set()
        self._guide_stop_event.set()
        self._calendar_stop_event.set()
        self._weekly_stop_event.set()
        self._health_stop_event.set()
        tasks = [task for task in (self._alert_task, self._sanity_task, self._auto_sign_task, self._wiki_task, self._guide_task, self._calendar_task, self._weekly_task, self._health_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _start_wiki_sync_loop(self):
        if self._wiki_task and not self._wiki_task.done():
            return
        self._wiki_stop_event.clear()
        self._wiki_task = asyncio.create_task(self._wiki_sync_loop())

    async def _wiki_sync_loop(self):
        try:
            await asyncio.wait_for(self._wiki_stop_event.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass
        while not self._wiki_stop_event.is_set():
            try:
                result = await self.wiki.sync()
                logger.info("[Priestess] %s", result)
            except Exception:
                logger.exception("[Priestess] Endfield wiki periodic sync failed")
            try:
                await asyncio.wait_for(self._wiki_stop_event.wait(), timeout=self.wiki.sync_interval)
            except asyncio.TimeoutError:
                pass

    def _start_guide_collection_loop(self):
        if self._guide_task and not self._guide_task.done():
            return
        self._guide_stop_event.clear()
        self._guide_task = asyncio.create_task(self._guide_collection_loop())

    async def _guide_collection_loop(self):
        try:
            await asyncio.wait_for(self._guide_stop_event.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass
        while not self._guide_stop_event.is_set():
            try:
                logger.info("[Priestess] Starting Endfield clear and equipment collection")
                result = await self.guide.collect()
                logger.info("[Priestess] Endfield collection finished: %s", result)
            except Exception:
                logger.exception("[Priestess] Endfield collection failed")
            now = datetime.now(CN)
            runs = [now.replace(hour=5, minute=30, second=0, microsecond=0), now.replace(hour=17, minute=30, second=0, microsecond=0)]
            next_run = next((run for run in runs if run > now), runs[0] + timedelta(days=1))
            try:
                await asyncio.wait_for(self._guide_stop_event.wait(), timeout=max(1, (next_run - now).total_seconds()))
            except asyncio.TimeoutError:
                pass

    def _start_calendar_loop(self):
        if self._calendar_task and not self._calendar_task.done():
            return
        self._calendar_stop_event.clear()
        self._calendar_task = asyncio.create_task(self.calendar.run(self._calendar_stop_event))

    def _start_weekly_report_loop(self):
        if self._weekly_task and not self._weekly_task.done():
            return
        self._weekly_stop_event.clear()
        self._weekly_task = asyncio.create_task(self.weekly_report.run(self._weekly_stop_event))

    def _start_health_loop(self):
        if self._health_task and not self._health_task.done():
            return
        self._health_stop_event.clear()
        self._health_task = asyncio.create_task(self.health.run_weekly(self._health_stop_event))

    @staticmethod
    def _format_error(e: Exception) -> str:
        """格式化异常为友好的错误消息

        区分 Token 失效和其他错误，Token 失效时提示重新登录
        """
        from .exception import LoginException, UnauthorizedException

        msg = str(e)
        if isinstance(e, (LoginException, UnauthorizedException)):
            if "Token 已失效" in msg:
                return msg  # 已经包含提示
            return f"Token 已失效, 请使用 /sk login 重新扫码绑定.\n原始错误: {msg}"
        elif isinstance(e, Exception):
            return f"请求失败: {msg}"
        return f"未知错误: {msg}"

    @staticmethod
    def _sk(text: str) -> str:
        """统一添加插件前缀，便于外部插件识别来源"""
        return f"[Sklands]\n{text}"

    # ==================== 指令组 ====================

    @filter.command_group("sk")
    def sk(self):
        """森空岛主指令组"""
        pass

    @filter.command("资料库更新")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_wiki_sync(self, event: AstrMessageEvent):
        """管理员立即同步 AKEData 终末地资料库。"""
        try:
            message = await self.wiki.sync(force=True)
        except Exception as e:
            logger.exception("[Priestess] Endfield wiki sync failed")
            message = f"同步失败：{type(e).__name__}"
        yield event.plain_result(message)
        event.stop_event()

    @filter.command("skl帮助", alias={"skd帮助", "森空岛帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """发送 Priestess 的森空岛指令帮助图。"""
        help_image = RES_DIR / "help" / "help.webp"
        if help_image.is_file():
            yield event.image_result(str(help_image))
        else:
            yield event.plain_result("帮助图文件缺失，请联系管理员检查插件安装。")
        event.stop_event()

    async def _forward_command(self, event: AstrMessageEvent, handler):
        async for result in handler(event):
            yield result
        event.stop_event()

    @filter.command("skl绑定", alias={"skd绑定", "森空岛绑定", "skl扫码", "森空岛扫码"})
    async def cmd_login_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_login):
            yield result

    @filter.command("skl解绑", alias={"skd解绑", "森空岛解绑"})
    async def cmd_unbind_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_unbind):
            yield result

    @filter.command("skl角色更新", alias={"skd角色更新", "森空岛角色更新"})
    async def cmd_sync_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_sync):
            yield result

    @filter.command("skl角色", alias={"skd角色", "森空岛角色"})
    async def cmd_roles(self, event: AstrMessageEvent):
        async with await get_session() as session:
            user = await get_user_by_platform(session, event.get_sender_id())
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号，请先发送 /skl绑定。"))
                event.stop_event()
                return
            lines = []
            for label, getter in (
                ("明日方舟", get_ark_characters),
                ("终末地", get_ef_characters),
            ):
                chars = self._ordered_characters(await getter(session, user))
                if chars:
                    lines.append(f"{label}：")
                    lines.extend(
                        f"{index}. {char.nickname}{'（默认）' if char.isdefault else ''}"
                        for index, char in enumerate(chars, 1)
                    )
            yield event.plain_result(self._sk("\n".join(lines) or "没有找到已绑定的游戏角色。"))
        event.stop_event()

    @filter.command("skl切换方舟角色", alias={"skd切换方舟角色", "切换方舟角色"})
    async def cmd_switch_ark_role(self, event: AstrMessageEvent):
        async for result in self._switch_default_role(event, "arknights", "明日方舟"):
            yield result

    @filter.command("skl切换终末地角色", alias={"skd切换终末地角色", "切换终末地角色"})
    async def cmd_switch_ef_role(self, event: AstrMessageEvent):
        async for result in self._switch_default_role(event, "endfield", "终末地"):
            yield result

    @filter.command("理智")
    async def cmd_sanity(self, event: AstrMessageEvent):
        status = await self.sanity.status(event)
        if event.get_group_id():
            yield event.chain_result([Comp.At(qq=event.get_sender_id()), Comp.Plain(" " + status)])
        else:
            yield event.plain_result(status)
        event.stop_event()

    @filter.command("理智提醒")
    async def cmd_sanity_reminder(self, event: AstrMessageEvent):
        action = self._command_argument(event).strip()
        yield event.plain_result(await self.sanity.configure(event, action))
        event.stop_event()

    @filter.command("周报预览")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_weekly_report_preview(self, event: AstrMessageEvent):
        try:
            image_path = await self.weekly_report.preview(event)
            yield event.image_result(image_path)
        except Exception as error:
            logger.exception("[Priestess] Weekly report preview failed")
            yield event.plain_result(f"周报预览生成失败：{type(error).__name__}")
        event.stop_event()

    @filter.command("森空岛体检")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_account_health(self, event: AstrMessageEvent):
        """管理员检查失效绑定；只有显式带“清理”才解除绑定。"""
        action = self._command_argument(event).strip()
        if action not in ("", "清理"):
            yield event.plain_result("用法：/森空岛体检 [清理]（AstrBot 管理员）")
            event.stop_event()
            return
        if self.health._lock.locked():
            yield event.plain_result("账号体检正在进行中，请稍后再试。")
            event.stop_event()
            return
        yield event.plain_result("开始检查全部森空岛账号；网络或接口异常的账号会保留。")
        result = await self.health.run(apply=action == "清理")
        yield event.plain_result(self.health.summary(result))
        event.stop_event()

    @filter.command("zmd欧非榜", alias={"ef欧非榜", "终末地欧非榜", "欧非榜"})
    async def cmd_gacha_rank(self, event: AstrMessageEvent):
        action = self._command_argument(event).strip()
        async for result in self._send_gacha_rank(event, None, action):
            yield result

    @filter.command("zmd角色欧非榜", alias={"ef角色欧非榜", "终末地角色欧非榜", "角色欧非榜"})
    async def cmd_character_gacha_rank(self, event: AstrMessageEvent):
        async for result in self._send_gacha_rank(event, "角色"):
            yield result

    @filter.command("zmd武器欧非榜", alias={"ef武器欧非榜", "终末地武器欧非榜", "武器欧非榜"})
    async def cmd_weapon_gacha_rank(self, event: AstrMessageEvent):
        async for result in self._send_gacha_rank(event, "武器"):
            yield result

    @filter.command("方舟欧非榜", alias={"明日方舟欧非榜", "mrfz欧非榜"})
    async def cmd_ark_gacha_rank(self, event: AstrMessageEvent):
        argument = self._command_argument(event).strip()
        action = argument if argument in ("退出", "加入") else ""
        board = None if action else argument or None
        async for result in self._send_gacha_rank(event, board, action, self.ark_gacha_rank):
            yield result

    @filter.command("卡池对比", alias={"群友卡池", "群卡池对比"})
    async def cmd_pool_compare(self, event: AstrMessageEvent):
        async for result in self._send_pool_compare(event):
            yield result

    @filter.command("方舟卡池对比", alias={"明日方舟卡池对比"})
    async def cmd_ark_pool_compare(self, event: AstrMessageEvent):
        async for result in self._send_pool_compare(event, "arknights"):
            yield result

    @filter.command("终末地卡池对比", alias={"zmd卡池对比", "ef卡池对比"})
    async def cmd_ef_pool_compare(self, event: AstrMessageEvent):
        async for result in self._send_pool_compare(event, "endfield"):
            yield result

    async def _send_pool_compare(self, event: AstrMessageEvent, game: str | None = None):
        try:
            message, comparison = await self.pool_compare.prepare(event, self._command_argument(event), game)
            if comparison is None:
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"正在生成「{comparison.pool.name}」本群对比：{len(comparison.members)} 位群友，预计 {comparison.page_count} 张图片。")
                paths = []
                async for path in self.pool_compare.pictures(comparison):
                    paths.append(path)
                async for result in self._send_gacha_images(event, paths, f"卡池对比 · {comparison.pool.name}"):
                    yield result
        except Exception:
            logger.exception("[Priestess] 群友卡池对比生成失败")
            yield event.plain_result("卡池对比生成失败，请管理员查看日志后重试。")
        event.stop_event()

    async def _send_gacha_rank(self, event: AstrMessageEvent, board: str | None, action: str = "", service=None):
        service = service or self.gacha_rank
        try:
            page = await service.page(event, board, action)
            if page.kind == "html":
                yield event.image_result(await service.render(self, page))
            else:
                yield event.plain_result(page.value)
        except Exception:
            logger.exception("[Priestess] 欧非榜生成失败")
            yield event.plain_result("欧非榜图片生成失败，请管理员查看日志后重试。")
        event.stop_event()

    @filter.command("zmd导入小黑盒", alias={"终末地导入小黑盒", "ef导入小黑盒", "导入小黑盒"})
    async def cmd_heybox_import(self, event: AstrMessageEvent):
        yield event.plain_result(await self.heybox.import_history(event, self._command_argument(event).strip()))
        event.stop_event()

    @filter.command("zmd撤销小黑盒导入", alias={"终末地撤销小黑盒导入", "ef撤销小黑盒导入", "撤销小黑盒导入"})
    async def cmd_heybox_undo(self, event: AstrMessageEvent):
        yield event.plain_result(await self.heybox.undo(event))
        event.stop_event()

    @filter.command("方舟导入小黑盒", alias={"明日方舟导入小黑盒", "方舟导入抽卡记录", "导入抽卡记录"})
    async def cmd_ark_heybox_import(self, event: AstrMessageEvent):
        argument = self._command_argument(event)
        url = re.sub(r"\s+-r\s+\d+\s*$", "", argument).strip()
        yield event.plain_result(await self.ark_heybox.import_history(event, url))
        event.stop_event()

    @filter.command("森空岛签到", alias={"skl签到", "skd签到"})
    async def cmd_sign_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_sign):
            yield result

    @filter.command("明日方舟签到", alias={"方舟签到", "mrfz签到", "明日方舟签到详情", "方舟签到详情"})
    async def cmd_arksign_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_arksign):
            yield result

    @filter.command("终末地签到", alias={"zmd签到", "ef签到", "zmd签到详情", "终末地签到详情"})
    async def cmd_efsign_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_efsign):
            yield result

    @filter.command("明日方舟卡片", alias={"方舟卡片", "mrfz卡片", "方舟角色卡"})
    async def cmd_arkcard_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_arkcard):
            yield result

    @filter.command("终末地卡片", alias={"zmd卡片", "ef卡片", "zmd开盒", "终末地开盒", "zmd账号详情", "ef账号详情", "终末地账号详情", "账号详情"})
    async def cmd_efcard_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_efcard):
            yield result

    @filter.command("终末地抽卡记录", alias={"zmd抽卡记录", "ef抽卡记录", "zmd抽卡记录更新", "终末地抽卡记录更新"})
    async def cmd_efgacha_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_efgacha):
            yield result

    @filter.command("明日方舟抽卡记录", alias={"方舟抽卡记录", "mrfz抽卡记录"})
    async def cmd_gacha_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_gacha):
            yield result

    @filter.command("公招", alias={"公开招募", "公开招募计算"})
    async def cmd_recruit_alias(self, event: AstrMessageEvent):
        async for result in self._forward_command(event, self.cmd_recruit):
            yield result

    @filter.regex(r"^/?[^\s/]{1,30}$", priority=-100, block=False)
    async def cmd_endfield_wiki_lookup(self, event: AstrMessageEvent):
        """Resolve Priestess wiki names after regular plugin commands."""
        text = event.get_message_str().strip()
        if not text.startswith("/") and not event.is_at_or_wake_command:
            return
        query = text.removeprefix("/")
        if not self.wiki.index_file.exists():
            yield event.plain_result("终末地资料库尚未就绪，正在同步，首次同步可能需要几分钟。")
        try:
            await self.wiki.ensure_ready()
            kind, value = await self.wiki.render_result(query)
        except Exception as e:
            logger.exception("[Priestess] Endfield wiki card render failed")
            message = "资料图生成失败，请稍后再试。" if self.wiki.index_file.exists() else "终末地资料同步失败，请检查机器人访问 AKEData 的网络，或由管理员执行 /资料库更新 后重试。"
            yield event.plain_result(message)
            event.stop_event()
            return
        if kind == "miss":
            self.wiki.record_miss(query)
            return
        if kind == "text":
            yield event.plain_result(value)
        elif kind == "image":
            yield event.image_result(value)
        else:
            return
        event.stop_event()

    @staticmethod
    def _command_argument(event: AstrMessageEvent) -> str:
        parts = event.get_message_str().strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _ordered_characters(chars: list[Character]) -> list[Character]:
        return sorted(chars, key=lambda char: (not char.isdefault, char.nickname.casefold(), char.uid))

    async def _selected_character(self, session, user: SkUser, game: str, event: AstrMessageEvent) -> Character | None:
        getter = get_ark_characters if game == "arknights" else get_ef_characters
        chars = self._ordered_characters(await getter(session, user))
        match = re.search(r"(?:^|\s)-r\s+(\d+)\s*$", event.get_message_str().strip())
        if match:
            index = int(match.group(1))
            return chars[index - 1] if 1 <= index <= len(chars) else None
        return next((char for char in chars if char.isdefault), chars[0] if chars else None)

    async def _switch_default_role(self, event: AstrMessageEvent, game: str, label: str):
        argument = self._command_argument(event)
        if not argument.isdecimal() or int(argument) < 1:
            game_name = "方舟" if game == "arknights" else "终末地"
            yield event.plain_result(f"用法：/skl切换{game_name}角色 序号；可先发送 /skl角色 查看序号。")
            event.stop_event()
            return
        async with await get_session() as session:
            user = await get_user_by_platform(session, event.get_sender_id())
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号，请先发送 /skl绑定。"))
                event.stop_event()
                return
            getter = get_ark_characters if game == "arknights" else get_ef_characters
            chars = self._ordered_characters(await getter(session, user))
            index = int(argument)
            if index > len(chars):
                yield event.plain_result(f"{label}角色序号超出范围，请发送 /skl角色 查看当前列表。")
                event.stop_event()
                return
            selected = chars[index - 1]
            for char in chars:
                char.isdefault = char.uid == selected.uid
            await session.commit()
            yield event.plain_result(f"已将{label}默认角色切换为：{selected.nickname}（{index}）")
        event.stop_event()

    async def _send_guide_page(self, event: AstrMessageEvent, command: str, query: str = ""):
        if not self.wiki.index_file.exists():
            yield event.plain_result("终末地资料库尚未就绪，正在同步，首次同步可能需要几分钟。")
        try:
            await self.wiki.ensure_ready()
            kind, value, followup = await self.guide.page(command, query, event)
        except Exception:
            logger.exception("[Priestess] Endfield guide failed")
            message = "攻略资料图生成失败，请稍后再试。" if self.wiki.index_file.exists() else "终末地资料同步失败，请检查机器人访问 AKEData 的网络，或由管理员执行 /资料库更新 后重试。"
            yield event.plain_result(message)
            event.stop_event()
            return
        if kind == "image":
            yield event.image_result(value)
            if followup:
                yield event.plain_result(followup)
        elif value:
            yield event.plain_result(value)
        event.stop_event()

    @filter.command("攻略")
    async def cmd_guide(self, event: AstrMessageEvent):
        async for result in self._send_guide_page(event, "攻略", self._command_argument(event)):
            yield result

    @filter.command("战争回响")
    async def cmd_rotation(self, event: AstrMessageEvent):
        async for result in self._send_guide_page(event, "战争回响"):
            yield result

    @filter.command("影拓丰碑")
    async def cmd_monument(self, event: AstrMessageEvent):
        async for result in self._send_guide_page(event, "影拓丰碑", self._command_argument(event)):
            yield result

    @filter.command("敌人", alias={"敌人属性", "敌人数值", "回响敌人", "丰碑敌人"})
    async def cmd_enemies(self, event: AstrMessageEvent):
        query = self._command_argument(event)
        command = event.get_message_str().strip().split(maxsplit=1)[0].lstrip("/")
        if command == "回响敌人" and not query:
            query = "战争回响"
        elif command == "丰碑敌人" and not query:
            query = "影拓丰碑"
        async for result in self._send_guide_page(event, "敌人", query):
            yield result

    @filter.command("攻略统计")
    async def cmd_guide_optout(self, event: AstrMessageEvent):
        action = self._command_argument(event)
        if action not in ("退出", "加入"):
            yield event.plain_result("用法：/攻略统计 退出 或 /攻略统计 加入\n此设置控制绑定账号的高难通关阵容与配装统计。")
        else:
            yield event.plain_result(self.guide.set_optout(event.get_sender_id(), action == "退出"))
        event.stop_event()

    @filter.command("竞速榜")
    async def cmd_speed_board(self, event: AstrMessageEvent):
        async for result in self._send_guide_page(event, "竞速榜", self._command_argument(event)):
            yield result

    @filter.command("回响竞速", alias={"战争回响竞速"})
    async def cmd_echo_speed(self, event: AstrMessageEvent):
        async for result in self._send_guide_page(event, "回响竞速", self._command_argument(event)):
            yield result

    @filter.command("丰碑竞速", alias={"影拓丰碑竞速"})
    async def cmd_monument_speed(self, event: AstrMessageEvent):
        async for result in self._send_guide_page(event, "丰碑竞速", self._command_argument(event)):
            yield result

    @filter.command("榜单角色出场率", alias={"角色出场率"})
    async def cmd_usage_board(self, event: AstrMessageEvent):
        async for result in self._send_guide_page(event, "榜单角色出场率", self._command_argument(event)):
            yield result

    @filter.regex(CALENDAR_COMMAND.pattern, priority=5, block=True)
    async def cmd_calendar(self, event: AstrMessageEvent):
        match = CALENDAR_COMMAND.fullmatch(event.get_message_str().strip())
        game_key = "endfield" if match and match.group("endfield") else "arknights" if match and match.group("arknights") else None
        try:
            for image_path in await self.calendar.pictures(game_key):
                yield event.image_result(image_path)
        except Exception as error:
            logger.exception("[Priestess] Calendar rendering failed")
            yield event.plain_result(str(error) if isinstance(error, RuntimeError) else "活动日历生成失败，请稍后再试。")
        event.stop_event()

    @filter.command("活动通知预览")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_calendar_preview(self, event: AstrMessageEvent):
        query = self._command_argument(event).lower()
        arknights = any(word in query for word in ARKNIGHTS_WORDS)
        try:
            image_path = await self.calendar.preview(arknights)
            yield event.image_result(image_path)
        except Exception as error:
            yield event.plain_result(str(error))
        event.stop_event()

    @filter.command("攻略数据更新", alias={"skl攻略数据更新"}, priority=10)
    async def cmd_guide_collect(self, event: AstrMessageEvent):
        async for result in self._collect_guide_data(event):
            yield result

    @filter.command("配装数据更新", alias={"群友配装更新", "skl配装数据更新"}, priority=10)
    async def cmd_build_collect(self, event: AstrMessageEvent):
        async for result in self._collect_guide_data(event, builds_only=True):
            yield result

    @filter.command("配队数据更新", alias={"群友配队更新", "skl配队数据更新"}, priority=10)
    async def cmd_team_collect(self, event: AstrMessageEvent):
        async for result in self._collect_guide_data(event, teams_only=True):
            yield result

    @filter.command("配队", alias={"群友配队"}, priority=10)
    async def cmd_team_view(self, event: AstrMessageEvent):
        try:
            kind, content = await self.guide.team_page(self._command_argument(event), event)
            yield event.image_result(content) if kind == "image" else event.plain_result(content)
        except Exception:
            logger.exception("[Priestess] Team page failed")
            yield event.plain_result("配队查询失败，请检查 NapCat 群成员权限及日志后重试。")
        event.stop_event()

    async def _collect_guide_data(self, event: AstrMessageEvent, *, builds_only: bool = False, teams_only: bool = False):
        if not event.is_admin():
            yield event.plain_result("此命令需要 AstrBot 管理员权限。请在 AstrBot 的管理员 ID 列表中配置你的 QQ；QQ群管理员身份不会自动获得此权限。")
            event.stop_event()
            return
        if self.guide.echo_store.lock.locked():
            yield event.plain_result("当前正在采集攻略或配装数据，请等待本轮结束后再试。")
            event.stop_event()
            return
        label = "配队" if teams_only else "配装" if builds_only else "高难战绩及配装"
        logger.info("[Priestess] Manual %s collection command entered", label)
        yield event.plain_result(f"开始采集绑定用户默认终末地角色的{label}数据，请稍候。")
        try:
            result = await self.guide.collect(builds_only=builds_only, **({"teams_only": True} if teams_only else {}))
            if teams_only:
                yield event.plain_result(
                    f"配队采集完成：战争回响成功 {result.get('echoes', 0)} 个，失败 {result.get('echoes_failed', 0)} 个；"
                    f"影拓丰碑成功 {result.get('monument', 0)} 个，失败 {result.get('monument_failed', 0)} 个。\n"
                    "失败的接口保留上次记录。发送 /配队 角色名 查看本群通关阵容，1 人也可展示。"
                )
                event.stop_event()
                return
            errors = sum(value for key, value in result.items() if key.startswith("builds_") and key != "builds_empty")
            yield event.plain_result(
                f"采集完成：符合条件的账号 {result.get('eligible', 0)} 个；配装读取成功 {result.get('builds', 0)} 个，失败 {errors} 个；"
                f"获得 {result.get('build_operators', 0)} 份 70 级以上干员配装样本。\n"
                f"高难战绩成功 {result.get('ok', 0)} 个；其中 {result.get('builds_empty', 0)} 个账号没有符合条件的配装。\n"
                "发送 /配队 角色名 查看群友通关配队。重新发送 /角色名 查看群友配装；同一干员至少需 3 位用户满足 70 级及三件套条件。"
            )
        except Exception as error:
            logger.exception("[Priestess] Manual Endfield collection failed")
            yield event.plain_result(f"采集失败：{type(error).__name__}。已成功采集的数据会保留，请管理员查看日志。")
        event.stop_event()

    # ==================== 绑定相关 ====================

    @sk.command("login")
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码绑定森空岛账号（推荐） 用法: /sk login"""
        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin

        try:
            scan_id = await SklandLoginAPI.get_scan()
        except Exception as e:
            yield event.plain_result(self._sk(f"❌ 获取二维码失败: {e}"))
            return

        try:
            scan_url = f"hypergryph://scan_login?scanId={scan_id}"
            qr_img = qrcode.make(scan_url)
            qr_path = self.data_dir / f"qrcode_{sender_id}.png"
            qr_img.save(str(qr_path))
        except Exception as e:
            yield event.plain_result(self._sk(f"❌ 生成二维码失败: {e}"))
            return

        yield event.plain_result(
            self._sk(
                "请使用[森空岛APP]扫描下方二维码完成绑定\n"
                "二维码有效期约2分钟"
            )
        )

        # 尝试获取 QQ 平台的 message_id 以便扫码成功后自动撤回
        qrcode_msg_id = None
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if isinstance(event, AiocqhttpMessageEvent):
                import base64
                with open(qr_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                client = event.bot
                # 尝试用 OneBot API 发送图片并获取 message_id
                if event.get_group_id():
                    resp = await client.call_action(
                        "send_group_msg",
                        group_id=int(event.get_group_id()),
                        message=f"[CQ:image,file=base64://{img_b64}]"
                    )
                else:
                    resp = await client.call_action(
                        "send_private_msg",
                        user_id=int(event.get_sender_id()),
                        message=f"[CQ:image,file=base64://{img_b64}]"
                    )
                # 解析返回的 message_id
                if isinstance(resp, dict):
                    qrcode_msg_id = resp.get("data", {}).get("message_id") or resp.get("message_id")
                logger.debug(f"[Skland] 二维码消息 ID: {qrcode_msg_id}")
            else:
                yield event.image_result(str(qr_path))
        except Exception as e:
            logger.warning(f"[Skland] 尝试获取二维码消息 ID 失败, 将不会自动撤回: {e}")
            yield event.image_result(str(qr_path))

        self._qrcode_tasks[scan_id] = {
            "umo": umo,
            "sender_id": sender_id,
            "qrcode_msg_id": qrcode_msg_id,
            "event": event if 'AiocqhttpMessageEvent' in str(type(event)) else None,
        }
        asyncio.create_task(self._poll_scan_status(scan_id))

    async def _poll_scan_status(self, scan_id: str):
        task_info = self._qrcode_tasks.get(scan_id)
        if not task_info:
            return
        umo = task_info["umo"]
        sender_id = task_info["sender_id"]

        for i in range(40):
            await asyncio.sleep(3)
            try:
                scan_code = await SklandLoginAPI.get_scan_status(scan_id)
                if not scan_code:
                    continue

                token = await SklandLoginAPI.get_token_by_scan_code(scan_code)
                grant_code = await SklandLoginAPI.get_grant_code(token, 0)
                cred_data = await SklandLoginAPI.get_cred(grant_code)

                skland_user_id = None
                try:
                    skland_user_id = await SklandAPI.get_user_ID(cred_data)
                except Exception:
                    pass

                async with await get_session() as session:
                    existing = await get_user_by_platform(session, sender_id)
                    if existing:
                        existing.cred = cred_data.cred
                        existing.cred_token = cred_data.token
                        existing.access_token = token
                        existing.umo = umo
                        if skland_user_id:
                            existing.user_id = skland_user_id
                        user = existing
                    else:
                        user = SkUser(
                            platform_user_id=sender_id,
                            user_id=skland_user_id,
                            access_token=token,
                            cred=cred_data.cred,
                            cred_token=cred_data.token,
                            umo=umo,
                        )
                        session.add(user)
                        await session.flush()

                    try:
                        binding_list = await SklandAPI.get_binding(cred_data)
                        for app in binding_list:
                            for char in app.bindingList:
                                if char.roles:
                                    for role in char.roles:
                                        await session.merge(
                                            Character(
                                                id=user.id,
                                                uid=char.uid,
                                                role_id=role.roleId,
                                                nickname=role.nickname,
                                                app_code=app.appCode,
                                                channel_master_id=role.serverId,
                                                isdefault=len(char.roles) == 1 or role.isDefault,
                                            )
                                        )
                                else:
                                    await session.merge(
                                        Character(
                                            id=user.id,
                                            uid=char.uid,
                                            nickname=char.nickName,
                                            app_code=app.appCode,
                                            channel_master_id=char.channelMasterId,
                                            isdefault=len(app.bindingList) == 1 or char.isDefault,
                                        )
                                    )
                    except Exception as e:
                        logger.warning(f"[Skland] 自动拉取角色列表失败: {e}")

                    await session.commit()

                # 检查角色是否同步成功
                async with await get_session() as check_session:
                    user_check = await get_user_by_platform(check_session, sender_id)
                    ark_chars = await get_ark_characters(check_session, user_check) if user_check else []
                    ef_chars = await get_ef_characters(check_session, user_check) if user_check else []
                    has_chars = bool(ark_chars or ef_chars)

                if has_chars:
                    msg = self._sk("✅ 扫码绑定成功!")
                else:
                    msg = self._sk(
                        "✅ 扫码绑定成功!\n"
                        "⚠️ 但自动同步角色列表失败, 请手动执行 /sk sync 同步游戏角色"
                    )
                chain = MessageChain()
                chain.chain = [Comp.Plain(msg)]
                await self.context.send_message(umo, chain)

                # 自动撤回二维码消息（QQ 平台）
                qrcode_msg_id = task_info.get("qrcode_msg_id")
                event_obj = task_info.get("event")
                if qrcode_msg_id and event_obj:
                    try:
                        client = event_obj.bot
                        await client.call_action("delete_msg", message_id=int(qrcode_msg_id))
                        logger.debug(f"[Skland] 已自动撤回二维码消息: {qrcode_msg_id}")
                    except Exception as e:
                        logger.warning(f"[Skland] 自动撤回二维码消息失败: {e}")

                break

            except Exception as e:
                logger.debug(f"[Skland] 轮询中 ({i+1}/40): {e}")
                continue
        else:
            chain = MessageChain()
            chain.chain = [Comp.Plain(self._sk("❌ 二维码已过期, 请重新发送 /sk login"))]
            await self.context.send_message(umo, chain)

        self._qrcode_tasks.pop(scan_id, None)

    @sk.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent, cred: str, cred_token: str):
        """手动绑定  用法: /sk bind <cred> <cred_token>"""
        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin
        async with await get_session() as session:
            existing = await get_user_by_platform(session, sender_id)
            if existing:
                existing.cred = cred
                existing.cred_token = cred_token
                existing.umo = umo
                user = existing
            else:
                user = SkUser(platform_user_id=sender_id, cred=cred, cred_token=cred_token, umo=umo)
                session.add(user)
                await session.flush()

            try:
                cred_data = CRED(cred=cred, token=cred_token)
                skland_user_id = await SklandAPI.get_user_ID(cred_data)
                user.user_id = skland_user_id
            except Exception:
                pass

            await self._sync_binding_chars(session, user, CRED(cred=cred, token=cred_token))
            await session.commit()
            yield event.plain_result(self._sk("绑定成功!"))

    @sk.command("unbind")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解绑账号  用法: /sk unbind"""
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("你还没有绑定账号."))
                return
            await session.delete(user)
            await session.commit()
            yield event.plain_result(self._sk("已解绑."))

    @sk.command("sync")
    async def cmd_sync(self, event: AstrMessageEvent):
        """手动同步角色  用法: /sk sync"""
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                return
            cred = CRED(cred=user.cred, token=user.cred_token)
            try:
                await self._sync_binding_chars(session, user, cred)
                await session.commit()
                ark_chars = await get_ark_characters(session, user)
                ef_chars = await get_ef_characters(session, user)
                parts = ["✅ 角色列表同步成功!"]
                if ark_chars:
                    parts.append(f"[Arknights]: {len(ark_chars)}个")
                if ef_chars:
                    parts.append(f"[EndField]: {len(ef_chars)}个")
                if not ark_chars and not ef_chars:
                    parts.append("未找到任何角色")
                yield event.plain_result(self._sk("\n".join(parts)))
            except Exception as e:
                yield event.plain_result(self._sk(f"同步失败: {e}"))

    async def _sync_binding_chars(self, session, user: SkUser, cred_data: CRED):
        binding_list = await SklandAPI.get_binding(cred_data)
        for app in binding_list:
            for char in app.bindingList:
                if char.roles:
                    for role in char.roles:
                        await session.merge(
                            Character(
                                id=user.id,
                                uid=char.uid,
                                role_id=role.roleId,
                                nickname=role.nickname,
                                app_code=app.appCode,
                                channel_master_id=role.serverId,
                                isdefault=len(char.roles) == 1 or role.isDefault,
                            )
                        )
                else:
                    await session.merge(
                        Character(
                            id=user.id,
                            uid=char.uid,
                            nickname=char.nickName,
                            app_code=app.appCode,
                            channel_master_id=char.channelMasterId,
                            isdefault=len(app.bindingList) == 1 or char.isDefault,
                        )
                    )

    # ==================== 签到 ====================

    async def _do_ark_sign(self, event: AstrMessageEvent | None = None, *, sender_id: str | None = None) -> list[str] | None:
        """执行明日方舟签到；None 表示未绑定账号, [] 表示已绑定但无角色"""
        sender_id = sender_id if sender_id is not None else event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                return None
            chars = await get_ark_characters(session, user)
            if not chars:
                return []

            results = []
            for char in chars:
                cred = CRED(cred=user.cred, token=user.cred_token)
                try:
                    result = await call_api_with_refresh(
                        user, SklandAPI.ark_sign, cred, str(char.uid), char.channel_master_id
                    )
                    awards_text = "\n".join(
                        f"  {award.resource.name} x {award.count}" for award in result.awards
                    )
                    results.append(f"✅ {char.nickname} 签到成功, 获得:\n📦{awards_text}")
                except Exception as e:
                    error_msg = self._format_error(e)
                    if "请勿重复签到" in error_msg or "已签到" in error_msg:
                        results.append(f"ℹ️ {char.nickname} 已签到")
                    else:
                        results.append(f"❌ {char.nickname} 签到失败: {error_msg}")
                await session.commit()
            return results

    async def _do_ef_sign(self, event: AstrMessageEvent | None = None, *, sender_id: str | None = None) -> list[str] | None:
        """执行终末地签到；None 表示未绑定账号, [] 表示已绑定但无角色"""
        sender_id = sender_id if sender_id is not None else event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                return None
            chars = await get_ef_characters(session, user)
            if not chars:
                return []

            results = []
            for char in chars:
                cred = CRED(cred=user.cred, token=user.cred_token)
                try:
                    result = await call_api_with_refresh(
                        user, SklandAPI.endfield_sign, cred, char.role_id, char.channel_master_id
                    )
                    resource_info_map = result.resourceInfoMap or {}
                    award_ids = result.awardIds or []
                    award_lines = []
                    for award in award_ids:
                        info = resource_info_map.get(award.id)
                        if info:
                            name = info.name
                            count = info.count
                        else:
                            name = "未知物品"
                            count = 0
                        award_lines.append(f"  {name} x{count}")
                    awards_text = "\n".join(award_lines) if award_lines else "  (无奖励信息)"
                    results.append(f"✅ {char.nickname} 签到成功, 获得:\n📦{awards_text}")
                except Exception as e:
                    error_msg = self._format_error(e)
                    if "请勿重复签到" in error_msg or "已签到" in error_msg:
                        results.append(f"ℹ️ {char.nickname} 已签到")
                    else:
                        results.append(f"❌ {char.nickname} 签到失败: {error_msg}")
                await session.commit()
            return results

    @staticmethod
    def _group_sign_icon(results: list[str] | None) -> str:
        if not results:
            return "—"
        signed = [result.startswith(("✅", "ℹ️")) for result in results]
        if all(signed):
            return "✅"
        return "⚠️" if any(signed) else "❌"

    @filter.command("全群签到", alias={"群签到", "森空岛群签到"}, priority=10)
    async def cmd_group_sign(self, event: AstrMessageEvent):
        if not event.get_group_id() or event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("请在 NapCat / OneBot QQ 群内使用 /全群签到。")
            event.stop_event()
            return
        if not event.is_admin():
            yield event.plain_result("全群签到需要 AstrBot 管理员权限，请将操作者 QQ 加入 AstrBot 管理员 ID 列表。")
            event.stop_event()
            return
        if self._group_sign_lock.locked():
            yield event.plain_result("当前正在进行全群签到，请等待本轮汇总。")
            event.stop_event()
            return
        async with self._group_sign_lock:
            try:
                members = await event.bot.call_action("get_group_member_list", group_id=int(event.get_group_id()))
                names = {str(member["user_id"]): str(member.get("card") or member.get("nickname") or member["user_id"]) for member in members}
                async with await get_session() as session:
                    bound = set((await session.execute(select(SkUser.platform_user_id))).scalars().all())
                users = sorted(set(names) & {str(qq) for qq in bound})
                rows = []
                for qq in users:
                    icons = []
                    for game, sign in (("明日方舟", self._do_ark_sign), ("终末地", self._do_ef_sign)):
                        try:
                            results = await sign(sender_id=qq)
                            icons.append(self._group_sign_icon(results))
                        except Exception:
                            # Do not echo API exceptions or credentials into a group summary.
                            icons.append("⚠️")
                        await asyncio.sleep(1)
                    nickname = re.sub(r"[\r\n\t\x00-\x1f\x7f]", " ", names[qq]).replace("|", "｜")[:30]
                    rows.append(f"{icons[0]} | {icons[1]} | {nickname}")
                yield event.plain_result(self._group_sign_text(rows))
            except Exception:
                yield event.plain_result("全群签到未能完成，请检查 NapCat 群成员读取权限及插件数据库后重试。")
        event.stop_event()

    @staticmethod
    def _group_sign_text(rows: list[str]) -> str:
        return "\n".join(["森空岛签到统计", "═══════════════", "方舟 | 终末 | 昵称", "-----------------", *(rows or ["本群暂无已绑定成员。"]), "═══════════════", "✅ 已签到  ❌ 失败  ⚠️ 部分失败/账号异常  — 无绑定角色"])

    @sk.command("arksign")
    async def cmd_arksign(self, event: AstrMessageEvent):
        """明日方舟签到  用法: /sk arksign"""
        results = await self._do_ark_sign(event)
        if results is None:
            yield event.plain_result(self._sk("未绑定森空岛账号."))
            return
        if not results:
            yield event.plain_result(self._sk("未找到绑定的[Arknights]角色."))
            return
        yield event.plain_result(self._sk("\n\n".join(results)))

    @sk.command("efsign")
    async def cmd_efsign(self, event: AstrMessageEvent):
        """终末地签到  用法: /sk efsign"""
        results = await self._do_ef_sign(event)
        if results is None:
            yield event.plain_result(self._sk("未绑定森空岛账号."))
            return
        if not results:
            yield event.plain_result(self._sk("未找到绑定的[EndField]角色."))
            return
        yield event.plain_result(self._sk("\n\n".join(results)))

    @sk.command("sign")
    async def cmd_sign(self, event: AstrMessageEvent):
        """一键签到所有游戏  用法: /sk sign"""
        all_results: list[str] = []

        ark_results = await self._do_ark_sign(event)
        if ark_results is None:
            yield event.plain_result(self._sk("未绑定森空岛账号."))
            return
        if ark_results:
            all_results.append("[Arknights]")
            all_results.extend(ark_results)

        ef_results = await self._do_ef_sign(event)
        if ef_results is None:
            yield event.plain_result(self._sk("未绑定森空岛账号."))
            return
        if ef_results:
            if all_results:
                all_results.append("")  # 仅用一个空行分隔两个游戏区块
            all_results.append("[EndField]")
            all_results.extend(ef_results)

        if not all_results:
            yield event.plain_result(self._sk("未找到任何可签到的角色."))
            return

        # 使用单换行拼接，避免角色之间出现多余空行
        yield event.plain_result(self._sk("\n".join(all_results)))

    # ==================== COS 图片 ====================

    @sk.command("cos")
    async def cmd_cos(self, event: AstrMessageEvent, name: str = ""):
        """森空岛 COS 图片  用法: /sk cos [角色名]

        不带角色名时随机获取一张 cosplay 图片；
        带角色名时搜索该角色相关的 cosplay 图片。
        """
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                return

            try:
                images = await fetch_cos_images(user.cred, keyword=name)
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(f"获取失败: {e}"))
                return

            if not images:
                if name:
                    yield event.plain_result(
                        self._sk(f"未找到 '{name}' 相关的 COS 图片，该角色可能没有 COS 投稿。")
                    )
                else:
                    yield event.plain_result(
                        self._sk("未获取到 COS 图片。\n可能原因：\n1. 森空岛社区 API 暂时不可用\n2. COS 标签数据为空\n3. API 响应结构变更\n请查看 AstrBot 日志获取详细调试信息。")
                    )
                return

            # 只取第一张返回（fetch_cos_images 已按热度排序并从 Top-N 随机）
            entry = images[0]
            image_url = entry["url"]
            post_url = entry.get("post_url", "")
            author = entry.get("author", "")
            title = entry.get("title", "")
            like_count = entry.get("like_count", 0)
            view_count = entry.get("view_count", 0)

            # 构建底部信息行
            info_lines: list[str] = []
            if author:
                info_lines.append(f"作者: {author}")
            if title:
                info_lines.append(f"标题: {title}")
            if like_count or view_count:
                info_lines.append(f"👍 {like_count}  👁 {view_count}")
            if post_url:
                info_lines.append(f"来源: {post_url}")
            footer = "\n".join(info_lines) if info_lines else ""

            try:
                local_path = await _download_image(image_url)
                yield event.image_result(local_path)
                if footer:
                    yield event.plain_result(self._sk(footer))
            except Exception as e:
                logger.warning(f"[Skland] 下载 COS 图片失败: {e}, 尝试直接返回 URL")
                if footer:
                    yield event.plain_result(self._sk(f"图片获取失败\n{footer}"))
                else:
                    yield event.plain_result(self._sk(f"图片获取失败: {e}"))

    # ==================== 纯文本看板 ====================

    @sk.command("arkmr")
    async def cmd_arkmr(self, event: AstrMessageEvent):
        """明日方舟看板  用法: /sk arkmr"""
        sender_id = event.get_sender_id()
        await self._ensure_user_umo(event)
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                return
            char = await self._selected_character(session, user, "arknights", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[Arknights]角色."))
                return

            cred = CRED(cred=user.cred, token=user.cred_token)
            try:
                card_data = await call_api_with_refresh(
                    user, SklandAPI.ark_card, cred, str(char.uid)
                )
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                return

            lines = self._format_arkmr(card_data)
            yield event.plain_result(self._sk("\n".join(lines)))

    def _format_arkmr(self, card) -> list[str]:
        """格式化明日方舟看板"""
        s = card.status
        ap = s.ap
        now_ts = datetime.now().timestamp()

        # 理智
        ap_now = ap.ap_now
        if ap_now >= ap.max:
            ap_str = f"{ap_now} / {ap.max}  (已满)"
        else:
            recover_secs = max(0, ap.completeRecoveryTime - now_ts)
            ap_str = f"{ap_now} / {ap.max}  ({self._fmt_time(recover_secs)})"

        # 公招：只统计真正进行中的招募（state!=1 且 startTs>0 说明已放置标签）
        finished = card.recruit_finished
        total_r = len(card.recruit)
        active_count = sum(
            1 for r in card.recruit
            if r.state != 1 and r.startTs > 0
        )
        if total_r == 0 or active_count == 0:
            recruit_str = "暂未公招"
        else:
            active_finishes = [
                r.finishTs for r in card.recruit
                if r.state != 1 and r.startTs > 0 and r.finishTs > now_ts
            ]
            if active_finishes:
                earliest = min(active_finishes)
                remain = self._fmt_time(earliest - now_ts)
                recruit_str = f"{finished}/{total_r} ✓ ({remain})"
            else:
                recruit_str = f"{finished}/{total_r} ✓"

        # 剿灭
        c = card.campaign
        if c.reward.total > 0:
            jm_str = f"已领取 {c.reward.current} / {c.reward.total}"
        else:
            jm_str = "无记录"

        # 每日/每周
        daily = card.routine.daily
        weekly = card.routine.weekly

        # 训练室：空闲单行，进行中拆分为两行
        training = getattr(card.building, "training", None) if card.building else None
        if training and training.trainee and training.trainee.targetSkill != -1:
            char_info = card.charInfoMap.get(training.trainee.charId)
            trainee_name = char_info.name if char_info else "未知"
            skill_name = training.training_state
            remain = max(0, training.remainSecs)
            train_lines = [
                f"  训练: {trainee_name} {skill_name}",
                f"     剩余 {self._fmt_time(remain)}",
            ]
        else:
            train_lines = [f"  训练: 空闲中"]

        lines = [
            f"═ {s.name}",
            f"  理智:    {ap_str}",
            f"  公招:    {recruit_str}",
            f"  剿灭:    {jm_str}",
            f"  每日:    {daily.current} / {daily.total}",
            f"  每周:    {weekly.current} / {weekly.total}",
            *train_lines,
        ]
        return lines

    @sk.command("efmr")
    async def cmd_efmr(self, event: AstrMessageEvent):
        """终末地看板  用法: /sk efmr"""
        sender_id = event.get_sender_id()
        await self._ensure_user_umo(event)
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                return
            char = await self._selected_character(session, user, "endfield", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[EndField]角色."))
                return

            cred = CRED(cred=user.cred, token=user.cred_token)
            try:
                skland_uid = user.user_id or sender_id
                card_data = await call_api_with_refresh(
                    user, SklandAPI.endfield_card, cred, skland_uid, char
                )
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                return

            lines = self._format_efmr(card_data)
            yield event.plain_result(self._sk("\n".join(lines)))

    def _format_efmr(self, card) -> list[str]:
        """格式化终末地看板"""
        base = card.base
        dungeon = card.dungeon
        dm = card.dailyMission
        wm = card.weeklyMission
        now_ts = datetime.now().timestamp()

        # 理智
        try:
            cur_ap = int(dungeon.curStamina) if dungeon.curStamina else 0
            max_ap = int(dungeon.maxStamina) if dungeon.maxStamina else 1
            max_ts = float(dungeon.maxTs) if dungeon.maxTs else now_ts
            remain_secs = max(0, max_ts - now_ts)
            if remain_secs <= 0:
                ap_str = f"{cur_ap} / {max_ap}  (已满)"
            else:
                ap_str = f"{cur_ap} / {max_ap}  ({self._fmt_time(remain_secs)})"
        except Exception:
            ap_str = "未知"

        # 每日/每周
        daily_str = f"活跃度 {dm.dailyActivation} / {dm.maxDailyActivation}"
        weekly_str = f"每周事务 {wm.score} / {wm.total}"

        # 据点票券
        domain_lines = []
        for domain in card.domain:
            name = domain.name or "未知据点"
            mgr = domain.moneyMgr
            if mgr:
                try:
                    total = int(mgr.total) if mgr.total else 0
                    count = int(mgr.count) if mgr.count else 0
                    domain_lines.append(f"  {name}:  {count} / {total}")
                except Exception:
                    domain_lines.append(f"  {name}:  数据异常")

        lines = [
            f"═ {base.name}",
            f"  理智:      {ap_str}",
            f"  每日任务:  {daily_str}",
            f"  每周任务:  {weekly_str}",
        ]
        if domain_lines:
            lines.append("  据点票券:")
            lines.extend(domain_lines)
        return lines

    @sk.command("mr")
    async def cmd_mr(self, event: AstrMessageEvent):
        """综合看板(管理员)  用法: /sk mr"""
        sender_id = event.get_sender_id()
        await self._ensure_user_umo(event)
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                return

            # 分别收集两个游戏的输出，只在真正需要时插入空行
            ark_lines: list[str] = []
            ef_lines: list[str] = []

            # 方舟
            ark_char = await self._selected_character(session, user, "arknights", event)
            if ark_char:
                cred = CRED(cred=user.cred, token=user.cred_token)
                try:
                    ark_card = await call_api_with_refresh(
                        user, SklandAPI.ark_card, cred, str(ark_char.uid)
                    )
                    ark_lines.append("[Arknights]")
                    ark_lines.extend(self._format_arkmr(ark_card))
                except Exception as e:
                    ark_lines.append(f"[Arknights]{self._format_error(e)}")
                await session.commit()
            else:
                ark_lines.append("[Arknights]未绑定角色")

            # 终末地
            ef_char = await self._selected_character(session, user, "endfield", event)
            if ef_char:
                cred = CRED(cred=user.cred, token=user.cred_token)
                try:
                    skland_uid = user.user_id or sender_id
                    ef_card = await call_api_with_refresh(
                        user, SklandAPI.endfield_card, cred, skland_uid, ef_char
                    )
                    ef_lines.append("[EndField]")
                    ef_lines.extend(self._format_efmr(ef_card))
                except Exception as e:
                    ef_lines.append(f"[EndField]{self._format_error(e)}")
                await session.commit()
            else:
                ef_lines.append("[EndField]未绑定角色")

            # 合并：标题 + 方舟 + （如有需要）空行 + 终末地
            final_lines = ["══^森空岛综合看板^══"]
            final_lines.extend(ark_lines)
            if ark_lines and ef_lines:
                final_lines.append("")  # 仅用一个空行分隔两个游戏区块
            final_lines.extend(ef_lines)
            yield event.plain_result(self._sk("\n".join(final_lines)))

    # ==================== 卡片渲染 ====================

    def _get_bg_path(self, game: str) -> str:
        """获取卡片背景图片路径；game 为 'ark' 或 'endfield'"""
        base = self.data_dir.parent / "resources" / "images" / "background"
        if game == "endfield":
            path = base / "endfield" / "default_bg.jpg"
        else:
            path = base / "bg.jpg"
        if path.exists():
            return str(path)
        # fallback: 使用插件包内资源
        from .config import RES_DIR
        if game == "endfield":
            return str(RES_DIR / "images" / "background" / "endfield" / "default_bg.jpg")
        return str(RES_DIR / "images" / "background" / "bg.jpg")

    @sk.command("arkcard")
    async def cmd_arkcard(self, event: AstrMessageEvent):
        """明日方舟卡片  用法: /sk arkcard"""
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                event.stop_event()
                return
            char = await self._selected_character(session, user, "arknights", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[Arknights]角色."))
                event.stop_event()
                return

            yield event.plain_result(self._sk("正在渲染卡片，请稍候..."))

            cred = CRED(cred=user.cred, token=user.cred_token)
            try:
                card_data = await call_api_with_refresh(
                    user, SklandAPI.ark_card, cred, str(char.uid)
                )
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                event.stop_event()
                return

            try:
                bg_path = self._get_bg_path("ark")
                image_path = await render_ark_card(self, card_data, bg_path)
                yield event.image_result(image_path)
            except Exception as e:
                logger.exception(f"[Skland] 渲染方舟卡片失败: {e}")
                yield event.plain_result(self._sk(f"卡片渲染失败: {e}"))
            event.stop_event()

    @sk.command("efcard")
    async def cmd_efcard(self, event: AstrMessageEvent):
        """终末地全干员练度表，含技能、武器与装备。用法: /sk efcard"""
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                event.stop_event()
                return
            char = await self._selected_character(session, user, "endfield", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[EndField]角色."))
                event.stop_event()
                return

            yield event.plain_result(self._sk("正在渲染卡片，请稍候..."))

            cred = CRED(cred=user.cred, token=user.cred_token)
            try:
                skland_uid = user.user_id or sender_id
                card_data = await call_api_with_refresh(
                    user, SklandAPI.endfield_card, cred, skland_uid, char
                )
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                event.stop_event()
                return

            try:
                image_path = await render_ef_roster(card_data)
                yield event.image_result(image_path)
            except Exception as e:
                logger.exception(f"[Skland] 渲染终末地卡片失败: {e}")
                yield event.plain_result(self._sk(f"卡片渲染失败: {e}"))
            event.stop_event()

    # ==================== 肉鸽战绩 ====================

    def _get_rogue_bg_path(self, rogue_id: str = "") -> str:
        """获取肉鸽背景图片路径"""
        from .config import RES_DIR

        rogue_bg_map = {
            "rogue_1": RES_DIR / "images" / "background" / "rogue" / "pic_rogue_1_KV1.png",
            "rogue_2": RES_DIR / "images" / "background" / "rogue" / "pic_rogue_2_50.png",
            "rogue_3": RES_DIR / "images" / "background" / "rogue" / "pic_rogue_3_KV2.png",
            "rogue_4": RES_DIR / "images" / "background" / "rogue" / "pic_rogue_4_47.png",
            "rogue_5": RES_DIR / "images" / "background" / "rogue" / "pic_rogue_5_KV1.png",
        }
        path = rogue_bg_map.get(rogue_id)
        if path and path.exists():
            return str(path)
        # fallback 到默认背景
        default = RES_DIR / "images" / "background" / "rogue" / "kv_epoque14.png"
        return str(default) if default.exists() else self._get_bg_path("ark")

    @sk.command("rogue")
    async def cmd_rogue(self, event: AstrMessageEvent, topic: str = ""):
        """明日方舟肉鸽战绩  用法: /sk rogue [主题名]

        主题名可选：傀影、水月、萨米、萨卡兹、界园
        不带主题名时使用默认主题。
        """
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                event.stop_event()
                return
            char = await self._selected_character(session, user, "arknights", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[Arknights]角色."))
                event.stop_event()
                return

            yield event.plain_result(self._sk("正在获取肉鸽战绩，请稍候..."))

            # 解析主题
            topic_id = ""
            if topic:
                try:
                    topic_id = Topics(topic).topic_id
                except Exception:
                    yield event.plain_result(self._sk(f"未知主题: {topic}，使用默认主题"))

            cred = CRED(cred=user.cred, token=user.cred_token, userId=str(user.user_id or ""))
            try:
                rogue_data = await call_api_with_refresh(
                    user, SklandAPI.get_rogue, cred, str(char.uid), topic_id
                )
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                event.stop_event()
                return

            try:
                bg_path = self._get_rogue_bg_path(rogue_data.topic)
                image_path = await render_rogue_card(self, rogue_data, bg_path)
                yield event.image_result(image_path)
            except Exception as e:
                logger.exception(f"[Skland] 渲染肉鸽卡片失败: {e}")
                yield event.plain_result(self._sk(f"肉鸽卡片渲染失败: {e}"))
            event.stop_event()

    @sk.command("rginfo")
    async def cmd_rginfo(self, event: AstrMessageEvent, record_id: int = 1, favored: bool = False):
        """明日方舟肉鸽战绩详情  用法: /sk rginfo [记录ID] [--favored]

        需要先执行 /sk rogue 获取战绩数据。
        record_id: 记录序号（从1开始）
        --favored: 查看珍藏记录
        """
        sender_id = event.get_sender_id()
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                event.stop_event()
                return
            char = await self._selected_character(session, user, "arknights", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[Arknights]角色."))
                event.stop_event()
                return

            yield event.plain_result(self._sk("正在获取肉鸽详情，请稍候..."))

            # 获取肉鸽数据（与 rogue 命令相同）
            cred = CRED(cred=user.cred, token=user.cred_token, userId=str(user.user_id or ""))
            try:
                rogue_data = await call_api_with_refresh(
                    user, SklandAPI.get_rogue, cred, str(char.uid), ""
                )
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                event.stop_event()
                return

            try:
                bg_path = self._get_rogue_bg_path(rogue_data.topic)
                image_path = await render_rogue_info(self, rogue_data, bg_path, record_id, favored)
                yield event.image_result(image_path)
            except Exception as e:
                logger.exception(f"[Skland] 渲染肉鸽详情失败: {e}")
                yield event.plain_result(self._sk(f"肉鸽详情渲染失败: {e}"))
            event.stop_event()

    # ==================== 抽卡记录 ====================

    @sk.command("gacha")
    async def cmd_gacha(self, event: AstrMessageEvent):
        """明日方舟抽卡记录  用法: /sk gacha"""
        sender_id = event.get_sender_id()
        gacha_render_max = 30  # 每页最多渲染卡池数

        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                event.stop_event()
                return
            char = await self._selected_character(session, user, "arknights", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[Arknights]角色."))
                event.stop_event()
                return

            yield event.plain_result(self._sk("正在获取抽卡记录，请稍候..."))

            # 获取官网登录凭证链
            try:
                grant_code = await SklandLoginAPI.get_grant_code(user.access_token, 1)
                role_token = await SklandLoginAPI.get_role_token_by_uid(char.uid, grant_code)
                ak_cookie = await SklandLoginAPI.get_ak_cookie(role_token)
            except Exception as e:
                logger.exception(f"[Skland] 获取抽卡凭证失败: {e}")
                yield event.plain_result(self._sk(f"获取抽卡凭证失败: {e}"))
                event.stop_event()
                return

            # 获取卡池类别并拉取记录
            try:
                categories = await SklandAPI.get_gacha_categories(char.uid, role_token, user.access_token, ak_cookie)
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                event.stop_event()
                return

            all_gacha_records_flat: list[GachaInfo] = []
            for cate in categories:
                count_before = len(all_gacha_records_flat)
                try:
                    async for record in get_all_gacha_records(char, cate, user.access_token, role_token, ak_cookie):
                        all_gacha_records_flat.append(record)
                except Exception as e:
                    logger.warning(f"[Skland] 获取类别 {cate.name} 抽卡记录失败: {e}")
                    continue
                count_after = len(all_gacha_records_flat)
                logger.debug(
                    f"[Skland] 角色 {char.nickname} 类别 {cate.name} 新增 {count_after - count_before} 条记录"
                )

            # 读取已有记录并去重
            existing_records = await get_gacha_records(session, user.id, char.uid)
            existing_set = {(r.gacha_ts, r.pos) for r in existing_records}

            record_to_save: list[GachaRecord] = []
            for gacha_record in all_gacha_records_flat:
                record = GachaRecord(
                    uid=user.id,
                    char_pk_id=char.id,
                    char_uid=char.uid,
                    pool_id=gacha_record.poolId,
                    pool_name=gacha_record.poolName,
                    char_id=gacha_record.charId,
                    char_name=gacha_record.charName,
                    rarity=gacha_record.rarity,
                    is_new=gacha_record.isNew,
                    gacha_ts=gacha_record.gacha_ts_sec,
                    pos=gacha_record.pos,
                )
                if (int(gacha_record.gacha_ts_sec), gacha_record.pos) not in existing_set:
                    record_to_save.append(record)

            # 先保存新记录到数据库（避免后续 call_api_with_refresh 中的 commit 导致新记录遗漏）
            if record_to_save:
                session.add_all(record_to_save)
                await session.commit()
                logger.info(f"[Skland] 保存 {len(record_to_save)} 条新抽卡记录")

            all_records = existing_records + record_to_save

        # 获取角色信息用于渲染（新开 session，避免与上面的 session 状态纠缠）
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            char = await self._selected_character(session, user, "arknights", event)
            cred = CRED(cred=user.cred, token=user.cred_token)
            try:
                user_info = await call_api_with_refresh(user, SklandAPI.ark_card, cred, str(char.uid))
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                event.stop_event()
                return

        # 重新读取所有记录并渲染（再次新开 session）
        async with await get_session() as session:
            all_records = await get_gacha_records(session, user.id, char.uid)
            gacha_data_grouped = group_gacha_records(all_records)

            if not gacha_data_grouped.pools:
                yield event.plain_result(self._sk("未找到抽卡记录。"))
                event.stop_event()
                return

            image_paths = []
            pools_slice = gacha_data_grouped.pools
            if len(pools_slice) > gacha_render_max:
                yield event.plain_result(self._sk("正在生成多页抽卡记录，将以合并转发发送。"))
                for i in range(0, len(pools_slice), gacha_render_max):
                    try:
                        image_path = await render_gacha_history(
                            self,
                            gacha_data_grouped,
                            char,
                            user_info.status,
                            i,
                            i + gacha_render_max,
                        )
                        image_paths.append(image_path)
                    except Exception as e:
                        logger.exception(f"[Skland] 渲染抽卡记录第 {i // gacha_render_max + 1} 页失败: {e}")
                        yield event.plain_result(self._sk(f"渲染第 {i // gacha_render_max + 1} 页失败: {e}"))
            else:
                try:
                    image_path = await render_gacha_history(self, gacha_data_grouped, char, user_info.status)
                    image_paths.append(image_path)
                except Exception as e:
                    logger.exception(f"[Skland] 渲染抽卡记录失败: {e}")
                    yield event.plain_result(self._sk(f"抽卡记录渲染失败: {e}"))

            async for result in self._send_gacha_images(event, image_paths, "明日方舟抽卡记录"):
                yield result
            event.stop_event()

    async def _send_gacha_images(self, event: AstrMessageEvent, paths: list[str], title: str):
        if len(paths) > 1 and event.get_platform_name() == "aiocqhttp":
            for start in range(0, len(paths), 20):
                nodes = [
                    Comp.Node(
                        uin=event.get_self_id(), name="Priestess Bot",
                        content=[Comp.Plain(f"{title} · 第 {index + 1}/{len(paths)} 页"), Comp.Image.fromFileSystem(path)],
                    )
                    for index, path in enumerate(paths[start:start + 20], start)
                ]
                yield event.chain_result([Comp.Nodes(nodes)])
        else:
            for path in paths:
                yield event.image_result(path)

    @sk.command("efgacha")
    async def cmd_efgacha(self, event: AstrMessageEvent):
        """终末地抽卡记录  用法: /sk efgacha"""
        sender_id = event.get_sender_id()
        ef_gacha_render_max = 5  # 每页最多渲染卡池数

        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            if not user:
                yield event.plain_result(self._sk("未绑定森空岛账号."))
                event.stop_event()
                return
            char = await self._selected_character(session, user, "endfield", event)
            if not char:
                yield event.plain_result(self._sk("未找到绑定的[EndField]角色."))
                event.stop_event()
                return

            yield event.plain_result(self._sk("正在获取终末地抽卡记录，请稍候..."))

            # 获取官网登录凭证
            try:
                grant_code = await SklandLoginAPI.get_grant_code(user.access_token, 1)
                role_token = await SklandLoginAPI.get_role_token_by_uid(char.uid, grant_code)
            except Exception as e:
                logger.exception(f"[Skland] 获取抽卡凭证失败: {e}")
                yield event.plain_result(self._sk(f"获取抽卡凭证失败: {e}"))
                event.stop_event()
                return

            # 获取所有卡池类型的记录
            all_ef_records: list[GachaRecord] = []
            existing_records = await get_gacha_records(session, user.id, char.uid)
            existing_set = {(r.gacha_ts, r.pos) for r in existing_records}

            pool_types = [EndfieldPoolType.STANDARD, EndfieldPoolType.SPECIAL, EndfieldPoolType.BEGINNER, EndfieldPoolType.WEAPON, RERUN]
            for pool_type in pool_types:
                try:
                    records = await get_all_ef_gacha_records(char, pool_type, role_token, concurrency=8)
                    for info in records:
                        record = GachaRecord(
                            uid=user.id,
                            char_pk_id=char.id,
                            char_uid=char.uid,
                            app_code="endfield",
                            item_type="weapon" if pool_type == EndfieldPoolType.WEAPON else "char",
                            pool_id=info.poolId,
                            pool_name=info.poolName,
                            char_id=info.item_id,
                            char_name=info.item_name,
                            rarity=info.rarity,
                            is_new=info.isNew,
                            is_free=getattr(info, "isFree", False),
                            gacha_ts=info.gacha_ts_sec,
                            pos=info.seq_id_int,
                        )
                        all_ef_records.append(record)
                        if (record.gacha_ts, record.pos) not in existing_set:
                            existing_set.add((record.gacha_ts, record.pos))
                            session.add(record)
                except Exception as e:
                    logger.warning(f"[Skland] 获取终末地 {pool_type.value} 池记录失败: {e}")
                    continue

            # 先提交新记录（渲染失败也不影响已保存的数据）
            await session.commit()

        await self.heybox.reconcile(char)

        # 重新读取所有记录并渲染（新开 session）
        async with await get_session() as session:
            user = await get_user_by_platform(session, sender_id)
            char = await self._selected_character(session, user, "endfield", event)
            all_records = await get_gacha_records(session, user.id, char.uid)

            # 获取角色信息用于渲染
            try:
                skland_uid = user.user_id or sender_id
                cred = CRED(cred=user.cred, token=user.cred_token)
                player_info = await call_api_with_refresh(
                    user, SklandAPI.endfield_card, cred, skland_uid, char
                )
                await session.commit()
            except Exception as e:
                yield event.plain_result(self._sk(self._format_error(e)))
                event.stop_event()
                return

            player = player_info.base

            # 分组并渲染
            await ef_gacha_pool_data.ensure_pools({record.pool_id for record in all_records})
            gacha_data_grouped = group_ef_gacha_records(all_records)

            if not gacha_data_grouped.all_pools:
                yield event.plain_result(self._sk("未找到终末地抽卡记录。"))
                event.stop_event()
                return

            image_paths = []
            page_pool_count = gacha_data_grouped.max_category_pool_count
            if page_pool_count > ef_gacha_render_max:
                yield event.plain_result(self._sk("正在生成多页抽卡记录，将以合并转发发送。"))
                for i in range(0, page_pool_count, ef_gacha_render_max):
                    try:
                        image_path = await render_ef_gacha_history(
                            self,
                            gacha_data_grouped,
                            player,
                            char,
                            i,
                            i + ef_gacha_render_max,
                        )
                        image_paths.append(image_path)
                    except Exception as e:
                        logger.exception(f"[Skland] 渲染终末地抽卡第 {i // ef_gacha_render_max + 1} 页失败: {e}")
                        yield event.plain_result(self._sk(f"渲染第 {i // ef_gacha_render_max + 1} 页失败: {e}"))
            else:
                try:
                    image_path = await render_ef_gacha_history(self, gacha_data_grouped, player, char)
                    image_paths.append(image_path)
                except Exception as e:
                    logger.exception(f"[Skland] 渲染终末地抽卡记录失败: {e}")
                    yield event.plain_result(self._sk(f"终末地抽卡记录渲染失败: {e}"))

            async for result in self._send_gacha_images(event, image_paths, "终末地抽卡记录"):
                yield result
            event.stop_event()

    # ==================== 公招推荐 ====================

    @sk.command("recruit")
    async def cmd_recruit(self, event: AstrMessageEvent):
        """明日方舟公开招募标签推荐  用法: /sk recruit [标签1 标签2 ...] 或直接发送截图"""
        raw_tags: list[str] = []
        image_url: str | None = None

        # 1. 尝试从消息链中提取图片
        # 使用 convert_to_file_path() 统一转换为本地路径，
        # 自动处理 QQ 的 file_id、网络 URL、base64 等各种格式
        for comp in event.get_messages():
            if isinstance(comp, Comp.Image):
                try:
                    image_url = await comp.convert_to_file_path()
                    if image_url:
                        break
                except Exception:
                    logger.warning("[Skland] convert_to_file_path 失败，尝试 fallback 到 url/file")
                    image_url = comp.url or comp.file
                    if image_url:
                        break

        # 2. 尝试从文字参数中提取标签
        msg_str = event.get_message_str()
        # 去掉命令前缀 "/sk recruit" 或 "/sk 公招"
        parts = msg_str.split()
        # 找到 "recruit" 或 "公招" 后的所有参数
        cmd_idx = -1
        for i, p in enumerate(parts):
            if p in ("recruit", "公招", "/sk"):
                if p == "/sk" and i + 1 < len(parts) and parts[i + 1] in ("recruit", "公招"):
                    cmd_idx = i + 1
                    break
                elif p in ("recruit", "公招"):
                    cmd_idx = i
                    break
        if cmd_idx >= 0:
            raw_tags = parts[cmd_idx + 1:]

        # 3. 分支处理：图片识别 vs 文字输入
        if image_url and not raw_tags:
            # LLM 视觉识别
            try:
                yield event.plain_result(self._sk("🔍 正在识别公招截图中的标签..."))
                vision_result = await recognize_tags_from_image(self.context, image_url)
                raw_tags = vision_result.tags
                logger.info(f"[Skland] LLM 识别公招标签: {raw_tags}")
            except Exception as e:
                logger.exception(f"[Skland] 公招图片识别失败: {e}")
                yield event.plain_result(self._sk(f"❌ 图片识别失败: {e}\n请尝试直接输入标签，如：/sk recruit 近卫 输出 群攻"))
                event.stop_event()
                return

        # 4. 校验输入
        if not raw_tags:
            usage = (
                "【公招推荐】\n"
                "用法1（文字）: /sk recruit 近卫 输出 群攻\n"
                "用法2（截图）: /sk recruit + [附带公招截图]\n"
                "\n支持标签：\n"
                "资质: 新手 | 资深干员 | 高级资深干员\n"
                "位置: 远程位 | 近战位\n"
                "职业: 先锋 | 近卫 | 狙击 | 重装 | 医疗 | 辅助 | 术师 | 特种\n"
                "词缀: 治疗 | 输出 | 支援 | 群攻 | 减速 | 生存 | 防护\n"
                "      削弱 | 位移 | 控场 | 爆发 | 召唤 | 快速复活 | 费用回复 | 支援机械"
            )
            yield event.plain_result(self._sk(usage))
            event.stop_event()
            return

        if len(raw_tags) != RECRUIT_TAG_COUNT:
            yield event.plain_result(
                self._sk(f"⚠️ 公招界面固定显示 {RECRUIT_TAG_COUNT} 个标签，你提供了 {len(raw_tags)} 个。继续计算，但结果可能不准确。")
            )

        # 5. 标准化标签
        normalized = normalize_tags(raw_tags)
        if not normalized:
            yield event.plain_result(
                self._sk(f"❌ 无法识别任何有效标签，输入: {' | '.join(raw_tags)}\n请检查拼写或尝试截图识别。")
            )
            event.stop_event()
            return

        # 6. 计算推荐
        try:
            combos = calculate_combinations(normalized)
            report = format_recommendation(combos, raw_tags)
            yield event.plain_result(self._sk(report))
        except Exception as e:
            logger.exception(f"[Skland] 公招计算失败: {e}")
            yield event.plain_result(self._sk(f"❌ 公招计算出错: {e}"))

        event.stop_event()

    # ==================== 体力预警 ====================

    async def _ensure_user_umo(self, event: AstrMessageEvent):
        """确保用户的 UMO 已记录到数据库，用于定时任务发消息

        每个命令入口都会调用，已绑定用户会自动更新 UMO。
        """
        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin
        try:
            async with await get_session() as session:
                user = await get_user_by_platform(session, sender_id)
                if user and user.umo != umo:
                    user.umo = umo
                    await session.commit()
        except Exception as e:
            logger.debug(f"[Skland] 保存 UMO 失败: {e}")

    def _start_stamina_alert_loop(self):
        """启动体力预警后台轮询任务"""
        if self._alert_task and not self._alert_task.done():
            return
        self._alert_stop_event.clear()
        self._alert_task = asyncio.create_task(self._stamina_alert_loop())
        logger.info("[Skland] 体力预警轮询已启动，间隔 1 小时")

    def _start_sanity_reminder_loop(self):
        if self._sanity_task and not self._sanity_task.done():
            return
        self._sanity_stop_event.clear()
        self._sanity_task = asyncio.create_task(self.sanity.run(self._sanity_stop_event))

    def _start_auto_sign_loop(self):
        if self._auto_sign_task and not self._auto_sign_task.done():
            return
        now = datetime.now(CN)
        scheduled = now.replace(hour=0, minute=15, second=0, microsecond=0)
        # Loading after today's scheduled time must not send catch-up group messages.
        self._auto_sign_silent_date = now.date().isoformat() if now >= scheduled else ""
        self._auto_sign_stop_event.clear()
        self._auto_sign_task = asyncio.create_task(self._auto_sign_loop())
        logger.info("[Priestess] 自动签到任务已启动（北京时间 00:15 双游戏签到并群通知；加载或重载后的补签不发群消息）")

    async def _auto_sign_loop(self):
        state_path = self.data_dir / "daily_jobs.json"
        try:
            state = json.loads(state_path.read_text("utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        while not self._auto_sign_stop_event.is_set():
            now = datetime.now(CN)
            today = now.date().isoformat()
            runs = [
                (now.replace(hour=0, minute=15, second=0, microsecond=0), "skland"),
                (now.replace(hour=1, minute=0, second=0, microsecond=0), "efgacha"),
            ]
            for run_at, game in runs:
                if run_at > now or state.get(game) == today:
                    continue
                if self._auto_sign_stop_event.is_set():
                    return
                try:
                    logger.info("[Priestess] 开始每日任务：%s（%s）", game, today)
                    if game == "efgacha":
                        await self._auto_sync_ef_gacha()
                        complete = True
                    else:
                        complete = await self._auto_sign_combined(state, today)
                    if complete:
                        state[game] = today
                    else:
                        logger.warning("[Priestess] 每日签到存在失败，5 分钟后重试：%s", game)
                    temporary = state_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(state), "utf-8")
                    temporary.replace(state_path)
                except Exception:
                    logger.exception("[Priestess] 每日任务异常，5 分钟后重试：%s", game)
            future = [run for run, _ in runs if run > now]
            delay = min(300, max(1, (min(future) - now).total_seconds())) if future else 300
            try:
                await asyncio.wait_for(self._auto_sign_stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    async def _auto_sign_combined(self, state: dict, today: str) -> bool:
        daily = state.setdefault("group_sign", {})
        if daily.get("date") != today:
            daily.clear()
            daily.update(date=today, results={}, notified=[])
        results = daily["results"]
        complete = True
        async with await get_session() as session:
            users = list((await session.execute(select(SkUser.platform_user_id))).scalars().all())
        async with self._group_sign_lock:
            for qq in users:
                icons = results.setdefault(str(qq), {})
                for game, sign in (("arknights", self._do_ark_sign), ("endfield", self._do_ef_sign)):
                    if icons.get(game) in ("✅", "—"):
                        continue
                    try:
                        icons[game] = self._group_sign_icon(await sign(sender_id=qq))
                    except Exception:
                        icons[game] = "⚠️"
                        logger.warning("[Priestess] 自动签到账号异常：%s/%s", qq, game)
                    if icons[game] not in ("✅", "—"):
                        complete = False
                    await asyncio.sleep(1)
            if today == getattr(self, "_auto_sign_silent_date", ""):
                logger.info("[Priestess] 启动补签结束，本次不发送群通知")
                return complete
            platforms = self.context.platform_manager.get_insts()
            onebots = [p for p in platforms if p.meta().name == "aiocqhttp"]
            if users and not onebots:
                return False
            for platform in onebots:
                try:
                    client = platform.get_client()
                    groups = await client.call_action("get_group_list")
                    for group in groups:
                        gid = str(group["group_id"])
                        key = f"{platform.meta().id}:{gid}"
                        if key in daily["notified"]:
                            continue
                        try:
                            members = await client.call_action("get_group_member_list", group_id=int(gid))
                            rows = []
                            for member in members:
                                icons = results.get(str(member["user_id"]))
                                if icons is None:
                                    continue
                                name = str(member.get("card") or member.get("nickname") or member["user_id"])
                                name = re.sub(r"[\r\n\t\x00-\x1f\x7f]", " ", name).replace("|", "｜")[:30]
                                rows.append(f"{icons['arknights']} | {icons['endfield']} | {name}")
                            if not rows:
                                continue
                            await client.call_action("send_group_msg", group_id=int(gid), message=self._group_sign_text(rows))
                            daily["notified"].append(key)
                        except Exception:
                            complete = False
                            logger.warning("[Priestess] 自动签到群通知失败：%s", gid)
                except Exception:
                    complete = False
                    logger.warning("[Priestess] 自动签到无法读取 QQ 群列表，稍后重试")
        logger.info("[Priestess] 双游戏自动签到结束，已通知 %s 个群", len(daily["notified"]))
        return complete

    async def _auto_sync_ef_gacha(self) -> None:
        async with await get_session() as session:
            users = await get_all_users_with_umos(session)
            user_ids = [user.platform_user_id for user in users]
        pool_types = [EndfieldPoolType.STANDARD, EndfieldPoolType.SPECIAL, EndfieldPoolType.BEGINNER, EndfieldPoolType.WEAPON, RERUN]
        for platform_user_id in user_ids:
            try:
                reconcile_chars = []
                async with await get_session() as session:
                    user = await get_user_by_platform(session, platform_user_id)
                    if user is None:
                        continue
                    chars = await get_ef_characters(session, user)
                    reconcile_chars = list(chars)
                    for char in chars:
                        try:
                            grant_code = await SklandLoginAPI.get_grant_code(user.access_token, 1)
                            role_token = await SklandLoginAPI.get_role_token_by_uid(char.uid, grant_code)
                            existing = await get_gacha_records(session, user.id, char.uid)
                            existing_set = {(record.gacha_ts, record.pos) for record in existing}
                            additions = []
                            for pool_type in pool_types:
                                records = await get_all_ef_gacha_records(char, pool_type, role_token, concurrency=4)
                                for info in records:
                                    key = (int(info.gacha_ts_sec), int(info.seq_id_int))
                                    if key in existing_set:
                                        continue
                                    existing_set.add(key)
                                    additions.append(GachaRecord(
                                        uid=user.id,
                                        char_pk_id=char.id,
                                        char_uid=char.uid,
                                        app_code="endfield",
                                        item_type="weapon" if pool_type == EndfieldPoolType.WEAPON else "char",
                                        pool_id=info.poolId,
                                        pool_name=info.poolName,
                                        char_id=info.item_id,
                                        char_name=info.item_name,
                                        rarity=info.rarity,
                                        is_new=info.isNew,
                                        is_free=getattr(info, "isFree", False),
                                        gacha_ts=key[0],
                                        pos=key[1],
                                    ))
                            if additions:
                                session.add_all(additions)
                            logger.info("[Priestess] Automatic Endfield gacha sync: %s (%s new rows)", char.nickname, len(additions))
                        except Exception as error:
                            logger.warning("[Priestess] Automatic Endfield gacha sync failed for %s: %s", char.nickname, type(error).__name__)
                    await session.commit()
                for char in reconcile_chars:
                    await self.heybox.reconcile(char)
            except Exception:
                logger.exception("[Priestess] Automatic Endfield gacha sync failed for one account")

    async def _auto_sign_game(self, game: str) -> bool:
        async with await get_session() as session:
            users = list((await session.execute(select(SkUser))).scalars().all())
            user_ids = [user.platform_user_id for user in users]
        complete = True
        logger.info("[Priestess] 每日 %s 签到检查 %s 个绑定账号", game, len(user_ids))
        getter = get_ark_characters if game == "arknights" else get_ef_characters
        for platform_user_id in user_ids:
            try:
                async with await get_session() as session:
                    user = await get_user_by_platform(session, platform_user_id)
                    if user is None:
                        continue
                    chars = await getter(session, user)
                    for char in chars:
                        try:
                            cred = CRED(cred=user.cred, token=user.cred_token)
                            if game == "arknights":
                                result = await call_api_with_refresh(
                                    user, SklandAPI.ark_sign, cred, str(char.uid), char.channel_master_id
                                )
                                awards = ", ".join(f"{item.resource.name} x{item.count}" for item in result.awards)
                            else:
                                result = await call_api_with_refresh(
                                    user, SklandAPI.endfield_sign, cred, char.role_id, char.channel_master_id
                                )
                                resources = result.resourceInfoMap or {}
                                awards = ", ".join(
                                    f"{resources[item.id].name} x{resources[item.id].count}"
                                    for item in (result.awardIds or []) if item.id in resources
                                )
                            logger.info("[Priestess] Daily %s sign-in succeeded for %s (%s)", game, platform_user_id, char.nickname)
                            if awards:
                                logger.info("[Priestess] Daily sign-in rewards: %s", awards)
                        except Exception as error:
                            message = str(error)
                            if "请勿重复签到" not in message and "已签到" not in message:
                                complete = False
                                logger.warning("[Priestess] Daily %s sign-in failed for %s/%s: %s", game, platform_user_id, char.nickname, self._format_error(error))
                    await session.commit()
            except Exception:
                complete = False
                logger.exception("[Priestess] Daily %s sign-in failed for one account", game)
        logger.info("[Priestess] 每日 %s 签到检查结束：%s", game, "全部完成（含已签到）" if complete else "部分失败，稍后重试")
        return complete

    async def _stamina_alert_loop(self):
        """体力预警主循环：每小时检查一次所有绑定用户的体力

        流程：
        1. 等待 30 秒让框架完全初始化
        2. 循环执行检查，直到收到停止信号
        3. 每次检查间隔 1 小时
        """
        await asyncio.sleep(30)
        while not self._alert_stop_event.is_set():
            try:
                await self._check_all_stamina()
            except Exception as e:
                logger.exception(f"[Skland] 体力预警检查异常: {e}")
            # 等待 1 小时或直到停止事件触发
            try:
                await asyncio.wait_for(self._alert_stop_event.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass

    async def _check_all_stamina(self):
        """检查所有已保存 UMO 的用户的体力状态"""
        async with await get_session() as session:
            users = await get_all_users_with_umos(session)
        if not users:
            return
        logger.info(f"[Skland] 体力预警：开始检查 {len(users)} 个用户")
        for user in users:
            try:
                await self._check_user_stamina(user)
            except Exception as e:
                logger.warning(f"[Skland] 检查用户 {user.platform_user_id} 体力失败: {e}")
        logger.info("[Skland] 体力预警：本轮检查完成")

    async def _check_user_stamina(self, user: SkUser):
        """检查单个用户的所有角色体力"""
        async with await get_session() as session:
            # 明日方舟角色
            ark_chars = await get_ark_characters(session, user)
            for char in ark_chars:
                await self._check_ark_stamina(session, user, char)
            # 终末地角色
            ef_chars = await get_ef_characters(session, user)
            for char in ef_chars:
                await self._check_ef_stamina(session, user, char)
            await session.commit()

    async def _check_ark_stamina(self, session, user: SkUser, char: Character):
        """检查明日方舟角色理智，超过阈值时发送预警

        预警逻辑：
        - 理智 >= 90% 且未预警 → 发送消息并标记已预警
        - 理智 < 90% 且已预警 → 重置预警状态
        """
        cred = CRED(cred=user.cred, token=user.cred_token)
        try:
            card = await call_api_with_refresh(
                user, SklandAPI.ark_card, cred, str(char.uid)
            )
        except Exception as e:
            logger.debug(f"[Skland] 获取方舟看板失败 ({char.nickname}): {e}")
            return

        ap = card.status.ap
        ap_now = ap.ap_now
        max_ap = ap.max
        ratio = ap_now / max_ap if max_ap > 0 else 0

        alert = await get_or_create_stamina_alert(session, user.id, char.uid, "arknights")

        if ratio >= 0.9:
            if not alert.alerted:
                msg = (
                    f"[Sklands 体力预警]\n"
                    f"游戏：明日方舟\n"
                    f"角色：{card.status.name}\n"
                    f"理智：{ap_now}/{max_ap} ({ratio * 100:.0f}%)\n"
                    f"理智已满或即将回满，请及时清理！"
                )
                chain = MessageChain()
                chain.chain = [Comp.Plain(self._sk(msg))]
                try:
                    await self.context.send_message(user.umo, chain)
                    alert.alerted = True
                    logger.info(
                        f"[Skland] 已发送方舟体力预警: {user.platform_user_id} / {card.status.name}"
                    )
                except Exception as e:
                    logger.warning(f"[Skland] 发送预警消息失败: {e}")
        else:
            if alert.alerted:
                alert.alerted = False
                logger.debug(
                    f"[Skland] 重置方舟预警状态: {user.platform_user_id} / {char.nickname}"
                )
        alert.last_check_time = int(datetime.now().timestamp())

    async def _check_ef_stamina(self, session, user: SkUser, char: Character):
        """检查终末地角色体力，超过阈值时发送预警"""
        cred = CRED(cred=user.cred, token=user.cred_token)
        try:
            skland_uid = user.user_id or user.platform_user_id
            card = await call_api_with_refresh(
                user, SklandAPI.endfield_card, cred, skland_uid, char
            )
        except Exception as e:
            logger.debug(f"[Skland] 获取终末地看板失败 ({char.nickname}): {e}")
            return

        dungeon = card.dungeon
        try:
            cur_ap = int(dungeon.curStamina) if dungeon.curStamina else 0
            max_ap = int(dungeon.maxStamina) if dungeon.maxStamina else 1
        except (ValueError, TypeError):
            return

        ratio = cur_ap / max_ap if max_ap > 0 else 0

        alert = await get_or_create_stamina_alert(session, user.id, char.uid, "endfield")

        if ratio >= 0.9:
            if not alert.alerted:
                msg = (
                    f"[Sklands 体力预警]\n"
                    f"游戏：终末地\n"
                    f"角色：{card.base.name}\n"
                    f"体力：{cur_ap}/{max_ap} ({ratio * 100:.0f}%)\n"
                    f"体力已满或即将回满，请及时清理！"
                )
                chain = MessageChain()
                chain.chain = [Comp.Plain(self._sk(msg))]
                try:
                    await self.context.send_message(user.umo, chain)
                    alert.alerted = True
                    logger.info(
                        f"[Skland] 已发送终末地体力预警: {user.platform_user_id} / {card.base.name}"
                    )
                except Exception as e:
                    logger.warning(f"[Skland] 发送预警消息失败: {e}")
        else:
            if alert.alerted:
                alert.alerted = False
                logger.debug(
                    f"[Skland] 重置终末地预警状态: {user.platform_user_id} / {char.nickname}"
                )
        alert.last_check_time = int(datetime.now().timestamp())

    # ==================== 工具方法 ====================

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        """格式化秒数为  xh xm"""
        if seconds <= 0:
            return "0m"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
