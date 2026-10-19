"""Group Endfield pull-luck boards over locally stored gacha records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal

from astrbot.api.event import AstrMessageEvent
from ..data_source import ef_gacha_pool_data
from ..render_adapter import _try_star_html_render
from . import gacha_rank_render as view

from ..db import (
    get_default_ef_character,
    get_gacha_records,
    get_session,
    get_user_by_platform,
)

MIN_UP = 2
TOP = 10


@dataclass(frozen=True)
class RankPage:
    kind: Literal["text", "html"]
    value: str


class GachaRank:
    def __init__(self, data_dir: Path) -> None:
        self.optout_path = data_dir / "gacha_rank_optout.json"

    def _opted_out(self) -> set[str]:
        if not self.optout_path.exists():
            return set()
        try:
            value = json.loads(self.optout_path.read_text("utf-8"))
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("invalid opt-out file")
            return set(value)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("cannot safely read the gacha-board opt-out list") from error

    def _save_opted_out(self, values: set[str]) -> None:
        self.optout_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.optout_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(values)), encoding="utf-8")
        temporary.replace(self.optout_path)

    async def page(self, event: AstrMessageEvent, board: str | None, action: str = "") -> RankPage:
        if action in ("退出", "加入"):
            try:
                values = self._opted_out()
            except RuntimeError:
                return RankPage("text", "欧非榜退出设置暂时无法读取，请联系管理员修复后再操作。")
            qq = event.get_sender_id()
            if action == "退出":
                values.add(qq)
            else:
                values.discard(qq)
            self._save_opted_out(values)
            return RankPage("text", "已退出欧非榜，你不会出现在榜单中。" if action == "退出" else "已重新加入欧非榜。")
        if not event.get_group_id():
            return RankPage("text", "欧非榜只能在群里使用。")
        group = await event.get_group()
        if group is None or not group.members:
            return RankPage("text", "无法获取本群成员列表，请稍后再试。")
        names = {
            str(member.user_id): (member.nickname or str(member.user_id))
            for member in group.members
        }
        try:
            opted_out = self._opted_out()
        except RuntimeError:
            return RankPage("text", "欧非榜退出设置暂时无法读取，为保护已退出成员的选择，当前不生成榜单。")
        entries: dict[str, dict] = {}
        missing_pools: set[str] = set()
        async with await get_session() as session:
            for qq, nickname in names.items():
                if qq in opted_out:
                    continue
                user = await get_user_by_platform(session, qq)
                if user is None:
                    continue
                char = await get_default_ef_character(session, user)
                if char is None:
                    continue
                records = [
                    record
                    for record in await get_gacha_records(session, user.id, char.uid)
                    if record.app_code == "endfield"
                ]
                if not records:
                    continue
                from ..main import group_ef_gacha_records

                await ef_gacha_pool_data.ensure_pools({record.pool_id for record in records})
                stats = group_ef_gacha_records(records)
                unknown_char = any(not pool.up_six_chars for pool in stats.special_pools)
                unknown_weapon = any(not pool.up_six_chars for pool in stats.weapon_pools)
                missing_pools.update(pool.pool_id for pool in stats.special_pools + stats.weapon_pools if not pool.up_six_chars)
                entries[qq] = {
                    "name": nickname,
                    "char_count": stats.special_up_count,
                    "char_avg": stats.special_up_avg,
                    "weapon_count": stats.weapon_up_count,
                    "weapon_avg": stats.weapon_up_avg,
                    "char_known": not unknown_char,
                    "weapon_known": not unknown_weapon,
                }
        boards = []
        if board in (None, "角色", "综合"):
            boards.append(("角色池", "char_avg", "char_count", "个", "UP 干员"))
        if board in (None, "武器", "综合"):
            boards.append(("武器池", "weapon_avg", "weapon_count", "把", "UP 武器"))
        sections = []
        for title, avg_key, count_key, unit, noun in boards:
            ranked = sorted(
                (entry for entry in entries.values() if entry[count_key.replace("_count", "_known")] and entry[count_key] >= MIN_UP),
                key=lambda entry: (entry[avg_key], -entry[count_key]),
            )
            sections.append(self._format_board(title, ranked, avg_key, count_key, unit, noun))
        if missing_pools:
            sections.append("部分卡池缺少 UP 资料，相关成员对应榜单暂不排名。请检查机器人访问 GitHub 的网络后重试；已导入记录仍保留。卡池：" + "、".join(sorted(missing_pools)))
        mine = entries.get(event.get_sender_id())
        if mine:
            sections.append(
                "你的记录：角色池 "
                + self._mine(mine, "char_avg", "char_count", "个", "一个 UP 干员")
                + "；武器池 "
                + self._mine(mine, "weapon_avg", "weapon_count", "把", "一把 UP 武器")
            )
        render_entries = [
            view.Entry(
                qq=qq, name=entry["name"], up_avg=entry["char_avg"], up_count=entry["char_count"],
                weapon_avg=entry["weapon_avg"], weapon_up=entry["weapon_count"],
                char_known=entry["char_known"], weapon_known=entry["weapon_known"],
            )
            for qq, entry in entries.items()
        ]
        requester = event.get_sender_id()
        if board == "角色":
            html = view._render_board(view.CHARACTERS, render_entries, requester)
        elif board == "武器":
            html = view._render_board(view.WEAPONS, render_entries, requester)
        else:
            html = view._render_overview(render_entries, requester)
        if html:
            if missing_pools:
                warning = "部分卡池缺少 UP 资料，相关成员对应榜单暂不排名：" + "、".join(sorted(missing_pools))
                html = html.replace('</body>', f'<div class="mine">{escape(warning)}</div></body>')
            return RankPage("html", html)
        return RankPage("text", "\n\n".join(sections))

    async def render(self, star, page: RankPage) -> str:
        return await _try_star_html_render(star, page.value, width=view.WIDTH)

    @staticmethod
    def _mine(entry: dict, avg_key: str, count_key: str, unit: str, target: str) -> str:
        if not entry[count_key.replace("_count", "_known")]:
            return "卡池 UP 资料缺失，暂无法计算"
        if entry[count_key] < MIN_UP:
            return f"已识别 {entry[count_key]} 个 UP，不足 {MIN_UP} 个"
        return f"平均 {entry[avg_key]:.1f} 抽出{target}（共 {entry[count_key]}{unit}）"

    @staticmethod
    def _format_board(title: str, ranked: list[dict], avg_key: str, count_key: str, unit: str, noun: str) -> str:
        if not ranked:
            return f"{title}：暂无足够记录参与排名。"
        rows = [f"{title}欧皇榜（平均抽数越低越欧，至少 {MIN_UP} 个 UP）："]
        for index, entry in enumerate(ranked[:TOP], 1):
            rows.append(f"{index}. {entry['name']}：{entry[avg_key]:.1f} 抽 / {entry[count_key]}{unit}{noun}")
        unlucky = ranked[::-1][:TOP]
        rows.append(f"{title}非酋榜：")
        for index, entry in enumerate(unlucky, 1):
            rows.append(f"{index}. {entry['name']}：{entry[avg_key]:.1f} 抽 / {entry[count_key]}{unit}{noun}")
        return "\n".join(rows)
