"""写入管线：六道闸门、去重、新颖度合并、候选审批。

写入门槛高、读取通道多。这里宁可漏记：一条错记会污染之后所有推理。
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from .models import (
    BUCKETS, MEMORY_TYPES, SOURCES, Record, Scope, empty_tags, now_ts,
)
from .tags import TagError, normalize_tags, parse_triple_tags
from .text import novelty_energy, title_jaccard

DEFAULT_SENSITIVE_PATTERNS = [
    r"验证码",
    r"身份证(号|号码)?",
    r"(银行|信用)卡(号)?",
    r"(密码|口令)(\s*[:：=]\s*|\s*是\s*)\S{2,}",
    r"住址|家庭地址|收货地址",
    r"(?i)(token|api[_-]?key|cookie|secret|password)\s*[:：=]\s*\S+",
    r"私钥|密钥",
]


class Rejected(Exception):
    """写入被闸门拒绝。reason 是机器可读的稳定值。"""

    def __init__(self, reason: str, detail: str = "", existing: str | None = None):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.existing = existing

    def as_dict(self) -> dict:
        out = {"ok": False, "rejected": self.reason, "detail": self.detail}
        if self.existing:
            out["existing_id"] = self.existing
        return out


class SensitiveFilter:
    def __init__(self, path: str | Path | None):
        self.patterns = list(DEFAULT_SENSITIVE_PATTERNS)
        if path and Path(path).exists():
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data = data.get("patterns", [])
                if isinstance(data, list) and data:
                    self.patterns = [str(p) for p in data]
            except (OSError, json.JSONDecodeError):
                pass
        self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    def match(self, text: str) -> str | None:
        for pattern in self._compiled:
            if pattern.search(text or ""):
                return pattern.pattern
        return None


class WriteService:
    def __init__(self, store, embedder, cfg: dict, tracer, sensitive: SensitiveFilter, candidates_path: str | Path):
        self.store = store
        self.embedder = embedder
        self.cfg = cfg
        self.tracer = tracer
        self.sensitive = sensitive
        self.candidates_path = Path(candidates_path)
        self.write_cfg = cfg.get("write", {})

    # ------------------------------------------------------------- helpers
    def _embed(self, title: str, content: str) -> list[float]:
        try:
            vectors = self.embedder.embed([f"{title}\n{content}"])
        except Exception as exc:
            self.tracer.log("write", "embedding_error", error=str(exc)[:200])
            raise Rejected("embedding_error", f"嵌入服务不可用：{str(exc)[:160]}") from exc
        if not vectors or not vectors[0]:
            raise Rejected("embedding_error", "嵌入服务返回空向量")
        return vectors[0]

    def _validate(self, memory_type: str, bucket: str, source: str, title: str, content: str, person_id: str) -> None:
        if memory_type not in MEMORY_TYPES:
            raise Rejected("invalid_type", f"type 必须是 {MEMORY_TYPES}")
        if bucket not in BUCKETS:
            raise Rejected("invalid_bucket", f"bucket 必须是 {BUCKETS}")
        if source not in SOURCES:
            raise Rejected("invalid_source", f"source 必须是 {SOURCES}")
        if not (title or "").strip():
            raise Rejected("empty_title", "title 不能为空")
        if not (content or "").strip():
            raise Rejected("empty_content", "content 不能为空")
        if memory_type == "person" and not (person_id or "").strip():
            raise Rejected("missing_person_id", "person 类型必须提供 person_id（platform:id）")

    def _check_sensitive(self, *texts: str) -> None:
        for text in texts:
            hit = self.sensitive.match(text)
            if hit:
                raise Rejected("sensitive", f"命中敏感规则：{hit}")

    def _check_sensitive_safe(self, text: str) -> bool:
        """只回答"是否命中"，供抽取器把候选标成 blocked 而不是整轮丢弃。"""
        return self.sensitive.match(text) is not None

    def _similar(self, vec: list[float], rows: list[dict]) -> list[tuple[float, dict]]:
        from .text import cosine

        scored = []
        for row in rows:
            other = row.get("_vec") or []
            if not other:
                continue
            scored.append((cosine(vec, other), row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def _combine_content(self, old: str, new: str, max_chars: int) -> str:
        if new.strip() and new.strip() in old:
            return old
        combined = old.rstrip() + "\n" + new.strip()
        if len(combined) <= max_chars:
            return combined
        half = max(1, max_chars // 2)
        return combined[:half].rstrip() + "……" + combined[-half:].lstrip()

    def _merge_tags(self, old: dict[str, list[str]], new: dict[str, list[str]]) -> dict[str, list[str]]:
        out = empty_tags()
        for tag_type in out:
            values = []
            for value in list(old.get(tag_type, [])) + list(new.get(tag_type, [])):
                value = str(value).strip()
                if value and value not in values:
                    values.append(value)
            out[tag_type] = values
        return out

    # ------------------------------------------------------------- create
    def create(
        self,
        scope: Scope,
        *,
        title: str,
        content: str,
        memory_type: str = "knowledge",
        bucket: str = "memory",
        source: str = "tool",
        tags: list[str] | None = None,
        triple_tags: str | dict | None = None,
        evidence: str = "",
        source_text: str = "",
        person_id: str = "",
        knowledge_type: str = "",
        procedure_type: str = "",
        event_start_at: float = 0.0,
        event_end_at: float = 0.0,
        related_people: list[str] | None = None,
        allow_merge: bool = True,
    ) -> dict:
        now = now_ts()
        self._validate(memory_type, bucket, source, title, content, person_id)
        self._check_sensitive(title, content, evidence)

        tag_groups = empty_tags()
        if triple_tags is not None:
            try:
                parsed = parse_triple_tags(triple_tags if isinstance(triple_tags, str) else json.dumps(triple_tags, ensure_ascii=False))
            except TagError as exc:
                raise Rejected("invalid_tags", str(exc)) from exc
            tag_groups.update(parsed)
        if tags:
            tag_groups["tag"] = normalize_tags(tags)

        verified = 0
        if evidence:
            if source_text:
                if evidence.strip() not in source_text:
                    raise Rejected(
                        "evidence_mismatch",
                        "evidence 不是 source_text 的原子串：宁可丢弃，不要编造依据",
                    )
                verified = 1

        vec = self._embed(title, content)
        rows = [
            row
            for row in self.store.candidate_rows(scope.folder_id, include_knowledge=True)
            if row["type"] == memory_type and row["bucket"] == bucket
        ]
        scored = self._similar(vec, rows)

        dedupe_at = float(self.write_cfg.get("dedupe_similarity", 0.90))
        if scored:
            best_cos, best = scored[0]
            same_title = title_jaccard(title, best["title"]) >= 0.80
            if best_cos >= dedupe_at or (best_cos >= dedupe_at - 0.05 and same_title):
                self.tracer.log(
                    "write", "rejected", reason="duplicate", existing=best["id"],
                    score=round(best_cos, 4),
                )
                raise Rejected("duplicate", "与库内已有记忆高度相似", existing=best["id"])

        if allow_merge and scored:
            best_cos, best = scored[0]
            floor = float(self.write_cfg.get("merge_similarity_floor", 0.50))
            cutoff = float(self.write_cfg.get("novelty_merge_cutoff", 0.10))
            if best_cos >= floor:
                basis = [row.get("_vec") or [] for _, row in scored[:8]]
                energy = novelty_energy(vec, [b for b in basis if b])
                if energy < cutoff:
                    merged_content = self._combine_content(
                        best["content"], content, int(self.write_cfg.get("merge_max_chars", 800))
                    )
                    merged_tags = self._merge_tags(best.get("tags", empty_tags()), tag_groups)
                    new_vec = self._embed(best["title"], merged_content)
                    self.store.update_record(
                        best["id"], scope.folder_id,
                        {"content": merged_content, "updated_at": now,
                         "novelty_energy": min(float(best.get("novelty_energy", 1.0)), energy)},
                        tags=merged_tags, vec=new_vec, model=self.embedder.model,
                    )
                    self.tracer.log(
                        "write", "merged", memory_id=best["id"],
                        novelty=round(energy, 4), score=round(best_cos, 4),
                    )
                    return {
                        "ok": True, "action": "merged", "memory_id": best["id"],
                        "novelty_energy": round(energy, 4),
                    }

        memory_id = "mem-" + uuid.uuid4().hex[:12]
        rec = Record(
            memory_id=memory_id,
            folder_id=scope.folder_id,
            bucket=bucket,
            source=source,
            memory_type=memory_type,
            status="active",
            title=title.strip()[:120],
            content=content.strip(),
            evidence=evidence.strip(),
            evidence_verified=verified,
            person_id=(person_id or scope.person_id or "").strip(),
            knowledge_type=knowledge_type,
            procedure_type=procedure_type,
            event_start_at=float(event_start_at or 0),
            event_end_at=float(event_end_at or 0),
            related_people=related_people or [],
            novelty_energy=1.0,
            created_at=now,
            updated_at=now,
        )
        rec.tags = tag_groups
        self.store.insert_record(rec, tag_groups, vec, self.embedder.model)
        self.tracer.log(
            "write", "created", memory_id=memory_id, type=memory_type,
            bucket=bucket, source=source, evidence_verified=verified,
        )
        return {"ok": True, "action": "created", "memory_id": memory_id, "id": memory_id}

    # ------------------------------------------------------------- update / delete
    def update(
        self,
        scope: Scope,
        memory_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        triple_tags: str | dict | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> dict:
        rec = self.store.get(memory_id, scope.folder_id)
        if rec is None:
            raise Rejected("not_found", "该 folder 下没有这条记忆")
        new_title = rec.title if title is None else title.strip()
        new_content = rec.content if content is None else content.strip()
        if not new_title or not new_content:
            raise Rejected("empty_content", "title/content 不能为空")
        self._check_sensitive(new_title, new_content)

        fields: dict[str, Any] = {"title": new_title, "content": new_content, "updated_at": now_ts()}
        tag_groups = None
        if triple_tags is not None:
            try:
                parsed = parse_triple_tags(triple_tags if isinstance(triple_tags, str) else json.dumps(triple_tags, ensure_ascii=False))
            except TagError as exc:
                raise Rejected("invalid_tags", str(exc)) from exc
            # update 的语义是"整组替换"，普通 tag 组保留
            tag_groups = {
                "tag": list(rec.tags.get("tag", [])),
                "core": parsed["core"],
                "diffusion": parsed["diffusion"],
                "opposing": parsed["opposing"],
            }
        elif tags is not None:
            tag_groups = self._merge_tags(rec.tags, {"tag": normalize_tags(tags), "core": [], "diffusion": [], "opposing": []})
        if status:
            if status not in ("active", "archived", "expired"):
                raise Rejected("invalid_status", "status 只能是 active/archived/expired")
            fields["status"] = status
            fields["is_archived"] = 1 if status == "archived" else 0

        vec = None
        if new_title != rec.title or new_content != rec.content:
            vec = self._embed(new_title, new_content)
        self.store.update_record(
            memory_id, scope.folder_id, fields, tags=tag_groups, vec=vec, model=self.embedder.model
        )
        self.tracer.log("write", "updated", memory_id=memory_id)
        return {"ok": True, "action": "updated", "memory_id": memory_id}

    def delete(self, scope: Scope, memory_id: str) -> dict:
        ok = self.store.soft_delete(memory_id, scope.folder_id, now_ts())
        if not ok:
            raise Rejected("not_found", "该 folder 下没有可删除的活跃记忆")
        self.tracer.log("write", "soft_deleted", memory_id=memory_id)
        return {"ok": True, "action": "soft_deleted", "memory_id": memory_id}

    # ------------------------------------------------------------- candidates
    def stage_candidate(
        self,
        scope: Scope,
        *,
        title: str,
        content: str,
        memory_type: str,
        source_text: str,
        evidence: str,
        source: str,
        block_reason: str = "",
        warn_flags: list[str] | None = None,
        payload: dict | None = None,
    ) -> dict:
        candidate_id = "cand-" + uuid.uuid4().hex[:12]
        now = now_ts()
        status = "blocked" if block_reason else "pending"
        data = {
            "candidate_id": candidate_id,
            "folder_id": scope.folder_id,
            "chat_type": scope.chat_type,
            "person_id": scope.person_id,
            "source": source,
            "memory_type": memory_type,
            "title": title,
            "content": content,
            "evidence": evidence,
            "source_text": source_text,
            "payload": payload or {},
            "status": status,
            "block_reason": block_reason,
            "warn_flags": warn_flags or [],
            "created_at": now,
        }
        self.store.audit_insert(data)
        self.candidates_path.parent.mkdir(parents=True, exist_ok=True)
        with self.candidates_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.tracer.log(
            "extract", status, candidate_id=candidate_id, title=title[:30],
            type=memory_type, block_reason=block_reason,
        )
        return {"candidate_id": candidate_id, "status": status, "block_reason": block_reason}

    def approve(self, scope: Scope, candidate_id: str) -> dict:
        audit = self.store.audit_get(candidate_id)
        if audit is None or audit["folder_id"] != scope.folder_id:
            raise Rejected("not_found", "候选不存在或不属于该 folder")
        if audit["status"] != "pending":
            raise Rejected("not_pending", f"候选状态为 {audit['status']}，不能审批")
        if audit["evidence"] and audit["evidence"] not in audit["source_text"]:
            self.store.audit_decide(candidate_id, "blocked", now_ts(), reason="evidence_mismatch")
            raise Rejected("evidence_mismatch", "evidence 不再存在于原话中")
        payload = json.loads(audit["payload"] or "{}")
        try:
            result = self.create(
                scope,
                title=audit["title"],
                content=audit["content"],
                memory_type=audit["memory_type"],
                bucket=payload.get("bucket", "memory"),
                source=audit["source"],
                tags=payload.get("tags", []),
                triple_tags=payload.get("triple_tags"),
                evidence=audit["evidence"],
                source_text=audit["source_text"],
                person_id=audit["person_id"],
                knowledge_type=payload.get("knowledge_type", ""),
                procedure_type=payload.get("procedure_type", ""),
                event_start_at=payload.get("event_start_at", 0),
                event_end_at=payload.get("event_end_at", 0),
                related_people=payload.get("related_people", []),
            )
        except Rejected as exc:
            self.store.audit_decide(candidate_id, "blocked", now_ts(), reason=exc.reason)
            raise
        self.store.audit_decide(candidate_id, "approved", now_ts(), result=result)
        return {"ok": True, "candidate_id": candidate_id, **result}

    def reject(self, scope: Scope, candidate_id: str, note: str = "") -> dict:
        audit = self.store.audit_get(candidate_id)
        if audit is None or audit["folder_id"] != scope.folder_id:
            raise Rejected("not_found", "候选不存在或不属于该 folder")
        if audit["status"] != "pending":
            raise Rejected("not_pending", f"候选状态为 {audit['status']}，不能驳回")
        self.store.audit_decide(candidate_id, "rejected", now_ts(), note=note)
        return {"ok": True, "candidate_id": candidate_id, "status": "rejected"}
