# 05 生命周期

> 实现在 `memory_core/lifecycle.py`。核心判断：遗忘是分级衰减，不是一刀切 TTL。

## 1. 每日清理

```
每日 04:00 低峰期
1. reconcile：向量库与 SQLite 对账（补缺失、清孤儿）
2. 扫描 updated_at 超过 90 天的 memory 记录
   ├─ 含 pin 标签        → 跳过（唯一免死金牌）
   ├─ activation_count≥1 → 归档（archived，仍可检索）
   └─ activation_count<1 → 删除候选
3. 写清理报告 reports/lifecycle-YYYYMMDD-HHMMSS.json
```

为什么 90 天：一个季节。短于 90 天会误杀季节性话题；长于 180 天库会明显膨胀。

为什么按激活次数而不是最后访问：最后访问会被一次误召回刷新，激活次数更稳定。

## 2. 默认行为

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `lifecycle.dry_run` | `true` | 只出报告不动数据 |
| `lifecycle.allow_hard_delete` | `false` | 关闭时达到删除条件的记录保持 active，只记为 `would_delete` |
| `lifecycle.archive_after_days` | 90 | 窗口 |
| `lifecycle.activation_archive_threshold` | 1 | 激活过就归档而不是删 |
| `lifecycle.pin_tag` | `pin` | 免疫标签 |

参考实现线上已开真删（`dry_run=false` + `allow_hard_delete=true`），**不要照抄**。生日、忌口、创伤边界这类"很少提但必须对"的事实会因一年未被召回而被判无用。

## 3. 上线阶梯

```
第 1 步：dry_run=true，跑 2–4 周，只看报告
第 2 步：抽查报告里 `would_delete` 的条目 30 条，认可 ≥27 条
第 3 步：开启归档（dry_run=false, allow_hard_delete=false）
第 4 步：再观察 4 周，确认归档没有误伤
第 5 步：才考虑 allow_hard_delete=true，且只对"从未被激活"的条目
```

每一步都可以回退：归档改回 active 即可；硬删不可恢复。

## 4. 可回滚矩阵

| 操作 | 数据是否还在 | 能否自动恢复 | 适用 |
| --- | --- | --- | --- |
| 合并 | 否（被合并条目删除） | 否（有 candidates/audit 记录） | 同义重复 |
| 归档 | 是（status=archived） | 是（改回 active） | 过期但可能有价值 |
| 软删 | 是（is_deleted=1） | 是（数据库层面） | 用户要求删除/纠错 |
| 硬删 | 否 | **否** | 从未被使用的陈旧记忆 |

说明：合并与硬删都不会为了"可回滚"保留全文；真正需要保留的内容请先用 `pin` 标记。

## 5. 对账（reconcile）

`lifecycle` 每轮先对账，修复四类不一致：

1. 主表有记录、`memory_embeddings` 缺失 → 重新嵌入。
2. `memory_fts` 缺行 → 从主表重建。
3. 孤立 embedding / tag / fts → 删除。
4. 嵌入失败 → 计入 `failed_embeddings`，不假装成功。

手动触发：

```bash
python memory/run.py lifecycle --json        # 含 reconcile + dry-run 清理
```

## 6. 每周巩固（v1 未实现）

参考实现的做法：标题 Jaccard ≥0.45 且向量相似度 ≥0.85 的条目聚类；≥0.90 自动合并，0.85–0.90 只生成提案。约束：摘要 ≤800 字、每组 ≥2 条、单次 ≤20 组、摘要置信度 ≥0.7。

v1 不做自动巩固，原因：没有人工审阅的自动合并是最容易"悄悄丢细节"的操作。需要时按以下步骤加：

1. 先只生成 `proposals.json`（不合并）；
2. 人工审一个月；
3. 再开自动合并，并保留来源链。

## 7. 定时任务

Linux/macOS：

```cron
0 4 * * * cd /path/to/memory && /usr/bin/python3 run.py lifecycle --json >> logs/lifecycle.log 2>&1
```

Windows（任务计划程序）：

```
程序：python.exe
参数：run.py lifecycle --json
起始于：C:\path\to\memory
触发器：每天 04:00
```

建议先只跑 dry-run，报告进 `reports/`；确认后再改配置开归档。

## 8. 已知取舍

- `pin` 只能手工加，没有"关键类自动免疫"。上线前把生日、过敏、创伤边界等手工 `pin`。
- 归档后默认不参与预取，但仍可 `search -include_archived true`。
- 生命周期只处理 `bucket='memory'`；`knowledge` 桶不自动清理。
