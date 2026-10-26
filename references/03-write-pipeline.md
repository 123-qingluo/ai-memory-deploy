# 03 写入管线

> 实现在 `memory_core/write.py` 与 `memory_core/extract.py`。核心判断：写入保守，宁可漏记。

## 1. 四个入口

| 入口 | 触发 | 落点 |
| --- | --- | --- |
| 工具写入 | 模型调用 `memory_command create` | 直接入库（受闸门约束） |
| 偏好/边界抽取 | 每轮私聊结束后 | shadow 候选 → 审批 |
| 事件抽取 | 命中时间信号词的轮次 | shadow 候选 → 审批 |
| 导入 | 人工脚本 / 种子数据 | 直接入库（`source=ingest`） |

偏好与事件用**两个抽取器**，不合并：偏好看稳定性，事件看时间锚点，判据完全不同，合并会让两头都做不好，也无法独立限流。

## 2. 六道闸门

| # | 闸门 | 实现 | 防的问题 |
| --- | --- | --- | --- |
| 1 | 只私聊 | `scope.chat_type == group` 且 `allow_group_extract=false` 时跳过 | 群里玩笑变成"主人的偏好" |
| 2 | 只抽稳定信息 | 提示词限定 preference / boundary / event | 一次性闲聊沉淀为事实 |
| 3 | evidence 必须是原话子串 | 不匹配 → 整条丢弃；工具写入带 `source_text` 时同样校验 | 模型编造依据 |
| 4 | 相似度去重 | 同 folder + 同类型，余弦 ≥0.90（或 ≥0.85 且标题 Jaccard ≥0.80）→ 拒收 | 同义记忆刷屏 |
| 5 | 写入即审计 | 默认 `shadow=true`，候选落 `data/candidates.jsonl` + `extraction_audit`，用 `candidates` 命令审批 | 无人复核的自动记忆 |
| 6 | 敏感词直接拦 | `sensitive.json` 正则，命中即拒 | 密钥类信息进库 |

补充闸门：最小用户字数（默认 40）、消息级去重（`message_id`）、日上限（偏好 10 / 事件 5 / 总 10）、标题长度、type/bucket/source 枚举。

## 3. 抽取器契约

### 偏好 / 边界

```json
[{"type":"preference|boundary","title":"≤20字","content":"一句话",
  "evidence":"用户原话片段","tags":["≤2个"],"opposing":["≤2个"]}]
```

### 事件

```json
[{"type":"event","title":"≤20字","content":"一句话",
  "evidence":"用户原话片段","time":"明天下午 / 周六 / 下个月初",
  "related_people":["qq:12345"],"tags":["≤2个"],"opposing":["≤2个"]}]
```

规则：

- 只输出 JSON 数组；没有就输出 `[]`。
- `evidence` 必须逐字出现在输入原文里。
- `time` 原样摘录，不自己换算；代码解析失败留 0。

### 失败语义（写死，不重试）

| 情况 | 处理 |
| --- | --- |
| LLM 超时/网络错误 | 记录 trace `extract:llm_error`，放弃本轮 |
| JSON 解析失败 | 记录 trace `extract:parse_error`，放弃本轮 |
| 某条 evidence 不是原话 | 丢弃该条，其余保留 |
| 时间为空/无法解析 | 留 0，标 `event_time_missing` 警告 |
| 敏感信息 | 候选标 `blocked`，留档待人工看 |

为什么不重试：一次超时重试会放大成雪崩；宁可漏这一轮，下一轮再抽。

## 4. 冲突合并：新颖度能量

```
新正文向量 vs 同 folder 同类型最近的至多 8 条 active 记录
  ├─ 与最近一条相似度 < 0.50 → 直接新建
  ├─ novelty_energy < 0.10 且相似度 ≥ 0.50 → 追加合并（正文上限 800 字，重算向量）
  └─ 否则 → 新建
```

`novelty_energy = 新向量落在旧向量张成空间之外的能量占比`（0~1）。
低于截止值说明"没说什么新东西"，该合并；而不是单纯看余弦阈值。

顺序：**先算最终正文与向量，再更新记录**。反过来会在嵌入失败时留下空记录。
合并只合并 `active` 记录，不把归档/软删记录"写活"。

正文超过 800 字时保留首尾各 400 字并用 `……` 标记，避免只留下旧内容。

## 5. shadow 与审批

默认 `shadow=true`：

1. 抽取器只产候选，写 `data/candidates.jsonl` 与 `extraction_audit`。
2. `memoryctl candidates list` 查看；`approve` / `reject` 处理。
3. 审批时**重新校验** evidence 是否仍在 `source_text` 中。
4. 审批通过才走完整写入闸门；再次被拒（重复/敏感）会标 `blocked`。

典型 `warn` 条件：事件缺时间线索、含临时情绪词（今天/刚刚/有点/心情）、可能涉及第三方（朋友/同事/群友）。

本实现的 `block` 只用于**敏感信息**：候选会以 `blocked` 状态留在审计表里，人工可以看到但不会入库。
type 不符、正文为空、evidence <4 字等非法条目直接丢弃，并留 `extract:invalid_item` trace；evidence 不是原话的条目留 `extract:dropped` trace。

上线节奏：shadow 至少跑 2 周，抽查 50 条候选，错记 ≤5 条再关 shadow。

## 6. 写入反模式

| 反模式 | 后果 | 替代 |
| --- | --- | --- |
| 每轮自动摘要入库 | 库被闲聊淹没 | 信号词 + 长度门槛 + 日上限 |
| 让模型自由发挥正文 | 编造事实、人设漂移 | 强制 evidence + 类型枚举 |
| 全自动写入不审计 | 错记无法回溯 | 先 shadow，再逐类放开 |
| 只用向量去重 | 同义不同表述重复 | 向量 + 标题相似度双判 |
| 合并直接覆盖正文 | 丢失细节 | 追加 + 上限 + 保留来源 |
| 敏感词只在前端过滤 | 已入库无法追回 | 写入闸门 + 定期扫描 |

## 7. 工具写入示例

```bash
python memory/run.py create \
  -title "主人不吃香菜" \
  -content "主人不吃香菜，点餐时要避开香菜。" \
  -type knowledge \
  -triple_tags "香菜,饮食|点餐,外卖|随便,都行" \
  -evidence "我不吃香菜" \
  -source-text "我不吃香菜，点餐别放。" \
  --folder qq-private:123 --person user:456
```

提供 `-source-text` 时，`evidence` 必须是其连续子串，否则整条拒绝。
工具写入若不带 `-source_text`，`evidence_verified` 只能记为 0（未核对）；需要严格可核对时，走抽取→审批，或由宿主在调用 `create` 时传入该轮用户原话。
