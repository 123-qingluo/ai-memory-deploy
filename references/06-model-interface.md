# 06 模型侧接口

> 两个工具、一份手册、三块 reminder。目标是让模型"会用"，而不是"知道有这个东西"。

## 1. 两个工具，边界清楚

| 工具 | 用途 | 生命周期 |
| --- | --- | --- |
| `memory_command` | 长期记忆：`search / read / create / update / delete`，支持 `&&` 串联 | 永久（受生命周期管理） |
| `temporary_memo` | 短期便签：跨群并行、多步任务 | 小时级自动过期，**不进检索** |

手册里必须明写："**不要把临时便签当作长期记忆**"。否则模型会把两者混用。

工具定义见 `templates/tool_schema.json`；分发器见 `memory_core/tools.py`，宿主只需一次
`dispatch(system, scope, name, arguments)`。

## 2. 命令手册（常驻 system prompt）

完整文本在 `templates/command_manual.md`，结构：

1. 工具边界；
2. 记什么 / 不记什么（逐条）；
3. 检索纪律：涉及偏好/约定/过往事件先 search；没有就说不记得，不要编造；
4. 类型约定：person 一人一档；偏好/边界 → knowledge；发生过的事 → event；流程 → procedure；
5. 写入格式：三元标签三组齐全，空组写 `-`；
6. 保护标签：`关键` 只用于人工识别，真正免清理要 `pin`；
7. 双轨检索：默认 memory 桶，需要时加 `-include_knowledge true`。

## 3. 每轮 reminder 的三块

```
① 命令手册（静态，策略）
② 最新 10 条 active 记忆标题（动态）
   附一句："这不是全部记忆，不要把这个列表当作全集"
③ 临时备忘录（动态，ONCE 消费）
```

②的附注极其重要：不加的话，模型会认为"列表里没有 = 从未发生"，从而否认用户说过的事。

`reminder()` 每轮只调用一次：它内部会给"未使用工具轮数"加一，重复调用会重复计数。

`reminder()` 一次返回三块的文本与结构化字段：

```json
{
  "titles": ["..."],
  "memos": ["..."],
  "warning": "[工具提醒] ...",
  "text": "..."
}
```

## 4. 自动预取块

每轮构建 prompt 时调用 `prefetch(scope, 最后一条用户消息)`，返回：

```json
{
  "items": [{"id": "...", "title": "...", "summary": "...", "type": "knowledge", "score": 0.71}],
  "text": "[记忆预取] ...",
  "status": "injected",
  "threshold": 0.6
}
```

`status` 可能是 `injected / below_threshold / empty / skipped_group / empty_query`。
**只有 `injected` 才拼进 prompt**；其余情况不要伪造一段"没有相关记忆"的话。

## 5. 工具使用监督

连续 20 轮没有调用 `memory_command` → 注入一次告警：

```
[工具提醒] 已经连续 20 轮没有查过记忆。涉及偏好、约定或过往事件时，
先 search 再回答；没有记录就说不记得，不要凭印象编造。
```

冷却 30 分钟、每日最多 3 次。实现里任何一次 `search/read/create/update/delete` 都会重置计数。

这是对抗"模型走捷径"的最低成本手段：不训练、不加重试，只在它偷懒时提醒。

## 6. 工具的返回文案

模型会照着返回文案行动，所以文案本身就是策略：

- 检索为空：`没有找到相关记忆。不要编造；可以换关键词再搜一次。`
- 写入被拒：说明原因（重复/敏感/evidence 不匹配），**不要**建议模型换个说法绕过。
- `read` 不存在：`没有找到这条记忆。`
- 便签：`它不是长期记忆。`

## 7. 命令示例

```
search 香菜
search 周末安排 -top_k 8
read mem-abc123
create -title "主人怕冷" -content "夏天空调只开 26 度" -type knowledge -triple_tags "怕冷,空调|夏天,温度|冰饮" -evidence "我怕冷，空调开 26 度就行"
update mem-abc123 -content "夏天空调只开 26 度，且不要对着吹"
delete mem-abc123
search 香菜 && read mem-abc123
```

`update` 的 `-triple_tags` 会整组替换，不是追加；`delete` 是软删。

## 8. 群聊

- 群聊不自动抽取（`allow_group_extract=false`）与不自动预取（`allow_group=false`）都是默认。
- 群聊仍可显式 `search`，但宿主应传群聊自己的 `folder_id`，不要把私聊 folder 的 id 传进去。
- 手册里写明：不把私聊记忆带进群聊复述。

## 9. 与人格状态机的边界

记忆只提供事实与关系状态，**不决定角色此刻的态度**。
把 `prefetch` / `reminder` 的文本作为 prompt 的一部分注入，不要写进人格状态机的判定逻辑；记忆块位置放在靠后（见 04 篇）。

如果角色有独立的人格/情绪状态机（例如 CBVTM），两者通过 prompt 共存即可：
状态机决定"怎么说"，记忆决定"说什么事实"。
