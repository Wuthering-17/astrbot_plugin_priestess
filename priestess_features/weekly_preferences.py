"""Persist each QQ user's choice of weekly mentions, independently per game."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot.api import logger

GAMES = {"arknights": "明日方舟", "endfield": "终末地"}
ALIASES = {"方舟": "arknights", "粥": "arknights", "舟": "arknights", "明日方舟": "arknights", "mrfz": "arknights", "终": "endfield", "终末地": "endfield", "zmd": "endfield"}


class WeeklyPreferences:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    def _load(self) -> dict | None:
        try:
            value = json.loads(self.path.read_text("utf-8"))
            if not isinstance(value, dict) or any(not isinstance(games, dict) for games in value.values()):
                raise ValueError("invalid weekly preferences")
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("[Priestess] Weekly mention preferences unavailable; suppressing mentions")
            return None

    def enabled(self, qq: str, game: str) -> bool:
        value = self._load()
        return value is not None and value.get(str(qq), {}).get(game, True) is True

    async def configure(self, qq: str, argument: str) -> str:
        words = argument.lower().split()
        if not words:
            return "\n".join(f"{label}周常 @：{'开' if self.enabled(qq, game) else '关'}" for game, label in GAMES.items()) + "\n关闭：/周常提醒 粥 关 或 /周常提醒 终 关；全部关闭：/周常提醒 关。"
        if len(words) == 1:
            games, action = list(GAMES), words[0]
        elif len(words) == 2 and words[0] in ALIASES:
            games, action = [ALIASES[words[0]]], words[1]
        else:
            return "用法：/周常提醒 粥 关、/周常提醒 终 关；把关改成开可恢复；/周常提醒 关 关闭两款游戏。"
        actions = {"开": True, "开启": True, "恢复": True, "关": False, "关闭": False, "取消": False}
        if action not in actions:
            return "请使用开或关，例如 /周常提醒 粥 关。"
        async with self.lock:
            value = self._load()
            if value is None:
                return "提醒偏好文件异常，当前已暂停周常 @，请管理员检查；不会覆盖已有设置。"
            value.setdefault(str(qq), {}).update({game: actions[action] for game in games})
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
            temporary.replace(self.path)
        labels = "、".join(GAMES[game] for game in games)
        return f"已{'开启' if actions[action] else '关闭'}你在所有群的{labels}周常 @ 提醒，不影响群周报图片。恢复请发送 /周常提醒 {'粥' if games == ['arknights'] else '终' if games == ['endfield'] else ''} 开。"
