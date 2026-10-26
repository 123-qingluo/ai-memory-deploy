"""SQLite 连接、建表与事务。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.fts_tokenizer = ""

    # ------------------------------------------------------------- schema
    def init(self) -> None:
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        self.conn.executescript(schema)
        self._ensure_fts()
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        else:
            found = int(row["value"])
            if found > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {found} 高于本代码支持的 {SCHEMA_VERSION}，请升级代码而不是降级数据"
                )
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('fts_tokenizer', ?)",
            (self.fts_tokenizer,),
        )

    def _ensure_fts(self) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
        if exists:
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key='fts_tokenizer'"
            ).fetchone()
            self.fts_tokenizer = row["value"] if row else "existing"
            return
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5("
                "memory_id UNINDEXED, title, content, tokenize='trigram')"
            )
            self.fts_tokenizer = "trigram"
        except sqlite3.OperationalError:
            self.conn.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5("
                "memory_id UNINDEXED, title, content, tokenize='unicode61')"
            )
            self.fts_tokenizer = "unicode61"

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------- helpers
    def execute(self, sql: str, params=()):
        return self.conn.execute(sql, params)

    def fetchall(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchall()

    def fetchone(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchone()

    def table_names(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
        return {r["name"] for r in rows}
