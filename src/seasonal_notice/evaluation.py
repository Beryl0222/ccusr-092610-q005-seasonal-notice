"""咨询事实与已签发规则的纯函数匹配。

本模块不做任何 I/O，不持有状态：同一事实集与同一批已签发通知，
无论何时调用都返回一致的建议与未确定项。匹配只做规则比对与信息
整理，不构成疾病诊断，也不替代医嘱。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 单个事实条件的评估结果
TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"

#: 转介级别，按严重程度递增排列
REFERRAL_LEVELS = ("self_care", "community_clinic", "prompt_care", "emergency")
REFERRAL_SEVERITY = {level: index for index, level in enumerate(REFERRAL_LEVELS)}

#: 服务边界声明，随每次评估结果返回
DISCLAIMER = "本服务仅提供规则匹配与信息整理，不构成疾病诊断，不替代医嘱。"

#: 匹配状态
MATCH = "match"
NO_MATCH = "no_match"
UNDETERMINED = "undetermined"

_OPERATORS = (
    "eq",
    "in",
    "includes",
    "includes_any",
    "includes_all",
    "lt",
    "lte",
    "gt",
    "gte",
)

_NUMERIC_OPERATORS = ("lt", "lte", "gt", "gte")


def normalize_condition(condition: Any) -> tuple[str, Any]:
    """把条件规范为 (操作符, 期望值)；裸值视为 eq。"""
    if isinstance(condition, Mapping):
        if len(condition) != 1:
            raise ValueError("条件必须且只能包含一个操作符")
        (operator, expected), = condition.items()
        if operator not in _OPERATORS:
            raise ValueError(f"未支持的条件操作符: {operator}")
        return operator, expected
    return "eq", condition


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_fact(facts: Mapping[str, Any], key: str, condition: Any) -> str:
    """评估单个事实条件；事实缺失或无法安全比较时返回 UNKNOWN。"""
    if key not in facts:
        return UNKNOWN
    value = facts[key]
    try:
        operator, expected = normalize_condition(condition)
    except ValueError:
        return UNKNOWN
    if operator == "eq":
        return TRUE if value == expected else FALSE
    if operator == "in":
        if not isinstance(expected, list):
            return UNKNOWN
        return TRUE if value in expected else FALSE
    if operator == "includes":
        if not isinstance(value, list):
            return UNKNOWN
        return TRUE if expected in value else FALSE
    if operator == "includes_any":
        if not isinstance(value, list) or not isinstance(expected, list):
            return UNKNOWN
        return TRUE if any(item in value for item in expected) else FALSE
    if operator == "includes_all":
        if not isinstance(value, list) or not isinstance(expected, list):
            return UNKNOWN
        return TRUE if all(item in value for item in expected) else FALSE
    if not _is_number(value) or not _is_number(expected):
        return UNKNOWN
    if operator == "lt":
        return TRUE if value < expected else FALSE
    if operator == "lte":
        return TRUE if value <= expected else FALSE
    if operator == "gt":
        return TRUE if value > expected else FALSE
    return TRUE if value >= expected else FALSE


def audience_conditions(audience: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """把适用人群描述编译为事实条件，顺序固定以保证结果稳定。"""
    conditions: list[tuple[str, Any]] = []
    if not isinstance(audience, Mapping):
        return conditions
    if "age_min" in audience:
        conditions.append(("age_years", {"gte": audience["age_min"]}))
    if "age_max" in audience:
        conditions.append(("age_years", {"lte": audience["age_max"]}))
    regions = audience.get("regions")
    if regions:
        conditions.append(("region", {"in": list(regions)}))
    tags = audience.get("tags")
    if tags:
        conditions.append(("population_tags", {"includes_all": list(tags)}))
    return conditions


def evaluate_notice(
    facts: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> tuple[str, list[str]]:
    """评估单条已签发通知，返回 (匹配状态, 未确定的事实键)。"""
    conditions: list[tuple[str, Any]] = []
    region_scope = snapshot.get("region_scope") or []
    if region_scope:
        conditions.append(("region", {"in": list(region_scope)}))
    conditions.extend(audience_conditions(snapshot.get("audience", {})))
    trigger_facts = snapshot.get("trigger_facts", {})
    for key in sorted(trigger_facts):
        conditions.append((key, trigger_facts[key]))
    unknown_keys: list[str] = []
    for key, condition in conditions:
        outcome = check_fact(facts, key, condition)
        if outcome == FALSE:
            return NO_MATCH, []
        if outcome == UNKNOWN:
            unknown_keys.append(key)
    if unknown_keys:
        return UNDETERMINED, sorted(set(unknown_keys))
    return MATCH, []


def build_result(
    active_notices: list[tuple[str, Mapping[str, Any]]], facts: Mapping[str, Any]
) -> dict[str, Any]:
    """汇总全部有效通知的评估结果。

    建议按转介级别从重到轻、再按通知标识排序；存在多条建议时整体
    转介级别取最严重的一条，绝不选择更乐观的结论。
    """
    advice: list[dict[str, Any]] = []
    undetermined: list[dict[str, Any]] = []
    for notice_id, snapshot in sorted(active_notices, key=lambda item: item[0]):
        status, missing = evaluate_notice(facts, snapshot)
        if status == MATCH:
            advice.append(
                {
                    "notice_id": notice_id,
                    "rule_id": snapshot["rule_id"],
                    "rule_revision": snapshot["rule_revision"],
                    "referral_level": snapshot["referral_level"],
                    "advice": snapshot["advice"],
                    "stop_self_care": list(snapshot["stop_self_care"]),
                    "valid_until": snapshot["valid_until"],
                    "source_id": snapshot["source_id"],
                    "source_version": snapshot["source_version"],
                }
            )
        elif status == UNDETERMINED:
            undetermined.append(
                {
                    "notice_id": notice_id,
                    "rule_id": snapshot["rule_id"],
                    "rule_revision": snapshot["rule_revision"],
                    "missing_facts": missing,
                }
            )
    advice.sort(
        key=lambda item: (-REFERRAL_SEVERITY[item["referral_level"]], item["notice_id"])
    )
    overall = advice[0]["referral_level"] if advice else None
    if advice:
        status = "matched"
    elif undetermined:
        status = "undetermined"
    else:
        status = "no_match"
    return {
        "status": status,
        "advice": advice,
        "overall_referral_level": overall,
        "undetermined": undetermined,
        "escalation": None,
        "disclaimer": DISCLAIMER,
    }
