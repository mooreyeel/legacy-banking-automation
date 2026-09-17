# executor — a runnable miniature of the design

~400 lines that run **one `transfer_funds` end-to-end** through a real (ugly) mock
legacy bank UI, driving the actual DOM with Playwright. This is the design from
[`../DESIGN.md`](../DESIGN.md) actually executing — not an animation.

## What it demonstrates

| Piece | File | Maps to |
|---|---|---|
| Ugly legacy console (nested tables, labels only, no test-IDs), **stateful** so money really posts | `mock_app.py` | §3 "messy UI" target |
| Driver: `resolve()` locates by **tier** (label → structural) and logs which fired; `identify()` by screen **signature** incl. a negative condition | `driver.py` | §3 driving the UI |
| Guarded **intent ledger** — transitions only succeed if exactly one row moves from the expected state | `ledger.py` | §2 execution model |
| Executor: check-then-act, readback verify, member binding, **stop-at-confirm**, approval-gated single submit, capture / reconcile / escalate, JSONL evidence | `executor.py` | §1–§7 |

## Run it

```bash
cd executor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python run.py happy        # clean transfer  -> ledger: confirmed
python run.py phantom      # click posts, no receipt -> unknown -> read-only reconcile -> confirmed
python run.py escalate     # unexpected dialog -> freeze -> work item (nothing irreversible fired)

python run.py happy --headed   # watch the browser drive the screen
```

> If `playwright install chromium` can't download in a restricted network, pin to a
> Playwright release whose browser build is already cached (e.g. `pip install
> playwright==1.53.0`) and re-run — the code is version-agnostic.

## The moment that matters

`python run.py phantom`: the submit posts the money, but the confirmation screen
never renders. The executor lands in **`unknown`**, **refuses to retry** (a blind
retry is a double-send), runs the read-only `did_intent_post` against Recent
Activity, matches the stamped memo ref, writes the receipt, and moves the ledger
`unknown → confirmed`. Truthful answer, no double-send. That's the phantom-send
war story — running.
