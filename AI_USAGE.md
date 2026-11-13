# AI Usage Disclosure

## Tools used
- **Gemini:** Architecture planning, PRD/traceability analysis, phase-by-phase implementation structuring, and business rule edge case auditing.
- **Antigravity IDE Agent:** Code scaffolding, unit and integration test creation, and interactive execution of verification harnesses against the mock telephony server.

## What I used them for
- Auditing `assignment-b-workflow/mock_telephony/server.py` and `README.md` to identify all potential 409, 422, 429, 503, and 504 error triggers.
- Scaffolding the SQLite persistence schema (`tenants`, `campaigns`, `leads`, `call_attempts`, `phone_suppressions`, `system_metadata`) and two-phase `PENDING_DISPATCH` state machine.
- Generating modular Python files across configuration, rules math, scheduler, FastAPI webhook receiver, HTTPX client, recovery reconciler, and CSV reporter.
- Writing the 24 fast, deterministic `pytest` tests using `FakeClock`.

## What I wrote or decided myself
- Architecting the solution around an embedded SQLite WAL database with `BEGIN IMMEDIATE` transactions rather than introducing external orchestration services (like Temporal), keeping the dispatcher zero-dependency, self-contained, and fast.
- Formulating the strict disposition and status precedence rule (`completed > exhausted > dnd_blocked`) so successful conversions are never obscured by attempt exhaustion or downstream opt-outs.
- Designing the two-phase intent reservation pattern (`PENDING_DISPATCH` committed before making the outbound HTTP request) combined with partial unique indexing on active phone numbers to prevent concurrent duplicate dialing.
- Identifying and handling the race condition where a webhook arrives at the webhook server before the corresponding `POST /v1/calls` response returns `201` to the dispatcher client.
- Defining the crash-recovery and stale-active reconciliation logic to reconcile orphaned attempts against `GET /v1/calls?request_id=...` and recover lost webhooks without double-dialing.

## Where the AI was wrong or unhelpful
- **Static Window Safety Margin:** The AI initially used a static 15-second simulated window safety margin (`window_safety_margin_sec = 15`). While this passed pure math unit tests, under live `1200x` simulation speed, 15 simulated seconds equals only 12.5 milliseconds in real time. Because network round-trip and process scheduling under chaos mode took ~114 milliseconds, a call reserved at 20:29:43 arrived at the mock server at 20:32:02—breaching the server's 120s grace period by 2 seconds and triggering an `outside_calling_window` violation.
- **Resolution:** Caught during chaos verification and resolved by introducing `effective_safety_margin_sec` in `Dispatcher`, which dynamically scales the safety cushion with simulation speed (`max(settings.window_safety_margin_sec, int(speed * 0.30))`), ensuring that real-world network latency can never push call delivery beyond window close.

## How I verified AI-generated code
- **Line-by-Line Code Review:** Manually inspected every generated function in `solution/app/` to ensure thread safety (`check_same_thread=False`), transactional integrity (`write_tx`), correct IST timezone offsets (`+05:30`), and exact SQL queries.
- **Deterministic Unit Testing:** Executed 24 unit and integration tests covering simulated clock mechanics, window boundaries, backoff math, tenant capacity caps, phone mutex locks, and HMAC signatures.
- **Live End-to-End Testing:** Ran live executions against `mock_telephony` at both `2400x` baseline speed (`--failure-scale 0`) and `1200x` chaos speed (`--failure-scale 1.0`).
- **Hard Process Kill & Crash Invariant Verification:** Sent hard termination signals (`taskkill /F /T` / `kill -9`) mid-run while calls were actively in-flight, restarted the dispatcher, and verified `violation_count == 0` on `GET /admin/report`.
- **Complete Dataset Diffing:** Verified all 200 rows of generated `final_state.csv` against the ground-truth `GET /admin/leads` endpoint, confirming 0 discrepancies in lead state, attempt count, last status, and last disposition.
