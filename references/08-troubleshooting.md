# 08 故障排查

> 先定位是哪一段出了问题：写入（库里没有）、检索（库里有但没想起来）、注入（想起来了但没用对）。

## 1. 症状 → 根因 → 处置

| 症状 | 可能根因 | 先查什么 | 处置 |
| --- | --- | --- | --- |
| 该记得的没记住 | 写入门槛过高 / 抽取失败静默 / 只抽了特定类型 | `run.py stats`、`traces/*.jsonl` 里的 `extract:llm_error`、`candidates list` | 降 `min_user_chars`；补类型；关键事实手工 `create` 并 `pin` |
| 记住了但从不提起 | 预取阈值过高 / query 太窄 / 嵌入服务降级 | `traces` 的 `auto_recall` 记录（`candidates`、`threshold`） | 降阈值；开多话题 query；查 `read:embedding_error` |
| 答非所问、记串了 | 去重不足 / 合并过度 / 标签缺失 | `run.py read <id>` 看正文与 tags；`stats.duplicate_pairs` | 提高去重阈值；补三元标签；必要时拆开被合并的条目 |
| 重复记忆一堆 | 去重失效 / 抽取器重复触发 | `stats.duplicate_pairs`；`processed_messages` 是否有 message_id | 修 evidence 校验；确认宿主传了稳定 message_id |
| 重要旧事消失 | 生命周期误删 | `reports/lifecycle-*.json` 的 `would_delete` / `deleted` | 立即关 `allow_hard_delete`；改用归档；给关键条目加 `pin` |
| 角色开始编造 | 库里有错记 / 没有"不确定"指令 | 抽查 `evidence` 与 `source_text` | 加 evidence 闸门；确认命令手册在 system prompt 里 |
| 检索变慢 | 库膨胀 / 嵌入服务慢 | `stats.records.total`；trace 的 `embedding_error` 与耗时 | 开归档；缩小 `top_k`；换本地嵌入 |
| 群聊串私聊 | 检索未按通道隔离 | 群聊调用是否传了私聊 `folder_id`；`prefetch.allow_group` | 群/私各用 folder；关群聊预取；手册禁止复述私聊 |
| 对话变念档案 | 剂量过大 | 注入条数、摘要长度占 prompt 比例 | 减到 3 条 × 160 字；只注入摘要 |
| 静默降级无人知 | 异常被吞 | `traces` 是否缺口；`health` | 所有检索失败写 trace；加健康检查 |
| 抽取一直不产出 | `llm.provider=none` / 群聊 / 消息太短 / 日上限 | `run.py extract` 的 `skipped` 字段 | 配 LLM；改私聊测试；降门槛；次日再试 |
| 写入总被判 duplicate | 与旧条目太像 / 阈值太低 | `create` 返回的 `existing_id` | 用 `update` 追旧条目；或调高 `dedupe_similarity` |
| embedding dim mismatch | 配置维度与服务端不一致 | `memory.config.json` 的 `embedding.dim` | 改成服务端实际维度；不要静默忽略 |
| 中文检索很差 | FTS 退化为 unicode61；嵌入是 hashing | `health.fts_tokenizer`；`embedding.provider` | 换带 trigram 的 SQLite；生产切 http 语义嵌入 |
| 换嵌入模型后检索变差/维度报错 | 旧向量未重算 | `health.stale_embeddings` | `run.py reindex`，再用 `eval` 重跑阈值 |

## 2. 三个排查命令

```bash
# 1. 库与索引是否健康（可写、表齐、schema 版本、FTS 分词器）
python memory/run.py health --json

# 2. 记录量、陈旧率、重复对、待审批候选、近 7 天 trace 计数
python memory/run.py stats --json

# 3. 直接看某条记忆的正文、evidence、标签
python memory/run.py read mem-xxxx --folder qq-private:123 --json
```

## 3. trace 怎么读

`memory/traces/YYYY-MM-DD.jsonl`，每行一个事件：

```json
{"event":"auto_recall","status":"injected","query_len":18,"query_hash":"ab12cd34ef56",
 "candidates":5,"threshold":0.6,
 "selected":[{"id":"mem-...","score":0.71,"type":"knowledge"}]}
```

常见事件：

| event:status | 含义 |
| --- | --- |
| `write:created` / `write:merged` / `write:rejected` | 写入结果（含 duplicate 的 existing） |
| `extract:candidate` / `extract:dropped` / `extract:parse_error` / `extract:llm_error` | 抽取链路 |
| `search:ok` / `search:empty` | 工具检索 |
| `auto_recall:injected` / `below_threshold` / `empty` | 自动预取 |
| `lifecycle:dry_run` / `applied` / `reconciled` | 清理 |
| `supervision:warned` | 工具监督告警 |

## 4. 常见配置错误

1. **`base_url` 少了 `/v1`**：OpenAI 兼容服务返回 404。正确形式 `http://127.0.0.1:8911/v1`。
2. **`min_score_http` 套用到 hashing 嵌入**：hashing 下几乎永远不注入。用 `min_score_hashing`。
3. **群聊也在跑抽取**：检查 `allow_group_extract` 与宿主传入的 `chat_type`。
4. **宿主没传 `folder_id`**：`Scope` 会拒绝空 folder，但宿主若把所有会话都填 `default`，隔离失效。
5. **用了 `--purge` 清数据**：不可恢复；只能从备份恢复。

## 5. Windows 下的引号

`run.py tool memory_command --args '{"command":"..."}'` 在 cmd 里容易把引号吃掉。
改用文件：

```bash
python memory/run.py tool memory_command --args-file args.json --folder qq-private:123
```

## 6. 什么时候该重装而不是修

- 主要配置（嵌入模型维度、folder 划分）彻底变了，且库内没有必须保留的数据；
- `schema_meta` 显示库版本高于代码支持版本（只能升级代码，不能降级库）；
- 大量条目 evidence 缺失、来源不可信，且没有审计价值。

重装前先备份 `data/memory.db`，再用 `uninstall` 只删代码、保留数据。
