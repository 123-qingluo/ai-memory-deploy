"""领域模型：作用域、枚举、记录与候选。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

MEMORY_TYPES = ("person", "event", "knowledge", "procedure")
BUCKETS = ("memory", "knowledge")
STATUSES = ("active", "archived", "expired")
SOURCES = ("tool", "auto_extract", "event_extract", "ingest")
TAG_TYPES = ("tag", "core", "diffusion", "opposing")


def now_ts() -> float:
    return time.time()


@dataclass(frozen=True)
class Scope:
    """一次调用的作用域。folder_id 是数据级隔离的硬边界。"""

    folder_id: str = "default"
    person_id: str = ""
    chat_type: str = ""  # private | group | "" (未知)

    def __post_init__(self):
        if not str(self.folder_id or "").strip():
            raise ValueError("scope.folder_id 不能为空；数据级隔离依赖它")

    def to_dict(self) -> dict:
        return {"folder_id": self.folder_id, "person_id": self.person_id, "chat_type": self.chat_type}


def _loads(value: Any, default):
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


@dataclass
class Record:
    memory_id: str
    folder_id: str
    bucket: str
    source: str
    memory_type: str
    status: str
    title: str
    content: str
    evidence: str = ""
    evidence_verified: int = 0
    person_id: str = ""
    relation_memory_ids: list = field(default_factory=list)
    relation_aliases: list = field(default_factory=list)
    event_start_at: float = 0.0
    event_end_at: float = 0.0
    related_people: list = field(default_factory=list)
    knowledge_type: str = ""
    procedure_type: str = ""
    address_or_coord: str = ""   # 预留，不启用
    place_type: str = ""         # 预留，不启用
    asset_type: str = ""         # 预留，不启用
    disposition_status: str = "" # 预留，不启用
    novelty_energy: float = 1.0
    is_archived: int = 0
    is_deleted: int = 0
    deleted_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_activated_at: float = 0.0
    activation_count: int = 0
    search_hits: int = 0
    flashback_hits: int = 0
    tags: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row) -> "Record":
        data = dict(row)
        data.pop("vec", None)
        data.pop("dim", None)
        data.pop("model", None)
        for key in ("relation_memory_ids", "relation_aliases", "related_people"):
            data[key] = _loads(data.get(key), [])
        return cls(**data)

    def summary(self, limit: int = 160) -> str:
        text = (self.content or "").strip().replace("\n", " ")
        return text if len(text) <= limit else text[:limit] + "…"

    def as_dict(self, include_content: bool = True) -> dict:
        out = {
            "id": self.memory_id,
            "folder_id": self.folder_id,
            "bucket": self.bucket,
            "source": self.source,
            "type": self.memory_type,
            "status": self.status,
            "title": self.title,
            "summary": self.summary(160),
            "evidence": self.evidence,
            "evidence_verified": bool(self.evidence_verified),
            "person_id": self.person_id,
            "event_start_at": self.event_start_at,
            "event_end_at": self.event_end_at,
            "related_people": self.related_people,
            "knowledge_type": self.knowledge_type,
            "procedure_type": self.procedure_type,
            "tags": self.tags,
            "activation_count": self.activation_count,
            "search_hits": self.search_hits,
            "flashback_hits": self.flashback_hits,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activated_at": self.last_activated_at,
        }
        if include_content:
            out["content"] = self.content
        return out


def empty_tags() -> dict[str, list[str]]:
    return {"tag": [], "core": [], "diffusion": [], "opposing": []}
