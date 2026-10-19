"""Group Arknights luck rankings using the same six-star averages as personal records."""

from html import escape

from ..db import get_default_ark_character, get_gacha_records, get_session, get_user_by_platform
from ..data_source import gacha_table_data
from ..plugins import ef_theme
from .gacha_rank import GachaRank, RankPage
from . import gacha_rank_render as view

CATEGORIES = {"常规": ("常规寻访", "norm"), "限定": ("限定寻访", "limit"), "中坚": ("中坚寻访", "doub")}


class ArkGachaRank(GachaRank):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.optout_path = data_dir / "ark_gacha_rank_optout.json"

    async def page(self, event, board: str | None = None, action: str = "") -> RankPage:
        if action in ("退出", "加入"):
            return await super().page(event, None, action)
        category = (board or "全部").strip()
        category = "常规" if category == "标准" else category
        if category not in (*CATEGORIES, "全部"):
            return RankPage("text", "用法：/方舟欧非榜 [常规/限定/中坚]\n/方舟欧非榜 退出 或 加入 控制是否上榜。")
        if not event.get_group_id():
            return RankPage("text", "方舟欧非榜只能在群里使用。")
        group = await event.get_group()
        if group is None or not group.members:
            return RankPage("text", "无法获取本群成员列表，请稍后再试。")
        try:
            opted_out = self._opted_out()
        except RuntimeError:
            return RankPage("text", "方舟欧非榜退出设置暂时无法读取，当前不生成榜单。")
        if not gacha_table_data.gacha_table:
            return RankPage("text", "明日方舟卡池资料尚未加载，暂无法准确划分榜单，请等待资料加载后重试。")
        selected = list(CATEGORIES) if category == "全部" else [category]
        entries = {key: [] for key in selected}
        unknown = set()
        pool_ids = {pool.gachaPoolId for pool in gacha_table_data.gacha_table}
        from ..main import group_gacha_records

        async with await get_session() as session:
            for member in group.members:
                qq = str(member.user_id)
                if qq in opted_out:
                    continue
                user = await get_user_by_platform(session, qq)
                if user is None:
                    continue
                char = await get_default_ark_character(session, user)
                if char is None:
                    continue
                records = [r for r in await get_gacha_records(session, user.id, char.uid) if r.app_code == "arknights"]
                unknown.update(r.pool_id for r in records if r.pool_id not in pool_ids)
                # Unknown pools cannot be assigned a fair comparison category.
                known_records = [r for r in records if r.pool_id in pool_ids]
                if not known_records:
                    continue
                stats = group_gacha_records(known_records)
                for key in selected:
                    _, group_key = CATEGORIES[key]
                    entries[key].append(view.Entry(
                        qq=qq, name=member.nickname or qq,
                        up_count=getattr(stats, f"{group_key}_total_six"),
                        up_avg=getattr(stats, f"{group_key}_six_avg"), weapon_avg=0, weapon_up=0,
                    ))
        sections = []
        requester = event.get_sender_id()
        for key in selected:
            title, _ = CATEGORIES[key]
            board_view = view.Board(
                pool=title, command="/方舟欧非榜", english="Arknights", basis="平均六星抽数",
                target="一个六星", measure="个", empty="暂无足够记录", avg=lambda entry: entry.up_avg,
                count=lambda entry: entry.up_count, count_label="6★",
            )
            ranked = board_view.ranked(entries[key])
            if not ranked:
                sections.append(f'<div class="mine">{title}：暂无达到 2 个六星的群成员记录。</div>')
                continue
            mine = next(((i, entry) for i, entry in enumerate(ranked, 1) if entry.qq == requester), None)
            if mine:
                i, entry = mine
                sections.append(f'<div class="mine">你的{title}排名：第 <b>{i}</b> / {len(ranked)} 名，平均 <b>{entry.up_avg:.1f}</b> 抽出六星，共 {entry.up_count} 个。</div>')
            sections.append(view._tables(board_view, ranked, requester))
        if unknown:
            sections.append('<div class="mine">部分记录的卡池资料缺失，已从统计排除：' + escape("、".join(sorted(unknown))) + '</div>')
        html = (
            f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{view.CSS % (view.WIDTH, view.FULL_COLUMNS)}</style></head><body class="ef">'
            + ef_theme.head("ARKNIGHTS · Headhunting Luck Board", "明日方舟 · <em>本群欧非榜</em>", "按平均六星抽数升序排名，越少越欧；常规、限定、中坚分别比较")
            + "".join(sections)
            + ef_theme.foot("统计来自本群成员默认方舟角色已保存的记录，每类至少 2 个六星参与排名。<br>平均六星抽数与个人抽卡统计一致，去掉每池末尾未出六星的垫抽；不以 UP 数排名。记录覆盖期不同，结果仅供参考。<br>发送 /方舟欧非榜 常规、限定 或 中坚 筛选；/方舟欧非榜 退出 或 加入。")
            + "</body></html>"
        )
        return RankPage("html", html)
