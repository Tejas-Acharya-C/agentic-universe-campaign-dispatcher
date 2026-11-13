"""Reconciliation of ambiguous in-flight calls, 504 timeouts, crashes, and lost webhooks."""

from datetime import datetime
import sqlite3
from typing import Dict, Set

from solution.app.clock import Clock, IST
from solution.app.state_machine import (
    abort_unplaced_attempt,
    apply_call_result,
    confirm_call_accepted,
)
from solution.app.telephony import TelephonyClient

# In-memory tracking of request_ids currently awaiting an HTTP response
in_flight_requests: Set[str] = set()


async def reconcile_attempts(
    conn: sqlite3.Connection,
    telephony: TelephonyClient,
    sim_clock: Clock,
    startup: bool = False,
) -> Dict[str, int]:
    """
    Reconcile pending/active call attempts against the carrier API.
    
    Step A: Resolve ambiguous PENDING_DISPATCH attempts (caused by 504 timeouts or kill -9).
    Step B: Recover lost webhooks on ACTIVE calls exceeding max possible duration.
    """
    reconciled_pending = 0
    reconciled_active = 0
    now_ist = sim_clock.now().astimezone(IST)

    # Step A: Reconcile PENDING_DISPATCH attempts
    pending_attempts = conn.execute(
        "SELECT request_id FROM call_attempts WHERE state = 'PENDING_DISPATCH';"
    ).fetchall()

    for row in pending_attempts:
        rid = row["request_id"]
        if not startup and rid in in_flight_requests:
            continue

        status_code, data = await telephony.get_call_by_request_id(rid)
        if status_code == 200 and data:
            confirm_call_accepted(conn, rid, data["call_sid"], now_ist)
            if data.get("state") == "ended" and data.get("result"):
                apply_call_result(
                    conn, data["result"], raw_webhook_body=None, sim_now=now_ist
                )
            reconciled_pending += 1
        elif status_code == 404:
            abort_unplaced_attempt(conn, rid, now_ist)
            reconciled_pending += 1

    # Step B: Reconcile stale ACTIVE calls (max carrier duration is 390 sim seconds)
    active_attempts = conn.execute(
        "SELECT request_id, call_sid, created_sim_at FROM call_attempts WHERE state = 'ACTIVE';"
    ).fetchall()

    for row in active_attempts:
        rid = row["request_id"]
        sid = row["call_sid"]
        created_dt = datetime.fromisoformat(row["created_sim_at"])
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=IST)
        else:
            created_dt = created_dt.astimezone(IST)

        sim_age = (now_ist - created_dt).total_seconds()
        if startup or sim_age > 410:
            status_code = 404
            data = None
            if sid:
                status_code, data = await telephony.get_call_by_sid(sid)
            if status_code != 200 or not data:
                status_code, data = await telephony.get_call_by_request_id(rid)

            if status_code == 200 and data and data.get("state") == "ended" and data.get("result"):
                apply_call_result(
                    conn, data["result"], raw_webhook_body=None, sim_now=now_ist
                )
                reconciled_active += 1

    return {
        "reconciled_pending": reconciled_pending,
        "reconciled_active": reconciled_active,
    }

