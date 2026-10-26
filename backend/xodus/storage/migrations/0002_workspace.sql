-- XODUS storage schema, migration 0002 (Gate E): conversations, model switch records, ingestion pause flag.
-- Same rules as 0001: AUTOINCREMENT ids are never reused, timestamps are Unix seconds, and this file may not be
-- edited after it has been applied (add a new migration instead).
-- Memory facts derived from a conversation carry source 'conversation:<id>' and are deleted with it in code
-- (in the same transaction), so this migration does not alter memory_items and its down section stays clean.

-- +up
CREATE TABLE conversations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX conversations_updated ON conversations(updated_at);

-- One row per turn. An assistant reply is written once, when the backend knows how it ended: 'complete' only after
-- the model finished, 'partial' when text arrived before an error, 'error' with no text, 'cancelled' when stopped.
CREATE TABLE messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  mode            TEXT NOT NULL CHECK (mode IN ('chat', 'ask', 'task')),
  content         TEXT NOT NULL CHECK (length(content) <= 200000),
  status          TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'error', 'cancelled')),
  model           TEXT,
  request_id      TEXT,
  task_id         TEXT,
  meta            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta)),
  created_at      REAL NOT NULL
);
CREATE INDEX messages_conversation ON messages(conversation_id, id);

-- Every attempted model switch, including refusals. vram_* are device-wide NVML readings in bytes (NULL when NVML is
-- unavailable); the footprint shown in the UI is vram_after_load - vram_after_unload of the latest 'switched' row.
CREATE TABLE model_switches (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  from_profile      TEXT NOT NULL,
  to_profile        TEXT NOT NULL,
  from_model        TEXT NOT NULL,
  to_model          TEXT NOT NULL,
  num_ctx           INTEGER NOT NULL,
  outcome           TEXT NOT NULL CHECK (outcome IN ('switched', 'refused', 'failed')),
  code              TEXT,
  message           TEXT,
  vram_before       INTEGER,
  vram_after_unload INTEGER,
  vram_after_load   INTEGER,
  duration_ms       REAL NOT NULL,
  started_at        REAL NOT NULL
);
CREATE INDEX model_switches_profile ON model_switches(to_profile, outcome, id);

INSERT INTO meta(key, value) VALUES ('ingestion_paused', '0');

-- +down
DELETE FROM meta WHERE key = 'ingestion_paused';
DROP TABLE model_switches;
DROP TABLE messages;
DROP TABLE conversations;
