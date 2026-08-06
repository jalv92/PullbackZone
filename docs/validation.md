# PullbackZone — Validation log

Pre-registered protocol lives in the design spec
(`docs/specs/2026-08-05-pullbackzone-design.md`, "Validation"). This file is the
append-only record of what was actually run and what it found — results go here
whatever they say, including a negative one. Nothing below V1's PASS is meant to
be believed; see the honest prior in the section after the table.

## Pre-registered order

| Stage | Gate | Status |
|---|---|---|
| V0 | `.cs` compiles via `nt8c`; plugin passes `plugins.py --check`; both sides' selfchecks green | **PASS** — see evidence below |
| V1 | Mirror fidelity: ≥5 Market Replay sessions, same trades, entry/stop/target ≤1 tick, zero `UNEXPLAINED` | **PENDING** — protocol below, not yet run |
| V2 | One PropSim backtest on `ALL`, single config, frozen defaults, pessimistic fills, then the pre-registered `deliver1` split | **LOCKED** — nothing here is believed until V1 passes |

### V0 evidence (2026-08-05, `build/v1`)

- `ninjascript/PullbackZoneStrategy.cs` — `nt8c` clean, 0 errors / 0 warnings (last code change `aba74e6`, last comment-only change `3f40ca2`).
- `propsim/pullback_zone.py` — 10 selfcheck blocks green: `candles`, `atr/pivots`, `zones`, `episodes`, `episodes (negative)`, `fast-pullback window`, `attempt gate`, `cross-leg gate`, `ttl wall clock`, `strategy`.
- `python3 ../PropSim/plugins.py --check propsim/pullback_zone.py` → clean pass, 22-param closed list printed, smoke test `signals == trades` (106/106) — the standing engine-consistency property. Passes **as of PropSim commit `de0abdf`** (2026-08-05, "plugins: allow numpy imports in the sandbox allowlist") — this plugin does not load on an older PropSim checkout.
- `research/compare_mirror.py --selftest` → 6/6 green (`a` perfect match → PASS, `b` 2-tick mismatch → FAIL with rows dumped, `c` delta-11 leg_died → expected-delta not unexplained, `d` out-of-RTH → sanity-gate rejection, `e` delta-11 hunt-reset bound at the 179s/181s boundary, `f` delta-13 target-undershoot → `UNEXPLAINED_EXIT` not gap-slippage). Commit `07d28ab`.

## V1 — mirror fidelity protocol

**Nothing downstream is meaningful until this passes.** Run ≥5 Market Replay RTH
sessions on the NT8 side; dump the same dates from the PropSim side; join with the
gate. All three must hold for the session set to count:

- `Contracts = 1`, `BreakevenAtR = 0`, `DailyLossR = 0` — PropSim cannot mirror a
  non-zero daily guard (accepted delta 1) and the exit model omits the breakeven
  stop (accepted delta 3); both must be off for the gate to mean anything.
- NT8 chart on the RTH session template so the 15m series lands on the same
  09:30 grid PropSim's `slice_range(rth_only=True)` produces; both series (30s
  primary, 15m secondary) must share the same history depth — a longer 15m
  history gives NT8's zones/ATR15 a warm-up PropSim never had.
- NT8 corpus: `%USERPROFILE%\Documents\PullbackZone\pz_corpus.jsonl` (grows through
  the Replay sessions, one JSONL line per state change).
- PropSim corpus: `research/dump_episodes.py --contract "NQ 09-26" --start D --end D --out FILE.jsonl` — **always pass an explicit contract.** `--contract ALL` spans contract rolls and is not the tape slice a single Replay session compares against; `compare_mirror.py` only warns on `ALL`, it does not refuse it.
- Gate: `research/compare_mirror.py --nt8 pz_corpus.jsonl --propsim FILE.jsonl`.
  **PASS** = `MATCHED` ≥ 95% of PropSim `filled` episodes, entry/stop/target all
  within 1 tick, **and** zero rows in any `UNEXPLAINED*` bucket. Known accepted
  divergences (deltas 5, 11–14 in the plan) get their own buckets and do not
  count against the 95%; only genuine mismatches do.
- First things to check if it disagrees: ATR seeding (both sides' Wilder
  recursion must start at bar 0, no NaN warmup, reaching across session breaks),
  first-session warm-up, 15m bar alignment, tie-breaking when two triggers fire
  on the same closed bar.

### Playback checklist (run once per session, before trusting the corpus)

1. Zone boxes appear on the 15m grid matching a hand-drawn S/R level — visual
   sanity on the detection layer before any order logic is trusted.
2. Trigger markers (up/down triangle) print on plausible reversal candles, with
   the pullback-extreme dot sitting at the stop anchor.
3. The entry stop order appears **only** after a trigger fires — never before,
   never speculatively.
4. An unfilled entry cancels at `entry_ttl_bars` (6 bars = 3 minutes); the order
   disappears and hunting continues without consuming the attempt.
5. On fill, both brackets (stop + target) appear immediately, at the prices
   frozen at trigger time — not recomputed from the fill price.
6. Dragging SL or TP by hand is adopted into the strategy's tracked price (a
   "moved by hand" message on the chart/log); a hand-cancelled target stays
   cancelled — it is not silently re-submitted.
7. Hand-cancelling a bracket while in position raises the deferred (~1s / next
   bar close) hand-cancel warning, and only while in position.
8. Session flatten fires at 15:58 ET: any working entry cancels, any open
   position closes, and no new entry is accepted until the next session.
9. `pz_corpus.jsonl` grows one row per state change: a `filled`/`expired` row
   per trigger, one `exit` row per closed position (`reason` = stop/target/
   flatten/manual, carrying `exit_px`) — prices in the shared fields stay
   unrounded, as PropSim computes them.
10. Rewinding Playback (stepping back and replaying forward again) does not
    double-write or resurrect stale rows — the epoch fence must drop in-flight
    notes from a discarded pass.

## V2 — first honest look (locked)

Unlocks only after V1 passes. Protocol, in order:

1. One PropSim backtest on the `ALL` tape, single config, the frozen calibrated
   defaults (no sweep, no re-tuning), pessimistic fills — the engine's existing
   worse-of-level-and-breaching-print convention.
2. Before any edge is believed: split the results by `deliver1` (already
   measured by `research/calibrate.py`) — **75.0% of armed hunts deliver their
   whole pullback depth in the single bar at `ext_i + 1`, full 238-session
   sample.** An apparent edge that lives only in the `deliver1` subset is the
   2026-08-05 feasibility study's phantom edge wearing a new name (see prior,
   below); an edge that survives in both `deliver1` and `deliver2+` is the first
   evidence worth taking seriously. This split is pre-registered — it happens
   before the result is looked at as a whole, not after.
3. Read the split against Amendment 2's honesty note (spec, "Pullback"
   section): one-bar delivery is accepted **by decision**, not fixed by
   measurement — Amendment 1 was a latency rule, not a span rule, and briefly
   claimed the opposite in two documents before that was corrected. The
   suspicion this correction protects is unchanged: a threshold a single 30s
   bar's own range can clear on its own is exactly the family of trap that
   produced the feasibility study's corrected 48.5%-vs-50% result.
4. Record whatever the data says, in this file, next to the pre-registered
   suspicions below — a negative result is not a bug in the process, it is the
   process working.

## Pre-registered suspicions (carried verbatim into V2)

These are written down **before** V2 runs so a favorable number cannot quietly
retarget which suspicion it needs to survive.

1. **The 2026-08-05 feasibility study prior.** 238 RTH sessions of real NQ
   ticks on the PropSim `ALL` tape killed the *naive* version of this trade
   three independent ways: a passive limit at a broken 30s pivot level has
   **zero** discriminating information — placebo parity, excursion parity vs
   random entries, and the apparent 60.8% win rate was an intrabar lookahead
   artifact, corrected to 48.5% vs a 50% breakeven, −$16.67/trade after costs.
   Study artifacts: workspace `tmp/pbstudy/`, memory `break-retest-study`. This
   design is structurally different on three axes (reversal-candle trigger with
   a confirmation-stop entry, not a passive limit at a level; 15-minute zone
   provenance, not same-session 30s pivots; selective legs, max 2 attempts) —
   testing it is legitimate, not re-litigation, and the burden of proof stays on
   the strategy.
2. **`deliver1` = 75.0%.** One-bar pullback delivery is the majority case under
   Amendment 2, by design. See the V2 protocol above — this is the split that
   decides whether any V2 edge is real or the feasibility study's artifact again.
3. **Delta 4 — schema gap, not yet closeable.** Intra-bar exit ordering (~6.5%
   of fills, 4/61 measured) can't be classified by `compare_mirror.py` today:
   the JSONL schema has no `exit_ts` field distinct from `trig_ts` on exit rows,
   so if this ever produces a visible symptom it surfaces as `UNEXPLAINED`
   rather than its own bucket. Closing it means adding a field to the `.cs`
   corpus writer — a future change, not something the current gate can do.
4. **Delta 9 — a guarded invariant, not a gap.** NT8's secondary-series
   processing pointer lags on shared timestamps (the primary series processes
   first). Task 5's absolute-index 15m fold structurally prevents this from
   happening. `compare_mirror.py` has no dedicated bucket for a delta-9
   violation on purpose: if the guard ever regresses, the resulting mismatch
   **should** fall through to `UNEXPLAINED` and fail the gate loudly, not be
   silently absorbed as an accepted delta.

## Kill criteria

**V2 gross expectancy ≤ 0 at the frozen defaults → the negative result is
published**, in this file and in the README's status table, in the same terms
as a positive one would have been. Any revival after that is not a parameter
tweak: it needs a structurally different setup, pre-registered as its own run,
per the spec's "Prior art and honest context" section. No metric from this repo
is quoted anywhere (README, memory, elsewhere) without its instrument, period,
and replay-vs-live provenance attached, and no "promising" language precedes a
number that has not cleared this protocol.

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
