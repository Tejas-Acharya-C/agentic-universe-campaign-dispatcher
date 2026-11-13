"""Tests for 503/504 handling, lost webhooks, crash recovery, and final CSV reporting."""

import csv
from datetime import datetime
import os
import pytest

from solution.app.clock import FakeClock, IST
from solution.app.config import Settings
from solution.app.database import get_connection, init_db, write_tx
from solution.app.dispatcher import Dispatcher
from solution.app.loader import load_fixtures
from solution.app.recovery import in_flight_requests, reconcile_attempts
from solution.app.reporting import is_all_leads_terminal, write_final_state_csv
from solution.app.state_machine import reserve_call


class MockTelephonyClient:
    """Mock TelephonyClient for deterministic unit tests."""

    def __init__(self):
        self.calls_by_rid = {}
        self.calls_by_sid = {}
        self.place_call_status = 201
        self.place_call_data = None
        self.clock_payload = {
            "sim_now": "2026-11-12T17:30:00+05:30",
            "sim_epoch": "2026-11-12T17:30:00+05:30",
            "real_epoch": 1000.0,
            "speed": 600.0,
        }

    async def get_clock(self):
        return self.clock_payload

    async def place_call(self, payload):
        rid = payload["request_id"]
        if self.place_call_status in (200, 201):
            data = self.place_call_data or {
                "call_sid": f"CA_{rid[:8]}",
                "request_id": rid,
                "campaign_id": payload["campaign_id"],
                "lead_id": payload["lead_id"],
                "state": "active",
                "created_at": "2026-11-12T17:30:00+05:30",
            }
            self.calls_by_rid[rid] = data
            self.calls_by_sid[data["call_sid"]] = data
            return self.place_call_status, data
        return self.place_call_status, self.place_call_data

    async def get_call_by_request_id(self, request_id: str):
        if request_id in self.calls_by_rid:
            return 200, self.calls_by_rid[request_id]
        return 404, None

    async def get_call_by_sid(self, call_sid: str):
        if call_sid in self.calls_by_sid:
            return 200, self.calls_by_sid[call_sid]
        return 404, None

    async def close(self):
        pass


@pytest.fixture
def populated_db(tmp_path):
    """Fixture providing initialized SQLite database with loaded fixtures."""
    db_file = str(tmp_path / "rec_test.db")
    conn = get_connection(db_file)
    init_db(conn)

    fixtures_path = "assignment-b-workflow/fixtures/campaigns.json"
    if not os.path.exists(fixtures_path):
        fixtures_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "assignment-b-workflow", "fixtures", "campaigns.json"
        )
    load_fixtures(conn, fixtures_path)
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_503_abort_and_restore_lead(populated_db):
    """
    Test that a 503 response from provider aborts PENDING_DISPATCH
    and restores lead to FRESH without incrementing attempts_count.
    """
    conn = populated_db
    fake_clock = FakeClock("2026-11-12T17:30:00+05:30")
    settings = Settings()
    mock_telephony = MockTelephonyClient()
    mock_telephony.place_call_status = 503

    dispatcher = Dispatcher(conn, settings, fake_clock, mock_telephony)

    res = reserve_call(conn, "c_acme_renewals", "ren_0001", fake_clock.now())
    assert res is not None

    # Lead is currently IN_FLIGHT, attempt is PENDING_DISPATCH
    await dispatcher._dispatch_single(res)

    lead_row = conn.execute(
        "SELECT state, attempts_count, active_request_id FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0001';"
    ).fetchone()
    assert lead_row["state"] == "FRESH"
    assert lead_row["attempts_count"] == 0
    assert lead_row["active_request_id"] is None

    attempt_row = conn.execute(
        "SELECT state FROM call_attempts WHERE request_id = ?;", (res["request_id"],)
    ).fetchone()
    assert attempt_row["state"] == "ABORTED"


@pytest.mark.asyncio
async def test_pre_accept_504_reconciliation(populated_db):
    """
    Test pre-accept 504 (carrier dropped request before creating call):
    GET /v1/calls?request_id=... returns 404 -> attempt aborted -> lead restored.
    """
    conn = populated_db
    fake_clock = FakeClock("2026-11-12T17:30:00+05:30")
    settings = Settings()
    mock_telephony = MockTelephonyClient()
    mock_telephony.place_call_status = 504

    dispatcher = Dispatcher(conn, settings, fake_clock, mock_telephony)

    res = reserve_call(conn, "c_acme_renewals", "ren_0002", fake_clock.now())
    assert res is not None
    rid = res["request_id"]

    await dispatcher._dispatch_single(res)

    # Attempt left in PENDING_DISPATCH, but in_flight_requests is empty
    assert rid not in in_flight_requests
    attempt_pending = conn.execute(
        "SELECT state FROM call_attempts WHERE request_id = ?;", (rid,)
    ).fetchone()
    assert attempt_pending["state"] == "PENDING_DISPATCH"

    # Reconciler runs: carrier returns 404 for this request_id
    rec_stats = await reconcile_attempts(conn, mock_telephony, fake_clock, startup=False)
    assert rec_stats["reconciled_pending"] == 1

    # Lead should be restored to FRESH with 0 attempts
    lead_row = conn.execute(
        "SELECT state, attempts_count, active_request_id FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0002';"
    ).fetchone()
    assert lead_row["state"] == "FRESH"
    assert lead_row["attempts_count"] == 0
    assert lead_row["active_request_id"] is None


@pytest.mark.asyncio
async def test_post_accept_504_reconciliation(populated_db):
    """
    Test post-accept 504 (carrier placed call but HTTP response timed out):
    GET /v1/calls?request_id=... returns 200 -> call confirmed accepted.
    """
    conn = populated_db
    fake_clock = FakeClock("2026-11-12T17:30:00+05:30")
    settings = Settings()
    mock_telephony = MockTelephonyClient()
    mock_telephony.place_call_status = 504

    dispatcher = Dispatcher(conn, settings, fake_clock, mock_telephony)

    res = reserve_call(conn, "c_acme_renewals", "ren_0003", fake_clock.now())
    rid = res["request_id"]

    # Carrier actually placed the call
    call_sid = "CA_POST_504"
    mock_telephony.calls_by_rid[rid] = {
        "call_sid": call_sid,
        "request_id": rid,
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0003",
        "state": "active",
        "created_at": "2026-11-12T17:30:00+05:30",
    }
    mock_telephony.calls_by_sid[call_sid] = mock_telephony.calls_by_rid[rid]

    await dispatcher._dispatch_single(res)

    # Reconciler runs: carrier returns 200
    rec_stats = await reconcile_attempts(conn, mock_telephony, fake_clock, startup=False)
    assert rec_stats["reconciled_pending"] == 1

    # Call should now be ACTIVE and lead attempts_count incremented to 1
    attempt_row = conn.execute(
        "SELECT state, accepted, call_sid FROM call_attempts WHERE request_id = ?;", (rid,)
    ).fetchone()
    assert attempt_row["state"] == "ACTIVE"
    assert attempt_row["accepted"] == 1
    assert attempt_row["call_sid"] == call_sid

    lead_row = conn.execute(
        "SELECT attempts_count FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0003';"
    ).fetchone()
    assert lead_row["attempts_count"] == 1


@pytest.mark.asyncio
async def test_lost_webhook_recovery(populated_db):
    """
    Test recovery of lost webhook on ACTIVE calls older than 410 sim seconds.
    """
    conn = populated_db
    fake_clock = FakeClock("2026-11-12T17:30:00+05:30")
    mock_telephony = MockTelephonyClient()

    # Reserve call and confirm accepted
    res = reserve_call(conn, "c_acme_renewals", "ren_0004", fake_clock.now())
    rid = res["request_id"]
    call_sid = "CA_LOST_WH"

    with write_tx(conn):
        conn.execute(
            "UPDATE call_attempts SET state = 'ACTIVE', accepted = 1, call_sid = ? WHERE request_id = ?;",
            (call_sid, rid),
        )
        conn.execute(
            "UPDATE leads SET attempts_count = 1 WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0004';"
        )

    # Advance clock by 500 sim seconds (> 410)
    fake_clock.advance(seconds=500)

    # Carrier has finished call with interested disposition, but webhook was lost
    result_data = {
        "call_sid": call_sid,
        "request_id": rid,
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0004",
        "phone": res["phone"],
        "status": "completed",
        "disposition": "interested",
        "ended_at": fake_clock.now().isoformat(),
        "duration_sec": 60,
    }
    mock_telephony.calls_by_sid[call_sid] = {
        "call_sid": call_sid,
        "request_id": rid,
        "state": "ended",
        "result": result_data,
    }

    rec_stats = await reconcile_attempts(conn, mock_telephony, fake_clock, startup=False)
    assert rec_stats["reconciled_active"] == 1

    # Lead should be transitioned to COMPLETED
    lead_row = conn.execute(
        "SELECT state, last_disposition FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0004';"
    ).fetchone()
    assert lead_row["state"] == "COMPLETED"
    assert lead_row["last_disposition"] == "interested"


@pytest.mark.asyncio
async def test_startup_crash_recovery(populated_db):
    """
    Test that startup=True reconciles orphaned PENDING_DISPATCH rows from a kill -9.
    """
    conn = populated_db
    fake_clock = FakeClock("2026-11-12T17:30:00+05:30")
    mock_telephony = MockTelephonyClient()

    # Simulate crash mid-dial: row in PENDING_DISPATCH, lead IN_FLIGHT
    rid = "req_crash_orphaned"
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO call_attempts (request_id, campaign_id, lead_id, tenant_id, phone, state, accepted, created_sim_at)
            VALUES (?, 'c_acme_renewals', 'ren_0005', 'acme_insurance', '+919999900000', 'PENDING_DISPATCH', 0, ?);
            """,
            (rid, fake_clock.now().isoformat()),
        )
        conn.execute(
            "UPDATE leads SET state = 'IN_FLIGHT', active_request_id = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0005';",
            (rid,),
        )

    # Carrier never received it (404)
    rec_stats = await reconcile_attempts(conn, mock_telephony, fake_clock, startup=True)
    assert rec_stats["reconciled_pending"] == 1

    lead_row = conn.execute(
        "SELECT state, active_request_id FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0005';"
    ).fetchone()
    assert lead_row["state"] == "FRESH"
    assert lead_row["active_request_id"] is None


def test_write_final_state_csv_and_terminal_detection(populated_db, tmp_path):
    """
    Test write_final_state_csv formatting, header, sorting, and is_all_leads_terminal check.
    """
    conn = populated_db

    # Initially leads are FRESH, so not all terminal
    assert is_all_leads_terminal(conn) is False

    # Mark all leads with valid terminal states
    with write_tx(conn):
        conn.execute(
            """
            UPDATE leads
            SET state = CASE
                WHEN lead_id LIKE '%01' THEN 'COMPLETED'
                WHEN lead_id LIKE '%02' THEN 'EXHAUSTED'
                ELSE 'DND_BLOCKED'
            END,
            attempts_count = 2,
            last_status = 'completed',
            last_disposition = 'interested';
            """
        )

    assert is_all_leads_terminal(conn) is True

    # If there is an active call attempt, it should return False
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO call_attempts (request_id, campaign_id, lead_id, tenant_id, phone, state, accepted, created_sim_at)
            VALUES ('req_test_act', 'c_acme_renewals', 'ren_0001', 'acme_insurance', '+919999', 'ACTIVE', 1, '2026-11-12T17:30:00+05:30');
            """
        )
    assert is_all_leads_terminal(conn) is False

    # Remove the active attempt
    with write_tx(conn):
        conn.execute("DELETE FROM call_attempts WHERE request_id = 'req_test_act';")
    assert is_all_leads_terminal(conn) is True

    # Test writing final_state.csv
    out_csv = str(tmp_path / "final_state.csv")
    rows_written = write_final_state_csv(conn, out_csv)
    assert rows_written == 200

    # Verify CSV file contents
    with open(out_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == [
            "campaign_id",
            "lead_id",
            "final_state",
            "attempts",
            "last_status",
            "last_disposition",
        ]

        rows = list(reader)
        assert len(rows) == 200

        # Check sorting: campaign_id ASC, lead_id ASC
        keys = [(r[0], r[1]) for r in rows]
        assert keys == sorted(keys)

        # Check final_state is mapped to lowercase
        for r in rows:
            assert r[2] in ("completed", "exhausted", "dnd_blocked")

