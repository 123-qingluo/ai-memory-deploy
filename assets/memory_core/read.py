"""读取管线：三路读取中的两路（自动预取 + 工具查询），以及每轮 reminder。

v1 不实现随机闪回；接口与参数在文档中保留，避免把未验证的行为塞进基线。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .models import Scope, now_ts
from .tags import tag_boost
from .text import cosine, lexical_overlap, title_jaccard

TYPE_LABELS = {
    "person": "人物档案",
    "event": "事件",
    "knowledge": "事实/偏好",
    "procedure": "流程",
}


class ReadService:
    def __init__(self, store, embedder, cfg: dict, tracer):
        self.store = store
        self.embedder = embedder
        self.cfg = cfg
        self.tracer = tracer
        self.retrieval = cfg.get("retrieval", {})
        self.prefetch_cfg = cfg.get("prefetch", {})
        self.reminder_cfg = cfg.get("reminder", {})
        self.supervision_cfg = cfg.get("supervision", {})

    # ------------------------------------------------------------- helpers
    def threshold(self) -> float:
        if getattr(self.embedder, "kind", "hashing") == "http":
            return float(self.retrieval.get("min_score_http", 0.60))
        return float(self.retrieval.get("min_score_hashing", 0.28))

    def expand_query(self, query: str) -> str:
        q = (query or "").strip()
        limit = int(self.prefetch_cfg.get("max_query_chars", 200))
        if not q:
            return ""
        if not self.prefetch_cfg.get("multi_topic_split", True) or len(q) <= 120:
            return q[:limit]
        parts = re.split(r"[。！？!?\n]+", q)
        heads = [p.strip() for p in parts if p.strip()][:3]
        return (" ".join(heads) or q)[:limit]

    def _embed_query(self, query: str) -> list[float] | None:
        try:
            return self.embedder.embed([query])[0]
        except Exception as exc:
            self.tracer.log("read", "embedding_error", error=str(exc)[:200])
            return None

    def _query_hash(self, query: str) -> str:
        return hashlib.sha1((query or "").encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------- ranking
    def _rank(
        self,
        scope: Scope,
        query: str,
        *,
        top_k: int,
        include_archived: bool = False,
        include_knowledge: bool = False,
    ) -> list[dict]:
        rows = self.store.candidate_rows(
            scope.folder_id,
            include_knowledge=include_knowledge,
            include_archived=include_archived,
        )
        if not rows:
            return []
        qvec = self._embed_query(query)
        factor = int(self.retrieval.get("fts_candidates_factor", 3))
        fts_ids = set(
            self.store.fts_ids(
                scope.folder_id,
                query,
                limit=max(top_k * factor, 10),
                include_knowledge=include_knowledge,
                include_archived=include_archived,
            )
        )
        scored: list[dict] = []
        alpha = 0.85 if getattr(self.embedder, "kind", "") == "http" else 0.50
        for row in rows:
            vec = row.get("_vec") or []
            lex = lexical_overlap(query, f"{row['title']}\n{row['content']}")
            if qvec and vec:
                cos = cosine(qvec, vec)
                score = alpha * cos + (1 - alpha) * lex
            elif lex > 0:
                cos = 0.0
                score = lex
            else:
                continue
            if row["id"] in fts_ids:
                score = max(score, 0.50 + 0.50 * lex)
            score += tag_boost(query, row.get("tags", {}), self.retrieval)
            if score <= 0.0:
                continue
            row["score"] = round(max(0.0, min(1.0, score)), 4)
            row["_cos"] = cos
            scored.append(row)

        scored.sort(key=lambda r: r["score"], reverse=True)
        dedupe_at = float(self.retrieval.get("result_dedupe", 0.88))
        kept: list[dict] = []
        for row in scored:
            redundant = False
            for other in kept:
                vec_a, vec_b = row.get("_vec") or [], other.get("_vec") or []
                if vec_a and vec_b and cosine(vec_a, vec_b) >= dedupe_at:
                    redundant = True
                    break
                if not vec_a or not vec_b:
                    if title_jaccard(row["title"], other["title"]) >= 0.85:
                        redundant = True
                        break
            if not redundant:
                kept.append(row)
            if len(kept) >= top_k:
                break
        return kept

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "summary": row.get("summary", ""),
            "type": row["type"],
            "bucket": row["bucket"],
            "status": row["status"],
            "score": row["score"],
            "updated_at": row.get("updated_at", 0),
            "tags": row.get("tags", {}),
        }

    # ------------------------------------------------------------- tool search
    def search(
        self,
        scope: Scope,
        query: str,
        top_k: int | None = None,
        include_archived: bool = False,
        include_knowledge: bool = False,
        min_score: float = 0.0,
    ) -> list[dict]:
        k = int(top_k or self.retrieval.get("top_k", 5))
        ranked = self._rank(
            scope, query, top_k=k,
            include_archived=include_archived, include_knowledge=include_knowledge,
        )
        ranked = [r for r in ranked if r["score"] >= min_score]
        if ranked:
            # 检索命中即"使用过"：同时计激活与检索命中，生命周期投票才包含工具查询
            self.store.bump_activation([r["id"] for r in ranked], now_ts(), search_hit=True)
        self.tracer.log(
            "search", "ok" if ranked else "empty",
            query_len=len(query or ""), query_hash=self._query_hash(query),
            candidates=len(ranked), top=[{"id": r["id"], "score": r["score"]} for r in ranked[:3]],
        )
        return [self._public(r) for r in ranked]

    # ------------------------------------------------------------- prefetch
    def prefetch(self, scope: Scope, query: str, now: float | None = None) -> dict:
        now = now or now_ts()
        if scope.chat_type == "group" and not self.prefetch_cfg.get("allow_group", False):
            return {"items": [], "text": "", "status": "skipped_group"}
        q = self.expand_query(query)
        if not q:
            return {"items": [], "text": "", "status": "empty_query"}
        k = int(self.retrieval.get("top_k", 5))
        ranked = self._rank(scope, q, top_k=k, include_archived=False, include_knowledge=False)
        threshold = self.threshold()
        window = float(self.prefetch_cfg.get("dedupe_window_s", 120))
        recent = self.store.state_get(scope.folder_id, "prefetch_recent", []) or []
        recent = [x for x in recent if isinstance(x, dict) and now - float(x.get("ts", 0)) < window]
        recent_ids = {x.get("id") for x in recent}
        count = int(self.retrieval.get("prefetch_count", 3))
        selected = [r for r in ranked if r["score"] >= threshold and r["id"] not in recent_ids][:count]

        if not selected:
            status = "below_threshold" if ranked else "empty"
            self.tracer.log(
                "auto_recall", status,
                query_len=len(q), query_hash=self._query_hash(q),
                candidates=len(ranked), threshold=threshold,
            )
            return {"items": [], "text": "", "status": status, "threshold": threshold}

        self.store.bump_activation([r["id"] for r in selected], now, search_hit=True)
        recent.extend({"id": r["id"], "ts": now} for r in selected)
        self.store.state_set(scope.folder_id, "prefetch_recent", recent[-20:], now)

        items = []
        for row in selected:
            items.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "summary": row.get("summary", ""),
                    "type": row["type"],
                    "score": row["score"],
                }
            )
        text = self._format_prefetch(items)
        self.tracer.log(
            "auto_recall", "injected",
            query_len=len(q), query_hash=self._query_hash(q),
            candidates=len(ranked), threshold=threshold,
            selected=[{"id": i["id"], "score": i["score"], "type": i["type"]} for i in items],
        )
        return {"items": items, "text": text, "status": "injected", "threshold": threshold}

    def _format_prefetch(self, items: list[dict]) -> str:
        lines = ["[记忆预取] 以下可能是相关记忆（不是全部；未列出不代表没有）："]
        for item in items:
            label = TYPE_LABELS.get(item["type"], item["type"])
            lines.append(f"- ({label}) {item['title']}：{item['summary']}")
        lines.append("需要确认时先检索；没有记录就说不记得，不要编造。")
        return "\n".join(lines)

    # ------------------------------------------------------------- reminder
    def reminder(self, scope: Scope, now: float | None = None) -> dict:
        now = now or now_ts()
        limit = int(self.reminder_cfg.get("active_titles_limit", 10))
        titles = self.store.active_titles(scope.folder_id, limit)
        memos = self.store.memo_consume(scope.folder_id, now)
        warning = self._supervision_tick(scope, now)

        blocks: list[str] = []
        if titles:
            lines = ["[活跃记忆标题] 最近更新的 10 条（这不是全部记忆，不要把这个列表当作全集）："]
            for item in titles:
                lines.append(f"- {item['title']}")
            blocks.append("\n".join(lines))
        if memos:
            lines = ["[临时便签]（已消费，不进长期检索）："]
            for item in memos:
                lines.append(f"- {item['content']}")
            blocks.append("\n".join(lines))
        if warning:
            blocks.append(warning)
        return {
            "titles": [t["title"] for t in titles],
            "memos": [m["content"] for m in memos],
            "warning": warning,
            "text": "\n\n".join(blocks),
        }

    def _supervision_tick(self, scope: Scope, now: float) -> str:
        cfg = self.supervision_cfg
        limit = int(cfg.get("rounds_without_tool_warning", 20))
        if limit <= 0:
            return ""
        turns = int(self.store.state_get(scope.folder_id, "turns_since_tool", 0) or 0) + 1
        if turns < limit:
            self.store.state_set(scope.folder_id, "turns_since_tool", turns, now)
            return ""
        cooldown = float(cfg.get("cooldown_minutes", 30)) * 60
        last_warn = float(self.store.state_get(scope.folder_id, "supervision_last_warn", 0) or 0)
        if now - last_warn < cooldown:
            self.store.state_set(scope.folder_id, "turns_since_tool", turns, now)
            return ""
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        state = self.store.state_get(scope.folder_id, "supervision_daily", {}) or {}
        if state.get("day") != day:
            state = {"day": day, "count": 0}
        if int(state.get("count", 0)) >= int(cfg.get("daily_max", 3)):
            self.store.state_set(scope.folder_id, "turns_since_tool", turns, now)
            return ""
        state["count"] = int(state.get("count", 0)) + 1
        self.store.state_set(scope.folder_id, "supervision_daily", state, now)
        self.store.state_set(scope.folder_id, "supervision_last_warn", now, now)
        self.store.state_set(scope.folder_id, "turns_since_tool", 0, now)
        self.tracer.log("supervision", "warned", turns=limit)
        return (
            f"[工具提醒] 已经连续 {limit} 轮没有查过记忆。涉及偏好、约定或过往事件时，"
            "先 search 再回答；没有记录就说不记得，不要凭印象编造。"
        )

    def note_tool_used(self, scope: Scope, now: float | None = None) -> None:
        now = now or now_ts()
        self.store.state_set(scope.folder_id, "turns_since_tool", 0, now)
