"""Guide image renderer adapted from Billtranscend/Priestess-bot (MIT).

Original: plugins/endfield_guide/__init__.py at 59e2be8.
Only AstrBot service dependencies are injected; original presentation is retained.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from html import escape

from .. import ef_theme
from ..endfield_wiki import data as wiki_data
from . import stages

CN = timezone(timedelta(hours=8))
WIDTH = stages.WIDTH

def _last_seen(service, endgame: dict, group_id: str) -> str:
    seen = ""
    for season in endgame.get("tower_seasons", []):
        for week in season["weeks"]:
            start = service._parse_time(week["open"])
            if group_id in week["groups"] and start and start <= datetime.now(CN):
                seen = f"{season['name']}·{week['name'] or '轮换' + str(week['week'])}"
    return seen


async def _enemy_table(wiki, enemies: list[dict]) -> str:
    if not enemies or "hp" not in enemies[0]:  # index built before enemy stats were added
        items = "".join(f'<li>{escape(e["name"])}<small>Lv{e["level"]}</small></li>' for e in enemies)
        return _box("敌人", f"<ul>{items}</ul>")
    same_def = len({e.get("def") for e in enemies}) == 1
    poise = all("poise" in e for e in enemies)  # index built before the bonus lines were added
    icons = await asyncio.gather(
        *(wiki._cached_image(wiki_data.enemy_icon_url(e["icon"]), f"{e['icon']}.png") if e.get("icon") else asyncio.sleep(0, "") for e in enemies)
    )
    rows = []
    for e, icon in zip(enemies, icons):
        thumb = f'<img src="{escape(icon)}">' if icon else '<i class="ph"></i>'
        notes = stages.notes_html(e)
        rows.append(
            f'<tr><td class="ei"{" rowspan=2" if notes else ""}>{thumb}</td><td class="fe">{escape(e["name"])}{" *" if e.get("plain") else ""}</td>'
            f'<td class="lv">Lv{e["level"]}</td><td class="num">{e.get("hp", 0):,}</td>'
            f'<td class="num">{e.get("atk", 0):,}</td>' + ("" if same_def else f'<td class="num">{e.get("def", 0):,}</td>')
            + (f'<td class="num">{e["poise"]:,}</td>' if poise else "")
            + f'<td class="rs">{stages.res_html(e)}</td></tr>'
            + (f'<tr class="bn"><td colspan="{(4 if same_def else 5) + poise + 1}">{notes}</td></tr>' if notes else "")
        )
    head = (
        "<th></th><th>敌人</th><th>等级</th><th class=\"num\">生命值</th><th class=\"num\">攻击力</th>"
        + ("" if same_def else "<th class=\"num\">防御力</th>")
        + ("<th class=\"num\">失衡值上限</th>" if poise else "")
        + "<th>抗性</th>"
    )
    note = f"防御力均为 {enemies[0].get('def', 0)}；" if same_def else ""
    plain = "；带 * 的敌人不在关卡的固定刷怪配置中（多为战斗中召唤），按其自身属性显示" if any(e.get("plain") for e in enemies) else ""
    return _box(
        "敌人数值",
        f'<table class="foe"><tr>{head}</tr>{"".join(rows)}</table>'
        f'<div class="note">{note}数值为敌人在本关卡内的最终属性，已包含每行下方列出的出生加成（该敌人自带）和关卡加成，未计入上方的特殊增益；抗性越高，受到该类伤害越少{plain}</div>',
        f"{len(enemies)} 种",
    )


GUIDE_CSS = """
body { width: %dpx; padding: 24px; }
.ef-head h1 { font-size: 46px; }
.ef-head p { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; color: #b9b9b2; }
.ef-head p span { font-size: 15px; padding: 3px 12px; border: 1px solid #55554f; color: #b9b9b2; }
.ef-head p span i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 17px; letter-spacing: .3px; }
.ef-head p span.on { background: var(--ef-yellow); border-color: var(--ef-yellow); color: var(--ef-ink); font-weight: 700; }
.ef-head p span.live { border-color: var(--ef-yellow); color: var(--ef-yellow); }
.ef-head p span.past { border: 0; padding-left: 4px; color: #8d8d86; }
.bd { padding: 12px 16px 14px; }
.desc { font-size: 16px; line-height: 1.8; }
.dim { color: var(--ef-faint); }
.up { font-family: var(--ef-num); font-weight: 600; font-size: 17px; padding: 0 4px; margin: 0 1px; background: var(--ef-ink); color: var(--ef-yellow); }
.down { font-weight: 700; color: var(--ef-red); }
.key { font-weight: 700; color: var(--ef-ink); box-shadow: inset 0 -.45em 0 var(--ef-yellow); }
.term { font-weight: 700; color: var(--ef-ink); border-bottom: 1px dashed var(--ef-ink); }
.info { color: var(--ef-sub); }
ul { list-style: none; display: flex; flex-wrap: wrap; gap: 8px; }
li { background: #fff; border: 1px solid var(--ef-line); padding: 4px 10px; font-size: 15px; }
li small { color: var(--ef-sub); margin-left: 6px; font-family: var(--ef-num); font-size: 15px; }
.note { font-size: 13px; color: var(--ef-sub); margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--ef-line-2); }
table { width: 100%%; border-collapse: collapse; font-size: 15px; }
th { text-align: left; padding: 4px 8px 6px; font-size: 13px; font-weight: 400; color: var(--ef-sub); border-bottom: 1px solid var(--ef-ink); white-space: nowrap; }
td { text-align: left; padding: 7px 8px; border-top: 1px solid var(--ef-line-2); white-space: nowrap; vertical-align: middle; }
tr:nth-child(2) td { border-top: 0; }
th.n, td.n, th.num, td.num { text-align: right; }
td.n, td.num, td.lv { font-family: var(--ef-num); font-weight: 600; font-size: 18px; letter-spacing: .3px; }
table.foe { font-size: 16px; } table.foe td.fe { font-weight: 700; }
table.foe td.ei { width: 66px; padding: 5px 8px 5px 0; }
table.foe td.ei img, table.foe td.ei .ph { display: block; width: 54px; height: 54px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
table.foe td.rs { white-space: normal; color: var(--ef-sub); font-size: 14px; }
table.foe td.rs b { font-family: var(--ef-num); font-weight: 700; font-size: 17px; color: var(--ef-ink); }
table.vid td.vt { white-space: normal; width: 60%%; line-height: 1.5; } table.vid td.au { color: var(--ef-sub); }
.team { display: flex; align-items: center; gap: 16px; padding: 12px 0; border-top: 1px solid var(--ef-line-2); }
.team:first-child { border-top: 0; padding-top: 4px; }
.tn { width: 190px; flex: none; font-weight: 900; font-size: 18px; }
.tn i { font-style: normal; display: inline-block; min-width: 26px; height: 26px; line-height: 26px; text-align: center; margin-left: 4px;
  font-family: var(--ef-num); font-weight: 700; font-size: 18px; background: var(--ef-ink); color: #f4f4f0; }
.team.t1 .tn i { background: var(--ef-yellow); color: var(--ef-ink); box-shadow: 0 0 0 1px var(--ef-ink); }
.team.t2 .tn i { background: var(--ef-silver); color: var(--ef-ink); box-shadow: 0 0 0 1px var(--ef-ink); }
.team.t3 .tn i { background: var(--ef-copper); color: #fff; box-shadow: 0 0 0 1px var(--ef-ink); }
.tn small { display: block; margin-top: 6px; font-size: 13px; color: var(--ef-sub); font-weight: 400; line-height: 1.5; }
.ops { display: flex; gap: 12px; }
.op { width: 120px; text-align: center; }
.op img, .op .ph { width: 74px; height: 74px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); border-bottom: 3px solid var(--ef-ink); margin: 0 auto; display: block; }
.op b { display: block; font-size: 15px; margin-top: 5px; }
.op small { font-family: var(--ef-num); font-weight: 500; font-size: 14px; color: var(--ef-sub); }
"""


def _box(title: str, body: str, small: str = "") -> str:
    """One light section panel (ef_theme .ef-sec) with padded content."""
    note = f'<small class="cjk">{small}</small>' if small else ""
    return f'<div class="ef-sec"><h3>{title}{note}</h3><div class="bd">{body}</div></div>'


async def render_guide(service, endgame, group, stage, difficulty, clears, target_text, videos, stale, in_group) -> str:
    wiki = service.wiki
    index = wiki.resolver()[0]
    operators = index["operators"]
    is_tower = group["mode"] == "战争回响"
    tabs = "".join(f'<span class="{"on" if s["difficulty"] == difficulty else ""}">{s["difficulty"]} · <i>Lv{s["recommend_lv"]}</i></span>' for s in group["stages"])

    status = ""
    if is_tower:
        current = service._current_week(endgame)
        if current and group["id"] in current[1]["groups"]:
            end = service._parse_time(current[1]["close"])
            status = f'<span class="live">本周开放中 · <i>{end:%m/%d %H:%M}</i> 轮换</span>'
        elif seen := _last_seen(service, endgame, group["id"]):
            status = f'<span class="past">上次出现：{escape(seen)}</span>'
    else:
        number = f" · 第{group['no']}关" if group.get("no") else ""
        status = f'<span class="past">系列：{escape(group["series"])}{number}</span>'

    enemy_block = await _enemy_table(wiki, stage["enemies"])
    extra = ""
    if stage["special_buff"]:
        extra += _box("特殊增益", f'<div class="desc">{stage["special_buff"]}</div>')
    if target_text:
        extra += _box("增辉条件", f'<div class="desc">{escape(target_text)}</div>')
    elif is_tower and difficulty == "残酷":
        extra += _box("增辉条件", '<div class="desc dim">残酷难度完成附加目标（多为限时通关）即可增辉</div>')
    if not is_tower and difficulty == "苦难":
        extra += _box("镀层", '<div class="desc dim">通关该系列苦难模式的全部关卡后，即可为该系列蚀刻章镀层</div>')

    team_block = _box("本群通关阵容" if in_group else "你的通关阵容", '<div class="desc dim">暂无群友的通关记录（绑定森空岛后，数据每天 05:30、17:30 起陆续更新）</div>')
    if clears and clears["teams"]:
        rows = []
        for i, team in enumerate(clears["teams"], 1):
            cells = []
            for c in team["chars"]:
                op = operators.get(c["id"], {})
                icon = await wiki._cached_image(wiki_data.char_icon_url(c["id"]), f"chr_{c['id']}.png") if op else ""
                img = f'<img src="{escape(icon)}">' if icon else '<div class="ph"></div>'
                cells.append(f'<div class="op">{img}<b>{escape(op.get("name", c["id"]))}</b><small>Lv{c["lv"]} · 潜能{c["pot"]:g}</small></div>')
            plus = f" · 其中 {team['plus']} 人增辉" if difficulty == "残酷" else ""
            rows.append(f'<div class="team t{i}"><div class="tn">阵容 <i>{i}</i><small>{team["count"]} 人使用{plus}</small></div><div class="ops">{"".join(cells)}</div></div>')
        scope = "本群" if in_group else "你"
        team_block = _box(
            f"{scope}通关阵容",
            f'{"".join(rows)}<div class="note">等级 / 潜能为使用该阵容群友的平均值；数据每天 05:30、17:30 起陆续更新，不含武器与装备</div>',
            f'{clears["clears"]} 人通关{difficulty}' + (f'，{clears["plus"]} 人增辉' if difficulty == "残酷" else ""),
        )

    if videos:
        video_rows = "".join(
            f'<tr><td class="vt">{escape(v.title)}</td><td class="au">{escape(v.author)}</td><td class="n">{v.play:,}</td><td class="n">{escape(v.duration)}</td>'
            f'<td class="n">{datetime.fromtimestamp(v.pubdate, CN):%m/%d}</td></tr>'
            for v in videos
        )
        stale_note = "（B站暂时无法访问，显示的是上次结果）" if stale else ""
        video_block = _box(
            "推荐视频",
            f'<table class="vid"><tr><th>标题</th><th>UP主</th><th class="n">播放</th><th class="n">时长</th><th class="n">发布</th></tr>{video_rows}</table>',
            f"按热度与发布时间排序{stale_note}，链接见下一条消息",
        )
    else:
        video_block = _box("推荐视频", '<div class="desc dim">暂时没有找到标题含该关卡名的视频</div>')

    mechanics = _box(
        "关卡机制",
        '<div class="desc">' + (stage["feature"] or '<span class="dim">无特殊机制说明</span>') + "</div>",
        f'{escape(difficulty)} · 推荐等级 {stage["recommend_lv"]}',
    )
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{GUIDE_CSS % WIDTH}{stages.NOTES_CSS}</style></head><body class="ef">'
        + ef_theme.head(f'Arknights: Endfield · {escape(group["mode"])}', escape(group["name"]), f"{tabs}{status}")
        + f"{mechanics}{enemy_block}{extra}{team_block}{video_block}"
        + ef_theme.foot(
            f'关卡数据 AKEData · 阵容 森空岛战绩 · 视频 哔哩哔哩<br>数据版本 <span class="ef-num">{escape(index.get("version", ""))}</span>',
            notes=stages.ENEMY_NOTES,
            title="数值怎么读",
        )
        + "</body></html>"
    )
