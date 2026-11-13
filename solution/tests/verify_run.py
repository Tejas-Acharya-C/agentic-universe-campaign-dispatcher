"""Verification runner for live mock telephony testing (clean & chaos runs)."""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import httpx


def find_repo_root() -> str:
    cur = os.path.dirname(os.path.abspath(__file__))
    while cur != os.path.dirname(cur):
        if os.path.exists(os.path.join(cur, "assignment-b-workflow")):
            return cur
        cur = os.path.dirname(cur)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_verification(failure_scale: float, speed: float, seed: int, kill_after: float = 0.0):
    repo_root = find_repo_root()
    workflow_dir = os.path.join(repo_root, "assignment-b-workflow")
    solution_dir = os.path.join(repo_root, "solution")

    # Clean previous artifacts
    for name in ["dispatcher.db", "dispatcher.db-wal", "dispatcher.db-shm", "final_state.csv"]:
        p = os.path.join(solution_dir if "dispatcher" in name else repo_root, name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception as e:
                print(f"Warning: could not remove {p}: {e}")

    # Ensure ports 8099 and 8200 are free
    if sys.platform == "win32":
        subprocess.run(
            'powershell -Command "Get-NetTCPConnection -LocalPort 8099, 8200 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"',
            shell=True,
            capture_output=True,
        )
        time.sleep(0.5)

    # 1. Start mock telephony
    mock_cmd = [
        sys.executable,
        "-m",
        "mock_telephony",
        "--failure-scale",
        str(failure_scale),
        "--speed",
        str(speed),
        "--seed",
        str(seed),
    ]
    print(f"Starting mock telephony: {' '.join(mock_cmd)}")
    mock_proc = subprocess.Popen(mock_cmd, cwd=workflow_dir)

    try:
        # Wait for mock server
        client = httpx.Client(base_url="http://127.0.0.1:8099", timeout=5.0)
        clock_ok = False
        for _ in range(30):
            try:
                r = client.get("/v1/clock")
                if r.status_code == 200:
                    clock_ok = True
                    break
            except Exception:
                time.sleep(0.2)

        if not clock_ok:
            raise RuntimeError("Mock telephony failed to start on http://127.0.0.1:8099")
        print("Mock telephony is ready.")

        # 2. Run dispatcher
        dispatcher_cmd = [sys.executable, "-m", "solution.app.main"]

        if kill_after > 0:
            print(f"Starting dispatcher for {kill_after}s before hard kill...")
            disp_proc = subprocess.Popen(dispatcher_cmd, cwd=repo_root)
            time.sleep(kill_after)
            print(f"Killing dispatcher PID {disp_proc.pid}...")
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(disp_proc.pid)], capture_output=True)
            else:
                disp_proc.kill()
            disp_proc.wait()
            print("Dispatcher killed. Waiting 0.5s before restarting...")
            time.sleep(0.5)

            print("Restarting dispatcher to completion...")
            restart_proc = subprocess.Popen(dispatcher_cmd, cwd=repo_root)
            exit_code = restart_proc.wait()
            print(f"Restarted dispatcher finished with exit code {exit_code}")
            if exit_code != 0:
                raise RuntimeError(f"Dispatcher exited with code {exit_code}")
        else:
            print("Running dispatcher to completion...")
            disp_proc = subprocess.Popen(dispatcher_cmd, cwd=repo_root)
            exit_code = disp_proc.wait()
            print(f"Dispatcher finished with exit code {exit_code}")
            if exit_code != 0:
                raise RuntimeError(f"Dispatcher exited with code {exit_code}")

        # 3. Query /admin/report
        report_resp = client.get("/admin/report")
        report = report_resp.json()
        print("\n================== /admin/report ==================")
        print(json.dumps(report, indent=2))
        print("===================================================\n")

        # 4. Query /admin/leads
        leads_resp = client.get("/admin/leads")
        expected_leads = leads_resp.json()

        # 5. Read final_state.csv
        csv_path = os.path.join(repo_root, "final_state.csv")
        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"final_state.csv not found at {csv_path}")

        actual_leads = {}
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                actual_leads[(row["campaign_id"], row["lead_id"])] = row

        print(f"Total rows in final_state.csv: {len(actual_leads)}")
        print(f"Total expected leads from /admin/leads: {len(expected_leads)}")

        # 6. Diff verification
        mismatches = []
        for exp in expected_leads:
            cid = exp["campaign_id"]
            lid = exp["lead_id"]
            key = (cid, lid)
            if key not in actual_leads:
                mismatches.append(f"Missing lead {key} in final_state.csv")
                continue

            act = actual_leads[key]
            if act["final_state"] != exp["expected_state"]:
                mismatches.append(
                    f"State mismatch for {key}: csv={act['final_state']} != expected={exp['expected_state']}"
                )
            if int(act["attempts"]) != exp["attempts"]:
                mismatches.append(
                    f"Attempts mismatch for {key}: csv={act['attempts']} != expected={exp['attempts']}"
                )
            if act["last_status"] != exp["last_status"]:
                mismatches.append(
                    f"Last status mismatch for {key}: csv={act['last_status']} != expected={exp['last_status']}"
                )
            if act["last_disposition"] != exp["last_disposition"]:
                mismatches.append(
                    f"Last disposition mismatch for {key}: csv={act['last_disposition']} != expected={exp['last_disposition']}"
                )

        if mismatches:
            print(f"FAILURE: {len(mismatches)} mismatches detected:")
            for m in mismatches[:10]:
                print(f"  - {m}")
            if len(mismatches) > 10:
                print(f"  ... and {len(mismatches) - 10} more")
            sys.exit(1)
        else:
            print("SUCCESS: 0 mismatches across all 200 leads!")

        # 7. Check violations
        violation_count = report.get("violation_count", 0)
        print(f"Violation count: {violation_count}")
        if violation_count > 0:
            print(f"FAILURE: Detected {violation_count} rule violations!")
            sys.exit(1)
        else:
            print("SUCCESS: 0 business rule violations!")

    finally:
        print("Stopping mock telephony server...")
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(mock_proc.pid)], capture_output=True)
        else:
            mock_proc.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live mock telephony test runner")
    parser.add_argument("--failure-scale", type=float, default=0.0)
    parser.add_argument("--speed", type=float, default=2400.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--kill-after", type=float, default=0.0)

    args = parser.parse_args()
    run_verification(args.failure_scale, args.speed, args.seed, args.kill_after)

