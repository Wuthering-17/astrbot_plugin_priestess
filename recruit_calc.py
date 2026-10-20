"""公招核心计算模块

实现标签组合枚举、干员交集计算、4星及以上筛选与排序。
算法参考 ak-recruiter 和 MAA 的实现逻辑。
"""

from astrbot.api import logger
from itertools import combinations
from dataclasses import dataclass

from .recruit_data import (
    load_operators,
    load_all_tags,
    load_profession_tags,
    build_operator_by_tag,
    RecruitOperator,
    TOP_OPERATOR_TAG,
    SENIOR_OPERATOR_TAG,
    ROBOT_TAG,
    PRIORITY_TAGS,
    RECRUIT_TAG_COUNT,
)



@dataclass
class RecruitCombo:
    """单个标签组合的推荐结果"""

    tags: tuple[str, ...]           # 标签组合（如 ("狙击", "输出")）
    operators: list[RecruitOperator]  # 满足该组合的所有干员（按星级降序）
    guaranteed_rarity: int          # 保底星级（该组合中最低星级的干员）
    priority_tags: list[str]        # 组合中包含的特殊标签
    has_top_operator: bool          # 是否包含"高级资深干员"

    def __repr__(self) -> str:
        tag_str = " + ".join(self.tags)
        op_names = ", ".join(f"{op.name_cn}(★{op.rarity})" for op in self.operators[:5])
        return f"RecruitCombo[{tag_str}] → 保底★{self.guaranteed_rarity} | {op_names}"


def normalize_tags(raw_tags: list[str]) -> list[str]:
    """将用户输入/OCR识别的标签标准化为有效标签

    处理规则：
    1. 去除首尾空白
    2. 去除"干员"后缀（"近卫干员" → "近卫"）
    3. 模糊匹配：如果精确匹配失败，尝试查找最相似的标签
    4. 过滤无效标签

    Args:
        raw_tags: 原始标签字符串列表

    Returns:
        标准化后的有效标签列表
    """
    all_valid = set(load_all_tags())
    professions = set(load_profession_tags())
    # 职业标签也是有效标签
    all_valid |= professions
    result: list[str] = []

    for raw in raw_tags:
        tag = raw.strip()
        if not tag:
            continue

        # 精确匹配（包含普通标签和职业标签）
        if tag in all_valid:
            result.append(tag)
            continue

        # 去除"干员"后缀（OCR常识别为"近卫干员"）
        if tag.endswith("干员"):
            stripped = tag[:-2]
            if stripped in all_valid:
                result.append(stripped)
                continue

        # 模糊匹配：尝试查找编辑距离最近的标签（简单版：包含关系）
        matched = _fuzzy_match_tag(tag, all_valid)
        if matched:
            result.append(matched)
            logger.debug(f"[Recruit] 模糊匹配: '{tag}' → '{matched}'")
        else:
            logger.warning(f"[Recruit] 无法识别的标签: '{tag}'，已忽略")

    return result


def _fuzzy_match_tag(raw: str, valid_tags: set[str]) -> str | None:
    """对单个标签进行模糊匹配

    匹配策略（按优先级）：
    1. 完全包含：某个有效标签是 raw 的子串
    2. 子串包含：raw 是某个有效标签的子串
    3. 编辑距离 ≤1（单字替换/删除/插入）

    Args:
        raw: 待匹配的标签
        valid_tags: 所有有效标签集合

    Returns:
        最佳匹配的标签，无匹配返回 None
    """
    # 策略1：raw 包含某个有效标签（如"高级资深"包含"高级资深干员"）
    for tag in valid_tags:
        if tag in raw and len(tag) >= 2:
            return tag

    # 策略2：某个有效标签包含 raw（如"近卫"是"近卫干员"的子串）
    for tag in valid_tags:
        if raw in tag and len(raw) >= 2:
            return tag

    # 策略3：编辑距离 ≤1
    for tag in valid_tags:
        if _levenshtein_distance(raw, tag) <= 1 and len(raw) >= 2:
            return tag

    return None


def _levenshtein_distance(s1: str, s2: str) -> int:
    """计算两个字符串的编辑距离（Levenshtein distance）"""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row

    return prev_row[-1]


def calculate_combinations(tags: list[str]) -> list[RecruitCombo]:
    """计算所有可能的标签组合及其对应的干员推荐

    算法步骤：
    1. 枚举 1~3 个标签的所有组合（公招最多选 3 个标签）
    2. 对每种组合，取各标签对应干员列表的交集
    3. 若无"高级资深干员"标签，排除 6 星干员
    4. 过滤：只保留有 3 星及以上干员的组合（或全低星但无更好选择时保留）
    5. 按优先级排序：特殊标签数量 > 保底星级 > 标签数量少优先

    Args:
        tags: 标准化后的有效标签列表（通常 5 个）

    Returns:
        排序后的推荐组合列表
    """
    tag_to_ops = build_operator_by_tag()
    final_combos: list[RecruitCombo] = []

    # 枚举 1~3 个标签的所有组合
    max_select = min(3, len(tags))
    for r in range(1, max_select + 1):
        for combo in combinations(tags, r):
            # 取交集：满足所有标签的干员
            ops = _intersect_operators(combo, tag_to_ops)
            if not ops:
                continue

            has_top = TOP_OPERATOR_TAG in combo

            # 若无高级资深干员标签，排除 6 星
            if not has_top:
                ops = [op for op in ops if op.rarity <= 5]
                if not ops:
                    continue

            # 计算保底星级（该组合中最低星级的干员）
            # 优先用 3 星及以上的干员计算保底，如果没有则退而求其次
            ops_high = [op for op in ops if op.rarity >= 3]
            ops_for_guarantee = ops_high if ops_high else ops
            guaranteed_rarity = min(op.rarity for op in ops_for_guarantee)

            # 只保留保底 3 星及以上的组合，或包含特殊标签的组合
            # （1~2 星组合通常不值得推荐，但支援机械是例外）
            priority_in_combo = [p for p in PRIORITY_TAGS if p in combo]
            if guaranteed_rarity < 3 and not priority_in_combo:
                continue

            final_combos.append(RecruitCombo(
                tags=combo,
                operators=ops,
                guaranteed_rarity=guaranteed_rarity,
                priority_tags=priority_in_combo,
                has_top_operator=has_top,
            ))

    # 排序：
    # 1. 特殊标签数量多优先
    # 2. 保底星级高优先
    # 3. 标签数量少优先（2标签 > 3标签，更灵活）
    final_combos.sort(
        key=lambda c: (
            len(c.priority_tags),          # 特殊标签数量（倒序）
            c.guaranteed_rarity,           # 保底星级（倒序）
            -len(c.tags),                  # 标签数量（倒序，少的在前）
        ),
        reverse=True,
    )

    return final_combos


def _intersect_operators(
    combo: tuple[str, ...],
    tag_to_ops: dict[str, list[RecruitOperator]],
) -> list[RecruitOperator]:
    """计算多个标签对应干员列表的交集

    Args:
        combo: 标签组合
        tag_to_ops: 标签→干员列表的映射表

    Returns:
        满足所有标签的干员列表（按星级降序）
    """
    if not combo:
        return []

    # 从第一个标签的干员列表开始
    result = set(tag_to_ops.get(combo[0], []))

    # 依次取交集
    for tag in combo[1:]:
        ops = set(tag_to_ops.get(tag, []))
        result &= ops
        if not result:
            return []

    # 转回列表并按星级降序
    return sorted(result, key=lambda op: op.rarity, reverse=True)


def format_recommendation(combos: list[RecruitCombo], raw_tags: list[str]) -> str:
    """将推荐结果格式化为易读的文字报告

    Args:
        combos: 排序后的推荐组合列表
        raw_tags: 原始输入的标签列表（用于显示）

    Returns:
        格式化的推荐文本
    """
    if not combos:
        return (
            f"【公招推荐】\n"
            f"识别标签：{' | '.join(raw_tags)}\n"
            f"\n未找到4星及以上的稳妥组合，建议刷新。"
        )

    lines: list[str] = []
    lines.append("【公招推荐】")
    lines.append(f"识别标签：{' | '.join(raw_tags)}")
    lines.append("")

    # 最高保底
    max_rarity = combos[0].guaranteed_rarity
    star_emoji = "★" * max_rarity
    lines.append(f"🎯 最高保底：{star_emoji}")
    lines.append("")

    # 只展示前几个有价值的组合（避免信息过载）
    shown = 0
    for combo in combos:
        if shown >= 6:
            break
        # 跳过明显劣于已展示组合的方案
        if shown > 0 and combo.guaranteed_rarity < max_rarity - 1:
            break

        tag_str = " + ".join(combo.tags)
        star_str = "★" * combo.guaranteed_rarity

        # 标记特殊标签
        markers = ""
        if combo.has_top_operator:
            markers = " 🔥必出6星"
        elif SENIOR_OPERATOR_TAG in combo.priority_tags:
            markers = " 🔥必出5星"
        elif ROBOT_TAG in combo.priority_tags:
            markers = " 🤖支援机械"

        lines.append(f"▸ {tag_str} → 保底{star_str}{markers}")

        # 列出该组合可获得的干员（限制显示数量）
        op_strs: list[str] = []
        for op in combo.operators:
            if op.rarity >= 4:
                op_strs.append(f"{op.name_cn}(★{op.rarity})")
            elif len(op_strs) < 8:  # 低星也显示一部分
                op_strs.append(f"{op.name_cn}(★{op.rarity})")
        if len(op_strs) > 12:
            op_strs = op_strs[:12]
            op_strs.append("...")
        lines.append(f"   可获得：{', '.join(op_strs)}")
        lines.append("")
        shown += 1

    # 补充提示
    if any(c.has_top_operator for c in combos):
        lines.append("💡 选中「高级资深干员」+ 任意其他标签，拉满9小时必出6星")
    elif any(SENIOR_OPERATOR_TAG in c.priority_tags for c in combos):
        lines.append("💡 选中「资深干员」+ 任意其他标签，拉满9小时必出5星")

    return "\n".join(lines)
