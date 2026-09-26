"""匹配器单元测试：三态门控、红旗、冲突转人工、地区边界、确定性。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seasonal_notice.matcher import evaluate  # noqa: E402
from seasonal_notice.model import (  # noqa: E402
    Advice,
    AdviceKind,
    Audience,
    ConsultationFacts,
    MatchedAdvice,
    MedicationCaution,
    ReferralLevel,
    Release,
    Actor,
    Role,
    RulePackage,
    StopCondition,
    TriggerFacts,
)

NOW = "2026-09-26T10:00:00+08:00"
FUTURE = "2026-12-31T10:00:00+08:00"
PAST = "2026-09-01T10:00:00+08:00"

SIGNER = Actor("u-pub", Role.PUBLISHER, "签发人")


def make_release(
    release_id: str,
    package: RulePackage,
    *,
    revision: int = 1,
    signed_at: str = NOW,
    valid_until: str = FUTURE,
    region_codes: frozenset[str] = frozenset(),
    source_id: str = "src-1",
    rule_id: str = "rule-x",
) -> Release:
    return Release(
        release_id=release_id,
        rule_id=rule_id,
        revision=revision,
        source_id=source_id,
        package_snapshot=package,
        signed_at=signed_at,
        signer=SIGNER,
        valid_until=valid_until,
        region_codes=region_codes,
    )


def child_diarrhea_package() -> RulePackage:
    return RulePackage(
        title="儿童腹泻",
        audience=Audience(min_age_months=0, max_age_months=72),
        trigger=TriggerFacts(symptoms=frozenset({"diarrhea", "vomiting"})),
        advice=(
            Advice(AdviceKind.ROUTINE, "口服补液", ReferralLevel.NONE, frozenset({"ors"})),
            Advice(AdviceKind.SEEK_CARE, "持续呕吐尽快就医", ReferralLevel.URGENT),
        ),
        stop_conditions=(
            StopCondition("bloody_stool", ReferralLevel.URGENT, "血便尽快就医"),
            StopCondition("lethargy", ReferralLevel.EMERGENCY, "萎靡立即急诊"),
        ),
        medication_cautions=(
            MedicationCaution(
                "loperamide", "儿童勿自行止泻", frozenset({"preschool_child"}), "avoid"
            ),
        ),
    )


def vector_package() -> RulePackage:
    """地区相关性由受众门控（含旅居史）统一判定，触发只看症状。"""
    return RulePackage(
        title="局部虫媒发热",
        audience=Audience(region_codes=frozenset({"CN-44"})),
        trigger=TriggerFacts(symptoms=frozenset({"fever"})),
        advice=(Advice(AdviceKind.SEEK_CARE, "发热门诊就医", ReferralLevel.URGENT),),
        stop_conditions=(
            StopCondition("confusion", ReferralLevel.EMERGENCY, "意识改变立即急诊"),
        ),
        medication_cautions=(
            MedicationCaution("aspirin", "不建议自行服用阿司匹林", severity="caution"),
        ),
    )


def nationwide_travel_package() -> RulePackage:
    """全国发布、仅凭风险地区旅居史触发的规则。"""
    return RulePackage(
        title="旅行史发热监测",
        audience=Audience(),
        trigger=TriggerFacts(
            symptoms=frozenset({"fever"}),
            any_travel_regions=frozenset({"CN-46"}),
        ),
        advice=(Advice(AdviceKind.SEEK_CARE, "告知旅居史并就医", ReferralLevel.URGENT),),
    )


class MatcherGateTests(unittest.TestCase):
    def test_age_within_bounds_matches(self) -> None:
        release = make_release("r1", child_diarrhea_package())
        facts = ConsultationFacts("c1", symptoms=frozenset({"diarrhea"}), age_months=18)
        result = evaluate([release], facts, NOW)
        self.assertEqual(["r1"], [m.release_id for m in result.matched])
        self.assertEqual(ReferralLevel.URGENT, result.referral)

    def test_age_outside_bounds_is_silently_rejected(self) -> None:
        release = make_release("r1", child_diarrhea_package())
        facts = ConsultationFacts("c1", symptoms=frozenset({"diarrhea"}), age_months=120)
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)
        self.assertEqual((), result.undetermined)

    def test_age_missing_is_undetermined_not_matched(self) -> None:
        release = make_release("r1", child_diarrhea_package())
        facts = ConsultationFacts("c1", symptoms=frozenset({"diarrhea"}))
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)
        self.assertTrue(any("年龄缺失" in note for note in result.undetermined))


class RedFlagTests(unittest.TestCase):
    def test_red_flag_fires_without_trigger_but_requires_audience(self) -> None:
        release = make_release("r1", child_diarrhea_package())
        # 年龄符合但没有腹泻/呕吐触发症状，只有红旗症状
        facts = ConsultationFacts("c1", symptoms=frozenset({"lethargy"}), age_months=18)
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)  # 未触发，不给常规建议
        self.assertEqual(ReferralLevel.EMERGENCY, result.referral)
        self.assertEqual(["lethargy"], [f.symptom for f in result.red_flags])

    def test_red_flag_respects_referral_max(self) -> None:
        release = make_release("r1", child_diarrhea_package())
        facts = ConsultationFacts(
            "c1", symptoms=frozenset({"diarrhea", "bloody_stool"}), age_months=18
        )
        result = evaluate([release], facts, NOW)
        self.assertEqual(ReferralLevel.URGENT, result.referral)


class RegionBoundaryTests(unittest.TestCase):
    def test_local_resident_matches(self) -> None:
        release = make_release(
            "r1", vector_package(), region_codes=frozenset({"CN-44"})
        )
        facts = ConsultationFacts(
            "c1", symptoms=frozenset({"fever"}), region_code="CN-44"
        )
        result = evaluate([release], facts, NOW)
        self.assertEqual(["r1"], [m.release_id for m in result.matched])

    def test_returned_traveler_matches_even_if_home_elsewhere(self) -> None:
        release = make_release(
            "r1", vector_package(), region_codes=frozenset({"CN-44"})
        )
        facts = ConsultationFacts(
            "c1",
            symptoms=frozenset({"fever"}),
            region_code="CN-11",
            travel_history=frozenset({"CN-44"}),
        )
        result = evaluate([release], facts, NOW)
        self.assertEqual(["r1"], [m.release_id for m in result.matched])

    def test_unrelated_region_does_not_receive_local_vector_risk(self) -> None:
        release = make_release(
            "r1", vector_package(), region_codes=frozenset({"CN-44"})
        )
        facts = ConsultationFacts(
            "c1", symptoms=frozenset({"fever"}), region_code="CN-11"
        )
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)
        self.assertEqual((), result.undetermined)  # 明确无关，不打扰值班人员

    def test_unknown_region_is_undetermined_not_matched(self) -> None:
        release = make_release(
            "r1", vector_package(), region_codes=frozenset({"CN-44"})
        )
        facts = ConsultationFacts("c1", symptoms=frozenset({"fever"}))
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)
        self.assertTrue(any("地区缺失" in note for note in result.undetermined))

    def test_publish_scope_region_also_gates(self) -> None:
        release = make_release(
            "r1", vector_package(), region_codes=frozenset({"CN-33"})
        )
        facts = ConsultationFacts(
            "c1", symptoms=frozenset({"fever"}), region_code="CN-44"
        )
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)


class UnknownFieldTests(unittest.TestCase):
    def test_confirmed_absence_rejects_unknown_travel_history_undetermined(self) -> None:
        release = make_release("r1", nationwide_travel_package())
        # 明确没有旅居史 -> 拒绝
        confirmed = ConsultationFacts(
            "c1", symptoms=frozenset({"fever"}), region_code="CN-11"
        )
        result = evaluate([release], confirmed, NOW)
        self.assertEqual((), result.matched)
        self.assertEqual((), result.undetermined)

        # 问不到旅居史 -> 未确定
        unknown = ConsultationFacts(
            "c2",
            symptoms=frozenset({"fever"}),
            region_code="CN-11",
            unknown_fields=frozenset({"travel_history"}),
        )
        result2 = evaluate([release], unknown, NOW)
        self.assertEqual((), result2.matched)
        self.assertTrue(any("旅居史缺失" in n for n in result2.undetermined))


class ConflictTests(unittest.TestCase):
    def test_contradictory_medication_instructions_go_to_human(self) -> None:
        pkg = RulePackage(
            title="关节痛用药",
            audience=Audience(),
            trigger=TriggerFacts(
                symptoms=frozenset({"joint_pain"}), medications=frozenset({"nsaid"})
            ),
            advice=(Advice(AdviceKind.ROUTINE, "可按说明书使用", medications_ok=frozenset({"nsaid"})),),
            medication_cautions=(
                MedicationCaution(
                    "nsaid", "溃疡史者避免", frozenset({"peptic_ulcer_history"}), "avoid"
                ),
            ),
        )
        release = make_release("r1", pkg)
        facts = ConsultationFacts(
            "c1",
            symptoms=frozenset({"joint_pain"}),
            medications=frozenset({"nsaid"}),
            tags=frozenset({"peptic_ulcer_history"}),
        )
        result = evaluate([release], facts, NOW)
        self.assertTrue(result.needs_human)
        self.assertTrue(any("nsaid" in c and "冲突" in c for c in result.conflict_reasons))

    def test_explicit_fact_conflict_goes_to_human(self) -> None:
        release = make_release("r1", child_diarrhea_package())
        facts = ConsultationFacts("c1", symptoms=frozenset({"diarrhea"}), age_months=18)
        result = evaluate([release], facts, NOW, conflicting_facts=["年龄两处记录不一致"])
        self.assertTrue(result.needs_human)
        self.assertIn("关键事实冲突：年龄两处记录不一致", result.conflict_reasons)

    def test_caution_without_contradiction_does_not_escalate_to_human(self) -> None:
        release = make_release("r1", vector_package())
        facts = ConsultationFacts(
            "c1",
            symptoms=frozenset({"fever"}),
            region_code="CN-44",
            medications=frozenset({"aspirin"}),
        )
        result = evaluate([release], facts, NOW)
        self.assertFalse(result.needs_human)
        self.assertEqual(
            ["aspirin"], [c.medication for m in result.matched for c in m.medication_cautions]
        )


class EffectivenessTests(unittest.TestCase):
    def test_expired_release_excluded(self) -> None:
        release = make_release(
            "r1", child_diarrhea_package(), valid_until="2026-09-02T00:00:00+08:00"
        )
        facts = ConsultationFacts("c1", symptoms=frozenset({"diarrhea"}), age_months=18)
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)

    def test_not_yet_signed_excluded(self) -> None:
        release = make_release(
            "r1", child_diarrhea_package(), signed_at=FUTURE
        )
        facts = ConsultationFacts("c1", symptoms=frozenset({"diarrhea"}), age_months=18)
        result = evaluate([release], facts, NOW)
        self.assertEqual((), result.matched)


class DeterminismTests(unittest.TestCase):
    def test_same_facts_same_output_regardless_of_input_order(self) -> None:
        releases = [
            make_release("r2", vector_package(), region_codes=frozenset({"CN-44"}),
                         rule_id="rv", source_id="s2"),
            make_release("r1", child_diarrhea_package(), rule_id="rc", source_id="s1"),
        ]
        facts = ConsultationFacts(
            "c1",
            symptoms=frozenset({"fever", "diarrhea", "confusion"}),
            age_months=18,
            region_code="CN-44",
        )
        import json

        first = json.dumps(evaluate(list(releases), facts, NOW).to_dict(), sort_keys=True)
        second = json.dumps(evaluate(list(reversed(releases)), facts, NOW).to_dict(), sort_keys=True)
        self.assertEqual(first, second)
        result = evaluate(releases, facts, NOW)
        self.assertEqual(["r1", "r2"], [m.release_id for m in result.matched])


if __name__ == "__main__":
    unittest.main()
