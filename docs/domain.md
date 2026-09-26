# 领域约定

国家卫健委秋季提醒覆盖关节不适用药边界、手足口病和诺如病毒防护、旅行后虫媒风险监测及体重管理训练安全，各类建议有不同人群和触发条件。

聚合对象包括 `guidance_source`、`audience_rule`、`notice_release`、`correction_task`、`consultation`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

**服务边界**：本服务只做规则匹配与信息整理，不诊断疾病、不替代医嘱；每条评估结果都携带该声明。

## 事件载荷

- `RULE_DRAFTED`：`revision`, `audience`, `trigger_facts`, `advice`, `stop_self_care`, `referral_level`, `source_id`, `source_version`, `valid_until`。
- `RULE_REVIEWED`：`audience`, `trigger_facts`，另含 `revision`, `decision`。
- `NOTICE_SIGNED`：`rule_revision`, `valid_until`，另含 `region_scope`, `recipients`, `snapshot`。
- `CORRECTION_OPENED`：`affected_release`, `reason`，另含 `recipient`。
- `SOURCE_CORRECTED`：`source_version`, `reason`。
- `NOTICE_PAUSED`：`reason`。
- `CONSULTATION_RECORDED`：`facts`, `result`。
- `CONSULTATION_ESCALATED`：`conflicting_keys`, `reason`。
- `CONSULTATION_RESOLVED`：`resolution`。

## 规则流水线与职责分离

规则要素在起草时登记：建议来源与采用版本、适用人群、触发事实、建议内容、建议有效期、停止自我处理条件、转介级别（`self_care` / `community_clinic` / `prompt_care` / `emergency`，按严重程度递增）。

1. 内容编辑（`editor`）起草规则修订（`RULE_DRAFTED`）。
2. 临床人员（`clinician`）审核（`RULE_REVIEWED`），不能审核自己起草的修订。
3. 发布人员（`publisher`）签发通知（`NOTICE_SIGNED`），不能是同一修订的起草人或审核人；发送时记录实际触达的接收人（`NOTICE_SENT`）。

任何单一角色无法完成全部环节；同一操作人也不能跨角色完成同一修订的多个环节。规则修订只产生新修订号，已签发通知的快照与依据保持不变。

## 咨询匹配

- 相同事件标识的咨询重放只保留一次：事实一致时返回首次登记的结果；关键信息冲突时不覆盖、不择优，登记 `CONSULTATION_ESCALATED` 转人工处理。
- 多条规则同时命中时，整体转介级别取最严重的一条，不选择更乐观的结论。
- 事实缺失或无法安全比较时，对应规则进入未确定项并列出缺失事实键，由值班人员补充询问。
- 按地区发布的规则只命中该地区人群；签发时发布地区不得超出规则适用地区，避免将局部虫媒风险扩展到无关人群。旅居史类规则按 `recent_travel` 等事实触发，与居住地区无关。
- 同一事实集的评估结果稳定：建议按转介级别与通知标识排序，未确定项按通知标识排序，与规则创建顺序无关。

## 来源更正

`SOURCE_CORRECTED` 登记后自动对账：未发送的受影响通知暂停（`NOTICE_PAUSED`），已经触达的接收人生成明确的纠正记录（`CORRECTION_OPENED`，送达后 `CORRECTION_DELIVERED`）。被更正的来源版本不能再起草新修订或签发新通知；登记更正后的新版本来源即可恢复流水线。

## 持久化与重启恢复

所有状态变化以契约事件追加到 JSONL 事件日志，重启后重放恢复。服务启动时执行 `recover()`：补记已过期的规则与通知（`RULE_EXPIRED` / `NOTICE_EXPIRED`）、补齐更正对账，并报告待送达的纠正记录与待人工处理的咨询，保证过期规则和待纠正通知在重启后继续处理。
