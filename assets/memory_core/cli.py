"""命令行入口：部署脚本、agent 运维、定时任务都用它。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

VERSION = "1.0.0"


def _root(args) -> Path:
    return Path(args.root or os.environ.get("MEMORY_ROOT", ".")).resolve()


def _scope(args):
    from .models import Scope

    return Scope(
        folder_id=getattr(args, "folder", None) or "default",
        person_id=getattr(args, "person", None) or "",
        chat_type=getattr(args, "chat_type", None) or "",
    )


def _emit(obj, as_json: bool, human=None) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif human is not None:
        print(human)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryctl",
        description="AI 角色长期记忆系统：部署、写入、检索、抽取审批、生命周期。",
    )
    parser.add_argument("--root", default=None, help="部署根目录（含 memory.config.json）；默认取 MEMORY_ROOT 或当前目录")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    def scope_args(p):
        p.add_argument("--folder", default="default", help="作用域 folder_id（数据级隔离）")
        p.add_argument("--person", default="", help="person_id，例如 qq:12345")
        p.add_argument("--chat-type", default="", choices=["", "private", "group"], help="通道类型")

    sub.add_parser("init", help="建库并做可写性探测")
    sub.add_parser("health", help="健康检查")
    sub.add_parser("stats", help="统计与指标")

    p = sub.add_parser("create", help="写入一条记忆")
    scope_args(p)
    p.add_argument("-title", required=True)
    p.add_argument("-content", required=True)
    p.add_argument("-type", default="knowledge", choices=["person", "event", "knowledge", "procedure"])
    p.add_argument("-bucket", default="memory", choices=["memory", "knowledge"])
    p.add_argument("-triple_tags", required=True, help="core|diffusion|opposing，空组写 -")
    p.add_argument("-tags", default="", help="普通标签，逗号分隔")
    p.add_argument("-evidence", default="")
    p.add_argument("-source-text", default="", help="用户原话；提供时 evidence 必须是其子串")
    p.add_argument("-knowledge-type", default="")
    p.add_argument("-procedure-type", default="")

    p = sub.add_parser("search", help="检索记忆")
    scope_args(p)
    p.add_argument("query")
    p.add_argument("-k", "--top-k", type=int, default=None)
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--include-knowledge", action="store_true")

    p = sub.add_parser("read", help="读取一条记忆全文")
    scope_args(p)
    p.add_argument("memory_id")

    p = sub.add_parser("update", help="更新一条记忆")
    scope_args(p)
    p.add_argument("memory_id")
    p.add_argument("-title", default=None)
    p.add_argument("-content", default=None)
    p.add_argument("-triple_tags", default=None)
    p.add_argument("-tags", default=None)
    p.add_argument("-status", default=None, choices=["active", "archived", "expired"])

    p = sub.add_parser("delete", help="软删一条记忆")
    scope_args(p)
    p.add_argument("memory_id")

    p = sub.add_parser("prefetch", help="模拟每轮自动预取")
    scope_args(p)
    p.add_argument("query")

    p = sub.add_parser("reminder", help="输出每轮 reminder（标题/便签/监督告警）")
    scope_args(p)

    p = sub.add_parser("extract", help="对一条用户消息跑抽取（默认 shadow）")
    scope_args(p)
    p.add_argument("--user-text", required=True)
    p.add_argument("--message-id", default="")

    p = sub.add_parser("candidates", help="候选列表 / 审批 / 驳回")
    scope_args(p)
    p.add_argument("action", choices=["list", "approve", "reject"])
    p.add_argument("candidate_id", nargs="?")
    p.add_argument("--note", default="")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--status", default="pending", choices=["pending", "approved", "rejected", "blocked", "all"])

    p = sub.add_parser("lifecycle", help="生命周期清理（默认 dry-run）")
    p.add_argument("--folder", default=None, help="只处理某个 folder；默认全部")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--allow-hard-delete", action="store_true", help="允许硬删（不可恢复）")

    p = sub.add_parser("reindex", help="按当前嵌入模型重算全部向量（换模型后必须跑）")
    p.add_argument("--force", action="store_true", help="连模型未变的记录也重算")

    p = sub.add_parser("memo", help="临时便签")
    scope_args(p)
    p.add_argument("action", choices=["set", "list", "consume"])
    p.add_argument("--content", default="")
    p.add_argument("--expire-hours", type=float, default=None)

    p = sub.add_parser("eval", help="跑离线评测集（Top3）")
    p.add_argument("--dataset", default=None)
    p.add_argument("--top-k", type=int, default=3)

    sub.add_parser("selfcheck", help="在临时库跑完整自检")

    p = sub.add_parser("tool", help="模拟模型工具调用")
    scope_args(p)
    p.add_argument("name", choices=["memory_command", "temporary_memo"])
    p.add_argument("--args", default="{}", help="工具参数 JSON")
    p.add_argument("--args-file", default=None, help="从文件读参数 JSON（Windows 下避免引号转义）")

    p = sub.add_parser("uninstall", help="卸载：默认保留数据；--purge 连数据一起删，需二次确认")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--yes", action="store_true", help="非交互确认（与 --purge 同时使用）")

    # 允许 --json 写在子命令之后（deploy 脚本与人工都更自然）
    for subparser in sub.choices.values():
        subparser.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS,
            help="输出机器可读 JSON（也可写在子命令前）",
        )

    return parser


def _uninstall(root: Path, purge: bool, yes: bool) -> int:
    code_items = ["memory_core", "prompts", "templates", "eval", "examples", "run.py", "memory.config.json", "sensitive.json"]
    data_items = ["data", "traces", "reports", "DEPLOY-REPORT.md"]
    existing_code = [root / item for item in code_items if (root / item).exists()]
    existing_data = [root / item for item in data_items if (root / item).exists()]
    print(f"部署根：{root}")
    print("将删除（代码/配置）：")
    for path in existing_code:
        print(f"  - {path}")
    if purge:
        print("将删除（数据，不可恢复）：")
        for path in existing_data:
            print(f"  - {path}")
        if not yes:
            if sys.stdin.isatty():
                try:
                    answer = input("确认删除全部数据？输入 DELETE：").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n未收到确认，已取消。")
                    return 2
                if answer != "DELETE":
                    print("已取消。")
                    return 2
            else:
                print("非交互环境需要同时传 --yes 才能 --purge。已取消。")
                return 2
    else:
        print("保留数据：")
        for path in existing_data:
            print(f"  - {path}")

    failures = []
    for path in existing_code + (existing_data if purge else []):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    if failures:
        print("以下路径未能删除（可能正被进程占用）：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("卸载完成。" + ("数据已删除。" if purge else "数据目录仍在，可手动备份或删除。"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = _root(args)

    if args.command == "selfcheck":
        from .config import load_config
        from .selfcheck import run_selfcheck

        report = run_selfcheck(load_config(root), root=root)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            for check in report["checks"]:
                mark = "PASS" if check["ok"] else "FAIL"
                print(f"[{mark}] {check['name']:<22} {check['detail']}")
            print(
                f"\n结果：{report['passed']}/{report['total']} 通过，"
                f"耗时 {report['duration_ms']}ms，eval Top3={report['eval'].get('hit_rate')}"
            )
        return 0 if report["ok"] else 1

    if args.command == "uninstall":
        return _uninstall(root, purge=args.purge, yes=args.yes)

    from .api import MemorySystem
    from .tools import dispatch

    system = MemorySystem.open(root)
    try:
        scope = _scope(args) if hasattr(args, "folder") else None

        if args.command == "init":
            health = system.health()
            _emit(health, args.json, f"已初始化：{health['db_path']}（schema v{health['schema_version']}, fts={health['fts_tokenizer']}）")
            return 0 if health["ok"] else 1

        if args.command == "health":
            health = system.health()
            _emit(health, args.json, json.dumps(health, ensure_ascii=False, indent=2))
            return 0 if health["ok"] else 1

        if args.command == "stats":
            stats = system.stats()
            _emit(stats, args.json, json.dumps(stats, ensure_ascii=False, indent=2))
            return 0

        if args.command == "create":
            result = system.create(
                scope,
                title=args.title,
                content=args.content,
                memory_type=args.type,
                bucket=args.bucket,
                triple_tags=args.triple_tags,
                tags=[t for t in (args.tags or "").split(",") if t.strip()],
                evidence=args.evidence,
                source_text=args.source_text,
                knowledge_type=args.knowledge_type,
                procedure_type=args.procedure_type,
            )
            _emit(result, args.json, json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 2

        if args.command == "search":
            results = system.search(
                scope, args.query, top_k=args.top_k,
                include_archived=args.include_archived,
                include_knowledge=args.include_knowledge,
            )
            human = "\n".join(
                f"[{r['id']}] {r['score']:.3f} ({r['type']}) {r['title']}：{r['summary']}" for r in results
            ) or "没有找到相关记忆。"
            _emit(results, args.json, human)
            return 0

        if args.command == "read":
            record = system.read(scope, args.memory_id)
            _emit(record, args.json, json.dumps(record, ensure_ascii=False, indent=2))
            return 0 if record else 2

        if args.command == "update":
            result = system.update(
                scope, args.memory_id, title=args.title, content=args.content,
                triple_tags=args.triple_tags,
                tags=[t for t in (args.tags or "").split(",") if t.strip()] or None,
                status=args.status,
            )
            _emit(result, args.json, json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 2

        if args.command == "delete":
            result = system.delete(scope, args.memory_id)
            _emit(result, args.json, json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 2

        if args.command == "prefetch":
            block = system.prefetch(scope, args.query)
            _emit(block, args.json, block["text"] or f"（未注入，status={block['status']}）")
            return 0

        if args.command == "reminder":
            block = system.reminder(scope)
            _emit(block, args.json, block["text"] or "（无提醒内容）")
            return 0

        if args.command == "extract":
            result = system.extract_after_turn(scope, args.user_text, message_id=args.message_id)
            _emit(result, args.json, json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "candidates":
            if args.action == "list":
                status = None if args.status == "all" else args.status
                rows = system.candidates(scope, status=status, limit=args.limit)
                human = "\n".join(
                    f"[{r['candidate_id']}] {r['memory_type']} {r['title']} "
                    f"warn={r['warn_flags']} block={r['block_reason'] or '-'}"
                    for r in rows
                ) or "没有待审批候选。"
                _emit(rows, args.json, human)
                return 0
            if not args.candidate_id:
                print("approve/reject 需要 candidate_id", file=sys.stderr)
                return 2
            if args.action == "approve":
                result = system.approve_candidate(scope, args.candidate_id)
            else:
                result = system.reject_candidate(scope, args.candidate_id, note=args.note)
            _emit(result, args.json, json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 2

        if args.command == "lifecycle":
            from .models import Scope

            sc = None if not args.folder else Scope(folder_id=args.folder)
            report = system.run_lifecycle(
                scope=sc,
                dry_run=False if args.apply else True,
                allow_hard_delete=True if args.allow_hard_delete else None,
            )
            _emit(report, args.json, json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        if args.command == "reindex":
            report = system.reindex(force=args.force)
            _emit(report, args.json, json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1

        if args.command == "memo":
            if args.action == "set":
                result = system.memo_set(scope, args.content, expire_hours=args.expire_hours)
            elif args.action == "list":
                result = {"ok": True, "memos": system.memo_list(scope)}
            else:
                result = {"ok": True, "memos": system.memo_consume(scope)}
            _emit(result, args.json, json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "eval":
            report = system.evaluate(args.dataset, top_k=args.top_k)
            _emit(report, args.json, json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1

        if args.command == "tool":
            try:
                if args.args_file:
                    payload = json.loads(Path(args.args_file).read_text(encoding="utf-8"))
                else:
                    payload = json.loads(args.args)
            except (json.JSONDecodeError, OSError) as exc:
                print(f"--args/--args-file 不是合法 JSON：{exc}", file=sys.stderr)
                return 2
            result = dispatch(system, scope, args.name, payload)
            _emit(result, args.json, result["text"])
            return 0 if result.get("ok") else 2

        parser.error("未知命令")
    finally:
        system.close()
    return 0
