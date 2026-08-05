# PullbackZone — Validation log (append-only)

Pre-registered protocol lives in the design spec
(`docs/specs/2026-08-05-pullbackzone-design.md`, "Validation"). Results go here
whatever they say. **Stub — Task 8 owns the full build-out of this file.**

## Pre-registered order

| Stage | Gate | Status |
|---|---|---|
| V0 | `.cs` compiles via `nt8c`; plugin passes `plugins.py --check`; both sides' selfchecks green | PropSim side green (10 selfcheck blocks, `--check` OK, signals == trades); `.cs` not written yet |
| V1 | Mirror fidelity: ≥5 Market Replay sessions, same trades, ≤1 tick | not started |
| V2 | One PropSim backtest on ALL, single config, frozen defaults, pessimistic fills | **not started — nothing here is believed until V1 passes** |

## Calibration (2026-08-05) — carried forward

Full table, method and audit trail are in the spec's "Calibration" section. The
three things a reader of a V2 result must know before believing it:

1. **The one-bar pullback was real, and was fixed structurally, not by a dial.**
   Before the ≥2-bar amendment, 66% of armed hunts armed on a single 30s bar's
   counter-move (77% at the provisional floor) — the same single-bar-range trap
   that gave the 2026-08-05 feasibility study a phantom edge. Raising
   `pullback_min_atr30` per the pre-registered percentile rule (1.00 → 1.15) only
   moved it to 66%. The spec §3 amendment (hunt arms no earlier than the close of
   the second bar after the leg extreme) makes lag 1 impossible. Residual shape:
   lag 2 = 85.0%, lag 3 = 6.9%, lag 4 = 2.9%, lag ≥5 = 5.2%, median 2 bars.
   **The pullback is now at minimum two bars but still typically exactly two.**
   If V2 shows an edge, this is the first place to attack it.

2. **The zone premise is weaker than the design assumed, and is NOT fixed.** A
   0.30 × ATR15 band touches 60% of pivot levels, but catching a confirmed swing
   rejection that often would need ≈1.5 × ATR15 — five times wider. Most zone
   touches are price drifting through the level, not rejecting at it. No amendment
   addresses this; it remains an open threat to the pattern's premise.

3. **Risk envelope, so nobody sizes off a backtest alone.** Full sample under the
   frozen dials: 1.47 fills/session, risk per trade p50 125 ticks ($626 at 1 NQ),
   p90 225 ticks ($1,124), max 746 ticks ($3,728). A $1,200 prop daily-loss limit
   absorbs about one p90 stop-out. 1 contract on NQ; MNQ below a $50k account.
   Risk manager has veto before real money.

Calibration used **no P&L metric of any kind** — `research/calibrate.py` computes
no profit, win rate, expectancy or R multiple, by design. The gate trip that
occurred during calibration (2.87× on `stop_buffer_atr30`) was diagnosed as sample
starvation from a mis-specified upstream metric, corrected on structural grounds,
and re-passed at 1.00× — full account in the spec's audit trail.
