"""渲染适配层：将原插件的 Jinja2 模板渲染为图片

原插件使用 nonebot-plugin-htmlrender 的 template_to_pic（基于 Playwright）。
AstrBot 也可能提供 html_render（基于 Playwright）。

本模块优先使用 AstrBot 的 html_render，如果不可用则使用内置 Playwright fallback。
"""

from astrbot.api import logger
import base64
import asyncio
import hashlib
import html
import time
from io import BytesIO
from urllib.parse import unquote, urlsplit
import jinja2
import mimetypes
import re
from pathlib import Path
from datetime import datetime

# Pillow 用于截图后裁剪右侧空白
from PIL import Image

from .browser import launch_browser

from .schemas import (
    ArkCard,
    EndfieldCard,
    RogueData,
    Clue,
    GroupedGachaRecord,
    EfGroupedGachaRecord,
    Status,
    PlayerBase,
)
from .config import CACHE_DIR, RES_DIR, TEMPLATES_DIR
from .filters import (
    format_timestamp,
    time_to_next_4am,
    time_to_next_monday_4am,
    format_stamina_time,
    format_date_ymd,
    get_domain_info,
    get_rarity_color,
    get_equip_rarity_color,
    get_profession_icon,
    get_property_icon,
    format_money_wan,
    format_timestamp_str,
    format_timestamp_md,
    charId_to_avatarUrl,
    charId_to_portraitUrl,
    ef_charId_to_avatarUrl,
    loads_json,
)

# 创建带自定义过滤器的 Jinja2 环境（与原插件保持一致）
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    enable_async=True,
)

# 方舟/终末地卡片过滤器
_jinja_env.filters["format_timestamp"] = format_timestamp
_jinja_env.filters["time_to_next_4am"] = time_to_next_4am
_jinja_env.filters["time_to_next_monday_4am"] = time_to_next_monday_4am
# 终末地卡片过滤器
_jinja_env.filters["format_stamina_time"] = format_stamina_time
_jinja_env.filters["format_date_ymd"] = format_date_ymd
_jinja_env.filters["get_domain_info"] = get_domain_info
_jinja_env.filters["get_rarity_color"] = get_rarity_color
_jinja_env.filters["get_equip_rarity_color"] = get_equip_rarity_color
_jinja_env.filters["get_profession_icon"] = get_profession_icon
_jinja_env.filters["get_property_icon"] = get_property_icon
_jinja_env.filters["format_money_wan"] = format_money_wan
# 肉鸽/抽卡过滤器
_jinja_env.filters["format_timestamp_str"] = format_timestamp_str
_jinja_env.filters["format_timestamp_md"] = format_timestamp_md
_jinja_env.filters["charId_to_avatarUrl"] = charId_to_avatarUrl
_jinja_env.filters["charId_to_portraitUrl"] = charId_to_portraitUrl
_jinja_env.filters["ef_charId_to_avatarUrl"] = ef_charId_to_avatarUrl
_jinja_env.filters["loads_json"] = loads_json


def _make_width_clamp_style(width: int = 706) -> str:
    """生成强制页面宽度的注入样式（解决 viewport 无法设置的问题）"""
    return f"""
<style id="astrbot-render-fix">
  html, body {{
    width: {width}px !important;
    min-width: {width}px !important;
    max-width: {width}px !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow-x: hidden !important;
  }}
</style>
"""


def _inline_resources(html_content: str, base_dir: Path) -> str:
    """将 HTML 中的本地资源路径替换为 base64 data URL。

    AstrBot 的 html_render 使用外部网络渲染服务，无法访问本地 file:// 资源。
    通过将图片、字体等资源内联为 base64，确保外部服务能正确渲染所有元素。
    """

    def _resolve_path(path: str) -> Path | None:
        """解析相对路径为绝对路径"""
        # 跳过已经是远程 URL 或 data URL 的路径
        if path.startswith(("http://", "https://", "data:")):
            return None
        if path.startswith("../"):
            full = (base_dir / path).resolve()
        elif path.startswith("./"):
            full = (base_dir / path[2:]).resolve()
        elif path.startswith("file://"):
            raw = unquote(urlsplit(path).path)
            if re.match(r"^/[A-Za-z]:/", raw):
                raw = raw[1:]
            full = Path(raw)
        else:
            full = (base_dir / path).resolve()
        return full if full.exists() else None

    def _to_data_url(path: str) -> str:
        full = _resolve_path(path)
        if not full:
            return path
        mime, _ = mimetypes.guess_type(str(full))
        if not mime:
            mime = "application/octet-stream"
        data = base64.b64encode(full.read_bytes()).decode()
        return f"data:{mime};base64,{data}"

    # 替换 img src="..."
    def _replace_img(match: re.Match) -> str:
        return f'src="{_to_data_url(match.group(1))}"'

    html_content = re.sub(r'src="([^"]+)"', _replace_img, html_content)

    # 替换 CSS url("...") 和 url('...')
    def _replace_url(match: re.Match) -> str:
        return f'url("{_to_data_url(match.group(1))}")'

    html_content = re.sub(r'url\("([^"]+)"\)', _replace_url, html_content)
    html_content = re.sub(r"url\('([^']+)'\)", _replace_url, html_content)
    html_content = re.sub(r'url\(([^\s\x22\x27][^)]*)\)', lambda match: f'url("{_to_data_url(match.group(1).strip())}")', html_content)

    return html_content


async def _inline_remote_images(html_content: str) -> str:
    """Fetch images locally so the remote HTML renderer needs no image-host access."""
    import httpx

    sources = set(re.findall(r'src="(https?://[^\"]+)"', html_content))
    semaphore = asyncio.Semaphore(6)
    image_cache = CACHE_DIR / "remote_images"
    image_cache.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        async def fetch(source: str) -> tuple[str, str]:
            url = html.unescape(source)
            path = image_cache / hashlib.sha256(url.encode()).hexdigest()
            async with semaphore:
                try:
                    if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
                        data = path.read_bytes()
                    else:
                        response = await client.get(url)
                        response.raise_for_status()
                        data = response.content
                    with Image.open(BytesIO(data)) as image:
                        mime = Image.MIME.get(image.format)
                        image.verify()
                    if not mime:
                        raise ValueError("unknown image format")
                    path.write_bytes(data)
                    return source, f"data:{mime};base64,{base64.b64encode(data).decode()}"
                except (httpx.HTTPError, OSError, ValueError) as error:
                    logger.warning("[Priestess] 抽卡图片资源加载失败 %s: %s", url, type(error).__name__)
                    return source, source

        replacements = dict(await asyncio.gather(*(fetch(source) for source in sources)))
    return re.sub(r'src="(https?://[^\"]+)"', lambda match: f'src="{replacements[match.group(1)]}"', html_content)


def _crop_image(image_path: str, html_content: str, width: int, device_scale_factor: float) -> str:
    """使用 Pillow 裁剪图片右侧空白。

    外部渲染服务可能使用固定 viewport 宽度，导致图片宽度大于实际内容宽度。
    本函数根据 HTML 中的 CSS 尺寸或传入参数推断真实内容宽度并裁剪。

    Args:
        image_path: 图片文件路径
        html_content: 原始 HTML 内容，用于提取 CSS 固定高度以推断 dsf
        width: 调用方传入的期望宽度（CSS 像素）
        device_scale_factor: 传入的设备缩放因子（服务可能忽略）

    Returns:
        裁剪后的图片路径（若无需裁剪则返回原路径）
    """
    try:
        with Image.open(image_path) as img:
            W_act, H_act = img.size

            # 1. 从 HTML 中提取固定 CSS 高度，用于精确推断实际 dsf
            h_match = re.search(r'h-\[(\d+(?:\.\d+)?)px\]', html_content)
            css_height = float(h_match.group(1)) if h_match else 0

            if css_height > 0:
                # 有固定高度模板（如 ark_card 1160px），dsf = 实际高度 / CSS 高度
                dsf = H_act / css_height
            else:
                # 无固定高度（h-auto），通过宽度启发式推断 dsf
                expected_w = width * device_scale_factor
                # 若实际宽度接近预期（容差 10% 或 50px），说明服务使用了传入的 dsf
                if abs(W_act - expected_w) <= max(width * 0.1, 50):
                    dsf = device_scale_factor
                else:
                    # 服务可能使用固定 viewport 而忽略了 dsf，按 dsf=1.0 处理
                    dsf = 1.0

            target_width = int(width * dsf)

            # 只有当实际宽度明显大于目标宽度时才裁剪，避免误裁
            if W_act > target_width + 30:
                cropped = img.crop((0, 0, target_width, H_act))
                cropped.save(image_path, "PNG")
    except Exception:
        # 裁剪失败不应阻断主流程，静默回退
        pass

    return image_path


async def _html_to_pic(
    html_content: str,
    width: int = 706,
    height: int = 1,
    full_page: bool = True,
    device_scale_factor: float = 1.5,
    base_url: str | None = None,
) -> str:
    from playwright.async_api import TimeoutError as PageTimeout

    for attempt in range(2):
        try:
            return await _html_to_pic_once(html_content, width, height, full_page, device_scale_factor, base_url)
        except PageTimeout as error:
            if attempt:
                raise RuntimeError("生成图片超时（图片素材加载太慢），请稍后再试") from error
            logger.info("[Priestess] Picture render timed out, trying once more")


async def _html_to_pic_once(
    html_content: str,
    width: int = 706,
    height: int = 1,
    full_page: bool = True,
    device_scale_factor: float = 1.5,
    base_url: str | None = None,
) -> str:
    """使用 Playwright 将 HTML 渲染为图片，返回图片路径。

    作为 AstrBot html_render 的 fallback，当 star.html_render 不可用时调用。

    Args:
        html_content: HTML 内容
        width: 视口宽度（默认 706）
        height: 视口高度（默认 1 表示自适应）
        full_page: 是否截取全页面（默认 True）
        device_scale_factor: 设备缩放因子（默认 1.5，提高清晰度）
        base_url: 基础 URL，用于解析相对路径资源
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright 未安装，无法渲染卡片。"
            "请运行: pip install playwright && playwright install chromium"
        )

    output_path = CACHE_DIR
    output_path.mkdir(parents=True, exist_ok=True)
    file_name = output_path / f"skland_card_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.png"

    async with async_playwright() as p:
        browser = await launch_browser(p)
        try:
            # 将 base_url 传给 new_page，使模板中的相对路径资源（CSS、图片）能正确解析
            page = await browser.new_page(
                viewport={"width": width, "height": max(height, 1)},
                device_scale_factor=device_scale_factor,
                base_url=base_url or f"file://{TEMPLATES_DIR}",
            )
            await page.set_content(
                html_content,
                wait_until="networkidle",
            )
            # 等待布局稳定，确保所有 CSS 计算完成
            await page.wait_for_timeout(300)
            # 获取内容实际尺寸，避免 full_page 截取到多余空白
            dimensions = await page.evaluate("""() => {
                const body = document.body;
                const html = document.documentElement;
                return {
                    width: Math.max(body.scrollWidth, body.offsetWidth, html.clientWidth, html.scrollWidth, html.offsetWidth),
                    height: Math.max(body.scrollHeight, body.offsetHeight, html.clientHeight, html.scrollHeight, html.offsetHeight)
                };
            }""")
            # 设置 viewport 匹配内容尺寸，确保截图完整
            await page.set_viewport_size({
                "width": max(dimensions["width"], width),
                "height": max(dimensions["height"], 1)
            })
            await page.screenshot(
                path=str(file_name),
                full_page=True,
            )
        finally:
            await browser.close()

    return str(file_name)


async def _try_star_html_render(
    star,
    html_content: str,
    width: int = 706,
    height: int = 1,
    full_page: bool = True,
    device_scale_factor: float = 1.5,
    base_url: str | None = None,
) -> str:
    """优先尝试 AstrBot 的 html_render，失败则使用内置 Playwright fallback。

    返回值始终是本地文件路径（因为 event.image_result 需要路径）。
    """


    html_content = _inline_resources(html_content, TEMPLATES_DIR)
    html_content = await _inline_remote_images(html_content)

    # 检查 star 是否有 html_render 方法
    if hasattr(star, "html_render"):
        try:
            # 将本地资源内联为 base64，使外部渲染服务能正确加载图片、字体
            inlined_html = _inline_resources(html_content, TEMPLATES_DIR)
            wrapper = "{{ html | safe }}"
            # 传递高质量渲染参数，覆盖默认的 jpeg/quality=40
            # 同时传递 viewport 尺寸，防止外部服务使用默认宽度导致右侧空白
            url = await star.html_render(
                wrapper,
                {"html": inlined_html},
                options={
                    "type": "png",
                    "quality": 100,
                    "full_page": True,
                    "width": width,
                    "deviceScaleFactor": device_scale_factor,
                },
            )
            logger.debug(f"[Skland] html_render 返回: {url}")
            # 统一获取本地图片路径
            if url.startswith("file://"):
                local_path = url[7:]
            elif url.startswith("http"):
                # URL 形式，下载到本地缓存
                logger.info(f"[Skland] html_render 返回 URL，正在下载图片...")
                import httpx

                async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    output_path = CACHE_DIR
                    output_path.mkdir(parents=True, exist_ok=True)
                    file_name = (
                        output_path
                        / f"skland_card_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.png"
                    )
                    file_name.write_bytes(resp.content)
                    local_path = str(file_name)
                    logger.info(f"[Skland] 图片下载完成: {local_path}")
            else:
                # 假设是本地路径
                local_path = url

            # 裁剪右侧可能存在的空白（外部服务固定 viewport 导致）
            _crop_image(local_path, html_content, width, device_scale_factor)
            return local_path
        except Exception as e:
            logger.warning(f"[Skland] html_render 失败，使用 Playwright fallback: {e}")

    return await _html_to_pic(
        html_content,
        width=width,
        height=height,
        full_page=full_page,
        device_scale_factor=device_scale_factor,
        base_url=base_url,
    )


# ==================== 方舟卡片 ====================


async def render_ark_card(star, card_data: ArkCard, bg_path: str) -> str:
    """渲染明日方舟角色卡片为图片路径"""


    logger.debug("[Skland] 开始渲染方舟卡片模板...")
    template = _jinja_env.get_template("ark_card.html.jinja2")

    rendered_html = await template.render_async(
        now_ts=datetime.now().timestamp(),
        background_image=bg_path,
        status=card_data.status,
        employed_chars=len(card_data.chars),
        skins=len(card_data.skins),
        building=card_data.building,
        medals=card_data.medal.total,
        assist_chars=card_data.assistChars,
        recruit_finished=card_data.recruit_finished,
        recruit_max=len(card_data.recruit),
        recruit_complete_time=card_data.recruit_complete_time,
        campaign=card_data.campaign,
        routine=card_data.routine,
        tower=card_data.tower,
        training_char=card_data.trainee_char,
    )
    logger.debug("[Skland] 模板渲染完成")

    width_clamp = _make_width_clamp_style(706)
    if "</head>" in rendered_html:
        rendered_html = rendered_html.replace("</head>", width_clamp + "</head>")
    else:
        rendered_html = width_clamp + rendered_html

    logger.debug("[Skland] 开始截图渲染...")
    result = await _try_star_html_render(
        star,
        rendered_html,
        width=706,
        height=1,  # 自适应高度，与原插件一致
        full_page=True,
        device_scale_factor=1.5,
        base_url=f"file://{TEMPLATES_DIR}",
    )
    logger.debug(f"[Skland] 截图完成: {result}")
    return result


# ==================== 终末地卡片 ====================


async def render_ef_card(
    star,
    card_data: EndfieldCard,
    bg_path: str,
    show_all: bool = True,
    is_simple: bool = False,
) -> str:
    """渲染终末地角色卡片为图片路径"""
    if show_all:
        filtered_chars = card_data.chars
    else:
        char_map = {char.charData.id: char for char in card_data.chars}
        filtered_chars = [char_map[cid] for cid in card_data.config.charIds if cid in char_map]

    control_center_level = 0
    for room in card_data.spaceShip.rooms:
        if room.type == 0:
            control_center_level = room.level
            break

    total_trchest_count = sum(
        collection.trchestCount
        for domain in card_data.domain
        for collection in domain.collections
    )
    total_puzzle_count = sum(
        collection.puzzleCount
        for domain in card_data.domain
        for collection in domain.collections
    )

    current_ts = float(card_data.currentTs) if card_data.currentTs else datetime.now().timestamp()
    max_ts = float(card_data.dungeon.maxTs) if card_data.dungeon.maxTs else current_ts
    stamina_remaining_seconds = max(0, max_ts - current_ts)

    cur_stamina = int(card_data.dungeon.curStamina) if card_data.dungeon.curStamina else 0
    max_stamina = int(card_data.dungeon.maxStamina) if card_data.dungeon.maxStamina else 1
    stamina_percent = min(100, (cur_stamina / max_stamina) * 100) if max_stamina > 0 else 0

    simple_bg = str(RES_DIR / "images" / "background" / "endfield" / "simple" / "simple_bg.png")
    simple_bg_top = str(
        RES_DIR / "images" / "background" / "endfield" / "simple" / "simple_bg_top.png"
    )

    template = _jinja_env.get_template("endfield_card.html.jinja2")

    rendered_html = await template.render_async(
        now_ts=datetime.now().timestamp(),
        background_image=bg_path,
        simple_bg_enabled=is_simple,
        simple_bg=simple_bg,
        simple_bg_top=simple_bg_top,
        chars=filtered_chars,
        base=card_data.base,
        dungeon=card_data.dungeon,
        bpSystem=card_data.bpSystem,
        dailyMission=card_data.dailyMission,
        weeklyMission=card_data.weeklyMission,
        achieve=card_data.achieve,
        domain=card_data.domain,
        control_center_level=control_center_level,
        total_trchest_count=total_trchest_count,
        total_puzzle_count=total_puzzle_count,
        stamina_remaining_seconds=stamina_remaining_seconds,
        stamina_percent=stamina_percent,
    )

    width_clamp = _make_width_clamp_style(706)
    if "</head>" in rendered_html:
        rendered_html = rendered_html.replace("</head>", width_clamp + "</head>")
    else:
        rendered_html = width_clamp + rendered_html

    return await _try_star_html_render(
        star,
        rendered_html,
        width=706,
        height=1,  # 自适应高度，与原插件一致
        full_page=True,
        device_scale_factor=1.5,
        base_url=f"file://{TEMPLATES_DIR}",
    )


# ==================== 肉鸽战绩 ====================


async def render_rogue_card(star, card_data: RogueData, bg_path: str) -> str:
    """渲染明日方舟肉鸽战绩卡片为图片路径"""
    template = _jinja_env.get_template("rogue.html.jinja2")

    rendered_html = await template.render_async(
        background_image=bg_path,
        topic_img=card_data.topic_img,
        topic=card_data.topic,
        now_ts=datetime.now().timestamp(),
        career=card_data.career,
        game_user_info=card_data.gameUserInfo,
        history=card_data.history,
    )

    width_clamp = _make_width_clamp_style(2200)
    if "</head>" in rendered_html:
        rendered_html = rendered_html.replace("</head>", width_clamp + "</head>")
    else:
        rendered_html = width_clamp + rendered_html

    return await _try_star_html_render(
        star,
        rendered_html,
        width=2200,
        height=1,
        full_page=True,
        device_scale_factor=1.5,
        base_url=f"file://{TEMPLATES_DIR}",
    )


async def render_rogue_info(
    star, card_data: RogueData, bg_path: str, record_id: int, is_favored: bool
) -> str:
    """渲染明日方舟肉鸽战绩详情为图片路径"""
    template = _jinja_env.get_template("rogue_info.html.jinja2")

    # 获取指定记录
    if is_favored and record_id - 1 < len(card_data.history.favourRecords):
        record = card_data.history.favourRecords[record_id - 1]
    elif record_id - 1 < len(card_data.history.records):
        record = card_data.history.records[record_id - 1]
    else:
        record = None

    rendered_html = await template.render_async(
        id=record_id,
        record=record,
        is_favored=is_favored,
        background_image=bg_path,
        topic_img=card_data.topic_img,
        topic=card_data.topic,
        now_ts=datetime.now().timestamp(),
        career=card_data.career,
        game_user_info=card_data.gameUserInfo,
        history=card_data.history,
    )

    width_clamp = _make_width_clamp_style(1100)
    if "</head>" in rendered_html:
        rendered_html = rendered_html.replace("</head>", width_clamp + "</head>")
    else:
        rendered_html = width_clamp + rendered_html

    return await _try_star_html_render(
        star,
        rendered_html,
        width=1100,
        height=1,
        full_page=True,
        device_scale_factor=1.5,
        base_url=f"file://{TEMPLATES_DIR}",
    )


# ==================== 线索看板 ====================


async def render_clue_board(star, clue_data: Clue) -> str:
    """渲染线索看板为图片路径"""
    template = _jinja_env.get_template("clue.html.jinja2")

    rendered_html = await template.render_async(clue=clue_data)

    width_clamp = _make_width_clamp_style(1100)
    if "</head>" in rendered_html:
        rendered_html = rendered_html.replace("</head>", width_clamp + "</head>")
    else:
        rendered_html = width_clamp + rendered_html

    return await _try_star_html_render(
        star,
        rendered_html,
        width=1100,
        height=1,
        full_page=True,
        device_scale_factor=1.5,
        base_url=f"file://{TEMPLATES_DIR}",
    )


# ==================== 抽卡记录 ====================


async def render_gacha_history(
    star,
    record: GroupedGachaRecord,
    char,
    status: Status,
    begin: int | None = None,
    limit: int | None = None,
) -> str:
    """渲染明日方舟抽卡记录为图片路径"""
    template = _jinja_env.get_template("gacha.html.jinja2")

    rendered_html = await template.render_async(
        record=record,
        character=char,
        status=status,
        start_index=begin,
        end_index=limit,
    )

    width_clamp = _make_width_clamp_style(720)
    if "</head>" in rendered_html:
        rendered_html = rendered_html.replace("</head>", width_clamp + "</head>")
    else:
        rendered_html = width_clamp + rendered_html

    return await _try_star_html_render(
        star,
        rendered_html,
        width=720,
        height=1,
        full_page=True,
        device_scale_factor=1.5,
        base_url=f"file://{TEMPLATES_DIR}",
    )


async def render_ef_gacha_history(
    star,
    record: EfGroupedGachaRecord,
    player: PlayerBase,
    char,
    begin: int | None = None,
    limit: int | None = None,
) -> str:
    """渲染终末地抽卡记录为图片路径"""
    template = _jinja_env.get_template("ef_gacha.html.jinja2")

    rendered_html = await template.render_async(
        avatar_url=player.avatarUrl,
        record=record,
        special_up_known=all(pool.up_six_chars for pool in record.special_pools),
        weapon_up_known=all(pool.up_six_chars for pool in record.weapon_pools),
        character=char,
        start_index=begin,
        end_index=limit,
    )

    width_clamp = _make_width_clamp_style(800)
    if "</head>" in rendered_html:
        rendered_html = rendered_html.replace("</head>", width_clamp + "</head>")
    else:
        rendered_html = width_clamp + rendered_html

    return await _try_star_html_render(
        star,
        rendered_html,
        width=800,
        height=1,
        full_page=True,
        device_scale_factor=1.5,
        base_url=f"file://{TEMPLATES_DIR}",
    )
