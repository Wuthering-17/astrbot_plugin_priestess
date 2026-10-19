"""Group Endfield pull-luck boards over locally stored gacha records."""

from __future__ import annotations

import json
from pathlib import Path

from astrbot.api.event import AstrMessageEvent

from ..db import (
    get_default_ef_character,
    get_gacha_records,
    get_session,
    get_user_by_platform,
)

MIN_UP = 2
TOP = 10


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

    async def page(self, event: AstrMessageEvent, board: str | None, action: str = "") -> str:
        if action in ("退出", "加入"):
            try:
                values = self._opted_out()
            except RuntimeError:
                return "欧非榜退出设置暂时无法读取，请联系管理员修复后再操作。"
            qq = event.get_sender_id()
            if action == "退出":
                values.add(qq)
            else:
                values.discard(qq)
            self._save_opted_out(values)
            return "已退出欧非榜，你不会出现在榜单中。" if action == "退出" else "已重新加入欧非榜。"
        if not event.get_group_id():
            return "欧非榜只能在群里使用。"
        group = await event.get_group()
        if group is None or not group.members:
            return "无法获取本群成员列表，请稍后再试。"
        names = {
            str(member.user_id): (member.nickname or str(member.user_id))
            for member in group.members
        }
        try:
            opted_out = self._opted_out()
        except RuntimeError:
            return "欧非榜退出设置暂时无法读取，为保护已退出成员的选择，当前不生成榜单。"
        entries: dict[str, dict] = {}
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

                stats = group_ef_gacha_records(records)
                entries[qq] = {
                    "name": nickname,
                    "char_count": stats.special_up_count,
                    "char_avg": stats.special_up_avg,
                    "weapon_count": stats.weapon_up_count,
                    "weapon_avg": stats.weapon_up_avg,
                }
        boards = []
        if board in (None, "角色", "综合"):
            boards.append(("角色池", "char_avg", "char_count", "个", "UP 干员"))
        if board in (None, "武器", "综合"):
            boards.append(("武器池", "weapon_avg", "weapon_count", "把", "UP 武器"))
        sections = []
        for title, avg_key, count_key, unit, noun in boards:
            ranked = sorted(
                (entry for entry in entries.values() if entry[count_key] >= MIN_UP),
                key=lambda entry: (entry[avg_key], -entry[count_key]),
            )
            sections.append(self._format_board(title, ranked, avg_key, count_key, unit, noun))
        if not sections or not any("暂无足够记录" not in section for section in sections):
            return "本群还没有足够的终末地抽卡记录（每个榜至少需要 2 个 UP）。"
        mine = entries.get(event.get_sender_id())
        if mine:
            sections.append(
                "你的记录：角色池 "
                + self._mine(mine, "char_avg", "char_count", "个", "一个 UP 干员")
                + "；武器池 "
                + self._mine(mine, "weapon_avg", "weapon_count", "把", "一把 UP 武器")
            )
        return "\n\n".join(sections)

    @staticmethod
    def _mine(entry: dict, avg_key: str, count_key: str, unit: str, target: str) -> str:
        if entry[count_key] < MIN_UP:
            return f"不足 {MIN_UP} 个 UP"
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
