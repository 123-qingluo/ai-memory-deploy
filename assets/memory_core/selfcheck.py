"""自检：在一份临时库里跑通写入、闸门、读取、生命周期与离线评测。

自检永不碰部署库；它证明的是"这套代码在这台机器上行为正确"。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path

from .config import DEFAULT_CONFIG, deep_merge
from .models import Scope
from .extract import parse_time_hint


class StubLLM:
    available = True
    model = "stub"

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system[:60], user[:60]))
        return self.responses.pop(0) if self.responses else "[]"


def _triple(tags: str, opposing: str = "") -> str:
    core = ",".join([t.strip() for t in (tags or "").split(",") if t.strip()]) or "-"
    opp = ",".join([t.strip() for t in (opposing or "").split(",") if t.strip()]) or "-"
    return f"{core}|-|{opp}"


def evaluate_in_temp(base_cfg: dict | None, root: str | Path, dataset_path: str | Path | None = None, top_k: int = 3) -> dict:
    """在临时库里跑评测，避免把 seed 写进部署库。"""
    temp_root = Path(tempfile.mkdtemp(prefix="aimem-eval-"))
    try:
        source_root = Path(root).resolve()
        for name in ("prompts", "eval"):
            if (source_root / name).exists():
                shutil.copytree(source_root / name, temp_root / name)
        if (source_root / "sensitive.json").exists():
            shutil.copy2(source_root / "sensitive.json", temp_root / "sensitive.json")
        cfg = deep_merge(DEFAULT_CONFIG, base_cfg or {})
        cfg.update(
            {
                "db_path": "data/memory.db",
                "trace_dir": "traces",
                "report_dir": "reports",
                "candidates_path": "data/candidates.jsonl",
            }
        )
        from .api import MemorySystem

        system = MemorySystem(temp_root, cfg)
        try:
            path = Path(dataset_path) if dataset_path else temp_root / "eval" / "queries.json"
            return evaluate_dataset(system, path, top_k=top_k)
        finally:
            system.close()
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _short(text: str, limit: int = 80) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _check_system(system, checks: list, name: str, fn) -> None:
    started = time.time()
    try:
        result = fn()
        if isinstance(result, tuple):
            ok, detail = result
        elif isinstance(result, dict):
            ok, detail = bool(result.get("ok")), result.get("detail", "")
        else:
            ok, detail = bool(result), ""
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=2)}"
    checks.append(
        {
            "name": name,
            "ok": bool(ok),
            "detail": _short(detail, 300),
            "ms": int((time.time() - started) * 1000),
        }
    )


def evaluate_dataset(system, path: str | Path, top_k: int = 3) -> dict:
    path = Path(path)
    if not path.exists():
        return {"ok": False, "detail": f"缺少评测集：{path}", "hit_rate": 0.0, "total": 0}
    dataset = json.loads(path.read_text(encoding="utf-8"))
    folder = f"eval-{int(time.time())}"
    scope = Scope(folder_id=folder, person_id="selfcheck")
    errors = []
    for index, seed in enumerate(dataset.get("seed", [])):
        result = system.create(
            scope,
            title=seed["title"],
            content=seed["content"],
            memory_type=seed.get("type", "knowledge"),
            bucket="memory",
            source="ingest",
            tags=[t for t in (seed.get("tags", "") or "").split(",") if t],
            triple_tags=_triple(seed.get("tags", ""), seed.get("opposing", "")),
            evidence=seed["title"],
            source_text=seed["title"],
        )
        if not result.get("ok") and result.get("rejected") != "duplicate":
            errors.append({"seed": seed["title"], "error": result})

    hits, misses = 0, []
    for item in dataset.get("queries", []):
        results = system.search(scope, item["q"], top_k=top_k, include_knowledge=True)
        blob = "\n".join((r["title"] + " " + r.get("summary", "")) for r in results)
        if item["expect"] in blob:
            hits += 1
        else:
            misses.append({"q": item["q"], "expect": item["expect"], "got": [r["title"] for r in results]})
    total = len(dataset.get("queries", []))
    hit_rate = round(hits / total, 4) if total else 0.0
    return {
        "ok": total > 0 and hit_rate >= 0.60,
        "hit_rate": hit_rate,
        "hits": hits,
        "total": total,
        "top_k": top_k,
        "misses": misses[:8],
        "seed_errors": errors[:5],
    }


def run_selfcheck(base_cfg: dict | None = None, keep_temp: bool = False, root: str | Path | None = None) -> dict:
    started = time.time()
    base_cfg = deep_merge(DEFAULT_CONFIG, base_cfg or {})
    temp_root = Path(tempfile.mkdtemp(prefix="aimem-selfcheck-"))
    checks: list[dict] = []
    eval_report: dict = {}
    try:
        source_root = Path(root or base_cfg.get("__root__") or ".").resolve()
        for name in ("prompts", "eval"):
            if (source_root / name).exists():
                shutil.copytree(source_root / name, temp_root / name)
        if (source_root / "sensitive.json").exists():
            shutil.copy2(source_root / "sensitive.json", temp_root / "sensitive.json")

        overrides = {
            "db_path": "data/memory.db",
            "trace_dir": "traces",
            "report_dir": "reports",
            "candidates_path": "data/candidates.jsonl",
            "embedding": {"provider": "hashing", "dim": 512},
            "llm": {"provider": "none"},
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
            "retrieval": {
                "top_k": 5,
                "prefetch_count": 3,
                "min_score_hashing": 0.28,
                "result_dedupe": 0.88,
            },
            "prefetch": {"dedupe_window_s": 120, "allow_group": False},
            "lifecycle": {
                "dry_run": True,
                "allow_hard_delete": False,
                "archive_after_days": 90,
                "activation_archive_threshold": 1,
                "pin_tag": "pin",
            },
        }
        cfg = deep_merge(base_cfg, overrides)
        cfg.pop("__root__", None)

        from .api import MemorySystem

        stub = StubLLM(
            [
                json.dumps(
                    [
                        {
                            "type": "preference",
                            "title": "主人乳糖不耐",
                            "content": "主人喝牛奶会肚子疼，点奶茶要换燕麦奶",
                            "evidence": "一喝牛奶就肚子疼",
                            "tags": ["牛奶", "饮食"],
                            "opposing": ["咖啡"],
                        },
                        {
                            "type": "preference",
                            "title": "不存在的偏好",
                            "content": "主人喜欢跑步",
                            "evidence": "我从没说过这句话",
                            "tags": [],
                            "opposing": [],
                        },
                    ],
                    ensure_ascii=False,
                ),
                "[]",
            ]
        )
        system = MemorySystem(temp_root, cfg, llm=stub)
        scope = Scope(folder_id="selfcheck-main", person_id="qq:10001")
        scope_b = Scope(folder_id="selfcheck-other", person_id="qq:10001")
        scope_pref = Scope(folder_id="selfcheck-prefetch", person_id="qq:10001")
        scope_life = Scope(folder_id="selfcheck-life", person_id="qq:10001")

        def check_health():
            report = system.health()
            return report["ok"], f"schema={report['schema_version']} fts={report['fts_tokenizer']} probe={report['write_probe']}"

        def check_create_read():
            created = system.create(
                scope,
                title="主人不吃香菜",
                content="主人不吃香菜，点餐时要避开香菜。",
                memory_type="knowledge",
                triple_tags="香菜,饮食|点餐,外卖|随便,都行",
                tags=["关键"],
                evidence="我不吃香菜",
                source_text="我不吃香菜，点餐别放。",
            )
            assert created.get("ok"), created
            loaded = system.read(scope, created["id"])
            assert loaded and loaded["title"] == "主人不吃香菜"
            assert loaded["evidence_verified"] is True
            assert "香菜" in loaded["tags"]["core"]
            return True, f"id={created['id']} tags={loaded['tags']}"

        def check_evidence_gate():
            result = system.create(
                scope,
                title="编造的记忆",
                content="主人喜欢下雨天。",
                triple_tags="天气|-|-",
                evidence="这不是原话",
                source_text="今天天气不错。",
            )
            assert result.get("rejected") == "evidence_mismatch", result
            return True, "evidence 非原话子串被拒"

        def check_duplicate_gate():
            first = system.create(
                scope,
                title="主人怕冷",
                content="主人生怕冷，夏天空调也只开 26 度。",
                triple_tags="怕冷,空调|-|-",
            )
            assert first.get("ok"), first
            second = system.create(
                scope,
                title="主人怕冷",
                content="主人生怕冷，夏天空调也只开 26 度。",
                triple_tags="怕冷,空调|-|-",
            )
            assert second.get("rejected") == "duplicate", second
            return True, f"重复命中 {second.get('existing_id')}"

        def check_merge_path():
            wc = system.write_svc.write_cfg
            saved = (wc["dedupe_similarity"], wc["novelty_merge_cutoff"], wc["merge_similarity_floor"])
            wc["dedupe_similarity"], wc["novelty_merge_cutoff"], wc["merge_similarity_floor"] = 0.999, 0.50, 0.30
            try:
                base = system.create(
                    scope,
                    title="主人喜欢喝咖啡",
                    content="主人喜欢喝咖啡，每天早上都要一杯。",
                    triple_tags="咖啡,饮品|-|-",
                )
                assert base.get("ok"), base
                merged = system.create(
                    scope,
                    title="主人喜欢喝咖啡",
                    content="主人喜欢喝咖啡，每天早上都要一杯美式。",
                    triple_tags="咖啡,饮品|-|-",
                )
                assert merged.get("action") == "merged", merged
                loaded = system.read(scope, merged["memory_id"])
                assert "美式" in loaded["content"]
                return True, f"合并进 {merged['memory_id']}，novelty={merged['novelty_energy']}"
            finally:
                wc["dedupe_similarity"], wc["novelty_merge_cutoff"], wc["merge_similarity_floor"] = saved

        def check_sensitive_gate():
            result = system.create(scope, title="登录信息", content="我的密码是 abc123", triple_tags="密码|-|-")
            assert result.get("rejected") == "sensitive", result
            return True, "敏感规则拦截"

        def check_tag_gate():
            result = system.create(scope, title="标签测试", content="这是一条标签测试记忆。", triple_tags="只有一组")
            assert result.get("rejected") == "invalid_tags", result
            return True, "缺少三组标签被拒"

        def check_scope_isolation():
            system.create(scope, title="A 频道的秘密", content="A 频道的约定：周五晚上一起看电影。", triple_tags="约定,电影|-|-")
            found = system.search(scope_b, "周五晚上看电影")
            assert found == [], found
            assert system.read(scope_b, system.search(scope, "电影")[0]["id"]) is None
            return True, "folder 数据级隔离生效"

        def check_prefetch():
            system.create(
                scope_pref,
                title="主人乳糖不耐",
                content="主人乳糖不耐，喝牛奶会肚子疼，点奶茶要换燕麦奶。",
                triple_tags="牛奶,饮食|奶茶,点单|咖啡",
            )
            block = system.prefetch(scope_pref, "晚上想喝牛奶，可以吗", now=time.time())
            assert block["status"] == "injected", block
            assert 1 <= len(block["items"]) <= 3, block
            assert all(len(item["summary"]) <= 161 for item in block["items"])
            assert "不是全部" in block["text"]
            again = system.prefetch(scope_pref, "晚上想喝牛奶，可以吗", now=time.time() + 5)
            assert again["status"] != "injected", again
            return True, f"注入 {len(block['items'])} 条，二次调用被 {cfg['prefetch']['dedupe_window_s']}s 窗口去重"

        def check_reminder_memo():
            system.create(scope, title="主人生日", content="主人生日是 3 月 14 日。", triple_tags="生日,日期|-|-", tags=["pin"])
            block = system.reminder(scope, now=time.time())
            assert "不是全部" in block["text"], block
            memo = system.memo_set(scope, "正在帮主人查周末展览", expire_hours=1)
            assert memo["ok"]
            assert len(system.memo_list(scope)) == 1
            assert len(system.memo_consume(scope)) == 1
            assert len(system.memo_list(scope)) == 0
            return True, f"标题 {len(block['titles'])} 条；便签 ONCE 消费"

        def check_extraction():
            user_text = (
                "我最近发现自己一喝牛奶就肚子疼，可能是乳糖不耐，"
                "所以以后点奶茶都换成燕麦奶吧，别的都还好，就是这个问题一直挺困扰我的。"
            )
            report = system.extract_after_turn(scope, user_text, message_id="msg-selfcheck-1", now=time.time())
            assert report.get("staged") == 1, report
            assert report.get("dropped", 0) >= 1, report
            pending = system.candidates(scope)
            assert len(pending) >= 1, pending
            candidate = pending[0]
            assert candidate["evidence"] in candidate["source_text"]
            approved = system.approve_candidate(scope, candidate["candidate_id"])
            assert approved.get("ok"), approved
            loaded = system.read(scope, approved["memory_id"])
            assert "牛奶" in loaded["tags"]["core"]
            return True, f"候选 {candidate['candidate_id']} 审批入 {approved['memory_id']}"

        def check_time_parse():
            now = time.time()
            start, end = parse_time_hint("明天下午", now)
            assert start > now and end > start, (start, end)
            assert parse_time_hint("随便吧", now) == (0.0, 0.0)
            start2, _ = parse_time_hint("3月14日", now)
            assert start2 > 0
            return True, "相对时间解析成功；无法解析留 0"

        def check_lifecycle():
            now = time.time()
            old = now - 91 * 86400
            m_delete = system.create(scope_life, title="从未被想起的旧事", content="一条从未被检索过的旧记忆。", triple_tags="旧事|-|-")
            m_archive = system.create(scope_life, title="偶尔被想起的旧事", content="一条被检索过、但很久没更新的记忆。", triple_tags="旧事|-|-")
            m_pin = system.create(scope_life, title="必须保留的旧事", content="用 pin 标签保护起来的旧记忆。", triple_tags="旧事|-|-", tags=["pin"])
            system.store.update_record(m_delete["id"], scope_life.folder_id, {"updated_at": old})
            system.store.update_record(m_archive["id"], scope_life.folder_id, {"updated_at": old})
            system.store.update_record(m_pin["id"], scope_life.folder_id, {"updated_at": old})
            system.store.bump_activation([m_archive["id"]], now, search_hit=False)

            dry = system.run_lifecycle(scope=scope_life, dry_run=True, now=now)
            assert dry["counts"]["would_delete"] == 1, dry["counts"]
            assert dry["counts"]["would_archive"] == 1, dry["counts"]
            assert dry["counts"]["protected"] == 1, dry["counts"]
            assert system.read(scope_life, m_delete["id"])["status"] == "active"

            applied = system.run_lifecycle(scope=scope_life, dry_run=False, allow_hard_delete=False, now=now)
            assert applied["counts"]["archived"] == 1, applied["counts"]
            assert applied["counts"]["deleted"] == 0, applied["counts"]
            assert system.read(scope_life, m_archive["id"])["status"] == "archived"
            assert system.read(scope_life, m_delete["id"]) is not None

            purged = system.run_lifecycle(scope=scope_life, dry_run=False, allow_hard_delete=True, now=now)
            assert purged["counts"]["deleted"] == 1, purged["counts"]
            assert system.read(scope_life, m_delete["id"]) is None
            return True, "dry-run → 归档 → 硬删 三级行为符合预期"

        def check_eval():
            nonlocal eval_report
            eval_report = evaluate_dataset(system, temp_root / "eval" / "queries.json", top_k=3)
            return eval_report["ok"], f"Top3={eval_report['hit_rate']} ({eval_report['hits']}/{eval_report['total']})"

        for name, fn in [
            ("health", check_health),
            ("create_read", check_create_read),
            ("evidence_gate", check_evidence_gate),
            ("duplicate_gate", check_duplicate_gate),
            ("merge_path", check_merge_path),
            ("sensitive_gate", check_sensitive_gate),
            ("tag_gate", check_tag_gate),
            ("scope_isolation", check_scope_isolation),
            ("prefetch", check_prefetch),
            ("reminder_memo", check_reminder_memo),
            ("extraction_approval", check_extraction),
            ("time_parse", check_time_parse),
            ("lifecycle", check_lifecycle),
            ("eval_top3", check_eval),
        ]:
            _check_system(system, checks, name, fn)

        system.close()
    finally:
        if not keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)

    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "eval": eval_report,
        "passed": len([c for c in checks if c["ok"]]),
        "total": len(checks),
        "duration_ms": int((time.time() - started) * 1000),
        "temp_root": None if not keep_temp else str(temp_root),
    }
