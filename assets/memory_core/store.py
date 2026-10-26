"""存储层：记录、标签、向量、FTS、候选审计、便签、运行状态。

所有读写都显式带 folder_id；没有"全局默认作用域"这种隐式行为。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

from .db import Database
from .models import Record, empty_tags
from .text import blob_to_vec, vec_to_blob

_JSON_COLUMNS = ("relation_memory_ids", "relation_aliases", "related_people")


class Store:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- records
    def insert_record(self, rec: Record, tags: dict[str, list[str]], vec: list[float], model: str) -> None:
        columns = (
            "memory_id", "folder_id", "bucket", "source", "memory_type", "status",
            "title", "content", "evidence", "evidence_verified", "person_id",
            "relation_memory_ids", "relation_aliases", "event_start_at", "event_end_at",
            "related_people", "knowledge_type", "procedure_type", "address_or_coord",
            "place_type", "asset_type", "disposition_status", "novelty_energy",
            "is_archived", "is_deleted", "deleted_at", "created_at", "updated_at",
            "last_activated_at", "activation_count", "search_hits", "flashback_hits",
        )
        values = []
        for col in columns:
            value = getattr(rec, col)
            if col in _JSON_COLUMNS:
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
        placeholders = ", ".join("?" for _ in columns)
        with self.db.tx() as conn:
            conn.execute(
                f"INSERT INTO memory_records ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values),
            )
            self._replace_tags(conn, rec.memory_id, tags)
            conn.execute(
                "INSERT INTO memory_fts(memory_id, title, content) VALUES (?, ?, ?)",
                (rec.memory_id, rec.title, rec.content),
            )
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings(memory_id, model, dim, vec) VALUES (?, ?, ?, ?)",
                (rec.memory_id, model, len(vec), vec_to_blob(vec)),
            )

    def _replace_tags(self, conn, memory_id: str, tags: dict[str, list[str]]) -> None:
        conn.execute("DELETE FROM memory_tags WHERE memory_id = ?", (memory_id,))
        rows = [
            (memory_id, tag_type, value)
            for tag_type, values in tags.items()
            for value in values
        ]
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO memory_tags(memory_id, tag_type, tag_value) VALUES (?, ?, ?)",
                rows,
            )

    def update_record(
        self,
        memory_id: str,
        folder_id: str,
        fields: dict[str, Any],
        tags: dict[str, list[str]] | None = None,
        vec: list[float] | None = None,
        model: str = "",
    ) -> bool:
        allowed = {
            "title", "content", "memory_type", "bucket", "status", "is_archived",
            "is_deleted", "deleted_at", "updated_at", "person_id", "knowledge_type",
            "procedure_type", "event_start_at", "event_end_at", "novelty_energy",
            "relation_memory_ids", "relation_aliases", "related_people", "evidence",
            "evidence_verified",
        }
        cleaned = {k: v for k, v in fields.items() if k in allowed}
        for key in _JSON_COLUMNS:
            if key in cleaned and not isinstance(cleaned[key], str):
                cleaned[key] = json.dumps(cleaned[key], ensure_ascii=False)
        if not cleaned and tags is None and vec is None:
            return False
        with self.db.tx() as conn:
            if cleaned:
                sets = ", ".join(f"{k} = ?" for k in cleaned)
                conn.execute(
                    f"UPDATE memory_records SET {sets} WHERE memory_id = ? AND folder_id = ?",
                    (*cleaned.values(), memory_id, folder_id),
                )
            if tags is not None:
                self._replace_tags(conn, memory_id, tags)
            if vec is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings(memory_id, model, dim, vec) VALUES (?, ?, ?, ?)",
                    (memory_id, model or "unknown", len(vec), vec_to_blob(vec)),
                )
            row = conn.execute(
                "SELECT title, content FROM memory_records WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE memory_fts SET title = ?, content = ? WHERE memory_id = ?",
                    (row["title"], row["content"], memory_id),
                )
                if conn.execute("SELECT changes() AS c").fetchone()["c"] == 0:
                    conn.execute(
                        "INSERT INTO memory_fts(memory_id, title, content) VALUES (?, ?, ?)",
                        (memory_id, row["title"], row["content"]),
                    )
        return True

    def soft_delete(self, memory_id: str, folder_id: str, now: float) -> bool:
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE memory_records SET is_deleted = 1, status = 'expired', deleted_at = ?, updated_at = ? "
                "WHERE memory_id = ? AND folder_id = ? AND is_deleted = 0",
                (now, now, memory_id, folder_id),
            )
            if cur.rowcount == 0:
                return False
            conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        return True

    def hard_delete(self, memory_id: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_tags WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_records WHERE memory_id = ?", (memory_id,))

    def get(self, memory_id: str, folder_id: str | None = None) -> Record | None:
        if folder_id is None:
            row = self.db.fetchone("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,))
        else:
            row = self.db.fetchone(
                "SELECT * FROM memory_records WHERE memory_id = ? AND folder_id = ?",
                (memory_id, folder_id),
            )
        if row is None:
            return None
        rec = Record.from_row(row)
        rec.tags = self.tags_for([memory_id]).get(memory_id, empty_tags())
        return rec

    def get_many(self, ids: Iterable[str]) -> dict[str, Record]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        out: dict[str, Record] = {}
        for chunk in _chunks(ids, 400):
            marks = ", ".join("?" for _ in chunk)
            rows = self.db.fetchall(
                f"SELECT * FROM memory_records WHERE memory_id IN ({marks})", tuple(chunk)
            )
            for row in rows:
                out[row["memory_id"]] = Record.from_row(row)
        tags = self.tags_for(ids)
        for mid, rec in out.items():
            rec.tags = tags.get(mid, empty_tags())
        return out

    def tags_for(self, ids: Iterable[str]) -> dict[str, dict[str, list[str]]]:
        ids = list(dict.fromkeys(ids))
        out: dict[str, dict[str, list[str]]] = {}
        for chunk in _chunks(ids, 400):
            marks = ", ".join("?" for _ in chunk)
            rows = self.db.fetchall(
                f"SELECT memory_id, tag_type, tag_value FROM memory_tags WHERE memory_id IN ({marks})",
                tuple(chunk),
            )
            for row in rows:
                out.setdefault(row["memory_id"], empty_tags())[row["tag_type"]].append(row["tag_value"])
        return out

    def candidate_rows(
        self,
        folder_id: str,
        include_knowledge: bool = False,
        include_archived: bool = False,
    ) -> list[dict]:
        buckets = ["memory"] + (["knowledge"] if include_knowledge else [])
        statuses = ["active"] + (["archived"] if include_archived else [])
        sql = (
            "SELECT r.*, e.vec AS vec, e.model AS vec_model FROM memory_records r "
            "LEFT JOIN memory_embeddings e ON e.memory_id = r.memory_id "
            "WHERE r.folder_id = ? AND r.is_deleted = 0 "
            f"AND r.bucket IN ({', '.join('?' for _ in buckets)}) "
            f"AND r.status IN ({', '.join('?' for _ in statuses)})"
        )
        rows = self.db.fetchall(sql, (folder_id, *buckets, *statuses))
        ids = [r["memory_id"] for r in rows]
        tags = self.tags_for(ids)
        out = []
        for row in rows:
            data = dict(row)
            vec_blob = data.pop("vec", None)
            data.pop("vec_model", None)
            rec = Record.from_row(data)
            rec.tags = tags.get(rec.memory_id, empty_tags())
            item = rec.as_dict()
            item["_vec"] = blob_to_vec(vec_blob) if vec_blob else []
            out.append(item)
        return out

    # ------------------------------------------------------------- FTS
    def _fts_query(self, query: str) -> str:
        from .text import normalize_text

        cleaned = re.sub(r'["*():^\-]', " ", query or "").strip()
        if len(normalize_text(cleaned)) < 3:
            return ""
        return cleaned

    def fts_ids(
        self,
        folder_id: str,
        query: str,
        limit: int = 15,
        include_knowledge: bool = False,
        include_archived: bool = False,
    ) -> list[str]:
        match = self._fts_query(query)
        if not match:
            return []
        buckets = ["memory"] + (["knowledge"] if include_knowledge else [])
        statuses = ["active"] + (["archived"] if include_archived else [])
        sql = (
            "SELECT f.memory_id FROM memory_fts f "
            "JOIN memory_records r ON r.memory_id = f.memory_id "
            "WHERE memory_fts MATCH ? AND r.folder_id = ? AND r.is_deleted = 0 "
            f"AND r.bucket IN ({', '.join('?' for _ in buckets)}) "
            f"AND r.status IN ({', '.join('?' for _ in statuses)}) "
            "LIMIT ?"
        )
        try:
            rows = self.db.fetchall(sql, (match, folder_id, *buckets, *statuses, limit))
        except sqlite3.OperationalError:
            return []
        return [r["memory_id"] for r in rows]

    # ------------------------------------------------------------- counters
    def bump_activation(self, ids: Iterable[str], now: float, search_hit: bool = True) -> None:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return
        with self.db.tx() as conn:
            for mid in ids:
                if search_hit:
                    conn.execute(
                        "UPDATE memory_records SET activation_count = activation_count + 1, "
                        "search_hits = search_hits + 1, last_activated_at = ? WHERE memory_id = ?",
                        (now, mid),
                    )
                else:
                    conn.execute(
                        "UPDATE memory_records SET activation_count = activation_count + 1, "
                        "last_activated_at = ? WHERE memory_id = ?",
                        (now, mid),
                    )

    # ------------------------------------------------------------- listing
    def active_titles(self, folder_id: str, limit: int = 10) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT memory_id, title, memory_type, updated_at FROM memory_records "
            "WHERE folder_id = ? AND bucket = 'memory' AND status = 'active' AND is_deleted = 0 "
            "ORDER BY updated_at DESC LIMIT ?",
            (folder_id, int(limit)),
        )
        return [dict(r) for r in rows]

    def lifecycle_candidates(self, cutoff: float, folder_id: str | None = None) -> list[Record]:
        sql = (
            "SELECT * FROM memory_records WHERE bucket = 'memory' AND status = 'active' "
            "AND is_deleted = 0 AND updated_at < ?"
        )
        params: list[Any] = [cutoff]
        if folder_id:
            sql += " AND folder_id = ?"
            params.append(folder_id)
        rows = self.db.fetchall(sql + " ORDER BY updated_at ASC", tuple(params))
        out = []
        for row in rows:
            rec = Record.from_row(row)
            out.append(rec)
        tags = self.tags_for([r.memory_id for r in out])
        for rec in out:
            rec.tags = tags.get(rec.memory_id, empty_tags())
        return out

    # ------------------------------------------------------------- runtime state
    def state_get(self, folder_id: str, key: str, default=None):
        row = self.db.fetchone(
            "SELECT value FROM runtime_state WHERE folder_id = ? AND key = ?", (folder_id, key)
        )
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def state_set(self, folder_id: str, key: str, value, now: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runtime_state(folder_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (folder_id, key, json.dumps(value, ensure_ascii=False), now),
        )

    # ------------------------------------------------------------- memos
    def memo_set(self, folder_id: str, content: str, expires_at: float, now: float, stream_id: str = "") -> str:
        import uuid

        memo_id = "memo-" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO temporary_memos(memo_id, folder_id, stream_id, content, expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memo_id, folder_id, stream_id, content, expires_at, now, now),
        )
        return memo_id

    def memo_list(self, folder_id: str, now: float, include_consumed: bool = False) -> list[dict]:
        self.db.execute(
            "DELETE FROM temporary_memos WHERE expires_at <= ? AND consumed_at = 0", (now,)
        )
        sql = "SELECT * FROM temporary_memos WHERE folder_id = ? AND expires_at > ?"
        if not include_consumed:
            sql += " AND consumed_at = 0"
        rows = self.db.fetchall(sql + " ORDER BY created_at ASC", (folder_id, now))
        return [dict(r) for r in rows]

    def memo_consume(self, folder_id: str, now: float) -> list[dict]:
        rows = self.memo_list(folder_id, now)
        if rows:
            with self.db.tx() as conn:
                for row in rows:
                    conn.execute(
                        "UPDATE temporary_memos SET consumed_at = ?, updated_at = ? WHERE memo_id = ?",
                        (now, now, row["memo_id"]),
                    )
        return rows

    # ------------------------------------------------------------- audit
    def audit_insert(self, data: dict) -> None:
        self.db.execute(
            "INSERT INTO extraction_audit(candidate_id, folder_id, chat_type, person_id, source, "
            "memory_type, title, content, evidence, source_text, payload, status, block_reason, "
            "warn_flags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["candidate_id"], data["folder_id"], data.get("chat_type", ""),
                data.get("person_id", ""), data["source"], data["memory_type"],
                data["title"], data["content"], data["evidence"], data["source_text"],
                json.dumps(data.get("payload", {}), ensure_ascii=False),
                data["status"], data.get("block_reason", ""),
                json.dumps(data.get("warn_flags", []), ensure_ascii=False),
                data["created_at"],
            ),
        )

    def audit_get(self, candidate_id: str) -> dict | None:
        row = self.db.fetchone(
            "SELECT * FROM extraction_audit WHERE candidate_id = ?", (candidate_id,)
        )
        return dict(row) if row else None

    def audit_list(self, folder_id: str, status: str | None = "pending", limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM extraction_audit WHERE folder_id = ?"
        params: list[Any] = [folder_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.db.fetchall(sql, tuple(params))]

    def audit_decide(self, candidate_id: str, status: str, now: float, reason: str = "", result: dict | None = None, note: str = "") -> None:
        self.db.execute(
            "UPDATE extraction_audit SET status = ?, block_reason = ?, result = ?, "
            "decided_at = ?, decision_note = ? WHERE candidate_id = ?",
            (status, reason, json.dumps(result or {}, ensure_ascii=False), now, note, candidate_id),
        )

    def candidate_count_since(self, folder_id: str, since: float, memory_type: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM extraction_audit WHERE folder_id = ? AND created_at >= ?"
        params: list[Any] = [folder_id, since]
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        row = self.db.fetchone(sql, tuple(params))
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------- messages
    def message_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        return self.db.fetchone(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ) is not None

    def mark_message(self, message_id: str, folder_id: str, now: float) -> None:
        if not message_id:
            return
        self.db.execute(
            "INSERT OR IGNORE INTO processed_messages(message_id, folder_id, processed_at) VALUES (?, ?, ?)",
            (message_id, folder_id, now),
        )

    # ------------------------------------------------------------- embeddings
    def embedding_map(self, ids: Iterable[str]) -> dict[str, list[float]]:
        ids = list(dict.fromkeys(ids))
        out: dict[str, list[float]] = {}
        for chunk in _chunks(ids, 400):
            marks = ", ".join("?" for _ in chunk)
            rows = self.db.fetchall(
                f"SELECT memory_id, vec FROM memory_embeddings WHERE memory_id IN ({marks})",
                tuple(chunk),
            )
            for row in rows:
                out[row["memory_id"]] = blob_to_vec(row["vec"])
        return out

    # ------------------------------------------------------------- stats / reconcile
    def counts(self) -> dict:
        rows = self.db.fetchall(
            "SELECT bucket, status, memory_type, COUNT(*) AS c FROM memory_records "
            "WHERE is_deleted = 0 GROUP BY bucket, status, memory_type"
        )
        by_bucket: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for row in rows:
            by_bucket[row["bucket"]] = by_bucket.get(row["bucket"], 0) + row["c"]
            by_status[row["status"]] = by_status.get(row["status"], 0) + row["c"]
            by_type[row["memory_type"]] = by_type.get(row["memory_type"], 0) + row["c"]
        total = sum(by_bucket.values())
        deleted = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM memory_records WHERE is_deleted = 1"
        )["c"]
        return {
            "total": total,
            "by_bucket": by_bucket,
            "by_status": by_status,
            "by_type": by_type,
            "soft_deleted": int(deleted),
        }

    def orphan_counts(self) -> dict:
        q = {
            "embeddings_without_record": "SELECT COUNT(*) AS c FROM memory_embeddings WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)",
            "tags_without_record": "SELECT COUNT(*) AS c FROM memory_tags WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)",
            "fts_without_record": "SELECT COUNT(*) AS c FROM memory_fts WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)",
            "records_without_embedding": "SELECT COUNT(*) AS c FROM memory_records WHERE is_deleted = 0 AND memory_id NOT IN (SELECT memory_id FROM memory_embeddings)",
            "records_without_fts": "SELECT COUNT(*) AS c FROM memory_records WHERE is_deleted = 0 AND memory_id NOT IN (SELECT memory_id FROM memory_fts)",
        }
        return {name: int(self.db.fetchone(sql)["c"]) for name, sql in q.items()}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
