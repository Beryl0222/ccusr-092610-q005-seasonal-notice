"""秋季健康提醒规则签发服务。

职责边界：本服务只做规则匹配与信息整理，不诊断疾病、不替代医嘱。

流水线职责分离：内容编辑（editor）起草规则，临床人员（clinician）审核，
发布人员（publisher）签发与发送；同一人不能完成同一修订的多个环节。

状态持久化在 JSONL 事件日志中，重启后重放恢复；过期规则与待纠正通知
在启动时由 recover() 继续处理。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import validate_event
from .evaluation import (
    DISCLAIMER,
    REFERRAL_LEVELS,
    build_result,
    normalize_condition,
)
from .storage import Journal

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
)

#: 角色：内容编辑 / 临床人员 / 发布人员
ROLE_EDITOR = "editor"
ROLE_CLINICIAN = "clinician"
ROLE_PUBLISHER = "publisher"

#: 规则修订状态
RULE_DRAFT = "DRAFT"
RULE_REVIEWED = "REVIEWED"
RULE_REJECTED = "REJECTED"
RULE_EXPIRED = "EXPIRED"

#: 通知状态
NOTICE_SIGNED = "SIGNED"
NOTICE_SENT = "SENT"
NOTICE_PAUSED = "PAUSED"
NOTICE_EXPIRED = "EXPIRED"

#: 纠正任务状态
TASK_PENDING = "PENDING"
TASK_DELIVERED = "DELIVERED"

#: 咨询事件状态
CONSULTATION_RECORDED = "recorded"
CONSULTATION_ESCALATED = "escalated"
CONSULTATION_RESOLVED = "resolved"

_AUDIENCE_KEYS = ("age_min", "age_max", "regions", "tags")
_LIST_OPERATORS = ("in", "includes_any", "includes_all")
_NUMERIC_OPERATORS = ("lt", "lte", "gt", "gte")


class ServiceError(Exception):
    """业务错误，携带稳定错误码与中文说明。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SourceState:
    source_id: str
    versions: dict[int, dict] = field(default_factory=dict)
    corrections: list[dict] = field(default_factory=list)


@dataclass
class RuleRevisionState:
    revision: int
    status: str
    draft: dict
    drafted_by: str
    reviewed_by: str | None = None
    decision: str | None = None


@dataclass
class RuleState:
    rule_id: str
    revisions: dict[int, RuleRevisionState] = field(default_factory=dict)


@dataclass
class NoticeState:
    notice_id: str
    status: str
    snapshot: dict
    valid_until: str
    recipients: list[str]
    signed_by: str
    reached: list[str] = field(default_factory=list)
    correction_open: bool = False


@dataclass
class ConsultationState:
    consultation_id: str
    canon: str
    facts: dict
    result: dict
    status: str
    escalations: list[dict] = field(default_factory=list)
    resolution: dict | None = None


@dataclass
class CorrectionTaskState:
    task_id: str
    status: str
    opened: dict
    opened_at: str
    delivered: dict | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash12(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:12]


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_instant(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ServiceError("invalid_input", f"{field_name} 必须是带时区的 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ServiceError("invalid_input", f"{field_name} 不是合法的 ISO 时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceError("invalid_input", f"{field_name} 必须携带时区")
    return parsed


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _diff_keys(original: Mapping[str, Any], incoming: Mapping[str, Any]) -> list[str]:
    changed = []
    for key in set(original) | set(incoming):
        if key not in original or key not in incoming:
            changed.append(key)
        elif _canonical(original[key]) != _canonical(incoming[key]):
            changed.append(key)
    return sorted(changed)


def _validate_fact_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(isinstance(item, (str, int, float, bool)) for item in value)
    return False


class RuleIssuanceService:
    """规则签发服务：规则流水线、咨询匹配、来源更正与重启恢复。"""

    def __init__(
        self,
        data_dir: str | Path,
        clock: Callable[[], datetime] | None = None,
        schema_path: str | Path | None = None,
    ):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        schema_file = Path(schema_path) if schema_path else DEFAULT_SCHEMA_PATH
        self._schema = json.loads(schema_file.read_text(encoding="utf-8"))
        self._journal = Journal(Path(data_dir) / "journal.jsonl")
        self._counts: dict[tuple[str, str], int] = {}
        self._event_index: dict[str, tuple[str, str, str]] = {}
        self.sources: dict[str, SourceState] = {}
        self.rules: dict[str, RuleState] = {}
        self.notices: dict[str, NoticeState] = {}
        self.consultations: dict[str, ConsultationState] = {}
        self.corrections: dict[str, CorrectionTaskState] = {}
        self._corrected_sources: dict[tuple[str, int], dict] = {}
        for event in self._journal.load():
            issues = validate_event(event, self._schema)
            if issues:
                raise ServiceError(
                    "journal_corrupt",
                    "事件日志含契约外事件: "
                    + "; ".join(f"{issue.field}:{issue.code}" for issue in issues),
                )
            if event["event_id"] in self._event_index:
                raise ServiceError(
                    "journal_corrupt", f"事件日志含重复事件标识 {event['event_id']}"
                )
            self._apply(event)
            self._index(event)
        self.recover()

    # ------------------------------------------------------------------
    # 事件骨架
    # ------------------------------------------------------------------

    def _now(self, now: datetime | None = None) -> datetime:
        moment = now if now is not None else self._clock()
        if (
            not isinstance(moment, datetime)
            or moment.tzinfo is None
            or moment.utcoffset() is None
        ):
            raise ServiceError("invalid_input", "时间必须携带时区")
        return moment

    def _event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        occurred_at: datetime,
        event_id: str,
    ) -> dict:
        return {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": _iso(occurred_at),
            "version": self._counts.get((aggregate_type, aggregate_id), 0) + 1,
            "payload": payload,
        }

    def _index(self, event: dict) -> None:
        self._event_index[event["event_id"]] = (
            event["event_type"],
            event["aggregate_type"],
            event["aggregate_id"],
        )

    def _append(self, event: dict) -> bool:
        if event["event_id"] in self._event_index:
            return False
        issues = validate_event(event, self._schema)
        if issues:
            raise ServiceError(
                "contract_violation",
                "; ".join(f"{issue.field}:{issue.code}" for issue in issues),
            )
        # 以 JSON 往返得到规范副本，保证内存状态与日志完全一致，
        # 调用方事后修改入参不会影响已登记内容。
        event = json.loads(json.dumps(event, ensure_ascii=False))
        self._journal.append(event)
        self._apply(event)
        self._index(event)
        return True

    def _apply(self, event: dict) -> None:
        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._counts.get(key, 0) + 1
        if event["version"] != expected:
            raise ServiceError(
                "journal_corrupt", f"事件 {event['event_id']} 版本号不连续"
            )
        self._counts[key] = expected
        handler = getattr(self, f"_fold_{event['event_type']}", None)
        if handler is None:
            raise ServiceError(
                "journal_corrupt", f"未登记的事件类型 {event['event_type']}"
            )
        handler(event["aggregate_id"], event["payload"], event)

    def _replay(self, event_id: str, expected_type: str) -> dict | None:
        fingerprint = self._event_index.get(event_id)
        if fingerprint is None:
            return None
        if fingerprint[0] != expected_type:
            raise ServiceError(
                "event_id_conflict", f"事件标识 {event_id} 已用于 {fingerprint[0]}"
            )
        return {
            "event_id": event_id,
            "replay": True,
            "event_type": fingerprint[0],
            "aggregate_id": fingerprint[2],
        }

    # ------------------------------------------------------------------
    # 事件折叠
    # ------------------------------------------------------------------

    def _fold_SOURCE_REGISTERED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        source = self.sources.setdefault(aggregate_id, SourceState(aggregate_id))
        source.versions[payload["version"]] = payload

    def _fold_SOURCE_CORRECTED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        self.sources[aggregate_id].corrections.append(payload)
        self._corrected_sources[(aggregate_id, payload["source_version"])] = payload

    def _fold_RULE_DRAFTED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        rule = self.rules.setdefault(aggregate_id, RuleState(aggregate_id))
        rule.revisions[payload["revision"]] = RuleRevisionState(
            revision=payload["revision"],
            status=RULE_DRAFT,
            draft=payload,
            drafted_by=payload["actor"],
        )

    def _fold_RULE_REVIEWED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        revision = self.rules[aggregate_id].revisions[payload["revision"]]
        revision.status = (
            RULE_REVIEWED if payload["decision"] == "approved" else RULE_REJECTED
        )
        revision.reviewed_by = payload["actor"]
        revision.decision = payload["decision"]

    def _fold_RULE_EXPIRED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        self.rules[aggregate_id].revisions[payload["revision"]].status = RULE_EXPIRED

    def _fold_NOTICE_SIGNED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        self.notices[aggregate_id] = NoticeState(
            notice_id=aggregate_id,
            status=NOTICE_SIGNED,
            snapshot=payload["snapshot"],
            valid_until=payload["valid_until"],
            recipients=list(payload["recipients"]),
            signed_by=payload["actor"],
        )

    def _fold_NOTICE_SENT(self, aggregate_id: str, payload: dict, event: dict) -> None:
        notice = self.notices[aggregate_id]
        notice.status = NOTICE_SENT
        notice.reached = list(payload["reached"])

    def _fold_NOTICE_PAUSED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        self.notices[aggregate_id].status = NOTICE_PAUSED

    def _fold_NOTICE_EXPIRED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        self.notices[aggregate_id].status = NOTICE_EXPIRED

    def _fold_CORRECTION_OPENED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        self.corrections[aggregate_id] = CorrectionTaskState(
            task_id=aggregate_id,
            status=TASK_PENDING,
            opened=payload,
            opened_at=event["occurred_at"],
        )
        notice = self.notices.get(payload["affected_release"])
        if notice is not None:
            notice.correction_open = True

    def _fold_CORRECTION_DELIVERED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        task = self.corrections[aggregate_id]
        task.status = TASK_DELIVERED
        task.delivered = payload

    def _fold_CONSULTATION_RECORDED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        self.consultations[aggregate_id] = ConsultationState(
            consultation_id=aggregate_id,
            canon=_canonical(payload["facts"]),
            facts=payload["facts"],
            result=payload["result"],
            status=CONSULTATION_RECORDED,
        )

    def _fold_CONSULTATION_ESCALATED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        consultation = self.consultations[aggregate_id]
        consultation.status = CONSULTATION_ESCALATED
        consultation.escalations.append(payload)

    def _fold_CONSULTATION_RESOLVED(self, aggregate_id: str, payload: dict, event: dict) -> None:
        consultation = self.consultations[aggregate_id]
        consultation.status = CONSULTATION_RESOLVED
        consultation.resolution = payload

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    def _require_role(self, role: Any, allowed: tuple[str, ...], action: str) -> None:
        if role not in allowed:
            raise ServiceError(
                "role_forbidden",
                f"{action}需要角色 {'/'.join(allowed)}，当前为 {role!r}",
            )

    @staticmethod
    def _require_actor(actor: Any) -> None:
        if not _non_empty_str(actor):
            raise ServiceError("invalid_input", "操作人必须是非空字符串")

    @staticmethod
    def _validate_audience(audience: Any) -> None:
        if not isinstance(audience, Mapping):
            raise ServiceError("invalid_input", "适用人群必须是 JSON 对象")
        for key in audience:
            if key not in _AUDIENCE_KEYS:
                raise ServiceError("invalid_input", f"适用人群含未登记字段 {key}")
        age_min = audience.get("age_min")
        age_max = audience.get("age_max")
        for label, bound in (("age_min", age_min), ("age_max", age_max)):
            if bound is not None and not _is_number(bound):
                raise ServiceError("invalid_input", f"{label} 必须是数字")
        if (
            _is_number(age_min)
            and _is_number(age_max)
            and age_min > age_max
        ):
            raise ServiceError("invalid_input", "age_min 不能大于 age_max")
        for label in ("regions", "tags"):
            values = audience.get(label)
            if values is not None and (
                not isinstance(values, list)
                or any(not _non_empty_str(item) for item in values)
            ):
                raise ServiceError("invalid_input", f"{label} 必须是非空字符串列表")

    @staticmethod
    def _validate_trigger_facts(trigger_facts: Any) -> None:
        if not isinstance(trigger_facts, Mapping):
            raise ServiceError("invalid_input", "触发事实必须是 JSON 对象")
        for key, condition in trigger_facts.items():
            if not _non_empty_str(key):
                raise ServiceError("invalid_input", "触发事实键必须是非空字符串")
            try:
                operator, expected = normalize_condition(condition)
            except ValueError as exc:
                raise ServiceError("invalid_input", str(exc)) from None
            if operator in _LIST_OPERATORS and not isinstance(expected, list):
                raise ServiceError(
                    "invalid_input", f"触发事实 {key} 的 {operator} 期望值必须是列表"
                )
            if operator in _NUMERIC_OPERATORS and not _is_number(expected):
                raise ServiceError(
                    "invalid_input", f"触发事实 {key} 的 {operator} 期望值必须是数字"
                )

    @staticmethod
    def _validate_facts(facts: Any) -> None:
        if not isinstance(facts, Mapping):
            raise ServiceError("invalid_input", "咨询事实必须是 JSON 对象")
        for key, value in facts.items():
            if not _non_empty_str(key):
                raise ServiceError("invalid_input", "事实键必须是非空字符串")
            if not _validate_fact_value(value):
                raise ServiceError(
                    "invalid_input", f"事实 {key} 的值必须是标量或标量列表"
                )

    # ------------------------------------------------------------------
    # 建议来源
    # ------------------------------------------------------------------

    def register_source(
        self,
        event_id: str,
        source_id: str,
        *,
        title: str,
        issuer: str,
        version: int,
        actor: str,
        role: str,
        now: datetime | None = None,
    ) -> dict:
        """登记建议来源的一个版本（内容编辑或临床人员）。"""
        self._require_role(role, (ROLE_EDITOR, ROLE_CLINICIAN), "登记建议来源")
        self._require_actor(actor)
        replay = self._replay(event_id, "SOURCE_REGISTERED")
        if replay:
            return replay
        if not _non_empty_str(source_id):
            raise ServiceError("invalid_input", "来源标识必须是非空字符串")
        if not _non_empty_str(title) or not _non_empty_str(issuer):
            raise ServiceError("invalid_input", "来源标题与发布机构必须是非空字符串")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ServiceError("invalid_input", "来源版本必须是从 1 开始的正整数")
        source = self.sources.get(source_id)
        if source is not None and version in source.versions:
            raise ServiceError(
                "source_version_exists", f"来源 {source_id} 的版本 {version} 已登记"
            )
        moment = self._now(now)
        self._append(
            self._event(
                "SOURCE_REGISTERED",
                "guidance_source",
                source_id,
                {
                    "title": title,
                    "issuer": issuer,
                    "version": version,
                    "actor": actor,
                    "role": role,
                },
                moment,
                event_id,
            )
        )
        return {"event_id": event_id, "source_id": source_id, "version": version}

    # ------------------------------------------------------------------
    # 规则流水线：起草 → 审核 → 签发 → 发送
    # ------------------------------------------------------------------

    def draft_rule(
        self,
        event_id: str,
        rule_id: str,
        *,
        audience: Mapping[str, Any],
        trigger_facts: Mapping[str, Any],
        advice: str,
        stop_self_care: list[str],
        referral_level: str,
        source_id: str,
        source_version: int,
        valid_until: str,
        actor: str,
        role: str,
        revision: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """起草规则修订（内容编辑）。"""
        self._require_role(role, (ROLE_EDITOR,), "起草规则")
        self._require_actor(actor)
        replay = self._replay(event_id, "RULE_DRAFTED")
        if replay:
            return replay
        if not _non_empty_str(rule_id):
            raise ServiceError("invalid_input", "规则标识必须是非空字符串")
        self._validate_audience(audience)
        self._validate_trigger_facts(trigger_facts)
        if not _non_empty_str(advice):
            raise ServiceError("invalid_input", "建议内容必须是非空字符串")
        if (
            not isinstance(stop_self_care, list)
            or not stop_self_care
            or any(not _non_empty_str(item) for item in stop_self_care)
        ):
            raise ServiceError(
                "invalid_input", "停止自我处理条件必须是非空字符串列表且至少一条"
            )
        if referral_level not in REFERRAL_LEVELS:
            raise ServiceError(
                "invalid_input",
                f"转介级别必须是 {'/'.join(REFERRAL_LEVELS)} 之一",
            )
        source = self.sources.get(source_id)
        if source is None or source_version not in source.versions:
            raise ServiceError(
                "unknown_source", f"来源 {source_id} 的版本 {source_version} 未登记"
            )
        if (source_id, source_version) in self._corrected_sources:
            raise ServiceError(
                "source_corrected", f"来源 {source_id} 的版本 {source_version} 已被更正"
            )
        moment = self._now(now)
        valid_at = _parse_instant(valid_until, "valid_until")
        if valid_at <= moment:
            raise ServiceError("invalid_input", "建议有效期必须晚于当前时间")
        rule = self.rules.get(rule_id)
        existing = sorted(rule.revisions) if rule else []
        next_revision = (existing[-1] + 1) if existing else 1
        if revision is None:
            revision = next_revision
        if revision != next_revision:
            raise ServiceError(
                "revision_conflict", f"规则 {rule_id} 下一修订应为 {next_revision}"
            )
        if existing and rule.revisions[existing[-1]].status == RULE_DRAFT:
            raise ServiceError(
                "draft_open", f"规则 {rule_id} 上一修订仍在起草中，请先审核"
            )
        self._append(
            self._event(
                "RULE_DRAFTED",
                "audience_rule",
                rule_id,
                {
                    "revision": revision,
                    "audience": dict(audience),
                    "trigger_facts": dict(trigger_facts),
                    "advice": advice,
                    "stop_self_care": list(stop_self_care),
                    "referral_level": referral_level,
                    "source_id": source_id,
                    "source_version": source_version,
                    "valid_until": valid_until,
                    "actor": actor,
                    "role": role,
                },
                moment,
                event_id,
            )
        )
        return {
            "event_id": event_id,
            "rule_id": rule_id,
            "revision": revision,
            "status": RULE_DRAFT,
        }

    def review_rule(
        self,
        event_id: str,
        rule_id: str,
        *,
        revision: int,
        decision: str,
        actor: str,
        role: str,
        note: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """审核规则修订（临床人员，且不能是起草人）。"""
        self._require_role(role, (ROLE_CLINICIAN,), "审核规则")
        self._require_actor(actor)
        replay = self._replay(event_id, "RULE_REVIEWED")
        if replay:
            return replay
        if decision not in ("approved", "rejected"):
            raise ServiceError("invalid_input", "审核结论必须是 approved 或 rejected")
        rule = self.rules.get(rule_id)
        revision_state = rule.revisions.get(revision) if rule else None
        if revision_state is None:
            raise ServiceError("unknown_rule", f"规则 {rule_id} 修订 {revision} 不存在")
        if revision_state.status != RULE_DRAFT:
            raise ServiceError(
                "state_conflict", f"规则 {rule_id} 修订 {revision} 不在起草状态"
            )
        if actor == revision_state.drafted_by:
            raise ServiceError(
                "sod_violation", "审核人不能是同一修订的起草人"
            )
        moment = self._now(now)
        draft = revision_state.draft
        payload = {
            "revision": revision,
            "decision": decision,
            "actor": actor,
            "role": role,
            "audience": draft["audience"],
            "trigger_facts": draft["trigger_facts"],
        }
        if note is not None:
            payload["note"] = note
        self._append(
            self._event("RULE_REVIEWED", "audience_rule", rule_id, payload, moment, event_id)
        )
        return {
            "event_id": event_id,
            "rule_id": rule_id,
            "revision": revision,
            "decision": decision,
        }

    def sign_notice(
        self,
        event_id: str,
        notice_id: str,
        *,
        rule_id: str,
        rule_revision: int,
        valid_until: str,
        region_scope: list[str],
        recipients: list[str],
        actor: str,
        role: str,
        now: datetime | None = None,
    ) -> dict:
        """签发通知（发布人员，且不能是同一修订的起草人或审核人）。"""
        self._require_role(role, (ROLE_PUBLISHER,), "签发通知")
        self._require_actor(actor)
        replay = self._replay(event_id, "NOTICE_SIGNED")
        if replay:
            return replay
        if not _non_empty_str(notice_id):
            raise ServiceError("invalid_input", "通知标识必须是非空字符串")
        if notice_id in self.notices:
            raise ServiceError("state_conflict", f"通知 {notice_id} 已签发")
        rule = self.rules.get(rule_id)
        revision_state = rule.revisions.get(rule_revision) if rule else None
        if revision_state is None:
            raise ServiceError("unknown_rule", f"规则 {rule_id} 修订 {rule_revision} 不存在")
        if revision_state.status != RULE_REVIEWED:
            raise ServiceError(
                "state_conflict", f"规则 {rule_id} 修订 {rule_revision} 未通过审核"
            )
        if actor in (revision_state.drafted_by, revision_state.reviewed_by):
            raise ServiceError(
                "sod_violation", "签发人不能是同一修订的起草人或审核人"
            )
        draft = revision_state.draft
        source_key = (draft["source_id"], draft["source_version"])
        if source_key in self._corrected_sources:
            raise ServiceError(
                "source_corrected",
                f"来源 {source_key[0]} 的版本 {source_key[1]} 已被更正，不能签发",
            )
        moment = self._now(now)
        valid_at = _parse_instant(valid_until, "valid_until")
        if valid_at <= moment:
            raise ServiceError("invalid_input", "通知有效期必须晚于当前时间")
        if valid_at > _parse_instant(draft["valid_until"], "valid_until"):
            raise ServiceError(
                "invalid_input", "通知有效期不能超过规则建议有效期"
            )
        if (
            not isinstance(region_scope, list)
            or any(not _non_empty_str(item) for item in region_scope)
        ):
            raise ServiceError("invalid_input", "发布地区必须是非空字符串列表")
        rule_regions = list(draft["audience"].get("regions") or [])
        if rule_regions:
            if not region_scope:
                raise ServiceError(
                    "region_scope_required", "区域性规则签发时必须限定发布地区"
                )
            outside = sorted(set(region_scope) - set(rule_regions))
            if outside:
                raise ServiceError(
                    "region_scope_exceeds",
                    f"发布地区 {outside} 超出规则适用地区，不得扩大局部风险提醒范围",
                )
        if (
            not isinstance(recipients, list)
            or not recipients
            or any(not _non_empty_str(item) for item in recipients)
        ):
            raise ServiceError("invalid_input", "接收人必须是非空字符串列表且至少一人")
        snapshot = {
            "rule_id": rule_id,
            "rule_revision": rule_revision,
            "audience": draft["audience"],
            "trigger_facts": draft["trigger_facts"],
            "advice": draft["advice"],
            "stop_self_care": draft["stop_self_care"],
            "referral_level": draft["referral_level"],
            "source_id": draft["source_id"],
            "source_version": draft["source_version"],
            "region_scope": list(region_scope),
            "valid_until": valid_until,
        }
        self._append(
            self._event(
                "NOTICE_SIGNED",
                "notice_release",
                notice_id,
                {
                    "rule_id": rule_id,
                    "rule_revision": rule_revision,
                    "valid_until": valid_until,
                    "region_scope": list(region_scope),
                    "recipients": list(recipients),
                    "snapshot": snapshot,
                    "actor": actor,
                    "role": role,
                },
                moment,
                event_id,
            )
        )
        return {"event_id": event_id, "notice_id": notice_id, "status": NOTICE_SIGNED}

    def send_notice(
        self,
        event_id: str,
        notice_id: str,
        *,
        actor: str,
        role: str,
        now: datetime | None = None,
    ) -> dict:
        """发送已签发通知（发布人员），记录实际触达的接收人。"""
        self._require_role(role, (ROLE_PUBLISHER,), "发送通知")
        self._require_actor(actor)
        replay = self._replay(event_id, "NOTICE_SENT")
        if replay:
            return replay
        notice = self.notices.get(notice_id)
        if notice is None:
            raise ServiceError("unknown_notice", f"通知 {notice_id} 不存在")
        if notice.status != NOTICE_SIGNED:
            raise ServiceError(
                "state_conflict", f"通知 {notice_id} 当前状态不能发送"
            )
        moment = self._now(now)
        if _parse_instant(notice.valid_until, "valid_until") <= moment:
            raise ServiceError("notice_expired", f"通知 {notice_id} 已过有效期")
        self._append(
            self._event(
                "NOTICE_SENT",
                "notice_release",
                notice_id,
                {"reached": list(notice.recipients), "actor": actor, "role": role},
                moment,
                event_id,
            )
        )
        return {
            "event_id": event_id,
            "notice_id": notice_id,
            "status": NOTICE_SENT,
            "reached": list(notice.recipients),
        }

    # ------------------------------------------------------------------
    # 咨询登记与匹配
    # ------------------------------------------------------------------

    def evaluate(
        self, facts: Mapping[str, Any], now: datetime | None = None
    ) -> dict:
        """对事实集做干跑匹配，不留下记录；同一事实集返回稳定结果。"""
        self._validate_facts(facts)
        moment = self._now(now)
        return self._evaluate(facts, moment)

    def _evaluate(self, facts: Mapping[str, Any], moment: datetime) -> dict:
        active = []
        for notice_id, notice in self.notices.items():
            if notice.status not in (NOTICE_SIGNED, NOTICE_SENT):
                continue
            if notice.correction_open:
                continue
            if _parse_instant(notice.valid_until, "valid_until") <= moment:
                continue
            active.append((notice_id, notice.snapshot))
        return build_result(active, facts)

    def submit_consultation(
        self,
        event_id: str,
        facts: Mapping[str, Any],
        *,
        channel: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """登记咨询事件并匹配建议。

        相同事件标识重放只保留一次：事实一致时返回首次登记的结果；
        事实冲突时不覆盖、不择优，直接转人工处理。
        """
        self._validate_facts(facts)
        if not _non_empty_str(event_id):
            raise ServiceError("invalid_input", "事件标识必须是非空字符串")
        fingerprint = self._event_index.get(event_id)
        if fingerprint is not None and fingerprint[0] != "CONSULTATION_RECORDED":
            raise ServiceError(
                "event_id_conflict", f"事件标识 {event_id} 已用于 {fingerprint[0]}"
            )
        existing = self.consultations.get(event_id)
        if existing is not None:
            if existing.canon == _canonical(facts):
                return existing.result
            return self._escalate(event_id, existing, facts, self._now(now))
        moment = self._now(now)
        result = self._evaluate(facts, moment)
        result["event_id"] = event_id
        payload: dict[str, Any] = {"facts": dict(facts), "result": result}
        if channel is not None:
            payload["channel"] = channel
        self._append(
            self._event(
                "CONSULTATION_RECORDED",
                "consultation",
                event_id,
                payload,
                moment,
                event_id,
            )
        )
        return result

    def _escalate(
        self,
        consultation_id: str,
        existing: ConsultationState,
        incoming_facts: Mapping[str, Any],
        moment: datetime,
    ) -> dict:
        conflicting = _diff_keys(existing.facts, incoming_facts)
        reason = "同一咨询事件的关键信息与已登记内容冲突，已转人工处理"
        escalation_id = f"{consultation_id}#esc:{_hash12(incoming_facts)}"
        if escalation_id not in self._event_index:
            self._append(
                self._event(
                    "CONSULTATION_ESCALATED",
                    "consultation",
                    consultation_id,
                    {
                        "conflicting_keys": conflicting,
                        "reason": reason,
                        "incoming_facts": dict(incoming_facts),
                    },
                    moment,
                    escalation_id,
                )
            )
        return {
            "event_id": consultation_id,
            "status": "escalated",
            "advice": [],
            "overall_referral_level": None,
            "undetermined": [],
            "escalation": {"conflicting_keys": conflicting, "reason": reason},
            "disclaimer": DISCLAIMER,
        }

    def resolve_manual_review(
        self,
        event_id: str,
        consultation_id: str,
        *,
        resolution: str,
        actor: str,
        role: str,
        now: datetime | None = None,
    ) -> dict:
        """登记人工处理结论（临床人员），关闭咨询的升级状态。"""
        self._require_role(role, (ROLE_CLINICIAN,), "人工处理咨询")
        self._require_actor(actor)
        replay = self._replay(event_id, "CONSULTATION_RESOLVED")
        if replay:
            return replay
        consultation = self.consultations.get(consultation_id)
        if consultation is None:
            raise ServiceError("unknown_consultation", f"咨询 {consultation_id} 不存在")
        if consultation.status != CONSULTATION_ESCALATED:
            raise ServiceError(
                "state_conflict", f"咨询 {consultation_id} 不在待人工处理状态"
            )
        if not _non_empty_str(resolution):
            raise ServiceError("invalid_input", "人工处理结论必须是非空字符串")
        moment = self._now(now)
        self._append(
            self._event(
                "CONSULTATION_RESOLVED",
                "consultation",
                consultation_id,
                {"resolution": resolution, "actor": actor, "role": role},
                moment,
                event_id,
            )
        )
        return {"event_id": event_id, "consultation_id": consultation_id, "status": CONSULTATION_RESOLVED}

    # ------------------------------------------------------------------
    # 来源更正
    # ------------------------------------------------------------------

    def correct_source(
        self,
        event_id: str,
        source_id: str,
        *,
        source_version: int,
        reason: str,
        actor: str,
        role: str,
        now: datetime | None = None,
    ) -> dict:
        """更正来源版本（临床人员）。

        未发送的受影响通知立即暂停；已经触达的接收人生成明确的纠正记录。
        """
        self._require_role(role, (ROLE_CLINICIAN,), "更正来源")
        self._require_actor(actor)
        replay = self._replay(event_id, "SOURCE_CORRECTED")
        if replay:
            return replay
        source = self.sources.get(source_id)
        if source is None or source_version not in source.versions:
            raise ServiceError(
                "unknown_source", f"来源 {source_id} 的版本 {source_version} 未登记"
            )
        if not _non_empty_str(reason):
            raise ServiceError("invalid_input", "更正原因必须是非空字符串")
        moment = self._now(now)
        self._append(
            self._event(
                "SOURCE_CORRECTED",
                "guidance_source",
                source_id,
                {
                    "source_version": source_version,
                    "reason": reason,
                    "actor": actor,
                    "role": role,
                },
                moment,
                event_id,
            )
        )
        reconciled = self._reconcile_corrections(moment)
        return {
            "event_id": event_id,
            "source_id": source_id,
            "source_version": source_version,
            **reconciled,
        }

    def _reconcile_corrections(self, moment: datetime) -> dict:
        """按已登记的来源更正补齐暂停与纠正任务（幂等，可重入）。"""
        paused: list[str] = []
        opened: list[str] = []
        for notice_id in sorted(self.notices):
            notice = self.notices[notice_id]
            snapshot = notice.snapshot
            key = (snapshot["source_id"], snapshot["source_version"])
            correction = self._corrected_sources.get(key)
            if correction is None:
                continue
            if (
                notice.status == NOTICE_SIGNED
                and _parse_instant(notice.valid_until, "valid_until") > moment
            ):
                event_id = f"{notice_id}#paused"
                if self._append(
                    self._event(
                        "NOTICE_PAUSED",
                        "notice_release",
                        notice_id,
                        {
                            "reason": correction["reason"],
                            "source_id": key[0],
                            "source_version": key[1],
                        },
                        moment,
                        event_id,
                    )
                ):
                    paused.append(notice_id)
            if notice.reached and not notice.correction_open:
                for recipient in notice.reached:
                    task_id = f"corr:{notice_id}:{recipient}"
                    if self._append(
                        self._event(
                            "CORRECTION_OPENED",
                            "correction_task",
                            task_id,
                            {
                                "affected_release": notice_id,
                                "recipient": recipient,
                                "reason": correction["reason"],
                                "source_id": key[0],
                                "source_version": key[1],
                            },
                            moment,
                            task_id,
                        )
                    ):
                        opened.append(task_id)
        return {"paused_notices": paused, "correction_tasks": opened}

    def deliver_correction(
        self,
        event_id: str,
        task_id: str,
        *,
        actor: str,
        role: str,
        now: datetime | None = None,
    ) -> dict:
        """确认纠正记录已送达接收人（发布人员）。"""
        self._require_role(role, (ROLE_PUBLISHER,), "送达纠正记录")
        self._require_actor(actor)
        replay = self._replay(event_id, "CORRECTION_DELIVERED")
        if replay:
            return replay
        task = self.corrections.get(task_id)
        if task is None:
            raise ServiceError("unknown_correction", f"纠正任务 {task_id} 不存在")
        if task.status != TASK_PENDING:
            raise ServiceError("state_conflict", f"纠正任务 {task_id} 已送达")
        moment = self._now(now)
        self._append(
            self._event(
                "CORRECTION_DELIVERED",
                "correction_task",
                task_id,
                {"actor": actor, "role": role},
                moment,
                event_id,
            )
        )
        return {"event_id": event_id, "task_id": task_id, "status": TASK_DELIVERED}

    # ------------------------------------------------------------------
    # 过期维护与重启恢复
    # ------------------------------------------------------------------

    def run_maintenance(self, now: datetime | None = None) -> list[str]:
        """把已过有效期的规则修订与通知标记为过期，返回追加的事件标识。"""
        moment = self._now(now)
        appended: list[str] = []
        for notice_id in sorted(self.notices):
            notice = self.notices[notice_id]
            if notice.status not in (NOTICE_SIGNED, NOTICE_SENT):
                continue
            if _parse_instant(notice.valid_until, "valid_until") <= moment:
                event_id = f"{notice_id}#expired"
                if self._append(
                    self._event(
                        "NOTICE_EXPIRED",
                        "notice_release",
                        notice_id,
                        {"valid_until": notice.valid_until},
                        moment,
                        event_id,
                    )
                ):
                    appended.append(event_id)
        for rule_id in sorted(self.rules):
            for revision in sorted(self.rules[rule_id].revisions):
                state = self.rules[rule_id].revisions[revision]
                if state.status not in (RULE_DRAFT, RULE_REVIEWED):
                    continue
                if _parse_instant(state.draft["valid_until"], "valid_until") <= moment:
                    event_id = f"{rule_id}#r{revision}:expired"
                    if self._append(
                        self._event(
                            "RULE_EXPIRED",
                            "audience_rule",
                            rule_id,
                            {
                                "revision": revision,
                                "valid_until": state.draft["valid_until"],
                            },
                            moment,
                            event_id,
                        )
                    ):
                        appended.append(event_id)
        return appended

    def recover(self, now: datetime | None = None) -> dict:
        """启动恢复：补记过期、补齐更正动作，并报告待处理事项。"""
        moment = self._now(now)
        expired = self.run_maintenance(moment)
        reconciled = self._reconcile_corrections(moment)
        return {
            "expired": expired,
            **reconciled,
            "pending_corrections": self.pending_corrections(),
            "open_manual_reviews": self.open_manual_reviews(),
        }

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------

    def pending_corrections(self) -> list[dict]:
        """待送达的纠正记录，按任务标识稳定排序。"""
        return [
            {
                "task_id": task.task_id,
                "affected_release": task.opened["affected_release"],
                "recipient": task.opened["recipient"],
                "reason": task.opened["reason"],
                "source_id": task.opened["source_id"],
                "source_version": task.opened["source_version"],
                "opened_at": task.opened_at,
            }
            for task in sorted(self.corrections.values(), key=lambda item: item.task_id)
            if task.status == TASK_PENDING
        ]

    def open_manual_reviews(self) -> list[dict]:
        """待人工处理的咨询，按事件标识稳定排序。"""
        return [
            {
                "consultation_id": consultation.consultation_id,
                "facts": consultation.facts,
                "conflicting_keys": consultation.escalations[-1]["conflicting_keys"],
                "escalations": len(consultation.escalations),
            }
            for consultation in sorted(
                self.consultations.values(), key=lambda item: item.consultation_id
            )
            if consultation.status == CONSULTATION_ESCALATED
        ]

    def source_state(self, source_id: str) -> dict:
        source = self.sources.get(source_id)
        if source is None:
            raise ServiceError("unknown_source", f"来源 {source_id} 未登记")
        return {
            "source_id": source_id,
            "versions": sorted(source.versions),
            "corrections": [dict(item) for item in source.corrections],
        }

    def rule_state(self, rule_id: str) -> dict:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise ServiceError("unknown_rule", f"规则 {rule_id} 不存在")
        return {
            "rule_id": rule_id,
            "revisions": {
                revision: {
                    "status": state.status,
                    "drafted_by": state.drafted_by,
                    "reviewed_by": state.reviewed_by,
                    "decision": state.decision,
                }
                for revision, state in sorted(rule.revisions.items())
            },
        }

    def notice_state(self, notice_id: str) -> dict:
        notice = self.notices.get(notice_id)
        if notice is None:
            raise ServiceError("unknown_notice", f"通知 {notice_id} 不存在")
        return {
            "notice_id": notice_id,
            "status": notice.status,
            "valid_until": notice.valid_until,
            "recipients": list(notice.recipients),
            "reached": list(notice.reached),
            "correction_open": notice.correction_open,
            "snapshot": dict(notice.snapshot),
        }

    def consultation_state(self, consultation_id: str) -> dict:
        consultation = self.consultations.get(consultation_id)
        if consultation is None:
            raise ServiceError(
                "unknown_consultation", f"咨询 {consultation_id} 不存在"
            )
        return {
            "consultation_id": consultation_id,
            "status": consultation.status,
            "facts": consultation.facts,
            "result": consultation.result,
            "escalations": len(consultation.escalations),
            "resolution": consultation.resolution,
        }

    def correction_task(self, task_id: str) -> dict:
        task = self.corrections.get(task_id)
        if task is None:
            raise ServiceError("unknown_correction", f"纠正任务 {task_id} 不存在")
        return {
            "task_id": task.task_id,
            "status": task.status,
            "opened": dict(task.opened),
            "opened_at": task.opened_at,
            "delivered": task.delivered,
        }
