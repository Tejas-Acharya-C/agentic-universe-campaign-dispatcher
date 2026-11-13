"""Reporting module for terminal state detection and CSV generation."""

import csv
import os
import sqlite3
import tempfile
from typing import Dict

STATE_MAPPING: Dict[str, str] = {
    "COMPLETED": "completed",
    "EXHAUSTED": "exhausted",
    "DND_BLOCKED": "dnd_blocked",
}


def is_all_leads_terminal(conn: sqlite3.Connection) -> bool:
    """
    Check if all leads have reached a final terminal state and no calls are active.
    
    Returns True iff:
    1. Total leads > 0
    2. Zero leads in non-terminal states
    3. Zero call attempts in PENDING_DISPATCH or ACTIVE
    """
    total_leads = conn.execute("SELECT COUNT(*) FROM leads;").fetchone()[0]
    if total_leads == 0:
        return False

    non_terminal_leads = conn.execute(
        """
        SELECT COUNT(*) FROM leads
        WHERE state NOT IN ('COMPLETED', 'EXHAUSTED', 'DND_BLOCKED');
        """
    ).fetchone()[0]
    if non_terminal_leads > 0:
        return False

    active_attempts = conn.execute(
        """
        SELECT COUNT(*) FROM call_attempts
        WHERE state IN ('PENDING_DISPATCH', 'ACTIVE');
        """
    ).fetchone()[0]
    return active_attempts == 0


def write_final_state_csv(conn: sqlite3.Connection, output_path: str) -> int:
    """
    Write final_state.csv atomically ordered by (campaign_id ASC, lead_id ASC).
    
    Header format:
    campaign_id,lead_id,final_state,attempts,last_status,last_disposition
    """
    abs_output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(abs_output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    rows = conn.execute(
        """
        SELECT campaign_id, lead_id, state, attempts_count, last_status, last_disposition
        FROM leads
        ORDER BY campaign_id ASC, lead_id ASC;
        """
    ).fetchall()

    fd, temp_file_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "campaign_id",
                "lead_id",
                "final_state",
                "attempts",
                "last_status",
                "last_disposition",
            ]
        )
        for r in rows:
            final_st = STATE_MAPPING.get(r["state"], r["state"].lower())
            writer.writerow(
                [
                    r["campaign_id"],
                    r["lead_id"],
                    final_st,
                    r["attempts_count"],
                    r["last_status"] or "",
                    r["last_disposition"] or "",
                ]
            )

    os.replace(temp_file_path, abs_output_path)
    return len(rows)

