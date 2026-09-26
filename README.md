# 秋季健康提醒规则签发簿

国家卫健委秋季提醒覆盖关节不适用药边界、手足口病和诺如病毒防护、旅行后虫媒风险监测及体重管理训练安全，各类建议有不同人群和触发条件。县级健康热线在节前同时收到旅行后发热、儿童腹泻和训练营学员头晕等咨询时，本仓库提供**规则签发服务**：登记建议来源与适用条件，经三权分立流程签发，并对咨询事实集返回稳定的就医分级建议。

> 边界：系统只做规则匹配与信息整理，**不诊断疾病、不替代医嘱**；每条结果均附带该声明。

## 两层结构

- **契约层**：`contracts/domain.schema.json` + `contracts.py` 定义可稳定交换的领域事件信封（`SOURCE_REGISTERED`、`RULE_REVIEWED`、`NOTICE_SIGNED`、`NOTICE_SENT`、`CORRECTION_OPENED`），只做基础校验。
- **服务层**：在事件之上实现业务状态（见 [`docs/service.md`](docs/service.md)）。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `data/seed_rules.json`：四类秋季提醒的种子规则（虫媒发热、儿童腹泻、训练营头晕、关节用药）。
- `src/seasonal_notice/`：契约校验、领域模型、事件存储、匹配器、签发服务与引导器。
- `tests/`：契约、匹配器、签发工作流测试（共 44 个）。
- `examples/demo.py`：节前热线端到端演示。
- `docs/domain.md` / `docs/service.md`：领域事件语义与服务说明。

## 核心保证

1. **三权分立**：内容编辑起草、临床人员审核、发布人员签发，须三个不同的人；任何单一角色不能完成全流程。
2. **就医分级**：红旗停止自我处理条件按 urgent/emergency 转介，转介级别就高不就低；其余为日常防护建议。
3. **修订不变性**：签发即固化规则快照，之后的修订不改变已发提醒的依据。
4. **来源更正**：暂停全部未发送消息；为已触达者逐人生成明确纠正记录，纠正通知持久化、重启后继续送达。
5. **冲突不乐观**：关键事实或用药指令冲突时转人工（`needs_human`），不挑选更乐观的结论；问不到的关键信息进入未确定项，不出建议。
6. **地区不外溢**：局部虫媒风险只对当地居民与确有旅居史者成立，按地区发布再叠加范围门控。
7. **幂等与稳定**：相同咨询事件重放只保留一次；同一事实集返回结构与顺序完全一致的建议与未确定项；过期规则与待纠正通知在重启后继续正确处理。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests examples
```

## 端到端演示

```bash
PYTHONPATH=src python3 examples/demo.py
```

## 样例校验

```bash
PYTHONPATH=src python3 -m seasonal_notice.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
