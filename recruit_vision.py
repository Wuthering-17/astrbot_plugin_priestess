"""LLM 视觉识别模块：从公招截图中识别标签

利用 AstrBot 已配置的 LLM Provider（多模态模型）识别截图中的 5 个公招标签。
无需本地 OCR 依赖，准确度由模型决定。
"""

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("astrbot")

# 公招标签完整列表（用于提示模型，提高识别准确度）
_ALL_RECRUIT_TAGS = [
    "新手", "资深干员", "高级资深干员",
    "远程位", "近战位",
    "先锋", "近卫", "狙击", "重装", "医疗", "辅助", "术师", "特种",
    "治疗", "输出", "支援", "群攻", "减速", "生存", "防护",
    "削弱", "位移", "控场", "爆发", "召唤", "快速复活", "费用回复", "支援机械",
]

_RECRUIT_VISION_PROMPT = f"""你是一个明日方舟公招标签识别助手。

请仔细查看用户提供的公招界面截图，识别其中显示的 **5 个标签**。

公招标签只能从以下列表中出现（不会超出这个范围）：
{_ALL_RECRUIT_TAGS}

注意：
- 职业标签通常显示为"XX干员"（如"近卫干员"），你只需要返回"近卫"即可
- 如果截图模糊或无法识别某个标签，用 "未知" 占位
- 必须返回恰好 5 个标签，按截图中从左到右/从上到下的顺序

请严格按以下 JSON 格式返回，不要添加任何其他文字：
{{"tags": ["标签1", "标签2", "标签3", "标签4", "标签5"]}}
"""


@dataclass
class VisionResult:
    """视觉识别结果"""

    tags: list[str]
    raw_response: str


async def recognize_tags_from_image(
    context,
    image_url: str,
    provider_id: str | None = None,
) -> VisionResult:
    """使用 LLM 视觉能力识别公招截图中的标签

    Args:
        context: AstrBot Context 对象
        image_url: 图片路径或 URL（支持 http/https/file 协议及本地绝对路径）
        provider_id: 指定的 LLM Provider ID，None 则使用当前会话默认 Provider

    Returns:
        识别结果，包含 tags 列表和原始响应文本

    Raises:
        RuntimeError: 当 LLM 调用失败或返回格式异常时
    """
    # 获取 provider ID
    if not provider_id:
        # 尝试获取默认 provider
        prov = context.get_using_provider()
        if not prov:
            raise RuntimeError("未配置可用的 LLM Provider，无法识别图片")
        provider_id = prov.meta().id if hasattr(prov, "meta") else getattr(prov, "id", None)
        if not provider_id:
            raise RuntimeError("无法获取 Provider ID")

    logger.info(f"[RecruitVision] 使用 Provider '{provider_id}' 识别公招标签")

    try:
        llm_resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=_RECRUIT_VISION_PROMPT,
            image_urls=[image_url],
            system_prompt="你是一个精准的OCR助手，只返回JSON格式的标签识别结果。",
        )
    except Exception as e:
        logger.error(f"[RecruitVision] LLM 调用失败: {e}")
        raise RuntimeError(f"LLM 视觉识别失败: {e}")

    raw_text = llm_resp.completion_text.strip()
    logger.debug(f"[RecruitVision] LLM 原始响应: {raw_text}")

    # 解析 JSON
    tags = _extract_tags_from_response(raw_text)

    return VisionResult(tags=tags, raw_response=raw_text)


def _extract_tags_from_response(text: str) -> list[str]:
    """从 LLM 响应中提取标签列表

    尝试多种解析策略：
    1. 直接解析 JSON
    2. 从 markdown 代码块中提取 JSON
    3. 从文本中按行提取标签
    """
    # 策略1：直接 JSON 解析
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "tags" in data:
            return [str(t).strip() for t in data["tags"] if str(t).strip()]
    except json.JSONDecodeError:
        pass

    # 策略2：从 markdown 代码块提取
    code_block = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if code_block:
        try:
            data = json.loads(code_block.group(1).strip())
            if isinstance(data, dict) and "tags" in data:
                return [str(t).strip() for t in data["tags"] if str(t).strip()]
        except (json.JSONDecodeError, AttributeError):
            pass

    # 策略3：从文本行中提取（ fallback ）
    tags: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        # 跳过空行和常见非标签内容
        if not line or line in ("tags", "[", "]", "{", "}", "```", "```json"):
            continue
        # 去除 JSON 格式符号
        line = line.strip('",').strip()
        if line and len(line) >= 2:
            tags.append(line)

    # fallback 后过滤：只保留已知标签或"未知"占位符，并限制为5个
    valid_tags = set(_ALL_RECRUIT_TAGS)
    tags = [t for t in tags if t in valid_tags or t == "未知"]
    tags = tags[:5]

    if not tags:
        raise RuntimeError(f"无法从 LLM 响应中解析标签: {text[:200]}")

    return tags
