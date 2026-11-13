"""Main entry point for the Durable Campaign Dispatcher service."""

import argparse
import asyncio
import logging
import sys
from fastapi import FastAPI
import uvicorn

from solution.app.clock import SimClock
from solution.app.config import Settings
from solution.app.database import get_connection, init_db
from solution.app.dispatcher import Dispatcher
from solution.app.loader import load_fixtures
from solution.app.reporting import write_final_state_csv
from solution.app.telephony import TelephonyClient
from solution.app.webhooks import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dispatcher")


def parse_args(args=None) -> Settings:
    """Parse CLI arguments and return configured Settings."""
    parser = argparse.ArgumentParser(description="Durable Campaign Dispatcher")
    parser.add_argument("--provider-url", dest="provider_url", help="Provider base URL")
    parser.add_argument("--callback-host", dest="callback_host", help="Callback webhook host")
    parser.add_argument(
        "--callback-port", dest="callback_port", type=int, help="Callback webhook port"
    )
    parser.add_argument(
        "--fixtures", dest="fixtures_path", help="Path to campaigns.json fixtures"
    )
    parser.add_argument("--db", dest="db_path", help="Path to SQLite database")
    parser.add_argument(
        "--output", dest="output_csv_path", help="Path to output final_state.csv"
    )
    parser.add_argument(
        "--safety-margin",
        dest="window_safety_margin_sec",
        type=int,
        help="Window safety margin in seconds",
    )

    parsed, _ = parser.parse_known_args(args)
    overrides = {k: v for k, v in vars(parsed).items() if v is not None}
    return Settings.from_cli(**overrides)


async def main_async(settings: Settings) -> None:
    """Initialize resources, run webhook server and dispatcher, and output final CSV."""
    conn = get_connection(settings.db_path)
    init_db(conn)
    summary = load_fixtures(conn, settings.fixtures_path)
    logger.info("Loaded fixtures: %s", summary)

    app = FastAPI(title="Campaign Dispatcher Webhook Service")
    app.state.db = conn
    app.include_router(router)

    config = uvicorn.Config(
        app=app,
        host=settings.callback_host,
        port=settings.callback_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    telephony = TelephonyClient(settings.provider_url)

    logger.info("Connecting to telephony provider at %s ...", settings.provider_url)
    clock_data = None
    for _ in range(30):
        try:
            clock_data = await telephony.get_clock()
            break
        except Exception:
            await asyncio.sleep(0.5)

    if not clock_data:
        logger.error(
            "Failed to fetch clock from provider at %s", settings.provider_url
        )
        server.should_exit = True
        await server_task
        await telephony.close()
        conn.close()
        sys.exit(1)

    sim_clock = SimClock.from_payload(clock_data)
    logger.info("Synchronized clock with provider: sim_now = %s", sim_clock.now())

    dispatcher = Dispatcher(
        conn=conn,
        settings=settings,
        sim_clock=sim_clock,
        telephony=telephony,
    )

    try:
        await dispatcher.run_until_complete()
        rows_written = write_final_state_csv(conn, settings.output_csv_path)
        logger.info(
            "All leads terminal. Wrote %d rows to %s",
            rows_written,
            settings.output_csv_path,
        )
    finally:
        server.should_exit = True
        await server_task
        await telephony.close()
        conn.close()


def main() -> None:
    """Synchronous entry point."""
    settings = parse_args()
    asyncio.run(main_async(settings))


if __name__ == "__main__":
    main()

