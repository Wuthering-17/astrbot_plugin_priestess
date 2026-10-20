"""公招数据加载与管理模块

从 Aceship 的 AN-EN-Tags 项目下载并缓存公招相关数据，
提供标签列表、干员映射等核心数据结构。

数据源：
- tl-akhr.json: 干员基础数据（名称、职业、星级、标签、隐藏标记）
- tl-tags.json: 公招标签数据（中文/英文/日文/韩文名称，标签类型）
- tl-type.json: 职业类型数据（中英文对照）
"""

from astrbot.api import logger
import json
from pathlib import Path
from typing import TypedDict


# Bundled read-only recruitment data lives with plugin assets, not runtime state.
DATA_DIR = Path(__file__).parent / "resources" / "recruit"

# 不使用线程锁：Python GIL 保证 dict 赋值是原子操作，且数据加载幂等
# 即使并发调用，最多重复加载一次（187KB 数据量可忽略）


class OperatorData(TypedDict):
    """单个干员的原始数据结构"""

    id: str
    name_cn: str
    name_en: str
    type: str          # 职业（中文，如"近卫"）
    level: int         # 星级 1~6
    tags: list[str]    # 标签列表（中文）
    hidden: bool       # 国服是否隐藏（True 表示不可公招）


class TagData(TypedDict):
    """单个标签的原始数据结构"""

    tag_cn: str
    tag_en: str
    tag_jp: str
    tag_kr: str
    type: str          # qualifications / position / affix


class TypeData(TypedDict):
    """单个职业类型的原始数据结构"""

    type_cn: str
    type_en: str


class RecruitOperator:
    """公招干员对象（经过清洗后的可用数据）"""

    def __init__(self, raw: dict) -> None:
        self.id = raw.get("id", "")
        self.name_cn = raw.get("name_cn", "未知")
        self.name_en = raw.get("name_en", "Unknown")
        self.profession = raw.get("type", "")          # 职业，如"近卫"
        self.rarity = raw.get("level", 1)              # 星级 1~6
        self.tags = list(raw.get("tags", []))          # 标签列表

    def __repr__(self) -> str:
        return f"RecruitOperator({self.name_cn}, ★{self.rarity}, {self.profession})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecruitOperator):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


# 全局缓存，避免重复加载
_operators: list[RecruitOperator] | None = None
_all_tags: list[str] | None = None
_profession_tags: list[str] | None = None
_operator_by_tag: dict[str, list[RecruitOperator]] | None = None
_tag_type_map: dict[str, str] | None = None


def _load_json(filename: str) -> list:
    """加载 JSON 数据文件，失败时返回空列表并记录错误"""
    path = DATA_DIR / filename
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            logger.error(f"[Recruit] 数据文件格式错误（应为列表）: {filename}")
            return []
    except FileNotFoundError:
        logger.error(f"[Recruit] 数据文件不存在: {path}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"[Recruit] JSON 解析失败 ({filename}): {e}")
        return []


def load_operators() -> list[RecruitOperator]:
    """加载所有可通过公招获得的干员（排除 hidden=True 的）

    国服环境下排除 hidden=True 的干员（如某些限定干员或测试干员）。
    返回按星级降序排列的干员列表。
    """
    global _operators
    if _operators is not None:
        return _operators

    raw_data = _load_json("tl-akhr.json")
    result: list[RecruitOperator] = []

    for item in raw_data:
        # 排除国服不可公招的干员
        if item.get("hidden", False):
            continue
        # 只保留有公招标签的干员
        if not item.get("tags"):
            continue
        result.append(RecruitOperator(item))

    # 按星级降序排列（高星级优先）
    result.sort(key=lambda op: op.rarity, reverse=True)
    _operators = result
    logger.info(f"[Recruit] 加载了 {len(result)} 名可公招干员")
    return result


def load_all_tags() -> list[str]:
    """加载所有有效的公招标签中文名称

    过滤掉特殊标记（如"三测暂不实装"）和职业标签（职业单独处理）。
    """
    global _all_tags
    if _all_tags is not None:
        return _all_tags

    raw_data = _load_json("tl-tags.json")
    result: list[str] = []

    for item in raw_data:
        tag_cn = item.get("tag_cn", "")
        # 跳过空标签和特殊标记
        if not tag_cn or tag_cn in ("三测暂不实装", "MELEE", "RANGED"):
            continue
        result.append(tag_cn)

    _all_tags = result
    return result


def load_profession_tags() -> list[str]:
    """加载所有职业标签的中文名称（如"近卫"、"狙击"等）"""
    global _profession_tags
    if _profession_tags is not None:
        return _profession_tags

    raw_data = _load_json("tl-type.json")
    result = [item["type_cn"] for item in raw_data if item.get("type_cn")]

    _profession_tags = result
    return result


def get_tag_type(tag: str) -> str:
    """获取标签的类型：qualifications / position / profession / affix

    Args:
        tag: 标签中文名称

    Returns:
        标签类型字符串，未知标签返回 "unknown"
    """
    global _tag_type_map
    if _tag_type_map is not None:
        return _tag_type_map.get(tag, "unknown")

    _tag_type_map = {}
    # 职业标签
    for prof in load_profession_tags():
        _tag_type_map[prof] = "profession"
    # 其他标签
    raw_data = _load_json("tl-tags.json")
    for item in raw_data:
        tag_cn = item.get("tag_cn", "")
        if tag_cn:
            _tag_type_map[tag_cn] = item.get("type", "affix")

    return _tag_type_map.get(tag, "unknown")


def build_operator_by_tag() -> dict[str, list[RecruitOperator]]:
    """构建标签→干员列表的反向映射表

    每个标签对应满足该标签的所有干员（按星级降序）。
    职业标签单独处理：干员的 profession 字段匹配。
    结果会被缓存，避免每次重复构建。
    """
    global _operator_by_tag
    if _operator_by_tag is not None:
        return _operator_by_tag

    operators = load_operators()
    result: dict[str, list[RecruitOperator]] = {}

    all_tags = load_all_tags()
    professions = load_profession_tags()

    # 初始化每个标签的列表（包含普通标签和职业标签）
    for tag in all_tags:
        result[tag] = []
    for prof in professions:
        result[prof] = []

    for op in operators:
        # 干员自带标签
        for tag in op.tags:
            if tag in result:
                result[tag].append(op)
        # 职业标签（从 profession 字段映射）
        if op.profession in result:
            result[op.profession].append(op)

    # 去重并按星级降序（利用 RecruitOperator 的 __hash__ 用 set 去重）
    for tag in result:
        unique = sorted(set(result[tag]), key=lambda op: op.rarity, reverse=True)
        result[tag] = unique

    _operator_by_tag = result
    return result


# 常量定义
TOP_OPERATOR_TAG = "高级资深干员"          # 6星保底标签
SENIOR_OPERATOR_TAG = "资深干员"           # 5星保底标签
ROBOT_TAG = "支援机械"                     # 1星特殊标签
PRIORITY_TAGS = [ROBOT_TAG, SENIOR_OPERATOR_TAG, TOP_OPERATOR_TAG]

# 公招界面固定显示 5 个标签
RECRUIT_TAG_COUNT = 5
