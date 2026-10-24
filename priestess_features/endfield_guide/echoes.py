"""Collect bound members' War Echoes (战争回响) best clears for per-group team statistics.

Only the best-record team (operator id, level, potential, elite phase) and the
增辉 flag are kept per stage and difficulty, keyed by QQ. Members who opted out
are never fetched and are dropped from stored data.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

import httpx
from astrbot.api import logger
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from ...api import SklandAPI, SklandLoginAPI
from ...db import Character, SkUser, get_default_ef_character, get_session
from ...exception import LoginException
from ...schemas import CRED

from .builds import CARD_URL, BuildStore, extract

WAR_ECHOES_URL = "https://zonai.skland.com/api/v1/game/endfield/card/war-echoes"
INDIE_HARD_URL = "https://zonai.skland.com/api/v1/game/endfield/card/indie-hard"  # 影拓丰碑
DIFFICULTY_FIELDS = (("normalDungeon", "普通"), ("hardDungeon", "困难"), ("cruelDungeon", "残酷"))
REQUEST_GAP = 2.0  # between each worker's requests
WORKERS = 5
SAVE_EVERY = 50
ECHO_SCHEMA = 2  # 2: keep the fastest clear when a stage recurs across seasons (1 kept the latest)


class RecordError(RuntimeError):
    """Skland answered the record request with a non-zero code."""


class EchoStore:
    def __init__(self, data_file: Path, optout_file: Path, layout_file: Path | None = None) -> None:
        self.data_file = data_file
        self.optout_file = optout_file
        self.layout_file = layout_file or data_file.with_name("layout.json")
        self.lock = asyncio.Lock()

    def layout(self) -> dict:
        """What Skland shows every player alike: series and stages in the game's order, with their pictures."""
        try:
            return json.loads(self.layout_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save_layout(self, layout: dict) -> None:
        if not layout.get("monument") or layout == self.layout():
            return
        tmp = self.layout_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(layout, ensure_ascii=False), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.layout_file)

    def load(self) -> dict:
        try:
            return json.loads(self.data_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {"members": {}, "targets": {}}

    def _save(self, data: dict) -> None:
        tmp = self.data_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.data_file)

    def optout(self) -> set[str]:
        # A missing file means nobody opted out; a corrupt one must not silently re-include people.
        if not self.optout_file.exists():
            return set()
        return set(json.loads(self.optout_file.read_text("utf-8")))

    def set_optout(self, qq: str, out: bool) -> None:
        ids = self.optout()
        ids.add(qq) if out else ids.discard(qq)
        tmp = self.optout_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(ids)), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.optout_file)
        if out:
            data = self.load()
            if data["members"].pop(qq, None) is not None:
                self._save(data)

    async def collect(self, builds: BuildStore | None = None, *, builds_only: bool = False) -> dict[str, int]:
        """Fetch every bound, non-opted-out member once; returns counters for logging.

        With `builds`, each member's card is read as well and their operators' equipment is stored there.
        """
        async with self.lock:
            optout = self.optout()
            data = self.load()
            stats = Counter()
            build_members = builds.load().get("members") or {} if builds else {}
            try:
                await self._collect_into(data, stats, optout, builds, build_members, builds_only=builds_only)
            finally:  # also after a failure: members already fetched keep their new records, the others their old ones
                data["schema"] = ECHO_SCHEMA
                self._save(data)
                if builds is not None:
                    builds.save(build_members)
            return dict(stats)

    async def _collect_into(self, data: dict, stats: Counter, optout: set[str], builds: BuildStore | None, build_members: dict, *, builds_only: bool = False) -> None:
        async with await get_session() as session:
            # Keep identifiers as plain strings; ORM objects expire after each token commit.
            binds = list((await session.execute(select(SkUser.id, SkUser.platform_user_id))).all())
            bound_qqs = {str(qq) for _, qq in binds}
            for qq in list(data["members"]):
                if qq not in bound_qqs or qq in optout:
                    data["members"].pop(qq, None)
            for qq in list(build_members):
                if qq not in bound_qqs or qq in optout:
                    build_members.pop(qq, None)
        queue: asyncio.Queue = asyncio.Queue()
        for bind in binds:
            queue.put_nowait(bind)
        done = 0

        async def worker() -> None:
            nonlocal done
            # SQLAlchemy sessions cannot be shared across concurrent tasks.
            async with await get_session() as session:
                session.autoflush = False
                while not queue.empty():
                    user_id, platform_user_id = queue.get_nowait()
                    await self._member(session, user_id, platform_user_id, data, stats, optout, builds, build_members, builds_only=builds_only)
                    done += 1
                    if done % 20 == 0:
                        logger.info(f"War Echoes collection progress: {done}/{len(binds)} binds, {dict(stats)}")
                    if done % SAVE_EVERY == 0:
                        data["schema"] = ECHO_SCHEMA
                        self._save(data)
                        if builds is not None:
                            builds.save(build_members)

        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(WORKERS):
                    group.create_task(worker())
        except ExceptionGroup as failed:
            raise failed.exceptions[0] from None

    async def _member(self, session, user_id: int, platform_user_id: str, data: dict, stats: Counter, optout: set[str], builds: BuildStore | None, build_members: dict, *, builds_only: bool = False) -> None:
        qq = str(platform_user_id)
        if qq in optout:
            data["members"].pop(qq, None)
            stats["opted_out"] += 1
            return
        user = await session.get(SkUser, user_id)
        if user is None:
            return
        # One entry per member: their default Endfield role (unbound since the last
        # run, or no such role: no stale clears on the boards).
        char = await get_default_ef_character(session, user)
        if char is None:
            stats["dropped"] += data["members"].pop(qq, None) is not None
            build_members.pop(qq, None)
            return
        stats["eligible"] += 1
        if not builds_only:
            old = data["members"].get(qq, {})
            sources = dict(old.get("sources") or {})
            fresh = 0
            for source, url, field in (("echoes", WAR_ECHOES_URL, "warEchoes"), ("monument", INDIE_HARD_URL, "indieHard")):
                for attempt in range(2):
                    try:
                        payload = (await _fetch(url, user, char))[field]
                        if source == "echoes":
                            records, targets = _extract(payload)
                            data["targets"].update(targets)
                            layout = self.layout()
                            layout["seasons"] = extract_layout(payload, {})["seasons"]
                        else:
                            records = _extract_monument(payload)
                            layout = self.layout()
                            layout["monument"] = extract_layout({}, payload)["monument"]
                        self.save_layout(layout)
                        stamp = time.time()
                        for record in records.values():
                            record["collected_at"] = stamp
                        sources[source] = {"ts": stamp, "records": records}
                        stats[source] += 1
                        fresh += 1
                        break
                    except Exception as e:
                        if isinstance(e, RecordError) or attempt:
                            stats[type(e).__name__] += 1
                            stats[f"{source}_failed"] += 1
                            break
                        await asyncio.sleep(REQUEST_GAP * 2)
                await asyncio.sleep(REQUEST_GAP)
            if fresh:
                # Legacy records have no source: keep them until both sources succeed.
                legacy = {} if len(sources) == 2 else old.get("legacy_records", old.get("records", {}) if not old.get("sources") else {})
                for record in legacy.values():
                    record.setdefault("collected_at", old.get("ts", 0))
                records = dict(legacy)
                for snapshot in sources.values():
                    records.update(snapshot["records"])
                data["members"][qq] = {"ts": time.time(), "sources": sources, "legacy_records": legacy, "records": records}
                stats["ok"] += 1
            elif old:
                stats["retained"] += 1
        if builds is not None:
            try:  # a failure keeps the member's previous snapshot
                await asyncio.sleep(REQUEST_GAP)
                card = await _fetch(CARD_URL, user, char)
                chars = extract(card.get("detail") or {})
                build_members[qq] = {"ts": time.time(), "chars": chars}
                stats["builds"] += 1
                stats["build_operators"] += len(chars)
                stats["builds_empty"] += not chars
            except Exception as e:
                stats[f"builds_{type(e).__name__}"] += 1
        # Persist refreshed tokens now: a write left pending across the whole run would hold
        # SQLite's write lock for minutes and make binding / gacha updates fail.
        await _commit(session, stats)
        await asyncio.sleep(REQUEST_GAP)


async def _commit(session, stats: Counter) -> None:
    """Save refreshed tokens; a database locked by another write is waited for, then given up (the token is refreshed again next time).

    On 2026-10-16 one locked commit at member 300 of 404 ended the morning run and threw away everything it had fetched.
    """
    for attempt in range(4):
        try:
            await session.commit()
            return
        except OperationalError:
            await session.rollback()
            await asyncio.sleep(3 * (attempt + 1))
    stats["commit_locked"] += 1


async def _fetch(base_url: str, user: SkUser, char: Character) -> dict:
    url = f"{base_url}?" + urlencode({"roleId": char.role_id, "serverId": char.channel_master_id, "userId": user.user_id})

    async def call() -> dict:
        cred = CRED(cred=user.cred, token=user.cred_token, userId=user.user_id)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=await SklandAPI.get_sign_header(cred, url, method="get"))
        return response.json()

    payload = await call()
    if payload.get("code") == 10000:  # cred_token expired
        user.cred_token = await SklandLoginAPI.refresh_token(user.cred)
        payload = await call()
    if payload.get("code") == 10002:  # cred expired; needs the stored access token
        if not user.access_token:
            raise LoginException("cred expired")
        new = await SklandLoginAPI.get_cred(await SklandLoginAPI.get_grant_code(user.access_token, 0))
        user.cred, user.cred_token = new.cred, new.token
        payload = await call()
    if payload.get("code"):
        raise RecordError(f"skland record code {payload.get('code')}")
    return payload["data"]


def _faster(new: dict, old: dict | None) -> bool:
    """Fastest clear wins; a record without a time loses; ties go to whoever was set first."""
    if old is None:
        return True
    rank = lambda r: (r["time"] is None, r["time"] or 0, r["at"] or float("inf"))
    return rank(new) < rank(old)


def _extract(echoes: dict) -> tuple[dict, dict]:
    """Fastest cleared team per "stage|difficulty" across all seasons, plus each stage's 增辉 target text.

    A stage recurs in several seasons/rotations, each with its own bestRecord.
    """
    records, targets = {}, {}
    for season in sorted(echoes.get("seasons", []), key=lambda s: int(s.get("id") or 0)):
        for week in season.get("weeks", []):
            for group in week.get("dungeonGroups", []):
                name = group.get("name", "")
                for field, difficulty in DIFFICULTY_FIELDS:
                    dungeon = group.get(field) or {}
                    if dungeon.get("additionalChallengeTarget"):
                        targets[f"{name}|{difficulty}"] = dungeon["additionalChallengeTarget"]
                    best = dungeon.get("bestRecord") or {}
                    chars = best.get("chars") or []
                    if dungeon.get("isPass") and chars:
                        record, key = _record(best, bool(dungeon.get("plusTask"))), f"{name}|{difficulty}"
                        if _faster(record, records.get(key)):
                            records[key] = record
    return records, targets


def _record(best: dict, plus: bool = False) -> dict:
    """passTs is the clear time in whole seconds (only present on top difficulties)."""
    seconds = best.get("passTs")
    return {
        "plus": plus,
        "time": int(seconds) if str(seconds or "").isdigit() and int(seconds) > 0 else None,
        "at": int(best["ts"]) if str(best.get("ts") or "").isdigit() else 0,
        "team": [
            {"id": c.get("charId", ""), "lv": c.get("level", 0), "pot": c.get("potentialLevel", 0), "phase": c.get("evolvePhase", 0)}
            for c in best.get("chars") or []
        ],
    }


def _extract_monument(indie_hard: dict) -> dict:
    """影拓丰碑 best clears keyed "stage|普通" / "stage|苦难"."""
    records = {}
    for series in indie_hard.get("indieHardGroups", []):
        for group in series.get("dungeonGroups", []):
            for field, difficulty in (("normalDungeon", "普通"), ("hardDungeon", "苦难")):
                dungeon = group.get(field) or {}
                best = dungeon.get("bestRecord") or {}
                if dungeon.get("isPass") and best.get("chars"):
                    name = dungeon.get("name", "").split("·")[0].strip()
                    record, key = _record(best), f"{name}|{difficulty}"
                    if _faster(record, records.get(key)):
                        records[key] = record
    return records


def extract_layout(echoes: dict, indie_hard: dict) -> dict:
    """Series and stage order as the game lists them, plus the cover pictures Skland uses.

    The game tables keep neither the order of 影拓丰碑 stages nor a series cover.
    """

    def stamp(value) -> int:
        return int(value) if str(value or "").isdigit() else 0

    monument = [
        {
            "name": series.get("name", ""),
            "pic": series.get("pic", ""),
            "stages": [
                (group.get("normalDungeon") or group.get("hardDungeon") or {}).get("name", "").split("·")[0].strip()
                for group in series.get("dungeonGroups", [])
            ],
            "activity": series.get("activityName", ""),
            "open": stamp(series.get("activityStartTs")),
            "close": stamp(series.get("activityEndTs")),
        }
        for series in indie_hard.get("indieHardGroups", [])
    ]
    seasons = {
        str(season.get("id")): {"name": season.get("name", ""), "kv": season.get("kvImage", ""), "header": season.get("headerImage", "")}
        for season in echoes.get("seasons", [])
    }
    return {"monument": monument, "seasons": seasons}


def speed_ranking(data: dict, group_members: set[str], stage_key: str) -> list[tuple[str, dict]]:
    """(qq, record) pairs with a clear time, fastest first; ties go to whoever set it earlier."""
    rows = [
        (qq, m["records"][stage_key])
        for qq, m in data.get("members", {}).items()
        if qq in group_members and m.get("records", {}).get(stage_key, {}).get("time")
    ]
    return sorted(rows, key=lambda row: (row[1]["time"], row[1]["at"] or float("inf")))


def usage_rates(data: dict, group_members: set[str], stage_keys: set[str] | None) -> tuple[int, Counter]:
    """How many recorded clears include each operator (each clear counts an operator once)."""
    total, counts = 0, Counter()
    for qq, member in data.get("members", {}).items():
        if qq not in group_members:
            continue
        for key, record in member.get("records", {}).items():
            if stage_keys is not None and key not in stage_keys:
                continue
            total += 1
            counts.update({c["id"] for c in record["team"] if c["id"]})
    return total, counts


def partners(data: dict, same: dict[str, str] | None = None, mates_top: int = 6, teams_top: int = 3) -> dict[str, dict]:
    """Per operator, from members' stored clears: how many members cleared with it, whom they teamed it
    with and their most common full lineups. A member counts once per teammate and once per lineup, so
    one member's many clears do not outweigh the others. `same` maps alternative ids to one (管理员)."""
    same = same or {}
    users, mates, lineups = Counter(), {}, {}
    for member in data.get("members", {}).values():
        with_whom: dict[str, set[str]] = {}
        teams: dict[str, set[tuple[str, ...]]] = {}
        for record in member.get("records", {}).values():
            ids = sorted({same.get(c["id"], c["id"]) for c in record["team"] if c.get("id")})
            if len(ids) < 2:
                continue
            for cid in ids:
                with_whom.setdefault(cid, set()).update(x for x in ids if x != cid)
                teams.setdefault(cid, set()).add(tuple(ids))
        for cid, others in with_whom.items():
            users[cid] += 1
            mates.setdefault(cid, Counter()).update(others)
            lineups.setdefault(cid, Counter()).update(t for t in teams[cid] if len(t) == 4)
    return {
        cid: {"n": n, "mates": mates[cid].most_common(mates_top), "teams": [[list(t), c] for t, c in lineups[cid].most_common(teams_top)]}
        for cid, n in users.items()
    }


def team_stats(data: dict, group_members: set[str], stage_key: str, top: int = 3) -> dict:
    """Most common teams among group members who cleared the stage."""
    clears = [m["records"][stage_key] for qq, m in data.get("members", {}).items() if qq in group_members and stage_key in m.get("records", {})]
    teams: dict[tuple, list] = {}
    for clear in clears:
        teams.setdefault(tuple(sorted(c["id"] for c in clear["team"])), []).append(clear)
    ranked = sorted(teams.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    result = []
    for ids, members in ranked:
        chars = []
        for cid in ids:
            rows = [c for m in members for c in m["team"] if c["id"] == cid]
            chars.append({"id": cid, "lv": round(sum(r["lv"] for r in rows) / len(rows)), "pot": round(sum(r["pot"] for r in rows) / len(rows), 1)})
        result.append({"count": len(members), "plus": sum(1 for m in members if m["plus"]), "chars": chars})
    return {"clears": len(clears), "plus": sum(1 for c in clears if c["plus"]), "teams": result}
