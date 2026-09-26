"""规则签发服务的领域模型。

契约层（contracts.py / domain.schema.json）只规定事件信封；这里定义
服务内部交换的业务对象。所有对象均可稳定序列化为普通 JSON 值，
``from_dict`` 容忍缺失的可选字段，默认值固定，保证同一输入得到同一结构。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping


class Role(str, Enum):
    """签发流程中的三种角色，任一角色都不能独立完成全流程。"""

    EDITOR = "editor"          # 内容编辑：起草
    CLINICIAN = "clinician"    # 临床人员：审核
    PUBLISHER = "publisher"    # 发布人员：签发


class AdviceKind(str, Enum):
    """建议的性质：系统只整理信息，不诊断疾病。"""

    ROUTINE = "routine"            # 日常防护
    SEEK_CARE = "seek_care"        # 尽快就医
    MEDICATION_CAUTION = "medication_caution"  # 用药风险提示
    HUMAN_REVIEW = "human_review"  # 转人工（不是医学结论）


class ReferralLevel(str, Enum):
    """转介级别。"""

    NONE = "none"          # 无需转介
    URGENT = "urgent"      # 尽快线下就医
    EMERGENCY = "emergency"  # 立即急诊 / 拨打急救电话


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: Role
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "role": self.role.value, "name": self.name}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Actor:
        return cls(
            user_id=data["user_id"],
            role=Role(data["role"]),
            name=data.get("name", ""),
        )


@dataclass(frozen=True)
class Audience:
    """适用人群。None 的边界表示不限制；标签为包含/排除两个集合。"""

    min_age_months: int | None = None
    max_age_months: int | None = None
    include_tags: frozenset[str] = frozenset()
    exclude_tags: frozenset[str] = frozenset()
    region_codes: frozenset[str] = frozenset()  # 空表示不按地区限定

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_age_months": self.min_age_months,
            "max_age_months": self.max_age_months,
            "include_tags": sorted(self.include_tags),
            "exclude_tags": sorted(self.exclude_tags),
            "region_codes": sorted(self.region_codes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Audience:
        data = data or {}
        return cls(
            min_age_months=data.get("min_age_months"),
            max_age_months=data.get("max_age_months"),
            include_tags=frozenset(data.get("include_tags", [])),
            exclude_tags=frozenset(data.get("exclude_tags", [])),
            region_codes=frozenset(data.get("region_codes", [])),
        )


@dataclass(frozen=True)
class TriggerFacts:
    """触发事实。任一症状命中、且全部旅居/暴露条件都满足时触发。"""

    symptoms: frozenset[str] = frozenset()
    any_travel_regions: frozenset[str] = frozenset()  # 命中其一即可
    exposure_tags: frozenset[str] = frozenset()       # 必须全部具备
    medications: frozenset[str] = frozenset()         # 正在使用的药物（命中其一）

    def to_dict(self) -> dict[str, Any]:
        return {
            "symptoms": sorted(self.symptoms),
            "any_travel_regions": sorted(self.any_travel_regions),
            "exposure_tags": sorted(self.exposure_tags),
            "medications": sorted(self.medications),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> TriggerFacts:
        data = data or {}
        return cls(
            symptoms=frozenset(data.get("symptoms", [])),
            any_travel_regions=frozenset(data.get("any_travel_regions", [])),
            exposure_tags=frozenset(data.get("exposure_tags", [])),
            medications=frozenset(data.get("medications", [])),
        )


@dataclass(frozen=True)
class MedicationCaution:
    """药物风险：某药物在某人群/条件下的边界提醒。

    severity="caution" 为提示风险，"avoid" 为该人群应避免使用。
    """

    medication: str
    message: str
    avoid_when_tags: frozenset[str] = frozenset()
    severity: str = "caution"

    def to_dict(self) -> dict[str, Any]:
        return {
            "medication": self.medication,
            "message": self.message,
            "avoid_when_tags": sorted(self.avoid_when_tags),
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MedicationCaution:
        return cls(
            medication=data["medication"],
            message=data["message"],
            avoid_when_tags=frozenset(data.get("avoid_when_tags", [])),
            severity=data.get("severity", "caution"),
        )


@dataclass(frozen=True)
class Advice:
    kind: AdviceKind
    text: str
    referral: ReferralLevel = ReferralLevel.NONE
    medications_ok: frozenset[str] = frozenset()  # 明确可自行使用的药物

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "referral": self.referral.value,
            "medications_ok": sorted(self.medications_ok),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Advice:
        return cls(
            kind=AdviceKind(data["kind"]),
            text=data["text"],
            referral=ReferralLevel(data.get("referral", ReferralLevel.NONE.value)),
            medications_ok=frozenset(data.get("medications_ok", [])),
        )


@dataclass(frozen=True)
class StopCondition:
    """停止自我处理条件：命中某症状即按指定级别转介。"""

    symptom: str
    referral: ReferralLevel
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"symptom": self.symptom, "referral": self.referral.value, "message": self.message}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StopCondition:
        return cls(
            symptom=data["symptom"],
            referral=ReferralLevel(data["referral"]),
            message=data["message"],
        )


@dataclass(frozen=True)
class RulePackage:
    """一条规则的完整业务内容（与来源、审核、版本等流程信息分离）。"""

    title: str
    audience: Audience
    trigger: TriggerFacts
    advice: tuple[Advice, ...]
    stop_conditions: tuple[StopCondition, ...] = ()  # 出现即停止自我处理的红旗
    medication_cautions: tuple[MedicationCaution, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "audience": self.audience.to_dict(),
            "trigger": self.trigger.to_dict(),
            "advice": [a.to_dict() for a in self.advice],
            "stop_conditions": [s.to_dict() for s in self.stop_conditions],
            "medication_cautions": [c.to_dict() for c in self.medication_cautions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RulePackage:
        return cls(
            title=data["title"],
            audience=Audience.from_dict(data.get("audience")),
            trigger=TriggerFacts.from_dict(data.get("trigger") or data.get("trigger_facts")),
            advice=tuple(Advice.from_dict(a) for a in data.get("advice", [])),
            stop_conditions=tuple(
                StopCondition.from_dict(s) for s in data.get("stop_conditions", [])
            ),
            medication_cautions=tuple(
                MedicationCaution.from_dict(c) for c in data.get("medication_cautions", [])
            ),
        )


@dataclass(frozen=True)
class GuidanceSource:
    source_id: str
    title: str
    publisher: str
    source_url: str = ""
    supersedes: str | None = None  # 更正时指向被替代的旧来源

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "publisher": self.publisher,
            "source_url": self.source_url,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GuidanceSource:
        return cls(
            source_id=data["source_id"],
            title=data["title"],
            publisher=data["publisher"],
            source_url=data.get("source_url", ""),
            supersedes=data.get("supersedes"),
        )


@dataclass(frozen=True)
class Release:
    """一次签发：规则内容在签发时固化，之后的修订不改变本发布。"""

    release_id: str
    rule_id: str
    revision: int
    source_id: str
    package_snapshot: RulePackage
    signed_at: str
    signer: Actor
    valid_until: str
    region_codes: frozenset[str] = frozenset()  # 发布范围；空=全域
    status: str = "active"  # active | paused | corrected | expired

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "rule_id": self.rule_id,
            "revision": self.revision,
            "source_id": self.source_id,
            "package_snapshot": self.package_snapshot.to_dict(),
            "signed_at": self.signed_at,
            "signer": self.signer.to_dict(),
            "valid_until": self.valid_until,
            "region_codes": sorted(self.region_codes),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Release:
        return cls(
            release_id=data["release_id"],
            rule_id=data["rule_id"],
            revision=data["revision"],
            source_id=data["source_id"],
            package_snapshot=RulePackage.from_dict(data["package_snapshot"]),
            signed_at=data["signed_at"],
            signer=Actor.from_dict(data["signer"]),
            valid_until=data["valid_until"],
            region_codes=frozenset(data.get("region_codes", [])),
            status=data.get("status", "active"),
        )

    def evolved(self, **changes: Any) -> Release:
        return replace(self, **changes)


@dataclass(frozen=True)
class ConsultationFacts:
    """一次热线咨询的事实集。字段缺失与字段冲突是两回事，由匹配器区分。

    空集合表示确认没有该项；把字段名列入 ``unknown_fields``
    （如 ``travel_history``）表示问不到，规则无法判定时进入未确定项。
    """

    consultation_id: str
    symptoms: frozenset[str] = frozenset()
    age_months: int | None = None
    tags: frozenset[str] = frozenset()
    region_code: str | None = None
    travel_history: frozenset[str] = frozenset()  # 近期待过的地区码
    exposure_tags: frozenset[str] = frozenset()
    medications: frozenset[str] = frozenset()
    unknown_fields: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "consultation_id": self.consultation_id,
            "symptoms": sorted(self.symptoms),
            "age_months": self.age_months,
            "tags": sorted(self.tags),
            "region_code": self.region_code,
            "travel_history": sorted(self.travel_history),
            "exposure_tags": sorted(self.exposure_tags),
            "medications": sorted(self.medications),
            "unknown_fields": sorted(self.unknown_fields),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConsultationFacts:
        return cls(
            consultation_id=data["consultation_id"],
            symptoms=frozenset(data.get("symptoms", [])),
            age_months=data.get("age_months"),
            tags=frozenset(data.get("tags", [])),
            region_code=data.get("region_code"),
            travel_history=frozenset(data.get("travel_history", [])),
            exposure_tags=frozenset(data.get("exposure_tags", [])),
            medications=frozenset(data.get("medications", [])),
            unknown_fields=frozenset(data.get("unknown_fields", [])),
        )


@dataclass(frozen=True)
class MatchedAdvice:
    release_id: str
    rule_id: str
    revision: int
    source_id: str
    advice: tuple[Advice, ...]
    medication_cautions: tuple[MedicationCaution, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "rule_id": self.rule_id,
            "revision": self.revision,
            "source_id": self.source_id,
            "advice": [a.to_dict() for a in self.advice],
            "medication_cautions": [c.to_dict() for c in self.medication_cautions],
        }


@dataclass(frozen=True)
class RedFlag:
    symptom: str
    referral: ReferralLevel
    message: str
    release_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symptom": self.symptom,
            "referral": self.referral.value,
            "message": self.message,
            "release_id": self.release_id,
        }


@dataclass(frozen=True)
class MatchResult:
    """对同一事实集永远稳定：建议、红旗、用药警示、未确定项、转介级别。

    ``needs_human`` 为真表示事实或建议间存在冲突，系统不挑选更乐观的结论，
    只整理信息并转人工；此时仍返回已确定的内容供值班人员参考。
    """

    consultation_id: str
    matched: tuple[MatchedAdvice, ...]
    red_flags: tuple[RedFlag, ...]
    undetermined: tuple[str, ...]
    escalation_reasons: tuple[str, ...]
    conflict_reasons: tuple[str, ...]
    referral: ReferralLevel
    needs_human: bool
    evaluated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "consultation_id": self.consultation_id,
            "matched": [m.to_dict() for m in self.matched],
            "red_flags": [f.to_dict() for f in self.red_flags],
            "undetermined": list(self.undetermined),
            "escalation_reasons": list(self.escalation_reasons),
            "conflict_reasons": list(self.conflict_reasons),
            "referral": self.referral.value,
            "needs_human": self.needs_human,
            "evaluated_at": self.evaluated_at,
            "disclaimer": "本结果仅做规则匹配与信息整理，不构成诊断，不能替代医嘱。",
        }
