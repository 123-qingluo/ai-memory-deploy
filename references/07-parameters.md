# 07 参数清单

> 默认值写在 `memory.config.json`。所有数值都是起点，不是唯一正确答案。

## 1. 写入

| 参数 | 默认 | 建议范围 | 调大的影响 |
| --- | --- | --- | --- |
| `min_user_chars` | 40 | 20–60 | 漏掉短而重要的陈述（如"我不吃香菜"） |
| `dedupe_similarity` | 0.90 | 0.85–0.95 | 调低→漏记同义；调高→重复 |
| `novelty_merge_cutoff` | 0.10 | 0.05–0.20 | 调高→更多合并，细节易丢 |
| `merge_similarity_floor` | 0.50 | 0.4–0.7 | 调低→不相干的记忆也被合并 |
| `merge_max_chars` | 800 | 400–1200 | 条目变长，检索精度下降 |
| `daily_cap_preference` | 10 | 5–20 | 记更多，噪声上升 |
| `daily_cap_event` | 5 | 3–10 | 同上 |
| `daily_cap_total` | 10 | 5–20 | 同上 |
| `shadow` | `true` | — | 关掉即自动写入，**不建议首周关** |
| `allow_group_extract` | `false` | — | 群聊玩笑变成偏好 |
| `max_title_chars` | 20 | 15–30 | 标题变长，检索报告感变重 |
| `evidence_min_chars` | 4 | 3–8 | 证据太短，无法核对 |

## 2. 读取

| 参数 | 默认 | 建议范围 | 调大的影响 |
| --- | --- | --- | --- |
| `top_k` | 5 | 3–10 | 成本与噪声 |
| `prefetch_count` | 3 | 2–5 | prompt 被记忆挤占 |
| `prefetch_summary_chars` | 160 | 100–250 | 注入变长，对话感变弱 |
| `min_score_http` | 0.60 | 0.55–0.70 | 调低→注入无关记忆 |
| `min_score_hashing` | 0.28 | 0.20–0.40 | 仅离线模式，按词面分布调 |
| `result_dedupe` | 0.88 | 0.80–0.95 | 调低→误删不同事实 |
| `fts_candidates_factor` | 3 | 2–5 | 候选增多，延迟上升 |
| `tag_core_bonus` | 0.12 | 0.05–0.20 | 标签压过语义 |
| `tag_diffusion_bonus` | 0.04 | 0.02–0.10 | 扩散过强，方向变散 |
| `tag_opposing_penalty` | 0.25 | 0.15–0.40 | 误伤相关记忆 |
| `rerank.enabled` | `false` | — | 需要独立 rerank 服务 |

**阈值与模型绑定**：0.60 是给 bge 一类语义模型的；hashing 词面嵌入必须用另一套（0.28）。换嵌入模型后必须重跑评测集再调，不能沿用数字。

## 3. 预取 / reminder

| 参数 | 默认 | 建议范围 | 调大的影响 |
| --- | --- | --- | --- |
| `prefetch.dedupe_window_s` | 120 | 60–300 | 连发时重复注入 |
| `prefetch.max_query_chars` | 200 | 120–400 | query 发散 |
| `prefetch.multi_topic_split` | `true` | — | 多话题批次只取最后一条 |
| `prefetch.allow_group` | `false` | — | 群聊串私聊 |
| `reminder.active_titles_limit` | 10 | 5–20 | reminder 挤占 prompt |
| `supervision.rounds_without_tool_warning` | 20 | 10–40 | 告警太频繁 |
| `supervision.cooldown_minutes` | 30 | 10–60 | 同一问题反复提醒 |
| `supervision.daily_max` | 3 | 1–5 | 告警疲劳 |

## 4. 生命周期

| 参数 | 默认 | 建议范围 | 调大的影响 |
| --- | --- | --- | --- |
| `archive_after_days` | 90 | 60–180 | 更长寿但库膨胀 |
| `activation_archive_threshold` | 1 | 1–2 | 调高→大量记忆被删 |
| `dry_run` | `true` | — | 关掉才真正动数据 |
| `allow_hard_delete` | `false` | — | 打开后不可恢复 |
| `pin_tag` | `pin` | — | 免死标签名 |

## 5. 嵌入 / LLM

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `embedding.provider` | `hashing` | `http` 才用于生产 |
| `embedding.base_url` | `http://127.0.0.1:8911/v1` | OpenAI 兼容，末尾带 `/v1` |
| `embedding.model` | `bge-small-zh-v1.5` | 与服务端一致 |
| `embedding.dim` | 512 | 与模型一致；不一致会报错而不是静默错配 |
| `llm.provider` | `none` | `http` 才开抽取 |
| `llm.model` | `deepseek-chat` | 抽取用的模型 |
| `llm.timeout_s` | 30 | 超时不重试，放弃本轮 |

**换嵌入模型（含 hashing → http）后必须执行 `python memory/run.py reindex --json`**，再用 `eval` 重跑阈值。旧向量与新 query 不同维/不同分布时，`health` 的 `stale_embeddings` 会大于 0，`ok=false`。

## 6. 调参顺序

1. **先调写入闸门**（决定库里有什么）。
2. **再调读取剂量**（决定模型看到什么）。
3. **最后调生命周期**（决定长期形态）。

反过来调通常无效：噪声库里的检索参数怎么调都不对。

## 7. 改动后的验收

每改一次检索参数或换嵌入模型：

```bash
python memory/run.py eval --json
python memory/run.py selfcheck --json
```

`eval` 的 Top3 命中率低于 0.60 或 `selfcheck` 有 FAIL，就不要上线。
