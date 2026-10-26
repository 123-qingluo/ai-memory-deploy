#!/usr/bin/env python3
"""把 ai-memory-deploy 部署到目标目录，并跑一次自检。

用法：
  python scripts/deploy.py --target D:\\path\\to\\project
  python scripts/deploy.py --target . --profile offline --dry-run

只新增 <target>/memory/，不改宿主业务代码；重复执行不会清库。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "assets"

RUN_PY = '''"""部署入口：把 memory 子目录设为根，再转给 CLI。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["MEMORY_ROOT"] = str(ROOT)
sys.path.insert(0, str(ROOT))

from memory_core.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
'''


def copy_tree(src: Path, dst: Path, force: bool, log: list[str]) -> None:
    if dst.exists() and force:
        shutil.rmtree(dst)
        log.append(f"替换 {dst}")
    if not dst.exists():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        log.append(f"复制 {src.name} → {dst}")
    else:
        log.append(f"保留已存在的 {dst}")


def copy_file(src: Path, dst: Path, force: bool, log: list[str]) -> None:
    if dst.exists() and not force:
        log.append(f"保留已存在的 {dst}")
        return
    shutil.copy2(src, dst)
    log.append(f"写入 {dst}")


def load_profile(profile: str) -> dict:
    cfg = json.loads((ASSETS / "memory.config.json").read_text(encoding="utf-8"))
    if profile == "http":
        cfg["embedding"]["provider"] = "http"
        cfg["llm"]["provider"] = "http"
    return cfg


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return subprocess.run(cmd, cwd=str(cwd), text=True, encoding="utf-8", capture_output=True, env=env)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="部署 AI 角色长期记忆系统")
    parser.add_argument("--target", required=True, help="目标项目根目录（必须显式指定，避免误部署到当前目录）")
    parser.add_argument("--profile", choices=["offline", "http"], default="offline", help="offline=确定性哈希嵌入；http=接 OpenAI 兼容服务")
    parser.add_argument("--force", action="store_true", help="覆盖代码目录（含 prompts/templates/eval；不动 data/）")
    parser.add_argument("--force-config", action="store_true", help="覆盖 memory.config.json 与 sensitive.json（会丢自定义）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--skip-selfcheck", action="store_true")
    parser.add_argument("--decisions-file", default=None, help="部署前五问的回答，写进部署报告")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print(f"需要 Python ≥3.11，当前 {sys.version.split()[0]}", file=sys.stderr)
        return 2

    target = Path(args.target).expanduser().resolve()
    memory_root = target / "memory"
    plan = [
        f"目标项目：{target}",
        f"部署目录：{memory_root}",
        f"profile：{args.profile}",
    ]
    if args.dry_run:
        print("\n".join(plan))
        print("dry-run：不写任何文件。")
        return 0

    if not target.exists():
        print(f"目标目录不存在：{target}", file=sys.stderr)
        return 2
    if not target.is_dir():
        print(f"目标不是目录：{target}", file=sys.stderr)
        return 2
    memory_root.mkdir(parents=True, exist_ok=True)

    log: list[str] = []
    copy_tree(ASSETS / "memory_core", memory_root / "memory_core", args.force, log)
    copy_tree(ASSETS / "prompts", memory_root / "prompts", args.force, log)
    copy_tree(ASSETS / "templates", memory_root / "templates", args.force, log)
    copy_tree(ASSETS / "eval", memory_root / "eval", args.force, log)
    copy_tree(ASSETS / "examples", memory_root / "examples", args.force, log)
    copy_file(ASSETS / "sensitive.json", memory_root / "sensitive.json", args.force_config, log)

    config_path = memory_root / "memory.config.json"
    if args.force_config or not config_path.exists():
        config_path.write_text(
            json.dumps(load_profile(args.profile), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.append(f"写入 {config_path}（profile={args.profile}）")
    else:
        log.append(f"保留已存在的 {config_path}")

    (memory_root / "run.py").write_text(RUN_PY, encoding="utf-8")
    log.append(f"写入 {memory_root / 'run.py'}")
    for sub in ("data", "traces", "reports"):
        (memory_root / sub).mkdir(exist_ok=True)

    started = time.time()
    init = run([sys.executable, "run.py", "init", "--json"], memory_root)
    init_ok = init.returncode == 0
    health = {}
    try:
        health = json.loads(init.stdout)
    except json.JSONDecodeError:
        health = {"raw": (init.stdout or init.stderr)[-2000:]}

    selfcheck = {}
    selfcheck_ok = True
    if not args.skip_selfcheck:
        check = run([sys.executable, "run.py", "selfcheck", "--json"], memory_root)
        selfcheck_ok = check.returncode == 0
        try:
            selfcheck = json.loads(check.stdout)
        except json.JSONDecodeError:
            selfcheck = {"ok": False, "raw": (check.stdout or check.stderr)[-3000:]}

    decisions = ""
    if args.decisions_file:
        path = Path(args.decisions_file)
        if path.exists():
            decisions = path.read_text(encoding="utf-8")

    report = [
        "# 记忆系统部署报告",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 目标项目：`{target}`",
        f"- 部署目录：`{memory_root}`",
        f"- profile：`{args.profile}`" + ("（未验证外部嵌入/LLM 连通性，填好配置后先跑 eval）" if args.profile == "http" else ""),
        f"- Python：{sys.version.split()[0]}",
        *(
            ["- 注意：offline 使用确定性哈希嵌入，仅用于验收/离线；生产请切 http 嵌入与 LLM。"]
            if args.profile == "offline"
            else []
        ),
        "",
        "## 文件操作",
        "",
        *[f"- {line}" for line in log],
        "",
        "## 目标库健康检查",
        "",
        "```json",
        json.dumps(health, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    if selfcheck:
        report += [
            "## 自检结果",
            "",
            f"- 结果：{'通过' if selfcheck.get('ok') else '未通过'}（{selfcheck.get('passed')}/{selfcheck.get('total')}）",
            f"- 耗时：{selfcheck.get('duration_ms')} ms",
            f"- 离线评测 Top3：{selfcheck.get('eval', {}).get('hit_rate')}",
            "",
            "| 检查 | 结果 | 说明 |",
            "| --- | --- | --- |",
        ]
        for item in selfcheck.get("checks", []):
            report.append(f"| {item['name']} | {'PASS' if item['ok'] else 'FAIL'} | {item.get('detail', '')} |")
        report.append("")
        if not selfcheck.get("ok"):
            report += ["**自检未通过：先修问题，不要把未验收的系统接进对话。**", ""]

    if decisions:
        report += ["## 部署前产品判断（用户回答）", "", decisions, ""]

    report += [
        "## 接入（宿主侧三处）",
        "",
        "1. 构建 prompt 前：`prefetch(scope, 最后一条用户消息)`，把 `text` 放在 prompt 靠后位置。",
        "2. 每轮 reminder：`reminder(scope)`，拼接标题块/便签块/监督告警。",
        "3. 一轮结束后：`extract_after_turn(scope, 用户原话, message_id=...)`（默认 shadow，进候选队列）。",
        "",
        "模型工具：`tools.dispatch(system, scope, name, args)`，工具定义见 `templates/tool_schema.json`。",
        "示例代码：`memory/examples/adapter_stub.py`。",
        "",
        "## 定时任务",
        "",
        "每天低峰期执行（默认 dry-run，先看报告再开归档/硬删）：",
        "",
        "```bash",
        "python memory/run.py lifecycle --json",
        "```",
        "",
        "## 切生产模型",
        "",
        "编辑 `memory/memory.config.json`：",
        "- `embedding.provider` 改 `http`，填 `base_url` / `model` / `api_key`（OpenAI 兼容 /embeddings）。",
        "- `llm.provider` 改 `http`，填同样的三项（OpenAI 兼容 /chat/completions）。",
        "- 阈值随模型变：`min_score_http` 默认 0.60，上线后用离线评测集重跑再调。",
        "",
        "## 回滚",
        "",
        "```bash",
        "python memory/run.py uninstall              # 只删代码/配置，保留数据",
        "python memory/run.py uninstall --purge --yes  # 连数据一起删（不可恢复）",
        "```",
        "",
    ]
    (memory_root / "DEPLOY-REPORT.md").write_text("\n".join(report), encoding="utf-8")

    print(f"部署目录：{memory_root}")
    print(f"初始化：{'OK' if init_ok else 'FAIL'}；自检：{'未跑' if args.skip_selfcheck else ('OK' if selfcheck_ok else 'FAIL')}")
    print(f"报告：{memory_root / 'DEPLOY-REPORT.md'}")
    if not init_ok:
        print((init.stderr or init.stdout)[-2000:], file=sys.stderr)
    if not selfcheck_ok and selfcheck:
        for item in selfcheck.get("checks", []):
            if not item["ok"]:
                print(f"[FAIL] {item['name']}: {item['detail']}", file=sys.stderr)
    print(f"耗时：{int((time.time() - started) * 1000)} ms")
    return 0 if init_ok and selfcheck_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
