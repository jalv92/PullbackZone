# PullbackZone — Validation log (append-only)

Pre-registered protocol lives in the design spec
(`docs/specs/2026-08-05-pullbackzone-design.md`, "Validation"). Results go here
whatever they say. **Stub — Task 8 owns the full build-out of this file.**

## Pre-registered order

| Stage | Gate | Status |
|---|---|---|
| V0 | `.cs` compiles via `nt8c`; plugin passes `plugins.py --check`; both sides' selfchecks green | PropSim side green (10 selfcheck blocks, `--check` OK, signals == trades 106/106); `.cs` not written yet |
| V1 | Mirror fidelity: ≥5 Market Replay sessions, same trades, ≤1 tick | not started |
| V2 | One PropSim backtest on ALL, single config, frozen defaults, pessimistic fills | **not started — nothing here is believed until V1 passes** |

## Calibration (2026-08-05) — carried forward

Full table, method and audit trail are in the spec's "Calibration" section. The
three things a reader of a V2 result must know before believing it:

1. **One-bar delivery: an accepted design choice AND a live suspicion. Both are
   true at once — do not collapse them.**

   *The decision (owner's call, Javier, 2026-08-05, two AskUserQuestion rounds):*
   sharp pullbacks are the DESIRED mode. "1 mecha vale, tope 2 barras" — a single
   wick delivering the whole retracement is a feature, and what must be excluded is
   the slow grind. Amendment 2 implements exactly that: the hunt arms only at
   `ext_i + 2`, so late-arriving depth never arms and one-bar delivery is allowed
   through by design.

   *The history, stated honestly:* Amendment 1 (`>= 2`) was written as a span rule
   but behaved as a **latency** rule — it delayed arming without changing who
   delivered the depth, and 67.6% of armed hunts were still one-bar delivered. Two
   documents (this file and the spec) briefly claimed the problem was "fixed
   structurally". That was false and has been corrected. Amendment 2 resolves the
   discrepancy **by decision, not by measurement**.

   *The live suspicion, unchanged:* the 2026-08-05 feasibility study's phantom edge
   came from exactly this — a threshold a single 30s bar's range can clear on its
   own. Under Amendment 2 the arming lag is 2 by construction (100%), but
   **75.0% of armed hunts had the floor already cleared by the single bar at ext+1**
   (82.1% at the provisional floor, full 238-session sample). The window
   *concentrates* one-bar delivery rather than reducing it.

   **Pre-registered for V2:** if a backtest shows an edge, the first hypothesis to
   attack is that it lives in the one-bar-delivered subset. Split the results by
   `deliver1` (already measured by `research/calibrate.py`) before believing
   anything. An edge that exists only there is the feasibility study's artifact
   wearing a new name; an edge that survives in both subsets is real evidence.

2. **The zone premise is weaker than the design assumed, and is NOT fixed.** A
   0.30 × ATR15 band touches 60% of pivot levels, but catching a confirmed swing
   rejection that often would need ≈1.5 × ATR15 — five times wider. Most zone
   touches are price drifting through the level, not rejecting at it. No amendment
   addresses this; it remains an open threat to the pattern's premise.

3. **Risk envelope, so nobody sizes off a backtest alone.** Full sample under
   Amendment 2 and the frozen dials: 1.40 fills/session, risk per trade p50 127
   ticks ($637 at 1 NQ), p90 227 ticks ($1,137), max 609 ticks ($3,044). A $1,200
   prop daily-loss limit absorbs about one p90 stop-out (~95% of it). 1 contract on
   NQ; MNQ below a $50k account. Risk manager has veto before real money.

Calibration used **no P&L metric of any kind** — `research/calibrate.py` computes
no profit, win rate, expectancy or R multiple, by design. The gate trip that
occurred during calibration (2.87× on `stop_buffer_atr30`) was diagnosed as sample
starvation from a mis-specified upstream metric, corrected on structural grounds,
and re-passed at 1.00× — full account in the spec's audit trail. `stop_buffer_atr30`
has since been re-frozen once per amendment (1.30 → 1.25 → **1.30**), each pass
pre-registered and each re-passing the gate.
