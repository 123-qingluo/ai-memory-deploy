"""对外门面：一次 open() 拿到全部能力；每个调用都必须带 Scope。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import load_config, resolve
from .db import Database
from .extract import ExtractionService
from .lifecycle import LifecycleService
from .models import Scope, now_ts
from .read import ReadService
from .store import Store
from .text import build_embedder, build_llm, cosine_scores
from .trace import Tracer
from .write import Rejected, SensitiveFilter, WriteService


class MemorySystem:
    def __init__(self, root: str | Path, cfg: dict, llm=None, embedder=None):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.db = Database(resolve(self.root, cfg["db_path"]))
        self.db.init()
        self.embedder = embedder if embedder is not None else build_embedder(cfg)
        self.llm = llm if llm is not None else build_llm(cfg)
        self.tracer = Tracer(resolve(self.root, cfg["trace_dir"]))
        self.store = Store(self.db)
        self.sensitive = SensitiveFilter(resolve(self.root, cfg.get("sensitive_path", "sensitive.json")))
        self.write_svc = WriteService(
            self.store, self.embedder, cfg, self.tracer, self.sensitive,
            candidates_path=resolve(self.root, cfg.get("candidates_path", "data/candidates.jsonl")),
        )
        self.read_svc = ReadService(self.store, self.embedder, cfg, self.tracer)
        self.extract_svc = ExtractionService(
            self.store, self.embedder, self.llm, self.write_svc, cfg, self.tracer,
            prompts_dir=resolve(self.root, "prompts"),
        )
        self.lifecycle_svc = LifecycleService(
            self.store, self.embedder, cfg, self.tracer,
            report_dir=resolve(self.root, cfg.get("report_dir", "reports")),
        )

    # ------------------------------------------------------------- lifecycle of the object
    @classmethod
    def open(cls, root: str | Path = ".", overrides: dict | None = None, llm=None, embedder=None) -> "MemorySystem":
        return cls(root, load_config(root, overrides), llm=llm, embedder=embedder)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- writes
    def create(self, scope: Scope, **kwargs) -> dict:
        try:
            return self.write_svc.create(scope, **kwargs)
        except Rejected as exc:
            return exc.as_dict()

    def update(self, scope: Scope, memory_id: str, **kwargs) -> dict:
        try:
            return self.write_svc.update(scope, memory_id, **kwargs)
        except Rejected as exc:
            return exc.as_dict()

    def delete(self, scope: Scope, memory_id: str) -> dict:
        try:
            return self.write_svc.delete(scope, memory_id)
        except Rejected as exc:
            return exc.as_dict()

    # ------------------------------------------------------------- reads
    def search(
        self,
        scope: Scope,
        query: str,
        top_k: int | None = None,
        include_archived: bool = False,
        include_knowledge: bool = False,
        min_score: float = 0.0,
    ) -> list[dict]:
        return self.read_svc.search(
            scope, query, top_k=top_k, include_archived=include_archived,
            include_knowledge=include_knowledge, min_score=min_score,
        )

    def read(self, scope: Scope, memory_id: str) -> dict | None:
        rec = self.store.get(memory_id, scope.folder_id)
        return rec.as_dict() if rec else None

    def prefetch(self, scope: Scope, query: str, now: float | None = None) -> dict:
        return self.read_svc.prefetch(scope, query, now=now)

    def reminder(self, scope: Scope, now: float | None = None) -> dict:
        return self.read_svc.reminder(scope, now=now)

    def note_tool_used(self, scope: Scope, now: float | None = None) -> None:
        self.read_svc.note_tool_used(scope, now=now)

    # ------------------------------------------------------------- extraction / approval
    def extract_after_turn(self, scope: Scope, user_text: str, message_id: str = "", now: float | None = None) -> dict:
        return self.extract_svc.after_turn(scope, user_text, message_id=message_id, now=now)

    def candidates(self, scope: Scope, status: str | None = "pending", limit: int = 50) -> list[dict]:
        rows = self.store.audit_list(scope.folder_id, status=status, limit=limit)
        for row in rows:
            row["payload"] = json.loads(row.get("payload") or "{}")
            row["warn_flags"] = json.loads(row.get("warn_flags") or "[]")
            row["result"] = json.loads(row.get("result") or "{}")
        return rows

    def approve_candidate(self, scope: Scope, candidate_id: str) -> dict:
        try:
            return self.write_svc.approve(scope, candidate_id)
        except Rejected as exc:
            return exc.as_dict()

    def reject_candidate(self, scope: Scope, candidate_id: str, note: str = "") -> dict:
        try:
            return self.write_svc.reject(scope, candidate_id, note=note)
        except Rejected as exc:
            return exc.as_dict()

    # ------------------------------------------------------------- memo
    def memo_set(self, scope: Scope, content: str, expire_hours: float | None = None, now: float | None = None) -> dict:
        now = now or now_ts()
        hours = float(expire_hours if expire_hours is not None else self.cfg.get("memo", {}).get("default_expire_hours", 2))
        memo_id = self.store.memo_set(scope.folder_id, content, now + hours * 3600, now)
        self.tracer.log("memo", "set", memo_id=memo_id, expire_hours=hours)
        return {"ok": True, "memo_id": memo_id, "expires_at": now + hours * 3600}

    def memo_list(self, scope: Scope, now: float | None = None) -> list[dict]:
        return self.store.memo_list(scope.folder_id, now or now_ts())

    def memo_consume(self, scope: Scope, now: float | None = None) -> list[dict]:
        return self.store.memo_consume(scope.folder_id, now or now_ts())

    # ------------------------------------------------------------- lifecycle
    def run_lifecycle(
        self,
        scope: Scope | None = None,
        dry_run: bool | None = None,
        allow_hard_delete: bool | None = None,
        now: float | None = None,
    ) -> dict:
        return self.lifecycle_svc.run(scope=scope, dry_run=dry_run, allow_hard_delete=allow_hard_delete, now=now)

    def reconcile(self) -> dict:
        return self.lifecycle_svc.reconcile()

    def reindex(self, force: bool = False) -> dict:
        return self.lifecycle_svc.reembed_all(force=force)

    # ------------------------------------------------------------- health / stats
    def health(self) -> dict:
        tables = self.db.table_names()
        required = {
            "memory_records", "memory_tags", "memory_embeddings", "memory_fts",
            "temporary_memos", "extraction_audit", "processed_messages", "runtime_state",
        }
        missing = sorted(required - tables)
        version_row = self.db.fetchone("SELECT value FROM schema_meta WHERE key='schema_version'")
        probe_ok = False
        try:
            self.db.conn.execute("BEGIN IMMEDIATE")
            self.db.conn.execute(
                "INSERT OR REPLACE INTO runtime_state(folder_id, key, value, updated_at) VALUES ('__health__','probe','1',?)",
                (time.time(),),
            )
            row = self.db.conn.execute(
                "SELECT value FROM runtime_state WHERE folder_id='__health__' AND key='probe'"
            ).fetchone()
            probe_ok = bool(row and row["value"] == "1")
            self.db.conn.execute("ROLLBACK")
        except Exception:
            try:
                self.db.conn.execute("ROLLBACK")
            except Exception:
                pass
            probe_ok = False
        models = [
            row["model"]
            for row in self.db.fetchall("SELECT DISTINCT model FROM memory_embeddings ORDER BY model")
        ]
        stale = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM memory_embeddings WHERE model != ?", (self.embedder.model,)
        )["c"]
        orphans = self.store.orphan_counts()
        return {
            "ok": (
                not missing
                and probe_ok
                and int(stale) == 0
                and orphans["records_without_embedding"] == 0
                and orphans["records_without_fts"] == 0
            ),
            "db_path": self.db.path,
            "schema_version": int(version_row["value"]) if version_row else None,
            "fts_tokenizer": self.db.fts_tokenizer,
            "missing_tables": missing,
            "write_probe": probe_ok,
            "embedding_model": self.embedder.model,
            "embedding_models": models,
            "stale_embeddings": int(stale),
            "missing_embeddings": orphans["records_without_embedding"],
            "missing_fts": orphans["records_without_fts"],
        }

    def stats(self, now: float | None = None) -> dict:
        now = now or now_ts()
        counts = self.store.counts()
        stale_cutoff = now - int(self.cfg.get("lifecycle", {}).get("archive_after_days", 90)) * 86400
        active_memory = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM memory_records WHERE bucket='memory' AND status='active' AND is_deleted=0"
        )["c"]
        stale = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM memory_records WHERE bucket='memory' AND status='active' "
            "AND is_deleted=0 AND updated_at < ?",
            (stale_cutoff,),
        )["c"]
        pending = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM extraction_audit WHERE status='pending'"
        )["c"]
        traces = self.tracer.summarize(days=7)
        injected = traces.get("auto_recall:injected", 0)
        below = traces.get("auto_recall:below_threshold", 0)
        empty = traces.get("auto_recall:empty", 0)
        # 这是"预取调用中成功注入"的比例，不是"有注入的轮次/总轮次"——
        # 后者需要宿主上报总轮数，核心不知道。
        injection_rate = round(injected / (injected + below + empty), 4) if (injected + below + empty) else None
        return {
            "records": counts,
            "stale": {
                "active_memory": int(active_memory),
                "older_than_archive_days": int(stale),
                "rate": round(stale / active_memory, 4) if active_memory else 0.0,
            },
            "duplicate_pairs": self._duplicate_pairs(),
            "candidates_pending": int(pending),
            "traces_7d": traces,
            "prefetch_injection_rate_7d": injection_rate,
        }

    def _duplicate_pairs(self, sample: int = 200) -> int:
        rows = self.db.fetchall(
            "SELECT r.memory_id, e.vec FROM memory_records r JOIN memory_embeddings e "
            "ON e.memory_id = r.memory_id WHERE r.is_deleted = 0 LIMIT ?",
            (sample,),
        )
        if len(rows) < 2:
            return 0
        from .text import blob_to_vec

        vectors = [blob_to_vec(r["vec"]) for r in rows]
        pairs = 0
        for i in range(len(vectors)):
            try:
                scores = cosine_scores(vectors[i], vectors[i + 1 :])
            except Exception:
                # 混入不同维度的旧向量时先不崩；health 会报 stale_embeddings
                continue
            pairs += sum(1 for s in scores if s >= 0.90)
        return pairs

    # ------------------------------------------------------------- eval / selfcheck
    def evaluate(self, dataset_path: str | Path | None = None, top_k: int = 3) -> dict:
        from .selfcheck import evaluate_in_temp

        path = Path(dataset_path).resolve() if dataset_path else None
        return evaluate_in_temp(self.cfg, self.root, path, top_k=top_k)

    def selfcheck(self) -> dict:
        from .selfcheck import run_selfcheck

        return run_selfcheck(self.cfg, root=self.root)
