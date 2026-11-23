"""Full Arknights roster adapted from Billtranscend/Priestess-bot (MIT)."""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from astrbot.api import logger

from . import ark_theme, ef_theme
from ..config import CACHE_DIR, RES_DIR
from ..data_source import gacha_table_data
from ..schemas.arknights.card import ArkCard
from ..schemas.arknights.game_data import CharTable

WIDTH = 1320
SCALE = 1.25  # 161 位干员在 1.5x 时为 783 KB；1.25x 格子依然清晰，大小约为七成
QUALITY = 55
# 干员多的账号用更密的排列和更低的缩放：往 QQ 上传慢时只有约 30 KB/s，
# 一张 434 位干员的卡按普通排版有 1838 KB（上传 61 秒）。
DENSE_FROM = 200  # 干员数
DENSE_SCALE = 1.0
DENSE_QUALITY = 50
RES = RES_DIR / "images"
AVATAR_URL = "https://web.hycdn.cn/arknights/game/assets/char/avatar/{}.png"
AVATAR_TIMEOUT = 6
AVATAR_KEY = re.compile(r"[A-Za-z0-9_@#-]+")
PROFESSIONS = {"pioneer", "warrior", "tank", "sniper", "caster", "medic", "support", "special"}
CN = timezone(timedelta(hours=8))

CSS = """
body { width: %dpx; padding: 24px; }
img { display: block; object-fit: contain; }
.ph { background: var(--ak-panel-2); }

/* head: doctor + secretary */
.ak-head.pl { padding-right: 260px; }
.ak-head.pl .code b small { font-family: var(--ak-cjk); font-size: 17px; font-weight: 700; letter-spacing: 0; color: var(--ak-blue); }
.who { display: flex; gap: 18px; align-items: center; margin-top: 12px; }
.who .avatar { width: 78px; height: 78px; flex: none; border: 1px solid var(--ak-line-2); background: var(--ak-panel-2); position: relative; }
.who .avatar:after { content: ""; position: absolute; right: -1px; bottom: -1px; width: 12px; height: 12px; background: var(--ak-blue); clip-path: polygon(100%% 0, 100%% 100%%, 0 100%%); }
.who .avatar img, .who .avatar .ph { width: 76px; height: 76px; }
.who h1 { margin-top: 0; font-size: 36px; }
.who p { margin-top: 4px; font-size: 16px; }
.sec { position: absolute; right: 26px; bottom: 26px; display: flex; gap: 10px; align-items: center; }
.sec img, .sec .ph { width: 52px; height: 52px; border: 1px solid var(--ak-line-2); }
.sec small { display: block; font-family: var(--ak-num); font-size: 12px; letter-spacing: 3px; color: var(--ak-faint); text-align: right; }
.sec b { display: block; font-size: 16px; text-align: right; }

/* status tiles */
.tiles { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-top: 16px; }
.tiles div { position: relative; background: var(--ak-panel); border: 1px solid var(--ak-line); padding: 9px 14px 10px; }
.tiles div:before { content: ""; position: absolute; left: -1px; top: -1px; width: 26px; height: 3px; background: var(--ak-blue); }
.tiles small { font-size: 14px; font-weight: 700; color: var(--ak-sub); }
.tiles b { display: block; margin-top: 2px; font-family: var(--ak-num); font-weight: 700; font-size: 32px; line-height: 1.1; letter-spacing: .5px; color: #fff; }
.tiles b span { font-size: 20px; color: var(--ak-faint); }
.tiles i { font-style: normal; font-size: 13px; color: var(--ak-faint); }

/* assist operators */
.assist { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; padding: 14px 16px 16px; }
.as { display: flex; gap: 14px; background: var(--ak-panel-2); border: 1px solid var(--ak-line); padding: 10px; }
.as .pic { width: 88px; height: 88px; flex: none; position: relative; border-bottom: 4px solid; background: #111; }
.as .pic img, .as .pic .ph { width: 88px; height: 84px; object-fit: cover; }
.as .nm { font-size: 20px; font-weight: 900; color: #fff; }
.as .meta { margin-top: 4px; display: flex; gap: 6px; align-items: center; font-size: 13px; color: var(--ak-sub); }
.as .meta img { width: 22px; height: 22px; }
.as .meta b { font-family: var(--ak-num); font-size: 19px; color: #fff; }
.as .sk { margin-top: 6px; display: flex; gap: 8px; align-items: center; font-size: 13px; color: var(--ak-sub); }
.as .sk img, .as .sk .ph { width: 34px; height: 34px; border: 1px solid var(--ak-line-2); }

/* base */
.base { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px; background: var(--ak-line); border-top: 1px solid var(--ak-line); }
.base div { background: var(--ak-panel); padding: 10px 14px; display: flex; gap: 10px; align-items: center; }
.base img { width: 30px; height: 30px; opacity: .85; }
.base small { display: block; font-size: 13px; color: var(--ak-sub); }
.base b { display: block; font-family: var(--ak-num); font-size: 22px; line-height: 1.15; color: #fff; }
.base b span { font-family: var(--ak-cjk); font-size: 13px; font-weight: 400; color: var(--ak-faint); }

/* operator wall */
.stats { display: flex; gap: 8px; padding: 12px 16px 0; }
.wall { display: grid; grid-template-columns: repeat(10, 1fr); gap: 8px; padding: 12px 16px 16px; }
.op { background: #121214; border: 1px solid var(--ak-line); position: relative; }
.op .pic { position: relative; height: 112px; overflow: hidden; }
.op .pic:after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 46px; background: linear-gradient(transparent, rgba(0,0,0,.85)); }
.op .pic > img.av, .op .pic > .ph { width: 100%%; height: 112px; object-fit: cover; }
.op .pf { position: absolute; left: 3px; top: 3px; width: 22px; height: 22px; z-index: 2; background: rgba(0,0,0,.55); padding: 2px; }
.op .el { position: absolute; right: 3px; top: 3px; width: 24px; height: 24px; z-index: 2; }
.op .pt { position: absolute; right: 3px; bottom: 4px; width: 22px; height: 22px; z-index: 2; }
.op .lv { position: absolute; left: 5px; bottom: 2px; z-index: 2; font-family: var(--ak-num); font-weight: 700; font-size: 26px; line-height: 1; color: #fff; }
.op .lv small { font-size: 11px; color: var(--ak-sub); margin-right: 1px; letter-spacing: 1px; }
.op .bar { height: 4px; }
.op .nm { padding: 4px 6px 0; font-size: 14px; font-weight: 700; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.op .ln { display: flex; gap: 3px; align-items: center; padding: 3px 6px 6px; min-height: 25px; }
.op .m { font-family: var(--ak-num); font-weight: 700; font-size: 12px; line-height: 16px; padding: 0 4px; border: 1px solid var(--ak-line-2); color: var(--ak-faint); }
.op .m.on { border-color: var(--ak-blue); color: #fff; background: var(--ak-blue-2); }
.op .sl { font-family: var(--ak-num); font-size: 13px; color: var(--ak-sub); }
.wall.dense { grid-template-columns: repeat(14, 1fr); gap: 6px; }
.dense .op .pic, .dense .op .pic > img.av, .dense .op .pic > .ph { height: 80px; }
.dense .op .pic:after { height: 34px; }
.dense .op .pf { width: 18px; height: 18px; } .dense .op .el, .dense .op .pt { width: 19px; height: 19px; }
.dense .op .lv { font-size: 20px; } .dense .op .lv small { font-size: 9px; }
.dense .op .nm { font-size: 12px; padding: 3px 4px 0; }
.dense .op .ln { padding: 2px 4px 4px; min-height: 20px; gap: 2px; }
.dense .op .m, .dense .op .md { font-size: 11px; line-height: 14px; padding: 0 3px; }
.dense .op .sl { font-size: 11px; }
.op .md { margin-left: auto; font-family: var(--ak-num); font-weight: 700; font-size: 12px; line-height: 16px; padding: 0 4px; color: #111; background: var(--ak-yellow); }
""" % WIDTH


def _local(path: Path) -> str:
    return path.resolve().as_uri() if path.is_file() else ""


def _res(path: str) -> str:
    return _local(RES / path)


def _avatar(char_id: str, skin_id: str = "") -> str:
    """皮肤的缓存头像，没有就用干员的（文件名规则同上游 charId_to_avatarUrl）。"""
    for key in (skin_id, char_id):
        if not AVATAR_KEY.fullmatch(key):
            continue
        name = next((key.replace(symbol, "_", 1) for symbol in ("@", "#") if symbol in key), key)
        if url := _local(CACHE_DIR / "avatar" / f"{name}.png"):
            return url
    return ""


async def _fill_avatars(char_ids: list[str]) -> None:
    """Warm the shared avatar cache within one time budget per card."""
    folder = CACHE_DIR / "avatar"
    missing = [key for key in dict.fromkeys(char_ids) if re.fullmatch(r"char_[A-Za-z0-9_]+", key) and not (folder / f"{key}.png").is_file()]
    if not missing:
        return
    gate = asyncio.Semaphore(8)

    async def fetch(client: httpx.AsyncClient, char_id: str) -> None:
        async with gate:
            try:
                response = await client.get(AVATAR_URL.format(char_id))
                if response.status_code == 200 and response.content.startswith(b"\x89PNG\r\n\x1a\n"):
                    tmp = folder / f".{char_id}.png.tmp"
                    tmp.write_bytes(response.content)
                    tmp.replace(folder / f"{char_id}.png")
            except (httpx.HTTPError, OSError):
                return

    try:
        folder.mkdir(parents=True, exist_ok=True)
        async with asyncio.timeout(AVATAR_TIMEOUT), httpx.AsyncClient() as client:
            await asyncio.gather(*(fetch(client, char_id) for char_id in missing))
    except (TimeoutError, OSError):
        logger.debug("[Priestess] Arknights roster avatar warm-up incomplete")


def _skill_icon(skill_id: str) -> str:
    return _local(CACHE_DIR / "skill" / f"skill_icon_{skill_id}.png") if AVATAR_KEY.fullmatch(skill_id) else ""


def _img(url: str, cls: str = "") -> str:
    return f'<img class="{cls}" src="{escape(url)}">' if url else f'<div class="{cls} ph"></div>'


def _rarity(entry: CharTable | None) -> int:
    if not entry:
        return 0
    if isinstance(entry.rarity, int) and 0 <= entry.rarity <= 5:
        return entry.rarity + 1
    match = re.fullmatch(r"TIER_([1-6])", str(entry.rarity))
    return int(match[1]) if match else 0


def _module(char, card: ArkCard) -> str:
    """当前选用的模组若是分支模组（不是初始的证章），返回其类型和等级。"""
    equip_id = char.defaultEquipId
    info = card.equipmentInfoMap.get(equip_id) if equip_id else None
    if not info or info.typeIcon == "original":
        return ""
    level = next((e.level for e in char.equip if e.id == equip_id), 1)
    branch = info.typeIcon.rsplit("-", 1)[-1].upper()  # "dec-y" -> Y；第三个分支写作 d -> Δ
    return f"{'Δ' if branch == 'D' else branch}{level}"


def _op_tile(char, card: ArkCard, entry) -> str:
    rarity = _rarity(entry)
    color = ark_theme.RARITY_COLOR.get(rarity, "#777")
    name = entry.name if entry else card.charInfoMap.get(char.charId).name if char.charId in card.charInfoMap else char.charId
    profession = entry.profession.lower() if entry else ""
    prof = _res(f"profession/icon_profession_{profession}.png") if profession in PROFESSIONS else ""
    elite = _res(f"ark_card/elite/elite_{char.evolvePhase}.png") if char.evolvePhase else ""
    potential = _res(f"ark_card/potential/potential_{char.potentialRank}.png") if char.potentialRank else ""
    masteries = [s.specializeLevel for s in char.skills]
    if any(masteries):
        skills = "".join(f'<span class="m{" on" if m else ""}">{f"M{m}" if m else "-"}</span>' for m in masteries)
    else:
        skills = f'<span class="sl">技能 {char.mainSkillLvl}</span>' if char.skills else ""
    module = _module(char, card)
    return (
        f'<div class="op"><div class="pic">{_img(_avatar(char.charId, char.skinId), "av")}'
        f'{_img(prof, "pf") if prof else ""}{_img(elite, "el") if elite else ""}{_img(potential, "pt") if potential else ""}'
        f'<div class="lv"><small>LV</small>{char.level}</div></div>'
        f'<div class="bar" style="background:{color}"></div>'
        f'<div class="nm">{escape(name)}</div>'
        f'<div class="ln">{skills}{f"<span class=md>{escape(module)}</span>" if module else ""}</div></div>'
    )


def _assist(char, card: ArkCard, catalog) -> str:
    entry = catalog.get(char.charId)
    rarity = _rarity(entry)
    name = entry.name if entry else card.charInfoMap[char.charId].name if char.charId in card.charInfoMap else char.charId
    elite = _res(f"ark_card/elite/elite_{char.evolvePhase}.png")
    potential = _res(f"ark_card/potential/potential_{char.potentialRank}.png") if char.potentialRank else ""
    mastery = f"专精 {char.specializeLevel}" if char.specializeLevel else f"技能 {char.mainSkillLvl}"
    module = card.equipmentInfoMap.get(char.equip.id) if char.equip else None
    module_text = f" · 模组 {module.typeIcon.upper()} 等级{char.equip.level}" if module and module.typeIcon != "original" else ""
    return (
        f'<div class="as"><div class="pic" style="border-bottom-color:{ark_theme.RARITY_COLOR.get(rarity, "#777")}">{_img(_avatar(char.charId, char.skinId))}</div>'
        f'<div><div class="nm">{escape(name)}</div>'
        f'<div class="meta">{_img(elite) if elite else ""}<b>Lv.{char.level}</b>{_img(potential) if potential else ""}'
        f'{f"<span>{rarity}★</span>" if rarity else ""}</div>'
        f'<div class="sk">{_img(_skill_icon(char.skillId))}<span>{mastery}{escape(module_text)}</span></div></div></div>'
    )


def _countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}天{seconds % 86400 // 3600}小时"
    return f"{seconds // 3600}小时{seconds % 3600 // 60}分"


def _base(card: ArkCard) -> str:
    b = card.building
    icon = lambda n: _res(f"ark_card/building/{n}.png")
    levels = lambda rooms: "·".join(str(r.level) for r in rooms) or "-"
    training = b.training
    trainee = card.trainee_char
    cells = [
        (icon("manufact"), "制造站", f"{len(b.manufactures)}<span> 座 Lv{levels(b.manufactures)}</span>"),
        (icon("trading"), "贸易站", f"{b.trading_stock}/{b.trading_stock_limit}<span> 订单</span>"),
        (icon("dorm"), "宿舍", f"{len(b.dormitories)}<span> 间 Lv{levels(b.dormitories)}</span>"),
        (icon("meeting"), "会客室", f"{len(b.meeting.clue.board) if b.meeting else 0}/7<span> 线索</span>"),
        (icon("labor"), "无人机", f"{b.labor.labor_now}/{b.labor.maxValue}"),
        ("", "控制中枢", f"Lv{b.control.level}"),
        ("", "发电站", f"{len(b.powers)}<span> 座 Lv{levels(b.powers)}</span>"),
        ("", "办公室", f"{b.hire.refreshCount if b.hire else 0}/3<span> 次刷新</span>"),
        ("", "训练室", f"{escape(trainee) if trainee else ('空闲' if training else '未建造')}"),
        (icon("tired"), "疲劳干员", f"{len(b.tiredChars)}<span> 位</span>"),
    ]
    return "".join(f'<div>{_img(i) if i else ""}<div><small>{a}</small><b>{v}</b></div></div>' for i, a, v in cells)


def _render(card: ArkCard, catalog: dict[str, CharTable], now: float) -> str:
    st = card.status
    chars = sorted(card.chars, key=lambda c: (-_rarity(catalog.get(c.charId)), -c.evolvePhase, -c.level, -c.potentialRank, c.charId))
    ap_max = st.ap.max  # 超过上限时（理智药、奖励）存储值才是真实值；上游的 ap_now 会把它截到上限
    recovered_ap = ap_max - math.ceil(max(0, st.ap.completeRecoveryTime - now) / 360)
    ap_now = max(st.ap.current, recovered_ap)
    ap_note = "已回满" if ap_now >= ap_max or st.ap.completeRecoveryTime <= now else f"{_countdown(st.ap.completeRecoveryTime - now)}后回满"
    recruit_total = len(card.recruit)
    tower = card.tower.reward
    tiles = [
        ("理智", f"{ap_now}<span>/{ap_max}</span>", ap_note),
        ("日常任务", f"{card.routine.daily.current}<span>/{card.routine.daily.total}</span>", "已完成"),
        ("周常任务", f"{card.routine.weekly.current}<span>/{card.routine.weekly.total}</span>", "已完成"),
        ("剿灭作战", f"{card.campaign.reward.current}<span>/{card.campaign.reward.total}</span>", "本周合成玉"),
        ("保全派驻", f"{tower.higherItem.current}<span>/{tower.higherItem.total}</span>", f"{_countdown(tower.termTs - now)}后结束" if tower.termTs > now else "本期已结束"),
        ("公开招募", f"{card.recruit_finished}<span>/{recruit_total}</span>", escape(card.recruit_complete_time) if recruit_total else "未开放"),
        ("干员", str(st.charCnt or len(card.chars)), "已招募"),
        ("时装", str(st.skinCnt), "已拥有"),
        ("家具", str(max(st.furnitureCnt, card.building.furniture.total)), "已拥有"),
        ("蚀刻章", str(card.medal.total), "已获得"),
    ]
    tiles_html = "".join(f"<div><small>{a}</small><b>{b}</b><i>{c}</i></div>" for a, b, c in tiles)

    e2 = sum(1 for c in card.chars if c.evolvePhase == 2)
    six = sum(1 for c in card.chars if _rarity(catalog.get(c.charId)) == 6)
    m3 = sum(1 for c in card.chars for s in c.skills if s.specializeLevel == 3)
    modules = sum(1 for c in card.chars if _module(c, card))
    stats = (f'<span class="ak-tag b">6★ {six}</span><span class="ak-tag">精英二 {e2}</span>'
             f'<span class="ak-tag">专精三技能 {m3}</span><span class="ak-tag">已装备模组 {modules}</span>')

    uid = escape(st.uid[-4:].rjust(len(st.uid), "*"))
    stage = re.sub(r"^(st|main)_", "", st.mainStageProgress or "")
    stage_text = f" · 主线 {escape(stage)}" if stage else " · 主线已全部通关"
    doctor_avatar = _avatar(st.avatar.id) if st.avatar and st.avatar.id else ""
    sec = st.secretary
    sec_entry = catalog.get(sec.charId) if sec else None
    secretary = (
        f'<div class="sec"><div><small>ASSISTANT</small><b>{escape(sec_entry.name if sec_entry else "")}</b></div>'
        f'{_img(_avatar(sec.charId, sec.skinId))}</div>' if sec and sec.charId else ""
    )
    assists = "".join(_assist(c, card, catalog) for c in card.assistChars) or '<div class="ak-empty">未设置助战干员</div>'

    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ark_theme.css()}{CSS}</style></head><body class="ak">'
        f"""<div class="ak-head pl"><span class="ak-corner tl"></span><span class="ak-corner br"></span>
  <div class="code">OPERATORS<b>{len(chars)} <small>位干员</small></b></div>
  <div class="k">Rhodes Island · Doctor Record</div>
  <div class="who">
    <div class="avatar">{_img(doctor_avatar)}</div>
    <div><h1>{escape(st.name)}</h1>
      <p>UID {uid} · 等级 {st.level} · 入职 {st.register_time}{stage_text}</p></div>
  </div>
  {secretary}
</div>
<div class="tiles">{tiles_html}</div>
<div class="ak-sec"><h3>助战干员<small>Support Unit</small></h3><div class="assist">{assists}</div></div>
<div class="ak-sec"><h3>基建<small>Rhodes Island Base</small></h3><div class="base">{_base(card)}</div></div>
<div class="ak-sec"><h3>干员<small class="cjk">按稀有度、精英化与等级排序</small></h3><div class="stats">{stats}</div>
<div class="wall{" dense" if len(chars) >= DENSE_FROM else ""}">{"".join(_op_tile(c, card, catalog.get(c.charId)) for c in chars)}</div></div>"""
        + ark_theme.foot(f'数据来源 森空岛<br>生成时间 {datetime.fromtimestamp(now, CN):%Y-%m-%d %H:%M}')
        + "</body></html>"
    )


async def render_ark_roster(card: ArkCard) -> str:
    catalog = {entry.char_id: entry for entry in gacha_table_data.character_table}
    ids = [card.status.secretary.charId, *(char.charId for char in card.assistChars), *(char.charId for char in card.chars)]
    await _fill_avatars(ids)
    dense = len(card.chars) >= DENSE_FROM
    image = await ef_theme.render_page(
        _render(card, catalog, time.time()), WIDTH, height=1,
        scale=DENSE_SCALE if dense else SCALE, quality=DENSE_QUALITY if dense else QUALITY,
    )
    path = CACHE_DIR / f"ark_roster_{datetime.now():%Y%m%d%H%M%S%f}.webp"
    path.write_bytes(image)
    return str(path)
