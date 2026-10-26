# 10 宿主接入

> 记忆核心与宿主解耦：宿主只需要传 `Scope`，并在三个位置调用三个方法。

## 1. Scope 是硬边界

```python
Scope(folder_id="qq-private:123", person_id="user:456", chat_type="private")
```

| 字段 | 规则 |
| --- | --- |
| `folder_id` | 必填；每个会话/通道一个。群聊与私聊必须不同 |
| `person_id` | 与用户相关时填 `platform:id`；person 类型记忆必须非空 |
| `chat_type` | `private` / `group` / 空；决定是否自动抽取与预取 |

推荐：`folder_id = f"{chat_type}:{chat_id}"`，`person_id = f"{platform}:{user_id}"`。

不要把多个会话都填 `default`：数据级隔离就失效了。

## 2. 三个 hook

### ① 构建 prompt 前

```python
block = system.prefetch(scope, last_user_text)
reminder = system.reminder(scope)
prompt_tail = "\n\n".join(x for x in (reminder["text"], block["text"]) if x)
```

- `prefetch`：3 条 × 160 字，失败返回空文本，不抛异常。
- `reminder`：10 条标题 + 便签（ONCE）+ 工具监督告警。
- 位置：放在 prompt 靠后（见 04 篇注入顺序）。

### ② 一轮对话结束后

```python
system.extract_after_turn(scope, user_text, message_id=message_id)
```

- 默认 `shadow=true`，只产候选，不写库。
- `message_id` 必须稳定，否则同一条消息会被重复抽取。
- 群聊默认跳过；用户消息 <40 字跳过。
- 这个调用不要阻塞回复：宿主可放到后台队列。

### ③ 模型工具调用

```python
result = dispatch(system, scope, name, arguments)
return result["text"]
```

支持 `memory_command` 与 `temporary_memo`；工具定义见 `templates/tool_schema.json`。

## 3. 定时任务

```python
system.run_lifecycle(dry_run=True)   # 每天低峰期
```

先 dry-run 看 `reports/lifecycle-*.json`，确认后改配置 `dry_run=false`。

## 4. 失败策略

记忆是增强，不是主链路。推荐：

| 失败 | 行为 |
| --- | --- |
| `prefetch` 抛异常 | 捕获、记日志、prompt 继续构建（不注入） |
| 嵌入服务不可用 | 核心内部降级到词面检索并写 trace；`prefetch` 仍可返回结果 |
| 抽取超时 | 核心内部放弃本轮（不重试）；不影响回复 |
| 写入被拒 | 返回原因文本给模型；不要让模型换措辞绕过 |

**不要把记忆调用包成"失败就重试三次"**：超时重试会放大成雪崩，且重复抽取会产生重复候选。

## 5. 非 Python 宿主

三种方式，按优先级：

1. **CLI 子进程**：`python memory/run.py tool memory_command --args-file args.json ...`，解析 stdout JSON。
2. **HTTP 适配示例**：`examples/http_stub.py`（仅开发用，无鉴权、无并发控制，不要直接暴露公网）。
3. **自己实现薄适配**：协议就是 `Scope` + 三个方法 + 两个工具，数据在 SQLite 里。

## 6. 与人格状态机的边界

如果宿主有独立的人格/情绪状态机（例如 CBVTM）：

- 状态机决定"怎么说"（语气、拒绝、沉默）；
- 记忆决定"说什么事实"（偏好、约定、边界）；
- 两者通过 prompt 共存，记忆块放在靠后；
- **记忆不写人格状态**，人格状态机也不写记忆表。

一句话：记忆只提供事实与关系状态，不替角色演。

## 7. 完整示例

见 `examples/adapter_stub.py`（三处 hook + 工具分发）与 `examples/http_stub.py`（可选 HTTP）。

## 8. 并发与部署形态

- 单进程内调用最稳；SQLite 已开 WAL 与 5s busy timeout。
- 多进程写同一库需要自行压测；高并发场景建议单写进程 + 读缓存。
- 不要把库放在网络盘上：SQLite 的锁与 WAL 在 SMB/NFS 上不可靠。
- 备份时连 `-wal` 一起备份，或使用 SQLite 在线备份 API。

## 9. 接入验收

接完后至少跑一次真实链路：

1. 发一条包含稳定偏好的长消息（≥40 字）；
2. 确认 `data/candidates.jsonl` 出现候选；
3. `candidates list` → `approve`；
4. 下一轮问相关问题，确认 `prefetch` 注入该条；
5. 确认 prompt 里出现注入文本，且模型回答引用了它。

## 10. 自定义嵌入 / LLM

不想用内置的 OpenAI 兼容客户端时，直接注入对象：

```python
system = MemorySystem(root, cfg, embedder=my_embedder, llm=my_llm)
```

契约：

| 对象 | 必需属性/方法 |
| --- | --- |
| `embedder` | `.embed(texts: list[str]) -> list[list[float]]`、`.model`（字符串）、`.kind`（`hashing` / `http`，决定阈值选择） |
| `llm` | `.complete(system: str, user: str) -> str`、`.available`（bool）、`.model` |

注入后 `health` 的 `embedding_model` 会显示自定义 `.model`；换模型仍要 `reindex`。
