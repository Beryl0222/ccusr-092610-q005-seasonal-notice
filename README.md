# 秋季健康提醒规则签发簿

国家卫健委秋季提醒覆盖关节不适用药边界、手足口病和诺如病毒防护、旅行后虫媒风险监测及体重管理训练安全，各类建议有不同人群和触发条件。

本仓库提供规则签发服务：登记建议来源、适用人群、触发事实、建议有效期、停止自我处理条件、转介级别和采用版本；内容编辑起草、临床人员审核、发布人员签发，任何单一角色不能完成全部环节。服务只做规则匹配与信息整理，不诊断疾病、不替代医嘱。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/seasonal_notice/contracts.py`：领域事件交换契约校验。
- `src/seasonal_notice/evaluation.py`：咨询事实与已签发规则的纯函数匹配。
- `src/seasonal_notice/storage.py`：JSONL 事件日志持久化。
- `src/seasonal_notice/service.py`：规则签发服务（流水线、咨询、更正、恢复）。
- `tests/`：契约测试与服务测试。
- `docs/domain.md`：领域对象、事件语义与服务行为约定。

## 服务用法

```python
from seasonal_notice import RuleIssuanceService

svc = RuleIssuanceService("var/notice-journal")  # 事件日志目录，重启后自动恢复

# 登记建议来源（内容编辑）
svc.register_source("e1", "src-nhc", title="国家卫健委秋季健康提醒",
                    issuer="国家卫健委", version=1, actor="editor-li", role="editor")

# 起草 → 审核 → 签发（三个角色分离）
svc.draft_rule("e2", "rule-diarrhea",
               audience={"age_max": 12, "tags": ["儿童"]},
               trigger_facts={"symptoms": {"includes": "腹泻"}},
               advice="儿童腹泻注意补液与手卫生",
               stop_self_care=["出现血便或持续高热立即就医"],
               referral_level="community_clinic",
               source_id="src-nhc", source_version=1,
               valid_until="2026-12-31T23:59:59+08:00",
               actor="editor-li", role="editor")
svc.review_rule("e3", "rule-diarrhea", revision=1, decision="approved",
                actor="doc-wang", role="clinician")
svc.sign_notice("e4", "n-001", rule_id="rule-diarrhea", rule_revision=1,
                valid_until="2026-12-01T00:00:00+08:00", region_scope=[],
                recipients=["hotline-staff"], actor="pub-zhao", role="publisher")

# 咨询匹配：同一事实集返回稳定的建议与未确定项
result = svc.submit_consultation("call-001",
                                 {"age_years": 4, "population_tags": ["儿童"],
                                  "symptoms": ["腹泻"]})

# 来源更正：暂停未发送通知，为已触达人生成纠正记录
svc.correct_source("e5", "src-nhc", source_version=1, reason="剂量表述更正",
                   actor="doc-wang", role="clinician")
svc.pending_corrections()   # 待送达的纠正记录，重启后仍在
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m seasonal_notice.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
