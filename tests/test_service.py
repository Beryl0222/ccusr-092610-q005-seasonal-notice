"""签发服务测试：三权分立、事件溯源、幂等、修订不变、更正与重启持久化。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seasonal_notice.bootstrap import seed_service  # noqa: E402
from seasonal_notice.model import (  # noqa: E402
    Actor,
    Advice,
    AdviceKind,
    Audience,
    ConsultationFacts,
    GuidanceSource,
    ReferralLevel,
    Role,
    RulePackage,
    StopCondition,
    TriggerFacts,
)
from seasonal_notice.service import RoleError, RuleSigningService, WorkflowError  # noqa: E402
from seasonal_notice.store import ContractViolation, DuplicateEventError  # noqa: E402

TZ = timezone(timedelta(hours=8))
SEED = ROOT / "data" / "seed_rules.json"

EDITOR = Actor("u-editor", Role.EDITOR, "编辑")
CLINICIAN = Actor("u-clinician", Role.CLINICIAN, "临床")
PUBLISHER = Actor("u-publisher", Role.PUBLISHER, "签发")
OTHER_EDITOR = Actor("u-editor-2", Role.EDITOR, "另一名编辑")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: int) -> None:
        self.at += timedelta(**delta)


def basic_package(text: str = "居家休息，多饮水") -> RulePackage:
    return RulePackage(
        title="普通发热照护",
        audience=Audience(),
        trigger=TriggerFacts(symptoms=frozenset({"fever"})),
        advice=(Advice(AdviceKind.ROUTINE, text),),
    )


def urgent_package() -> RulePackage:
    return RulePackage(
        title="高危发热",
        audience=Audience(),
        trigger=TriggerFacts(symptoms=frozenset({"fever"})),
        advice=(Advice(AdviceKind.SEEK_CARE, "尽快就医", ReferralLevel.URGENT),),
        stop_conditions=(
            StopCondition("confusion", ReferralLevel.EMERGENCY, "意识改变立即急诊"),
        ),
    )


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.events = Path(self.tmp.name) / "events.jsonl"
        self.sidecar = Path(self.tmp.name) / "work.jsonl"
        self.clock = Clock(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
        self.svc = RuleSigningService(self.events, self.sidecar, clock=self.clock)
        self.svc.register_source(
            EDITOR, GuidanceSource("src-1", "测试来源", "测试发布机构")
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _publish(
        self,
        package: RulePackage,
        rule_id: str = "rule-1",
        release_id: str = "rel-1",
        valid_days: int = 30,
        source_id: str = "src-1",
        editor: Actor = EDITOR,
        clinician: Actor = CLINICIAN,
        publisher: Actor = PUBLISHER,
        svc: RuleSigningService | None = None,
    ) -> Release:  # type: ignore[name-defined]
        svc = svc or self.svc
        revision = svc.draft_rule(editor, rule_id, source_id, package)
        svc.review_rule(clinician, rule_id, revision, package)
        return svc.sign_release(
            publisher,
            release_id,
            rule_id,
            (self.clock.at + timedelta(days=valid_days)).isoformat(),
        )


class SeparationOfDutiesTests(ServiceTestCase):
    def test_full_flow_requires_three_roles(self) -> None:
        package = basic_package()
        rev = self.svc.draft_rule(EDITOR, "rule-1", "src-1", package)
        self.svc.review_rule(CLINICIAN, "rule-1", rev, package)
        release = self.svc.sign_release(
            PUBLISHER, "rel-1", "rule-1",
            (self.clock.at + timedelta(days=7)).isoformat(),
        )
        self.assertEqual(1, release.revision)
        self.assertEqual(package.title, release.package_snapshot.title)

    def test_editor_cannot_review(self) -> None:
        rev = self.svc.draft_rule(EDITOR, "rule-1", "src-1", basic_package())
        with self.assertRaises(RoleError):
            self.svc.review_rule(OTHER_EDITOR, "rule-1", rev, basic_package())

    def test_clinician_cannot_sign(self) -> None:
        package = basic_package()
        rev = self.svc.draft_rule(EDITOR, "rule-1", "src-1", package)
        self.svc.review_rule(CLINICIAN, "rule-1", rev, package)
        with self.assertRaises(RoleError):
            self.svc.sign_release(
                CLINICIAN, "rel-1", "rule-1",
                (self.clock.at + timedelta(days=7)).isoformat(),
            )

    def test_drafter_cannot_review_own_draft(self) -> None:
        clinician_alt = Actor("u-editor", Role.CLINICIAN, "同一个人换角色")
        package = basic_package()
        rev = self.svc.draft_rule(EDITOR, "rule-1", "src-1", package)
        with self.assertRaises(RoleError):
            self.svc.review_rule(clinician_alt, "rule-1", rev, package)

    def test_three_distinct_people_required(self) -> None:
        package = basic_package()
        rev = self.svc.draft_rule(EDITOR, "rule-1", "src-1", package)
        self.svc.review_rule(CLINICIAN, "rule-1", rev, package)
        with self.assertRaises(RoleError):
            self.svc.sign_release(
                Actor("u-editor", Role.PUBLISHER, "起草人兼任签发"),
                "rel-1", "rule-1",
                (self.clock.at + timedelta(days=7)).isoformat(),
            )

    def test_cannot_sign_unreviewed_revision(self) -> None:
        self.svc.draft_rule(EDITOR, "rule-1", "src-1", basic_package())
        with self.assertRaises(WorkflowError):
            self.svc.sign_release(
                PUBLISHER, "rel-1", "rule-1",
                (self.clock.at + timedelta(days=7)).isoformat(),
            )

    def test_valid_until_must_be_future(self) -> None:
        package = basic_package()
        rev = self.svc.draft_rule(EDITOR, "rule-1", "src-1", package)
        self.svc.review_rule(CLINICIAN, "rule-1", rev, package)
        with self.assertRaises(WorkflowError):
            self.svc.sign_release(
                PUBLISHER, "rel-1", "rule-1",
                (self.clock.at - timedelta(days=1)).isoformat(),
            )


class EventSourcingTests(ServiceTestCase):
    def test_versions_increment_per_aggregate_and_events_are_persisted(self) -> None:
        self._publish(basic_package())
        lines = [json.loads(x) for x in self.events.read_text(encoding="utf-8").splitlines()]
        types = [e["event_type"] for e in lines]
        self.assertEqual(
            ["SOURCE_REGISTERED", "RULE_REVIEWED", "RULE_REVIEWED", "NOTICE_SIGNED"], types
        )
        rule_events = [e for e in lines if e["aggregate_type"] == "audience_rule"]
        self.assertEqual([1, 2], [e["version"] for e in rule_events])

    def test_all_events_satisfy_contract(self) -> None:
        self._publish(basic_package())
        seed_service(self.svc, SEED, EDITOR, CLINICIAN, PUBLISHER)
        for line in self.events.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            self.assertIn("event_id", event)
            self.assertIn("+", event["occurred_at"])  # 时区必须落盘

    def test_duplicate_event_id_rejected(self) -> None:
        self._publish(basic_package())
        raw = json.loads(self.events.read_text(encoding="utf-8").splitlines()[0])
        with self.assertRaises(DuplicateEventError):
            self.svc.store.append(raw)

    def test_bad_contract_event_rejected_before_write(self) -> None:
        before = len(self.events.read_text(encoding="utf-8").splitlines())
        bad = {
            "event_id": "x",
            "event_type": "NOTICE_SIGNED",
            "aggregate_type": "notice_release",
            "aggregate_id": "r",
            "occurred_at": "2026-09-26T09:00:00",  # 无时区
            "version": 0,
            "payload": {},
        }
        with self.assertRaises(ContractViolation):
            self.svc.store.append(bad)
        after = len(self.events.read_text(encoding="utf-8").splitlines())
        self.assertEqual(before, after)


class RevisionImmutabilityTests(ServiceTestCase):
    def test_revision_does_not_change_sent_release_basis(self) -> None:
        self._publish(basic_package("旧建议：多休息"), release_id="rel-1")
        message = self.svc.queue_message("rel-1", "citizen-1")
        self.assertTrue(self.svc.send_message(message))
        sent_release = self.svc.get_release("rel-1")

        # 编辑发起修订并重新签发；已发发布的快照内容不变
        package2 = urgent_package()
        rev2 = self.svc.draft_rule(EDITOR, "rule-1", "src-1", package2)
        self.svc.review_rule(CLINICIAN, "rule-1", rev2, package2)
        self.svc.sign_release(
            PUBLISHER, "rel-2", "rule-1",
            (self.clock.at + timedelta(days=30)).isoformat(),
        )

        again = self.svc.get_release("rel-1")
        self.assertEqual(sent_release.package_snapshot.to_dict(), again.package_snapshot.to_dict())
        self.assertEqual(1, again.revision)
        self.assertEqual("旧建议：多休息", again.package_snapshot.advice[0].text)
        # 新咨询匹配到的是新版本
        result = self.svc.handle_consultation(
            ConsultationFacts("c-1", symptoms=frozenset({"fever"}))
        )
        self.assertEqual(["rel-2"], [m.release_id for m in result.matched])

    def test_cannot_draft_next_revision_before_review(self) -> None:
        self.svc.draft_rule(EDITOR, "rule-1", "src-1", basic_package())
        with self.assertRaises(WorkflowError):
            self.svc.draft_rule(EDITOR, "rule-1", "src-1", urgent_package())


class MessageAndCorrectionTests(ServiceTestCase):
    def test_send_is_idempotent_and_expired_release_blocks_send(self) -> None:
        self._publish(basic_package(), valid_days=1)
        message = self.svc.queue_message("rel-1", "citizen-1")
        self.assertTrue(self.svc.send_message(message))
        self.assertFalse(self.svc.send_message(message))
        self.clock.advance(days=3)
        msg2 = self.svc.queue_message("rel-1", "citizen-2")
        with self.assertRaises(WorkflowError):
            self.svc.send_message(msg2)

    def test_correction_pauses_unsent_and_records_for_delivered(self) -> None:
        self._publish(basic_package())
        sent = self.svc.queue_message("rel-1", "citizen-a")
        waiting = self.svc.queue_message("rel-1", "citizen-b")
        self.svc.send_message(sent)

        outcome = self.svc.correct_source(
            CLINICIAN,
            GuidanceSource("src-2", "更正来源", "机构"),
            "src-1",
            "指引中剂量表述更正",
        )
        self.assertEqual([waiting], outcome["paused_messages"])
        self.assertEqual(
            ["correction|rel-1|citizen-a"], outcome["correction_tasks"]
        )

        # 未发消息被暂停，不能再发送
        with self.assertRaises(WorkflowError):
            self.svc.send_message(waiting)
        statuses = {m["recipient_id"]: m["status"] for m in self.svc.messages()}
        self.assertEqual("sent", statuses["citizen-a"])
        self.assertEqual("paused", statuses["citizen-b"])

        # 已触达者有明确纠正记录，且引用旧依据快照
        record = self.svc.correction_record("correction|rel-1|citizen-a")
        self.assertIn("剂量表述更正", record["message"])
        self.assertEqual("src-1", record["superseded_notice"]["source_id"])
        self.assertIsNone(record["delivered_at"])

        # 被更正的发布不再参与新咨询匹配
        result = self.svc.handle_consultation(
            ConsultationFacts("c-9", symptoms=frozenset({"fever"}))
        )
        self.assertEqual((), result.matched)

    def test_correction_requires_reason_and_clinician(self) -> None:
        self._publish(basic_package())
        with self.assertRaises(RoleError):
            self.svc.correct_source(
                PUBLISHER, GuidanceSource("src-2", "x", "x"), "src-1", "原因"
            )
        with self.assertRaises(WorkflowError):
            self.svc.correct_source(
                CLINICIAN, GuidanceSource("src-2", "x", "x"), "src-1", "  "
            )

    def test_correction_is_idempotent_per_recipient(self) -> None:
        self._publish(basic_package())
        sent = self.svc.queue_message("rel-1", "citizen-a")
        self.svc.send_message(sent)
        args = (GuidanceSource("src-2", "更正", "机构"), "src-1", "原因")
        first = self.svc.correct_source(CLINICIAN, *args)
        second = self.svc.correct_source(CLINICIAN, *args)
        self.assertEqual(1, len(first["correction_tasks"]))
        self.assertEqual(0, len(second["correction_tasks"]))


class ConsultationTests(ServiceTestCase):
    def test_same_consultation_replays_once(self) -> None:
        self._publish(basic_package())
        first = self.svc.handle_consultation(
            ConsultationFacts("c-1", symptoms=frozenset({"fever"}))
        )
        # 即便第二次给出不同（错误的）事实，也只保留首诊结果
        second = self.svc.handle_consultation(
            ConsultationFacts("c-1", symptoms=frozenset())
        )
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_conflicting_facts_escalate_to_human(self) -> None:
        self._publish(basic_package())
        result = self.svc.handle_consultation(
            ConsultationFacts("c-1", symptoms=frozenset({"fever"})),
            conflicting_facts=["体温记录 37.2℃ 与 39.4℃ 冲突"],
        )
        self.assertTrue(result.needs_human)
        self.assertEqual(ReferralLevel.NONE, result.referral)


class RestartPersistenceTests(ServiceTestCase):
    def test_state_and_pending_corrections_survive_restart(self) -> None:
        self._publish(basic_package())
        sent = self.svc.queue_message("rel-1", "citizen-a")
        self.svc.send_message(sent)
        self.svc.correct_source(
            CLINICIAN, GuidanceSource("src-2", "更正来源", "机构"), "src-1", "表述更正"
        )
        # 重启前不送达纠正通知
        restarted = RuleSigningService(self.events, self.sidecar, clock=self.clock)
        pending = restarted.pending_corrections()
        self.assertEqual(1, len(pending))
        self.assertEqual("correction|rel-1|citizen-a", pending[0]["task_id"])
        self.assertTrue(restarted.deliver_correction(pending[0]["task_id"]))
        self.assertFalse(restarted.deliver_correction(pending[0]["task_id"]))

        again = RuleSigningService(self.events, self.sidecar, clock=self.clock)
        self.assertEqual(0, len(again.pending_corrections()))

    def test_expired_rules_stay_excluded_after_restart(self) -> None:
        seed_service(self.svc, SEED, EDITOR, CLINICIAN, PUBLISHER)
        self.clock.advance(days=120)
        restarted = RuleSigningService(self.events, self.sidecar, clock=self.clock)
        result = restarted.handle_consultation(
            ConsultationFacts(
                "c-late",
                symptoms=frozenset({"fever"}),
                region_code="CN-44",
            )
        )
        self.assertEqual((), result.matched)
        self.assertEqual(ReferralLevel.NONE, result.referral)

    def test_seed_flow_is_reproducible(self) -> None:
        ids_a = seed_service(self.svc, SEED, EDITOR, CLINICIAN, PUBLISHER)
        events_b = Path(self.tmp.name) / "e2.jsonl"
        work_b = Path(self.tmp.name) / "w2.jsonl"
        svc_b = RuleSigningService(events_b, work_b, clock=self.clock)
        ids_b = seed_service(svc_b, SEED, EDITOR, CLINICIAN, PUBLISHER)
        self.assertEqual(ids_a, ids_b)
        facts = ConsultationFacts(
            "c-seed",
            symptoms=frozenset({"diarrhea", "lethargy"}),
            age_months=10,
            tags=frozenset({"preschool_child"}),
            region_code="CN-11",
        )
        a = json.dumps(self.svc.handle_consultation(facts).to_dict(), sort_keys=True)
        b = json.dumps(svc_b.handle_consultation(facts).to_dict(), sort_keys=True)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
