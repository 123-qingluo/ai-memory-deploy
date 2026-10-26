"""模型侧工具：memory_command（长期记忆）与 temporary_memo（短期便签）。

手册里必须写死：临时便签不是长期记忆；检索没有结果就说不记得。
"""
from __future__ import annotations

import shlex

from .models import Scope

REASON_TEXT = {
    "duplicate": "库内已有高度相似的记忆，未重复写入",
    "sensitive": "命中敏感信息规则，拒绝写入",
    "evidence_mismatch": "evidence 不是用户原话子串，按规则丢弃",
    "invalid_tags": "三元标签格式不对：需要 core|diffusion|opposing 三组",
    "invalid_type": "memory_type 只能是 person/event/knowledge/procedure",
    "missing_person_id": "person 类型必须提供 person_id",
    "empty_content": "标题或正文为空",
    "not_found": "该 folder 下没有这条记忆",
    "invalid_bucket": "bucket 只能是 memory/knowledge",
    "invalid_status": "status 只能是 active/archived/expired",
    "embedding_error": "嵌入服务不可用，写入被拒绝（宁可漏记也不写脏数据）",
}

BOOL_FLAGS = {"include_archived", "include_knowledge"}
VALUE_FLAGS = {
    "title", "content", "type", "triple_tags", "tags", "evidence", "source_text", "person",
    "bucket", "knowledge_type", "procedure_type", "top_k", "status",
}


def _parse(segment: str) -> tuple[str, list[str], dict[str, str], str]:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError as exc:
        return "", [], {}, f"命令无法解析：{exc}"
    if not tokens:
        return "", [], {}, "空命令"
    op = tokens[0].lower()
    rest = tokens[1:]
    flags: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        key = token.lstrip("-").lower() if token.startswith("-") else ""
        if key in BOOL_FLAGS:
            flags[key] = "true"
            i += 1
        elif key in VALUE_FLAGS and i + 1 < len(rest):
            flags[key] = rest[i + 1]
            i += 2
        elif token.startswith("-") and key in VALUE_FLAGS:
            return "", [], {}, f"参数 -{key} 缺值"
        else:
            positional.append(token)
            i += 1
    return op, positional, flags, ""


def _reject_text(result: dict) -> str:
    reason = result.get("rejected", "error")
    detail = result.get("detail", "")
    text = REASON_TEXT.get(reason, reason)
    return f"未写入：{text}" + (f"（{detail}）" if detail else "")


def _fmt_search(results: list[dict]) -> str:
    if not results:
        return "没有找到相关记忆。不要编造；可以换关键词再搜一次。"
    lines = ["检索结果（只是候选，需要精确内容用 read <id>）："]
    for item in results:
        lines.append(f"- [{item['id']}] ({item['type']}) {item['title']}：{item['summary']}  score={item['score']}")
    return "\n".join(lines)


def _fmt_record(record: dict | None) -> str:
    if not record:
        return "没有找到这条记忆。"
    tags = record.get("tags", {})
    lines = [
        f"[{record['id']}] {record['title']}",
        f"类型：{record['type']} / bucket：{record['bucket']} / 状态：{record['status']}",
        f"正文：{record['content']}",
        f"evidence：{record.get('evidence') or '（无）'}（verified={record.get('evidence_verified')}）",
        f"标签：core={tags.get('core', [])} diffusion={tags.get('diffusion', [])} opposing={tags.get('opposing', [])}",
    ]
    return "\n".join(lines)


def dispatch(system, scope: Scope, name: str, arguments: dict | None) -> dict:
    args = arguments or {}
    if name == "memory_command":
        return _memory_command(system, scope, str(args.get("command", "")))
    if name == "temporary_memo":
        return _temporary_memo(system, scope, args)
    return {"ok": False, "text": f"未知工具：{name}（只支持 memory_command / temporary_memo）"}


def _memory_command(system, scope: Scope, command: str) -> dict:
    segments = [seg.strip() for seg in (command or "").split("&&") if seg.strip()]
    if not segments:
        return {"ok": False, "text": "命令为空。示例：search 香菜；create -title … -content … -triple_tags core|diffusion|opposing"}
    outputs: list[str] = []
    ok_all = True
    for segment in segments:
        op, positional, flags, error = _parse(segment)
        if error:
            outputs.append(error)
            ok_all = False
            continue
        if op == "search":
            query = " ".join(positional).strip()
            if not query:
                outputs.append("search 需要一个查询词")
                ok_all = False
                continue
            results = system.search(
                scope, query,
                top_k=int(flags.get("top_k", 0) or 0) or None,
                include_archived=flags.get("include_archived") == "true",
                include_knowledge=flags.get("include_knowledge") == "true",
            )
            system.note_tool_used(scope)
            outputs.append(_fmt_search(results))
        elif op == "read":
            if not positional:
                outputs.append("read 需要一个 memory id")
                ok_all = False
                continue
            record = system.read(scope, positional[0])
            system.note_tool_used(scope)
            outputs.append(_fmt_record(record))
        elif op == "create":
            result = system.create(
                scope,
                title=flags.get("title", ""),
                content=flags.get("content", ""),
                memory_type=flags.get("type", "knowledge"),
                bucket=flags.get("bucket", "memory"),
                triple_tags=flags.get("triple_tags"),
                tags=[t for t in (flags.get("tags", "") or "").split(",") if t.strip()],
                evidence=flags.get("evidence", ""),
                source_text=flags.get("source_text", ""),
                person_id=flags.get("person", "") or scope.person_id,
                knowledge_type=flags.get("knowledge_type", ""),
                procedure_type=flags.get("procedure_type", ""),
            )
            system.note_tool_used(scope)
            if result.get("ok"):
                outputs.append(f"已写入：[{result['memory_id']}]（{result.get('action')}）")
            else:
                ok_all = False
                outputs.append(_reject_text(result))
        elif op == "update":
            if not positional:
                outputs.append("update 需要一个 memory id")
                ok_all = False
                continue
            result = system.update(
                scope, positional[0],
                title=flags.get("title"),
                content=flags.get("content"),
                triple_tags=flags.get("triple_tags"),
                tags=[t for t in (flags.get("tags", "") or "").split(",") if t.strip()] or None,
                status=flags.get("status"),
            )
            system.note_tool_used(scope)
            outputs.append(f"已更新：[{positional[0]}]" if result.get("ok") else _reject_text(result))
            ok_all = ok_all and bool(result.get("ok"))
        elif op == "delete":
            if not positional:
                outputs.append("delete 需要一个 memory id")
                ok_all = False
                continue
            result = system.delete(scope, positional[0])
            system.note_tool_used(scope)
            outputs.append(f"已软删：[{positional[0]}]" if result.get("ok") else _reject_text(result))
            ok_all = ok_all and bool(result.get("ok"))
        else:
            outputs.append(f"未知子命令：{op}（支持 search/read/create/update/delete）")
            ok_all = False
    return {"ok": ok_all, "text": "\n\n".join(outputs)}


def _temporary_memo(system, scope: Scope, args: dict) -> dict:
    action = str(args.get("action", "set")).lower()
    if action == "set":
        content = str(args.get("content", "")).strip()
        if not content:
            return {"ok": False, "text": "temporary_memo set 需要 content"}
        result = system.memo_set(scope, content, expire_hours=args.get("expire_hours"))
        return {"ok": True, "text": f"便签已记录（{result['memo_id']}），到期自动失效；它不是长期记忆。"}
    if action == "list":
        rows = system.memo_list(scope)
        if not rows:
            return {"ok": True, "text": "当前没有未消费的临时便签。"}
        return {"ok": True, "text": "\n".join(f"- {r['content']}" for r in rows)}
    if action == "consume":
        rows = system.memo_consume(scope)
        if not rows:
            return {"ok": True, "text": "当前没有未消费的临时便签。"}
        return {"ok": True, "text": "\n".join(f"- {r['content']}" for r in rows)}
    return {"ok": False, "text": "action 只能是 set/list/consume"}
