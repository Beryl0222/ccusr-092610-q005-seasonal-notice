"""节前热线端到端演示。

覆盖：三种咨询（旅行后发热 / 儿童腹泻 / 训练营头晕）的就医分级、
用药边界、地区风险不外溢、关键事实冲突转人工、相同咨询幂等、
三权分立、来源更正（暂停未发 + 已触达纠正）与重启后续处理。

用法：
    PYTHONPATH=src python3 examples/demo.py
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from seasonal_notice.bootstrap import seed_service
from seasonal_notice.model import (
    Actor,
    ConsultationFacts,
    GuidanceSource,
    Role,
    RulePackage,
)
from seasonal_notice.service import RoleError, RuleSigningService, WorkflowError

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed_rules.json"

EDITOR = Actor("u-editor", Role.EDITOR, "内容编辑小李")
CLINICIAN = Actor("u-clinician", Role.CLINICIAN, "临床医生王医生")
PUBLISHER = Actor("u-publisher", Role.PUBLISHER, "发布人员老周")


class Clock:
    """固定起步、可手动推进的时钟，保证演示输出可复现。"""

    def __init__(self, start: datetime) -> None:
        self.at = start

    def __call__(self) -> datetime:
        return self.at

    def advance(self, days: int = 0, **kw: object) -> None:
        self.at = self.at + timedelta(days=days, **kw)  # type: ignore[arg-type]


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


def show(result: object) -> None:
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    clock = Clock(datetime(2026, 9, 26, 9, 0, tzinfo=timezone(timedelta(hours=8))))
    tmp = tempfile.TemporaryDirectory()
    events = Path(tmp.name) / "events.jsonl"
    sidecar = Path(tmp.name) / "work.jsonl"

    banner("一、三权分立：编辑起草 → 临床审核 → 发布签发")
    svc = RuleSigningService(events, sidecar, clock=clock)
    releases = seed_service(svc, SEED, EDITOR, CLINICIAN, PUBLISHER)
    print(f"已签发 {len(releases)} 条提醒：{releases}")

    banner("二、单一角色不能完成全部环节")
    solo = Actor("u-solo", Role.EDITOR, "想一个人全包的编辑")
    camp_package = RulePackage.from_dict(
        next(r["package"] for r in json.loads(SEED.read_text(encoding="utf-8"))["rules"]
             if r["rule_id"] == "rule-camp-dizziness")
    )
    try:
        svc.review_rule(solo, "rule-camp-dizziness", 1, camp_package)
    except RoleError as exc:
        print(f"被拒：{exc}")
    try:
        svc.sign_release(solo, "rel-x", "rule-camp-dizziness",
                         (clock.at + timedelta(days=1)).isoformat())
    except RoleError as exc:
        print(f"被拒：{exc}")

    banner("三、咨询 1：旅行后发热（有广东旅居史）→ 尽快就医 + 用药警示")
    traveler = ConsultationFacts(
        consultation_id="hotline-001",
        symptoms=frozenset({"fever"}),
        tags=frozenset({"adult"}),
        region_code="CN-11",
        travel_history=frozenset({"CN-44"}),
        medications=frozenset({"aspirin"}),
    )
    show(svc.handle_consultation(traveler))

    banner("四、咨询 1b：同一咨询重放，只保留一次（结果与首诊一致）")
    again = svc.handle_consultation(
        ConsultationFacts(consultation_id="hotline-001", symptoms=frozenset({"fever"}))
    )
    print(f"referral 仍为 {again.referral.value}，matched 条数 {len(again.matched)}（按首诊事实）")

    banner("五、地区边界：北方无旅居史者发热，不收局部虫媒提醒")
    local = ConsultationFacts(
        consultation_id="hotline-002",
        symptoms=frozenset({"fever"}),
        region_code="CN-11",
    )
    result = svc.handle_consultation(local)
    print(f"matched={[m.rule_id for m in result.matched]}，referral={result.referral.value}")

    banner("六、咨询 2：幼儿腹泻伴血便 → 日常照护 + 红旗急诊/就医分级")
    child = ConsultationFacts(
        consultation_id="hotline-003",
        symptoms=frozenset({"diarrhea", "bloody_stool"}),
        age_months=18,
        tags=frozenset({"preschool_child"}),
        region_code="CN-11",
        medications=frozenset({"loperamide"}),
    )
    show(svc.handle_consultation(child))

    banner("七、咨询 3：训练营学员头晕后晕厥 → 立即急诊")
    camper = ConsultationFacts(
        consultation_id="hotline-004",
        symptoms=frozenset({"dizziness", "syncope"}),
        tags=frozenset({"training_camp"}),
        region_code="CN-33",
    )
    show(svc.handle_consultation(camper))

    banner("八、关键信息冲突 → 转人工，不选择更乐观的结论")
    conflict = svc.handle_consultation(
        ConsultationFacts(
            consultation_id="hotline-005",
            symptoms=frozenset({"joint_pain"}),
            tags=frozenset({"peptic_ulcer_history"}),
            medications=frozenset({"nsaid"}),
            region_code="CN-11",
        ),
        conflicting_facts=["录入年龄同时为 14 岁与 41 岁"],
    )
    print(json.dumps(
        {
            "needs_human": conflict.needs_human,
            "conflict_reasons": list(conflict.conflict_reasons),
            "escalation_reasons": list(conflict.escalation_reasons),
        },
        ensure_ascii=False, indent=2,
    ))

    banner("九、来源更正：暂停未发消息，为已触达者建立纠正记录")
    m_sent = svc.queue_message(releases[1], "citizen-1001")
    m_wait = svc.queue_message(releases[1], "citizen-1002")
    svc.send_message(m_sent)          # 已触达
    # m_wait 仍在发件箱
    corrected = svc.correct_source(
        CLINICIAN,
        GuidanceSource(
            "src-nhc-2026-autumn-r1",
            "国家卫健委2026年秋季健康防护提醒（更正版）",
            "国家卫生健康委员会",
        ),
        "src-nhc-2026-autumn",
        "儿童腹泻口服补液指引中补液频次表述更正",
    )
    print(json.dumps(corrected, ensure_ascii=False, indent=2))
    print("\n待发送消息现在的状态：")
    print(json.dumps(svc.messages(), ensure_ascii=False, indent=2, sort_keys=True))
    print("\n面向已触达者的明确纠正记录：")
    print(json.dumps(svc.correction_record(corrected["correction_tasks"][0]),
                     ensure_ascii=False, indent=2, sort_keys=True))

    banner("十、重启后继续处理：过期规则不参与，纠正通知仍可送达")
    svc.deliver_correction(corrected["correction_tasks"][0])
    clock.advance(days=120)  # 越过所有规则有效期
    svc2 = RuleSigningService(events, sidecar, clock=clock)
    print(f"重启后待送达纠正：{len(svc2.pending_corrections())} 条（上一条已送达）")
    expired = svc2.handle_consultation(
        ConsultationFacts(
            consultation_id="hotline-006",
            symptoms=frozenset({"dizziness", "syncope"}),
            tags=frozenset({"training_camp"}),
            region_code="CN-33",
        )
    )
    print(f"规则全部过期后：matched={len(expired.matched)}，red_flags={len(expired.red_flags)}，"
          f"referral={expired.referral.value}")
    try:
        svc2.send_message(m_wait)
    except WorkflowError as exc:
        print(f"暂停的旧消息不会被发送：{exc}")

    banner("十一、接口稳定性：同一事实集两次评估，JSON 完全一致")
    clock2 = Clock(datetime(2026, 9, 26, 9, 0, tzinfo=timezone(timedelta(hours=8))))
    a = RuleSigningService(Path(tmp.name) / "e2.jsonl", Path(tmp.name) / "w2.jsonl", clock=clock2)
    seed_service(a, SEED, EDITOR, CLINICIAN, PUBLISHER)
    b = RuleSigningService(Path(tmp.name) / "e3.jsonl", Path(tmp.name) / "w3.jsonl", clock=Clock(
        datetime(2026, 9, 26, 9, 0, tzinfo=timezone(timedelta(hours=8)))))
    seed_service(b, SEED, EDITOR, CLINICIAN, PUBLISHER)
    fa = ConsultationFacts("s", frozenset({"fever"}), None, frozenset({"adult"}),
                           "CN-44", frozenset({"CN-44"}))
    ra = json.dumps(a.handle_consultation(fa).to_dict(), ensure_ascii=False, sort_keys=True)
    rb = json.dumps(b.handle_consultation(fa).to_dict(), ensure_ascii=False, sort_keys=True)
    print("两次独立评估结果一致：", ra == rb)


if __name__ == "__main__":
    main()
