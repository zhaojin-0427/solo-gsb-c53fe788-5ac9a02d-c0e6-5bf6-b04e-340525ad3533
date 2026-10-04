"""Database pool and schema management.

Schema design notes
-------------------
* ``messages`` rows are **immutable** and append-only.  Each row carries the
  revision at which it was imported.  Because rows are never updated, the
  state of every thread *as of any historical revision* can be derived by
  filtering ``revision <= R`` -- merges never rewrite history.
* ``revisions`` is an append-only audit log: one row per accepted import.
* ``mailboxes.head_revision`` is the mutable high-water mark, always updated
  inside the same transaction that inserts the revision + messages, so a
  rolled-back import never advances the revision sequence (no gaps).
"""

from __future__ import annotations

import time

from psycopg_pool import ConnectionPool

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailboxes (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    head_revision INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS revisions (
    mailbox_id BIGINT NOT NULL REFERENCES mailboxes(id),
    revision   INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    imported   JSONB NOT NULL,   -- message-ids first imported by this revision
    merges     JSONB NOT NULL,   -- [{old_thread_id, new_thread_id}]
    PRIMARY KEY (mailbox_id, revision)
);

CREATE TABLE IF NOT EXISTS messages (
    mailbox_id  BIGINT NOT NULL REFERENCES mailboxes(id),
    message_id  TEXT   NOT NULL,           -- normalized Message-ID (no angle brackets)
    parent_id   TEXT,                      -- resolved parent Message-ID, NULL = root
    in_reply_to JSONB  NOT NULL,           -- all ids found in In-Reply-To
    refs        JSONB  NOT NULL,           -- all ids found in References (order kept)
    sha256      TEXT   NOT NULL,           -- sha256 of the exact raw bytes received
    raw         BYTEA  NOT NULL,           -- original EML payload (archive)
    revision    INTEGER NOT NULL,          -- import revision (immutable)
    PRIMARY KEY (mailbox_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages (mailbox_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_revision ON messages (mailbox_id, revision);
"""

_SCHEMA_LOCK_KEY = 0x4D41494C  # "MAIL" -- serializes concurrent schema init

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.DATABASE_URL,
            min_size=1,
            max_size=8,
            kwargs={"autocommit": False},
            open=False,
        )
        _pool.open()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_schema(retries: int = 30, delay: float = 1.0) -> None:
    """Wait for the database to come up, then create the schema (idempotent)."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            pool = get_pool()
            with pool.connection() as conn:
                conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_KEY,))
                try:
                    conn.execute(SCHEMA)
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_KEY,))
            return
        except Exception as err:  # noqa: BLE001 - retry on anything (db not up yet)
            last_err = err
            time.sleep(delay)
    raise RuntimeError(f"database not ready after {retries} attempts: {last_err}")
