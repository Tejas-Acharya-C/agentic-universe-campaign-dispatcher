"""Core dispatcher loop coordinating reservation, dialling, and reconciliation."""

import asyncio
import logging
import sqlite3
import time
from typing import Optional, Set

from solution.app.clock import Clock
from solution.app.config import Settings
from solution.app.recovery import in_flight_requests, reconcile_attempts
from solution.app.reporting import is_all_leads_terminal
from solution.app.scheduler import select_next_candidates
from solution.app.rules import in_calling_window
from solution.app.state_machine import (
    abort_unplaced_attempt,
    apply_call_result,
    confirm_call_accepted,
    get_campaign,
    get_campaign_window,
    get_regulatory_window,
    reserve_call,
)
from solution.app.telephony import TelephonyClient

logger = logging.getLogger(__name__)


class Dispatcher:
    """Dispatches outbound calls under concurrency and window constraints."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        sim_clock: Clock,
        telephony: TelephonyClient,
    ):
        self.conn = conn
        self.settings = settings
        self.sim_clock = sim_clock
        self.telephony = telephony
        self.active_tasks: Set[asyncio.Task] = set()

    @property
    def effective_safety_margin_sec(self) -> int:
        speed = getattr(self.sim_clock, "speed", 1.0)
        # At high simulation speeds (e.g. 1200x or 2400x), network I/O and process scheduling
        # latency (e.g. 100-200ms) corresponds to 120-480 simulated seconds.
        # Ensure the safety margin accounts for at least 0.30s of real-world latency or settings value
        # so calling window close is never breached during network transit.
        dynamic_margin = int(speed * 0.30)
        return max(self.settings.window_safety_margin_sec, dynamic_margin)

    async def _dispatch_single(self, reservation: dict) -> None:
        """Place a single outbound call and handle response synchronously."""
        rid = reservation["request_id"]
        in_flight_requests.add(rid)

        try:
            # Re-verify calling window at the exact moment before dialling
            camp = get_campaign(self.conn, reservation["campaign_id"])
            reg_window = get_regulatory_window(self.conn)
            camp_window = get_campaign_window(camp) if camp else {}
            margin = self.effective_safety_margin_sec
            now = self.sim_clock.now()
            if not in_calling_window(
                now, reg_window, camp_window, margin
            ):
                abort_unplaced_attempt(self.conn, rid, now)
                return

            payload = {
                "request_id": rid,
                "campaign_id": reservation["campaign_id"],
                "lead_id": reservation["lead_id"],
                "phone": reservation["phone"],
                "callback_url": self.settings.callback_url,
            }
            status_code, data = await self.telephony.place_call(payload)

            if status_code in (200, 201) and data:
                confirm_call_accepted(
                    self.conn, rid, data["call_sid"], self.sim_clock.now()
                )
                if data.get("state") == "ended" and data.get("result"):
                    apply_call_result(
                        self.conn,
                        data["result"],
                        raw_webhook_body=None,
                        sim_now=self.sim_clock.now(),
                    )
            elif status_code == 503:
                # Transient provider rejection -> abort attempt cleanly
                abort_unplaced_attempt(self.conn, rid, self.sim_clock.now())
            elif status_code == 504:
                # Outcome unknown -> keep PENDING_DISPATCH; recovery will resolve it
                logger.warning("504 timeout on call placement: %s", rid)
            elif status_code in (409, 422, 429):
                logger.warning(
                    "Provider rejected call %s with HTTP %d: %s",
                    rid,
                    status_code,
                    data,
                )
                abort_unplaced_attempt(self.conn, rid, self.sim_clock.now())
            else:
                logger.error(
                    "Unexpected telephony status %d on request %s: %s",
                    status_code,
                    rid,
                    data,
                )
        finally:
            in_flight_requests.discard(rid)

    async def run_until_complete(
        self, stop_event: Optional[asyncio.Event] = None
    ) -> None:
        """
        Main dispatcher loop.
        
        Runs until all leads reach a terminal state (or stop_event is set).
        """
        # Initial startup crash recovery
        await reconcile_attempts(
            self.conn, self.telephony, self.sim_clock, startup=True
        )

        last_clock_sync = 0.0

        while not is_all_leads_terminal(self.conn):
            if stop_event is not None and stop_event.is_set():
                break

            now_real = time.time()
            if now_real - last_clock_sync >= 2.0:
                try:
                    clock_data = await self.telephony.get_clock()
                    if hasattr(self.sim_clock, "sync_from_payload"):
                        self.sim_clock.sync_from_payload(clock_data)
                    last_clock_sync = now_real
                except Exception:
                    pass

            # Regular reconciliation tick
            await reconcile_attempts(
                self.conn, self.telephony, self.sim_clock, startup=False
            )

            # Query candidate leads
            margin = self.effective_safety_margin_sec
            candidates = select_next_candidates(
                self.conn,
                self.sim_clock.now(),
                margin,
            )

            for cid, lid in candidates:
                res = reserve_call(
                    self.conn,
                    cid,
                    lid,
                    self.sim_clock.now(),
                    margin,
                )
                if res:
                    task = asyncio.create_task(self._dispatch_single(res))
                    self.active_tasks.add(task)
                    task.add_done_callback(self.active_tasks.discard)

            await asyncio.sleep(0.05)

        # Wait for all in-flight dispatch tasks to complete
        if self.active_tasks:
            await asyncio.gather(*self.active_tasks, return_exceptions=True)

