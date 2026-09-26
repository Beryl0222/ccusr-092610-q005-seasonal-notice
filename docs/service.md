# 规则签发服务说明

本服务在 `contracts/domain.schema.json` 的事件信封之上实现业务状态推进。
**定位边界：系统只做规则匹配与信息整理，不诊断疾病、不替代医嘱。**
每条匹配结果都附带该声明。

## 角色与三权分立

| 角色 | 标识 | 可执行环节 |
| --- | --- | --- |
| 内容编辑 | `editor` | 登记建议来源、起草规则/修订稿 |
| 临床人员 | `clinician` | 审核规则、发起来源更正 |
| 发布人员 | `publisher` | 签发提醒（确定发布范围与有效期） |

约束：

1. 起草 → 审核 → 签发缺一不可，未审核的版本不能签发；
2. 三个环节必须由**三个不同的自然人**完成（按 `user_id` 判定，换角色无效）；
3. 上一版本未完成审核，不能起草新修订；
4. 任何单一角色调用越权操作都会得到 `RoleError`，不产生事件。

## 登记的规则要素

`RulePackage` 完整登记需求中的七类信息：

- **建议来源** `GuidanceSource`：发布机构、标题、链接；更正来源用 `supersedes` 指向前来源。
- **适用人群** `Audience`：月龄上下限、包含/排除标签、地区码集合。
- **触发事实** `TriggerFacts`：症状（任一命中）、风险地区旅居史（任一命中）、
  暴露标签与在用药物（全部满足）。
- **建议有效期**：签发时给定 `valid_until`，过期后不参与匹配、不允许发送。
- **停止自我处理条件** `StopCondition`：红旗症状 + 转介级别（urgent/emergency）。
- **转介级别** `ReferralLevel`：每条建议与每个红旗都带级别，结果就高不就低。
- **采用版本**：发布固化 `rule_id` + `revision` + 来源 + 规则快照。

## 匹配语义（`matcher.evaluate`）

判定一律三态，**绝不选择更乐观的结论**：

- `applicable`：条件确认满足 → 给出建议；
- `rejected`：条件确认不满足（年龄超出、明确无旅居史、地区无关）→ 规则静默；
- `indeterminate`：关键信息问不到（字段名列入 `unknown_fields`，或年龄/地区缺失）
  → 不出建议，在 `undetermined` 中列出需要补问的内容。

其他规则：

- 红旗只要求受众与地区确认适用，**不要求触发**，急症信号不会被触发条件挡住；
- 用药警示要求药物确实出现且人群标签命中；`severity="avoid"` 进入升级理由；
- 同一药物同时存在"可自行使用（medications_ok）"与"本人群应避免（avoid）"时，
  置 `needs_human=true` 转人工；
- 调用方显式上报的关键事实冲突通过 `conflicting_facts` 传入，同样转人工；
- **地区边界**：局部（含虫媒）风险只对当地居民（`region_code` 命中）或
  确有旅居史（`travel_history` 命中）的人成立；按地区发布时
  （`Release.region_codes`）再叠加发布范围门控，不向无关人群外溢；
- 输出按 `release_id`、症状码等稳定排序；同一事实集 + 同一发布集 →
  JSON 完全一致。

## 修订不变性

签发时把规则内容深拷贝进 `NOTICE_SIGNED.package_snapshot`。之后编辑修订、
临床审核、重新签发，都只产生新版本与新发布；旧发布（含已触达消息）的
依据快照永不改变。旧发布在匹配中被同规则的新发布取代，但事件与快照
永久留档可查。

## 来源更正流程（`correct_source`）

仅临床人员可发起，必须说明原因。对引用旧来源的每个发布：

1. **未发送消息**：发件箱记录置 `paused`，永不产生 `NOTICE_SENT`；
   重启后仍然暂停，不能发送；
2. **已触达收件人**：逐人生成 `CORRECTION_OPENED` 纠正任务，
   `correction_record()` 给出面向收件人的明确纠正文案，包含被纠正的
   旧发布号、旧来源、旧建议快照、新来源与原因；
3. 纠正通知送达状态持久化在工作日志中，`pending_corrections()` 在
   重启后继续返回未送达项，`deliver_correction()` 幂等送达；
4. 被更正的发布在新依据重新签发前不再参与新咨询匹配（保守暂停，而非
   继续沿用旧结论）。

## 幂等与持久化

- 领域事件写入 JSONL 前先过契约校验，按 `event_id` 全局去重；
  校验失败或重复均不写入。版本号按聚合从 1 递增。
- 相同 `consultation_id` 的咨询只处理一次：首诊结果随事实持久化，
  重放（即便带来不同事实）返回首诊结果。
- 发件箱消息按 `发布号|收件人` 幂等；`NOTICE_SENT` 按
  `(发布号, 收件人)` 去重，重复发送返回 `False`。
- 所有状态由事件日志 + 工作状态日志重放得到；重启后过期规则继续被排除，
  待送达纠正继续可处理。

## 文件

- `model.py`：领域对象与稳定 JSON 序列化。
- `store.py`：事件存储（契约校验/幂等/重放）与追加式工作状态日志。
- `matcher.py`：纯函数确定性匹配。
- `service.py`：签发工作流、发件箱、更正、咨询入口。
- `bootstrap.py`：从 `data/seed_rules.json` 引导完成全流程签发。
- `examples/demo.py`：节前三类咨询与治理场景的端到端演示。

## 使用要点

```python
svc = RuleSigningService("events.jsonl", "work.jsonl")
rev = svc.draft_rule(editor, "rule-x", "src-1", package)
svc.review_rule(clinician, "rule-x", rev, package)
svc.sign_release(publisher, "rel-1", "rule-x", "2026-10-26T09:00:00+08:00",
                 region_codes=["CN-44"])
result = svc.handle_consultation(facts)  # MatchResult，.to_dict() 即稳定 JSON
```
