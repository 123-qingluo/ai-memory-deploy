"""配置加载：memory.config.json 是唯一配置源，相对路径以部署根目录为基准。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "db_path": "data/memory.db",
    "trace_dir": "traces",
    "report_dir": "reports",
    "candidates_path": "data/candidates.jsonl",
    "sensitive_path": "sensitive.json",
    "eval_path": "eval/queries.json",
    "embedding": {
        "provider": "hashing",          # hashing（离线，仅验收/应急） | http（生产）
        "base_url": "http://127.0.0.1:8911/v1",
        "api_key": "",
        "model": "bge-small-zh-v1.5",
        "dim": 512,
        "timeout_s": 15,
    },
    "llm": {
        "provider": "none",             # none（关闭抽取） | http
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "timeout_s": 30,
    },
    "retrieval": {
        "top_k": 5,
        "prefetch_count": 3,
        "prefetch_summary_chars": 160,
        "min_score_hashing": 0.28,
        "min_score_http": 0.60,
        "result_dedupe": 0.88,
        "fts_candidates_factor": 3,
        "tag_core_bonus": 0.12,
        "tag_diffusion_bonus": 0.04,
        "tag_opposing_penalty": 0.25,
        "rerank": {"enabled": False, "endpoint": "", "timeout_s": 10},
    },
    "write": {
        "min_user_chars": 40,
        "dedupe_similarity": 0.90,
        "novelty_merge_cutoff": 0.10,
        "merge_similarity_floor": 0.50,
        "merge_max_chars": 800,
        "daily_cap_preference": 10,
        "daily_cap_event": 5,
        "daily_cap_total": 10,
        "shadow": True,
        "allow_group_extract": False,
        "max_title_chars": 20,
        "evidence_min_chars": 4,
    },
    "lifecycle": {
        "archive_after_days": 90,
        "activation_archive_threshold": 1,
        "dry_run": True,
        "allow_hard_delete": False,
        "pin_tag": "pin",
        "daily_hour": 4,
    },
    "prefetch": {
        "dedupe_window_s": 120,
        "max_query_chars": 200,
        "multi_topic_split": True,
        "allow_group": False,
    },
    "reminder": {
        "active_titles_limit": 10,
    },
    "memo": {
        "default_expire_hours": 2,
    },
    "supervision": {
        "rounds_without_tool_warning": 20,
        "cooldown_minutes": 30,
        "daily_max": 3,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(root: str | Path, overrides: dict | None = None) -> dict:
    root = Path(root)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = root / "memory.config.json"
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            cfg = deep_merge(cfg, json.load(fh))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg


def resolve(root: str | Path, relative: str) -> Path:
    p = Path(relative)
    return p if p.is_absolute() else Path(root) / p


def save_config(root: str | Path, cfg: dict) -> Path:
    path = Path(root) / "memory.config.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path
