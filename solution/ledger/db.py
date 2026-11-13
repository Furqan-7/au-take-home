"""SQLite schema. Every statement is idempotent (IF NOT EXISTS)."""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- One row per input file we have fully ingested. A file is identified by its
-- name AND content hash, so re-running ingest on the same drop is a no-op.
-- The per-file counters feed data_quality.json.
CREATE TABLE IF NOT EXISTS ingested_files (
    file_key          TEXT NOT NULL,          -- e.g. leads/motor_renewal/batch_001.csv
    sha256            TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    ingested_at       TEXT NOT NULL,
    rows_total        INTEGER NOT NULL DEFAULT 0,  -- lead rows, or non-empty event lines
    rows_rejected     INTEGER NOT NULL DEFAULT 0,
    duplicates_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings    INTEGER NOT NULL DEFAULT 0,
    malformed_events  INTEGER NOT NULL DEFAULT 0,
    duplicate_events  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_key, sha256)
);

-- Current state of each lead after merging duplicates.
CREATE TABLE IF NOT EXISTS leads (
    lead_id      INTEGER PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    phone_hmac   TEXT NOT NULL,     -- keyed hash of +91XXXXXXXXXX, used for linking
    phone_last4  TEXT NOT NULL,     -- only what the masked phone needs
    external_ref TEXT NOT NULL,
    batch_id     TEXT NOT NULL,     -- batch of the first valid row; never changes
    fields_json  TEXT NOT NULL,     -- all schema fields; PII values are encrypted
    UNIQUE (workflow_id, phone_hmac)
);

CREATE TABLE IF NOT EXISTS rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

-- The interaction ledger. Append-only: the triggers below make UPDATE and
-- DELETE fail. Events are stored even if they can't be linked to a lead yet;
-- linking happens at report time so late-arriving leads pick them up.
CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,   -- dedupe key
    event_type        TEXT,
    workflow_id       TEXT,
    call_id           TEXT,
    occurred_at       TEXT,               -- ISO-8601 in IST, NULL if unparseable
    occurred_epoch_us INTEGER,            -- for ordering
    phone_hmac        TEXT,               -- NULL if missing or invalid phone
    disposition_code  TEXT,
    payload_json      TEXT NOT NULL,      -- original payload (contains no phone)
    source_file       TEXT NOT NULL,
    line_number       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_phone ON events (workflow_id, phone_hmac);
CREATE INDEX IF NOT EXISTS events_call ON events (workflow_id, call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;

-- Disposition catalogue, reloaded from dispositions.csv on every run.
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code        TEXT NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success  INTEGER NOT NULL CHECK (is_success IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
