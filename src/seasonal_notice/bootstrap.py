"""从种子 JSON 引导一个完成全流程签发的服务（供演示与联调）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .model import Actor, GuidanceSource, RulePackage
from .service import RuleSigningService


def seed_service(
    service: RuleSigningService,
    seed_path: str | Path,
    editor: Actor,
    clinician: Actor,
    publisher: Actor,
) -> list[str]:
    """按种子数据登记来源并完成起草、审核、签发，返回发布号列表。"""
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    for raw in data["sources"]:
        service.register_source(editor, GuidanceSource.from_dict(raw))

    release_ids: list[str] = []
    for index, rule in enumerate(data["rules"], 1):
        package = RulePackage.from_dict(rule["package"])
        revision = service.draft_rule(editor, rule["rule_id"], rule["source_id"], package)
        service.review_rule(clinician, rule["rule_id"], revision, package)
        now = datetime.fromisoformat(service.current_time())
        valid_until = (now + timedelta(days=int(rule.get("valid_days", 30)))).isoformat()
        release_id = f"rel-{index:03d}-{rule['rule_id']}"
        service.sign_release(
            publisher,
            release_id,
            rule["rule_id"],
            valid_until,
            region_codes=rule.get("region_codes", []),
        )
        release_ids.append(release_id)
    return release_ids
