-- XODUS storage schema, migration 0001 (SQLite + FTS5 + sqlite-vec).
-- Format: statements after "-- +up" are applied, statements after "-- +down" undo them.
-- {{EMBEDDING_DIM}} is replaced by embedding.dimensions from the configuration; the same value
-- is stored in meta so a later configuration that disagrees with the database is caught at startup.
-- Timestamps are Unix seconds (REAL). Nothing in this file may be edited after it has been applied:
-- the runner stores a checksum and refuses to start if it changes. Add a new migration instead.

-- +up
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
INSERT INTO meta(key, value) VALUES ('embedding_dim', '{{EMBEDDING_DIM}}');

CREATE TABLE collections (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 64),
  description TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL
);

-- One row per source file. Deleting the collection or the document cascades to everything below.
CREATE TABLE documents (
  id              INTEGER PRIMARY KEY,
  collection_id   INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  source_root     TEXT NOT NULL,
  source_path     TEXT NOT NULL,
  title           TEXT NOT NULL,
  format          TEXT NOT NULL CHECK (format IN ('markdown', 'text', 'csv', 'pdf', 'docx')),
  size_bytes      INTEGER NOT NULL,
  content_hash    TEXT NOT NULL,
  chunker_version INTEGER NOT NULL,
  encoding        TEXT,
  status          TEXT NOT NULL CHECK (status IN ('ingesting', 'ready', 'failed', 'cancelled')),
  error           TEXT,
  chunks_done     INTEGER NOT NULL DEFAULT 0,
  pages           INTEGER,
  created_by      TEXT NOT NULL DEFAULT 'user' CHECK (created_by IN ('user', 'model')),
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  UNIQUE (collection_id, source_root, source_path)
);
CREATE INDEX documents_status ON documents(status);

-- Chunks never change after they are written (a changed file is re-ingested from scratch).
CREATE TABLE chunks (
  id             INTEGER PRIMARY KEY,
  document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal        INTEGER NOT NULL,
  text           TEXT NOT NULL,
  text_hash      TEXT NOT NULL,
  heading_path   TEXT NOT NULL DEFAULT '',
  locator        TEXT,
  page           INTEGER,
  char_start     INTEGER,
  char_end       INTEGER,
  token_estimate INTEGER NOT NULL,
  UNIQUE (document_id, ordinal)
);
CREATE TRIGGER chunks_immutable BEFORE UPDATE OF document_id, ordinal, text, text_hash, heading_path ON chunks
BEGIN
  SELECT RAISE(ABORT, 'chunks are immutable');
END;

-- Keyword index (external content: the text lives once, in chunks). secure-delete makes FTS5 erase the
-- index entries of deleted rows instead of leaving them in old segments.
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, heading_path,
  content = 'chunks', content_rowid = 'id',
  tokenize = 'unicode61 remove_diacritics 2'
);
INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('secure-delete', 1);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text, heading_path) VALUES (new.id, new.text, new.heading_path);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text, heading_path) VALUES ('delete', old.id, old.text, old.heading_path);
END;

-- Vector index: rowid = chunks.id. Virtual tables take no foreign keys, so the code deletes vectors
-- explicitly, in the same transaction that deletes their chunks.
CREATE VIRTUAL TABLE chunk_vectors USING vec0(
  embedding float[{{EMBEDDING_DIM}}] distance_metric = cosine,
  collection_id integer
);

-- Durable ingestion queue: one source file per job. A job is leased by the worker that claims it
-- (claimed_by = "pid:create_time") and returns to the queue if that worker dies.
CREATE TABLE ingestion_jobs (
  id               INTEGER PRIMARY KEY,
  collection_id    INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  root_id          TEXT NOT NULL,
  rel_path         TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  outcome          TEXT CHECK (outcome IN ('ingested', 'replaced', 'unchanged')),
  error_code       TEXT,
  error            TEXT,
  attempts         INTEGER NOT NULL DEFAULT 0,
  claimed_by       TEXT,
  heartbeat_at     REAL,
  document_id      INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  chunks_done      INTEGER NOT NULL DEFAULT 0,
  created_by       TEXT NOT NULL DEFAULT 'user',
  created_at       REAL NOT NULL,
  started_at       REAL,
  finished_at      REAL
);
CREATE INDEX ingestion_jobs_status ON ingestion_jobs(status, id);

-- Long-term memory. Model-derived items start as 'proposed' and only a user decision makes them 'approved'.
CREATE TABLE memory_items (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('note', 'fact', 'preference')),
  content        TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 10000),
  status         TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected')),
  created_by     TEXT NOT NULL CHECK (created_by IN ('user', 'model')),
  source         TEXT NOT NULL,
  document_id    INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  model          TEXT,
  prompt_version TEXT,
  edited_by_user INTEGER NOT NULL DEFAULT 0,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL,
  decided_at     REAL,
  CHECK (created_by = 'user' OR (model IS NOT NULL AND prompt_version IS NOT NULL)),
  CHECK (NOT (created_by = 'model' AND status IN ('approved', 'rejected') AND decided_at IS NULL))
);
CREATE INDEX memory_items_status ON memory_items(status, kind);

-- Retrieval metadata. The query text is never stored, only its hash and length.
CREATE TABLE retrieval_log (
  id          INTEGER PRIMARY KEY,
  ts          REAL NOT NULL,
  request_id  TEXT,
  origin      TEXT NOT NULL,
  query_hash  TEXT NOT NULL,
  query_chars INTEGER NOT NULL,
  collections TEXT NOT NULL,
  mode        TEXT NOT NULL,
  n_vector    INTEGER NOT NULL,
  n_fts       INTEGER NOT NULL,
  n_returned  INTEGER NOT NULL,
  chunk_ids   TEXT NOT NULL,
  sufficient  INTEGER NOT NULL,
  embed_ms    REAL,
  vector_ms   REAL,
  fts_ms      REAL,
  total_ms    REAL NOT NULL
);

-- +down
DROP TABLE retrieval_log;
DROP TABLE memory_items;
DROP TABLE ingestion_jobs;
DROP TABLE chunk_vectors;
DROP TRIGGER chunks_ad;
DROP TRIGGER chunks_ai;
DROP TABLE chunks_fts;
DROP TRIGGER chunks_immutable;
DROP TABLE chunks;
DROP TABLE documents;
DROP TABLE collections;
DROP TABLE meta;
