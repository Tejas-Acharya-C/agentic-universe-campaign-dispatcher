"""Fixture loader for campaigns, tenants, metadata, and leads."""

import csv
import json
import os
import sqlite3
from typing import Any, Dict

from solution.app.database import write_tx


def load_fixtures(conn: sqlite3.Connection, fixtures_path: str) -> Dict[str, Any]:
    """
    Load campaigns.json and associated leads CSV files into SQLite.
    
    Operations are executed idempotently inside a single immediate write transaction:
    - Upserts system metadata
    - Upserts tenants and campaigns
    - Inserts new leads with INSERT OR IGNORE (existing leads remain untouched)
    - Suppresses active leads whose phone exists in phone_suppressions
    """
    abs_fixtures_path = os.path.abspath(fixtures_path)
    base_dir = os.path.dirname(abs_fixtures_path)

    with open(abs_fixtures_path, "r", encoding="utf-8") as f:
        fixtures = json.load(f)

    sim_start = fixtures.get("sim_start", "")

    with write_tx(conn):
        # 1. Upsert system metadata
        metadata_items = [
            ("sim_start", str(fixtures["sim_start"])),
            ("timezone", str(fixtures["timezone"])),
            ("regulatory_window", json.dumps(fixtures["regulatory_window"])),
            ("window_grace_seconds", str(fixtures["window_grace_seconds"])),
            ("retry_policy", json.dumps(fixtures["retry_policy"])),
            ("webhook_secret", str(fixtures["webhook_secret"])),
        ]
        for key, value in metadata_items:
            conn.execute(
                """
                INSERT INTO system_metadata (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                (key, value),
            )

        # 2. Upsert tenants
        tenants = fixtures.get("tenants", [])
        for tenant in tenants:
            conn.execute(
                """
                INSERT INTO tenants (tenant_id, max_concurrent_calls)
                VALUES (?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET max_concurrent_calls = excluded.max_concurrent_calls;
                """,
                (tenant["tenant_id"], int(tenant["max_concurrent_calls"])),
            )

        # 3. Upsert campaigns
        campaigns = fixtures.get("campaigns", [])
        for campaign in campaigns:
            window_days_json = json.dumps(campaign["window"]["days"])
            conn.execute(
                """
                INSERT INTO campaigns (
                    campaign_id, tenant_id, leads_file,
                    window_start, window_end, window_days, max_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    leads_file = excluded.leads_file,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    window_days = excluded.window_days,
                    max_attempts = excluded.max_attempts;
                """,
                (
                    campaign["campaign_id"],
                    campaign["tenant_id"],
                    campaign["leads_file"],
                    campaign["window"]["start"],
                    campaign["window"]["end"],
                    window_days_json,
                    int(campaign["max_attempts"]),
                ),
            )

            # 4. Load leads CSV for this campaign
            leads_file_path = os.path.join(base_dir, campaign["leads_file"])
            with open(leads_file_path, "r", encoding="utf-8") as lf:
                reader = csv.DictReader(lf)
                for row in reader:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO leads (
                            campaign_id, lead_id, tenant_id, phone, customer_name,
                            state, attempts_count, next_eligible_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'FRESH', 0, ?, ?);
                        """,
                        (
                            campaign["campaign_id"],
                            row["lead_id"],
                            campaign["tenant_id"],
                            row["phone"],
                            row.get("customer_name", ""),
                            sim_start,
                            sim_start,
                        ),
                    )

        # 5. Apply suppressions to active leads matching phone_suppressions
        conn.execute(
            """
            UPDATE leads
            SET state = 'DND_BLOCKED',
                updated_at = ?
            WHERE phone IN (SELECT phone FROM phone_suppressions)
              AND state IN ('FRESH', 'RETRY_WAIT', 'CALLBACK_WAIT');
            """,
            (sim_start,),
        )

        total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    return {
        "tenants": len(tenants),
        "campaigns": len(campaigns),
        "leads_total": total_leads,
    }

