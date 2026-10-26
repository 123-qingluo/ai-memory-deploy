---
name: ai-memory-deploy
description: 把一套可验收的 AI 角色长期记忆系统部署进目标项目：五问定策→复制核心→建库→自检→交付接入点。用于 QQ/Discord/微信 bot、桌面 Agent 需要"跨会话记得住人"时；不用于聊天归档或文档问答。
disable-model-invocation: true
---

# AI 记忆系统部署

> 部署的是**档案**，不是聊天归档：有类型、有状态、可合并、可更正、可核对。
> 核心判断：把记忆当档案、把写入当审计、把读取当回忆、把遗忘当衰减。

## 何时用

- 给长期陪伴型 AI 角色（QQ / Discord / 微信机器人、桌面 Agent）加"跨会话记得住人"。
- 已有记忆系统但该记的没记、记得的不对、答非所问，需要一套可验收的基线。
- 要把一套已经验证过的记忆管线复用到新项目。

## 何时不用

| 想要什么 | 去哪 |
| --- | --- |
| 只把聊天记录存下来 | 用归档/全文搜索，不要记忆 |
| 文档问答、知识库 | 用 RAG；v1 不做文档切块导入 |
| 已有成熟系统只调参 | 直接看 `references/08-troubleshooting.md` |
| 人格/情绪状态机 | 那是另一个系统；记忆只管事实（见 `references/10-integration.md`） |

## 交付物

部署后在目标项目下新增一个目录，不改宿主业务代码：

```
<target>/memory/
├─ memory_core/           # 核心（Python ≥3.11，标准库为主）
├─ prompts/               # 偏好/事件两个抽取器提示词
├─ templates/             # 命令手册、reminder 模板、工具 schema
├─ eval/queries.json      # 20 条种子 + 20 条查询的离线评测集
├─ examples/              # 宿主适配器示例、可选 HTTP stub
├─ sensitive.json         # 写入敏感词规则
├─ memory.config.json     # 唯一配置源
├─ data/memory.db         # SQLite（记录/标签/向量/FTS/候选/便签）
├─ traces/                # 每日 JSONL：检索/抽取/清理/监督
├─ reports/               # 生命周期报告
├─ run.py                 # CLI 入口
└─ DEPLOY-REPORT.md       # 部署证据：健康检查、自检、决策、接入说明
```

## 执行流程

### Step 0 五问闸门（不可跳过）

逐项向用户确认，或让用户回答"按默认"。把回答写进 `decisions.md`：

| # | 问题 | 默认答案 |
| --- | --- | --- |
| 1 | 记什么？ | 稳定偏好、习惯、边界、过敏、重要日期、约定、反复话题、重要事件 |
| 2 | 不记什么？ | 密钥/验证码/证件/住址；临时情绪、玩笑、扮演、一次性闲聊；第三方隐私 |
| 3 | 用哪些类型？ | person / event / knowledge / procedure；place、asset 预留不启用 |
| 4 | 可核对强度？ | 抽取强制 evidence 原话子串；不匹配整条丢弃 |
| 5 | 删除策略？ | 90 天窗口；先 dry-run；硬删默认关闭；`pin` 免疫 |

用户没有明确回答时，用默认值，并把"使用了默认值"写进 `decisions.md`，不要静默假设。

### Step 1 预检

- Python ≥3.11（`python --version`）。
- 目标目录存在且可写。
- 目标下是否已有 `memory/`：默认保留现有代码与配置；加 `--force` 刷新代码（不动 data/），加 `--force-config` 才会重写配置。
- `--force` 会覆盖 `prompts/`、`templates/`、`eval/`；改过提示词或评测集先备份。`sensitive.json` 只在 `--force-config` 时覆盖。
- 明确告诉用户：部署只新增 `<target>/memory/`，不会改宿主代码。

### Step 2 部署

以下命令相对于本 skill 目录；`--target` 用绝对路径，避免落到错误目录。

```bash
python scripts/deploy.py \
  --target <目标项目根> \
  --profile offline \
  --decisions-file decisions.md
```

- `--profile offline`：确定性哈希嵌入，任何机器可跑，**只用于验收/离线**。
- `--profile http`：生成待填的 OpenAI 兼容配置（嵌入 + LLM）。
- 重复执行是幂等的；`--force` 刷新代码，保留配置与数据。
- `--dry-run` 只打印计划。

### Step 3 读报告，判定"部署完成"

打开 `<target>/memory/DEPLOY-REPORT.md`：

- 健康检查 `ok=true`，schema v1，FTS `trigram`，写探针成功。
- 自检 **14/14**；离线评测 Top3 **≥0.60**。
- 任何 FAIL：按 `references/08-troubleshooting.md` 修，修完重跑 `python memory/run.py selfcheck --json`。
- 若是换嵌入模型导致 `stale_embeddings>0`：先跑 `python memory/run.py reindex --json`，再重跑 health/selfcheck。
- **不要把未验收的系统接进对话。**

### Step 4 宿主接入（三处 + 工具）

| 位置 | 调用 |
| --- | --- |
| 构建 prompt 前 | `system.prefetch(scope, 最后一条用户消息)` + `system.reminder(scope)` |
| 一轮结束后 | `system.extract_after_turn(scope, 用户原话, message_id=...)`（默认 shadow） |
| 模型工具调用 | `dispatch(system, scope, name, arguments)["text"]` |
| 每天低峰期 | `system.run_lifecycle(dry_run=True)` |

`Scope(folder_id=..., person_id=..., chat_type=...)` 每次必传；群聊与私聊用不同 `folder_id`。
完整示例：`<target>/memory/examples/adapter_stub.py`；协议说明：`references/10-integration.md`。

### Step 5 交付说明

向用户报告，至少包含：

1. 部署目录与数据库路径；
2. 自检结果与评测命中率（引用报告中的真实数字）；
3. 接入三处与工具 schema 的位置；
4. 当前是 offline 模式，切生产要改 `embedding.provider` / `llm.provider` 为 `http` 并填三项；换嵌入模型后对已有数据跑 `python memory/run.py reindex --json`（`health` 的 `stale_embeddings` 必须为 0）；
5. 抽取默认 shadow：候选在 `data/candidates.jsonl`，用 `python memory/run.py candidates list` 审批；
6. 生命周期默认 dry-run，报告在 `reports/`；
7. 回滚：`python memory/run.py uninstall`（保数据）/ `--purge --yes`（连数据删）。

### Step 6 上线后的头两周

- 每天看 `reports/lifecycle-*.json`（应全为 0，因为刚开始没有 90 天旧数据）。
- 每周看候选：`candidates list`，抽查 10 条，统计错记率；错记 >10% 不要关 shadow。
- 用真实对话抽 20 轮，人工判断注入相关性 ≥70%（见 `references/09-acceptance.md`）。

## 硬规则

1. **不改宿主业务代码**：只新增 `memory/`，接入由 adapter stub 与 hook 契约完成。
2. **不跳过自检**：`selfcheck` 不是可选项；没有 14/14 就没有"部署完成"。
3. **不默认开硬删**：`dry_run=true`、`allow_hard_delete=false`；开启前必须人工看报告。
4. **不把 hashing 当生产嵌入**：它只做词面相似，阈值也不同（0.28 vs 0.60）。
5. **不把敏感信息写进库**：密码/验证码/证件/住址/token 命中即拒；不要建议模型换措辞绕过。
6. **不伪造记忆**：evidence 必须是原话子串；没有记录时模型应说不记得。
7. **不跨通道**：群聊与私聊 folder 必须不同；群聊默认不抽取、不预取。

## v1 边界（诚实列出）

- 未实现：随机闪回、每周巩固合并、自述台账、管理页、文档导入（knowledge 桶可检索但无切块）。
- 抽取审批只有 CLI，没有对话审批中心。
- 三元标签在自动抽取时允许显式空组（`-`）；工具写入要求至少一组非空。
- `pin` 只能手工加，没有"关键类自动免疫"。
- 并发写入只保证 WAL + 5s busy timeout，不保证高并发。

## 参考索引（按需读，不要一次全读）

| 文件 | 什么时候读 |
| --- | --- |
| `references/01-decisions.md` | 要向用户解释"为什么这样设计"、做五问时 |
| `references/02-data-model.md` | 改表、排查数据、备份/迁移时 |
| `references/03-write-pipeline.md` | 调写入闸门、改抽取器、处理候选时 |
| `references/04-read-pipeline.md` | 调检索/预取剂量、注入位置时 |
| `references/05-lifecycle.md` | 开归档/硬删、写定时任务时 |
| `references/06-model-interface.md` | 写系统提示、接工具、调 reminder 时 |
| `references/07-parameters.md` | 调任何参数前 |
| `references/08-troubleshooting.md` | 自检失败或线上效果不对时 |
| `references/09-acceptance.md` | 判断"部署完成"、上线检查时 |
| `references/10-integration.md` | 接宿主、处理并发与失败策略时 |
