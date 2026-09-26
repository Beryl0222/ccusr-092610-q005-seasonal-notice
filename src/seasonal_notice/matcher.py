"""确定性规则匹配器。

输入同一事实集与同一批有效发布，输出结构与顺序完全一致。匹配器是纯函数。

判定一律三态，绝不做乐观选择：

* ``applicable``    条件确认满足，可以给出该规则的建议；
* ``rejected``      条件确认不满足（如年龄超出、地区无关），规则静默；
* ``indeterminate`` 关键信息缺失，规则不生效，只在 ``undetermined`` 中
  列出需要补问的内容，由值班人员核实后重放。

其余规则：

* 触发事实要求"症状任一命中、旅居/暴露/用药条件全部满足"；明确为空表示
  确认没有，列入 ``unknown_fields`` 表示问不到，二者区别对待。
* 红旗（停止自我处理条件）只要求受众与地区确认适用，不要求触发，确保
  急症信号不会被触发条件挡住。
* 用药警示只在药物确实出现、且人群标签命中时生效。
* 转介级别就高不就低；互相矛盾的用药指令（可使用 vs 应避免）不挑选更
  乐观的结论，置 ``needs_human`` 转人工。
* 局部地区（含虫媒）风险只对当地受众与确有旅居史的人成立，不外溢。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from .model import (
    ConsultationFacts,
    MatchedAdvice,
    MatchResult,
    MedicationCaution,
    RedFlag,
    ReferralLevel,
    Release,
    RulePackage,
)

GateVerdict = Literal["applicable", "rejected", "indeterminate"]

_REFERRAL_ORDER = {
    ReferralLevel.NONE: 0,
    ReferralLevel.URGENT: 1,
    ReferralLevel.EMERGENCY: 2,
}


def stricter(left: ReferralLevel, right: ReferralLevel) -> ReferralLevel:
    return left if _REFERRAL_ORDER[left] >= _REFERRAL_ORDER[right] else right


@dataclass(frozen=True)
class _Gate:
    verdict: GateVerdict
    missing: tuple[str, ...] = ()

    @property
    def applicable(self) -> bool:
        return self.verdict == "applicable"


def _region_verdict(
    regions: frozenset[str], facts: ConsultationFacts, label: str
) -> _Gate:
    """局部风险对当地居民与近期旅居者成立；归属不明为未确定。"""
    if not regions:
        return _Gate("applicable")
    if facts.region_code in regions:
        return _Gate("applicable")
    if regions & facts.travel_history:
        return _Gate("applicable")
    if facts.region_code is None:
        return _Gate("indeterminate", (f"地区缺失：无法判断{label}的地区适用条件",))
    return _Gate("rejected")


def _audience_gate(package: RulePackage, facts: ConsultationFacts, scope_regions: frozenset[str]) -> _Gate:
    audience = package.audience
    missing: list[str] = []

    bounded_age = audience.min_age_months is not None or audience.max_age_months is not None
    if facts.age_months is None:
        if bounded_age:
            lower = audience.min_age_months if audience.min_age_months is not None else 0
            upper = audience.max_age_months if audience.max_age_months is not None else "∞"
            missing.append(f"年龄缺失：无法判断《{package.title}》是否适用（{lower}-{upper} 月龄）")
    else:
        if audience.min_age_months is not None and facts.age_months < audience.min_age_months:
            return _Gate("rejected")
        if audience.max_age_months is not None and facts.age_months > audience.max_age_months:
            return _Gate("rejected")

    if audience.include_tags and not audience.include_tags <= facts.tags:
        return _Gate("rejected")
    if audience.exclude_tags & facts.tags:
        return _Gate("rejected")

    for gate in (
        _region_verdict(audience.region_codes, facts, f"《{package.title}》"),
        _region_verdict(scope_regions, facts, "该发布的地区范围"),
    ):
        if gate.verdict == "rejected":
            return _Gate("rejected")
        missing.extend(gate.missing)

    if missing:
        return _Gate("indeterminate", tuple(missing))
    return _Gate("applicable")


def _trigger_verdict(package: RulePackage, facts: ConsultationFacts) -> _Gate:
    """症状任一命中，其余条件全部满足；关键条件未知则未确定。"""
    trigger = package.trigger
    missing: list[str] = []

    if trigger.symptoms and not (trigger.symptoms & facts.symptoms):
        return _Gate("rejected")

    if trigger.any_travel_regions and not (trigger.any_travel_regions & facts.travel_history):
        if "travel_history" in facts.unknown_fields:
            missing.append(
                f"旅居史缺失：《{package.title}》需要旅居史 {sorted(trigger.any_travel_regions)}"
            )
        else:
            return _Gate("rejected")

    for tag in sorted(trigger.exposure_tags):
        if tag not in facts.exposure_tags:
            if "exposure_tags" in facts.unknown_fields:
                missing.append(f"暴露信息缺失：《{package.title}》要求暴露条件 {tag}")
            else:
                return _Gate("rejected")

    if trigger.medications and not (trigger.medications & facts.medications):
        if "medications" in facts.unknown_fields:
            missing.append(f"用药信息缺失：《{package.title}》涉及药物 {sorted(trigger.medications)}")
        else:
            return _Gate("rejected")

    if missing:
        return _Gate("indeterminate", tuple(missing))
    return _Gate("applicable")


def _applicable_cautions(
    package: RulePackage, facts: ConsultationFacts
) -> tuple[MedicationCaution, ...]:
    cautions: list[MedicationCaution] = []
    for caution in package.medication_cautions:
        if caution.medication not in facts.medications:
            continue
        if caution.avoid_when_tags and not (caution.avoid_when_tags & facts.tags):
            continue
        cautions.append(caution)
    return tuple(cautions)


def _is_effective(release: Release, now: str) -> bool:
    return release.status == "active" and release.signed_at <= now <= release.valid_until


def evaluate(
    releases: Sequence[Release],
    facts: ConsultationFacts,
    now: str,
    conflicting_facts: Sequence[str] = (),
) -> MatchResult:
    """对有效发布执行确定性匹配。``now`` 为带时区的 ISO 时间字符串。"""
    matched: list[MatchedAdvice] = []
    red_flags: list[RedFlag] = []
    undetermined: set[str] = set()
    escalation: set[str] = set()
    conflicts: list[str] = []
    referral = ReferralLevel.NONE

    effective = sorted(
        (r for r in releases if _is_effective(r, now)),
        key=lambda r: r.release_id,
    )

    for release in effective:
        package = release.package_snapshot
        gate = _audience_gate(package, facts, release.region_codes)
        if gate.verdict == "rejected":
            continue
        undetermined.update(gate.missing)
        # 信息不足时不出建议，也不做红旗判定，避免把结论外推给归属不明者。
        if gate.verdict == "indeterminate":
            continue

        # 红旗只要求受众/地区确认适用，不受触发条件限制。
        for stop in package.stop_conditions:
            if stop.symptom in facts.symptoms:
                red_flags.append(
                    RedFlag(
                        symptom=stop.symptom,
                        referral=stop.referral,
                        message=stop.message,
                        release_id=release.release_id,
                    )
                )
                referral = stricter(referral, stop.referral)
                escalation.add(f"{stop.symptom}:{stop.message}")

        trigger = _trigger_verdict(package, facts)
        if trigger.verdict == "rejected":
            continue
        undetermined.update(trigger.missing)
        if trigger.verdict == "indeterminate":
            continue

        cautions = _applicable_cautions(package, facts)
        matched.append(
            MatchedAdvice(
                release_id=release.release_id,
                rule_id=release.rule_id,
                revision=release.revision,
                source_id=release.source_id,
                advice=package.advice,
                medication_cautions=cautions,
            )
        )
        for advice in package.advice:
            referral = stricter(referral, advice.referral)
            if advice.kind.value == "seek_care":
                escalation.add(f"seek_care:{advice.text}")
        for caution in cautions:
            if caution.severity == "avoid":
                escalation.add(f"medication_avoid:{caution.medication}")

    # 互相矛盾的用药指令：一边说可自行使用，一边要求本人群避免 -> 转人工。
    ok_meds = {
        med
        for item in matched
        for advice in item.advice
        for med in advice.medications_ok
    }
    for med in sorted(
        caution.medication
        for item in matched
        for caution in item.medication_cautions
        if caution.severity == "avoid"
    ):
        if med in ok_meds:
            conflicts.append(
                f"用药建议冲突：{med} 同时存在“可使用”与“应避免”的规则，需人工判定"
            )

    for item in conflicting_facts:
        conflicts.append(f"关键事实冲突：{item}")

    red_flags.sort(key=lambda flag: (flag.symptom, flag.release_id))

    return MatchResult(
        consultation_id=facts.consultation_id,
        matched=tuple(matched),
        red_flags=tuple(red_flags),
        undetermined=tuple(sorted(undetermined)),
        escalation_reasons=tuple(sorted(escalation)),
        conflict_reasons=tuple(conflicts),
        referral=referral,
        needs_human=bool(conflicts),
        evaluated_at=now,
    )
