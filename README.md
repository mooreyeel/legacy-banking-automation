# Driving the UI — automation for legacy banking

An AI system that operates legacy back-office bank applications **that have no API**, by driving the UI like a human would — moving money **reliably, cheaply, and auditably**, and stopping to hand a live session to a person the instant it can't proceed safely.

## What's here

| File | What it is |
|---|---|
| **`index.html`** | The presentation site — explains the whole system in a visual scroll and hands off to the live demo. **Start here.** |
| **`walkthrough.html`** | The interactive demo. An executor drives a mock legacy bank screen through three scenarios — happy path, phantom send, escalation. Press `Step` or space to advance. |
| **`DESIGN.md`** | The full design document (sections 0–9): task contract, execution model, driving the UI, error taxonomy, escalation, safety, evidence, generalization. The depth. |
| **`deck.html`** | Static slide version — backup diagrams. |

## View it

Just open `index.html` in a browser — everything is self-contained, no build step, no dependencies.

```
open index.html
```

## The idea in one loop

Every step the executor takes: **look → identify → locate → act → verify.**
It re-derives the world from a fresh look at the page, then verifies the act landed before moving on. On an API-less app, the readback is the only receipt you get.

The one safety move that matters: money-moving tasks fill everything and **stop at the confirmation screen**. The submit fires *only* on an outside go-ahead — the single guarded transition `PREPARED → TRIGGERING`. Everything else is reads and navigation.
