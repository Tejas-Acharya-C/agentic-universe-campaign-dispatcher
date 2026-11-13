"""Tests for state machine, priority scheduler, and webhook receiver."""

from datetime import datetime
import hashlib
import hmac
import json
import os
import sqlite3
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from solution.app.clock import FakeClock, IST
from solution.app.database import get_connection, init_db, write_tx
from solution.app.loader import load_fixtures
from solution.app.scheduler import select_next_candidates
from solution.app.state_machine import (
    abort_unplaced_attempt,
    apply_call_result,
    confirm_call_accepted,
    reserve_call,
)
from solution.app.webhooks import router


@pytest.fixture
def populated_db(tmp_path):
    """Fixture providing an initialized database with loaded campaign fixtures."""
    db_file = str(tmp_path / "dispatcher_test.db")
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


def test_priority_ordering(populated_db):
    """
    Test that scheduler orders strictly: CALLBACK_WAIT > RETRY_WAIT > FRESH.
    """
    conn = populated_db
    sim_time = datetime(2026, 11, 12, 17, 30, tzinfo=IST)

    # Pick 3 different leads from c_acme_renewals
    leads = conn.execute(
        "SELECT lead_id FROM leads WHERE campaign_id = 'c_acme_renewals' LIMIT 3;"
    ).fetchall()
    l1, l2, l3 = leads[0]["lead_id"], leads[1]["lead_id"], leads[2]["lead_id"]

    with write_tx(conn):
        # Set all leads in c_acme_renewals to COMPLETED except our 3 test leads
        conn.execute(
            "UPDATE leads SET state = 'COMPLETED' WHERE campaign_id = 'c_acme_renewals' AND lead_id NOT IN (?, ?, ?);",
            (l1, l2, l3),
        )
        # Also complete all other campaigns so only these 3 are candidates
        conn.execute(
            "UPDATE leads SET state = 'COMPLETED' WHERE campaign_id != 'c_acme_renewals';"
        )

        # Set l1 as FRESH
        conn.execute(
            "UPDATE leads SET state = 'FRESH', next_eligible_at = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = ?;",
            (sim_time.isoformat(), l1),
        )
        # Set l2 as RETRY_WAIT
        conn.execute(
            "UPDATE leads SET state = 'RETRY_WAIT', attempts_count = 1, next_eligible_at = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = ?;",
            (sim_time.isoformat(), l2),
        )
        # Set l3 as CALLBACK_WAIT
        conn.execute(
            "UPDATE leads SET state = 'CALLBACK_WAIT', attempts_count = 1, next_eligible_at = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = ?;",
            (sim_time.isoformat(), l3),
        )

    candidates = select_next_candidates(conn, sim_time, limit=10)
    assert len(candidates) == 3

    # Priority 1: CALLBACK_WAIT (l3)
    assert candidates[0] == ("c_acme_renewals", l3)
    # Priority 2: RETRY_WAIT (l2)
    assert candidates[1] == ("c_acme_renewals", l2)
    # Priority 3: FRESH (l1)
    assert candidates[2] == ("c_acme_renewals", l1)


def test_tenant_concurrency_cap(populated_db):
    """
    Test tenant concurrency cap enforced across two campaigns sharing acme_insurance (limit 4).
    """
    conn = populated_db
    sim_time = datetime(2026, 11, 12, 17, 30, tzinfo=IST)

    # acme_insurance has max_concurrent_calls = 4
    # Reserve 3 calls across campaigns
    r1 = reserve_call(conn, "c_acme_renewals", "ren_0001", sim_time)
    r2 = reserve_call(conn, "c_acme_renewals", "ren_0002", sim_time)
    r3 = reserve_call(conn, "c_acme_feedback", "fb_0001", sim_time)
    assert r1 is not None and r2 is not None and r3 is not None

    # Only 1 slot left for acme_insurance!
    # Even if limit=10, scheduler should only select 1 candidate for acme_insurance
    # Disable zen_retail leads for this test
    with write_tx(conn):
        conn.execute("UPDATE leads SET state = 'COMPLETED' WHERE campaign_id = 'c_zen_winback';")

    candidates = select_next_candidates(conn, sim_time, limit=10)
    assert len(candidates) == 1

    # Now reserve the 4th call
    c_camp, c_lead = candidates[0]
    r4 = reserve_call(conn, c_camp, c_lead, sim_time)
    assert r4 is not None

    # Capacity is now exhausted (4/4 active/pending)
    candidates_after_full = select_next_candidates(conn, sim_time, limit=10)
    assert len(candidates_after_full) == 0


def test_same_phone_exclusion(populated_db):
    """
    Test that scheduler and reserve_call enforce one call per phone across campaigns.
    """
    conn = populated_db
    sim_time = datetime(2026, 11, 12, 17, 30, tzinfo=IST)
    test_phone = "+919999988888"

    # Insert two leads with the same phone in different campaigns
    with write_tx(conn):
        conn.execute(
            """
            UPDATE leads SET phone = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0003';
            """,
            (test_phone,),
        )
        conn.execute(
            """
            UPDATE leads SET phone = ? WHERE campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0002';
            """,
            (test_phone,),
        )
        # Mark all other leads completed
        conn.execute(
            """
            UPDATE leads SET state = 'COMPLETED'
            WHERE NOT (
                (campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0003') OR
                (campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0002')
            );
            """
        )

    candidates = select_next_candidates(conn, sim_time, limit=10)
    # Only one of them may be selected
    assert len(candidates) == 1
    selected_camp, selected_lead = candidates[0]

    # Reserve the selected one
    res = reserve_call(conn, selected_camp, selected_lead, sim_time)
    assert res is not None

    # Attempting to reserve the second one with the same phone must fail
    other_camp = "c_acme_feedback" if selected_camp == "c_acme_renewals" else "c_acme_renewals"
    other_lead = "fb_0002" if selected_camp == "c_acme_renewals" else "ren_0003"
    res_second = reserve_call(conn, other_camp, other_lead, sim_time)
    assert res_second is None


def test_global_dnd_propagation(populated_db):
    """
    Test global DND propagation and precedence (completed > exhausted > dnd_blocked).
    """
    conn = populated_db
    sim_time = datetime(2026, 11, 12, 17, 30, tzinfo=IST)
    phone_dnd = "+919111122222"

    with write_tx(conn):
        # Assign phone_dnd to 5 leads
        conn.execute("UPDATE leads SET phone = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0001';", (phone_dnd,))
        conn.execute("UPDATE leads SET phone = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0002';", (phone_dnd,))
        conn.execute("UPDATE leads SET phone = ? WHERE campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0001';", (phone_dnd,))
        conn.execute("UPDATE leads SET phone = ? WHERE campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0002';", (phone_dnd,))
        conn.execute("UPDATE leads SET phone = ? WHERE campaign_id = 'c_zen_winback' AND lead_id = 'zen_0001';", (phone_dnd,))

        # ren_0002 is FRESH
        conn.execute("UPDATE leads SET state = 'FRESH' WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0002';")
        # fb_0001 is COMPLETED
        conn.execute("UPDATE leads SET state = 'COMPLETED' WHERE campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0001';")
        # fb_0002 is EXHAUSTED
        conn.execute("UPDATE leads SET state = 'EXHAUSTED' WHERE campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0002';")
        # zen_0001 is IN_FLIGHT
        conn.execute("UPDATE leads SET state = 'IN_FLIGHT' WHERE campaign_id = 'c_zen_winback' AND lead_id = 'zen_0001';")

    # Reserve call for ren_0001
    res1 = reserve_call(conn, "c_acme_renewals", "ren_0001", sim_time)
    assert res1 is not None

    # ren_0001 ends with 'dnd'
    result_dnd = {
        "call_sid": "CA_DND_1",
        "request_id": res1["request_id"],
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0001",
        "phone": phone_dnd,
        "status": "completed",
        "disposition": "dnd",
        "ended_at": "2026-11-12T17:35:00+05:30",
        "duration_sec": 45,
    }
    applied = apply_call_result(conn, result_dnd, sim_now=sim_time)
    assert applied is True

    # Check states:
    # 1. ren_0001 is COMPLETED
    r1_row = conn.execute("SELECT state FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0001';").fetchone()
    assert r1_row["state"] == "COMPLETED"

    # 2. ren_0002 (was FRESH) is now DND_BLOCKED
    r2_row = conn.execute("SELECT state FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0002';").fetchone()
    assert r2_row["state"] == "DND_BLOCKED"

    # 3. fb_0001 (was COMPLETED) stayed COMPLETED
    fb1_row = conn.execute("SELECT state FROM leads WHERE campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0001';").fetchone()
    assert fb1_row["state"] == "COMPLETED"

    # 4. fb_0002 (was EXHAUSTED) stayed EXHAUSTED
    fb2_row = conn.execute("SELECT state FROM leads WHERE campaign_id = 'c_acme_feedback' AND lead_id = 'fb_0002';").fetchone()
    assert fb2_row["state"] == "EXHAUSTED"

    # 5. zen_0001 (was IN_FLIGHT):
    # If it finishes with terminal disposition 'interested', it becomes COMPLETED
    # Let's create an attempt record for zen_0001
    zen_req_id = "req_zen_test_1"
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO call_attempts (request_id, campaign_id, lead_id, tenant_id, phone, state, accepted, created_sim_at)
            VALUES (?, 'c_zen_winback', 'zen_0001', 'zen_retail', ?, 'PENDING_DISPATCH', 0, '2026-11-12T17:30:00+05:30');
            """,
            (zen_req_id, phone_dnd),
        )

    result_zen_interested = {
        "call_sid": "CA_ZEN_INTERESTED",
        "request_id": zen_req_id,
        "campaign_id": "c_zen_winback",
        "lead_id": "zen_0001",
        "phone": phone_dnd,
        "status": "completed",
        "disposition": "interested",
        "ended_at": "2026-11-12T17:38:00+05:30",
        "duration_sec": 120,
    }
    apply_call_result(conn, result_zen_interested, sim_now=sim_time)
    zen_row = conn.execute("SELECT state FROM leads WHERE campaign_id = 'c_zen_winback' AND lead_id = 'zen_0001';").fetchone()
    assert zen_row["state"] == "COMPLETED"

    # Test the second branch: if an in-flight call on a suppressed phone finishes with no_answer (non-terminal)
    # on attempt 1, it must become DND_BLOCKED (since phone is in phone_suppressions)
    with write_tx(conn):
        conn.execute("UPDATE leads SET state = 'IN_FLIGHT', attempts_count = 0 WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0004';")
        conn.execute("UPDATE leads SET phone = ? WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0004';", (phone_dnd,))
        conn.execute(
            """
            INSERT INTO call_attempts (request_id, campaign_id, lead_id, tenant_id, phone, state, accepted, created_sim_at)
            VALUES ('req_ren_0004', 'c_acme_renewals', 'ren_0004', 'acme_insurance', ?, 'PENDING_DISPATCH', 0, '2026-11-12T17:30:00+05:30');
            """,
            (phone_dnd,),
        )

    result_no_answer = {
        "call_sid": "CA_NO_ANSWER_SUPPRESSED",
        "request_id": "req_ren_0004",
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0004",
        "phone": phone_dnd,
        "status": "no_answer",
        "disposition": None,
        "ended_at": "2026-11-12T17:39:00+05:30",
        "duration_sec": 0,
    }
    apply_call_result(conn, result_no_answer, sim_now=sim_time)
    ren4_row = conn.execute("SELECT state FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0004';").fetchone()
    assert ren4_row["state"] == "DND_BLOCKED"


def test_early_webhook_arrival_and_abort(populated_db):
    """
    Test early webhook arrival before 201 confirmation and abort_unplaced_attempt.
    """
    conn = populated_db
    sim_time = datetime(2026, 11, 12, 17, 30, tzinfo=IST)

    res = reserve_call(conn, "c_acme_renewals", "ren_0005", sim_time)
    req_id = res["request_id"]

    # Attempt is PENDING_DISPATCH, accepted == 0, lead attempts_count == 0
    lead_before = conn.execute("SELECT attempts_count FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0005';").fetchone()
    assert lead_before["attempts_count"] == 0

    # Early webhook arrives BEFORE 201 response!
    webhook_res = {
        "call_sid": "CA_EARLY_1",
        "request_id": req_id,
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0005",
        "phone": res["phone"],
        "status": "completed",
        "disposition": "interested",
        "ended_at": "2026-11-12T17:32:00+05:30",
        "duration_sec": 30,
    }
    applied = apply_call_result(conn, webhook_res, sim_now=sim_time)
    assert applied is True

    # Verify attempt was incremented and lead is COMPLETED
    lead_after_webhook = conn.execute("SELECT state, attempts_count FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0005';").fetchone()
    assert lead_after_webhook["state"] == "COMPLETED"
    assert lead_after_webhook["attempts_count"] == 1

    # Late confirmation arrived now: must NOT double-increment attempts_count
    confirmed = confirm_call_accepted(conn, req_id, "CA_EARLY_1", sim_time)
    assert confirmed is False

    lead_after_late_confirm = conn.execute("SELECT attempts_count FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0005';").fetchone()
    assert lead_after_late_confirm["attempts_count"] == 1

    # Test abort_unplaced_attempt on a different lead
    res_abort = reserve_call(conn, "c_acme_renewals", "ren_0006", sim_time)
    assert res_abort is not None
    aborted = abort_unplaced_attempt(conn, res_abort["request_id"], sim_time)
    assert aborted is True

    lead_aborted = conn.execute("SELECT state, active_request_id FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0006';").fetchone()
    assert lead_aborted["state"] == "FRESH"
    assert lead_aborted["active_request_id"] is None


def test_webhook_fastapi_endpoint(populated_db):
    """
    Test FastAPI /webhooks/telephony endpoint:
    - Rejects bad / missing signature with 401
    - Accepts valid signature with 200
    - Handles duplicate delivery idempotently
    """
    conn = populated_db
    sim_time = datetime(2026, 11, 12, 17, 30, tzinfo=IST)

    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    client = TestClient(app)

    # Reserve a call
    res = reserve_call(conn, "c_acme_renewals", "ren_0007", sim_time)
    assert res is not None
    confirm_call_accepted(conn, res["request_id"], "CA_WH_1", sim_time)

    secret = "whsec_au_assignment_local"
    payload = {
        "call_sid": "CA_WH_1",
        "request_id": res["request_id"],
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0007",
        "phone": res["phone"],
        "status": "completed",
        "disposition": "not_interested",
        "ended_at": "2026-11-12T17:35:00+05:30",
        "duration_sec": 45,
    }
    body_bytes = json.dumps(payload).encode("utf-8")

    # 1. Missing signature -> 401
    resp_no_sig = client.post("/webhooks/telephony", content=body_bytes)
    assert resp_no_sig.status_code == 401

    # 2. Invalid signature -> 401
    resp_bad_sig = client.post(
        "/webhooks/telephony",
        content=body_bytes,
        headers={"X-AU-Signature": "invalid_sig_hex"},
    )
    assert resp_bad_sig.status_code == 401

    # 3. Valid signature -> 200
    valid_sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    resp_valid = client.post(
        "/webhooks/telephony",
        content=body_bytes,
        headers={"X-AU-Signature": valid_sig},
    )
    assert resp_valid.status_code == 200
    assert resp_valid.json() == {"status": "ok"}

    # Verify DB state
    lead_row = conn.execute(
        "SELECT state, last_disposition FROM leads WHERE campaign_id = 'c_acme_renewals' AND lead_id = 'ren_0007';"
    ).fetchone()
    assert lead_row["state"] == "COMPLETED"
    assert lead_row["last_disposition"] == "not_interested"

    # Verify webhook_events table recorded the event
    wh_event = conn.execute("SELECT * FROM webhook_events WHERE call_sid = 'CA_WH_1';").fetchone()
    assert wh_event is not None
    assert wh_event["request_id"] == res["request_id"]

    # 4. Duplicate webhook delivery -> 200 OK (idempotent)
    resp_dup = client.post(
        "/webhooks/telephony",
        content=body_bytes,
        headers={"X-AU-Signature": valid_sig},
    )
    assert resp_dup.status_code == 200
    assert resp_dup.json() == {"status": "ok"}

