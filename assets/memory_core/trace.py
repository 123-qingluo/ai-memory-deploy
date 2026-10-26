"""trace：每次检索/抽取/清理/工具调用留一行 JSONL。

没有 trace，后续调参都是猜。按天分文件，失败只吞掉写日志的错误，不影响主流程。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path


class Tracer:
    def __init__(self, trace_dir: str | Path, enabled: bool = True):
        self.dir = Path(trace_dir)
        self.enabled = enabled
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, status: str = "ok", **fields) -> None:
        if not self.enabled:
            return
        now = time.time()
        record = {
            "ts": round(now, 3),
            "iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "event": event,
            "status": status,
        }
        record.update(fields)
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        try:
            with (self.dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def summarize(self, days: int = 7) -> dict:
        if not self.enabled or not self.dir.exists():
            return {}
        cutoff = time.time() - days * 86400
        counts: dict[str, dict[str, int]] = {}
        for path in sorted(self.dir.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if item.get("ts", 0) < cutoff:
                            continue
                        key = f"{item.get('event')}:{item.get('status')}"
                        counts[key] = counts.get(key, 0) + 1
            except OSError:
                continue
        return counts
