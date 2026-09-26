"""秋季健康提醒规则签发簿：契约校验 + 规则签发服务。

本包只做规则匹配与信息整理，不诊断疾病、不替代医嘱。
"""

from .contracts import ContractIssue, validate_event
from .matcher import evaluate
from .model import (
    Actor,
    Advice,
    AdviceKind,
    Audience,
    ConsultationFacts,
    GuidanceSource,
    MatchResult,
    ReferralLevel,
    Role,
    RulePackage,
    StopCondition,
    TriggerFacts,
)
from .service import RoleError, RuleSigningService, WorkflowError

__all__ = [
    "ContractIssue",
    "validate_event",
    "evaluate",
    "Actor",
    "Advice",
    "AdviceKind",
    "Audience",
    "ConsultationFacts",
    "GuidanceSource",
    "MatchResult",
    "ReferralLevel",
    "Role",
    "RoleError",
    "RulePackage",
    "RuleSigningService",
    "StopCondition",
    "TriggerFacts",
    "WorkflowError",
]
