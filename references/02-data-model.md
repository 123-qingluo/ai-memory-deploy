# 02 数据模型

> 建表语句在 `memory_core/schema.sql`。这一篇解释字段为什么存在、哪些字段有隐含语义。

## 1. 表总览

| 表 | 作用 |
| --- | --- |
| `memory_records` | 长期记忆主表（memory / knowledge 两个桶共用） |
| `memory_tags` | 标签：`tag / core / diffusion / opposing` 四类独立成行 |
| `memory_embeddings` | 向量（BLOB），与主表一对一 |
| `memory_fts` | FTS5 全文索引（优先 trigram，失败退 unicode61） |
| `temporary_memos` | 短期便签，`consumed_at` 实现 ONCE 消费 |
| `extraction_audit` | 抽取候选与审批记录（shadow 的落点） |
| `processed_messages` | 消息级去重，防止同一条消息重复抽取 |
| `runtime_state` | 每 folder 的运行状态：预取去重窗口、监督计数等 |
| `schema_meta` | schema 版本与 FTS 分词器记录 |

## 2. 主表关键字段

| 字段 | 语义 | 注意 |
| --- | --- | --- |
| `memory_id` | `mem-` + 12 hex | 不支持手写 id |
| `folder_id` | **数据级隔离边界** | 每次调用必传；跨 folder 不检索、不可读 |
| `bucket` | `memory` / `knowledge` | knowledge 默认不参与闪回/预取 |
| `source` | `tool / auto_extract / event_extract / ingest` | 用于审计"这条从哪来" |
| `memory_type` | `person / event / knowledge / procedure` | person 必须带 `person_id` |
| `status` | `active / archived / expired` | archived 仍可检索；expired 是软删 |
| `evidence` | 用户原话片段 | `evidence_verified=1` 表示已核对为 source_text 子串 |
| `event_start_at` / `event_end_at` | 事件时间（Unix 秒） | 解析失败留 0，**不拿消息时间冒充** |
| `novelty_energy` | 写入时相对库内旧记忆的信息增量 | 低于截止值走合并 |
| `is_deleted` / `deleted_at` | 软删 | 软删同时置 status=expired、移出 FTS |

### 预留字段

`address_or_coord / place_type / asset_type / disposition_status` 在表里存在但 v1 不启用。
理由：槽位越多，抽取器乱填概率越高；等真有需求再放开，不用改表。

## 3. 三个计数器

| 字段 | 谁在加 | 用途 |
| --- | --- | --- |
| `activation_count` | 工具检索命中、预取注入、闪回命中 | **生命周期投票**：决定归档还是删除 |
| `search_hits` | 工具检索命中、预取注入 | 观测"哪些记忆靠检索被想起" |
| `flashback_hits` | 闪回注入（v1 未实现） | 观测随机回忆 |

拆开的原因：如果同一个计数既驱动淘汰又驱动闪回权重，热门记忆会同时更长寿、更少被闪回，两个目标互相打架。

## 4. 三元标签

```
core_tags       这条记忆"关于什么"
diffusion_tags  可以往哪些方向扩展
opposing_tags   必须避开的语义
```

标签单独成表的原因：需要"按类型整组替换"，JSON 列容易写坏；也便于统计"哪些扩散标签最常见"。

写入格式：`core1,core2|diffusion1,diffusion2|opposing1,opposing2`。
三组必须齐全；某组为空写 `-`。工具写入要求至少一组非空。

## 5. 桶与状态的语义

| 组合 | 是否可检索 | 说明 |
| --- | --- | --- |
| memory + active | 是 | 默认路径 |
| memory + archived | 需 `include_archived` | 生命周期降级，不是删除 |
| knowledge + active | 需 `include_knowledge` | 导入资料/稳定知识 |
| 任意 + is_deleted=1 | 否 | 软删，可恢复（数据库层面） |

## 6. 索引

```sql
(folder_id, bucket, is_deleted, is_archived)   -- 检索范围过滤
(memory_type, status, last_activated_at)       -- 生命周期扫描
(person_id)                                    -- 人物档案查询
(folder_id, updated_at)                        -- 最新标题列表
```

## 7. Schema 版本与迁移

- `schema_meta.schema_version` 当前为 `1`。
- 代码发现库版本高于自身支持版本会直接报错，**不允许降级数据**。
- 新增字段的迁移方式：写 `schema.sql` 的 `ALTER TABLE ... ADD COLUMN`（幂等难以保证时用版本判断），并同步 `SCHEMA_VERSION`。
- 升级前先备份 `data/memory.db` 与 `data/memory.db-wal`；WAL 里可能有未落盘事务。

## 8. 备份

```bash
# 冷备份（先停进程）
copy memory\data\memory.db      backup\memory-YYYYMMDD.db
copy memory\data\memory.db-wal  backup\memory-YYYYMMDD.db-wal

# 热备份（SQLite 在线备份）
python -c "import sqlite3; src=sqlite3.connect(r'memory/data/memory.db'); dst=sqlite3.connect(r'backup/memory.db'); src.backup(dst); dst.close(); src.close()"
```

向量与标签都在同一个库里，备份一个文件即可；`memory_fts` 可由主表重建（`reconcile`）。

## 9. 换嵌入模型

`memory_embeddings.model` 记录每条向量来自哪个模型。换模型（例如 hashing → bge）后，旧向量与新 query 的维度/分布都不一致，**必须先重算**：

```bash
python memory/run.py reindex --json
```

`health` 会报告 `stale_embeddings`；不为 0 时 `ok=false`，检索结果不可信。换模型后还要用 `eval` 重跑阈值。
