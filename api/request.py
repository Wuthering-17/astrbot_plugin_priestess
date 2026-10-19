import hmac
import json
import hashlib
import contextlib
from typing import Literal
from datetime import datetime
from urllib.parse import urlparse

import logging

import httpx
from pydantic import TypeAdapter

from ..db import Character

logger = logging.getLogger(__name__)

from ..exception import LoginException, RequestException, UnauthorizedException
from ..schemas import (
    CRED,
    ArkCard,
    GachaCate,
    RogueData,
    BindingApp,
    EndfieldCard,
    GachaResponse,
    ArkSignResponse,
    EndfieldPoolType,
    EfCharGachaResponse,
    EndfieldSignResponse,
    EfWeaponGachaResponse,
    EfGachaContentResponse,
)

base_url = "https://zonai.skland.com/api/v1"


class SklandAPI:
    _headers = {
        "User-Agent": ("Skland/1.32.1 (com.hypergryph.skland; build:103201004; Android 33; ) Okhttp/4.11.0"),
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }

    _header_for_sign = {"platform": "", "timestamp": "", "dId": "", "vName": ""}

    @staticmethod
    def _check_response(resp_json: dict, action: str):
        """统一检查 API 响应状态码，抛出带详细信息的异常

        Args:
            resp_json: API 返回的 JSON 数据
            action: 当前操作描述（用于错误消息）

        Raises:
            UnauthorizedException: code=10000 (cred_token 失效)
            LoginException: code=10002 (access_token 失效)
            RequestException: 其他非 0 code
        """
        if status := resp_json.get("code"):
            msg = resp_json.get('message') or '无详细错误信息'
            detail = f"{action} [code={status}]：{msg}"
            if status == 10000:
                raise UnauthorizedException(detail)
            elif status == 10002:
                raise LoginException(detail)
            elif status != 0:
                raise RequestException(detail)

    @classmethod
    async def get_binding(cls, cred: CRED) -> list[BindingApp]:
        """获取绑定的角色"""
        binding_url = f"{base_url}/game/player/binding"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    binding_url,
                    headers=await cls.get_sign_header(cred, binding_url, method="get"),
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取绑定角色失败")
                return TypeAdapter(list[BindingApp]).validate_python(resp_json["data"]["list"])
            except httpx.HTTPError as e:
                raise RequestException(f"获取绑定角色失败: {e}")

    @classmethod
    async def get_sign_header(
        cls,
        cred: CRED,
        url: str,
        method: Literal["get", "post"],
        query_body: dict | None = None,
        use_did: bool = False,
    ) -> dict:
        """获取带sign请求头

        Args:
            cred: 认证凭据。
            url: 请求 URL。
            method: 请求方法。
            query_body: POST 请求体。
            use_did: 是否获取并使用设备 ID 参与签名计算。
        """
        timestamp = int(datetime.now().timestamp()) - 1
        header_for_sign = {**cls._header_for_sign}
        if use_did:
            from .dId import get_dId

            header_for_sign["dId"] = await get_dId()
        header_ca = {**header_for_sign, "timestamp": str(timestamp)}
        parsed_url = urlparse(url)
        if method == "post":
            query_params = json.dumps(query_body) if query_body is not None else ""
        else:
            query_params = parsed_url.query
        header_ca_str = json.dumps(
            {**header_for_sign, "timestamp": str(timestamp)},
            separators=(",", ":"),
        )
        secret = f"{parsed_url.path}{query_params}{timestamp}{header_ca_str}"
        hex_secret = hmac.new(cred.token.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()
        signature = hashlib.md5(hex_secret.encode("utf-8")).hexdigest()
        return {"cred": cred.cred, **cls._headers, "sign": signature, **header_ca}

    @classmethod
    async def ark_sign(cls, cred: CRED, uid: str, channel_master_id: str) -> ArkSignResponse:
        """进行明日方舟签到"""
        body = {"uid": uid, "gameId": channel_master_id}
        json_body = json.dumps(body, ensure_ascii=False, separators=(", ", ": "), allow_nan=False)
        sign_url = f"{base_url}/game/attendance"
        headers = await cls.get_sign_header(
            cred,
            sign_url,
            method="post",
            query_body=body,
        )
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    sign_url,
                    headers={**headers, "Content-Type": "application/json"},
                    content=json_body,
                )
                resp_json = response.json()
                logger.debug(f"签到回复：{resp_json}")
                cls._check_response(resp_json, f"角色 {uid} 签到失败")
                return ArkSignResponse(**resp_json["data"])
            except httpx.HTTPError as e:
                raise RequestException(f"角色 {uid} 签到请求失败: {e}")

    @classmethod
    async def get_user_ID(cls, cred: CRED) -> str:
        uid_url = f"{base_url}/user/teenager"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    uid_url,
                    headers=await cls.get_sign_header(cred, uid_url, method="get"),
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取账号 userId 失败")
                return resp_json["data"]["teenager"]["userId"]
            except httpx.HTTPError as e:
                raise RequestException(f"获取账号 userId 失败: {e}")

    @classmethod
    async def ark_card(cls, cred: CRED, uid: str) -> ArkCard:
        """获取明日方舟角色信息"""
        game_info_url = f"{base_url}/game/player/info?uid={uid}"
        # 增加超时时间到 15 秒（某些 API 响应较慢）
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(
                    game_info_url,
                    headers=await cls.get_sign_header(cred, game_info_url, method="get"),
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取账号 game_info 失败")
                return ArkCard(**resp_json["data"])
            except httpx.HTTPError as e:
                raise RequestException(f"获取账号 game_info 失败: {e}") from e

    @classmethod
    async def get_rogue(cls, cred: CRED, uid: str, topic_id: str) -> RogueData:
        """获取肉鸽数据"""
        rogue_url = f"{base_url}/game/arknights/rogue?uid={uid}&targetUserId={cred.userId}&topicId={topic_id}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    rogue_url,
                    headers=await cls.get_sign_header(cred, rogue_url, method="get"),
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取肉鸽数据失败")
                return RogueData(**resp_json["data"])
            except httpx.HTTPError as e:
                raise RequestException(f"获取肉鸽数据失败: {e}") from e

    @classmethod
    async def get_gacha_categories(cls, uid: str, role_token: str, token: str, ak_cookie: str) -> list[GachaCate]:
        """获取卡池类别"""
        gacha_categories_url = f"https://ak.hypergryph.com/user/api/inquiry/gacha/cate?uid={uid}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    gacha_categories_url,
                    headers={"X-Account-Token": token, "X-Role-Token": role_token},
                    cookies={"ak-user-center": ak_cookie},
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取抽卡类别失败")
                return [GachaCate(**item) for item in resp_json.get("data", [])]
            except httpx.HTTPError as e:
                raise RequestException(f"获取抽卡类别失败: {e}") from e

    @classmethod
    async def get_gacha_history(
        cls,
        uid: str,
        role_token: str,
        token: str,
        ak_cookie: str,
        category: str,
        size: int = 100,
        gachaTs: str | None = None,
        pos: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> GachaResponse:
        """获取明日方舟抽卡记录"""
        gacha_history_url = "https://ak.hypergryph.com/user/api/inquiry/gacha/history"
        query_params = {
            "uid": uid,
            "category": category,
            "size": size,
        }
        if gachaTs is not None and pos is not None:
            query_params["gachaTs"] = gachaTs
            query_params["pos"] = pos
        async with httpx.AsyncClient() if client is None else contextlib.nullcontext(client) as client:
            try:
                response = await client.get(
                    gacha_history_url,
                    headers={"X-Account-Token": token, "X-Role-Token": role_token},
                    cookies={"ak-user-center": ak_cookie},
                    params=query_params,
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取抽卡记录失败")
                return GachaResponse(**resp_json["data"])
            except httpx.HTTPError as e:
                raise RequestException(f"获取抽卡记录失败: {e}") from e

    @classmethod
    async def get_ef_gacha_history(
        cls,
        pool_type: EndfieldPoolType,
        server_id: str,
        role_token: str,
        seq_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> EfCharGachaResponse | EfWeaponGachaResponse:
        """获取终末地抽卡记录"""
        is_weapon = pool_type == EndfieldPoolType.WEAPON
        pool_url = "weapon" if is_weapon else "char"
        ef_gacha_url = f"https://ef-webview.hypergryph.com/api/record/{pool_url}"
        query_params: dict = {
            "token": role_token,
            "server_id": server_id,
            "lang": "zh-cn",
        }
        if not is_weapon:
            query_params["pool_type"] = pool_type.value
        if seq_id is not None:
            query_params["seq_id"] = seq_id
        async with httpx.AsyncClient() if client is None else contextlib.nullcontext(client) as client:
            try:
                response = await client.get(
                    ef_gacha_url,
                    params=query_params,
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取终末地抽卡记录失败")
                data = resp_json["data"]
                if is_weapon:
                    return EfWeaponGachaResponse(**data)
                return EfCharGachaResponse(**data)
            except httpx.HTTPError as e:
                raise RequestException(f"获取终末地抽卡记录失败: {e}") from e

    @classmethod
    async def get_ef_gacha_content(
        cls,
        pool_id: str,
        server_id: str,
    ) -> EfGachaContentResponse:
        """获取终末地卡池内容（UP角色信息）

        Args:
            pool_id: 卡池ID（如 special_xxx）。
            server_id: 服务器ID。
        """
        content_url = "https://ef-webview.hypergryph.com/api/content"
        query_params = {
            "pool_id": pool_id,
            "server_id": server_id,
            "lang": "zh-cn",
        }
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(content_url, params=query_params)
                resp_json = response.json()
                cls._check_response(resp_json, "获取终末地卡池内容失败")
                return EfGachaContentResponse(**resp_json["data"])
            except httpx.HTTPError as e:
                raise RequestException(f"获取终末地卡池内容失败: {e}") from e

    @classmethod
    async def endfield_sign(cls, cred: CRED, role_id: str, server_id: str) -> EndfieldSignResponse:
        """进行明日方舟：终末地签到"""
        sign_url = "https://zonai.skland.com/web/v1/game/endfield/attendance"
        headers = await cls.get_sign_header(
            cred,
            sign_url,
            method="post",
            query_body=None,
        )
        game_role = f"3_{role_id}_{server_id}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    sign_url,
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                        "sk-game-role": game_role,
                    },
                )
                resp_json = response.json()
                logger.debug(f"终末地签到回复：{resp_json}")
                cls._check_response(resp_json, f"角色 {role_id} 终末地签到失败")
            except httpx.HTTPError as e:
                raise RequestException(f"角色 {role_id} 终末地签到失败: {e}") from e
            return EndfieldSignResponse(**resp_json["data"])

    @classmethod
    async def endfield_card(cls, cred: CRED, uid: str, char: Character) -> EndfieldCard:
        """获取终末地角色信息"""
        game_info_url = f"https://zonai.skland.com/web/v1/game/endfield/card/detail?roleId={char.role_id}&serverId={char.channel_master_id}&userId={uid}"
        headers = await cls.get_sign_header(
            cred,
            game_info_url,
            method="get",
        )
        # 增加超时时间到 15 秒（某些 API 响应较慢）
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(
                    game_info_url,
                    headers=headers,
                )
                resp_json = response.json()
                cls._check_response(resp_json, "获取终末地角色卡片失败")
                return EndfieldCard(**resp_json["data"]["detail"])
            except httpx.HTTPError as e:
                raise RequestException(f"获取终末地角色卡片失败: {e}") from e
