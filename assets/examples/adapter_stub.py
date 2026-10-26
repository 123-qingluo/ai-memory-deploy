"""宿主适配器示例：三处 hook + 工具分发。

把下面四个函数接进你的框架即可；记忆核心不需要知道你用的是 QQ、Discord 还是桌面 Agent。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_core import MemorySystem, Scope  # noqa: E402
from memory_core.tools import dispatch  # noqa: E402

SYSTEM = MemorySystem.open(Path(__file__).resolve().parent.parent)


def scope_for(chat_id: str, user_id: str, chat_type: str) -> Scope:
    """folder 按通道隔离：群聊的记忆不会在私聊被检索到，反之亦然。"""
    return Scope(folder_id=f"{chat_type}:{chat_id}", person_id=f"user:{user_id}", chat_type=chat_type)


def before_prompt(chat_id: str, user_id: str, chat_type: str, last_user_text: str) -> str:
    """每轮构建 prompt 时调用；返回值放在 prompt 靠后位置。"""
    scope = scope_for(chat_id, user_id, chat_type)
    reminder = SYSTEM.reminder(scope)          # 标题/便签/工具监督
    prefetch = SYSTEM.prefetch(scope, last_user_text)  # 3 条 × 160 字
    parts = [reminder["text"], prefetch["text"]]
    return "\n\n".join(part for part in parts if part)


def after_turn(chat_id: str, user_id: str, chat_type: str, user_text: str, message_id: str) -> dict:
    """一轮结束后调用；默认 shadow，只产候选，审批后才入库。"""
    scope = scope_for(chat_id, user_id, chat_type)
    return SYSTEM.extract_after_turn(scope, user_text, message_id=message_id)


def on_tool_call(chat_id: str, user_id: str, chat_type: str, name: str, arguments: dict) -> str:
    """把模型工具调用转给记忆系统；工具定义见 templates/tool_schema.json。"""
    scope = scope_for(chat_id, user_id, chat_type)
    return dispatch(SYSTEM, scope, name, arguments)["text"]


def daily_lifecycle() -> dict:
    """每天低峰期跑一次；默认 dry-run，先看 reports/ 里的报告。"""
    return SYSTEM.run_lifecycle(dry_run=True)
