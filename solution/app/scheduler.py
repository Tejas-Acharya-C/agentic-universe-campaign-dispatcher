"""Priority scheduler for dispatching leads under tenant, phone, and window constraints."""

from datetime import datetime
import sqlite3
from typing import List, Tuple

from solution.app.clock import IST
from solution.app.rules import in_calling_window
from solution.app.state_machine import (
    get_campaign_window,
    get_regulatory_window,
)


def select_next_candidates(
    conn: sqlite3.Connection,
    sim_now: datetime,
    safety_margin_sec: int = 15,
    limit: int = 20,
) -> List[Tuple[str, str]]:
    """
    Select up to `limit` candidate (campaign_id, lead_id) pairs to dispatch.
    
    Priority order:
    1. CALLBACK_WAIT (ordered by next_eligible_at ASC, campaign_id ASC, lead_id ASC)
    2. RETRY_WAIT    (ordered by next_eligible_at ASC, campaign_id ASC, lead_id ASC)
    3. FRESH         (ordered by campaign_id ASC, lead_id ASC)
    
    Greedily ensures:
    - Campaign calling window is open at sim_now
    - Lead eligibility time has arrived (or is NULL)
    - Phone number is not suppressed
    - Remaining tenant concurrent capacity > 0
    - Distinct phone numbers (no active call to the same phone)
    """
    sim_now_ist = sim_now.astimezone(IST)
    sim_now_iso = sim_now_ist.isoformat()

    reg_window = get_regulatory_window(conn)

    # 1. Determine which campaigns have open calling windows
    all_campaigns = conn.execute("SELECT * FROM campaigns;").fetchall()
    open_campaign_ids = []
    for c in all_campaigns:
        camp_win = get_campaign_window(c)
        if in_calling_window(sim_now_ist, reg_window, camp_win, safety_margin_sec):
            open_campaign_ids.append(c["campaign_id"])

    if not open_campaign_ids:
        return []

    # 2. Compute remaining capacity per tenant
    all_tenants = conn.execute("SELECT tenant_id, max_concurrent_calls FROM tenants;").fetchall()
    tenant_max = {t["tenant_id"]: t["max_concurrent_calls"] for t in all_tenants}

    active_counts = {
        row["tenant_id"]: row["cnt"]
        for row in conn.execute(
            """
            SELECT tenant_id, COUNT(*) as cnt
            FROM call_attempts
            WHERE state IN ('PENDING_DISPATCH', 'ACTIVE')
            GROUP BY tenant_id;
            """
        ).fetchall()
    }

    remaining_capacity = {
        t_id: max(0, max_calls - active_counts.get(t_id, 0))
        for t_id, max_calls in tenant_max.items()
    }

    # 3. Collect active/pending phone numbers
    active_phones = {
        row["phone"]
        for row in conn.execute(
            """
            SELECT DISTINCT phone
            FROM call_attempts
            WHERE state IN ('PENDING_DISPATCH', 'ACTIVE');
            """
        ).fetchall()
    }

    # 4. Query candidate leads in open campaigns ordered strictly by business priority
    placeholders = ",".join("?" for _ in open_campaign_ids)
    query = f"""
    SELECT campaign_id, lead_id, tenant_id, phone, state, next_eligible_at
    FROM leads
    WHERE campaign_id IN ({placeholders})
      AND state IN ('CALLBACK_WAIT', 'RETRY_WAIT', 'FRESH')
      AND phone NOT IN (SELECT phone FROM phone_suppressions)
      AND (next_eligible_at IS NULL OR next_eligible_at <= ?)
    ORDER BY
      CASE state
        WHEN 'CALLBACK_WAIT' THEN 1
        WHEN 'RETRY_WAIT' THEN 2
        WHEN 'FRESH' THEN 3
        ELSE 4
      END ASC,
      CASE WHEN state = 'FRESH' THEN NULL ELSE next_eligible_at END ASC,
      campaign_id ASC,
      lead_id ASC;
    """
    params = list(open_campaign_ids) + [sim_now_iso]
    candidates = conn.execute(query, params).fetchall()

    # 5. Greedily select candidate pairs
    selected: List[Tuple[str, str]] = []
    for cand in candidates:
        if len(selected) >= limit:
            break

        t_id = cand["tenant_id"]
        phone = cand["phone"]

        if remaining_capacity.get(t_id, 0) <= 0:
            continue

        if phone in active_phones:
            continue

        selected.append((cand["campaign_id"], cand["lead_id"]))
        remaining_capacity[t_id] -= 1
        active_phones.add(phone)

    return selected

