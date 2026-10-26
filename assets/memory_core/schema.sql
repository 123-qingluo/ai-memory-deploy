-- ai-memory-deploy / schema version 1
-- 设计来源：AI 角色记忆系统设计参考 §4（数据结构）
-- 约定：时间一律用 Unix 秒（REAL）；JSON 列存 TEXT；软删优先。

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_records (
  memory_id          TEXT PRIMARY KEY,
  folder_id          TEXT NOT NULL,                    -- 数据级隔离（会话/角色/租户）
  bucket             TEXT NOT NULL DEFAULT 'memory',   -- memory | knowledge
  source             TEXT NOT NULL,                    -- tool | auto_extract | event_extract | ingest
  memory_type        TEXT NOT NULL DEFAULT 'knowledge',-- person | event | knowledge | procedure
  status             TEXT NOT NULL DEFAULT 'active',   -- active | archived | expired
  title              TEXT NOT NULL,
  content            TEXT NOT NULL,
  evidence           TEXT NOT NULL DEFAULT '',
  evidence_verified  INTEGER NOT NULL DEFAULT 0,       -- 1 = evidence 是 source_text 的原话子串
  person_id          TEXT NOT NULL DEFAULT '',         -- person 类型必填（platform:id）
  relation_memory_ids TEXT NOT NULL DEFAULT '[]',
  relation_aliases   TEXT NOT NULL DEFAULT '[]',
  event_start_at     REAL NOT NULL DEFAULT 0,          -- 解析失败留 0，不拿消息时间冒充
  event_end_at       REAL NOT NULL DEFAULT 0,
  related_people     TEXT NOT NULL DEFAULT '[]',
  knowledge_type     TEXT NOT NULL DEFAULT '',         -- concept | model | quote | counterintuitive
  procedure_type     TEXT NOT NULL DEFAULT '',         -- process | tech | deploy | cooking
  address_or_coord   TEXT NOT NULL DEFAULT '',         -- 预留，不启用
  place_type         TEXT NOT NULL DEFAULT '',         -- 预留，不启用
  asset_type         TEXT NOT NULL DEFAULT '',         -- 预留，不启用
  disposition_status TEXT NOT NULL DEFAULT '',         -- 预留，不启用
  novelty_energy     REAL NOT NULL DEFAULT 1.0,
  is_archived        INTEGER NOT NULL DEFAULT 0,
  is_deleted         INTEGER NOT NULL DEFAULT 0,
  deleted_at         REAL NOT NULL DEFAULT 0,
  created_at         REAL NOT NULL,
  updated_at         REAL NOT NULL,
  last_activated_at  REAL NOT NULL DEFAULT 0,
  activation_count   INTEGER NOT NULL DEFAULT 0,
  search_hits        INTEGER NOT NULL DEFAULT 0,
  flashback_hits     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memory_scope
  ON memory_records (folder_id, bucket, is_deleted, is_archived);
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle
  ON memory_records (memory_type, status, last_activated_at);
CREATE INDEX IF NOT EXISTS idx_memory_person
  ON memory_records (person_id);
CREATE INDEX IF NOT EXISTS idx_memory_updated
  ON memory_records (folder_id, updated_at);

CREATE TABLE IF NOT EXISTS memory_tags (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT NOT NULL,
  tag_type  TEXT NOT NULL,   -- tag | core | diffusion | opposing
  tag_value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_memory ON memory_tags (memory_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tags
  ON memory_tags (memory_id, tag_type, tag_value);

CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id TEXT PRIMARY KEY,
  model     TEXT NOT NULL,
  dim       INTEGER NOT NULL,
  vec       BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS temporary_memos (
  memo_id     TEXT PRIMARY KEY,
  folder_id   TEXT NOT NULL,
  stream_id   TEXT NOT NULL DEFAULT '',
  content     TEXT NOT NULL,
  expires_at  REAL NOT NULL,
  consumed_at REAL NOT NULL DEFAULT 0,   -- ONCE 消费
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memo_scope
  ON temporary_memos (folder_id, consumed_at, expires_at);

CREATE TABLE IF NOT EXISTS extraction_audit (
  candidate_id  TEXT PRIMARY KEY,
  folder_id     TEXT NOT NULL,
  chat_type     TEXT NOT NULL DEFAULT '',
  person_id     TEXT NOT NULL DEFAULT '',
  source        TEXT NOT NULL,
  memory_type   TEXT NOT NULL,
  title         TEXT NOT NULL,
  content       TEXT NOT NULL,
  evidence      TEXT NOT NULL,
  source_text   TEXT NOT NULL,          -- 审批时重新校验 evidence
  payload       TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL,          -- pending | approved | rejected | blocked
  block_reason  TEXT NOT NULL DEFAULT '',
  warn_flags    TEXT NOT NULL DEFAULT '[]',
  result        TEXT NOT NULL DEFAULT '{}',
  created_at    REAL NOT NULL,
  decided_at    REAL NOT NULL DEFAULT 0,
  decision_note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_scope
  ON extraction_audit (folder_id, status, created_at);

CREATE TABLE IF NOT EXISTS processed_messages (
  message_id   TEXT PRIMARY KEY,
  folder_id    TEXT NOT NULL,
  processed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_messages (processed_at);

CREATE TABLE IF NOT EXISTS runtime_state (
  folder_id  TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (folder_id, key)
);
