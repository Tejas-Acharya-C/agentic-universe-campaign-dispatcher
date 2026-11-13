# Assignment B: Durable Campaign Dispatcher

A robust, crash-resilient campaign dialing engine designed to dispatch outbound calls according to strict business constraints (calling windows, tenant concurrency limits, phone exclusivity, callback schedules, retry policies, and global DND suppressions) against a mock telephony provider.

---

## Architecture Overview

```
                      +---------------------------------------+
                      |       Mock Telephony Provider         |
                      |        http://127.0.0.1:8099          |
                      +---------------------------------------+
                           ^                         |
           POST /v1/calls  |                         | Inbound Webhooks
           GET /v1/clock   |                         | (HMAC-SHA256 Signed)
           GET /v1/calls   |                         v
                      +----+-------------------------+--------+
                      |           Campaign Dispatcher         |
                      |                                       |
                      |  +---------------------------------+  |
                      |  |      FastAPI Webhook Server     |  |
                      |  |      http://127.0.0.1:8200      |  |
                      |  +---------------------------------+  |
                      |  |   Async Priority Dispatcher     |  |
                      |  |   (Callbacks > Retries > Fresh) |  |
                      |  +---------------------------------+  |
                      |  |   Recovery & Stale Reconciler   |  |
                      |  |   (503 / 504 / Lost Webhooks)   |  |
                      |  +---------------------------------+  |
                      |                   |                   |
                      |                   v                   |
                      |     SQLite WAL Database (Embedded)    |
                      |       (PRAGMA synchronous = FULL)     |
                      +---------------------------------------+
```

### Key Modules inside `solution/app/`:
- `config.py`: Environment configuration and CLI override parser.
- `database.py`: SQLite persistence layer with WAL mode, foreign keys, 5000ms busy timeout, and atomic `write_tx()` (`BEGIN IMMEDIATE`) context manager.
- `loader.py`: Idempotent JSON fixture loader (`tenants`, `campaigns`, `leads`, `phone_suppressions`).
- `clock.py`: Timezone-aware IST (`UTC+05:30`) clock abstraction (`SimClock` for mock provider time, `FakeClock` for tests).
- `rules.py`: Side-effect-free pure mathematical functions for calling windows, forward window searches, backoffs, callback eligibility, and HMAC-SHA256 signature verification.
- `state_machine.py`: Atomic lead state transitions (`reserve_call`, `confirm_call_accepted`, `abort_unplaced_attempt`, `apply_call_result`).
- `scheduler.py`: Priority candidate selection ordering (`CALLBACK_WAIT` > `RETRY_WAIT` > `FRESH`) under tenant concurrency and phone exclusivity constraints.
- `webhooks.py`: FastAPI webhook receiver validating `X-AU-Signature` and recording outcomes atomically.
- `telephony.py`: Async HTTPX provider client with error handling for 503, 504, 409, 422, and 429 status codes.
- `recovery.py`: Startup and periodic background reconciler for orphaned `PENDING_DISPATCH` calls and lost-webhook `ACTIVE` calls (>410 sim seconds).
- `reporting.py`: Terminal state detection and sorted `final_state.csv` writer.
- `dispatcher.py`: Concurrent asyncio dispatching engine with speed-scaled window safety cushion (`effective_safety_margin_sec`).
- `main.py`: Unified CLI entrypoint running the FastAPI server and dispatcher loop concurrently.

---

## Getting Started

### 1. Environment Setup

From the repository root:
```bash
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate
# On Windows (PowerShell):
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Running Unit & Integration Tests

Run the full automated test suite (24 tests, runs in ~1.2s without sleeps):
```bash
pytest solution/tests/
```

### 3. Running Against Mock Telephony

#### Step A: Start Mock Telephony Provider
In terminal 1:
```bash
# Clean baseline run (2400x speed)
python -m mock_telephony --failure-scale 0 --speed 2400 --seed 7

# Or chaos mode (1200x speed with 503s, 504s, lost/duplicate webhooks)
# python -m mock_telephony --failure-scale 1.0 --speed 1200 --seed 42
```

#### Step B: Start Dispatcher
In terminal 2:
```bash
python -m solution.app.main
```
The dispatcher will:
1. Initialize SQLite database (`solution/dispatcher.db`) and load fixtures.
2. Synchronize clock with provider (`GET /v1/clock`).
3. Reconcile any pre-existing or orphaned attempts.
4. Dispatch calls according to priority, concurrency caps, and calling windows.
5. Receive webhooks at `http://127.0.0.1:8200/webhooks/telephony`.
6. Once all leads reach a terminal state, output `final_state.csv` and shut down cleanly.

### 4. Running the End-to-End Verification Harness

An automated verification harness is provided in `solution/tests/verify_run.py`. It boots the mock server, launches the dispatcher (with optional mid-run hard kill and restart), verifies `/admin/report`, and diffs `final_state.csv` against `GET /admin/leads`:

```bash
# Chaos mode + Mid-Run Hard Kill after 4.0 seconds
python solution/tests/verify_run.py --failure-scale 1.0 --speed 1200 --seed 42 --kill-after 4.0
```
