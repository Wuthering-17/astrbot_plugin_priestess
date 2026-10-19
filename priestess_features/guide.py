"""AstrBot service for Endfield high-difficulty guides and clear collection."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from astrbot.api import logger

from .endfield_guide import bili as bili_module, echoes, stages
from .endfield_guide.builds import BuildStore, MIN_SAMPLE
from .endfield_guide.echoes import EchoStore
from .endfield_wiki import data as wiki_data
from .endfield_wiki.lookup import normalize, to_pinyin
from ..plugins import ef_theme
from ..render_adapter import _try_star_html_render

CN = timezone(timedelta(hours=8))
DIFFICULTIES = {"普通": "普通", "简单": "普通", "困难": "困难", "残酷": "残酷", "增辉": "残酷", "苦难": "苦难", "镀层": "苦难", "高难": "苦难"}
STAGE_NUMBER = re.compile(r"^第?(\d{1,2})关?$")


class EndfieldGuide:
    def __init__(self, plugin, wiki, data_dir: Path) -> None:
        self.plugin = plugin
        self.wiki = wiki
        folder = data_dir / "endfield_guide"
        folder.mkdir(parents=True, exist_ok=True)
        self.echo_store = EchoStore(folder / "echoes.json", folder / "optout.json")
        self.build_store = BuildStore(folder / "builds.json")
        self.bili = bili_module.BiliSearch(folder / "videos.json")
        self.covers = stages.Covers(wiki.image_dir)

    def endgame(self) -> dict:
        loaded = self.wiki.resolver()
        index = loaded[0] if loaded else {}
        return stages.ordered(index.get("endgame", {}), self.echo_store.layout())

    def version(self) -> str:
        loaded = self.wiki.resolver()
        return loaded[0].get("version", "") if loaded else ""

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        try:
            return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN)
        except (TypeError, ValueError):
            return None

    def _current_week(self, endgame: dict):
        now = datetime.now(CN)
        for season in endgame.get("tower_seasons", []):
            for week in season["weeks"]:
                start, end = self._parse_time(week["open"]), self._parse_time(week["close"])
                if start and end and start <= now < end:
                    return season, week
        return None

    @staticmethod
    def _stage_groups(endgame: dict) -> list[dict]:
        groups = list(endgame.get("tower", {}).values())
        for series in endgame.get("monument", []):
            groups.extend(series["groups"])
        return groups

    def _resolve(self, endgame: dict, query: str):
        q = normalize(query)
        if not q:
            return None
        entries = [(normalize(group["name"]), "group", group) for group in self._stage_groups(endgame)]
        entries.extend((normalize(series["name"]), "series", series) for series in endgame.get("monument", []))
        exact = [entry for entry in entries if entry[0] == q]
        if exact:
            return exact[0][1], exact[0][2]
        readings = to_pinyin(q)
        by_sound = [entry for entry in entries if readings & to_pinyin(entry[0])]
        if len(by_sound) == 1:
            return by_sound[0][1], by_sound[0][2]
        prefix = [entry for entry in entries if entry[0].startswith(q) or (len(q) >= 2 and q in entry[0])]
        if len(prefix) == 1:
            return prefix[0][1], prefix[0][2]
        if 1 < len(prefix) <= 8:
            return "candidates", [entry[2]["name"] for entry in prefix]
        close = difflib.get_close_matches(q, [entry[0] for entry in entries], n=2, cutoff=0.6)
        if len(close) == 1:
            match = next(entry for entry in entries if entry[0] == close[0])
            return match[1], match[2]
        return None

    @staticmethod
    def _words(query: str) -> tuple[str, str | None, int | None]:
        words = query.split()
        difficulty = next((DIFFICULTIES[word] for word in words if word in DIFFICULTIES), None)
        number = next((int(match.group(1)) for word in words if (match := STAGE_NUMBER.match(word))), None)
        name = "".join(word for word in words if word not in DIFFICULTIES and not STAGE_NUMBER.match(word))
        return name, difficulty, number

    @staticmethod
    def _upcoming(endgame: dict) -> list[tuple[str, str, list[str]]]:
        now = datetime.now(CN)
        for season in endgame.get("tower_seasons", []):
            for week in season["weeks"]:
                start = EndfieldGuide._parse_time(week["open"])
                names = [endgame["tower"][key]["name"] for key in week["groups"] if key in endgame.get("tower", {})]
                if start and start > now and names:
                    return [(f"下期 · {week['name'] or '轮换' + str(week['week'])}", f"{start:%m/%d %H:%M}", names)]
        return []

    def _cover_jobs(self, series: list[dict], groups: list[dict]):
        return [stages.poster(item) for item in series] + [stages.stage_cover(group, wiki_data.stage_cover_url(group)) for group in groups]

    async def _foe_icons(self, groups: list[dict]) -> dict[str, str]:
        ids = sorted({foe["icon"] for group in groups for stage in group["stages"] for foe in stage["enemies"] if foe.get("icon")})
        uris = await asyncio.gather(*(self.wiki._cached_image(wiki_data.enemy_icon_url(icon_id), f"{icon_id}.png") for icon_id in ids))
        return dict(zip(ids, uris))

    async def _render(self, html: str, width: int = stages.WIDTH) -> str:
        html = self.wiki._inline_local_images(html)
        return await _try_star_html_render(self.plugin, html, width=width, base_url=self.wiki.data_dir.as_uri())

    async def collect(self):
        return await self.echo_store.collect(self.build_store)

    def set_optout(self, user_id: str, opted_out: bool) -> str:
        self.echo_store.set_optout(user_id, opted_out)
        return "已退出统计，你的记录不会再被收集或出现在榜单中" if opted_out else "已重新加入通关阵容统计，下次更新后生效"

    async def _member_data(self, event):
        requester = str(event.get_sender_id()) if event is not None else ""
        pool = {requester} if requester else set()
        names = {requester: requester} if requester else {}
        group_id = event.get_group_id() if event is not None else ""
        if group_id:
            try:
                members = await event.bot.call_action("get_group_member_list", group_id=int(group_id))
                pool = {str(member["user_id"]) for member in members}
                names = {str(member["user_id"]): str(member.get("card") or member.get("nickname") or member["user_id"]) for member in members}
            except Exception:
                logger.warning("[Priestess] Could not read QQ group members for guide statistics", exc_info=True)
        clear_data = self.echo_store.load()
        for qq in self.echo_store.optout():
            clear_data.get("members", {}).pop(qq, None)
        operators = (self.wiki.resolver() or ({}, None))[0].get("operators", {})
        aliases = {"chr_0003_endminf": "chr_0002_endminm", "chr_9000_endmin": "chr_0002_endminm"}
        known_ids = list(operators) + list(aliases) + list(aliases.values())
        by_hash = {hashlib.md5(char_id.encode()).hexdigest(): char_id for char_id in known_ids}
        for member in clear_data.get("members", {}).values():
            for record in member.get("records", {}).values():
                for character in record.get("team", []):
                    character["id"] = by_hash.get(character.get("id", ""), character.get("id", ""))
        return pool, names, clear_data, operators, group_id

    @staticmethod
    def _top_difficulty(group: dict) -> str:
        return "残酷" if group["mode"] == "战争回响" else "苦难"

    def _rank_query(self, endgame: dict, command: str, query: str, event):
        name, difficulty, _ = self._words(query)
        groups = self._stage_groups(endgame)
        if command == "回响竞速" and not name:
            current = self._current_week(endgame)
            if not current:
                return [], None, "当前没有开放中的战争回响轮换。"
            return [endgame["tower"][key] for key in current[1]["groups"] if key in endgame["tower"]], difficulty, None
        if command == "丰碑竞速" and not name:
            menu = [f"【{series['name']}】" + "、".join(f"{group['no']}.{group['name']}" for group in series["groups"]) for series in endgame.get("monument", [])]
            return [], None, "丰碑竞速：\n" + "\n".join(menu) + "\n请追加丰碑名或关卡名。"
        if not name:
            return [], None, "用法：/竞速榜 关卡名、/回响竞速 [关卡名]、/丰碑竞速 [丰碑名或关卡名]。"
        found = self._resolve(endgame, name.removesuffix("丰碑") or name)
        if not found:
            return [], None, f"没有找到「{name}」，发送 /战争回响 或 /影拓丰碑 查看关卡名。"
        if found[0] == "candidates":
            return [], None, f"找到多个关卡：{'、'.join(found[1])}"
        if found[0] == "series":
            if command == "回响竞速":
                return [], None, f"「{found[1]['name']}」是影拓丰碑，请发送 /丰碑竞速 {found[1]['name']}。"
            groups = found[1]["groups"]
        else:
            groups = [found[1]]
        if command == "回响竞速" and groups[0]["mode"] != "战争回响":
            return [], None, f"「{groups[0]['name']}」是影拓丰碑，请发送 /丰碑竞速 {groups[0]['name']}。"
        if command == "丰碑竞速" and groups[0]["mode"] == "战争回响":
            return [], None, f"「{groups[0]['name']}」是战争回响，请发送 /回响竞速 {groups[0]['name']}。"
        return groups, difficulty, None

    async def _leaderboard(self, command: str, query: str, event) -> str:
        endgame = self.endgame()
        pool, names, data, operators, group_id = await self._member_data(event)
        if command == "榜单角色出场率":
            name = query.strip()
            all_keys = {f"{group['name']}|{self._top_difficulty(group)}" for group in self._stage_groups(endgame)}
            if name in ("战争回响", "本周"):
                current = self._current_week(endgame)
                keys = {f"{endgame['tower'][key]['name']}|残酷" for key in current[1]["groups"] if key in endgame["tower"]} if current else set()
                scope = "本周战争回响（残酷）"
            elif name == "影拓丰碑":
                keys = {f"{group['name']}|苦难" for series in endgame.get("monument", []) for group in series["groups"]}
                scope = "影拓丰碑（苦难）"
            elif not name:
                keys, scope = all_keys, "全部关卡"
            else:
                found = self._resolve(endgame, name)
                if not found or found[0] != "group":
                    return f"没有找到「{name}」，用法：/榜单角色出场率 [关卡名/战争回响/影拓丰碑]。"
                group = found[1]
                keys = {f"{group['name']}|{self._top_difficulty(group)}"}
                scope = group["name"]
            total, counts = echoes.usage_rates(data, pool, keys)
            if not counts:
                return f"{scope}暂无本群记录。"
            rows = sorted(counts.items(), key=lambda row: (-row[1], operators.get(row[0], {}).get("name", row[0])))[:20]
            lines = [f"{scope} · {total} 条通关记录中的干员出场次数"]
            lines.extend(f"{rank}. {operators.get(cid, {}).get('name', cid)}：{count} 次（{count / total:.0%}）" for rank, (cid, count) in enumerate(rows, 1))
            return "\n".join(lines)

        endgame = self.endgame()
        groups, difficulty, error = self._rank_query(endgame, command, query, event)
        if error:
            return error
        limit = 20 if len(groups) == 1 else 5
        lines = ["高难竞速榜 · 本群最快通关记录"]
        for group in groups:
            selected = difficulty if difficulty and any(stage["difficulty"] == difficulty for stage in group["stages"]) else self._top_difficulty(group)
            ranking = echoes.speed_ranking(data, pool, f"{group['name']}|{selected}")
            lines.append(f"\n{group['name']} · {selected}")
            if not ranking:
                lines.append("暂无记录")
                continue
            shown = ranking[:limit]
            for rank, (qq, record) in enumerate(shown, 1):
                seconds = int(record["time"])
                duration = f"{seconds // 60}:{seconds % 60:02d}"
                plus = " · 增辉" if record.get("plus") else ""
                lines.append(f"{rank}. {names.get(qq, qq)}　{duration}{plus}")
        return "\n".join(lines)

    async def page(self, command: str, query: str = "", event=None) -> tuple[str, str, str]:
        """Return (kind, value, follow-up text); kinds are text, image, or miss."""
        endgame = self.endgame()
        if not endgame.get("tower") and not endgame.get("monument"):
            return "text", "终末地攻略资料尚未同步，请稍后再试。", ""
        version = self.version()

        if command in ("竞速榜", "回响竞速", "丰碑竞速", "榜单角色出场率"):
            return "text", await self._leaderboard(command, query, event), ""

        if command == "战争回响":
            current = self._current_week(endgame)
            if not current:
                return "text", "当前没有开放中的战争回响轮换（资料可能尚未更新）", ""
            season, week = current
            groups = [endgame["tower"][key] for key in week["groups"] if key in endgame["tower"]]
            covers, icons = await asyncio.gather(self.covers.get(self._cover_jobs([], groups)), self._foe_icons(groups))
            html = stages.rotation_html(ef_theme, season, week, groups, self._upcoming(endgame), covers, icons, datetime.now(CN), version)
            return "image", await self._render(html), ""

        if command == "影拓丰碑":
            monument = endgame.get("monument", [])
            query = query.strip().removesuffix("丰碑")
            if not query:
                covers = await self.covers.get(self._cover_jobs(monument, []))
                html = stages.overview_html(ef_theme, monument, covers, time.time(), version)
                return "image", await self._render(html), ""
            found = self._resolve(endgame, query)
            if not found:
                return "text", f"没有找到丰碑「{query}」，发送 /影拓丰碑 查看全部关卡。", ""
            if found[0] == "candidates":
                return "text", f"找到多个结果：{'、'.join(found[1])}\n请输入更完整的名字", ""
            if found[0] == "group" and found[1]["mode"] == "战争回响":
                return "text", f"「{found[1]['name']}」是战争回响关卡，请发送 /战争回响 查看本期轮换。", ""
            series = found[1] if found[0] == "series" else next(item for item in monument if item["name"] == found[1]["series"])
            marked = found[1]["name"] if found[0] == "group" else ""
            covers, icons = await asyncio.gather(self.covers.get(self._cover_jobs([series], series["groups"])), self._foe_icons(series["groups"]))
            html = stages.series_html(ef_theme, series, covers, icons, time.time(), version, marked)
            return "image", await self._render(html), ""

        if command in ("攻略", "敌人"):
            name, difficulty, number = self._words(query)
            if command == "敌人" and name in ("领袖", "领袖敌人", "目录", "列表"):
                leaders = endgame.get("leaders", [])
                if not leaders:
                    return "text", "领袖敌人数据尚未同步。", ""
                return "text", "领袖敌人目录：\n" + "、".join(item["name"] for item in leaders), ""
            if command == "敌人" and name in ("战争回响", "回响", "本周"):
                current = self._current_week(endgame)
                if not current:
                    return "text", "当前没有开放中的战争回响轮换。", ""
                groups = [endgame["tower"][key] for key in current[1]["groups"] if key in endgame["tower"]]
                icons = await self._foe_icons(groups)
                html = stages.enemies_html(ef_theme, "Arknights: Endfield · War Echoes", "机制与敌人 · <em>本期战争回响</em>", f"<span>{escape(current[0]['name'])} · {escape(current[1]['name'] or '轮换')}</span>", groups, difficulty, icons, version)
                return "image", await self._render(html), ""
            if command == "敌人" and name in ("影拓丰碑", "丰碑"):
                series = next((item for item in endgame.get("monument", []) if stages.live(item, time.time())), None)
                if not series:
                    return "text", "当前没有开放中的影拓丰碑。", ""
                groups = series["groups"]
                icons = await self._foe_icons(groups)
                html = stages.enemies_html(ef_theme, "Arknights: Endfield · Monument", escape(series["name"]), "<span>影拓丰碑 · 全部关卡</span>", groups, difficulty, icons, version)
                return "image", await self._render(html), ""
            if command == "敌人" and name:
                leaders = [item for item in endgame.get("leaders", []) if normalize(name) == normalize(item["name"]) or normalize(name) in normalize(item["name"])]
                if len(leaders) == 1:
                    leader = leaders[0]
                    icon = await self.wiki._cached_image(wiki_data.enemy_icon_url(leader["id"]), f"{leader['id']}.png")
                    html = stages.leader_html(ef_theme, leader, icon, version)
                    return "image", await self._render(html), ""
                if len(leaders) > 1:
                    return "text", f"找到多个领袖敌人：{'、'.join(dict.fromkeys(item['name'] for item in leaders))}\n请输入更完整的名字", ""
            if not name:
                return "text", f"用法：/{command} 关卡名 [难度]，例如 /{command} 重伤之围 残酷。", ""
            found = self._resolve(endgame, name.removesuffix("丰碑") or name)
            if not found:
                return "miss", f"没有找到「{name}」，发送 /战争回响 或 /影拓丰碑 查看名称。", ""
            if found[0] == "candidates":
                return "text", f"找到多个关卡：{'、'.join(found[1])}\n请输入更完整的名字", ""
            if found[0] == "series":
                if command == "攻略":
                    if number and 1 <= number <= len(found[1]["groups"]):
                        group = found[1]["groups"][number - 1]
                    else:
                        return "text", f"影拓丰碑【{found[1]['name']}】包含：" + "、".join(f"{item['no']}.{item['name']}" for item in found[1]["groups"]) + f"\n请发送 /攻略 关卡名 苦难 或 /攻略 {found[1]['name']} 序号。", ""
                else:
                    groups = found[1]["groups"]
                    icons = await self._foe_icons(groups)
                    html = stages.enemies_html(ef_theme, "Arknights: Endfield · Monument", escape(found[1]["name"]), "<span>全部关卡</span>", groups, difficulty, icons, version)
                    return "image", await self._render(html), ""
            else:
                group = found[1]
            available = [stage for stage in group["stages"] if difficulty in (None, stage["difficulty"])]
            if not available:
                return "text", f"「{group['name']}」没有 {difficulty} 难度资料。", ""
            stage = available[-1]
            actual_difficulty = stage["difficulty"]
            icons = await self._foe_icons([group])
            title = f"关卡资料 · <em>{escape(group['name'])}</em>"
            chips = f"<span>{escape(group['mode'])}</span><span>{escape(actual_difficulty)} · Lv{stage['recommend_lv']}</span>"
            html = stages.enemies_html(ef_theme, "Arknights: Endfield · Guide", title, chips, [group], actual_difficulty, icons, version)
            followup = ""
            if command == "攻略":
                pool, names, clear_data, operators, group_id = await self._member_data(event)
                group_id = event.get_group_id() if event is not None else ""
                stats = echoes.team_stats(clear_data, pool, f"{group['name']}|{actual_difficulty}")
                if stats["teams"]:
                    rows = []
                    for rank, team in enumerate(stats["teams"], 1):
                        names = [operators.get(char["id"], {}).get("name", char["id"]) for char in team["chars"]]
                        rows.append(f"{rank}. {' / '.join(names)}：{team['count']} 人使用")
                    target = "本群" if group_id else "你的"
                    followup = f"{target}已记录 {stats['clears']} 人通关 {actual_difficulty}，常见阵容：\n" + "\n".join(rows)
                videos, stale = await self.bili.videos(group["name"], ("残酷", "增辉") if actual_difficulty == "残酷" else ("苦难",) if actual_difficulty == "苦难" else ())
                if videos:
                    note = "（B站暂时无法访问，显示缓存结果）\n" if stale else ""
                    video_text = "推荐视频（点击链接观看）\n" + note + "\n".join(f"{i}. {video.title[:40]}\nhttps://www.bilibili.com/video/{video.bvid}" for i, video in enumerate(videos, 1))
                    followup = f"{followup}\n\n{video_text}" if followup else video_text
            return "image", await self._render(html), followup
        return "miss", "", ""
