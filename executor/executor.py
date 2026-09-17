"""
The executor — drives one transfer_funds end-to-end through the real mock UI.

The whole loop is: look -> identify the screen -> locate a field by tier -> act
-> verify the readback. Money-moving steps fill everything, STOP at the confirm
screen (ledger: prepared), and the submit fires only after an outside approval
advances the ledger to triggered. If the confirmation never renders we land in
`unknown` and reconcile read-only rather than retrying blind.
"""
import re
import json
import time
import uuid
import hashlib
from pathlib import Path

from driver import Driver
from ledger import Ledger

# --- the task, in the declarative require/act/expect shape (illustrative) ---
TASK = {
    "task": "transfer_funds", "version": "1.4.0", "label": "irreversible",
    "steps": [
        {"id": "open_member",  "require": "member_search",  "act": "type member_id, Search"},
        {"id": "bind",         "require": "member_summary", "act": "verify accounts belong to member"},
        {"id": "fill_form",    "require": "transfer_form",  "act": "set from,to,amount,memo (readback)"},
        {"id": "gate",         "require": "transfer_confirm", "act": "match summary; ledger->prepared; await approval"},
        {"id": "submit",       "act": "click submit (single fire)",
                               "expect": {"ok": "receipt", "timeout": "unknown_protocol"}},
        {"id": "capture",      "require": "receipt", "act": "read confirmation_number; ledger->confirmed"},
    ],
    "unknown_protocol": "did_intent_post(intent_key) read-only, then confirm",
}

INPUTS = {"from_a": "Checking ****1204", "to_a": "Savings ****9917",
          "amount": "250.00", "member_id": "M-4471"}


class Trace:
    """One event per line: pretty to stdout, JSONL to disk for the auditor."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.write_text("")

    def __call__(self, ev, **kw):
        rec = {"ev": ev, **kw}
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        detail = "  ".join(f"{k}={v}" for k, v in kw.items())
        print(f"  · {ev:<18} {detail}")


def approve(intent_key, amount):
    """The outside go-ahead: an institution rule. Real approval could be a human."""
    return float(amount) <= 500.0                 # rule: internal <= $500


def _member_ref(member_id):
    return "sha256:" + hashlib.sha256(member_id.encode()).hexdigest()[:10]


def _work_item(intent_key, observed, ledger_state):
    return {
        "ticket": "wi_" + uuid.uuid4().hex[:4], "lane": "member_waiting",
        "task": f"{TASK['task']}@{TASK['version']}", "step": "gate",
        "failed_check": {"expected": "transfer_confirm", "observed": observed},
        "inputs_masked": {"from": "****1204", "to": "****9917", "amount": "$250.00"},
        "irreversible_done": False, "ledger_state": ledger_state,
        "live_session": {"control_url": "https://ops.internal/cede/...", "expires_in_s": 900},
    }


def run_transfer(driver: Driver, ledger: Ledger, trace: Trace):
    ik = str(uuid.uuid4())
    ref = ik[:8]                                    # stamped into the memo
    ledger.create(ik, TASK["task"], TASK["version"], _member_ref(INPUTS["member_id"]))
    trace("run_created", intent=ik[:8], task=f"{TASK['task']}@{TASK['version']}", ledger="created")

    # --- open_member: identify, locate by tier, act, verify readback ---
    driver.goto("/member_search")
    _require(driver, trace, "member_search")
    loc, tier = driver.resolve("Member ID")
    got = driver.set(loc, INPUTS["member_id"])
    trace("act", field="member_id", tier=tier, readback="ok" if got == INPUTS["member_id"] else "MISMATCH")
    driver.click("Search")

    # --- bind: accounts in the request must belong to the bound member ---
    _require(driver, trace, "member_summary")
    on_screen = [a for a in ("Checking ****1204", "Savings ****9917") if a in driver.body_text()]
    bound = all(INPUTS[k] in on_screen for k in ("from_a", "to_a"))
    trace("member_binding", accounts=len(on_screen), ok=bound)
    if not bound:
        trace("decline", reason="ACCT_NOT_FOUND"); return ik
    driver.click("Transfer Funds", kind="link")

    # --- fill_form: each value set then read back ---
    _require(driver, trace, "transfer_form")
    for field, label, is_sel in [("from_a", "From Account", True), ("to_a", "To Account", True),
                                 ("amount", "Amount", False), ("memo", "Memo", False)]:
        val = ref if field == "memo" else INPUTS[field]
        loc, tier = driver.resolve(label, kind="select" if is_sel else "input")
        got = driver.set(loc, val, select=is_sel)
        trace("act", field=field, tier=tier, value=val, readback="ok" if got == val else "MISMATCH")
    driver.click("Continue")

    # --- gate: STOP at confirm. escalate on anything unexpected. ---
    screen = driver.identify()
    if screen != "transfer_confirm":
        trace("check", step="gate", expected="transfer_confirm", observed=screen, ok=False)
        observed = "dialog: Account flagged for review" if screen == "flagged" else screen
        wi = _work_item(ik, observed, ledger.state(ik))
        trace("escalation_created", wi=wi["ticket"], ledger=ledger.state(ik))
        print("\n  WORK ITEM (frozen — zero irreversible actions):")
        print("  " + json.dumps(wi, indent=2).replace("\n", "\n  "))
        return ik
    ok_match = f"${INPUTS['amount']}" in driver.body_text()
    trace("check", step="gate", expected="transfer_confirm", match_amount=ok_match, ok=True)
    ledger.advance(ik, "created", "prepared")
    trace("ledger", state="prepared", note="filled; stopped; awaiting outside go-ahead")

    # --- the one red arrow: submit fires only after approval advances the ledger ---
    if not approve(ik, INPUTS["amount"]):
        ledger.advance(ik, "prepared", "aborted"); trace("ledger", state="aborted"); return ik
    if not ledger.advance(ik, "prepared", "triggered"):     # guarded transition
        trace("race_lost", note="another executor already advanced this intent"); return ik
    trace("approval", rule="internal <= $500", ledger="triggered")

    # --- submit: single fire, gated by the ledger being 'triggered' ---
    assert ledger.state(ik) == "triggered", "submit only fires from triggered"
    driver.click("Submit Transfer")

    # --- capture, with a post-submit wait budget (never retry the click) ---
    conf = _capture_with_budget(driver, trace)
    if conf:
        ledger.advance(ik, "triggered", "confirmed")
        ledger.write_receipt(ik, {"confirmation_number": conf, "via": "capture"})
        trace("ledger", state="confirmed", receipt=conf)
        return ik

    # --- phantom send: unknown -> read-only reconcile, never a blind retry ---
    ledger.advance(ik, "triggered", "unknown")
    trace("ledger", state="unknown", note="post-submit wait budget exceeded")
    conf = _did_intent_post(driver, trace, ref)
    if conf:
        ledger.advance(ik, "unknown", "confirmed")
        ledger.write_receipt(ik, {"confirmation_number": conf, "via": "reconciliation"})
        trace("ledger", state="confirmed", receipt=conf, note="resolved by reconciliation")
    else:
        trace("reconcile", found=False, note="no matching posting — stays unknown for a human")
    return ik


def _require(driver, trace, expected):
    seen = driver.identify()
    trace("identify", expected=expected, observed=seen, ok=(seen == expected))
    if seen != expected:
        raise RuntimeError(f"expected {expected}, saw {seen}")


def _capture_with_budget(driver, trace, tries=3, pause=0.4):
    for _ in range(tries):
        if driver.identify() == "receipt":
            m = re.search(r"CN-\d+-\d+", driver.body_text())
            conf = m.group(0) if m else None
            trace("capture", confirmation_number=conf, ok=bool(conf))
            return conf
        time.sleep(pause)
    return None


def _did_intent_post(driver, trace, ref):
    """Read-only: read Recent Activity BEFORE touching anything; match the memo ref."""
    driver.goto("/recent_activity")
    trace("reconcile", task="did_intent_post", ref=ref)
    if f"ref {ref}" in driver.body_text():
        trace("reconcile", match=f"ref {ref}", ok=True)
        return "posted:" + ref
    return None
