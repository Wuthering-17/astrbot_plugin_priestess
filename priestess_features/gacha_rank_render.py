"""Leaderboard HTML adapted from Billtranscend/Priestess-bot."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from ..plugins import ef_theme
TOP = 10
MIN_UP = 2
WIDTH = 1000
FULL_COLUMNS = 3

@dataclass
class Entry:
    qq: str
    name: str
    up_avg: float
    up_count: int
    weapon_avg: float
    weapon_up: int
    char_known: bool = True
    weapon_known: bool = True


@dataclass(frozen=True)
class Board:
    pool: str  # 角色池 / 武器池
    command: str
    english: str
    basis: str
    target: str  # what one UP is, with its measure word
    measure: str
    empty: str
    avg: Callable[[Entry], float]
    count: Callable[[Entry], int]
    count_label: str = "UP"

    def ranked(self, entries: list[Entry]) -> list[Entry]:
        """Luckiest first; fewer UPs than MIN_UP is not ranked."""
        return sorted((e for e in entries if self.count(e) >= MIN_UP), key=lambda e: (self.avg(e), -self.count(e)))


CHARACTERS = Board(
    pool="角色池",
    command="/角色欧非榜",
    english="Operator",
    basis="限定池「平均多少抽出一个 UP 干员」",
    target="一个 UP 干员",
    measure="个",
    empty="本群还没有可统计的终末地限定池抽卡记录",
    avg=lambda e: e.up_avg,
    count=lambda e: e.up_count if e.char_known else 0,
)


WEAPONS = Board(
    pool="武器池",
    command="/武器欧非榜",
    english="Weapon",
    basis="武器池「平均多少抽出一把 UP 武器」",
    target="一把 UP 武器",
    measure="把",
    empty="本群还没有可统计的终末地武器池抽卡记录",
    avg=lambda e: e.weapon_avg,
    count=lambda e: e.weapon_up if e.weapon_known else 0,
)


CSS = """
body { width: %dpx; padding: 26px; }
.num { font-family: var(--ef-num); font-variant-numeric: tabular-nums; }
.summary { display: flex; gap: 12px; margin-top: 16px; }
.summary div { flex: 1; padding: 10px 16px 12px; background: var(--ef-panel); border: 1px solid var(--ef-line); border-top: 3px solid var(--ef-ink);
  font-size: 16px; color: var(--ef-sub); }
.summary b { display: block; margin-top: 2px; font-family: var(--ef-num); font-weight: 700; font-size: 40px; line-height: 1.05; color: var(--ef-ink); }
.summary div.hl b { display: inline-block; padding: 0 8px; background: var(--ef-yellow); border: 1px solid var(--ef-ink); }
.mine { position: relative; margin-top: 12px; padding: 11px 16px 11px 24px; background: var(--ef-dark); color: #f4f4f0; font-size: 19px; }
.mine:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 8px; background: var(--ef-yellow); }
.mine b { font-family: var(--ef-num); font-weight: 700; font-size: 24px; color: var(--ef-yellow); padding: 0 2px; }
.mine i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 22px; }
.mine div + div { margin-top: 5px; }
.mine em { font-style: normal; color: #b9b9b2; }
.pair { display: flex; gap: 14px; align-items: flex-start; }
.pair .ef-sec { flex: 1; min-width: 0; }
.unlucky > h3:before { background: var(--ef-ink); }
table { width: 100%%; border-collapse: collapse; table-layout: fixed; }
td { padding: 7px 12px; border-top: 1px solid var(--ef-line-2); font-size: 19px; white-space: nowrap; }
tr:first-child td { border-top: 0; }
td.rk { width: 48px; padding-right: 0; }
td.rk span { display: inline-block; width: 28px; height: 28px; line-height: 27px; text-align: center; font-family: var(--ef-num); font-weight: 700;
  font-size: 18px; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
tr:nth-child(1) td.rk span { background: var(--ef-yellow); border-color: var(--ef-ink); }
tr:nth-child(2) td.rk span { background: var(--ef-silver); color: var(--ef-ink); border-color: var(--ef-ink); } tr:nth-child(3) td.rk span { background: var(--ef-copper); color: #fff; border-color: var(--ef-ink); }
td.nm { overflow: hidden; text-overflow: ellipsis; }
td.val { width: 104px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 25px; line-height: 1; }
td.val small { font-family: var(--ef-cjk); font-size: 14px; color: var(--ef-sub); font-weight: 400; margin-left: 3px; }
tr:nth-child(1) td.val b { font-weight: 700; background: var(--ef-yellow); padding: 0 4px; }
td.val b { font-weight: 700; }
td.cnt { width: 96px; text-align: right; font-size: 14px; color: var(--ef-sub); }
td.cnt b { font-family: var(--ef-num); font-weight: 600; font-size: 18px; color: var(--ef-ink); margin-right: 2px; }
tr.me td { background: rgba(255, 225, 0, .3); }
tr.me td.nm { font-weight: 700; }
tr.me td:first-child { box-shadow: inset 4px 0 0 var(--ef-ink); }
.grid { display: grid; grid-auto-flow: column; grid-template-columns: repeat(%d, 1fr); gap: 0 18px; padding: 8px 16px 10px; }
.grid span { display: flex; align-items: center; gap: 10px; font-size: 17px; padding: 5px 6px; border-bottom: 1px solid var(--ef-line-2); overflow: hidden; }
.grid span.me { background: var(--ef-yellow); box-shadow: inset 0 0 0 1px var(--ef-ink); }
.grid i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 17px; color: var(--ef-sub); width: 30px; flex: none; text-align: right; }
.grid span.me i { color: var(--ef-ink); }
.grid em { font-style: normal; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.grid u { margin-left: auto; text-decoration: none; font-size: 13px; color: var(--ef-sub); flex: none; }
.grid b { width: 52px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 20px; flex: none; }
.grid span.top i { color: var(--ef-ink); font-weight: 700; }
"""


def _rows(board: Board, entries: list[Entry], requester: str) -> str:
    return "".join(
        f'<tr class="{"me" if e.qq == requester else ""}"><td class="rk"><span>{i}</span></td><td class="nm">{escape(e.name)}</td>'
        f'<td class="val"><b>{board.avg(e):.1f}</b><small>抽</small></td>'
        f'<td class="cnt"><b>{board.count(e)}</b> {board.measure} {board.count_label}</td></tr>'
        for i, e in enumerate(entries, 1)
    )


def _tables(board: Board, ranked: list[Entry], requester: str) -> str:
    """The ten luckiest and the ten unluckiest of one board, side by side."""

    def table(title: str, kind: str, entries: list[Entry]) -> str:
        return (
            f'<div class="ef-sec {kind}"><h3>{board.pool} · {title}<small>{kind.upper()} · TOP {TOP}</small></h3>'
            f"<table>{_rows(board, entries, requester)}</table></div>"
        )

    return f'<div class="pair">{table("欧皇榜", "lucky", ranked[:TOP])}{table("非酋榜", "unlucky", ranked[::-1][:TOP])}</div>'


def _mine(board: Board, ranked: list[Entry], entries: list[Entry], requester: str) -> str:
    """One line with the requester's place on a board; empty when they have no saved records in this group."""
    if not any(e.qq == requester for e in entries):
        return ""
    label = f"你的{board.pool}排名："
    pos = next((i for i, e in enumerate(ranked, 1) if e.qq == requester), None)
    me_entry = next(e for e in entries if e.qq == requester)
    if not (me_entry.char_known if board is CHARACTERS else me_entry.weapon_known):
        return f"<div>{label}<em>卡池 UP 资料缺失，暂未上榜</em></div>"
    if pos is None:
        return f"<div>{label}<em>UP 不足 {MIN_UP} {board.measure}，暂未上榜</em></div>"
    me = ranked[pos - 1]
    return (
        f"<div>{label}第 <b>{pos}</b> / <i>{len(ranked)}</i> 名，"
        f"平均 <b>{board.avg(me):.1f}</b> 抽出{board.target}（共 <i>{board.count(me)}</i> {board.measure}）</div>"
    )


def _average(board: Board, ranked: list[Entry]) -> float:
    return sum(board.avg(e) for e in ranked) / len(ranked)


def _page(code: str, title: str, subtitle: str, body: str, note: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS % (WIDTH, FULL_COLUMNS)}</style></head><body class="ef">'
        + ef_theme.head(code, f"终末地 · <em>{title}</em>", subtitle, f"DATE<b>{datetime.now(ZoneInfo('Asia/Shanghai')):%y-%m-%d}</b>")
        + body
        + ef_theme.foot(
            f"数据来自已保存的抽卡记录（不含当前垫抽），至少出过 {MIN_UP} 个 UP 才参与排名<br>"
            f"{note}不想上榜可发送 /zmd欧非榜 退出，重新加入发送 /zmd欧非榜 加入"
        )
        + "</body></html>"
    )


def _render_overview(entries: list[Entry], requester: str) -> str | None:
    """Both boards, top and bottom ten each; None when nobody in the group can be ranked on the character board."""
    boards = [(b, ranked) for b in (CHARACTERS, WEAPONS) if (ranked := b.ranked(entries))]
    if not boards:
        return None
    tiles = "".join(
        f'<div>{b.pool}上榜<b>{len(ranked)}</b></div><div class="hl">{b.pool}群平均 UP 抽数<br><b>{_average(b, ranked):.1f}</b></div>'
        for b, ranked in boards
    )
    if len(boards) == 1:
        tiles += f"<div>本群有抽卡记录<b>{len(entries)}</b></div>"
    mine = "".join(_mine(b, ranked, entries, requester) for b, ranked in boards)
    return _page(
        "Endfield · Headhunting Luck Board",
        "本群欧非榜",
        f"按「平均多少抽出一个 UP」排名，越少越欧；每个榜只列前 {TOP} 名和后 {TOP} 名",
        f'<div class="summary">{tiles}</div>'
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + "".join(_tables(b, ranked, requester) for b, ranked in boards),
        f"完整榜单发送 {CHARACTERS.command} 或 {WEAPONS.command}；",
    )


def _render_board(board: Board, entries: list[Entry], requester: str) -> str | None:
    """Everyone on one board; None when nobody in the group can be ranked on it."""
    ranked = board.ranked(entries)
    if not ranked:
        return None
    cells = "".join(
        f'<span class="{"me" if e.qq == requester else "top" if i <= 3 else ""}"><i>{i}</i><em>{escape(e.name)}</em>'
        f"<u>{board.count(e)} {board.measure}</u><b>{board.avg(e):.1f}</b></span>"
        for i, e in enumerate(ranked, 1)
    )
    mine = _mine(board, ranked, entries, requester)
    rows = -(-len(ranked) // FULL_COLUMNS)
    return _page(
        f"Endfield · {board.english} Luck Board",
        f"本群{board.pool}欧非榜",
        f"按{board.basis}排名，越少越欧",
        f"""<div class="summary">
    <div>参与排名<b>{len(ranked)}</b></div>
    <div>最欧<b>{board.avg(ranked[0]):.1f}</b></div>
    <div>最非<b>{board.avg(ranked[-1]):.1f}</b></div>
    <div class="hl">群平均 UP 抽数<br><b>{_average(board, ranked):.1f}</b></div>
  </div>"""
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + f'<div class="ef-sec rest"><h3>完整榜单<small class="cjk">名次 · 群名片 · UP 数 · 平均抽数</small></h3><div class="grid" style="grid-template-rows: repeat({rows}, auto)">{cells}</div></div>',
        "前后十名总览发送 /zmd欧非榜；",
    )
