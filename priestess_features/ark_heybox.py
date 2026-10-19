"""Arknights Heybox export import; format based on FrostN0v0/nonebot-plugin-skland."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from ..data_source import gacha_table_data
from ..db import GachaRecord, get_gacha_records, get_session, get_user_by_platform
from ..exception import RequestException

MAX_BYTES = 5 * 1024 * 1024
POOL_RULES = {
    "中坚寻访": {4, 6, 10}, "标准寻访": {0, 9}, "中坚甄选": {6},
    "联合行动": {0}, "常驻标准寻访": {0}, "【联合行动】特选干员定向寻访": {0},
    "进攻-防守-战术交汇": {2}, "前路回响": {0},
}


async def download_export(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(4):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError("请使用小黑盒“数据管理”生成的 HTTPS 导出链接。")
            addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
                raise ValueError("导出链接必须指向公网文件。")
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("导出链接跳转无效。")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise ValueError("导出文件超过 5 MB，暂不支持。")
                payload = json.loads(content)
                if not isinstance(payload, dict):
                    raise ValueError("文件不是小黑盒明日方舟导出格式。")
                return payload
    raise ValueError("导出链接跳转次数过多，请重新导出。")


def parse_records(payload: dict, user_id: int, character) -> list[GachaRecord]:
    info, data = payload.get("info"), payload.get("data")
    if not isinstance(info, dict) or not isinstance(data, dict):
        raise ValueError("文件格式不正确：需要小黑盒明日方舟“数据管理”导出的 JSON。")
    if str(info.get("uid", "")) != str(character.uid):
        raise ValueError("导出记录的游戏 UID 与当前方舟角色不一致。请使用本人的导出链接，或追加 -r 角色序号。")
    chars = {char.name: char.char_id for char in gacha_table_data.character_table}
    records = []
    for timestamp, batch in data.items():
        if not isinstance(timestamp, str) or not timestamp.isdecimal() or not 0 < int(timestamp) < 10**11:
            raise ValueError("抽卡时间格式不正确，未导入任何记录。")
        if not isinstance(batch, dict) or not isinstance(batch.get("p"), str) or not isinstance(batch.get("c"), list):
            raise ValueError("抽卡批次格式不正确，未导入任何记录。")
        ts, pool_name = int(timestamp), batch["p"]
        if not 1 <= len(batch["c"]) <= 10:
            raise ValueError("抽卡批次数量不正确，未导入任何记录。")
        available = [pool for pool in gacha_table_data.gacha_table if pool.openTime <= ts <= pool.endTime]
        matches = [pool for pool in available if pool.gachaPoolName == pool_name]
        if not matches:
            matches = [pool for pool in available if pool.gachaRuleType in POOL_RULES.get(pool_name, set())]
        if len(matches) != 1:
            raise ValueError(f"无法唯一识别卡池「{pool_name}」，请先更新方舟卡池资料；未导入任何记录。")
        for pos, item in enumerate(batch["c"]):
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError("干员记录格式不正确，未导入任何记录。")
            name, rarity, is_new = item
            if not isinstance(name, str) or type(rarity) is not int or not 0 <= rarity <= 5 or type(is_new) not in (bool, int) or is_new not in (0, 1):
                raise ValueError("干员星级或新干员标记不正确，未导入任何记录。")
            name = "麒麟R夜刀" if name == "麒麟X夜刀" else name
            if name not in chars:
                raise ValueError(f"方舟资料中没有干员「{name}」，请先更新资料；未导入任何记录。")
            records.append(GachaRecord(
                uid=user_id, char_pk_id=character.id, char_uid=character.uid, app_code="arknights", item_type="char",
                pool_id=matches[0].gachaPoolId, pool_name=matches[0].gachaPoolName,
                char_id=chars[name], char_name=name, rarity=rarity, is_new=bool(is_new), is_free=False,
                gacha_ts=ts, pos=pos,
            ))
    return records


class ArkHeyboxImporter:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._lock = asyncio.Lock()

    async def import_history(self, event, url: str) -> str:
        if not url:
            return "用法：/方舟导入小黑盒 导出链接 [-r 角色序号]\n在小黑盒的明日方舟抽卡分析页点击“数据管理”，导出并复制链接。这里需要导出链接，不是小黑盒 ID。"
        if self._lock.locked():
            return "当前正在导入明日方舟记录，请稍后再试。"
        async with self._lock:
            async with await get_session() as session:
                user = await get_user_by_platform(session, event.get_sender_id())
                if user is None:
                    return "请先发送 /skl绑定 绑定森空岛账号。"
                character = await self.plugin._selected_character(session, user, "arknights", event)
                if character is None:
                    return "没有找到选定的明日方舟角色，请发送 /skl角色 查看角色序号。"
                try:
                    payload = await download_export(url)
                    # Check ownership before downloading game tables.
                    info = payload.get("info")
                    if not isinstance(info, dict) or str(info.get("uid", "")) != str(character.uid):
                        return "导出记录的游戏 UID 与当前方舟角色不一致，未导入。可追加 -r 角色序号选择自己的其他方舟角色。"
                    if not gacha_table_data.gacha_table or not gacha_table_data.character_table:
                        await gacha_table_data.load()
                    records = parse_records(payload, user.id, character)
                except (httpx.HTTPError, OSError):
                    return "无法下载导出文件，请检查链接是否过期及机器人网络后重试。"
                except RequestException:
                    return "方舟卡池资料加载失败，请管理员检查机器人访问游戏资料源的网络后重试。"
                except (ValueError, TypeError, KeyError) as error:
                    if isinstance(error, json.JSONDecodeError):
                        return "导出链接没有返回有效 JSON，请使用小黑盒“数据管理”中的文件下载链接。"
                    return str(error)
                existing = {
                    (record.gacha_ts, record.pos): record
                    for record in await get_gacha_records(session, user.id, character.uid)
                    if record.app_code == "arknights"
                }
                additions = []
                for record in records:
                    old = existing.get((record.gacha_ts, record.pos))
                    if old:
                        if old.char_name != record.char_name or old.rarity != record.rarity or old.pool_id != record.pool_id:
                            return "导出记录与已有记录存在冲突，本次未导入任何记录，请确认导出文件及角色是否正确。"
                        continue
                    additions.append(record)
                session.add_all(additions)
                await session.commit()
                return f"明日方舟导入成功：读取 {len(records)} 条，新增 {len(additions)} 条，重复 {len(records) - len(additions)} 条。\n发送 /明日方舟抽卡记录 查看统计图片。"
