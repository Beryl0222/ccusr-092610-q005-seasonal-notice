"""规则签发工作流服务。

在契约事件之上实现：

* 三权分立：编辑起草、临床审核、发布签发，三个环节必须由不同用户完成；
  系统只做规则匹配与信息整理，不诊断、不替代医嘱。
* 事件溯源：所有状态由五个领域事件重放得到；事件版本按聚合从 1 递增，
  event_id 全局幂等。
* 修订不变性：签发时固化规则快照，之后修订规则不会改变已发提醒的依据。
* 来源更正：暂停所有未发送消息；对已触达的人生成纠正任务与明确纠正记录，
  纠正通知持久化，重启后仍可查询与送达。
* 咨询幂等：相同 consultation_id 只处理一次，结果随首诊持久化。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .matcher import evaluate
from .model import (
    Actor,
    Advice,
    ConsultationFacts,
    GuidanceSource,
    MatchedAdvice,
    MatchResult,
    MedicationCaution,
    RedFlag,
    ReferralLevel,
    Release,
    Role,
    RulePackage,
)
from .store import AppendLog, EventStore, load_schema, utc_clock_factory


class WorkflowError(RuntimeError):
    """流程不满足前置条件（状态、有效期、范围等）。"""


class RoleError(WorkflowError):
    """操作者角色或分权约束不满足。"""


def _event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    version: int,
    now: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": f"{aggregate_type}:{aggregate_id}:{version}",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": now,
        "version": version,
        "payload": dict(payload),
    }


def _package_event_payload(package: RulePackage) -> dict[str, Any]:
    """契约要求 RULE_REVIEWED 载荷携带 audience 与 trigger_facts。"""
    data = package.to_dict()
    data["trigger_facts"] = data.pop("trigger")
    return data


class _RevisionState:
    def __init__(self, revision: int) -> None:
        self.revision = revision
        self.drafted_by: Actor | None = None
        self.drafted_at: str | None = None
        self.reviewed_by: Actor | None = None
        self.reviewed_at: str | None = None
        self.package: RulePackage | None = None

    @property
    def is_reviewed(self) -> bool:
        return self.reviewed_by is not None and self.package is not None


class _RuleState:
    def __init__(self, rule_id: str, source_id: str) -> None:
        self.rule_id = rule_id
        self.source_id = source_id
        self.revisions: dict[int, _RevisionState] = {}

    @property
    def current_revision(self) -> int:
        return max(self.revisions)


class _CorrectionState:
    def __init__(
        self,
        task_id: str,
        release_id: str,
        recipient_id: str,
        reason: str,
        new_source_id: str | None,
        original: Mapping[str, Any],
        opened_at: str,
    ) -> None:
        self.task_id = task_id
        self.release_id = release_id
        self.recipient_id = recipient_id
        self.reason = reason
        self.new_source_id = new_source_id
        self.original = dict(original)
        self.opened_at = opened_at
        self.delivered_at: str | None = None


class RuleSigningService:
    def __init__(
        self,
        events_path: str | Path,
        sidecar_path: str | Path,
        schema: Mapping[str, Any] | None = None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._clock = clock or utc_clock_factory()
        self.store = EventStore(events_path, schema or load_schema())
        # sidecar：发件箱状态流转、咨询首诊记录、纠正送达状态（本地工作状态）
        self._sidecar = AppendLog(sidecar_path)
        self._lock = threading.RLock()
        self._sources: dict[str, GuidanceSource] = {}
        self._rules: dict[str, _RuleState] = {}
        self._releases: dict[str, Release] = {}
        self._sign_order: list[str] = []
        self._sent: set[tuple[str, str]] = set()
        self._corrections: dict[str, _CorrectionState] = {}
        self._messages: dict[str, dict[str, Any]] = {}
        self._consultations: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, int] = defaultdict(int)
        self._rebuild()

    # ------------------------------------------------------------------ 重放

    def _rebuild(self) -> None:
        for event in self.store.replay():
            self._project(event)
        for record in self._sidecar.replay():
            kind = record.get("type")
            ref = record.get("ref")
            if kind == "message":
                self._messages[ref] = record  # 后写状态覆盖前写状态
            elif kind == "consultation":
                self._consultations.setdefault(ref, record)  # 首诊为准
            elif kind == "correction_delivery":
                state = self._corrections.get(ref)
                if state is not None and record.get("delivered_at"):
                    state.delivered_at = record["delivered_at"]

    def _project(self, event: Mapping[str, Any]) -> None:
        agg_type = event["aggregate_type"]
        agg_id = event["aggregate_id"]
        self._versions[f"{agg_type}:{agg_id}"] = max(
            self._versions[f"{agg_type}:{agg_id}"], event["version"]
        )
        p = event["payload"]
        et = event["event_type"]
        if et == "SOURCE_REGISTERED":
            self._sources[agg_id] = GuidanceSource.from_dict({"source_id": agg_id, **p})
        elif et == "RULE_REVIEWED":
            revision = int(p["revision"])
            rule = self._rules.setdefault(agg_id, _RuleState(agg_id, p["source_id"]))
            state = rule.revisions.setdefault(revision, _RevisionState(revision))
            actor = Actor.from_dict(p["actor"])
            package = RulePackage.from_dict(p)
            if p["action"] == "drafted":
                state.drafted_by = actor
                state.drafted_at = event["occurred_at"]
            else:
                state.reviewed_by = actor
                state.reviewed_at = event["occurred_at"]
            state.package = package  # 起草与审核携带同版内容，审核版为生效内容
        elif et == "NOTICE_SIGNED":
            self._releases[agg_id] = Release(
                release_id=agg_id,
                rule_id=p["rule_id"],
                revision=int(p["rule_revision"]),
                source_id=p["source_id"],
                package_snapshot=RulePackage.from_dict(p["package_snapshot"]),
                signed_at=event["occurred_at"],
                signer=Actor.from_dict(p["signer"]),
                valid_until=p["valid_until"],
                region_codes=frozenset(p.get("region_codes", [])),
                status="active",
            )
            self._sign_order.append(agg_id)
        elif et == "NOTICE_SENT":
            self._sent.add((agg_id, p["recipient_id"]))
        elif et == "CORRECTION_OPENED":
            self._corrections[agg_id] = _CorrectionState(
                task_id=agg_id,
                release_id=p["affected_release"],
                recipient_id=p["recipient_id"],
                reason=p["reason"],
                new_source_id=p.get("new_source_id"),
                original=p.get("original", {}),
                opened_at=event["occurred_at"],
            )

    def _next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions[f"{aggregate_type}:{aggregate_id}"] + 1

    def _append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """组装、追加并就地投影（持锁调用）。"""
        version = self._next_version(aggregate_type, aggregate_id)
        event = _event(event_type, aggregate_type, aggregate_id, version, self._now(), payload)
        self.store.append(event)
        self._project(event)
        return event

    def _now(self) -> str:
        return self._clock().isoformat()

    def current_time(self) -> str:
        """服务当前时钟（带时区 ISO 字符串），供调用方计算有效期。"""
        return self._now()

    # -------------------------------------------------------------- 来源登记

    def register_source(self, actor: Actor, source: GuidanceSource) -> dict[str, Any]:
        self._require_role(actor, Role.EDITOR)
        with self._lock:
            return self._register_source_locked(source)

    def _register_source_locked(self, source: GuidanceSource) -> dict[str, Any]:
        if source.source_id in self._sources:
            raise WorkflowError(f"来源已登记: {source.source_id}")
        body = source.to_dict()
        body.pop("source_id")
        return self._append(
            "SOURCE_REGISTERED",
            "guidance_source",
            source.source_id,
            {k: v for k, v in body.items() if v is not None},
        )

    # ------------------------------------------------------------ 起草/审核

    @staticmethod
    def _require_role(actor: Actor, role: Role) -> None:
        if actor.role is not role:
            raise RoleError(f"该操作需要 {role.value} 角色，当前为 {actor.role.value}")

    def _rule_payload(
        self, source_id: str, revision: int, action: str, actor: Actor, package: RulePackage
    ) -> dict[str, Any]:
        data = _package_event_payload(package)
        data.update({"action": action, "source_id": source_id, "revision": revision,
                     "actor": actor.to_dict()})
        return data

    def draft_rule(
        self, actor: Actor, rule_id: str, source_id: str, package: RulePackage
    ) -> int:
        """内容编辑起草规则（或修订稿），返回业务版本号。"""
        self._require_role(actor, Role.EDITOR)
        with self._lock:
            if source_id not in self._sources:
                raise WorkflowError(f"来源未登记: {source_id}")
            existing = self._rules.get(rule_id)
            revision = 1 if existing is None else existing.current_revision + 1
            if existing is not None:
                prior = existing.revisions[existing.current_revision]
                if not prior.is_reviewed:
                    raise WorkflowError("规则上一版本尚未完成临床审核，不能新增修订")
            payload = self._rule_payload(source_id, revision, "drafted", actor, package)
            self._append("RULE_REVIEWED", "audience_rule", rule_id, payload)
            return revision

    def review_rule(
        self, actor: Actor, rule_id: str, revision: int, package: RulePackage
    ) -> None:
        """临床人员审核指定版本；审核内容留档，审核通过后才能签发。"""
        self._require_role(actor, Role.CLINICIAN)
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None or revision not in rule.revisions:
                raise WorkflowError(f"待审核版本不存在: {rule_id} r{revision}")
            state = rule.revisions[revision]
            if state.drafted_by is None:
                raise WorkflowError("该版本尚未起草")
            if state.drafted_by.user_id == actor.user_id:
                raise RoleError("起草与审核不能由同一人完成")
            if state.is_reviewed:
                raise WorkflowError("该版本已审核")
            payload = self._rule_payload(rule.source_id, revision, "reviewed", actor, package)
            self._append("RULE_REVIEWED", "audience_rule", rule_id, payload)

    # ------------------------------------------------------------------ 签发

    def sign_release(
        self,
        actor: Actor,
        release_id: str,
        rule_id: str,
        valid_until: str,
        region_codes: Sequence[str] = (),
    ) -> Release:
        """发布人员对已审核版本签发；规则内容在此刻固化为快照。"""
        self._require_role(actor, Role.PUBLISHER)
        with self._lock:
            if release_id in self._releases:
                raise WorkflowError(f"发布号已存在: {release_id}")
            rule = self._rules.get(rule_id)
            if rule is None:
                raise WorkflowError(f"规则不存在: {rule_id}")
            state = rule.revisions[rule.current_revision]
            if not state.is_reviewed or state.package is None:
                raise WorkflowError("当前规则版本尚未通过临床审核，不能签发")
            participants = {state.drafted_by.user_id, state.reviewed_by.user_id, actor.user_id}
            if len(participants) < 3:
                raise RoleError("起草、审核、签发必须由三个不同的人完成")
            now = self._now()
            if not valid_until > now:
                raise WorkflowError("建议有效期必须晚于签发时间")
            payload = {
                "rule_id": rule_id,
                "rule_revision": state.revision,
                "source_id": rule.source_id,
                "package_snapshot": _package_event_payload(state.package),
                "signer": actor.to_dict(),
                "valid_until": valid_until,
                "region_codes": sorted(region_codes),
                "signed_at": now,
            }
            self._append("NOTICE_SIGNED", "notice_release", release_id, payload)
            return self._releases[release_id]

    # ------------------------------------------------------------ 发件箱/发送

    def queue_message(self, release_id: str, recipient_id: str) -> str:
        """把一条提醒放入待发送发件箱，幂等。"""
        with self._lock:
            self._require_release(release_id)
            message_id = f"{release_id}|{recipient_id}"
            if message_id in self._messages:
                return message_id
            record = {
                "ref": message_id,
                "type": "message",
                "release_id": release_id,
                "recipient_id": recipient_id,
                "status": "queued",
                "queued_at": self._now(),
            }
            self._sidecar.append(record)
            self._messages[message_id] = record
            return message_id

    def send_message(self, message_id: str) -> bool:
        """发送一条待发消息。过期、暂停、已纠正的发布不允许发送。

        返回 True 表示本次发送；已发送返回 False。
        """
        with self._lock:
            message = self._messages.get(message_id)
            if message is None:
                raise WorkflowError(f"发件箱中没有该消息: {message_id}")
            if message["status"] == "sent":
                return False
            if message["status"] == "paused":
                raise WorkflowError("消息因来源更正已暂停，等待新依据后再处理")
            release = self._require_release(message["release_id"])
            self._assert_sendable(release)
            self._append(
                "NOTICE_SENT",
                "notice_release",
                release.release_id,
                {"recipient_id": message["recipient_id"], "sent_at": self._now()},
            )
            record = {**message, "status": "sent", "sent_at": self._now()}
            self._sidecar.append(record)
            self._messages[message_id] = record
            return True

    def queued_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(m) for m in self._messages.values() if m["status"] == "queued"]

    def _assert_sendable(self, release: Release) -> None:
        now = self._now()
        if release.signed_at > now:
            raise WorkflowError("发布尚未生效")
        if release.valid_until < now:
            raise WorkflowError("规则已过有效期，不能发送")
        if release.release_id in self._corrected_releases():
            raise WorkflowError("来源已更正，该发布暂停发送")
        if release.release_id in self._superseded_releases():
            raise WorkflowError("规则已有新签发版本，旧发布不再发送")

    # ------------------------------------------------------------------ 更正

    def correct_source(
        self,
        actor: Actor,
        new_source: GuidanceSource,
        old_source_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """临床人员登记来源更正，并处理所有引用旧来源的发布。

        * 未发送消息：立即暂停（不产生 NOTICE_SENT）；
        * 已触达收件人：逐人生成纠正任务与明确纠正记录，通知待送达，
          重启后仍可在 :meth:`pending_corrections` 中处理。
        """
        self._require_role(actor, Role.CLINICIAN)
        if not reason.strip():
            raise WorkflowError("更正必须说明原因")
        with self._lock:
            if old_source_id not in self._sources:
                raise WorkflowError(f"旧来源不存在: {old_source_id}")
            if new_source.source_id not in self._sources:
                self._register_source_locked(
                    GuidanceSource(
                        source_id=new_source.source_id,
                        title=new_source.title,
                        publisher=new_source.publisher,
                        source_url=new_source.source_url,
                        supersedes=old_source_id,
                    )
                )
            paused: list[str] = []
            corrected: list[str] = []
            for release_id, release in sorted(self._releases.items()):
                if release.source_id != old_source_id:
                    continue
                for message_id, message in sorted(self._messages.items()):
                    if message["release_id"] != release_id or message["status"] != "queued":
                        continue
                    record = {
                        **message,
                        "status": "paused",
                        "paused_at": self._now(),
                        "reason": reason,
                    }
                    self._sidecar.append(record)
                    self._messages[message_id] = record
                    paused.append(message_id)
                recipients = sorted(
                    recipient for (rid, recipient) in self._sent if rid == release_id
                )
                for recipient in recipients:
                    task_id = f"correction|{release_id}|{recipient}"
                    if task_id in self._corrections:
                        continue
                    payload = {
                        "affected_release": release_id,
                        "reason": reason,
                        "recipient_id": recipient,
                        "new_source_id": new_source.source_id,
                        "original": {
                            "release_id": release_id,
                            "rule_id": release.rule_id,
                            "rule_revision": release.revision,
                            "source_id": release.source_id,
                            "signed_at": release.signed_at,
                            "valid_until": release.valid_until,
                            "title": release.package_snapshot.title,
                            "advice": [a.to_dict() for a in release.package_snapshot.advice],
                        },
                        "opened_by": actor.to_dict(),
                    }
                    self._append("CORRECTION_OPENED", "correction_task", task_id, payload)
                    corrected.append(task_id)
            return {
                "new_source_id": new_source.source_id,
                "supersedes": old_source_id,
                "reason": reason,
                "paused_messages": paused,
                "correction_tasks": corrected,
            }

    def correction_record(self, task_id: str) -> dict[str, Any]:
        """返回面向已触达者的明确纠正记录。"""
        with self._lock:
            state = self._corrections.get(task_id)
            if state is None:
                raise WorkflowError(f"纠正任务不存在: {task_id}")
            original = state.original
            return {
                "task_id": state.task_id,
                "recipient_id": state.recipient_id,
                "opened_at": state.opened_at,
                "delivered_at": state.delivered_at,
                "reason": state.reason,
                "new_source_id": state.new_source_id,
                "superseded_notice": original,
                "message": (
                    f"您此前收到的《{original.get('title', '')}》提醒"
                    f"（发布号 {state.release_id}，依据 {original.get('source_id')}）"
                    f"所依据的来源已更正：{state.reason}。请以新来源 "
                    f"{state.new_source_id} 为准；如已按旧建议处理或仍有不适，"
                    "请尽快联系热线或就医，本提醒不替代医嘱。"
                ),
            }

    def pending_corrections(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self.correction_record(task_id)
                for task_id in sorted(self._corrections)
                if self._corrections[task_id].delivered_at is None
            ]

    def deliver_correction(self, task_id: str) -> bool:
        with self._lock:
            state = self._corrections.get(task_id)
            if state is None:
                raise WorkflowError(f"纠正任务不存在: {task_id}")
            if state.delivered_at is not None:
                return False
            delivered_at = self._now()
            self._sidecar.append(
                {"ref": task_id, "type": "correction_delivery", "delivered_at": delivered_at}
            )
            state.delivered_at = delivered_at
            return True

    # ------------------------------------------------------------------ 咨询

    def handle_consultation(
        self,
        facts: ConsultationFacts,
        conflicting_facts: Sequence[str] = (),
    ) -> MatchResult:
        """对一次咨询执行匹配。相同 consultation_id 只保留首诊结果。"""
        with self._lock:
            prior = self._consultations.get(facts.consultation_id)
            if prior is not None:
                return _match_from_dict(prior["result"])
            result = evaluate(
                self.effective_releases(),
                facts,
                self._now(),
                conflicting_facts=tuple(conflicting_facts),
            )
            record = {
                "ref": facts.consultation_id,
                "type": "consultation",
                "facts": facts.to_dict(),
                "conflicting_facts": sorted(conflicting_facts),
                "result": result.to_dict(),
                "recorded_at": self._now(),
            }
            self._sidecar.append(record)
            self._consultations[facts.consultation_id] = record
            return result

    # ------------------------------------------------------------------ 查询

    def _require_release(self, release_id: str) -> Release:
        release = self._releases.get(release_id)
        if release is None:
            raise WorkflowError(f"发布不存在: {release_id}")
        return release

    def _corrected_releases(self) -> dict[str, set[str]]:
        grouped: dict[str, set[str]] = defaultdict(set)
        for state in self._corrections.values():
            grouped[state.release_id].add(state.recipient_id)
        return grouped

    def _superseded_releases(self) -> set[str]:
        """同规则后签发的发布使旧发布在匹配中被取代（旧依据仍留档）。"""
        latest: dict[str, str] = {}
        for release_id in self._sign_order:
            latest[self._releases[release_id].rule_id] = release_id
        return {
            release_id
            for release_id, release in self._releases.items()
            if latest.get(release.rule_id) != release_id
        }

    def effective_releases(self, now: str | None = None) -> list[Release]:
        """当前可用于匹配的发布：有效、未过期、未被取代、未因更正暂停。"""
        now = now or self._now()
        corrected = self._corrected_releases()
        superseded = self._superseded_releases()
        result = [
            release
            for release_id, release in self._releases.items()
            if release_id not in superseded
            and release_id not in corrected
            and release.signed_at <= now <= release.valid_until
        ]
        return sorted(result, key=lambda r: r.release_id)

    def get_release(self, release_id: str) -> Release:
        with self._lock:
            return self._require_release(release_id)

    def messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._messages[k]) for k in sorted(self._messages)]

    def corrections(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.correction_record(k) for k in sorted(self._corrections)]


def _match_from_dict(data: Mapping[str, Any]) -> MatchResult:
    """从持久化的首诊记录重建 MatchResult（咨询幂等重放用）。"""
    return MatchResult(
        consultation_id=data["consultation_id"],
        matched=tuple(
            MatchedAdvice(
                release_id=m["release_id"],
                rule_id=m["rule_id"],
                revision=m["revision"],
                source_id=m["source_id"],
                advice=tuple(Advice.from_dict(a) for a in m.get("advice", [])),
                medication_cautions=tuple(
                    MedicationCaution.from_dict(c) for c in m.get("medication_cautions", [])
                ),
            )
            for m in data.get("matched", [])
        ),
        red_flags=tuple(
            RedFlag(
                symptom=f["symptom"],
                referral=ReferralLevel(f["referral"]),
                message=f["message"],
                release_id=f["release_id"],
            )
            for f in data.get("red_flags", [])
        ),
        undetermined=tuple(data.get("undetermined", [])),
        escalation_reasons=tuple(data.get("escalation_reasons", [])),
        conflict_reasons=tuple(data.get("conflict_reasons", [])),
        referral=ReferralLevel(data["referral"]),
        needs_human=data.get("needs_human", False),
        evaluated_at=data["evaluated_at"],
    )
