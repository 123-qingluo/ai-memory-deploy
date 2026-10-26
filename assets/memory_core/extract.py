"""抽取器：偏好/边界 与 事件 两条独立轨道。

判据不同（偏好看稳定性、事件看时间锚点），所以不合并成一个"万能抽取器"。
默认 shadow：只产出候选，审批后才入库。
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta
from pathlib import Path

from .models import Scope, now_ts
from .tags import normalize_tags
from .text import cosine, parse_json_array
from .write import Rejected

EVENT_SIGNALS = re.compile(
    r"明天|后天|大后天|下周|周末|生日|纪念日|约好|答应|提醒|记得|考试|面试|出差|旅行|"
    r"见面|聚会|截止|报名|会议|复诊|看病|搬家|节日|假期|放假|出发|返程|上课|比赛|"
    r"演出|展览|婚礼|体检|打针|吃药|截止日期|deadline|schedule|appointment",
    re.IGNORECASE,
)
TEMPORARY_WORDS = re.compile(r"今天|刚刚|现在|有点|心情|临时|突然|一会儿|待会|此刻")
THIRD_PARTY_WORDS = re.compile(r"朋友|同事|同学|群友|室友|老板|领导|客户|闺蜜|兄弟")

WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def start_of_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _at(dt: datetime, hour: int) -> datetime:
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


def _period(hint: str) -> tuple[int, int]:
    if re.search(r"上午|早上|清晨", hint):
        return 9, 12
    if re.search(r"中午", hint):
        return 11, 14
    if re.search(r"下午", hint):
        return 13, 18
    if re.search(r"傍晚", hint):
        return 17, 19
    if re.search(r"晚上|夜里|今晚|明晚", hint):
        return 19, 23
    return 9, 23


def parse_time_hint(hint: str, now: float) -> tuple[float, float]:
    """把中文相对时间解析成绝对区间；解析失败返回 (0, 0)，绝不猜。"""
    if not hint:
        return 0.0, 0.0
    hint = hint.strip()
    base = datetime.fromtimestamp(now)
    target: datetime | None = None

    m = re.search(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", hint)
    if m:
        try:
            target = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return 0.0, 0.0

    if target is None:
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", hint)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            year = base.year
            try:
                candidate = datetime(year, month, day)
            except ValueError:
                return 0.0, 0.0
            if candidate.date() < base.date():
                try:
                    candidate = datetime(year + 1, month, day)
                except ValueError:
                    return 0.0, 0.0
            target = candidate

    if target is None:
        if "大后天" in hint:
            target = base + timedelta(days=3)
        elif "后天" in hint:
            target = base + timedelta(days=2)
        elif "明天" in hint or "明日" in hint:
            target = base + timedelta(days=1)
        elif "今天" in hint or "今日" in hint:
            target = base

    if target is None:
        m = re.search(r"下个?月\s*(初|中|底|末)", hint)
        if m:
            year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
            day = 1 if m.group(1) == "初" else (15 if m.group(1) == "中" else calendar.monthrange(year, month)[1])
            target = datetime(year, month, day)
        elif re.search(r"下个?月", hint):
            year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
            target = datetime(year, month, 1)

    if target is None:
        m = re.search(r"(下下?周|下个?星期)\s*([一二三四五六日天])", hint)
        if m:
            week_offset = 2 if m.group(1).startswith("下下") else 1
            monday = base - timedelta(days=base.weekday())
            target = monday + timedelta(weeks=week_offset, days=WEEKDAY[m.group(2)])
        elif re.search(r"下周末", hint):
            monday = base - timedelta(days=base.weekday())
            target = monday + timedelta(weeks=2, days=5)
        elif re.search(r"这周末|本周末|周末", hint):
            monday = base - timedelta(days=base.weekday())
            target = monday + timedelta(days=5)
            if target.date() < base.date():
                target = target + timedelta(weeks=1)

    if target is None:
        m = re.search(r"(?:周|星期)([一二三四五六日天])", hint)
        if m:
            delta = (WEEKDAY[m.group(1)] - base.weekday()) % 7
            target = base + timedelta(days=delta)

    if target is None:
        m = re.search(r"(\d{1,2})\s*[日号]", hint)
        if m:
            day = int(m.group(1))
            year, month = base.year, base.month
            try:
                candidate = datetime(year, month, day)
            except ValueError:
                return 0.0, 0.0
            if candidate.date() < base.date():
                month, year = (1, year + 1) if month == 12 else (month + 1, year)
                try:
                    candidate = datetime(year, month, day)
                except ValueError:
                    return 0.0, 0.0
            target = candidate

    if target is None:
        return 0.0, 0.0

    start_hour, end_hour = _period(hint)
    start = _at(target, start_hour).timestamp()
    end = _at(target, end_hour).timestamp()
    return float(start), float(end)


class ExtractionService:
    def __init__(self, store, embedder, llm, write, cfg: dict, tracer, prompts_dir: str | Path):
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.write = write
        self.cfg = cfg
        self.tracer = tracer
        self.prompts_dir = Path(prompts_dir)
        self.write_cfg = cfg.get("write", {})

    # ------------------------------------------------------------- prompts
    def _prompt(self, name: str) -> str:
        path = self.prompts_dir / f"{name}.md"
        if not path.exists():
            raise RuntimeError(f"缺少抽取提示词：{path}")
        return path.read_text(encoding="utf-8")

    def _call(self, prompt_name: str, user_text: str) -> list | None:
        try:
            raw = self.llm.complete(self._prompt(prompt_name), user_text)
        except Exception as exc:  # 超时/网络/解析失败都放弃本轮，不重试
            self.tracer.log("extract", "llm_error", prompt=prompt_name, error=str(exc)[:200])
            return None
        items = parse_json_array(raw)
        if items is None:
            self.tracer.log("extract", "parse_error", prompt=prompt_name, raw_len=len(raw or ""))
        return items

    # ------------------------------------------------------------- validation
    def _validate(self, item, allowed_types: list[str], user_text: str) -> tuple[dict | None, str, str]:
        if not isinstance(item, dict):
            return None, "invalid_item", "not_object"
        mtype = str(item.get("type", "")).strip()
        if mtype not in allowed_types:
            return None, "invalid_item", "type_not_allowed"
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if not title or not content:
            return None, "invalid_item", "empty_content"
        if len(evidence) < int(self.write_cfg.get("evidence_min_chars", 4)):
            return None, "invalid_item", "evidence_too_short"
        if evidence not in user_text:
            return None, "dropped", "evidence_mismatch"
        return {
            "type": mtype,
            "title": title[: int(self.write_cfg.get("max_title_chars", 20))],
            "content": content,
            "evidence": evidence,
            "tags": normalize_tags(item.get("tags", []))[:2],
            "opposing": normalize_tags(item.get("opposing", []))[:2],
            "time": str(item.get("time", "")).strip(),
            "related_people": [str(x) for x in (item.get("related_people") or [])][:5],
        }, "ok", ""

    def _triple(self, item: dict) -> str:
        core = item.get("tags") or [item["title"][:20]]
        clean = lambda xs: [re.sub(r"[,|]", " ", x).strip() for x in xs if str(x).strip()]
        return f"{','.join(clean(core)) or '-'}|-|{','.join(clean(item.get('opposing') or [])) or '-'}"

    def _is_duplicate(self, scope: Scope, title: str, content: str) -> str | None:
        try:
            vec = self.embedder.embed([f"{title}\n{content}"])[0]
        except Exception:
            return None
        rows = self.store.candidate_rows(scope.folder_id)
        threshold = float(self.write_cfg.get("dedupe_similarity", 0.90))
        for row in rows:
            other = row.get("_vec") or []
            if other and cosine(vec, other) >= threshold:
                return row["id"]
        return None

    # ------------------------------------------------------------- main
    def after_turn(self, scope: Scope, user_text: str, message_id: str = "", now: float | None = None) -> dict:
        now = now or now_ts()
        if not self.llm.available:
            return {"ok": True, "skipped": "llm_unavailable"}
        if scope.chat_type == "group" and not self.write_cfg.get("allow_group_extract", False):
            return {"ok": True, "skipped": "group"}
        if len((user_text or "").strip()) < int(self.write_cfg.get("min_user_chars", 40)):
            self.tracer.log("extract", "skipped", reason="too_short")
            return {"ok": True, "skipped": "too_short"}
        if message_id and self.store.message_seen(message_id):
            return {"ok": True, "skipped": "duplicate_message"}
        if message_id:
            self.store.mark_message(message_id, scope.folder_id, now)

        day_start = start_of_day(now)
        cap_total = int(self.write_cfg.get("daily_cap_total", 10))
        cap_pref = int(self.write_cfg.get("daily_cap_preference", 10))
        cap_event = int(self.write_cfg.get("daily_cap_event", 5))
        used_total = self.store.candidate_count_since(scope.folder_id, day_start)
        used_pref = self.store.candidate_count_since(scope.folder_id, day_start, "preference")
        used_event = self.store.candidate_count_since(scope.folder_id, day_start, "event")

        staged: list[dict] = []
        dropped: list[dict] = []

        def run(kind: str, prompt: str, allowed: list[str], source: str, cap_left: int):
            if cap_left <= 0:
                return 0
            items = self._call(prompt, user_text)
            if not items:
                return 0
            count = 0
            for item in items:
                if count >= cap_left:
                    break
                valid, action, reason = self._validate(item, allowed, user_text)
                if valid is None:
                    self.tracer.log("extract", action, reason=reason, type=str(item.get("type", ""))[:20] if isinstance(item, dict) else "")
                    dropped.append({"action": action, "reason": reason})
                    continue
                dup = self._is_duplicate(scope, valid["title"], valid["content"])
                if dup:
                    self.tracer.log("extract", "dropped", reason="duplicate", existing=dup)
                    dropped.append({"action": "dropped", "reason": "duplicate", "existing": dup})
                    continue
                block_reason = ""
                warn_flags: list[str] = []
                if self.write._check_sensitive_safe(valid["title"] + valid["content"] + valid["evidence"]):
                    block_reason = "sensitive"
                if valid["type"] == "event":
                    start_at, end_at = parse_time_hint(valid["time"], now)
                    if not start_at:
                        warn_flags.append("event_time_missing")
                        start_at, end_at = 0.0, 0.0
                else:
                    start_at, end_at = 0.0, 0.0
                if TEMPORARY_WORDS.search(valid["content"]):
                    warn_flags.append("temporary_emotion")
                if THIRD_PARTY_WORDS.search(valid["content"]):
                    warn_flags.append("third_party")
                payload = {
                    "bucket": "memory",
                    "extract_type": valid["type"],
                    # 抽取出的 tags 是语义方向，进 core（triple_tags），不重复塞进普通 tag 组
                    "tags": [],
                    "triple_tags": self._triple(valid),
                    "event_start_at": start_at,
                    "event_end_at": end_at,
                    "related_people": valid["related_people"],
                }
                # preference / boundary 是抽取轨道，不是 memory_type：统一落成 knowledge
                mapped_type = "event" if valid["type"] == "event" else "knowledge"
                if self.write_cfg.get("shadow", True) or block_reason:
                    result = self.write.stage_candidate(
                        scope,
                        title=valid["title"],
                        content=valid["content"],
                        memory_type=mapped_type,
                        source_text=user_text,
                        evidence=valid["evidence"],
                        source=source,
                        block_reason=block_reason,
                        warn_flags=warn_flags,
                        payload=payload,
                    )
                    staged.append(result)
                else:
                    try:
                        result = self.write.create(
                            scope,
                            title=valid["title"],
                            content=valid["content"],
                            memory_type=mapped_type,
                            bucket="memory",
                            source=source,
                            tags=[],
                            triple_tags=payload["triple_tags"],
                            evidence=valid["evidence"],
                            source_text=user_text,
                            event_start_at=start_at,
                            event_end_at=end_at,
                            related_people=valid["related_people"],
                        )
                        staged.append({"status": "created", **result})
                    except Rejected as exc:
                        self.tracer.log("extract", "blocked", reason=exc.reason)
                        dropped.append({"action": "blocked", "reason": exc.reason})
                count += 1
            return count

        pref_left = min(cap_pref - used_pref, cap_total - used_total)
        if pref_left > 0:
            run("preference", "extract_preference", ["preference", "boundary"], "auto_extract", pref_left)
        used_total = self.store.candidate_count_since(scope.folder_id, day_start)
        event_left = min(cap_event - used_event, cap_total - used_total)
        if EVENT_SIGNALS.search(user_text or "") and event_left > 0:
            run("event", "extract_event", ["event"], "event_extract", event_left)

        summary = {
            "ok": True,
            "staged": len([s for s in staged if s.get("status") in ("pending", "blocked")]),
            "created": len([s for s in staged if s.get("status") == "created"]),
            "blocked": len([s for s in staged if s.get("status") == "blocked"]),
            "dropped": len(dropped),
            "candidates": staged,
        }
        self.tracer.log(
            "extract", "done", staged=summary["staged"], created=summary["created"],
            blocked=summary["blocked"], dropped=summary["dropped"],
        )
        return summary
