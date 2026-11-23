"""Sunday 19:00 image reports, with independent game groups and personal opt-outs.

Only verified unfinished Skland progress produces real QQ mentions. All explanations
and opt-out commands are inside the image. Per-week receipts survive plugin reloads.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import re
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import httpx
from PIL import Image
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from . import ef_theme
from .endfield_guide.echoes import speed_ranking
from .guide import CN
from ..notifications import notification_enabled
from ..api import SklandAPI
from ..db import SkUser, get_default_ark_character, get_default_ef_character, get_session, get_user_by_platform
from ..schemas import CRED
from ..utils import call_api_with_refresh
from .weekly_preferences import ALIASES, GAMES, WeeklyPreferences
from sqlalchemy import select

WIDTH = 1000
SEND_GAP = 6.0
ESSENCE_LIMIT = 8
IMAGE_LIMIT = 10 * 1024 * 1024
AT_ALL = False  # @全体成员 with the scheduled Sunday report; off at the owner's request since 2026-10-18
REQUEST_GAP = 1.0
MENTIONS_PER_IMAGE = 20


# ── helpers ─────────────────────────────────────────────────────────────


def _parse(value: str) -> datetime | None:
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        with contextlib.suppress(ValueError, TypeError):
            return datetime.strptime(value, fmt).replace(tzinfo=CN)
    return None


def _left(end: datetime, now: datetime) -> str:
    seconds = max(0, int((end - now).total_seconds()))
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    if days:
        return f"{days} 天 {hours} 小时"
    return f"{hours} 小时 {minutes} 分" if hours else f"{minutes} 分钟"


def _week_of(opened: datetime, moment: datetime) -> int:
    """1-based 每周事务 week number (weeks run from the activity's opening, Monday 04:00)."""
    return int((moment - opened).total_seconds() // (7 * 86400)) + 1


def _next_monday_4am(now: datetime) -> datetime:
    reset = (now + timedelta(days=(7 - now.weekday()) % 7)).replace(hour=4, minute=0, second=0, microsecond=0)
    return reset if reset > now else reset + timedelta(days=7)


def _rotations(endgame: dict) -> list[tuple[dict, dict, datetime, datetime]]:
    rows = []
    for season in endgame.get("tower_seasons", []):
        for week in season["weeks"]:
            start, end = _parse(week["open"]), _parse(week["close"])
            if start and end:
                rows.append((season, week, start, end))
    return sorted(rows, key=lambda row: row[2])


def _rotation_name(season: dict, week: dict) -> str:
    return f"{season['name']} · {week['name'] or '轮换' + str(week['week'])}"


def _stage_names(endgame: dict, week: dict) -> list[str]:
    return [endgame["tower"][g]["name"] for g in week["groups"] if g in endgame["tower"]]


def _card(title: str, body: str, note: str = "") -> str:
    small = f'<small class="cjk">{note}</small>' if note else ""
    return f'<div class="ef-sec"><h3>{title}{small}</h3>{body}</div>'


# ── sections ────────────────────────────────────────────────────────────


def _tasks_section(index: dict, now: datetime, game_key: str = "endfield") -> str:
    rows = []
    reset = _next_monday_4am(now)
    if game_key == "arknights":
        return _card("每周任务提醒", f'<table class="list"><tr><td class="tag ak"><span>明日方舟</span></td><td><b>剿灭作战、周常任务</b><div class="sub">周一 {reset:%m/%d} 04:00 重置，还剩 {_left(reset, now)}</div></td></tr></table>')
    weekly = index.get("calendar", {}).get("weekly") or {}
    opened = _parse(weekly.get("open", ""))
    if opened and weekly.get("weeks") and opened <= now:
        last = max(int(w) for w in weekly["weeks"])
        week = min(last, _week_of(opened, now))
        reset = opened + timedelta(days=7 * week)
        tasks = weekly["weeks"].get(str(week), [])
        version = next(
            (v for v in index.get("calendar", {}).get("versions", []) if (_parse(v["open"]) or now) <= now < (_parse(v["close"]) or now)),
            None,
        )
        if version:  # count weeks from the version start: the reset week containing it is week 1
            first = _week_of(opened, _parse(version["open"]))
            label = f'{escape(version["name"])} 版本第 {week - first + 1} 周（共 {_week_of(opened, _parse(version["close"]) - timedelta(seconds=1)) - first + 1} 周）'
        else:
            label = f"第 {week} 周"
        items = "".join(f'<li>{escape(t["desc"])}<em>{t["score"]} 分</em></li>' for t in tasks)
        goal = max(weekly.get("milestones") or [0])
        total = sum(t["score"] for t in tasks)
        rows.append(
            f'<tr><td class="tag ef"><span>终末地</span></td><td><b>{escape(weekly["name"])} · {label}</b>'
            f'<div class="sub">{reset:%m/%d %H:%M} 重置，还剩 {_left(reset, now)}；共 {total} 分'
            + (f"，满 {goal} 分领取全部里程碑奖励" if goal else "")
            + f'</div><ul class="tasks">{items}</ul></td></tr>'
        )
    for bp in index.get("calendar", {}).get("passes", []):
        start, end = _parse(bp["open"]), _parse(bp["close"])
        if not (start and end and start <= now < end):
            continue
        opened = [w for w in bp["weeks"] if (_parse(w["open"]) or end) <= now]
        upcoming = [w for w in bp["weeks"] if (_parse(w["open"]) or start) > now]
        if opened:
            week = opened[-1]
            tasks = "".join(f"<li>{escape(t)}</li>" for t in week["tasks"])
            nxt = f"第 {upcoming[0]['week']} 周任务 {_parse(upcoming[0]['open']):%m/%d %H:%M} 开放；" if upcoming else ""
            rows.append(
                f'<tr><td class="tag ef"><span>终末地</span></td><td><b>通行证 第 {week["week"]} 周周常</b>'
                f'<div class="sub">{nxt}本期通行证 {end:%m/%d %H:%M} 结束</div><ul class="tasks">{tasks}</ul></td></tr>'
            )
    endgame = index.get("endgame", {})
    for season, week, start, end in _rotations(endgame):
        if start <= now < end:
            rows.append(
                f'<tr><td class="tag ef"><span>终末地</span></td><td><b>战争回响 本期轮换</b>'
                f'<div class="sub">{escape(_rotation_name(season, week))}，{end:%m/%d %H:%M} 轮换，还剩 {_left(end, now)}</div></td></tr>'
            )
    for activity in index.get("calendar", {}).get("activities", []):
        start, end = _parse(activity["open"]), _parse(activity["close"])
        if activity["id"].startswith("activity_high_difficulty") and start and end and start <= now < end:
            rows.append(
                f'<tr><td class="tag ef"><span>终末地</span></td><td><b>{escape(activity["name"])}</b>'
                f'<div class="sub">影拓丰碑限时活动，{end:%m/%d %H:%M} 结束，还剩 {_left(end, now)}</div></td></tr>'
            )
    return _card("每周任务提醒", f'<table class="list">{"".join(rows)}</table>')


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}分{seconds % 60:02d}秒" if seconds >= 60 else f"{seconds}秒"


async def _speed_section(index: dict, now: datetime, pool: set[str], names: dict[str, str], guide) -> str:
    endgame = index.get("endgame", {})
    current = next(((s, w) for s, w, start, end in _rotations(endgame) if start <= now < end), None)
    if not current:
        return _card("本周榜单", '<div class="ef-empty">当前没有开放中的战争回响轮换</div>')
    data = guide.echo_store.load()
    blocks = []
    for stage in _stage_names(endgame, current[1]):
        ranking = speed_ranking(data, pool, f"{stage}|残酷")
        if ranking:
            rows = "".join(
                f'<div class="rk"><i class="m{i}">{i}</i><span class="nm">{escape(names.get(qq, qq))}</span><b>{_fmt_time(rec["time"])}</b></div>'
                for i, (qq, rec) in enumerate(ranking[:3], 1)
            )
            more = f'<div class="sub">共 {len(ranking)} 人有记录</div>'
        else:
            rows, more = '<div class="ef-empty">本群还没有用时记录</div>', ""
        blocks.append(f'<div class="stage"><div class="sn">{escape(stage)}<small>残酷</small></div>{rows}{more}</div>')
    note = "回响竞速前 3 名 · 完整榜单发 /回响竞速"
    return _card("本周榜单", f'<div class="stages">{"".join(blocks)}</div>', note)


def _rotation_section(index: dict, now: datetime) -> str:
    endgame = index.get("endgame", {})
    rotations = _rotations(endgame)
    current = next((r for r in rotations if r[2] <= now < r[3]), None)
    upcoming = next((r for r in rotations if r[2] > now and _stage_names(endgame, r[1])), None)
    rows = []
    if current:
        season, week, _, end = current
        rows.append(
            f'<div class="rot"><div class="rl">本期</div><div><b>{escape(_rotation_name(season, week))}</b>'
            f'<div class="sub">{end:%m/%d %H:%M} 轮换 · 还剩 <em>{_left(end, now)}</em></div>'
            f'<div class="chips">{"".join(f"<span>{escape(n)}</span>" for n in _stage_names(endgame, week))}</div></div></div>'
        )
    if upcoming:
        season, week, start, _ = upcoming
        rows.append(
            f'<div class="rot"><div class="rl next">下期</div><div><b>{escape(_rotation_name(season, week))}</b>'
            f'<div class="sub">{start:%m/%d %H:%M} 开放</div>'
            f'<div class="chips">{"".join(f"<span>{escape(n)}</span>" for n in _stage_names(endgame, week))}</div></div></div>'
        )
    else:
        rows.append('<div class="rot"><div class="rl next">下期</div><div><b>待公布</b><div class="sub">游戏数据更新后自动显示</div></div></div>')
    return _card("战争回响轮换", "".join(rows), "关卡攻略发 /攻略 关卡名")


def _shrink(content: bytes) -> tuple[str, float] | None:
    """JPEG data URI no larger than 900px on the long side, plus the width/height ratio."""
    with Image.open(io.BytesIO(content)) as image:
        image = image.convert("RGB")
        image.thumbnail((900, 900))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        return f"data:image/jpeg;base64,{base64.b64encode(out.getvalue()).decode()}", image.width / image.height


async def _fetch_image(url: str) -> tuple[str, float] | None:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        if len(response.content) > IMAGE_LIMIT:
            return None
        return await asyncio.to_thread(_shrink, response.content)
    except Exception:
        return None


ROW_WIDTH = WIDTH - 52 - 2 - 28  # essence area inside the card
TILE_EXTRA = 26  # tile padding + side borders
TILE_GAP, IMAGE_GAP = 12, 6


def _tile_width(tile: dict, height: float) -> float:
    ratios = sum(r for _, r in tile["images"])
    return ratios * height + IMAGE_GAP * (len(tile["images"]) - 1) + TILE_EXTRA


def _layout(tiles: list[dict]) -> str:
    """Text-only tiles in a two-column grid, then justified rows of image tiles that share one height."""
    texts = [t for t in tiles if not t["images"]]
    grid = "".join(f'<div class="ess">{t["head"]}<div class="ec">{t["text"]}</div></div>' for t in texts)
    html = [f'<div class="essgrid{" one" if len(texts) == 1 else ""}">{grid}</div>'] if texts else []
    rows, row = [], []
    for tile in (t for t in tiles if t["images"]):
        if row and sum(_tile_width(t, 420) for t in row + [tile]) + TILE_GAP * len(row) > ROW_WIDTH:
            rows.append(row)
            row = []
        row.append(tile)
    if row:
        rows.append(row)
    for row in rows:
        ratios = sum(r for t in row for _, r in t["images"])
        fixed = sum(_tile_width(t, 0) for t in row) + TILE_GAP * (len(row) - 1)
        height = max(160, min(680, (ROW_WIDTH - fixed) / ratios))
        cells = []
        for tile in row:
            imgs = "".join(f'<img src="{src}" style="width:{r * height:.0f}px;height:{height:.0f}px">' for src, r in tile["images"])
            style, gallery = f"width:{_tile_width(tile, height):.0f}px", f'<div class="gal">{imgs}</div>'
            text = f'<div class="ec">{tile["text"]}</div>' if tile["text"] else ""
            cells.append(f'<div class="ess" style="{style}">{tile["head"]}{text}{gallery}</div>')
        html.append(f'<div class="essrow">{"".join(cells)}</div>')
    return "".join(html)


async def _essence_section(bot, group_id: int | None, since: datetime, names: dict[str, str]) -> str:
    if group_id is None:
        return ""
    for attempt in range(3):  # large essence lists take NapCat ~10 s and occasionally fail once
        try:
            items = (await bot.call_api("get_essence_msg_list", group_id=group_id)) or []
            break
        except Exception as e:
            if attempt == 2:
                logger.warning(f"Weekly report essence unavailable: {type(e).__name__}")
                return _card("本周群精华", '<div class="ef-empty">精华消息暂时获取失败</div>')
            await asyncio.sleep(5)
    fresh = [
        item for item in items
        if int(item.get("operator_time") or 0) >= since.timestamp() and str(item.get("sender_id")) != str(bot.self_id)
    ]
    fresh.sort(key=lambda item: int(item.get("operator_time") or 0), reverse=True)
    if not fresh:
        return _card("本周群精华", '<div class="ef-empty">本周没有新的精华消息</div>')
    tiles = []
    for item in fresh[:ESSENCE_LIMIT]:
        parts, images = [], []
        for segment in item.get("content") or []:
            kind, data = segment.get("type"), segment.get("data") or {}
            if kind == "text":
                parts.append(escape(data.get("text", "")).replace("\n", "<br>"))
            elif kind == "at":
                parts.append(f'<span class="at">@{escape(names.get(str(data.get("qq")), data.get("name") or "群友"))}</span>')
            elif kind == "image":
                url = data.get("url") or (data.get("file") if str(data.get("file", "")).startswith("http") else "")
                image = await _fetch_image(url) if url else None
                images.append(image) if image else parts.append('<span class="dim">[图片]</span>')
            elif kind == "face":
                parts.append('<span class="dim">[表情]</span>')
            elif kind in ("video", "record", "file", "forward", "json"):
                parts.append(f'<span class="dim">[{ {"video": "视频", "record": "语音", "file": "文件", "forward": "聊天记录", "json": "卡片"}[kind] }]</span>')
        sender = names.get(str(item.get("sender_id")), item.get("sender_nick") or "群友")
        when = datetime.fromtimestamp(int(item.get("operator_time") or 0), CN)
        head = f'<div class="eh"><b>{escape(sender)}</b><span>{when:%m/%d} · {escape(item.get("operator_nick") or "管理员")} 设精</span></div>'
        text = "".join(parts) or ("" if images else '<span class="dim">[无法显示的消息]</span>')
        tiles.append({"head": head, "text": text, "images": images})
    more = f"共 {len(fresh)} 条，显示最近 {ESSENCE_LIMIT} 条" if len(fresh) > ESSENCE_LIMIT else f"共 {len(fresh)} 条"
    return _card("本周群精华", f'<div class="esslist">{_layout(tiles)}</div>', more)


def _events_section(index: dict, now: datetime) -> str:
    horizon = now + timedelta(days=7)
    opening, closing, seen = [], [], set()
    for activity in index.get("calendar", {}).get("activities", []):
        start, end = _parse(activity["open"]), _parse(activity["close"])
        key = (activity["name"], activity["open"])
        if key in seen or not start:
            continue
        seen.add(key)
        if now < start <= horizon:
            opening.append((start, activity["name"], f"{start:%m/%d %H:%M} 开放" + (f"，持续到 {end:%m/%d}" if end else "")))
        elif end and start <= now < end <= horizon:
            closing.append((end, activity["name"], f"{end:%m/%d %H:%M} 结束，还剩 {_left(end, now)}"))
    body = ""
    for title, rows in (("即将开放", sorted(opening)), ("即将结束", sorted(closing))):
        if rows:
            body += f'<div class="ev"><div class="evh">{title}</div>' + "".join(
                f'<div class="evr"><b>{escape(name)}</b><span>{when}</span></div>' for _, name, when in rows
            ) + "</div>"
    if not body:
        body = '<div class="ef-empty">未来 7 天没有活动开放或结束</div>'
    return _card("下周活动简报", body, "终末地 · 数据来自游戏解包，以游戏内公告为准")


def _pools_section(index: dict, now: datetime) -> str:
    rows = []
    for pool in sorted(index.get("calendar", {}).get("pools", []), key=lambda p: (p["kind"] != "char", p["close"])):
        start, end = _parse(pool["open"]), _parse(pool["close"])
        if not (start and end) or end <= now or start > now + timedelta(days=7):
            continue
        kind = "角色" if pool["kind"] == "char" else "武器"
        up = "、".join(pool["up"])
        if start > now:
            status = f'<span class="soon">{start:%m/%d %H:%M} 开放</span>'
        else:
            days = (end - now).total_seconds() / 86400
            status = f'<span class="{"hot" if days < 3 else "ok"}">还剩 {_left(end, now)}</span><small>{end:%m/%d %H:%M} 结束</small>'
        rows.append(f'<tr><td class="tag {"ef" if kind == "角色" else "wp"}"><span>{kind}</span></td><td><b>{escape(pool["name"])}</b><div class="sub">UP：{escape(up)}</div></td><td class="pl">{status}</td></tr>')
    body = f'<table class="list">{"".join(rows)}</table>' if rows else '<div class="ef-empty">没有查到当前开放的限定卡池（游戏数据可能尚未更新）</div>'
    return _card("卡池倒计时", body, "终末地")


# ── page ────────────────────────────────────────────────────────────────

CSS = """
body { width: %dpx; padding: 26px; }
.sub { font-size: 15px; color: var(--ef-sub); margin-top: 3px; } .sub em { color: var(--ef-ink); font-style: normal; font-weight: 700; background: var(--ef-yellow); padding: 0 4px; }
table.list { width: 100%%; border-collapse: collapse; }
table.list td { padding: 11px 14px; border-top: 1px solid var(--ef-line-2); font-size: 18px; vertical-align: top; }
table.list tr:first-child td { border-top: 0; }
td.tag { width: 96px; white-space: nowrap; padding-top: 13px; }
td.tag span { display: inline-block; font-size: 13px; padding: 1px 7px; border: 1px solid var(--ef-ink); }
td.tag.ef span { background: var(--ef-yellow); } td.tag.ak span { background: var(--ef-ink); color: #f4f4f0; } td.tag.wp span { background: var(--ef-panel-2); }
ul.tasks { list-style: none; margin-top: 8px; display: grid; grid-template-columns: 1fr 1fr; gap: 4px 18px; }
ul.tasks li { font-size: 14px; color: #3c3c38; padding-left: 13px; position: relative; }
ul.tasks li em { white-space: nowrap; font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 14px; margin-left: 6px; padding: 0 4px; background: var(--ef-ink); color: var(--ef-yellow); }
ul.tasks li:before { content: ""; position: absolute; left: 0; top: 7px; width: 6px; height: 6px; background: var(--ef-ink); }
td.pl { width: 240px; text-align: right; white-space: nowrap; }
td.pl span { font-weight: 700; } td.pl small { display: block; font-size: 13px; color: var(--ef-sub); margin-top: 3px; }
.ok { color: var(--ef-ink); background: var(--ef-yellow); padding: 0 6px; } .hot { color: #fff; background: var(--ef-red); padding: 0 6px; } .soon { color: var(--ef-blue); }
.stages { display: grid; grid-template-columns: repeat(3, 1fr); }
.stage { padding: 12px 16px; border-left: 1px solid var(--ef-line-2); } .stage:first-child { border-left: 0; }
.sn { font-size: 19px; font-weight: 900; margin-bottom: 8px; } .sn small { font-size: 13px; color: var(--ef-sub); margin-left: 6px; font-weight: 400; }
.rk { display: flex; align-items: center; gap: 9px; padding: 4px 0; font-size: 16px; }
.rk i { font-style: normal; flex: none; width: 24px; height: 24px; line-height: 24px; text-align: center; font-family: var(--ef-num); font-weight: 700; font-size: 16px; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
.rk i.m1 { background: var(--ef-yellow); border-color: var(--ef-ink); } .rk i.m2 { background: var(--ef-silver); color: var(--ef-ink); border-color: var(--ef-ink); } .rk i.m3 { background: var(--ef-copper); color: #fff; border-color: var(--ef-ink); }
.rk .nm { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; } .rk b { font-family: var(--ef-num); font-weight: 700; font-size: 18px; }
.rot { display: flex; gap: 14px; padding: 12px 16px; border-top: 1px solid var(--ef-line-2); font-size: 18px; } .rot:first-of-type { border-top: 0; }
.rl { width: 56px; height: 28px; line-height: 28px; text-align: center; background: var(--ef-yellow); border: 1px solid var(--ef-ink); font-size: 15px; font-weight: 700; flex: none; }
.rl.next { background: var(--ef-panel-2); border-color: var(--ef-line); color: var(--ef-sub); }
.chips { margin-top: 8px; display: flex; gap: 8px; flex-wrap: wrap; }
.chips span { font-size: 15px; padding: 3px 12px; border: 1px solid var(--ef-ink); background: #fff; }
.esslist { padding: 14px 14px 2px; }
.essgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; } .essgrid.one { grid-template-columns: 1fr; }
.essrow { display: flex; gap: 12px; margin-bottom: 12px; justify-content: center; align-items: flex-start; }
.ess { padding: 10px 12px; background: #fff; border: 1px solid var(--ef-line); border-top: 3px solid var(--ef-ink); min-width: 0; }
.gal { display: flex; gap: 6px; margin-top: 8px; } .gal img { display: block; object-fit: cover; flex: none; border: 1px solid var(--ef-line-2); }
.eh { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; } .eh b { font-size: 17px; } .eh span { font-size: 13px; color: var(--ef-sub); white-space: nowrap; }
.ec { margin-top: 6px; font-size: 17px; line-height: 1.6; word-break: break-all; }
.at { color: var(--ef-blue); } .dim { color: var(--ef-faint); }
.ev { padding: 10px 16px; border-top: 1px solid var(--ef-line-2); } .ev:first-of-type { border-top: 0; }
.evh { display: inline-block; font-size: 13px; padding: 0 8px; margin-bottom: 6px; background: var(--ef-ink); color: #f4f4f0; }
.evr { display: flex; justify-content: space-between; padding: 4px 0; font-size: 18px; } .evr span { color: var(--ef-sub); font-size: 16px; }
"""


def _pending_section(game: str, pending: list[dict]) -> str:
    label = "剿灭合成玉奖励未领满" if game == "arknights" else "每周事务未完成"
    command = "粥" if game == "arknights" else "终"
    rows = "".join(f'<tr><td>{escape(row["name"])}</td><td>{row["current"]} / {row["total"]}</td></tr>' for row in pending)
    body = f'<table class="list">{rows}</table>' if rows else '<div class="ef-empty">本次无已确认未完成且允许 @ 的成员</div>'
    return _card(label, body + f'<div class="sub">以本次森空岛进度为准，查询失败或数据缺失时不 @。<br>取消本游戏 @：<b>/周常提醒 {command} 关</b>；恢复：/周常提醒 {command} 开。<br>关闭两款游戏：/周常提醒 关。仅影响本人 @，群周报图片仍正常发送。</div>')


def _ark_calendar_sections(now: datetime) -> str:
    from .game_calendar.calendar import ARKNIGHTS
    data = ARKNIGHTS.load()
    horizon = now + timedelta(days=7)
    blocks = []
    for key, title in (("activities", "方舟活动"), ("pools", "方舟卡池")):
        rows = []
        for item in data[key]:
            start, end = item["start"], item.get("end")
            if start > horizon or (end and end <= now):
                continue
            status = f'{start:%m/%d %H:%M} 开启' if start > now else (f'{end:%m/%d %H:%M} 结束 · 还剩 {_left(end, now)}' if end else "进行中")
            rows.append(f'<div class="evr"><b>{escape(item["name"])}</b><span>{status}</span></div>')
        blocks.append(_card(title, "".join(rows) or '<div class="ef-empty">暂无可用数据</div>'))
    return "".join(blocks)


async def build_pending_image(game: str, pending: list[dict]) -> bytes:
    html = f'<!doctype html><meta charset="utf-8"><style>{ef_theme.css()}{CSS % WIDTH}</style><body class="ef">' + ef_theme.head("Weekly Reminder", escape(GAMES[game]) + " · 周日任务提醒") + _pending_section(game, pending) + "</body>"
    return await ef_theme.render_page(html, WIDTH, height=400)


async def build_report(plugin, bot, group_id: int | None, viewer: str | None = None, *, game_key: str = "endfield", pending: list[dict] | None = None) -> bytes:
    now = datetime.now(CN)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    loaded = plugin.wiki.resolver()
    index = loaded[0] if loaded else {}
    if group_id is not None:
        members = await bot.get_group_member_list(group_id=group_id)
        names = {str(m["user_id"]): m.get("card") or m.get("nickname") or "群友" for m in members}
    else:
        names = {viewer: "你"} if viewer else {}
    pool = set(names) - plugin.guide.echo_store.optout()
    sections = [await _essence_section(bot, group_id, week_start, names), _tasks_section(index, now, game_key)]
    if game_key == "arknights":
        sections.append(_ark_calendar_sections(now))
    else:
        sections.extend([await _speed_section(index, now, pool, names, plugin.guide), _rotation_section(index, now), _events_section(index, now), _pools_section(index, now)])
    if pending is not None:
        sections.append(_pending_section(game_key, pending))
    else:
        sections.append(_card("周常 @ 设置", f'<div class="sub">取消本人 @：/周常提醒 {"粥" if game_key == "arknights" else "终"} 关；恢复请把关改成开。预览不会 @ 任何成员。</div>'))
    week_end = week_start + timedelta(days=6)
    html = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS % WIDTH}</style></head><body class="ef">'
        + ef_theme.head(
            ("Arknights" if game_key == "arknights" else "Endfield") + " · Weekly Report",
            escape(GAMES[game_key]) + " <em>周报</em>",
            f"{week_start:%Y/%m/%d} – {week_end:%m/%d} · 本周回顾与下周预告",
            f'WEEK<b>{week_start:%y}-W{week_start.isocalendar().week:02d}</b>',
        )
        + "".join(sections)
        + ef_theme.foot(('活动与卡池：ArknightsGameResource / PRTS Wiki' if game_key == "arknights" else f'关卡、活动与卡池：AKEData（版本 {escape(index.get("version", ""))}）；竞速记录：森空岛') + '<br>每周日 19:00 发送 · 全部指令发 /skl帮助')
        + "</body></html>"
    )
    return await ef_theme.render_page(html, WIDTH, height=600, template_path=plugin.wiki.data_dir.resolve().as_uri())


class _OneBotClient:
    def __init__(self, client, platform_id: str) -> None:
        self.client = client
        self.platform_id = platform_id
        self.self_id = str(getattr(client, "self_id", ""))

    async def call_api(self, action: str, **kwargs):
        result = await self.client.call_action(action, **kwargs)
        if isinstance(result, dict) and (result.get("status") == "failed" or result.get("retcode", 0) not in (0, None)):
            raise RuntimeError("OneBot action rejected")
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    async def get_group_list(self):
        return await self.call_api("get_group_list")

    async def get_group_member_list(self, group_id: int):
        return await self.call_api("get_group_member_list", group_id=int(group_id))

    async def get_group_member_info(self, group_id: int, user_id: int, **kwargs):
        return await self.call_api("get_group_member_info", group_id=int(group_id), user_id=int(user_id), **kwargs)

    async def send_group_msg(self, group_id: int, image: bytes, at_all: bool = False):
        import base64

        message = []
        if at_all:
            message.append({"type": "at", "data": {"qq": "all"}})
            message.append({"type": "text", "data": {"text": " "}})
        message.append({"type": "image", "data": {"file": "base64://" + base64.b64encode(image).decode("ascii")}})
        return await self.call_api("send_group_msg", group_id=int(group_id), message=message)

    async def send_weekly_image(self, group_id: int, image: bytes, mentions: list[str]):
        # Actual QQ mentions accompany the image; all explanations stay inside it.
        message = [{"type": "at", "data": {"qq": qq}} for qq in mentions]
        message.append({"type": "image", "data": {"file": "base64://" + base64.b64encode(image).decode("ascii")}})
        return await self.call_api("send_group_msg", group_id=int(group_id), message=message)


def _report_groups(config_file: Path) -> set[int] | None:
    try:
        return {int(group) for group in json.loads(config_file.read_text("utf-8")).get("groups", [])}
    except FileNotFoundError:
        return None


def _current_bot(plugin, platform_id: str | None = None) -> _OneBotClient | None:
    platform = plugin.context.get_platform_inst(platform_id) if platform_id else None
    if platform is None:
        platform = next((item for item in plugin.context.platform_manager.get_insts() if item.meta().name == "aiocqhttp"), None)
    if platform is None or not hasattr(platform, "get_client"):
        return None
    return _OneBotClient(platform.get_client(), platform.meta().id)


def _load_admin_groups(admin_file: Path) -> dict[str, str]:
    try:
        return json.loads(admin_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


async def _bot_role(bot: _OneBotClient, group_id: int) -> str:
    info = await bot.get_group_member_info(group_id=group_id, user_id=int(bot.self_id), no_cache=True)
    return info.get("role") or "member"


async def refresh_admin_groups(bot: _OneBotClient, admin_file: Path) -> dict[str, str]:
    previous, current = _load_admin_groups(admin_file), {}
    for group in await bot.get_group_list():
        try:
            role = await _bot_role(bot, group["group_id"])
        except Exception as error:
            logger.warning("Weekly report admin check failed for a group: %s", type(error).__name__)
            role = previous.get(str(group["group_id"]), "member")
        if role in ("admin", "owner"):
            current[str(group["group_id"])] = role
    admin_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = admin_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(current), "utf-8")
    temporary.chmod(0o600)
    temporary.replace(admin_file)
    logger.info("Weekly report admin groups: %s", len(current))
    return current


async def can_at_all(bot: _OneBotClient, group_id: int, admin_file: Path) -> bool:
    try:
        admin = await _bot_role(bot, group_id) in ("admin", "owner")
    except Exception:
        admin = str(group_id) in _load_admin_groups(admin_file)
    if not admin:
        return False
    try:
        remain = await bot.call_api("get_group_at_all_remain", group_id=int(group_id))
        return bool(remain.get("can_at_all")) and min(remain.get("remain_at_all_count_for_group", 1), remain.get("remain_at_all_count_for_uin", 1)) > 0
    except Exception:
        return True


async def _send_report(bot: _OneBotClient, group_id: int, image: bytes, at_all: bool, admin_file: Path) -> bool:
    if at_all and await can_at_all(bot, group_id, admin_file):
        try:
            await bot.send_group_msg(group_id, image, at_all=True)
            return True
        except Exception as error:
            logger.warning("Weekly report @all failed; sending image only: %s", type(error).__name__)
    await bot.send_group_msg(group_id, image)
    return False


async def send_all(plugin, bot: _OneBotClient, admin_file: Path, config_file: Path, at_all: bool = False) -> dict[str, int]:
    return await plugin.weekly_report.send_for_bot(bot)


class WeeklyReport:
    def __init__(self, plugin, data_dir: Path) -> None:
        self.plugin = plugin
        self.data_dir = data_dir / "weekly_report"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.admin_file = self.data_dir / "admin_groups.json"
        self.config_file = self.data_dir / "config.json"
        self.delivery_file = self.data_dir / "delivery.json"
        self.preferences = WeeklyPreferences(self.data_dir / "mention_preferences.json")
        self._send_lock = asyncio.Lock()
        self.now = lambda: datetime.now(CN)
        self._last_admin_day: date | None = None
        self._last_weekly_day: date | None = None

    async def preview(self, event: AstrMessageEvent, game: str = "") -> str:
        game_key = ALIASES.get(game.lower(), "endfield" if not game else "")
        if not game_key:
            raise ValueError("用法：/周报预览 粥 或 /周报预览 终")
        bot = _current_bot(self.plugin, event.get_platform_id())
        if bot is None:
            raise RuntimeError("未连接 OneBot v11 平台")
        group_id = int(event.get_group_id()) if event.get_group_id() else None
        image = await build_report(self.plugin, bot, group_id, event.get_sender_id(), game_key=game_key)
        path = self.data_dir / f"preview-{event.get_sender_id()}.png"
        path.write_bytes(image)
        return str(path.resolve())

    def groups(self, game: str) -> set[str]:
        raw = str(self.plugin.plugin_config.get(f"weekly_report_{game}_groups", ""))
        return {part for part in re.split(r"[,，;；\s]+", raw) if part.isdigit() and len(part) <= 12 and int(part) > 0}

    def enabled(self, game: str) -> bool:
        return notification_enabled(self.plugin, "weekly_report") and notification_enabled(self.plugin, f"weekly_report_{game}")

    def _state(self, now: datetime) -> dict:
        week = (now - timedelta(days=now.weekday())).date().isoformat()
        try:
            state = json.loads(self.delivery_file.read_text("utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("deliveries"), dict):
                raise ValueError("invalid weekly delivery state")
        except FileNotFoundError:
            state = {}
        return state if state.get("week") == week else {"week": week, "deliveries": {}}

    def _save_state(self, state: dict) -> None:
        temporary = self.delivery_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
        temporary.replace(self.delivery_file)

    async def _fetch_progress(self, qq: str, game: str) -> tuple[int, int] | None:
        async with await get_session() as session:
            user = await get_user_by_platform(session, qq)
            if user is None:
                return None
            char = await (get_default_ark_character(session, user) if game == "arknights" else get_default_ef_character(session, user))
            if char is None:
                return None
            cred = CRED(cred=user.cred, token=user.cred_token)
            if game == "arknights":
                card = await asyncio.wait_for(call_api_with_refresh(user, SklandAPI.ark_card, cred, str(char.uid)), timeout=30)
                current, total = card.campaign.reward.current, card.campaign.reward.total
            else:
                card = await asyncio.wait_for(call_api_with_refresh(user, SklandAPI.endfield_card, cred, user.user_id or user.platform_user_id, char), timeout=30)
                current, total = card.weeklyMission.score, card.weeklyMission.total
            await session.commit()
            # A default/missing total must never become an "unfinished" judgment.
            if not isinstance(current, int) or not isinstance(total, int) or current < 0 or total <= 0:
                return None
            return current, total

    async def _pending(self, bot, group_id: str, game: str, cache: dict, handled: set[str]) -> tuple[list[dict], bool]:
        if not notification_enabled(self.plugin, f"weekly_mentions_{game}"):
            return [], False
        members = await bot.get_group_member_list(group_id=int(group_id))
        names = {str(m["user_id"]): str(m.get("card") or m.get("nickname") or m["user_id"]) for m in members if str(m["user_id"]).isdigit()}
        candidates = [qq for qq in names if qq != getattr(bot, "self_id", "") and qq not in handled and self.preferences.enabled(qq, game)]
        if not candidates:
            return [], False
        async with await get_session() as session:
            bound = list((await session.execute(select(SkUser.platform_user_id).where(SkUser.platform_user_id.in_(candidates)))).scalars())
        pending, failed = [], False
        for qq in sorted(bound):
            key = (qq, game)
            if key not in cache:
                try:
                    cache[key] = (await self._fetch_progress(qq, game), False)
                except Exception as error:
                    cache[key] = (None, True)
                    logger.warning("[Priestess] Weekly %s progress query failed: %s", game, type(error).__name__)
                await asyncio.sleep(REQUEST_GAP)
            progress, error = cache[key]
            failed |= error
            if progress is not None and progress[0] < progress[1]:
                pending.append({"qq": qq, "name": names[qq], "current": progress[0], "total": progress[1]})
        return pending, failed

    async def send_for_bot(self, bot: _OneBotClient, cache: dict | None = None) -> dict[str, int]:
        stats = {"ok": 0, "fail": 0, "at_all": 0}
        cache = {} if cache is None else cache
        async with self._send_lock:
            now = self.now()
            state = self._state(now)
            for game in GAMES:
                if not self.enabled(game):
                    continue
                for group_id in sorted(self.groups(game)):
                    key = f"{game}:{group_id}"
                    delivered = state["deliveries"].setdefault(key, {"report": False, "mentions": [], "done": False})
                    if delivered["done"]:
                        continue
                    try:
                        pending, failed = await self._pending(bot, group_id, game, cache, set(delivered["mentions"]))
                        pending = [p for p in pending if p["qq"] not in delivered["mentions"]]
                        while pending or not delivered["report"]:
                            if not self.enabled(game) or group_id not in self.groups(game) or self.now().date() != now.date():
                                break
                            # Re-read choices after API awaits, so a cancellation takes effect immediately.
                            pending = [p for p in pending if self.preferences.enabled(p["qq"], game)] if notification_enabled(self.plugin, f"weekly_mentions_{game}") else []
                            batch, pending = pending[:MENTIONS_PER_IMAGE], pending[MENTIONS_PER_IMAGE:]
                            if delivered["report"] and not batch:
                                break
                            image = await (build_pending_image(game, batch) if delivered["report"] else build_report(self.plugin, bot, int(group_id), game_key=game, pending=batch))
                            # Rendering also awaits; do not mention somebody who opted out while it ran.
                            mentions = [p["qq"] for p in batch if self.preferences.enabled(p["qq"], game)] if notification_enabled(self.plugin, f"weekly_mentions_{game}") else []
                            if not self.enabled(game) or group_id not in self.groups(game) or self.now().date() != now.date():
                                break
                            await bot.send_weekly_image(int(group_id), image, mentions)
                            delivered["report"] = True
                            delivered["mentions"] = sorted(set(delivered["mentions"]) | set(mentions))
                            self._save_state(state)
                            stats["ok"] += 1
                            await asyncio.sleep(SEND_GAP)
                        if delivered["report"] and not pending and not failed:
                            delivered["done"] = True
                            self._save_state(state)
                    except Exception as error:
                        stats["fail"] += 1
                        logger.warning("[Priestess] Weekly %s image failed for group %s: %s", game, group_id, type(error).__name__)
        return stats

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            now = self.now()
            # Persistent per-game/group/member receipts also cover individual plugin reloads.
            if now.weekday() == 6 and now.hour >= 19:
                await self._send_all()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

    async def _refresh_all(self) -> None:
        for platform in self.plugin.context.platform_manager.get_insts():
            if platform.meta().name != "aiocqhttp" or not hasattr(platform, "get_client"):
                continue
            try:
                await refresh_admin_groups(_OneBotClient(platform.get_client(), platform.meta().id), self.admin_file)
            except Exception as error:
                logger.warning("Weekly report admin list refresh failed: %s", type(error).__name__)

    async def _send_all(self) -> None:
        cache = {}
        for platform in self.plugin.context.platform_manager.get_insts():
            if platform.meta().name != "aiocqhttp" or not hasattr(platform, "get_client"):
                continue
            bot = _OneBotClient(platform.get_client(), platform.meta().id)
            try:
                stats = await self.send_for_bot(bot, cache)
                logger.info("Weekly report sent: %s", stats)
            except Exception as error:
                logger.warning("Weekly report run failed: %s", type(error).__name__)
