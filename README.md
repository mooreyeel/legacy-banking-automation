# Driving the UI — automation for legacy banking

An AI system that operates legacy back-office bank applications **that have no API**, by driving the UI like a human would — moving money **reliably, cheaply, and auditably**, and stopping to hand a live session to a person the instant it can't proceed safely.

**▶ Live:** https://legacy-banking-automation.vercel.app · [**run the walkthrough**](https://legacy-banking-automation.vercel.app/walkthrough.html) · [**read the design**](https://legacy-banking-automation.vercel.app/design.html)

## What's here

| File | What it is |
|---|---|
| **`index.html`** | The presentation site — explains the whole system in a visual scroll and hands off to the live demo. **Start here.** |
| **`walkthrough.html`** | The interactive demo. An executor drives a mock legacy bank screen through three scenarios — happy path, phantom send, escalation. Press `Step` or space to advance. |
| **`DESIGN.md`** | The full design document (sections 0–9): task contract, execution model, driving the UI, error taxonomy, escalation, safety, evidence, generalization. The depth. |
| **`executor/`** | A **runnable** ~400-line miniature — the design actually executing against a real (ugly) mock legacy UI via Playwright. `python run.py happy\|phantom\|escalate`. See [`executor/README.md`](executor/README.md). |
| **`deck.html`** | Static slide version — backup diagrams. |

## View it

Live at **https://legacy-banking-automation.vercel.app** — or open `index.html` locally; everything is self-contained, no build step, no dependencies.

```
open index.html
```

## The idea in one loop

Every step the executor takes: **look → identify → locate → act → verify.**
It re-derives the world from a fresh look at the page, then verifies the act landed before moving on. On an API-less app, the readback is the only receipt you get.

The one safety move that matters: money-moving tasks fill everything and **stop at the confirmation screen**. The submit fires *only* on an outside go-ahead — the single guarded transition `PREPARED → TRIGGERING`. Everything else is reads and navigation.
