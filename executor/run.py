"""
Run one scenario end-to-end against the real mock UI.

    python run.py happy       # one clean transfer -> confirmed
    python run.py phantom     # submit posts, no receipt -> unknown -> reconcile
    python run.py escalate    # unexpected dialog -> freeze -> work item

    add --headed to watch the browser drive the screen.
"""
import sys
import json
import threading

import mock_app
from driver import Driver
from ledger import Ledger
from executor import run_transfer, Trace

PORT = 8799
SCENARIOS = {"happy": "none", "phantom": "phantom", "escalate": "escalate"}


def main():
    scenario = next((a for a in sys.argv[1:] if not a.startswith("-")), "happy")
    headed = "--headed" in sys.argv
    if scenario not in SCENARIOS:
        print(f"unknown scenario '{scenario}'. choose: {', '.join(SCENARIOS)}"); sys.exit(1)

    # reset mock state and arm the fault for this scenario
    mock_app.FAULT = SCENARIOS[scenario]
    mock_app.SESSION.clear()
    mock_app.POSTINGS.clear()

    server = mock_app.serve(PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"\n=== scenario: {scenario}  (fault={mock_app.FAULT}) ===")
    ledger = Ledger()                       # in-memory for a clean run each time
    trace = Trace("trace.jsonl")
    driver = Driver(f"http://127.0.0.1:{PORT}", headed=headed, slow=250 if headed else 0)
    try:
        ik = run_transfer(driver, ledger, trace)
    finally:
        driver.close()
        server.shutdown()

    row = ledger.row(ik)
    print("\n=== final ledger row ===")
    print(json.dumps(row, indent=2))
    print(f"\nmoney actually posted at the bank: {len(mock_app.POSTINGS)} transfer(s)")
    print("full evidence trace -> executor/trace.jsonl\n")


if __name__ == "__main__":
    main()
