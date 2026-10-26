# 09 验收

> "部署完成"不是文件复制完，而是自检全绿 + 离线评测达标 + 宿主完成三处接入。

## 1. 验收门

| 门 | 通过标准 | 命令 |
| --- | --- | --- |
| 建库与可写性 | `health.ok=true`，表齐全，写探针成功 | `python memory/run.py health --json` |
| 行为自检 | 14/14 通过 | `python memory/run.py selfcheck --json` |
| 离线检索 | Top3 命中率 ≥0.60 | `python memory/run.py eval --json` |
| 宿主接入 | 三处 hook + 工具分发已接 | 见 `10-integration.md` |
| 人工抽检（可选） | 20 轮注入相关性 ≥70% | 按第 4 节 |

任何一门不过，不要把系统接进对话。

## 2. selfcheck 覆盖的 14 项

| # | 检查 | 证明什么 |
| --- | --- | --- |
| 1 | health | schema/表/FTS/可写性 |
| 2 | create_read | 写入、读取、evidence 校验、标签落库 |
| 3 | evidence_gate | evidence 不是原话子串时拒绝 |
| 4 | duplicate_gate | 同 folder 同类型重复被拒，返回 existing_id |
| 5 | merge_path | 新颖度低时走合并而不是新建（测试用放宽阈值） |
| 6 | sensitive_gate | 密码类信息被拦 |
| 7 | tag_gate | 三元标签缺组被拒 |
| 8 | scope_isolation | 跨 folder 检索不到、读不到 |
| 9 | prefetch | 命中注入、剂量 ≤3、摘要 ≤160 字、120s 去重 |
| 10 | reminder_memo | 标题块带"不是全部"；便签 ONCE 消费 |
| 11 | extraction_approval | 抽取→候选→审批→入库全链路；坏 evidence 被丢 |
| 12 | time_parse | 相对时间可解析；无法解析留 0 |
| 13 | lifecycle | dry-run → 归档 → 硬删三级行为；pin 免疫 |
| 14 | eval_top3 | 离线评测 ≥0.60 |

selfcheck 在临时目录里跑，**不会写部署库**。`eval` 同样在临时库跑，seed 不会污染生产数据。

## 3. 本次实测记录（dogfood，2026-10-27）

部署命令：

```bash
python scripts/deploy.py --target <temp> --profile offline
```

结果：

```
初始化：OK；自检：OK
14/14 通过，耗时约 0.15 s（本机多次实测 137–149 ms）
离线评测 Top3 = 0.95（19/20）
FTS 分词器：trigram
schema 版本：1
```

评测集：20 条种子记忆 + 20 条中文查询，覆盖饮食、过敏、作息、称呼、宠物、出差、约定、工作时段等。
本次实测的唯一未命中：`送什么味道的礼物` → 期望"薄荷"，返回了三条弱相关记忆。

这不是 bug，而是离线 hashing 嵌入的边界：它只懂词面相似度，不懂"礼物 → 香味"的语义跳跃。
生产环境切语义嵌入（bge 一类）后，用同一份评测集重跑，通常会补上这一类。
这是一次固定条件（20 条查询、offline hashing、Top3）下的记录，不是验收规定；换数据、模型或参数后，未命中数量会变。

另外用本地假 OpenAI 兼容服务（16 维嵌入 + `/chat/completions`）验证过 HTTP 协议路径（生产模式）：
`create → search → extract → candidates approve → health` 全部通过，`embedding_model` 与 `stale_embeddings` 正确。
这不代表真实模型的抽取质量，只证明协议与链路可用。

## 4. 人工抽检（可选但建议）

自动评测只能证明"已知查询能命中"，证明不了"注入是否自然"。

1. 抽 20 轮真实对话；
2. 记录每轮 `prefetch` 的 `items` 与 `status`；
3. 人工判断注入的 ≤3 条里，有几条与当前对话相关；
4. 相关比例 ≥70% 视为通过；低于 70% 先降 `min_score_*` 或补标签，不要直接上线。

## 5. 切生产模型后的复验

把 `embedding.provider` / `llm.provider` 改为 `http` 后：

1. `health` 确认可写；
2. 如果库里已有数据：`python memory/run.py reindex --json`，确认 `stale_embeddings=0`；
3. `selfcheck` 仍 14/14（selfcheck 内部用 hashing，不依赖外部服务）；
4. `eval` 用**生产嵌入**跑一遍（`evaluate_in_temp` 会沿用当前配置），Top3 ≥0.60；
5. 用一条真实对话手工跑 `prefetch`，确认阈值合适；
6. 再开自动预取。

阈值与模型绑定：换模型不换阈值，是上线后"她突然想不起事"的最常见原因。

## 6. 没有覆盖的东西

诚实列出，避免把自检当万能：

- **不验证你的 LLM 抽取质量**：selfcheck 用 StubLLM，只验证契约与闸门。
- **不验证宿主注入顺序**：只验证 `prefetch` 返回的文本，不验证你把它拼在哪。
- **不验证多进程并发**：SQLite 开了 WAL 与 5s busy timeout，但高并发写入需要你自己压测。
- **不验证隐私合规**：敏感词是兜底，不是合规方案。
- **不验证"像不像人"**：那是人格状态机的事，不是记忆系统的事。

## 7. 上线前检查清单

- [ ] 五问已回答并写进 `DEPLOY-REPORT.md`
- [ ] `selfcheck` 14/14
- [ ] `eval` Top3 ≥0.60（生产嵌入下重跑过）
- [ ] 宿主三处 hook 已接（prefetch / reminder / extract）
- [ ] 工具 schema 已注册，`dispatch` 可调用
- [ ] 群聊与私聊使用不同 `folder_id`
- [ ] 敏感词规则覆盖业务实际风险
- [ ] 关键事实（生日、过敏、边界）已 `pin`
- [ ] 生命周期先 dry-run，报告已人工看过
- [ ] 备份策略覆盖 `data/memory.db` 与 WAL
- [ ] `uninstall` 路径已确认（默认保数据）
