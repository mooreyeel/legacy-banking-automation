# Automation for Legacy Banking

*A design for an AI system that operates legacy back-office bank applications through their UI — reliably, cheaply, and auditably.*

---

## 0. Pre-read (system contract)

Banks and credit unions run back-office applications that often have **no API**, so the only way in is the UI their staff already use. Our system automates through that UI: the agent calls a typed task — say `transfer_funds(from_acct, to_acct, amount, intent_key)` — and gets back a confirmation number or a structured reason the transfer didn't happen.

Behind the interface, our AI system signs in and works through the application screens like a human would, and stops and hands the live session to a human when it cannot proceed safely.

Take two examples throughout: **`get_balance`** (read-only) and **`transfer_funds`** (irreversible).

### Philosophy

1. A failed check stops everything.
2. An action never fires twice blindly.
3. Nothing irreversible fires without two things on record: a ledger entry proving this exact intent has not already executed, and a go-ahead from outside the system (a human, or a rule the institution set).
4. No model runs in the runtime loop.
5. Credentials and raw member data are never written to storage. Checks verify against live screen values; masking is applied at the moment evidence is written (§6).

### Overall architecture

```
Agent (the caller)  ─▶  typed task
Task API            ─▶  catalog of tasks, schema + policy checks, lock to member
Executor            ─▶  runs the task: always check-then-act, logs to intent ledger, returns an envelope
  ├─ Seam 1: screen model + per-bank overlay turn the task's logical field names into on-screen locators (swap per bank)
  └─ Seam 2: one driver interface per surface (web vs. desktop)
Driver              ─▶  drive through DOM or UI automation, or anchored OCR
Legacy application  ─▶  the UI we drive through
  ├─ When stuck ─▶ WORK ITEM: live view for human takeover
  ├─ Every run  ─▶ EVIDENCE: structured trace + masked screenshots
  └─ Secrets    ─▶ VAULT: credentials, injected in the login step only
LLM                 ─▶ works offline: authors task definitions and screen models under human review
```

---

## 1. Task contract

The agent never sees a screen; it sees a **catalog of typed tasks** and calls one. A task is a single business action with a typed result — `transfer_funds`, `get_balance`. Each is a catalog entry the agent discovers via `GET /tasks?tenant=`, which returns each task's inputs, its safety label, and whether it needs consent — so the caller knows exactly what it's about to trigger before it triggers it.

A task definition looks like this:

```yaml
task: transfer_funds
version: 1.4.0
label: irreversible          # read_only | reversible | irreversible
description: internal transfer between the bound member's accounts
inputs:  {from_acct: acct_ref, to_acct: acct_ref, amount: money>0, intent_key: uuid, consent_ref}
outputs: {confirmation_number: string, posted_at: datetime}
declines: [MEMBER_NOT_FOUND, ACCT_NOT_FOUND, INSUFFICIENT_FUNDS,
           ACCOUNT_RESTRICTED, LIMIT_EXCEEDED]
preconditions: [member_session_verified, accounts_belong_to_bound_member]
requires_consent: true
approval_state: approved     # draft | approved | suspended
sla_hint_seconds: 60
```

`get_balance(member_id)` is read-only, so it's about the same but with no consent requirement and no ledger involvement. The **label** controls whether the task can run unattended, whether a failure can be retried, how much evidence to record, and whether it must stop for human approval.

The `consent_ref` is echoed into the ledger row and the trace, so the consent that authorized a transfer is auditable after the fact.

**Invocation.** Calls are async by default: `POST /tasks/{name}:invoke` returns a `run_id`, and the caller either polls `GET /runs/{id}` or registers a callback. A `priority` of `member_waiting` or `batch` sets which queue it lands in. Read-only tasks also get a synchronous shortcut — one call that waits up to 15 s; if the task hasn't finished, it hands `run_id` back to the poll, so a slow read degrades to the async path instead of hanging.

**The envelope.** Every task returns the same shaped object: a `status`, and then exactly one of `outputs` (it worked), `decline` (a typed business reason it didn't), or `escalation` (a human is handling it). It never contains UI words, selectors, screenshots, or stack traces. An agent developer should be able to write complete handling code from this contract alone, without looking under the hood.

**Idempotency — two keys for two questions.** When a call fails, the caller doesn't know whether the work happened, so it retries. Bad ending: you double-send someone's money.

- The **`Idempotency-Key` header** is a guard on the network. The caller attaches it to the HTTP call; if the response gets lost and the same header key is retried, the server replays the stored response rather than running anything. This handles the ordinary case: a dropped connection, an automatic client retry.
- The **`intent_key`** is a guard on the money. The caller mints it once, up front, to identify the business intent itself ("this particular transfer"), and it travels with the request into the ledger. Before firing, the executor doesn't check *whether* the intent exists — it tries to *advance its state*: move this intent from `prepared` to `triggered`, and only proceeds if that move succeeds. If two executors race the same intent, the database lets exactly one win the transition.

The header key only catches a retry that looks identical. If the caller's process restarts and sends the same transfer as a fresh call, the header sees something new — but the `intent_key` still catches it, because the ledger remembers the intent no matter how the request arrives.

**Member binding.** Every run is tied to one specific member, and the caller supplies that member context. Before the executor even opens a session, the orchestrator checks that the accounts in the request actually belong to that member, and rejects the call if they don't.

**Caller authentication.** The agent platform logs in to our API with its own credentials, scoped to one institution, and every call it makes is stamped with that caller's identity in the trace, kept separate from the member it's acting for. A caller can only invoke tasks for its own institution, so reaching across institutions or members is not possible. Verifying *who the member is* stays the caller's job, because that happens in the phone or chat conversation our system never sees. What we require is the member we're acting for, and — for anything irreversible — that they consented.

**Caching.** We don't cache business outcomes like remaining balance; we cache items with clean expiry, like logged-in sessions and pinned task files. A cached balance can be wrong and look identical to a right one.

---

## 2. Execution model

Section 1 was what the agent/caller sees. This is what actually runs behind it: how a single run moves through its states, and the ledger that makes double-execution impossible.

### Run-state machine

```
                                        ┌───────────┐
                          ┌── read ───▶ │ Recovering │
                          │             └─────┬─────┘
                          │       hiccup      │ back
RECEIVED ─checks─▶ ADMITTED ─login─▶ WORKING ─┤
                                        │ "no"─▶ DECLINED (clean business "no")
                                        │
                                 trigger│
                                        ▼
                                    PREPARED ─▶ AWAIT TRIGGER ─approval─▶ TRIGGERING ─confirmed─▶ CONFIRMED
                                   (filled in,   (waiting for            (submit fired)          (money moved)
                                    stopped)      go-ahead)                   │
                                                                    no conf 20s│
   IF SOMETHING GOES WRONG                                                     ▼
   FROZEN ─▶ ESCALATED ─▶ STOPPED SAFE                                     UNKNOWN
   (no further  (human    (nothing                              (did it post? human/reconcile)
    actions)     takes     half-done)
                 over)

The one red arrow:  PREPARED ─▶ TRIGGERING fires only on an outside approval.
                    There is no other path into TRIGGERING.
```

These are **guards, not labels.** The reason for a real state machine instead of a simple status field: with a status field, code could just *set* a run to "done"; with guarded states, the dangerous transitions are impossible to reach by accident, and you can prove it in a test.

### The intent ledger

Every money-moving intent gets one row in one table. `intent_key` is the primary key, refusing to let two rows with the same intent exist.

```sql
CREATE TABLE intent_ledger (
  intent_key   uuid PRIMARY KEY,          -- the dedup guarantee lives here
  task         text NOT NULL,
  task_version text NOT NULL,
  tenant       text NOT NULL,
  member_ref   text NOT NULL,             -- hashed reference, never raw PII
  state        text NOT NULL CHECK (state IN
                 ('created','prepared','triggered','confirmed','aborted','unknown')),
  receipt      jsonb,                      -- readback: confirmation number, screen hash
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz
);
```

### Task definition format

A task is an explicit state machine over screens: each step is a `require` (check the screen) then an `act`, and `expect` lists the named outcomes — where business results like "not found" are branches, not errors.

```yaml
task: transfer_funds
version: 1.4.0
label: irreversible

steps:
  - id: open_member          # check-then-act: verify screen, then act
    require: {screen: member_search}
    act: [{type_into: member_id, value: "{member_ref}"}, {press: Enter}]
    expect:
      ok:        {screen: member_summary, then: fill_form}
      not_found: {then: decline MEMBER_NOT_FOUND}   # a business "no" is a branch, not an error

  - id: fill_form
    require: {screen: transfer_form}
    act: [set from_acct, set to_acct, set amount]    # each typed value is read back to confirm
    expect:
      ok:           {then: gate}
      insufficient: {then: decline INSUFFICIENT_FUNDS}

  - id: gate                 # prepared-then-triggered lives here
    require: {screen: transfer_confirm, match: {from, to, amount}}   # confirm screen matches inputs
    hold_for: trigger        # ledger -> PREPARED; waits for outside approval

  - id: submit
    act: {click: submit, single_fire: true}          # fires once, never retried blind
    expect:
      ok:      {screen: receipt, then: capture}
      timeout: {after_ms: 20000, then: unknown_protocol}   # clicked but no confirmation

  - id: capture
    act: {read: confirmation_number into outputs}
    finish: completed        # ledger -> CONFIRMED

recoveries:                  # known interruptions, handled and resumed
  session_expired: {guard: no_irreversible_act_yet, do: [login, relocate, resume]}

unknown_protocol: {ledger: lock, escalate: "query the account BEFORE any retry"}
```

A read task like `get_balance` is the same shape with far fewer steps, no gate, and no ledger, because it moves nothing.

Beyond what any single task file says, the engine enforces three things no task can override: a failed check halts with zero further actions, an action never fires twice blindly, and retry behavior is set by the label, not configured per step.

**If the executor crashes mid-run.** We save each state change *before* taking the action that follows, and running executors send heartbeats. A run whose executor died is detectable, and how it recovers depends on where it was. If it hadn't done anything irreversible yet, it closes safely and the session is dropped. If it was parked at `prepared` (filled in, waiting for the trigger), it stays there, because the go-ahead outlives the crashed process. If it crashed *between clicking submit and seeing the confirmation*, it goes to `unknown` — the same "did the money move?" state — and a human resolves it by checking before doing anything. Nothing orphaned is ever silently re-run; re-running is a retry, and retry rules come from the label.

---

## 3. Driving the UI

This is how the executor actually operates a screen. The whole approach is one loop: **look at the page, figure out which screen it is, find the field, act on it, then verify the act landed.**

**Looking at the page.** Instead of screenshotting the page and guessing at pixels, we ask the browser itself what it rendered. One call (Chrome DevTools Protocol's `DOMSnapshot`) returns a flat list of every visible element, its text, and where it sits on screen, across all frames. Pixels get used in only two places: masked screenshots kept as evidence for humans, and a last-resort OCR path for surfaces that expose no structure at all. Because we rely on where things render, the runner is pinned to a fixed window size, resolution, and fonts, so the layout is the same every run.

**Which screen is this?** Each screen we care about has a **signature** — a few text labels that must all be present (optionally a title or URL). A single function, `identify()`, checks the current page against those signatures and returns the screen's name, or `UNKNOWN`. This function checks before it acts, and it's how we match an outcome to an expected branch, and how we continue after recovery.

**Finding a field.** Each element (the amount box, the submit button) carries an ordered list of ways to find it, tried top to bottom. A tier has to match *exactly one* element; if it matches two (a page rendered the submit button twice), that tier fails and we fall to the next, or halt. The primary way is by its **label**: the input just right of or below the text "Amount." Labels are what human operators navigate by and the most stable thing on a legacy screen. When there's no usable label, we fall back, in order: to structural position from a nearby landmark, then to a text pattern (for reading a value like `$1,234.56`), and lastly, on a pixel-only surface, image matching.

We log *which* tier matched on every action. If tier 1 quietly starts failing over to tier 2 across runs, that's the earliest warning that a screen changed — visible before anything actually breaks.

Right before acting, we re-locate the target from a fresh look at the page, so if a popup or re-render slipped in since the check, the click either can't find its target or fails verification, and we halt instead of acting on a changed page.

On old frameset apps, which split a page into embedded sub-documents, the frame is part of an element's location, so we act in the right one.

**Waiting.** Never a blind `sleep`. We wait on a *condition* being true (the screen is right, the spinner is gone, a value stopped changing) with a time budget: 15 s for a page load, 5 s within a screen, 20 s after a submit. If the budget runs out: one retry, then a hard stop for reversible waits — that's how we tell "slow" from "dead." The retry budget follows **reversibility**: reads and navigation retry once; the one irreversible action (submit) gets zero blind retries. When submit's wait budget blows, we don't retry — we reconcile.

**Acting.** Every action is verified: type a value then read it back, click then wait for the expected next screen. On an API-less legacy app, that readback is the only receipt you get. And actions fire exactly once — a re-clicked submit is a double transfer.

**One interface, many surfaces.** All of the above hides behind one small driver interface. The *task* never changes across surfaces; only the driver beneath it does:

```
interface Driver:
  snapshot()               -> Snapshot        # normalized element table
  resolve(chain, snapshot) -> ElementRef|None # records which tier fired
  read(ref)                -> str
  set(ref, value)          -> None            # native input dispatch
  click(ref)               -> None
  press(key)               -> None
  screencast(enable: bool) -> None            # takeover only (§5)
```

| Surface | Snapshot from | Anchor means | Postcondition means |
|---|---|---|---|
| DOM (web, legacy web) | CDP `DOMSnapshot` across frames | label text + geometry, landmark path | readback of field text; `identify()` of next screen |
| UIA / Java Access Bridge (desktop) | OS accessibility tree | automation-id path, then name + control type | control value re-read; window/state assert |
| Anchored OCR (pixel floor) | screenshot in pinned environment | anchor image + offset + OCR region | type-then-verify mandatory: re-read what landed |

**Where the model is, and isn't.** A model explores a new app *offline*, under human supervision, and proposes the screen signatures and locator chains, which a human reviews. At runtime it's absent: it doesn't look at screens, doesn't pick targets, doesn't decide whether to proceed, never fires money. Three reasons: replaying zero-judgment work should be deterministic; a vision call every step would be ~100× the cost and latency on UIs that barely change; and improvised behavior can't be reviewed or audited. The one narrow exception is the bounded fallback in §9, and it ships in shadow mode first.

---

## 4. Runtime errors and exceptional states

**Classification is declarative, not heuristic.** Every step declares the expected edge/outcome. After acting, the executor identifies the state and matches it against those edges in order. The outcomes/taxonomy therefore live in the task definition, where they're reviewed, versioned, and grown deliberately.

There are three kinds of outcome, and the kind decides what happens:

1. **A business outcome** is a real answer from the bank — "no member found," "insufficient funds." It returns a typed decline (the caller sees `declined MEMBER_NOT_FOUND`), and the ledger is never touched.
2. **A recoverable outcome** is a known interruption we can handle and continue past, like the session expiring mid-flow. We log back in, re-find our place, and resume; the caller never knows.
3. **A hard outcome** is anything we didn't expect, like an unexpected screen or dialog. We freeze with zero further actions, capture, and escalate.

Every hard outcome a human resolves becomes a candidate for a new outcome/edge in the next version of the task, reviewed like any other change. We don't guess with money.

The three hard cases share the same immediate behavior (freeze, capture, escalate) but route differently: an unexpected screen **suspends** that task for that tenant pending review, a dead permission **disables** it and pages operations, and a never-seen screen just **escalates**.

**Service outages.** Per (tenant, application), the executor keeps a health check: `up → suspect → down → probing → up`.

- **Detection.** While running, we may hit a page that won't load, a gateway timeout, or a maintenance page. We retry once (in case it's a fluke) and try to continue. Three flukes across 2 different runs (not just 1, because it could be the session) is a cause for suspicion.
- **While it's down.** We stop opening new sessions against that application, and state "unavailable, try again later." A background request gets queued with a deadline, and if it expires, we drop it — yesterday's balance doesn't matter today.
- **Coming back.** A read-only probe runs every couple of minutes, backing off up to fifteen. One clean probe doesn't mean green light: we bring read-only back first, at half the normal rate. Then reversible tasks, then money-moving ones. Any locked `unknown` states from before get resolved (check-first) before any queued money task runs.

**Human-only mode.** In some cases staff can use the app fine and only our automation is blocked. The institution can flip the affected tasks to assist mode: we still validate the inputs and check the ledger, and produce fully prepared work items, but a human does the actual clicking in their own session, with us reading final states to close the run.

---

## 5. Escalation: pause, cede, resume

When a run gets stuck, a human takes over the live session, finishes or fixes it, and hands control back. We designed the system to be a checkpointed state machine, so human intervention is just another state.

**Stuck** is our hard edge: a step that blew its time budget, or a run that blew its total budget. The executor stops at the failed check, with zero actions taken past it, and leaves the browser session alive and parked for the human to look into.

**The work item.** The human doesn't get an alert; they get a work item with everything they need to act:

```json
{
  "ticket": "wi_5590", "lane": "member_waiting",
  "run": "r_01HXQ...", "task": "transfer_funds@1.4.0", "step": "review",
  "failed_check": {"expected": "screen transfer_confirm",
                   "observed": "dialog: 'Account flagged for review'"},
  "inputs_masked": {"from_acct": "****1204", "to_acct": "****9917", "amount": "$250.00"},
  "actions_taken": [{"step": "fill_form", "irreversible": false}],
  "ledger_state": "created",
  "evidence": ["masked capture at freeze", "step trace"],
  "live_session": {"view_url": "...", "control_url": "...", "expires_in_s": 900}
}
```

The failed check is written in plain domain words ("expected the confirm screen, saw an 'account flagged' dialog"), everything already done is listed (including anything irreversible), and the only actions offered are the ones that are legal for this kind of failure.

This "account flagged" dialog is a live example of the loop in §4. In v1.4.0 it wasn't a declared outcome, so it hit as a hard case and escalated, exactly what the work item above shows. A human resolved it, and v1.5.0 adds it as a declared branch. From then on the same screen returns cleanly as `ACCOUNT_RESTRICTED` instead of escalating.

**Cede.** The runner's browser runs headed inside its container, so the human drives the *same* frozen session instead of starting over. However, viewing and controlling are separate permissions, and control is an exclusive lock with a timeout, so the executor and the human are never both acting at once. Every input the human sends is recorded as theirs.

**Resume, three ways**, because we can't just continue blindly. We either:
- **Resume at a step** — re-check against the live screen for the step's expected screen.
- **Complete by hand** — the human finishes it, so we run our final verification and checks and close the ledger.
- **Abort** — we stop.

Every one of these resumes is captured as a recording (what screen we found, what the human did, what screen we left on), which becomes the raw material for the next version of the task — so each escalation makes the next one less likely.

---

## 6. Safety and data handling

How do we guarantee the agent can only do permitted things? Not with a permissions list, but through design.

1. **The catalog.** If a task doesn't exist in the catalog, the agent can't ask for it.
2. **Inside a task,** the executor can only do the steps that are written and reviewed. There's no discovery or `click()` command the caller sees. Unlisted/unreviewed actions are impossible to express in our system.
3. **Money-moving steps** fill in everything, **stop at the confirmation screen**, and only fire on an outside go-ahead. A money-moving task fills the form all the way to the final confirmation screen and then stops. At that point we check the on-screen summary against the typed inputs, the ledger sits at `prepared`, and the submit only fires when an outside authority says go — a human, or a rule the institution set.

We start all tasks off under human review, until enough clean supervised runs at that institution accumulate, and only then can they auto-run. A good success rate never bypasses this system.

**Secrets.** Credentials live in a vault and are injected only during the login step. So we record "typed password" and not the password. For MFA, the code secret lives with us — unless the org disagrees, in which case it becomes a human step.

**Member data.** We verify against what's on the live screen, but we don't *store* raw values. Evidence is masked at the moment it's written (last four digits) so a screenshot or trace can be redacted without weakening the check that already ran against the live value.

**Sessions** are pooled per (institution, app, service account). On single-session legacy apps, where a second login kicks the first, the pool size is 1 — so the queue *is* the concurrency answer: a second run waits rather than corrupting the first. Sessions are invalidated on a version bump or an outage.

---

## 7. Evidence and observability

Every run writes a trace, one event per line, from a fixed set of event types. So we can see: run created, each policy check, the login, each screen identified, each check (with expected vs. what we saw), each action (with which locator tier fired), each readback, which branch was taken, any recovery, any escalation, any human takeover (with the human's identity), who or what approved a trigger, each ledger move, and the final result.

```json
{"run":"r_01HXQ","ev":"run_created","task":"transfer_funds","ver":"1.4.0","tenant":"cu_047","keys":{"http":"...","intent":"..."}}
{"run":"r_01HXQ","ev":"check","step":"fill_form","expect":{"screen":"transfer_form"},"observed":"transfer_form","pass":true}
{"run":"r_01HXQ","ev":"act","step":"fill_form","kind":"type_into","target":"transfer_form.amount","tier":1,"readback":"pass"}
{"run":"r_01HXQ","ev":"check","step":"review","expect":{"screen":"transfer_confirm"},"observed":"dialog:'Account flagged for review'","pass":false,"actions_after":0,"capture":"sha256:ab12..."}
{"run":"r_01HXQ","ev":"escalation_created","wi":"wi_5590","lane":"member_waiting"}
```

**Two readers, two needs.** For debugging, the trace is a step-by-step timeline: at any freeze you see exactly what screen was expected versus what showed up, which tier found each field, and a masked screenshot at the boundary.

For an auditor, the trace turns an investigation into a query: for any run you can answer who acted (our service identity, plus the human on any takeover), under which task and version, reviewed and approved by whom, for which member, when, with what result, and who or what fired the money step.

**What we watch.** Per task, per version, per institution: the mix of outcomes (completed, each decline reason, escalated, stopped), the escalation rate, the count of `unknown` states (any nonzero pages someone), which locator tiers are firing, and step timings. The two early-warning signals are **locator-tier drift** (tier 1 quietly failing over to tier 2) and a **shift in the decline mix** — both of which show up *before* a member ever hits a failure.

---

## 8. Generalization

~200 institutions run the same few vendor products. We can therefore reuse what we build.

**One base per (vendor, version), plus a small per-institution overlay.** The base task and screen model are written once for a vendor product. Each institution gets an overlay: a small data file of local differences (here the field says 'Member No.' not 'Member Number').

As long as the overlay stays small, this institution is basically running the same task, just with local label swaps. But if an institution's overlay keeps *growing* — the moment it needs logic (if/else) — it's not config anymore, and it has to become part of the program. That is the **fork threshold.**

**Everything is pinned.** Each institution runs a specific version of a task and screen model. Fixes go to a few partner institutions first, then roll out in waves. A stale task suspends itself rather than improvising.

**Onboarding a new institution is not a project.** Run through their apps with our discovery agent, match each to a known vendor and version, inherit that base, generate their overlay through supervised read-only runs, and have their own staff validate and sign.

---

## 9. Extensions

- **Reusability / catalog:** the design and tasks are already versioned, typed, and discoverable.
- **Code generation:** because a screen model is already a map of element-name to locator (a Playwright "page object"), and a task is already an ordered list of steps, we can mechanically emit a runnable Playwright test from a completed task. We get a test suite for free from the artifacts we already have.
- **Confidence & approval:** a task starts as a draft and only becomes approved for real use after it's run cleanly under supervision enough times; how many scales with the label — more for money-moving tasks than for read-only.
- **Assisted fallback:** on a hard failure, the model may propose *one* bounded recovery step from a fixed menu, policy-checked, read-only, shipped to a shadow env first. The halt stays the default; this is the graduated exception.
- **Cross-tenant reuse:** base + overlay is the mechanism; the fork threshold is the rule.
- **Multi-run stability:** run a read task repeatedly, report a flakiness signal per step.
