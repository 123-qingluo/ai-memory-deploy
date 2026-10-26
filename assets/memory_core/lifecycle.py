"""生命周期：对账、分级衰减、清理报告。

默认 dry_run + 硬删关闭。归档不等于不可检索，硬删才是真的消失。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import Scope, now_ts
from .text import vec_to_blob


class LifecycleService:
    def __init__(self, store, embedder, cfg: dict, tracer, report_dir: str | Path):
        self.store = store
        self.embedder = embedder
        self.cfg = cfg
        self.tracer = tracer
        self.report_dir = Path(report_dir)
        self.life_cfg = cfg.get("lifecycle", {})

    # ------------------------------------------------------------- reconcile
    def reconcile(self) -> dict:
        before = self.store.orphan_counts()
        fixed = {
            "embeddings_rebuilt": 0,
            "fts_rebuilt": 0,
            "failed_embeddings": 0,
            "messages_pruned": 0,
            "memos_pruned": 0,
            "runtime_state_pruned": 0,
        }
        db = self.store.db
        now = now_ts()
        with db.tx() as conn:
            fixed["messages_pruned"] = conn.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?", (now - 30 * 86400,)
            ).rowcount
            fixed["memos_pruned"] = conn.execute(
                "DELETE FROM temporary_memos WHERE expires_at <= ?", (now,)
            ).rowcount
            fixed["runtime_state_pruned"] = conn.execute(
                "DELETE FROM runtime_state WHERE updated_at < ?", (now - 30 * 86400,)
            ).rowcount
            conn.execute(
                "DELETE FROM memory_embeddings WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)"
            )
            conn.execute(
                "DELETE FROM memory_tags WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)"
            )
            conn.execute(
                "DELETE FROM memory_fts WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)"
            )
            conn.execute(
                "DELETE FROM memory_fts WHERE memory_id IN "
                "(SELECT memory_id FROM memory_records WHERE is_deleted = 1)"
            )
        missing = db.fetchall(
            "SELECT memory_id, title, content FROM memory_records "
            "WHERE memory_id NOT IN (SELECT memory_id FROM memory_embeddings)"
        )
        for row in missing:
            try:
                vec = self.embedder.embed([f"{row['title']}\n{row['content']}"])[0]
            except Exception:
                fixed["failed_embeddings"] += 1
                continue
            with db.tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings(memory_id, model, dim, vec) VALUES (?, ?, ?, ?)",
                    (row["memory_id"], self.embedder.model, len(vec), vec_to_blob(vec)),
                )
            fixed["embeddings_rebuilt"] += 1
        missing_fts = db.fetchall(
            "SELECT memory_id, title, content FROM memory_records "
            "WHERE is_deleted = 0 AND memory_id NOT IN (SELECT memory_id FROM memory_fts)"
        )
        for row in missing_fts:
            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO memory_fts(memory_id, title, content) VALUES (?, ?, ?)",
                    (row["memory_id"], row["title"], row["content"]),
                )
            fixed["fts_rebuilt"] += 1
        after = self.store.orphan_counts()
        report = {"before": before, "fixed": fixed, "after": after}
        self.tracer.log("lifecycle", "reconciled", **fixed)
        return report

    # ------------------------------------------------------------- reindex
    def reembed_all(self, force: bool = False) -> dict:
        """按当前嵌入模型重算向量。换模型后必须跑，否则旧向量与新 query 不同维/不同分布。"""
        rows = self.store.db.fetchall(
            "SELECT r.memory_id, r.title, r.content, e.model AS model "
            "FROM memory_records r LEFT JOIN memory_embeddings e ON e.memory_id = r.memory_id"
        )
        total = len(rows)
        updated = skipped = failed = 0
        for row in rows:
            if not force and row["model"] == self.embedder.model:
                skipped += 1
                continue
            try:
                vec = self.embedder.embed([f"{row['title']}\n{row['content']}"])[0]
            except Exception as exc:
                self.tracer.log("lifecycle", "reindex_error", memory_id=row["memory_id"], error=str(exc)[:200])
                failed += 1
                continue
            with self.store.db.tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings(memory_id, model, dim, vec) VALUES (?, ?, ?, ?)",
                    (row["memory_id"], self.embedder.model, len(vec), vec_to_blob(vec)),
                )
            updated += 1
        report = {
            "ok": failed == 0,
            "model": self.embedder.model,
            "total": total,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
        }
        self.tracer.log("lifecycle", "reindexed", **{k: report[k] for k in ("total", "updated", "skipped", "failed")})
        return report

    # ------------------------------------------------------------- cleanup
    def run(
        self,
        scope: Scope | None = None,
        dry_run: bool | None = None,
        allow_hard_delete: bool | None = None,
        now: float | None = None,
    ) -> dict:
        now = now or now_ts()
        reconcile_report = self.reconcile()
        dry = bool(self.life_cfg.get("dry_run", True) if dry_run is None else dry_run)
        allow_delete = bool(
            self.life_cfg.get("allow_hard_delete", False) if allow_hard_delete is None else allow_hard_delete
        )
        days = int(self.life_cfg.get("archive_after_days", 90))
        threshold = int(self.life_cfg.get("activation_archive_threshold", 1))
        pin_tag = str(self.life_cfg.get("pin_tag", "pin"))
        cutoff = now - days * 86400
        folder_id = scope.folder_id if scope else None

        records = self.store.lifecycle_candidates(cutoff, folder_id)
        protected, to_archive, to_delete = [], [], []
        for rec in records:
            if pin_tag in rec.tags.get("tag", []):
                protected.append(rec)
            elif rec.activation_count >= threshold:
                to_archive.append(rec)
            else:
                to_delete.append(rec)

        archived, deleted, kept = [], [], []
        if not dry:
            for rec in to_archive:
                self.store.update_record(
                    rec.memory_id, rec.folder_id,
                    {"status": "archived", "is_archived": 1, "updated_at": now},
                )
                archived.append(rec)
            for rec in to_delete:
                if allow_delete:
                    self.store.hard_delete(rec.memory_id)
                    deleted.append(rec)
                else:
                    kept.append(rec)

        def brief(rec):
            return {
                "id": rec.memory_id,
                "folder_id": rec.folder_id,
                "title": rec.title,
                "type": rec.memory_type,
                "activation_count": rec.activation_count,
                "updated_at": rec.updated_at,
            }

        report = {
            "params": {
                "dry_run": dry,
                "allow_hard_delete": allow_delete,
                "archive_after_days": days,
                "activation_archive_threshold": threshold,
                "pin_tag": pin_tag,
                "folder_id": folder_id or "*",
                "cutoff": cutoff,
                "generated_at": now,
            },
            "counts": {
                "candidates": len(records),
                "protected": len(protected),
                "archived": len(archived),
                "deleted": len(deleted),
                "would_archive": len(to_archive),
                "would_delete": len(to_delete),
                "kept_because_hard_delete_disabled": len(kept),
            },
            "reconcile": reconcile_report,
            "protected": [brief(r) for r in protected[:200]],
            "archived": [brief(r) for r in archived[:200]],
            "deleted": [brief(r) for r in deleted[:200]],
            "would_archive": [brief(r) for r in to_archive[:200]],
            "would_delete": [brief(r) for r in to_delete[:200]],
        }

        self.report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        path = self.report_dir / f"lifecycle-{stamp}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path)
        self.tracer.log(
            "lifecycle", "dry_run" if dry else "applied",
            candidates=len(records), archived=len(archived), deleted=len(deleted),
            would_delete=len(to_delete), hard_delete_allowed=allow_delete,
        )
        return report
