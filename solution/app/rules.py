"""Pure business rules for campaign windows, retries, callbacks, and HMAC verification."""

from datetime import datetime, timedelta
import hashlib
import hmac
from typing import Dict, Tuple

from solution.app.clock import DAYS, IST

TERMINAL_DISPOSITIONS = frozenset(
    {"interested", "not_interested", "wrong_number", "dnd"}
)


def parse_hm(hm_str: str) -> Tuple[int, int]:
    """Parse 'HH:MM' string into (hour, minute)."""
    parts = hm_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {hm_str}")
    return int(parts[0]), int(parts[1])


def in_calling_window(
    sim_now: datetime,
    reg_window: Dict[str, str],
    camp_window: Dict,
    safety_margin_sec: int = 15,
) -> bool:
    """
    Check if sim_now is within the allowed calling window.
    
    Must satisfy:
    1. Campaign active day (mon-sun)
    2. Within [effective_start, effective_end) where:
       effective_start = max(regulatory_start, campaign_start)
       effective_end = min(regulatory_end, campaign_end) - safety_margin_sec
    """
    lt = sim_now.astimezone(IST)
    current_day = DAYS[lt.weekday()]
    allowed_days = camp_window.get("days", [])
    if current_day not in allowed_days:
        return False

    reg_sh, reg_sm = parse_hm(reg_window["start"])
    reg_eh, reg_em = parse_hm(reg_window["end"])
    camp_sh, camp_sm = parse_hm(camp_window["start"])
    camp_eh, camp_em = parse_hm(camp_window["end"])

    reg_start = lt.replace(hour=reg_sh, minute=reg_sm, second=0, microsecond=0)
    reg_end = lt.replace(hour=reg_eh, minute=reg_em, second=0, microsecond=0)
    camp_start = lt.replace(hour=camp_sh, minute=camp_sm, second=0, microsecond=0)
    camp_end = lt.replace(hour=camp_eh, minute=camp_em, second=0, microsecond=0)

    effective_start = max(reg_start, camp_start)
    effective_end = min(reg_end, camp_end) - timedelta(seconds=safety_margin_sec)

    return effective_start <= lt < effective_end


def next_eligible_window_open(
    target_time: datetime,
    reg_window: Dict[str, str],
    camp_window: Dict,
) -> datetime:
    """
    Find the earliest instant >= target_time within an open calling window.
    
    Searches forward up to 14 days:
    - If target_time falls before effective_start on a valid day: returns effective_start.
    - If target_time falls within [effective_start, effective_end): returns target_time unchanged.
    - If target_time is at or past effective_end: rolls over to effective_start of next valid day.
    """
    lt = target_time.astimezone(IST)
    reg_sh, reg_sm = parse_hm(reg_window["start"])
    reg_eh, reg_em = parse_hm(reg_window["end"])
    camp_sh, camp_sm = parse_hm(camp_window["start"])
    camp_eh, camp_em = parse_hm(camp_window["end"])
    allowed_days = camp_window.get("days", [])

    for d in range(14):
        candidate_date = lt + timedelta(days=d)
        candidate_day_name = DAYS[candidate_date.weekday()]
        if candidate_day_name not in allowed_days:
            continue

        reg_start = candidate_date.replace(hour=reg_sh, minute=reg_sm, second=0, microsecond=0)
        reg_end = candidate_date.replace(hour=reg_eh, minute=reg_em, second=0, microsecond=0)
        camp_start = candidate_date.replace(hour=camp_sh, minute=camp_sm, second=0, microsecond=0)
        camp_end = candidate_date.replace(hour=camp_eh, minute=camp_em, second=0, microsecond=0)

        effective_start = max(reg_start, camp_start)
        effective_end = min(reg_end, camp_end)

        if effective_start >= effective_end:
            continue

        if d == 0:
            if lt < effective_start:
                return effective_start
            elif effective_start <= lt < effective_end:
                return lt
            else:
                continue
        else:
            return effective_start

    raise ValueError("No eligible calling window found within 14 days")


def compute_retry_eligible_at(
    ended_at: datetime,
    status: str,
    retry_policy: Dict[str, int],
    reg_window: Dict[str, str],
    camp_window: Dict,
) -> datetime:
    """
    Compute next eligible datetime for a retried call based on call status.
    
    Status mapping:
    - 'no_answer' -> no_answer_minutes
    - 'busy'      -> busy_minutes
    - 'failed'    -> failed_minutes
    """
    status_key = status.lower()
    if status_key == "no_answer":
        delay_minutes = retry_policy["no_answer_minutes"]
    elif status_key == "busy":
        delay_minutes = retry_policy["busy_minutes"]
    elif status_key == "failed":
        delay_minutes = retry_policy["failed_minutes"]
    else:
        raise ValueError(f"Unknown retry status: {status}")

    raw_next = ended_at.astimezone(IST) + timedelta(minutes=delay_minutes)
    return next_eligible_window_open(raw_next, reg_window, camp_window)


def compute_callback_eligible_at(
    callback_at: datetime,
    reg_window: Dict[str, str],
    camp_window: Dict,
) -> datetime:
    """Compute next eligible datetime for a customer-requested callback."""
    return next_eligible_window_open(callback_at.astimezone(IST), reg_window, camp_window)


def verify_hmac_signature(
    secret: str,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    """Verify telephony webhook HMAC-SHA256 signature."""
    if not signature_header or not secret:
        return False

    computed = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature_header.strip())

