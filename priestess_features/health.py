"""Skland account health checks and protected cleanup backups for AstrBot."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select

from astrbot.api import logger

from ..api import SklandAPI, SklandLoginAPI
from ..db import Character, GachaRecord, SkUser, StaminaAlert, get_session
from ..exception import RequestException
from ..schemas import CRED
from .guide import CN

BASE = "https://zonai.skland.com/api/v1"
BINDING_URL = f"{BASE}/game/player/binding"
ARK_ATTENDANCE_URL = f"{BASE}/game/attendance"
EF_ATTENDANCE_URL = "https://zonai.skland.com/web/v1/game/endfield/attendance"
CHECKED_APPS = {"arknights", "endfield"}
GAP = 0.6
RECHECK_GAP = 8
NO_ROLE = "终末地绑定下没有角色（仅移除这条空记录）"
EXPIRED = "登录已过期"


class NetworkError(Exception):
    """Skland could not be reached reliably during this check."""


class AccountHealth:
    def __init__(self, plugin, data_dir: Path) -> None:
        self.plugin = plugin
        self.backup_dir = data_dir / "backups" / "skland-health"
        self._lock = asyncio.Lock()

    @staticmethod
    def _row(obj) -> dict:
        return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}

    @staticmethod
    def _is_network_error(error: Exception) -> bool:
        cause = error.__cause__ or error.__context__
        return isinstance(error, httpx.HTTPError) or isinstance(cause, httpx.HTTPError)

    async def _refresh_cred(self, user: SkUser) -> bool:
        try:
            token = await SklandLoginAPI.refresh_token(user.cred)
        except (RequestException, httpx.HTTPError) as error:
            if self._is_network_error(error):
                raise NetworkError(type(error).__name__) from error
            return False
        user.cred_token = token
        return True

    async def _refresh_account(self, user: SkUser) -> bool:
        if not user.access_token:
            return False
        try:
            grant_code = await SklandLoginAPI.get_grant_code(user.access_token, 0)
            refreshed = await SklandLoginAPI.get_cred(grant_code)
        except (RequestException, httpx.HTTPError) as error:
            if self._is_network_error(error):
                raise NetworkError(type(error).__name__) from error
            return False
        user.cred = refreshed.cred
        user.cred_token = refreshed.token
        return True

    async def _json_get(self, user: SkUser, url: str, extra: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                cred = CRED(cred=user.cred, token=user.cred_token)
                headers = await SklandAPI.get_sign_header(cred, url, method="get")
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(url, headers={**headers, **(extra or {})})
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                raise ValueError("invalid JSON payload")
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(3 * (attempt + 1))
        raise NetworkError(type(last_error).__name__ if last_error else "request failed")

    async def _binding(self, user: SkUser) -> tuple[str, list | None]:
        for _ in range(3):
            payload = await self._json_get(user, BINDING_URL)
            code = payload.get("code")
            if code == 0:
                return "ok", (payload.get("data") or {}).get("list") or []
            refreshed = False
            if code == 10000:
                refreshed = await self._refresh_cred(user)
                if not refreshed:
                    refreshed = await self._refresh_account(user)
            elif code == 10002:
                refreshed = await self._refresh_account(user)
            else:
                return f"接口错误 {code}", None
            if not refreshed:
                return EXPIRED, None
        return "登录状态无法确认", None

    async def _role_state(self, user: SkUser, char: Character) -> str:
        if char.app_code == "endfield":
            if not char.role_id:
                return NO_ROLE
            url = EF_ATTENDANCE_URL
            extra = {"sk-game-role": f"3_{char.role_id}_{char.channel_master_id}"}
        else:
            url = f"{ARK_ATTENDANCE_URL}?{urlencode({'uid': char.uid, 'gameId': char.channel_master_id})}"
            extra = None
        for _ in range(2):
            payload = await self._json_get(user, url, extra)
            code = payload.get("code")
            if code == 0:
                return "ok"
            if code == 10000 and await self._refresh_cred(user):
                continue
            if code == 10002 and await self._refresh_account(user):
                continue
            if code in (10000, 10002):
                raise NetworkError("credential refresh failed")
            return f"{char.app_code} {code} {str(payload.get('message') or '')[:24]}"
        return "登录状态无法确认"

    @staticmethod
    def _binding_keys(binding: list) -> set[tuple[str, str]]:
        return {
            (str(char.get("uid", "")), str(app.get("appCode", "")))
            for app in binding
            for char in app.get("bindingList", [])
            if str(app.get("appCode", "")) in CHECKED_APPS
        }

    async def _export_for(self, session, user: SkUser, chars: list[Character], include_user: bool) -> dict:
        export = {"skland_user": [], "skland_characters": [], "skland_gacha_record": []}
        if include_user:
            export["skland_user"].append(self._row(user))
            chars = (await session.scalars(select(Character).where(Character.id == user.id))).all()
            records = (await session.scalars(select(GachaRecord).where(GachaRecord.uid == user.id))).all()
            export["skland_characters"].extend(self._row(char) for char in chars)
            export["skland_gacha_record"].extend(self._row(record) for record in records)
            return export
        for char in chars:
            export["skland_characters"].append(self._row(char))
            records = (
                await session.scalars(
                    select(GachaRecord).where(
                        GachaRecord.uid == user.id,
                        GachaRecord.char_pk_id == char.id,
                        GachaRecord.char_uid == char.uid,
                    )
                )
            ).all()
            export["skland_gacha_record"].extend(self._row(record) for record in records)
        return export

    def _write_export(self, export: dict) -> str:
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.backup_dir / f"{datetime.now(CN):%Y%m%dT%H%M%S%z}-{time.time_ns()}.json"
        path.write_text(json.dumps(export, ensure_ascii=False, default=str), "utf-8")
        os.chmod(path, 0o600)
        return path.name

    async def _remove_characters(self, session, user: SkUser, chars: list[Character]) -> None:
        for char in chars:
            await session.execute(
                delete(GachaRecord).where(
                    GachaRecord.uid == user.id,
                    GachaRecord.char_pk_id == char.id,
                    GachaRecord.char_uid == char.uid,
                )
            )
            await session.execute(
                delete(StaminaAlert).where(
                    StaminaAlert.user_id == user.id,
                    StaminaAlert.char_uid == char.uid,
                    StaminaAlert.game == char.app_code,
                )
            )
            await session.execute(delete(Character).where(Character.id == char.id, Character.uid == char.uid))

    async def _remove_user(self, session, user: SkUser) -> None:
        await session.execute(delete(GachaRecord).where(GachaRecord.uid == user.id))
        await session.execute(delete(StaminaAlert).where(StaminaAlert.user_id == user.id))
        await session.execute(delete(Character).where(Character.id == user.id))
        await session.execute(delete(SkUser).where(SkUser.id == user.id))

    async def _ensure_defaults(self, session) -> int:
        fixed = 0
        for user_id in (await session.scalars(select(SkUser.id))).all():
            for app_code in CHECKED_APPS:
                chars = (
                    await session.scalars(
                        select(Character)
                        .where(Character.id == user_id, Character.app_code == app_code)
                        .order_by(Character.uid)
                    )
                ).all()
                if chars and not any(char.isdefault for char in chars):
                    chars[0].isdefault = True
                    fixed += 1
        return fixed

    async def repair_defaults(self) -> int:
        async with await get_session() as session:
            fixed = await self._ensure_defaults(session)
            await session.commit()
            return fixed

    async def run(self, apply: bool) -> dict:
        async with self._lock:
            stats, reasons = Counter(), Counter()
            backups: list[str] = []
            started = time.time()
            async with await get_session() as session:
                account_ids = (await session.scalars(select(SkUser.id))).all()
                for account_id in account_ids:
                    user = await session.get(SkUser, account_id)
                    if user is None:
                        continue
                    stats["accounts"] += 1
                    try:
                        state, binding = await self._binding(user)
                        if state == EXPIRED:
                            stats["invalid_accounts"] += 1
                            reasons[f"账号：{state}"] += 1
                            if apply:
                                export = await self._export_for(session, user, [], include_user=True)
                                backups.append(await asyncio.to_thread(self._write_export, export))
                                await self._remove_user(session, user)
                            await session.commit()
                            continue
                        if state != "ok":
                            stats["unclear_accounts"] += 1
                            reasons[f"账号：{state}（保留）"] += 1
                            await session.commit()
                            continue

                        keys = self._binding_keys(binding or [])
                        chars = (
                            await session.scalars(
                                select(Character).where(Character.id == account_id, Character.app_code.in_(CHECKED_APPS))
                            )
                        ).all()
                        stale = [char for char in chars if (str(char.uid), str(char.app_code)) not in keys]
                        for _char in stale:
                            stats["invalid_roles"] += 1
                            reasons["角色：已从森空岛解绑"] += 1
                        invalid: list[Character] = []
                        alive = rejected = 0
                        for char in (char for char in chars if char not in stale):
                            stats["roles"] += 1
                            role_state = await self._role_state(user, char)
                            if role_state not in ("ok", NO_ROLE):
                                await asyncio.sleep(RECHECK_GAP)
                                role_state = await self._role_state(user, char)
                            if role_state == "ok":
                                alive += 1
                            elif role_state == NO_ROLE:
                                invalid.append(char)
                                stats["invalid_roles"] += 1
                                reasons[f"角色：{role_state}"] += 1
                            elif role_state.startswith(("arknights ", "endfield ")):
                                invalid.append(char)
                                rejected += 1
                                stats["invalid_roles"] += 1
                                reasons[f"角色：{role_state}"] += 1
                            else:
                                raise NetworkError("role state could not be confirmed")
                            await asyncio.sleep(GAP)

                        remove_user = rejected > 0 and alive == 0
                        if remove_user:
                            stats["empty_accounts"] += 1
                            reasons["账号：全部角色都被接口拒绝"] += 1
                        to_remove = list({(char.id, char.uid): char for char in [*stale, *invalid]}.values())
                        if apply and (remove_user or to_remove):
                            export = await self._export_for(session, user, to_remove, include_user=remove_user)
                            backups.append(await asyncio.to_thread(self._write_export, export))
                            if remove_user:
                                await self._remove_user(session, user)
                            else:
                                await self._remove_characters(session, user, to_remove)
                                await self.plugin._sync_binding_chars(
                                    session, user, CRED(cred=user.cred, token=user.cred_token)
                                )
                        await session.commit()
                    except NetworkError:
                        stats["network_skipped"] += 1
                        reasons["网络或凭据刷新失败（保留）"] += 1
                        await session.rollback()
                    except Exception as error:
                        stats["errors"] += 1
                        logger.warning("Skland health check skipped one account after %s", type(error).__name__)
                        await session.rollback()
                    await asyncio.sleep(GAP)
                if apply:
                    stats["defaults_set"] = await self._ensure_defaults(session)
                    await session.commit()
            result = {
                "apply": apply,
                "seconds": round(time.time() - started),
                "backups": backups,
                "reasons": dict(reasons),
                **dict(stats),
            }
            logger.info("Skland health check: %s", {key: value for key, value in result.items() if key != "backups"})
            return result

    @staticmethod
    def summary(result: dict) -> str:
        removed_accounts = result.get("invalid_accounts", 0) + result.get("empty_accounts", 0)
        verb = "已解除" if result["apply"] else "将会解除"
        lines = [
            f"森空岛账号体检{'（仅检查，未改动）' if not result['apply'] else ''}",
            f"检查 {result.get('accounts', 0)} 个账号、{result.get('roles', 0)} 个角色，用时 {result['seconds']} 秒",
            f"{verb} {removed_accounts} 个失效账号、{result.get('invalid_roles', 0)} 个失效角色",
        ]
        lines.extend(f"· {reason} ×{count}" for reason, count in sorted(result["reasons"].items(), key=lambda item: -item[1]))
        skipped = result.get("network_skipped", 0) + result.get("errors", 0) + result.get("unclear_accounts", 0)
        if skipped:
            lines.append(f"因网络、接口报错或异常跳过 {skipped} 个账号（保留，下次再查）")
        if result["apply"] and result["backups"]:
            lines.append(f"删除前已备份 {len(result['backups'])} 份：data/plugin_data/astrbot-plugin-priestess/backups/skland-health/")
        if not result["apply"] and (removed_accounts or result.get("invalid_roles")):
            lines.append("发送 /森空岛体检 清理 执行解除")
        return "\n".join(lines)

    async def _notify_admins(self, message: str) -> None:
        admins = self.plugin.context.get_config().get("admins_id", [])
        platform = next(
            (item for item in self.plugin.context.platform_manager.get_insts() if item.meta().name == "aiocqhttp"), None
        )
        if platform is None or not hasattr(platform, "get_client"):
            logger.info("Skland health check finished; OneBot admin notification is unavailable")
            return
        client = platform.get_client()
        for admin_id in admins:
            with contextlib.suppress(Exception):
                await client.call_action("send_private_msg", user_id=int(admin_id), message=message)

    async def run_weekly(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            now = datetime.now(CN)
            days_until_monday = (7 - now.weekday()) % 7
            next_run = (now + timedelta(days=days_until_monday)).replace(hour=4, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=7)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(1, (next_run - now).total_seconds()))
                return
            except asyncio.TimeoutError:
                pass
            try:
                result = await self.run(apply=False)
                if any(result.get(key) for key in ("invalid_accounts", "empty_accounts", "invalid_roles", "network_skipped", "errors")):
                    await self._notify_admins(self.summary(result))
            except Exception:
                logger.exception("Scheduled Skland health check failed")
