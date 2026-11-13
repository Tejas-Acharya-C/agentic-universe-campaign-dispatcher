"""Database persistence layer with SQLite WAL mode and transaction management."""

from contextlib import contextmanager
import os
import sqlite3

DDL_STATEMENTS = """
CREATE TABLE IF NOT EXISTS system_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    max_concurrent_calls INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    leads_file TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    window_days TEXT NOT NULL,
    max_attempts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    lead_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    phone TEXT NOT NULL,
    customer_name TEXT,
    state TEXT NOT NULL DEFAULT 'FRESH',
    attempts_count INTEGER NOT NULL DEFAULT 0,
    next_eligible_at TEXT,
    callback_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    last_disposition TEXT NOT NULL DEFAULT '',
    last_ended_at TEXT,
    active_request_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (campaign_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_scheduling ON leads(state, next_eligible_at);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);

CREATE TABLE IF NOT EXISTS phone_suppressions (
    phone TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT 'dnd',
    source_campaign_id TEXT,
    source_lead_id TEXT,
    suppressed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_attempts (
    request_id TEXT PRIMARY KEY,
    call_sid TEXT UNIQUE,
    campaign_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    state TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    created_sim_at TEXT NOT NULL,
    ended_sim_at TEXT,
    status TEXT,
    disposition TEXT,
    callback_at TEXT,
    duration_sec INTEGER,
    FOREIGN KEY (campaign_id, lead_id) REFERENCES leads(campaign_id, lead_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_call_per_phone ON call_attempts(phone) WHERE state IN ('PENDING_DISPATCH', 'ACTIVE');
CREATE INDEX IF NOT EXISTS idx_attempts_tenant_active ON call_attempts(tenant_id, state) WHERE state IN ('PENDING_DISPATCH', 'ACTIVE');

CREATE TABLE IF NOT EXISTS webhook_events (
    call_sid TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    raw_body TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and required pragmas."""
    if db_path != ":memory:" and not db_path.startswith("file:"):
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")

    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Context manager for write transactions using BEGIN IMMEDIATE."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection) -> None:
    """Create all required tables and indexes if they do not already exist."""
    conn.executescript(DDL_STATEMENTS)

