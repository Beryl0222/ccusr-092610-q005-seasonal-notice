import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seasonal_notice import evaluation
from seasonal_notice.contracts import validate_event
from seasonal_notice.service import RuleIssuanceService, ServiceError

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 26, 8, 0, tzinfo=CST)

EDITOR = "editor-li"
CLINICIAN = "doc-wang"
PUBLISHER = "pub-zhao"


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = T0
        self.svc = self.boot()

    def boot(self) -> RuleIssuanceService:
        return RuleIssuanceService(self.tmp.name, clock=lambda: self.now)

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)

    def journal_events(self) -> list[dict]:
        path = Path(self.tmp.name) / "journal.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def event_count(self, event_type: str) -> int:
        return sum(1 for e in self.journal_events() if e["event_type"] == event_type)

    # -- 流水线辅助 ------------------------------------------------------

    def register_source(self, source_id="src-nhc", version=1, event_id=None):
        return self.svc.register_source(
            event_id or f"src-{source_id}-v{version}",
            source_id,
            title="国家卫健委秋季健康提醒",
            issuer="国家卫健委",
            version=version,
            actor=EDITOR,
            role="editor",
        )

    def draft(self, rule_id="rule-diarrhea", event_id=None, **overrides):
        params = {
            "audience": {"age_max": 12, "tags": ["儿童"]},
            "trigger_facts": {"symptoms": {"includes": "腹泻"}},
            "advice": "儿童腹泻注意补液与手卫生",
            "stop_self_care": ["出现血便或持续高热立即就医"],
            "referral_level": "community_clinic",
            "source_id": "src-nhc",
            "source_version": 1,
            "valid_until": "2026-12-31T23:59:59+08:00",
            "actor": EDITOR,
            "role": "editor",
        }
        params.update(overrides)
        return self.svc.draft_rule(event_id or f"draft-{rule_id}", rule_id, **params)

    def review(self, rule_id="rule-diarrhea", revision=1, event_id=None, **overrides):
        params = {"decision": "approved", "actor": CLINICIAN, "role": "clinician"}
        params.update(overrides)
        return self.svc.review_rule(
            event_id or f"review-{rule_id}-r{revision}",
            rule_id,
            revision=revision,
            **params,
        )

    def sign(self, notice_id="n-001", rule_id="rule-diarrhea", revision=1, event_id=None, **overrides):
        params = {
            "rule_id": rule_id,
            "rule_revision": revision,
            "valid_until": "2026-12-01T00:00:00+08:00",
            "region_scope": [],
            "recipients": ["hotline-staff"],
            "actor": PUBLISHER,
            "role": "publisher",
        }
        params.update(overrides)
        return self.svc.sign_notice(event_id or f"sign-{notice_id}", notice_id, **params)

    def send(self, notice_id="n-001", event_id=None):
        return self.svc.send_notice(
            event_id or f"send-{notice_id}", notice_id, actor=PUBLISHER, role="publisher"
        )

    def make_signed_notice(self, notice_id="n-001", **sign_overrides):
        self.register_source()
        self.draft()
        self.review()
        return self.sign(notice_id, **sign_overrides)


class PipelineTests(ServiceTestCase):
    def test_full_pipeline_and_evaluation(self):
        self.make_signed_notice()
        self.send()
        notice = self.svc.notice_state("n-001")
        self.assertEqual("SENT", notice["status"])
        self.assertEqual(["hotline-staff"], notice["reached"])
        snapshot = notice["snapshot"]
        # 登记的规则要素完整进入签发快照
        for key in (
            "audience",
            "trigger_facts",
            "advice",
            "stop_self_care",
            "referral_level",
            "source_id",
            "source_version",
            "valid_until",
        ):
            self.assertIn(key, snapshot)
        result = self.svc.evaluate(
            {"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]}
        )
        self.assertEqual("matched", result["status"])
        self.assertEqual("community_clinic", result["overall_referral_level"])
        self.assertEqual("儿童腹泻注意补液与手卫生", result["advice"][0]["advice"])
        self.assertEqual(["出现血便或持续高热立即就医"], result["advice"][0]["stop_self_care"])

    def test_separation_of_duties(self):
        self.register_source()
        self.draft()
        # 角色不符
        with self.assertRaises(ServiceError) as ctx:
            self.svc.review_rule("rv-role", "rule-diarrhea", revision=1, decision="approved", actor=CLINICIAN, role="editor")
        self.assertEqual("role_forbidden", ctx.exception.code)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.draft_rule("dr-role", "r2", audience={}, trigger_facts={}, advice="x",
                                stop_self_care=["y"], referral_level="self_care",
                                source_id="src-nhc", source_version=1,
                                valid_until="2026-12-31T23:59:59+08:00",
                                actor=EDITOR, role="clinician")
        self.assertEqual("role_forbidden", ctx.exception.code)
        # 同一人不能审核自己的起草（即使持有临床角色）
        with self.assertRaises(ServiceError) as ctx:
            self.svc.review_rule("rv-self", "rule-diarrhea", revision=1, decision="approved", actor=EDITOR, role="clinician")
        self.assertEqual("sod_violation", ctx.exception.code)
        self.review()
        # 起草人与审核人都不能签发同一修订
        with self.assertRaises(ServiceError) as ctx:
            self.svc.sign_notice("sg-self1", "n-x", rule_id="rule-diarrhea", rule_revision=1,
                                 valid_until="2026-12-01T00:00:00+08:00", region_scope=[],
                                 recipients=["a"], actor=EDITOR, role="publisher")
        self.assertEqual("sod_violation", ctx.exception.code)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.sign_notice("sg-self2", "n-x", rule_id="rule-diarrhea", rule_revision=1,
                                 valid_until="2026-12-01T00:00:00+08:00", region_scope=[],
                                 recipients=["a"], actor=CLINICIAN, role="publisher")
        self.assertEqual("sod_violation", ctx.exception.code)
        # 编辑不能签发
        with self.assertRaises(ServiceError) as ctx:
            self.svc.sign_notice("sg-role", "n-x", rule_id="rule-diarrhea", rule_revision=1,
                                 valid_until="2026-12-01T00:00:00+08:00", region_scope=[],
                                 recipients=["a"], actor=PUBLISHER, role="editor")
        self.assertEqual("role_forbidden", ctx.exception.code)
        # 发布人员完成最后一步
        self.sign()
        self.assertEqual("SIGNED", self.svc.notice_state("n-001")["status"])

    def test_revision_does_not_rewrite_issued_basis(self):
        self.make_signed_notice()
        self.send()
        # 修订规则：新建议内容
        self.draft(event_id="draft-r2", advice="修订后的腹泻建议", revision=2)
        self.review(revision=2, event_id="review-r2")
        self.sign("n-002", revision=2, event_id="sign-n002")
        # 已发通知的依据保持原样
        self.assertEqual("儿童腹泻注意补液与手卫生", self.svc.notice_state("n-001")["snapshot"]["advice"])
        self.assertEqual(1, self.svc.notice_state("n-001")["snapshot"]["rule_revision"])
        self.assertEqual("修订后的腹泻建议", self.svc.notice_state("n-002")["snapshot"]["advice"])
        state = self.svc.rule_state("rule-diarrhea")
        self.assertEqual({1: "REVIEWED", 2: "REVIEWED"},
                         {k: v["status"] for k, v in state["revisions"].items()})
        # 评估同时返回两个版本的各自建议
        result = self.svc.evaluate({"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]})
        self.assertEqual(
            {"n-001": "儿童腹泻注意补液与手卫生", "n-002": "修订后的腹泻建议"},
            {item["notice_id"]: item["advice"] for item in result["advice"]},
        )

    def test_draft_blocked_while_previous_draft_open(self):
        self.register_source()
        self.draft()
        with self.assertRaises(ServiceError) as ctx:
            self.draft(event_id="draft-r2")
        self.assertEqual("draft_open", ctx.exception.code)
        self.review(decision="rejected")
        # 被拒后可在新修订上重新起草
        self.draft(event_id="draft-r2b", revision=2)
        self.assertEqual("REJECTED", self.svc.rule_state("rule-diarrhea")["revisions"][1]["status"])

    def test_sign_requires_review_and_validity_bounds(self):
        self.register_source()
        self.draft()
        with self.assertRaises(ServiceError) as ctx:
            self.sign()
        self.assertEqual("state_conflict", ctx.exception.code)
        self.review()
        with self.assertRaises(ServiceError) as ctx:
            self.sign(valid_until="2027-02-01T00:00:00+08:00")  # 超过规则有效期
        self.assertEqual("invalid_input", ctx.exception.code)
        with self.assertRaises(ServiceError) as ctx:
            self.sign(valid_until="2026-09-01T00:00:00+08:00")  # 已过期的有效期
        self.assertEqual("invalid_input", ctx.exception.code)

    def test_command_replay_is_idempotent(self):
        self.register_source()
        first = self.draft()
        self.assertEqual(1, first["revision"])
        self.assertEqual("DRAFT", first["status"])
        replay = self.draft(event_id="draft-rule-diarrhea")
        self.assertTrue(replay["replay"])
        self.assertEqual(1, self.event_count("RULE_DRAFTED"))
        # 同一事件标识不能用于其他命令
        with self.assertRaises(ServiceError) as ctx:
            self.svc.review_rule("draft-rule-diarrhea", "rule-diarrhea", revision=1,
                                 decision="approved", actor=CLINICIAN, role="clinician")
        self.assertEqual("event_id_conflict", ctx.exception.code)

    def test_naive_time_is_rejected(self):
        self.register_source()
        with self.assertRaises(ServiceError) as ctx:
            self.draft(valid_until="2026-12-31T23:59:59")
        self.assertEqual("invalid_input", ctx.exception.code)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.evaluate({}, now=datetime(2026, 9, 26, 9, 0))
        self.assertEqual("invalid_input", ctx.exception.code)


class CorrectionTests(ServiceTestCase):
    def test_correction_pauses_unsent_and_opens_records_for_reached(self):
        self.make_signed_notice()
        self.send()
        self.sign("n-002", event_id="sign-n002", recipients=["later-batch"])
        # 来源更正
        outcome = self.svc.correct_source(
            "corr-1", "src-nhc", source_version=1, reason="补液盐剂量表述有误",
            actor=CLINICIAN, role="clinician",
        )
        self.assertEqual(["n-002"], outcome["paused_notices"])
        self.assertEqual(["corr:n-001:hotline-staff"], outcome["correction_tasks"])
        # 未发送的已暂停，不能再发送
        self.assertEqual("PAUSED", self.svc.notice_state("n-002")["status"])
        with self.assertRaises(ServiceError) as ctx:
            self.send("n-002", event_id="send-n002")
        self.assertEqual("state_conflict", ctx.exception.code)
        # 已触达的生成明确纠正记录
        tasks = self.svc.pending_corrections()
        self.assertEqual(1, len(tasks))
        self.assertEqual("hotline-staff", tasks[0]["recipient"])
        self.assertEqual("补液盐剂量表述有误", tasks[0]["reason"])
        self.assertEqual("n-001", tasks[0]["affected_release"])
        # 被更正的通知不再参与匹配
        result = self.svc.evaluate({"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]})
        self.assertEqual("no_match", result["status"])
        # 送达纠正记录
        self.svc.deliver_correction("deliver-1", "corr:n-001:hotline-staff", actor=PUBLISHER, role="publisher")
        self.assertEqual([], self.svc.pending_corrections())
        self.assertEqual("DELIVERED", self.svc.correction_task("corr:n-001:hotline-staff")["status"])

    def test_corrected_source_version_blocks_new_work(self):
        self.make_signed_notice()
        self.svc.correct_source("corr-1", "src-nhc", source_version=1, reason="内容更正",
                                actor=CLINICIAN, role="clinician")
        with self.assertRaises(ServiceError) as ctx:
            self.draft(event_id="draft-r2", revision=2)
        self.assertEqual("source_corrected", ctx.exception.code)
        with self.assertRaises(ServiceError) as ctx:
            self.sign("n-002", event_id="sign-n002")
        self.assertEqual("source_corrected", ctx.exception.code)
        # 登记更正后的新版本来源即可恢复流水线
        self.register_source(version=2)
        self.draft(event_id="draft-r2b", revision=2, source_version=2)
        self.review(revision=2, event_id="review-r2")
        self.sign("n-002", revision=2, event_id="sign-n002b")
        self.assertEqual("SIGNED", self.svc.notice_state("n-002")["status"])

    def test_correction_requires_clinician(self):
        self.make_signed_notice()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.correct_source("corr-1", "src-nhc", source_version=1, reason="x",
                                    actor=PUBLISHER, role="publisher")
        self.assertEqual("role_forbidden", ctx.exception.code)


class ConsultationTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.make_signed_notice()
        self.facts = {"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]}

    def test_identical_replay_kept_once(self):
        first = self.svc.submit_consultation("call-001", self.facts)
        self.assertEqual("matched", first["status"])
        replay = self.svc.submit_consultation("call-001", dict(self.facts))
        self.assertEqual(first, replay)
        self.assertEqual(1, self.event_count("CONSULTATION_RECORDED"))
        self.assertEqual("recorded", self.svc.consultation_state("call-001")["status"])

    def test_conflicting_replay_escalates_to_human(self):
        self.svc.submit_consultation("call-001", self.facts)
        conflict = dict(self.facts, age_years=8)
        outcome = self.svc.submit_consultation("call-001", conflict)
        self.assertEqual("escalated", outcome["status"])
        self.assertEqual([], outcome["advice"])  # 不选择更乐观的结论
        self.assertEqual(["age_years"], outcome["escalation"]["conflicting_keys"])
        # 相同的冲突重放只记一次
        again = self.svc.submit_consultation("call-001", dict(conflict))
        self.assertEqual(outcome, again)
        self.assertEqual(1, self.event_count("CONSULTATION_ESCALATED"))
        # 待人工列表可见，临床人员登记结论后关闭
        reviews = self.svc.open_manual_reviews()
        self.assertEqual(["call-001"], [item["consultation_id"] for item in reviews])
        self.svc.resolve_manual_review("resolve-1", "call-001",
                                       resolution="电话核实儿童实际 4 岁，按原建议答复",
                                       actor=CLINICIAN, role="clinician")
        self.assertEqual([], self.svc.open_manual_reviews())
        self.assertEqual("resolved", self.svc.consultation_state("call-001")["status"])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.resolve_manual_review("resolve-2", "call-001", resolution="重复关闭",
                                           actor=CLINICIAN, role="clinician")
        self.assertEqual("state_conflict", ctx.exception.code)

    def test_original_record_survives_conflict(self):
        first = self.svc.submit_consultation("call-001", self.facts)
        self.svc.submit_consultation("call-001", dict(self.facts, age_years=8))
        # 首次登记的结果不被改写
        self.assertEqual(first, self.svc.consultation_state("call-001")["result"])
        self.assertEqual(1, self.event_count("CONSULTATION_RECORDED"))

    def test_undetermined_items_listed(self):
        result = self.svc.evaluate({"symptoms": ["腹泻"]})
        self.assertEqual("undetermined", result["status"])
        self.assertEqual([], result["advice"])
        self.assertEqual(["age_years", "population_tags"],
                         result["undetermined"][0]["missing_facts"])

    def test_overall_referral_takes_most_severe(self):
        self.draft(rule_id="rule-fever", event_id="draft-fever",
                   audience={}, trigger_facts={"symptoms": {"includes": "腹泻"}},
                   advice="伴高热需尽快就医", stop_self_care=["体温超过 39 度停止居家观察"],
                   referral_level="prompt_care")
        self.review(rule_id="rule-fever", event_id="review-fever")
        self.sign("n-002", rule_id="rule-fever", event_id="sign-n002")
        result = self.svc.evaluate(self.facts)
        self.assertEqual("prompt_care", result["overall_referral_level"])
        self.assertEqual("prompt_care", result["advice"][0]["referral_level"])
        self.assertEqual(2, len(result["advice"]))

    def test_result_carries_no_diagnosis_disclaimer(self):
        result = self.svc.evaluate(self.facts)
        self.assertEqual(evaluation.DISCLAIMER, result["disclaimer"])
        self.assertIn("不构成疾病诊断", result["disclaimer"])
        # 干跑评估不留下任何记录
        self.assertEqual(0, self.event_count("CONSULTATION_RECORDED"))


class RegionScopeTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.register_source()
        # 局部虫媒风险规则：仅适用于云和县
        self.draft(rule_id="rule-vector", event_id="draft-vector",
                   audience={"regions": ["云和县"]},
                   trigger_facts={"symptoms": {"includes": "发热"}},
                   advice="旅行后发热警惕虫媒疾病，尽快就医并告知旅居史",
                   stop_self_care=["高热不退立即停止自我处理"],
                   referral_level="prompt_care")
        self.review(rule_id="rule-vector", event_id="review-vector")

    def test_local_risk_does_not_spread_to_unrelated_people(self):
        self.sign("n-vector", rule_id="rule-vector", event_id="sign-vector",
                  region_scope=["云和县"])
        # 外县居民且无相关事实：不匹配
        outsider = self.svc.evaluate({"region": "邻近县", "symptoms": ["发热"]})
        self.assertEqual("no_match", outsider["status"])
        # 本县居民：匹配
        local = self.svc.evaluate({"region": "云和县", "symptoms": ["发热"]})
        self.assertEqual("matched", local["status"])
        # 地区未知：列入未确定项而非默认匹配
        unknown = self.svc.evaluate({"symptoms": ["发热"]})
        self.assertEqual("undetermined", unknown["status"])
        self.assertEqual(["region"], unknown["undetermined"][0]["missing_facts"])

    def test_release_cannot_exceed_rule_regions(self):
        with self.assertRaises(ServiceError) as ctx:
            self.sign("n-vector", rule_id="rule-vector", event_id="sign-vector",
                      region_scope=["云和县", "邻近县"])
        self.assertEqual("region_scope_exceeds", ctx.exception.code)
        with self.assertRaises(ServiceError) as ctx:
            self.sign("n-vector", rule_id="rule-vector", event_id="sign-vector",
                      region_scope=[])
        self.assertEqual("region_scope_required", ctx.exception.code)

    def test_travel_history_matches_regardless_of_residence(self):
        # 另一条规则：按旅居史触发，不限居住地区
        self.draft(rule_id="rule-travel", event_id="draft-travel",
                   audience={},
                   trigger_facts={"recent_travel": {"includes": "云和县"},
                                  "symptoms": {"includes": "发热"}},
                   advice="有流行区旅居史且发热，尽快就医",
                   stop_self_care=["出现皮疹立即就医"],
                   referral_level="prompt_care")
        self.review(rule_id="rule-travel", event_id="review-travel")
        self.sign("n-travel", rule_id="rule-travel", event_id="sign-travel", region_scope=[])
        result = self.svc.evaluate(
            {"region": "邻近县", "recent_travel": ["云和县"], "symptoms": ["发热"]}
        )
        self.assertEqual("matched", result["status"])
        self.assertEqual(["n-travel"], [item["notice_id"] for item in result["advice"]])


class DeterminismTests(ServiceTestCase):
    def test_same_facts_same_result(self):
        self.make_signed_notice()
        self.draft(rule_id="rule-camp", event_id="draft-camp",
                   audience={"tags": ["训练营学员"]},
                   trigger_facts={"symptoms": {"includes": "头晕"}},
                   advice="训练营头晕停止训练并补水", stop_self_care=["意识模糊立即送医"],
                   referral_level="prompt_care")
        self.review(rule_id="rule-camp", event_id="review-camp")
        self.sign("n-camp", rule_id="rule-camp", event_id="sign-camp")
        facts = {"age_years": 20, "population_tags": ["训练营学员"], "symptoms": ["头晕"]}
        first = self.svc.evaluate(facts)
        second = self.svc.evaluate(dict(facts))
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_result_independent_of_creation_order(self):
        facts = {"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]}
        self.make_signed_notice()  # n-001
        self.draft(rule_id="rule-b", event_id="draft-b",
                   audience={}, trigger_facts={"symptoms": {"includes": "腹泻"}},
                   advice="建议 B", stop_self_care=["条件 B"], referral_level="prompt_care")
        self.review(rule_id="rule-b", event_id="review-b")
        self.sign("n-002", rule_id="rule-b", event_id="sign-b")
        baseline = json.dumps(self.svc.evaluate(facts), sort_keys=True, ensure_ascii=False)
        # 第二个服务以相反顺序创建同样的通知
        other_dir = tempfile.TemporaryDirectory()
        self.addCleanup(other_dir.cleanup)
        other = RuleIssuanceService(other_dir.name, clock=lambda: self.now)
        other.register_source("e1", "src-nhc", title="国家卫健委秋季健康提醒",
                              issuer="国家卫健委", version=1, actor=EDITOR, role="editor")
        other.draft_rule("d1", "rule-b", audience={},
                         trigger_facts={"symptoms": {"includes": "腹泻"}},
                         advice="建议 B", stop_self_care=["条件 B"],
                         referral_level="prompt_care", source_id="src-nhc",
                         source_version=1, valid_until="2026-12-31T23:59:59+08:00",
                         actor=EDITOR, role="editor")
        other.review_rule("r1", "rule-b", revision=1, decision="approved",
                          actor=CLINICIAN, role="clinician")
        other.sign_notice("s1", "n-002", rule_id="rule-b", rule_revision=1,
                          valid_until="2026-12-01T00:00:00+08:00", region_scope=[],
                          recipients=["hotline-staff"], actor=PUBLISHER, role="publisher")
        other.draft_rule("d2", "rule-diarrhea",
                         audience={"age_max": 12, "tags": ["儿童"]},
                         trigger_facts={"symptoms": {"includes": "腹泻"}},
                         advice="儿童腹泻注意补液与手卫生",
                         stop_self_care=["出现血便或持续高热立即就医"],
                         referral_level="community_clinic", source_id="src-nhc",
                         source_version=1, valid_until="2026-12-31T23:59:59+08:00",
                         actor=EDITOR, role="editor")
        other.review_rule("r2", "rule-diarrhea", revision=1, decision="approved",
                          actor=CLINICIAN, role="clinician")
        other.sign_notice("s2", "n-001", rule_id="rule-diarrhea", rule_revision=1,
                          valid_until="2026-12-01T00:00:00+08:00", region_scope=[],
                          recipients=["hotline-staff"], actor=PUBLISHER, role="publisher")
        self.assertEqual(baseline, json.dumps(other.evaluate(facts), sort_keys=True, ensure_ascii=False))


class ExpiryAndRecoveryTests(ServiceTestCase):
    def test_expired_rules_and_notices_stop_matching(self):
        self.register_source()
        self.draft(valid_until="2026-09-27T00:00:00+08:00")
        self.review()
        self.sign(valid_until="2026-09-27T00:00:00+08:00")
        self.send()
        self.advance(hours=17)  # 2026-09-27 01:00，已过有效期
        expired = self.svc.run_maintenance()
        self.assertEqual(sorted(["n-001#expired", "rule-diarrhea#r1:expired"]), sorted(expired))
        self.assertEqual("EXPIRED", self.svc.notice_state("n-001")["status"])
        self.assertEqual("EXPIRED", self.svc.rule_state("rule-diarrhea")["revisions"][1]["status"])
        result = self.svc.evaluate({"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]})
        self.assertEqual("no_match", result["status"])
        # 重复维护不产生重复事件
        self.assertEqual([], self.svc.run_maintenance())
        self.assertEqual(1, self.event_count("NOTICE_EXPIRED"))

    def test_restart_continues_expiry_and_pending_corrections(self):
        self.register_source()
        self.draft(valid_until="2026-09-27T00:00:00+08:00")
        self.review()
        self.sign(valid_until="2026-09-27T00:00:00+08:00")
        self.send()
        self.svc.submit_consultation("call-001", {"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]})
        self.svc.correct_source("corr-1", "src-nhc", source_version=1, reason="剂量更正",
                                actor=CLINICIAN, role="clinician")
        self.assertEqual(1, len(self.svc.pending_corrections()))
        self.advance(hours=17)
        # 重启：过期与待纠正事项继续处理
        restarted = self.boot()
        report = restarted.recover()
        self.assertEqual([], report["expired"])  # 构造时已补记，无重复
        self.assertEqual("EXPIRED", restarted.notice_state("n-001")["status"])
        pending = report["pending_corrections"]
        self.assertEqual(1, len(pending))
        self.assertEqual("corr:n-001:hotline-staff", pending[0]["task_id"])
        # 重启后咨询重放仍幂等
        replay = restarted.submit_consultation("call-001", {"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]})
        self.assertEqual("matched", replay["status"])
        self.assertEqual(1, sum(1 for e in self.journal_events() if e["event_type"] == "CONSULTATION_RECORDED"))
        # 重启后纠正记录仍可送达
        restarted.deliver_correction("deliver-1", "corr:n-001:hotline-staff",
                                     actor=PUBLISHER, role="publisher")
        self.assertEqual([], restarted.pending_corrections())
        # 再次重启状态保持一致
        again = self.boot()
        self.assertEqual("DELIVERED", again.correction_task("corr:n-001:hotline-staff")["status"])
        self.assertEqual(1, self.event_count("NOTICE_EXPIRED"))

    def test_journal_events_stay_within_contract(self):
        self.make_signed_notice()
        self.send()
        self.svc.submit_consultation("call-001", {"age_years": 4, "population_tags": ["儿童"], "symptoms": ["腹泻"]})
        self.svc.submit_consultation("call-001", {"age_years": 8, "population_tags": ["儿童"], "symptoms": ["腹泻"]})
        self.svc.correct_source("corr-1", "src-nhc", source_version=1, reason="更正",
                                actor=CLINICIAN, role="clinician")
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        for event in self.journal_events():
            self.assertEqual([], validate_event(event, schema), event["event_id"])
        # 每个聚合的版本号从 1 开始连续递增
        versions = {}
        for event in self.journal_events():
            key = (event["aggregate_type"], event["aggregate_id"])
            versions.setdefault(key, []).append(event["version"])
        for key, seq in versions.items():
            self.assertEqual(list(range(1, len(seq) + 1)), seq, key)


if __name__ == "__main__":
    unittest.main()
