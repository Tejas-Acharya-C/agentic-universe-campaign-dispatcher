"""Unit tests for simulated clock, business rules, windows, retries, callbacks, and HMAC verification."""

from datetime import datetime, timedelta
import hashlib
import hmac
import pytest

from solution.app.clock import DAYS, FakeClock, IST, SimClock
from solution.app.rules import (
    TERMINAL_DISPOSITIONS,
    compute_callback_eligible_at,
    compute_retry_eligible_at,
    in_calling_window,
    next_eligible_window_open,
    parse_hm,
    verify_hmac_signature,
)


def test_clock_abstractions(monkeypatch):
    """Test SimClock calculation formula and FakeClock manipulation without sleeps."""
    sim_epoch = datetime(2026, 11, 12, 17, 30, tzinfo=IST)
    real_epoch = 1000.0
    speed = 600.0

    clock = SimClock(sim_epoch=sim_epoch, real_epoch=real_epoch, speed=speed)

    # Mock time.time() at exactly 1 second after real_epoch
    monkeypatch.setattr("time.time", lambda: 1001.0)
    # 1 real second at 600x = 600 sim seconds = 10 sim minutes -> 17:40:00
    now = clock.now()
    assert now == datetime(2026, 11, 12, 17, 40, tzinfo=IST)
    assert clock.sim_to_real_seconds(600.0) == 1.0

    # Test sync_from_payload and from_payload
    payload = {
        "sim_epoch": "2026-11-12T18:00:00+05:30",
        "real_epoch": 2000.0,
        "speed": 1200.0,
    }
    clock.sync_from_payload(payload)
    assert clock.speed == 1200.0
    assert clock.real_epoch == 2000.0
    assert clock.sim_epoch == datetime(2026, 11, 12, 18, 0, tzinfo=IST)

    clock2 = SimClock.from_payload(payload)
    assert clock2.speed == 1200.0

    # Test FakeClock
    fake = FakeClock("2026-11-12T10:00:00+05:30")
    assert fake.now() == datetime(2026, 11, 12, 10, 0, tzinfo=IST)
    fake.advance(minutes=15)
    assert fake.now() == datetime(2026, 11, 12, 10, 15, tzinfo=IST)
    fake.advance(hours=2, seconds=30)
    assert fake.now() == datetime(2026, 11, 12, 12, 15, 30, tzinfo=IST)
    fake.advance(days=1)
    assert fake.now() == datetime(2026, 11, 13, 12, 15, 30, tzinfo=IST)


def test_in_calling_window_boundaries_and_days():
    """
    Test in_calling_window for:
    - Start inclusive (10:00:00 True)
    - End exclusive (19:00:00 False)
    - Safety margin boundary (18:59:45 False when margin=15s, 18:59:44 True)
    - Sunday rejection for mon-sat campaign
    - Weekend rejection for mon-fri campaign
    - Regulatory 09:00–21:00 clamping if campaign window is wider (08:00–22:00)
    """
    reg_window = {"start": "09:00", "end": "21:00"}
    camp_renewals = {
        "start": "10:00",
        "end": "19:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
    }
    camp_zen = {
        "start": "09:30",
        "end": "18:00",
        "days": ["mon", "tue", "wed", "thu", "fri"],
    }
    camp_wide = {
        "start": "08:00",
        "end": "22:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    }

    # 2026-11-12 is Thursday
    # 1. Start inclusive
    t_start = datetime(2026, 11, 12, 10, 0, 0, tzinfo=IST)
    assert in_calling_window(t_start, reg_window, camp_renewals, safety_margin_sec=15) is True

    # 2. End exclusive
    t_end = datetime(2026, 11, 12, 19, 0, 0, tzinfo=IST)
    assert in_calling_window(t_end, reg_window, camp_renewals, safety_margin_sec=15) is False

    # 3. Safety margin boundary (margin 15s means close is 18:59:45)
    t_before_margin = datetime(2026, 11, 12, 18, 59, 44, tzinfo=IST)
    assert in_calling_window(t_before_margin, reg_window, camp_renewals, safety_margin_sec=15) is True

    t_at_margin = datetime(2026, 11, 12, 18, 59, 45, tzinfo=IST)
    assert in_calling_window(t_at_margin, reg_window, camp_renewals, safety_margin_sec=15) is False

    # 4. Sunday rejection for mon-sat campaign (2026-11-15 is Sunday)
    t_sunday = datetime(2026, 11, 15, 12, 0, 0, tzinfo=IST)
    assert in_calling_window(t_sunday, reg_window, camp_renewals, safety_margin_sec=15) is False

    # 5. Weekend rejection for mon-fri campaign (2026-11-14 is Saturday)
    t_saturday = datetime(2026, 11, 14, 12, 0, 0, tzinfo=IST)
    assert in_calling_window(t_saturday, reg_window, camp_zen, safety_margin_sec=15) is False
    assert in_calling_window(t_sunday, reg_window, camp_zen, safety_margin_sec=15) is False

    # 6. Regulatory clamping (09:00-21:00 vs campaign 08:00-22:00)
    # 08:30 is campaign open, but regulatory closed (< 09:00)
    t_early = datetime(2026, 11, 12, 8, 30, 0, tzinfo=IST)
    assert in_calling_window(t_early, reg_window, camp_wide, safety_margin_sec=15) is False

    t_reg_open = datetime(2026, 11, 12, 9, 0, 0, tzinfo=IST)
    assert in_calling_window(t_reg_open, reg_window, camp_wide, safety_margin_sec=15) is True

    # Clamped end is 21:00 - 15s = 20:59:45
    t_reg_margin_ok = datetime(2026, 11, 12, 20, 59, 44, tzinfo=IST)
    assert in_calling_window(t_reg_margin_ok, reg_window, camp_wide, safety_margin_sec=15) is True

    t_reg_margin_close = datetime(2026, 11, 12, 20, 59, 45, tzinfo=IST)
    assert in_calling_window(t_reg_margin_close, reg_window, camp_wide, safety_margin_sec=15) is False

    t_late = datetime(2026, 11, 12, 21, 30, 0, tzinfo=IST)
    assert in_calling_window(t_late, reg_window, camp_wide, safety_margin_sec=15) is False


def test_next_eligible_window_open():
    """
    Test next_eligible_window_open for:
    - Before opening same day -> snaps to start
    - Inside open window -> returns target_time unchanged
    - After window close on Saturday -> skips Sunday (mon-sat) and snaps to Monday 10:00 IST
    """
    reg_window = {"start": "09:00", "end": "21:00"}
    camp_renewals = {
        "start": "10:00",
        "end": "19:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
    }

    # Before opening on Thursday (08:30 IST) -> snaps to 10:00 same day
    t_before = datetime(2026, 11, 12, 8, 30, tzinfo=IST)
    nxt1 = next_eligible_window_open(t_before, reg_window, camp_renewals)
    assert nxt1 == datetime(2026, 11, 12, 10, 0, tzinfo=IST)

    # Inside open window (14:30 IST) -> returns 14:30 unchanged
    t_inside = datetime(2026, 11, 12, 14, 30, tzinfo=IST)
    nxt2 = next_eligible_window_open(t_inside, reg_window, camp_renewals)
    assert nxt2 == t_inside

    # After close on Saturday (2026-11-14 19:30 IST)
    # Sunday (2026-11-15) not in days -> snaps to Monday (2026-11-16 10:00 IST)
    t_sat_after = datetime(2026, 11, 14, 19, 30, tzinfo=IST)
    nxt3 = next_eligible_window_open(t_sat_after, reg_window, camp_renewals)
    assert nxt3 == datetime(2026, 11, 16, 10, 0, tzinfo=IST)


def test_compute_retry_eligible_at():
    """
    Test compute_retry_eligible_at for:
    - no_answer (+60m from ended_at)
    - busy (+60m)
    - failed (+15m)
    - rollover when +60m lands after 19:00 window close
    """
    reg_window = {"start": "09:00", "end": "21:00"}
    camp_renewals = {
        "start": "10:00",
        "end": "19:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
    }
    retry_policy = {
        "no_answer_minutes": 60,
        "busy_minutes": 60,
        "failed_minutes": 15,
    }

    ended_at = datetime(2026, 11, 12, 14, 0, tzinfo=IST)

    # no_answer -> +60m = 15:00
    res_na = compute_retry_eligible_at(ended_at, "no_answer", retry_policy, reg_window, camp_renewals)
    assert res_na == datetime(2026, 11, 12, 15, 0, tzinfo=IST)

    # busy -> +60m = 15:00
    res_busy = compute_retry_eligible_at(ended_at, "busy", retry_policy, reg_window, camp_renewals)
    assert res_busy == datetime(2026, 11, 12, 15, 0, tzinfo=IST)

    # failed -> +15m = 14:15
    res_failed = compute_retry_eligible_at(ended_at, "failed", retry_policy, reg_window, camp_renewals)
    assert res_failed == datetime(2026, 11, 12, 14, 15, tzinfo=IST)

    # Rollover: ended_at = 18:30, +60m = 19:30 (after 19:00 window close)
    # Next day Friday 10:00
    ended_late = datetime(2026, 11, 12, 18, 30, tzinfo=IST)
    res_rollover = compute_retry_eligible_at(ended_late, "no_answer", retry_policy, reg_window, camp_renewals)
    assert res_rollover == datetime(2026, 11, 13, 10, 0, tzinfo=IST)


def test_compute_callback_eligible_at():
    """Test compute_callback_eligible_at for inside window vs late night rollover."""
    reg_window = {"start": "09:00", "end": "21:00"}
    camp_renewals = {
        "start": "10:00",
        "end": "19:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
    }

    # Inside window
    cb_inside = datetime(2026, 11, 12, 15, 30, tzinfo=IST)
    res_inside = compute_callback_eligible_at(cb_inside, reg_window, camp_renewals)
    assert res_inside == cb_inside

    # Late night (22:30 IST) -> rolls over to Friday 10:00 IST
    cb_late = datetime(2026, 11, 12, 22, 30, tzinfo=IST)
    res_late = compute_callback_eligible_at(cb_late, reg_window, camp_renewals)
    assert res_late == datetime(2026, 11, 13, 10, 0, tzinfo=IST)


def test_verify_hmac_signature():
    """Test HMAC-SHA256 signature verification."""
    secret = "whsec_au_assignment_local"
    raw_body = b'{"event":"call.ended","call_sid":"CA12345"}'

    # Compute valid signature
    valid_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    assert verify_hmac_signature(secret, raw_body, valid_sig) is True
    assert verify_hmac_signature(secret, b'{"event":"call.tampered"}', valid_sig) is False
    assert verify_hmac_signature("wrong_secret", raw_body, valid_sig) is False
    assert verify_hmac_signature(secret, raw_body, None) is False
    assert verify_hmac_signature(secret, raw_body, "") is False
    assert verify_hmac_signature("", raw_body, valid_sig) is False


def test_terminal_dispositions_and_parse_hm():
    """Verify terminal dispositions constant and parse_hm utility."""
    assert TERMINAL_DISPOSITIONS == {"interested", "not_interested", "wrong_number", "dnd"}
    assert parse_hm("09:30") == (9, 30)
    assert parse_hm("21:00") == (21, 0)
    with pytest.raises(ValueError):
        parse_hm("invalid")

