"""三元标签：core / diffusion / opposing。

写入时强制三组齐全（允许用 `-` 显式声明某组为空），避免只剩 core 导致
检索只能"像不像"、无法表达"该不该避开"。
"""
from __future__ import annotations

import json
from typing import Iterable

from .text import lexical_overlap, normalize_text


class TagError(ValueError):
    pass


def _split_group(group: str) -> list[str]:
    group = (group or "").strip()
    if group in ("", "-", "—"):
        return []
    items = [x.strip() for x in group.replace("，", ",").split(",")]
    return [x for x in items if x]


def parse_triple_tags(spec: str | None) -> dict[str, list[str]]:
    """解析 `核心1,核心2|扩散1,扩散2|对立1,对立2`；也接受 JSON 对象。"""
    if spec is None:
        raise TagError("缺少三元标签：需要 core|diffusion|opposing 三组（可用 - 表示空组）")
    raw = spec.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TagError(f"三元标签 JSON 无法解析: {exc}") from exc
        groups = {
            "core": list(data.get("core", [])),
            "diffusion": list(data.get("diffusion", [])),
            "opposing": list(data.get("opposing", [])),
        }
    else:
        parts = raw.split("|")
        if len(parts) != 3:
            raise TagError(
                "三元标签必须恰好三组，用 | 分隔：core|diffusion|opposing（空组写 -）"
            )
        groups = {
            "core": _split_group(parts[0]),
            "diffusion": _split_group(parts[1]),
            "opposing": _split_group(parts[2]),
        }
    if not (groups["core"] or groups["diffusion"] or groups["opposing"]):
        raise TagError("三元标签三组全为空；至少给 core 一个词")
    return groups


def normalize_tags(tags: Iterable[str] | None) -> list[str]:
    if not tags:
        return []
    out: list[str] = []
    for item in tags:
        value = str(item).strip()
        if value and value not in out:
            out.append(value)
    return out


def _edge(value: str, query_norm: str) -> bool:
    v = normalize_text(value)
    if len(v) < 1 or not query_norm:
        return False
    if v in query_norm or (len(query_norm) >= 2 and query_norm in v):
        return True
    return lexical_overlap(v, query_norm) >= 0.5


def tag_boost(query: str, tags: dict[str, list[str]], cfg: dict) -> float:
    """core 加分、diffusion 小幅加分、opposing 硬性减分。"""
    qn = normalize_text(query)
    boost = 0.0
    for value in tags.get("core", []):
        if _edge(value, qn):
            boost += float(cfg.get("tag_core_bonus", 0.12))
    for value in tags.get("diffusion", []):
        if _edge(value, qn):
            boost += float(cfg.get("tag_diffusion_bonus", 0.04))
    for value in tags.get("opposing", []):
        if _edge(value, qn):
            boost -= float(cfg.get("tag_opposing_penalty", 0.25))
    return boost
