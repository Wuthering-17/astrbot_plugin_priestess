"""森空岛 COS 图片获取模块

基于 nonebot-plugin-skland-cos 的 API 实现改写。
森空岛社区 API（web/v1）使用独立的认证流程：
1. 通过 /web/v1/auth/refresh 用 cred 获取 sign token
2. 用 sign token + 时间戳 + dId 生成签名
3. 请求时 header 中携带 cred、sign、timestamp 等

游戏 API（api/v1）和社区 API（web/v1）的认证方式完全不同，
因此社区接口不走 call_api_with_refresh 的 Token 刷新逻辑。
"""

import hmac
import hashlib
import json
import random
import string
import time
import tempfile
from pathlib import Path

import httpx
from astrbot.api import logger

# 森空岛社区 API 基础配置
BASE_URL = "https://zonai.skland.com"
COSPLAY_TAG_ID = 451  # 明日方舟 cosplay 板块 tagId
GAME_ID = "1"         # 明日方舟
CATE_ID = "3"         # 同人板块
PLATFORM = "3"        # 签名 header 中的 platform
V_NAME = "1.0.0"      # 签名 header 中的 vName
TIMEOUT = 15.0

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.skland.com/",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.skland.com",
}

# 按 cred 缓存 sign token，避免频繁刷新
_sign_token_cache: dict[str, str] = {}


def _make_sign(token: str, did: str, path: str, method: str, query: str, body: str, ts: str) -> str:
    """Skland 社区 API 签名算法（逆向自前端 JS）

    sign = MD5( HMAC-SHA256(token, path + query/body + ts + JSON_headers) )
    JSON_headers = {"platform":"3","timestamp":ts,"dId":did,"vName":"1.0.0"}
    """
    msg = path
    msg += (query or "") if method.upper() == "GET" else (body or "")
    msg += ts
    hdr = {"platform": PLATFORM, "timestamp": ts, "dId": did, "vName": V_NAME}
    msg += json.dumps(hdr, separators=(",", ":"))
    raw = hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hashlib.md5(raw.encode()).hexdigest()


async def _refresh_token(client: httpx.AsyncClient, cred: str) -> str:
    """调用 /web/v1/auth/refresh 获取新的 sign token"""
    try:
        resp = await client.get(
            f"{BASE_URL}/web/v1/auth/refresh",
            headers={**_BASE_HEADERS, "cred": cred},
            timeout=TIMEOUT,
        )
        data = resp.json()
        if data.get("code") == 0:
            token = data["data"]["token"]
            _sign_token_cache[cred] = token
            logger.info(f"[SklandCos] sign token 刷新成功: {token[:12]}...")
            return token
        else:
            logger.warning(f"[SklandCos] token 刷新失败: {data.get('message')}")
    except Exception as e:
        logger.warning(f"[SklandCos] token 刷新异常: {e}")
    return _sign_token_cache.get(cred, "")


async def _signed_get(
    client: httpx.AsyncClient, cred: str, did: str, path: str, params: dict
) -> dict:
    """发送带签名的 GET 请求，token 过期时自动刷新一次"""
    token = _sign_token_cache.get(cred, "")
    if not token:
        token = await _refresh_token(client, cred)

    async def _do_request(tok: str) -> httpx.Response:
        # 先构建请求以获取实际 query string
        req = client.build_request("GET", f"{BASE_URL}{path}", params=params)
        query = str(req.url).split("?", 1)[1] if "?" in str(req.url) else ""
        ts = str(int(time.time()))
        sign = _make_sign(tok, did, path, "GET", query, "", ts)
        headers = {
            **_BASE_HEADERS,
            "cred": cred,
            "platform": PLATFORM,
            "vName": V_NAME,
            "timestamp": ts,
            "dId": did,
            "sign": sign,
        }
        return await client.get(f"{BASE_URL}{path}", params=params, headers=headers)

    resp = await _do_request(token)
    data = resp.json()

    # code=10000 表示 token 失效，刷新后重试一次
    if data.get("code") == 10000:
        logger.info("[SklandCos] token 失效，自动刷新重试")
        token = await _refresh_token(client, cred)
        resp = await _do_request(token)
        data = resp.json()

    return data


def _random_list_id() -> str:
    """生成随机 listId，用于分页追踪"""
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


async def _fetch_tag_index_page(
    client: httpx.AsyncClient, cred: str, did: str,
    tag_id: int, list_id: str, sort_type: str = "1",
) -> tuple[list[dict], bool]:
    """获取 tag/index 分页数据

    Returns:
        (帖子列表, 是否还有更多)
    """
    data = await _signed_get(
        client, cred, did,
        "/web/v1/tag/index",
        {
            "tagId": str(tag_id),
            "sortType": sort_type,
            "pageSize": "10",
            "listId": list_id,
            "gameId": "0",
        },
    )
    if data.get("code") != 0:
        logger.warning(
            f"[SklandCos] tag/index 失败: code={data.get('code')} msg={data.get('message')}"
        )
        return [], False
    payload = data.get("data", {})
    posts = payload.get("list", [])
    has_more = bool(payload.get("hasMore"))
    logger.info(f"[SklandCos] tag/index tagId={tag_id} 获取 {len(posts)} 条, hasMore={has_more}")
    return posts, has_more


async def _feed_index(
    client: httpx.AsyncClient, cred: str, did: str, limit: int = 50
) -> list[dict]:
    """获取 feed/index 数据（同人板块最新帖子）"""
    data = await _signed_get(
        client, cred, did,
        "/web/v1/feed/index",
        {"gameId": GAME_ID, "cateId": CATE_ID, "limit": str(limit)},
    )
    if data.get("code") != 0:
        logger.warning(
            f"[SklandCos] feed/index 失败: code={data.get('code')} msg={data.get('message')}"
        )
        return []
    posts = data.get("data", {}).get("list", [])
    logger.info(f"[SklandCos] feed/index 获取 {len(posts)} 条")
    return posts


def _get_hd_url(url: str) -> str:
    """去除图片 URL 中的缩略图参数，还原高清原图

    常见 CDN 缩略图参数：
    - 阿里云 OSS: ?x-oss-process=image/resize,w_xxx
    - 七牛云: ?imageView2/2/w/xxx
    - 腾讯云: ?imageMogr2/thumbnail/xxx
    """
    thumbnail_params = (
        "x-oss-process=",
        "imageView",
        "imageMogr",
        "thumbnail",
        "resize",
        "w_",
    )
    # 分离 base url 和 query string
    if "?" not in url:
        return url
    base, query = url.split("?", 1)
    # 如果 query 中包含任何缩略图参数，直接丢弃整个 query
    if any(p in query for p in thumbnail_params):
        return base
    return url


def _extract_image_entries(posts: list[dict], _debug_print_structure: bool = False) -> list[dict]:
    """从帖子列表中提取图片元数据

    返回每项包含：
    - url: 高清图片 URL（已去除缩略图参数）
    - post_url: 帖子在森空岛的链接
    - title: 帖子标题
    - author: 作者昵称
    - like_count: 点赞数（API 返回时）
    - view_count: 浏览量（API 返回时）
    - comment_count: 评论数（API 返回时）

    社区 API 的帖子结构：
    - entry["item"]["imageListSlice"]: 图片列表，每项含 url 字段
    - entry["item"]["id"]: 帖子 ID
    - entry["user"]["nickname"]: 作者昵称
    """
    entries: list[dict] = []
    seen_urls: set[str] = set()

    for idx, entry in enumerate(posts):
        if not isinstance(entry, dict):
            continue

        # 调试用：首次调用时打印一条完整的原始帖子结构，帮助确认可用字段
        if _debug_print_structure and idx == 0:
            logger.info(f"[SklandCos] 帖子结构样例: {json.dumps(entry, ensure_ascii=False, default=str)[:1200]}")

        # 提取作者信息
        user = entry.get("user", {})
        author = ""
        if isinstance(user, dict):
            author = user.get("nickname", "") or user.get("name", "")

        item = entry.get("item", {})
        if not isinstance(item, dict):
            continue

        post_id = item.get("id", "")
        post_url = f"https://www.skland.com/article?id={post_id}" if post_id else ""
        title = item.get("title", "") or ""

        # 尝试提取互动数据（字段名需根据实际 API 响应确认）
        like_count = item.get("likeCount") or item.get("like_count") or item.get("likes") or 0
        view_count = item.get("viewCount") or item.get("view_count") or item.get("views") or 0
        comment_count = item.get("commentCount") or item.get("comment_count") or item.get("comments") or 0

        for img in item.get("imageListSlice", []):
            raw_url = img.get("url", "") if isinstance(img, dict) else str(img)
            if not raw_url or not raw_url.startswith("http"):
                continue
            # 去重：基于原始 URL
            if raw_url in seen_urls:
                continue
            seen_urls.add(raw_url)
            # 还原高清图
            hd_url = _get_hd_url(raw_url)
            entries.append({
                "url": hd_url,
                "post_url": post_url,
                "title": title,
                "author": author,
                "like_count": int(like_count) if like_count else 0,
                "view_count": int(view_count) if view_count else 0,
                "comment_count": int(comment_count) if comment_count else 0,
            })
    logger.info(f"[SklandCos] 从 {len(posts)} 条帖子中提取 {len(entries)} 张图片")
    return entries


def _sort_by_popularity(entries: list[dict]) -> list[dict]:
    """按热度排序：优先点赞数，其次浏览量，其次评论数"""
    return sorted(
        entries,
        key=lambda e: (e.get("like_count", 0), e.get("view_count", 0), e.get("comment_count", 0)),
        reverse=True,
    )


def _pick_from_top_n(entries: list[dict], top_n: int = 20) -> list[dict]:
    """从热度 Top N 中随机打乱返回，避免全局随机抽到冷门低质图"""
    top = entries[:top_n] if len(entries) > top_n else entries
    random.shuffle(top)
    return top


async def fetch_cos_images(
    cred_str: str, keyword: str = "", top_n: int = 20
) -> list[dict]:
    """获取森空岛 COS 图片元数据列表

    Args:
        cred_str: 森空岛 cred（即 SK_OAUTH_CRED_KEY 的值）
        keyword: 角色关键词，如"阿米娅"
        top_n: 从热度前 N 中随机选取，默认 20

    Returns:
        图片元数据列表（已按热度排序并 Top-N 随机），每项包含
        url/post_url/title/author/like_count/view_count/comment_count。
        未找到时返回空列表。
    """
    if not cred_str:
        logger.warning("[SklandCos] cred 为空，无法获取 COS 图片")
        return []

    did = ""  # device ID，可选，留空即可

    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        # 先确保有 sign token
        await _refresh_token(client, cred_str)

        # ---------- 无关键词模式：从 cosplay 板块拉取 ----------
        if not keyword:
            result: list[dict] = []
            list_id = _random_list_id()
            # sort_type 尝试 "1"（可能为最热/推荐），失败则回退 "2"（最新）
            sort_type_candidates = ["1", "2"]
            for sort_type in sort_type_candidates:
                for _ in range(5):
                    posts, has_more = await _fetch_tag_index_page(
                        client, cred_str, did, COSPLAY_TAG_ID, list_id, sort_type=sort_type
                    )
                    if not posts:
                        break
                    result.extend(_extract_image_entries(posts, _debug_print_structure=(sort_type == "1" and _ == 0)))
                    if not has_more:
                        break
                if result:
                    logger.info(f"[SklandCos] 无关键词模式 sort_type={sort_type} 共获取 {len(result)} 张")
                    break

            if result:
                sorted_result = _sort_by_popularity(result)
                return _pick_from_top_n(sorted_result, top_n)
            logger.info("[SklandCos] 无关键词模式未获取到任何图片")
            return []

        # ---------- 有关键词模式：先尝试 feed/index 标题匹配 ----------
        posts = await _feed_index(client, cred_str, did, limit=100)
        keyword_lower = keyword.lower()
        matched: list[dict] = []
        for entry in posts:
            item = entry.get("item", {})
            title = item.get("title", "") or ""
            if keyword_lower in title.lower():
                matched.append(entry)

        result = _extract_image_entries(matched, _debug_print_structure=True)
        if result:
            sorted_result = _sort_by_popularity(result)
            logger.info(
                f"[SklandCos] 关键词'{keyword}' 匹配到 {len(result)} 张，"
                f"热度最高: 👍{sorted_result[0].get('like_count', 0)} "
                f"👁{sorted_result[0].get('view_count', 0)}"
            )
            return _pick_from_top_n(sorted_result, top_n)

        logger.info(f"[SklandCos] 关键词'{keyword}' 未匹配到任何图片")
        return result


async def _download_image(url: str) -> str:
    """下载图片到本地临时文件

    Args:
        url: 图片 URL

    Returns:
        本地文件路径
    """
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, verify=False
    ) as client:
        response = await client.get(url, headers={"Referer": "https://www.skland.com/"})
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if "png" in content_type:
            suffix = ".png"
        elif "gif" in content_type:
            suffix = ".gif"
        elif "webp" in content_type:
            suffix = ".webp"
        else:
            suffix = ".jpg"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(response.content)
            return f.name
