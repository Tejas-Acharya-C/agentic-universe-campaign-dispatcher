"""Tests for persistence layer, database initialization, and fixture loader."""

import json
import os
import sqlite3
import pytest

from solution.app.config import Settings
from solution.app.database import get_connection, init_db, write_tx
from solution.app.loader import load_fixtures


def test_settings_defaults_and_overrides(monkeypatch):
    """Test Settings defaults, environment overrides, and callback_url."""
    # Test defaults
    settings = Settings()
    assert settings.provider_url == "http://127.0.0.1:8099"
    assert settings.callback_host == "127.0.0.1"
    assert settings.callback_port == 8200
    assert settings.callback_path == "/webhooks/telephony"
    assert settings.callback_url == "http://127.0.0.1:8200/webhooks/telephony"
    assert settings.fixtures_path == "assignment-b-workflow/fixtures/campaigns.json"
    assert settings.db_path == "solution/dispatcher.db"
    assert settings.output_csv_path == "final_state.csv"
    assert settings.window_safety_margin_sec == 15

    # Test environment variable overrides
    monkeypatch.setenv("PROVIDER_URL", "http://provider.local:9000")
    monkeypatch.setenv("CALLBACK_HOST", "0.0.0.0")
    monkeypatch.setenv("CALLBACK_PORT", "8300")
    monkeypatch.setenv("FIXTURES_PATH", "custom/campaigns.json")
    monkeypatch.setenv("DB_PATH", "custom/db.sqlite")
    monkeypatch.setenv("OUTPUT_CSV_PATH", "custom_out.csv")
    monkeypatch.setenv("WINDOW_SAFETY_MARGIN_SEC", "30")

    env_settings = Settings()
    assert env_settings.provider_url == "http://provider.local:9000"
    assert env_settings.callback_host == "0.0.0.0"
    assert env_settings.callback_port == 8300
    assert env_settings.callback_url == "http://0.0.0.0:8300/webhooks/telephony"
    assert env_settings.fixtures_path == "custom/campaigns.json"
    assert env_settings.db_path == "custom/db.sqlite"
    assert env_settings.output_csv_path == "custom_out.csv"
    assert env_settings.window_safety_margin_sec == 30

    # Test CLI / constructor override
    cli_settings = Settings.from_cli(callback_port=9999)
    assert cli_settings.callback_port == 9999
    assert cli_settings.callback_url == "http://0.0.0.0:9999/webhooks/telephony"


def test_database_connection_and_pragmas(tmp_path):
    """Test SQLite connection creation and required pragmas."""
    db_file = str(tmp_path / "test.db")
    conn = get_connection(db_file)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert foreign_keys == 1

        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert busy_timeout == 5000

        synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
        # FULL is 2 in SQLite pragma synchronous
        assert synchronous in (2, "2", "FULL", "full")
    finally:
        conn.close()


def test_write_tx_commit_and_rollback(tmp_path):
    """Test write_tx committing on success and rolling back on error."""
    db_file = str(tmp_path / "tx_test.db")
    conn = get_connection(db_file)
    init_db(conn)

    # Success commit
    with write_tx(conn):
        conn.execute("INSERT INTO tenants (tenant_id, max_concurrent_calls) VALUES ('t1', 5);")

    row = conn.execute("SELECT max_concurrent_calls FROM tenants WHERE tenant_id = 't1';").fetchone()
    assert row is not None
    assert row["max_concurrent_calls"] == 5

    # Rollback on exception
    with pytest.raises(RuntimeError):
        with write_tx(conn):
            conn.execute("INSERT INTO tenants (tenant_id, max_concurrent_calls) VALUES ('t2', 10);")
            raise RuntimeError("Forced error for rollback")

    row2 = conn.execute("SELECT * FROM tenants WHERE tenant_id = 't2';").fetchone()
    assert row2 is None
    conn.close()


def test_load_fixtures_and_idempotency(tmp_path):
    """
    Test that load_fixtures loads 2 tenants, 3 campaigns, and 200 leads,
    and modifying a lead to COMPLETED preserves it across re-runs.
    """
    db_file = str(tmp_path / "fixtures_test.db")
    conn = get_connection(db_file)
    init_db(conn)

    fixtures_path = "assignment-b-workflow/fixtures/campaigns.json"
    if not os.path.exists(fixtures_path):
        # Fallback if run from a different relative root
        fixtures_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "assignment-b-workflow", "fixtures", "campaigns.json"
        )

    summary = load_fixtures(conn, fixtures_path)
    assert summary["tenants"] == 2
    assert summary["campaigns"] == 3
    assert summary["leads_total"] == 200

    # Verify system metadata keys
    meta_rows = {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM system_metadata;").fetchall()
    }
    assert "sim_start" in meta_rows
    assert meta_rows["timezone"] == "Asia/Kolkata"
    assert "retry_policy" in meta_rows
    assert "webhook_secret" in meta_rows

    # Verify a lead exists and modify its state to COMPLETED
    sample_lead = conn.execute(
        "SELECT campaign_id, lead_id, state FROM leads LIMIT 1;"
    ).fetchone()
    assert sample_lead is not None
    assert sample_lead["state"] == "FRESH"

    camp_id = sample_lead["campaign_id"]
    lead_id = sample_lead["lead_id"]

    with write_tx(conn):
        conn.execute(
            "UPDATE leads SET state = 'COMPLETED', attempts_count = 1 WHERE campaign_id = ? AND lead_id = ?;",
            (camp_id, lead_id),
        )

    modified_lead = conn.execute(
        "SELECT state, attempts_count FROM leads WHERE campaign_id = ? AND lead_id = ?;",
        (camp_id, lead_id),
    ).fetchone()
    assert modified_lead["state"] == "COMPLETED"
    assert modified_lead["attempts_count"] == 1

    # Re-run load_fixtures to verify idempotency
    summary_rerun = load_fixtures(conn, fixtures_path)
    assert summary_rerun["tenants"] == 2
    assert summary_rerun["campaigns"] == 3
    assert summary_rerun["leads_total"] == 200

    lead_after_rerun = conn.execute(
        "SELECT state, attempts_count FROM leads WHERE campaign_id = ? AND lead_id = ?;",
        (camp_id, lead_id),
    ).fetchone()
    assert lead_after_rerun["state"] == "COMPLETED"
    assert lead_after_rerun["attempts_count"] == 1

    conn.close()


def test_dnd_suppression_on_loader(tmp_path):
    """Test that existing phone_suppressions marks active leads as DND_BLOCKED."""
    db_file = str(tmp_path / "suppression_test.db")
    conn = get_connection(db_file)
    init_db(conn)

    fixtures_path = "assignment-b-workflow/fixtures/campaigns.json"
    if not os.path.exists(fixtures_path):
        fixtures_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "assignment-b-workflow", "fixtures", "campaigns.json"
        )

    # First load
    load_fixtures(conn, fixtures_path)

    # Pick a phone from leads
    lead_row = conn.execute("SELECT phone, campaign_id, lead_id FROM leads WHERE state = 'FRESH' LIMIT 1;").fetchone()
    target_phone = lead_row["phone"]

    # Insert into phone_suppressions
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO phone_suppressions (phone, reason, source_campaign_id, source_lead_id, suppressed_at)
            VALUES (?, 'dnd', ?, ?, '2026-11-12T18:00:00+05:30');
            """,
            (target_phone, lead_row["campaign_id"], lead_row["lead_id"]),
        )

    # Run loader again
    load_fixtures(conn, fixtures_path)

    suppressed_lead = conn.execute(
        "SELECT state FROM leads WHERE phone = ?;", (target_phone,)
    ).fetchone()
    assert suppressed_lead["state"] == "DND_BLOCKED"

    conn.close()

