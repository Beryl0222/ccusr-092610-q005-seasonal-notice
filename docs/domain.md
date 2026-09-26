# 领域约定

国家卫健委秋季提醒覆盖关节不适用药边界、手足口病和诺如病毒防护、旅行后虫媒风险监测及体重管理训练安全，各类建议有不同人群和触发条件。

聚合对象包括`guidance_source`、`audience_rule`、`notice_release`、`correction_task`。事件类型包括`SOURCE_REGISTERED`、`RULE_REVIEWED`、`NOTICE_SIGNED`、`NOTICE_SENT`、`CORRECTION_OPENED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `RULE_REVIEWED`：载荷还需包含 `audience`, `trigger_facts`。
- `NOTICE_SIGNED`：载荷还需包含 `rule_revision`, `valid_until`。
- `CORRECTION_OPENED`：载荷还需包含 `affected_release`, `reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。上层服务（三权分立签发、修订不变性、来源更正、确定性匹配、重启重放）见 `service.md`。
