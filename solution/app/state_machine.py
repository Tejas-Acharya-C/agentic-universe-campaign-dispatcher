"""Atomic SQLite state machine for leads and call attempts."""

from datetime import datetime
import json
import sqlite3
from typing import Any, Dict, Optional
import uuid

from solution.app.clock import IST
from solution.app.database import write_tx
from solution.app.rules import (
    TERMINAL_DISPOSITIONS,
    compute_callback_eligible_at,
    compute_retry_eligible_at,
    in_calling_window,
)


def get_regulatory_window(conn: sqlite3.Connection) -> Dict[str, str]:
    """Fetch the regulatory window configuration from system metadata."""
    row = conn.execute(
        "SELECT value FROM system_metadata WHERE key = 'regulatory_window';"
    ).fetchone()
    if not row:
        return {"start": "09:00", "end": "21:00"}
    return json.loads(row["value"])


def get_retry_policy(conn: sqlite3.Connection) -> Dict[str, int]:
    """Fetch the retry policy configuration from system metadata."""
    row = conn.execute(
        "SELECT value FROM system_metadata WHERE key = 'retry_policy';"
    ).fetchone()
    if not row:
        return {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15}
    return json.loads(row["value"])


def get_campaign(conn: sqlite3.Connection, campaign_id: str) -> Optional[sqlite3.Row]:
    """Fetch a campaign row by campaign_id."""
    return conn.execute(
        "SELECT * FROM campaigns WHERE campaign_id = ?;", (campaign_id,)
    ).fetchone()


def get_campaign_window(camp_row: sqlite3.Row) -> Dict[str, Any]:
    """Construct campaign window dictionary from a campaigns database row."""
    return {
        "start": camp_row["window_start"],
        "end": camp_row["window_end"],
        "days": json.loads(camp_row["window_days"]),
    }


def reserve_call(
    conn: sqlite3.Connection,
    campaign_id: str,
    lead_id: str,
    sim_now: datetime,
    safety_margin_sec: int = 15,
) -> Optional[Dict[str, Any]]:
    """
    Atomically verify all business constraints and reserve an in-flight call attempt.
    
    Returns reservation dict with request_id if reserved, or None if any check fails.
    """
    sim_now_ist = sim_now.astimezone(IST)
    with write_tx(conn):
        lead = conn.execute(
            "SELECT * FROM leads WHERE campaign_id = ? AND lead_id = ?;",
            (campaign_id, lead_id),
        ).fetchone()
        if not lead:
            return None

        # 1. Lead state must be eligible
        if lead["state"] not in ("CALLBACK_WAIT", "RETRY_WAIT", "FRESH"):
            return None

        # 2. Eligibility time check
        if lead["next_eligible_at"] is not None:
            elig_dt = datetime.fromisoformat(lead["next_eligible_at"])
            if elig_dt.tzinfo is None:
                elig_dt = elig_dt.replace(tzinfo=IST)
            if elig_dt > sim_now_ist:
                return None

        # 3. Campaign attempt limit
        camp = get_campaign(conn, campaign_id)
        if not camp or lead["attempts_count"] >= camp["max_attempts"]:
            return None

        # 4. Suppression check
        suppressed = conn.execute(
            "SELECT 1 FROM phone_suppressions WHERE phone = ?;", (lead["phone"],)
        ).fetchone()
        if suppressed:
            conn.execute(
                """
                UPDATE leads
                SET state = 'DND_BLOCKED',
                    updated_at = ?
                WHERE campaign_id = ? AND lead_id = ?;
                """,
                (sim_now_ist.isoformat(), campaign_id, lead_id),
            )
            return None

        # 5. Calling window check
        reg_window = get_regulatory_window(conn)
        camp_window = get_campaign_window(camp)
        if not in_calling_window(sim_now_ist, reg_window, camp_window, safety_margin_sec):
            return None

        # 6. One active call per phone constraint
        active_phone = conn.execute(
            """
            SELECT 1 FROM call_attempts
            WHERE phone = ? AND state IN ('PENDING_DISPATCH', 'ACTIVE');
            """,
            (lead["phone"],),
        ).fetchone()
        if active_phone:
            return None

        # 7. Tenant concurrent call limit
        tenant = conn.execute(
            "SELECT max_concurrent_calls FROM tenants WHERE tenant_id = ?;",
            (lead["tenant_id"],),
        ).fetchone()
        if not tenant:
            return None

        active_tenant_count = conn.execute(
            """
            SELECT COUNT(*) FROM call_attempts
            WHERE tenant_id = ? AND state IN ('PENDING_DISPATCH', 'ACTIVE');
            """,
            (lead["tenant_id"],),
        ).fetchone()[0]
        if active_tenant_count >= tenant["max_concurrent_calls"]:
            return None

        # Reservation successful
        request_id = str(uuid.uuid4())
        created_sim_at = sim_now_ist.isoformat()

        conn.execute(
            """
            INSERT INTO call_attempts (
                request_id, campaign_id, lead_id, tenant_id, phone,
                state, accepted, created_sim_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING_DISPATCH', 0, ?);
            """,
            (
                request_id,
                campaign_id,
                lead_id,
                lead["tenant_id"],
                lead["phone"],
                created_sim_at,
            ),
        )

        conn.execute(
            """
            UPDATE leads
            SET state = 'IN_FLIGHT',
                active_request_id = ?,
                updated_at = ?
            WHERE campaign_id = ? AND lead_id = ?;
            """,
            (request_id, created_sim_at, campaign_id, lead_id),
        )

        return {
            "request_id": request_id,
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "tenant_id": lead["tenant_id"],
            "phone": lead["phone"],
        }


def confirm_call_accepted(
    conn: sqlite3.Connection,
    request_id: str,
    call_sid: str,
    sim_now: datetime,
) -> bool:
    """
    Confirm that the provider accepted a call (HTTP 201/200).
    
    Increments lead attempts_count and marks the attempt ACTIVE.
    If the call already completed via an early webhook, returns False.
    """
    sim_now_ist = sim_now.astimezone(IST)
    with write_tx(conn):
        attempt = conn.execute(
            "SELECT * FROM call_attempts WHERE request_id = ?;", (request_id,)
        ).fetchone()
        if not attempt:
            return False

        if attempt["state"] in ("ENDED", "ABORTED"):
            return False

        if attempt["accepted"] == 0:
            conn.execute(
                """
                UPDATE call_attempts
                SET call_sid = ?,
                    state = 'ACTIVE',
                    accepted = 1
                WHERE request_id = ?;
                """,
                (call_sid, request_id),
            )
            conn.execute(
                """
                UPDATE leads
                SET attempts_count = attempts_count + 1,
                    updated_at = ?
                WHERE campaign_id = ? AND lead_id = ?;
                """,
                (sim_now_ist.isoformat(), attempt["campaign_id"], attempt["lead_id"]),
            )
        else:
            conn.execute(
                """
                UPDATE call_attempts
                SET call_sid = COALESCE(call_sid, ?)
                WHERE request_id = ?;
                """,
                (call_sid, request_id),
            )

        return True


def abort_unplaced_attempt(
    conn: sqlite3.Connection,
    request_id: str,
    sim_now: datetime,
) -> bool:
    """
    Abort an unplaced attempt (e.g. 503 rejection or network failure before provider acceptance).
    
    Restores the lead state back to CALLBACK_WAIT, RETRY_WAIT, FRESH, or DND_BLOCKED.
    """
    sim_now_ist = sim_now.astimezone(IST)
    with write_tx(conn):
        attempt = conn.execute(
            "SELECT * FROM call_attempts WHERE request_id = ?;", (request_id,)
        ).fetchone()
        if not attempt or attempt["accepted"] == 1 or attempt["state"] != "PENDING_DISPATCH":
            return False

        ended_sim_at = sim_now_ist.isoformat()
        conn.execute(
            """
            UPDATE call_attempts
            SET state = 'ABORTED',
                ended_sim_at = ?
            WHERE request_id = ?;
            """,
            (ended_sim_at, request_id),
        )

        lead = conn.execute(
            "SELECT * FROM leads WHERE campaign_id = ? AND lead_id = ?;",
            (attempt["campaign_id"], attempt["lead_id"]),
        ).fetchone()
        if not lead:
            return True

        suppressed = conn.execute(
            "SELECT 1 FROM phone_suppressions WHERE phone = ?;", (lead["phone"],)
        ).fetchone()

        if suppressed:
            new_state = "DND_BLOCKED"
        elif lead["callback_at"] and lead["last_disposition"] == "callback_requested":
            new_state = "CALLBACK_WAIT"
        elif lead["attempts_count"] > 0:
            new_state = "RETRY_WAIT"
        else:
            new_state = "FRESH"

        conn.execute(
            """
            UPDATE leads
            SET state = ?,
                active_request_id = NULL,
                updated_at = ?
            WHERE campaign_id = ? AND lead_id = ?;
            """,
            (new_state, ended_sim_at, attempt["campaign_id"], attempt["lead_id"]),
        )

        return True


def apply_call_result(
    conn: sqlite3.Connection,
    result: Dict[str, Any],
    raw_webhook_body: Optional[str] = None,
    sim_now: Optional[datetime] = None,
) -> bool:
    """
    Process telephony call result webhook and transition lead state.
    
    Enforces exact precedence: COMPLETED > EXHAUSTED > DND_BLOCKED.
    Handles out-of-order/early webhooks and duplicate webhook deliveries idempotently.
    """
    call_sid = result.get("call_sid")
    request_id = result.get("request_id")
    ended_at_str = result.get("ended_at")

    if ended_at_str:
        ended_at_dt = datetime.fromisoformat(ended_at_str)
        if ended_at_dt.tzinfo is None:
            ended_at_dt = ended_at_dt.replace(tzinfo=IST)
        else:
            ended_at_dt = ended_at_dt.astimezone(IST)
    else:
        ended_at_dt = sim_now.astimezone(IST) if sim_now else datetime.now(IST)

    received_at_str = (
        sim_now.astimezone(IST).isoformat()
        if sim_now
        else ended_at_dt.isoformat()
    )

    with write_tx(conn):
        # 1. Idempotently persist raw webhook event
        if raw_webhook_body is not None and call_sid and request_id:
            conn.execute(
                """
                INSERT OR IGNORE INTO webhook_events (call_sid, request_id, raw_body, received_at)
                VALUES (?, ?, ?, ?);
                """,
                (call_sid, request_id, raw_webhook_body, received_at_str),
            )

        # 2. Lookup call attempt
        attempt = None
        if request_id:
            attempt = conn.execute(
                "SELECT * FROM call_attempts WHERE request_id = ?;", (request_id,)
            ).fetchone()
        if not attempt and call_sid:
            attempt = conn.execute(
                "SELECT * FROM call_attempts WHERE call_sid = ?;", (call_sid,)
            ).fetchone()

        if not attempt:
            return False

        # Idempotent check
        if attempt["state"] == "ENDED":
            return False

        # If webhook arrived before confirmation (e.g. 504 reconciliation or early webhook)
        if attempt["accepted"] == 0:
            conn.execute(
                """
                UPDATE leads
                SET attempts_count = attempts_count + 1
                WHERE campaign_id = ? AND lead_id = ?;
                """,
                (attempt["campaign_id"], attempt["lead_id"]),
            )

        disp = result.get("disposition") or ""
        status = result.get("status", "")
        duration_sec = int(result.get("duration_sec", 0))
        callback_at_str = result.get("callback_at")

        conn.execute(
            """
            UPDATE call_attempts
            SET call_sid = COALESCE(?, call_sid),
                state = 'ENDED',
                accepted = 1,
                ended_sim_at = ?,
                status = ?,
                disposition = ?,
                callback_at = ?,
                duration_sec = ?
            WHERE request_id = ?;
            """,
            (
                call_sid,
                ended_at_dt.isoformat(),
                status,
                disp,
                callback_at_str,
                duration_sec,
                attempt["request_id"],
            ),
        )

        lead = conn.execute(
            "SELECT * FROM leads WHERE campaign_id = ? AND lead_id = ?;",
            (attempt["campaign_id"], attempt["lead_id"]),
        ).fetchone()

        camp = get_campaign(conn, attempt["campaign_id"])
        reg_window = get_regulatory_window(conn)
        camp_window = get_campaign_window(camp) if camp else {}
        retry_policy = get_retry_policy(conn)

        next_elig: Optional[str] = None
        new_cb_at: Optional[str] = None

        # Precedence: COMPLETED > EXHAUSTED > DND_BLOCKED
        if disp in TERMINAL_DISPOSITIONS:
            new_state = "COMPLETED"
            if disp == "dnd":
                conn.execute(
                    """
                    INSERT OR IGNORE INTO phone_suppressions (
                        phone, reason, source_campaign_id, source_lead_id, suppressed_at
                    ) VALUES (?, 'dnd', ?, ?, ?);
                    """,
                    (
                        attempt["phone"],
                        attempt["campaign_id"],
                        attempt["lead_id"],
                        ended_at_dt.isoformat(),
                    ),
                )
                conn.execute(
                    """
                    UPDATE leads
                    SET state = 'DND_BLOCKED',
                        updated_at = ?
                    WHERE phone = ?
                      AND (campaign_id != ? OR lead_id != ?)
                      AND state NOT IN ('COMPLETED', 'EXHAUSTED', 'IN_FLIGHT');
                    """,
                    (
                        ended_at_dt.isoformat(),
                        attempt["phone"],
                        attempt["campaign_id"],
                        attempt["lead_id"],
                    ),
                )
        elif lead["attempts_count"] >= camp["max_attempts"]:
            new_state = "EXHAUSTED"
        elif conn.execute(
            "SELECT 1 FROM phone_suppressions WHERE phone = ?;", (lead["phone"],)
        ).fetchone():
            new_state = "DND_BLOCKED"
        elif disp == "callback_requested" and callback_at_str:
            new_state = "CALLBACK_WAIT"
            cb_dt = datetime.fromisoformat(callback_at_str)
            if cb_dt.tzinfo is None:
                cb_dt = cb_dt.replace(tzinfo=IST)
            next_elig = compute_callback_eligible_at(cb_dt, reg_window, camp_window).isoformat()
            new_cb_at = callback_at_str
        else:
            new_state = "RETRY_WAIT"
            next_elig = compute_retry_eligible_at(
                ended_at_dt, status, retry_policy, reg_window, camp_window
            ).isoformat()

        conn.execute(
            """
            UPDATE leads
            SET state = ?,
                last_status = ?,
                last_disposition = ?,
                last_ended_at = ?,
                callback_at = ?,
                next_eligible_at = ?,
                active_request_id = NULL,
                updated_at = ?
            WHERE campaign_id = ? AND lead_id = ?;
            """,
            (
                new_state,
                status,
                disp,
                ended_at_dt.isoformat(),
                new_cb_at,
                next_elig,
                ended_at_dt.isoformat(),
                attempt["campaign_id"],
                attempt["lead_id"],
            ),
        )

        return True

