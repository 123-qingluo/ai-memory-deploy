# AI Memory Deploy

> 给长期陪伴型 AI 角色（QQ / Discord / 微信机器人、桌面 Agent）部署一套**可验收**的长期记忆系统。
> 部署的是档案，不是聊天归档：有类型、有状态、可合并、可更正、可核对。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 它是什么

一个 agent skill + 一套可直接运行的 Python 部署包：

- **五问闸门**：先确定记什么、不记什么、类型、可核对强度、删除策略；
- **一键部署**：把核心复制到 `<项目>/memory/`，建库并跑 14 项自检，不改宿主业务代码；
- **接入契约**：`prefetch`（自动预取）、`reminder`（每轮提醒）、`extract_after_turn`（抽取候选）三个 hook，加 `memory_command` / `temporary_memo` 两个模型工具；
- **可回滚**：默认卸载保留数据，`--purge --yes` 才连数据删除。

核心能力：六道写入闸门、evidence 原话校验、三元标签、相似度去重、新颖度合并、shadow 候选审批、自动预取（3 条 × 160 字）、FTS5 + 向量混合检索、90 天分级衰减、trace、离线评测、`reindex`、folder 数据级隔离。

## 它不是什么

- 不是聊天归档：归档 append-only，记忆是编辑过的档案。
- 不是文档问答/RAG：v1 不做文档切块导入。
- 不是人格状态机：记忆只提供事实与关系状态，不决定"她此刻什么态度"。
- 不是长上下文替代品：记忆是把上下文压缩成可核对的事实。

## 快速开始

### 方式一：作为 agent skill

把整个目录放进 skills 目录（例如 `~/.zcode/skills/ai-memory-deploy/`），然后显式调用：

```
/ai-memory-deploy
```

### 方式二：直接部署

```bash
python scripts/deploy.py \
  --target /path/to/project \
  --profile offline \
  --decisions-file decisions.md
```

部署完成后读 `/path/to/project/memory/DEPLOY-REPORT.md`：

- 健康检查 `ok=true`；
- 自检 **14/14**；
- 离线评测 Top3 **≥0.60**。

任何一项不过，不要把系统接进对话。

## 目录结构

```
ai-memory-deploy/
├─ SKILL.md                 # agent 执行手册：五问 → 部署 → 验收 → 接入
├─ references/              # 01–10：决策、数据模型、写入/读取、生命周期、接口、参数、排障、验收、接入
├─ assets/
│  ├─ memory_core/          # 可运行核心（Python ≥3.11，标准库为主）
│  ├─ prompts/              # 偏好 / 事件两个抽取器提示词
│  ├─ templates/            # 命令手册、reminder 模板、工具 JSON Schema
│  ├─ eval/queries.json     # 20 条种子 + 20 条中文查询的离线评测集
│  └─ examples/             # 宿主适配器示例、可选本机 HTTP stub
└─ scripts/deploy.py        # 部署器：复制、建库、自检、生成部署报告
```

部署后在目标项目生成：

```
<target>/memory/
├─ memory_core/  prompts/  templates/  eval/  examples/
├─ memory.config.json      # 唯一配置源
├─ sensitive.json          # 写入敏感规则
├─ data/memory.db          # SQLite：记录/标签/向量/FTS/候选/便签
├─ traces/                 # 每日 JSONL：检索/抽取/清理/监督
├─ reports/                # 生命周期报告
├─ run.py                  # CLI 入口
└─ DEPLOY-REPORT.md        # 部署证据
```

## 本次实测记录

2026-10-27，Windows 11 / Python 3.14.3 / SQLite 3.50.4（FTS5 trigram）：

- 全新部署：初始化 OK，自检 **14/14**，离线评测 **Top3 = 0.95（19/20）**；
- HTTP 协议路径（生产模式）用本地假 OpenAI 兼容服务验证：`create → search → extract → approve → health` 全通；
- 额外验证：幂等重部署、软删后健康检查、换嵌入模型后 `reindex`、自定义 embedder 注入、两种卸载模式（保留数据 / 彻底删除）。

验收细节与本次实测的唯一未命中样例见 [`references/09-acceptance.md`](references/09-acceptance.md)。

## 离线模式与生产模式

- `--profile offline`：确定性哈希嵌入，任何机器可跑，**只用于验收/离线**。
- 生产：编辑 `memory/memory.config.json`，把 `embedding.provider` 与 `llm.provider` 改为 `http`，填 OpenAI 兼容的 `base_url` / `model` / `api_key`。
- 换嵌入模型后必须 `python memory/run.py reindex --json`，否则 `health.stale_embeddings > 0`，检索结果不可信。
- 阈值与模型绑定：http 默认 0.60，hashing 默认 0.28；换模型后重跑评测再调。

## 安全与边界

- 默认 `shadow=true`：自动抽取只产候选，人工 `approve` 后才入库。
- 默认 `dry_run=true` + `allow_hard_delete=false`：生命周期先出报告，硬删关闭。
- 敏感信息（验证码/证件/银行卡/住址/token 等）命中即拒；这是兜底，不是合规方案。
- 群聊与私聊使用不同 `folder_id`；群聊默认不自动抽取、不自动预取。
- v1 未实现：随机闪回、每周巩固合并、自述台账、管理页、文档导入；审批入口只有 CLI。

## 环境要求

- Python ≥3.11；
- SQLite 支持 FTS5（官方 Windows/macOS Python 自带；trigram 不可用时自动退 unicode61）；
- 无必装第三方依赖；numpy 存在时用于向量加速。

## 参考

设计判断已蒸馏进 `references/`，按需阅读：

| 文件 | 内容 |
| --- | --- |
| `01-decisions.md` | 产品判断与 ADR |
| `02-data-model.md` | 表结构、计数器、备份、换模型 |
| `03-write-pipeline.md` | 六道闸门、抽取契约、合并、审批 |
| `04-read-pipeline.md` | 预取、剂量、混合检索、注入顺序 |
| `05-lifecycle.md` | 对账、分级衰减、回滚阶梯 |
| `06-model-interface.md` | 工具、命令手册、reminder、监督 |
| `07-parameters.md` | 参数表与调参顺序 |
| `08-troubleshooting.md` | 症状 → 排查 → 处置 |
| `09-acceptance.md` | 验收门与实测记录 |
| `10-integration.md` | 宿主接入、并发、失败策略、自定义模型 |

## License

MIT。见 [LICENSE](LICENSE)。
