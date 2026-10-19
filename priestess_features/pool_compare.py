"""Current-group, single-pool history comparison over saved default-role records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from html import escape
import math

from ..data_source import ef_gacha_pool_data, gacha_table_data
from ..db import get_default_ark_character, get_default_ef_character, get_gacha_records, get_session, get_user_by_platform
from ..plugins import ef_theme
from ..render_adapter import _try_star_html_render

WIDTH = 1100
MEMBERS_PER_PAGE = 16
EVENTS_PER_PAGE = 40
GAME_NAMES = {"arknights": "明日方舟", "endfield": "终末地"}
CN = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Pool:
    game: str
    id: str
    name: str


@dataclass
class Member:
    qq: str
    name: str
    role: str
    records: list
    six: int = 0
    free_six: int = 0
    up: int = 0
    free_up: int = 0
    average: float | None = None
    up_average: float | None = None
    tail: int = 0
    rank: int | None = None


@dataclass
class Comparison:
    pool: Pool
    members: list[Member]
    up_ids: set[str] = field(default_factory=set)
    full: bool = False
    requester: str = ""

    def six_star(self, record) -> bool:
        return record.rarity == (5 if self.pool.game == "arknights" else 6)

    def free(self, record) -> bool:
        return self.pool.game == "endfield" and record.is_free

    def item_id(self, record) -> str:
        if record.char_id or self.pool.game == "arknights":
            return record.char_id
        return ef_gacha_pool_data.resolve_item_id(record.item_type, record.char_name, record.pool_id)

    def is_up(self, record) -> bool:
        return self.six_star(record) and self.item_id(record) in self.up_ids

    def events(self) -> list[tuple[Member, object, int, int | None]]:
        events = []
        for member in self.members:
            interval = 0
            for index, record in enumerate(member.records, 1):
                free = self.free(record)
                if not free:
                    interval += 1
                if self.full or self.six_star(record):
                    events.append((member, record, index, None if free else interval))
                if self.six_star(record) and not free:
                    interval = 0
        return events

    @property
    def page_count(self) -> int:
        events = sum(len(member.records) if self.full else member.six + member.free_six for member in self.members)
        return math.ceil(len(self.members) / MEMBERS_PER_PAGE) + math.ceil(events / EVENTS_PER_PAGE)


def calculate(comparison: Comparison) -> None:
    for member in comparison.members:
        paid = [record for record in member.records if not comparison.free(record)]
        six_positions = [i for i, record in enumerate(paid, 1) if comparison.six_star(record)]
        up_positions = [i for i, record in enumerate(paid, 1) if comparison.is_up(record)]
        member.six = len(six_positions)
        member.free_six = sum(comparison.six_star(record) and comparison.free(record) for record in member.records)
        member.up = len(up_positions)
        member.free_up = sum(comparison.is_up(record) and comparison.free(record) for record in member.records)
        member.average = six_positions[-1] / len(six_positions) if six_positions else None
        member.up_average = up_positions[-1] / len(up_positions) if up_positions else None
        member.tail = len(paid) - (six_positions[-1] if six_positions else 0)
    ranked = sorted((member for member in comparison.members if member.six >= 2), key=lambda member: (member.average, -member.six, member.qq))
    for rank, member in enumerate(ranked, 1):
        member.rank = rank
    comparison.members.sort(key=lambda member: (member.rank is None, member.rank or 0, member.name, member.qq))


CSS = """
body{width:1100px;padding:24px} .meta{padding:14px 0;color:var(--ef-sub);font-size:15px;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;table-layout:fixed;background:var(--ef-panel);margin:14px 0}
th{background:var(--ef-dark);color:white;font-size:15px;text-align:left;padding:10px 8px}
td{padding:10px 8px;border-bottom:1px solid var(--ef-line);font-size:15px;overflow-wrap:anywhere}
.person{width:190px}.person small{display:block;color:var(--ef-sub);font-size:12px;margin-top:3px}
.rank{width:65px}.num{font-family:var(--ef-num);font-size:22px;font-weight:700}
.me td{background:#fff4ab}.hint{padding:12px 16px;border-left:6px solid var(--ef-yellow);background:var(--ef-panel);font-size:14px;line-height:1.7}
.six{font-weight:bold;background:#ffeabb}.badge{font-size:12px;background:var(--ef-yellow);padding:2px 5px;margin-left:5px}
.stamp{font-size:12px;color:var(--ef-sub);width:150px}.event{width:260px}
"""


def _number(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "—"


def document(comparison: Comparison, title: str, body: str, number: int) -> str:
    pool = comparison.pool
    notice = "已加载该池 UP 资料。" if comparison.up_ids else "该池 UP 资料缺失或未配置，UP 列暂不计算；六星统计不受影响。"
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS}</style></head><body class="ef">'
        + ef_theme.head("PRIESTESS · GROUP POOL COMPARISON", escape(title), escape(f"{GAME_NAMES[pool.game]} · {pool.name}"))
        + f'<div class="meta">卡池 ID：{escape(pool.id)} · {len(comparison.members)} 位群友 · 第 {number}/{comparison.page_count} 页</div>'
        + body
        + f'<div class="hint">{notice}<br>六星/UP 数显示为“有效 + 免费”。平均抽数去掉上次出货后的尾抽，免费抽不计入平均值；至少 2 个有效六星参与排名。<br>单池记录无法完整还原跨池继承保底，记录覆盖期不同，比较仅供参考。小黑盒的低星占位记录会按原名称展示。</div>'
        + ef_theme.foot("仅展示当前群成员默认角色已保存的本池记录；遵守对应游戏欧非榜的退出设置。<br>追加“明细”查看全部逐抽记录；本指令不刷新游戏接口。")
        + "</body></html>"
    )


def summary_html(comparison: Comparison, members: list[Member], page: int) -> str:
    rows = []
    for member in members:
        rank = str(member.rank) if member.rank is not None else "样本不足"
        up = f"{member.up} + {member.free_up}" if comparison.up_ids else "—"
        rows.append(
            f'<tr class="{"me" if member.qq == comparison.requester else ""}"><td class="rank">{rank}</td>'
            f'<td class="person">{escape(member.name)}<small>{escape(member.role)}</small></td>'
            f'<td class="num">{len(member.records)}</td><td>{member.six} + {member.free_six}</td><td>{up}</td>'
            f'<td class="num">{_number(member.average)}</td><td class="num">{_number(member.up_average) if comparison.up_ids else "—"}</td><td>{member.tail}</td></tr>'
        )
    body = '<table><thead><tr><th class="rank">排名</th><th class="person">群友 / 游戏角色</th><th>总抽数</th><th>六星</th><th>UP</th><th>平均六星</th><th>平均 UP</th><th>尾抽</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
    return document(comparison, "本群 · 指定卡池对比", body, page)


def events_html(comparison: Comparison, events: list, page: int) -> str:
    rows = []
    for member, record, index, interval in events:
        six = comparison.six_star(record)
        free = comparison.free(record)
        star = record.rarity + (1 if comparison.pool.game == "arknights" else 0)
        badge = '<span class="badge">免费</span>' if free else ''
        if comparison.is_up(record):
            badge += '<span class="badge">UP</span>'
        elif six and comparison.up_ids:
            badge += '<span class="badge">非 UP</span>'
        stamp = datetime.fromtimestamp(record.gacha_ts, CN).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            f'<tr class="{"six" if six else ""}"><td class="person">{escape(member.name)}</td><td>{index}</td>'
            f'<td class="event">{escape(record.char_name)} · {star}★{badge}</td><td>{interval if interval is not None else "免费"}</td><td class="stamp">{stamp}</td></tr>'
        )
    title = "完整逐抽记录" if comparison.full else "六星出货明细"
    body = '<table><thead><tr><th class="person">群友</th><th>本地记录序号</th><th class="event">抽取结果</th><th>距上次有效六星</th><th class="stamp">时间</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
    return document(comparison, title, body, page)


class PoolCompare:
    def __init__(self, plugin):
        self.plugin = plugin

    async def prepare(self, event, query: str, game: str | None = None) -> tuple[str, Comparison | None]:
        if not event.get_group_id():
            return "卡池对比只能在群里使用。", None
        full = query.endswith(" 明细")
        query = query[:-3].strip() if full else query.strip()
        if not query:
            return "用法：/卡池对比 卡池名或ID [明细]\n也可用 /方舟卡池对比 或 /终末地卡池对比 限定游戏。只读取本群成员默认角色已保存的记录。", None
        group = await event.get_group()
        if group is None or not group.members:
            return "无法读取本群成员列表，请稍后重试。", None
        games = [game] if game else list(GAME_NAMES)
        try:
            opted_out = {key: (self.plugin.ark_gacha_rank if key == "arknights" else self.plugin.gacha_rank)._opted_out() for key in games}
        except RuntimeError:
            return "欧非榜退出设置无法读取，当前暂停卡池对比。", None
        candidates = {}
        if "arknights" in games:
            for pool in gacha_table_data.gacha_table:
                candidates[("arknights", pool.gachaPoolId)] = Pool("arknights", pool.gachaPoolId, pool.gachaPoolName)
        if "endfield" in games:
            for pool_id, pool in ef_gacha_pool_data.pool_table.items():
                candidates[("endfield", pool_id)] = Pool("endfield", pool_id, pool.pool_name or pool_id)
        saved = []
        async with await get_session() as session:
            for member in group.members:
                qq = str(member.user_id)
                user = await get_user_by_platform(session, qq)
                if user is None:
                    continue
                for key in games:
                    if qq in opted_out[key]:
                        continue
                    getter = get_default_ark_character if key == "arknights" else get_default_ef_character
                    char = await getter(session, user)
                    if char is None:
                        continue
                    records = [record for record in await get_gacha_records(session, user.id, char.uid) if record.app_code == key]
                    for record in records:
                        candidates.setdefault((key, record.pool_id), Pool(key, record.pool_id, record.pool_name or record.pool_id))
                    saved.append((key, qq, member.nickname or qq, char.nickname, records))
        needle = query.casefold()
        matches = [pool for pool in candidates.values() if needle in (pool.id.casefold(), pool.name.casefold())]
        if not matches and len(needle) >= 2:
            matches = [pool for pool in candidates.values() if needle in pool.name.casefold() or needle in pool.id.casefold()]
        if not matches:
            return f"没有找到卡池「{query}」。请填写准确卡池名或 ID，并先保存参与者的抽卡记录。", None
        if len(matches) > 1:
            choices = sorted(matches, key=lambda pool: (pool.game, pool.id))
            return f"找到 {len(matches)} 个卡池，请用卡池 ID 精确查询，避免混合不同期次：\n" + "\n".join(f"{GAME_NAMES[pool.game]} · {pool.name} · {pool.id}" for pool in choices[:20]), None
        selected = matches[0]
        members = []
        for key, qq, name, role, records in saved:
            if key == selected.game:
                records = sorted((record for record in records if record.pool_id == selected.id), key=lambda record: (record.gacha_ts, record.pos))
                if records:
                    members.append(Member(qq, name, role, records))
        if not members:
            return "本群尚无可展示的该池已保存记录。参与者需先查询自己的抽卡记录或导入历史，并保持加入对应游戏欧非榜。", None
        if selected.game == "endfield":
            await ef_gacha_pool_data.ensure_pools({selected.id})
            content = ef_gacha_pool_data.get_pool(selected.id)
            up_ids = set(content.up_six_char_ids) if content else set()
        else:
            from ..main import _get_up_chars
            up_ids = set(_get_up_chars(selected.id)[1])
        comparison = Comparison(selected, members, up_ids, full, event.get_sender_id())
        calculate(comparison)
        return "", comparison

    async def pictures(self, comparison: Comparison):
        number = 0
        for start in range(0, len(comparison.members), MEMBERS_PER_PAGE):
            number += 1
            yield await _try_star_html_render(self.plugin, summary_html(comparison, comparison.members[start:start + MEMBERS_PER_PAGE], number), width=WIDTH)
        events = comparison.events()
        for start in range(0, len(events), EVENTS_PER_PAGE):
            number += 1
            yield await _try_star_html_render(self.plugin, events_html(comparison, events[start:start + EVENTS_PER_PAGE], number), width=WIDTH)
