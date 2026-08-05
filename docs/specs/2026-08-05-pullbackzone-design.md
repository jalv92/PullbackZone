# PullbackZone — Design Spec

Date: 2026-08-05 · Status: approved by Javier (brainstorm session) · Repo: jalv92/PullbackZone (public, MIT)

## What this is

An automated NQ strategy built twice from one closed specification — a NinjaTrader 8
strategy (`ninjascript/PullbackZoneStrategy.cs`) and a PropSim plugin
(`propsim/pullback_zone.py`) — that trades the classic **pullback-continuation**
pattern on 30-second bars, long and short, RTH only (09:30–16:00 ET):

1. Price departs from a well-defined **15-minute support/resistance zone**.
2. The 30-second leg away from that zone pulls back.
3. A classic **reversal candle** (engulfing, hammer/shooting star, doji star) fires
   inside the pullback, in the direction of the leg.
4. Entry is a **confirmation stop order** beyond the trigger candle's extreme.
5. Stop loss sits beyond the pullback extreme plus a **data-calibrated buffer**;
   target is an R-multiple with hand-movable brackets.

The two implementations are **mirrors**: every parameter exists on both sides with
identical defaults, and a fidelity gate requires both to produce the same trades on
the same sessions before any backtest number is believed. This is the
LatigoBreak ↔ PropSim playbook, applied from birth instead of retrofitted.

## Prior art and honest context

- The 2026-08-05 feasibility study (238 RTH sessions of real NQ ticks, PropSim ALL
  tape) killed the *naive* version of this trade three independent ways: a passive
  limit at a broken 30s pivot level has **zero** discriminating information (placebo
  parity, excursion parity vs random entries, and the apparent 60.8% win rate was an
  intrabar lookahead artifact — corrected: 48.5% vs 50% breakeven, −$16.67/trade
  after costs). Study artifacts: workspace `tmp/pbstudy/`, memory `break-retest-study`.
- This design is structurally different on three axes — reversal-candle trigger with
  confirmation-stop entry (not a passive limit at a level), 15-minute zone provenance
  (not same-session 30s pivots), and selective legs (max 2 attempts) — so testing it
  is legitimate, not re-litigation. The burden of proof stays on the strategy;
  `docs/validation.md` will record whatever the data says, either way.
- The archived `jalv92/Pullback` (moving-average touch) is unrelated except as a
  methodology lesson: thresholds scale in ATR fractions, never fixed ticks; risk
  management does not manufacture expectancy.

## Signal state machine

All evaluation happens on **closed bars** (15m for zones, 30s for everything else).
The only tick-resolution elements are the resting orders themselves.

### 1. ZONES (15m)

- Swing pivots on closed 15m bars: bar j is a pivot high if `high[j]` is the strict
  maximum of `high[j−K..j+K]` (`zone_pivot_k` bars per side); mirror for pivot lows.
  A pivot is usable only after its confirming bar closes (no lookahead).
- A pivot price becomes a **zone** once respected ≥ `zone_min_touches` times.
  Touch = a later 15m bar's high/low enters the band and the bar closes back on the
  original side.
- A zone is a **band**, not a line: `pivot_price ± zone_width_atr15 × ATR15`.
- A zone dies when a 15m bar closes beyond the far edge by more than
  `zone_break_atr15 × ATR15` (clean break), or after `zone_expiry_sessions` (age).
- Overlapping zones within one band-width merge (keep the older, more-touched one).

### 2. LEG

- When price touches a zone and then departs from the zone edge by
  ≥ `leg_min_atr15 × ATR15` (measured on 30s closes), a **leg** arms:
  direction = away from the origin zone.
- A leg dies when: price closes back inside the origin zone (invalidation), price
  reaches the next 15m zone in the leg direction (destination), `leg_timeout_min`
  elapses, or `max_attempts_per_leg` entries have been consumed.
- Leg death gates NEW entries only. An open position outlives its leg and is
  managed exclusively by its brackets (and the session flatten backstop).

### 3. PULLBACK

- Within an active leg, after the leg has extended ≥ `impulse_min_atr30 × ATR30s`
  from its arming point, a counter-move of ≥ `pullback_min_atr30 × ATR30s` from the
  leg extreme arms the trigger hunt.
- **Amendment 2 — the FAST-PULLBACK WINDOW (approved by Javier 2026-08-05,
  supersedes Amendment 1; pre-registered before any P&L was observed):** the
  trigger hunt arms ONLY at the close of the second bar after the bar that set
  the leg extreme (`i == ext_i + 2`), using the pullback extreme known through
  that bar. Consequences, both intended: (a) a one-bar wick MAY deliver the whole
  depth — the owner explicitly wants sharp pullbacks ("1 mecha vale"); (b) depth
  that arrives LATER than the second bar never arms for that extreme — slow-grind
  retracements are excluded ("tope 2 barras"). A new leg extreme resets the
  window. `stop_buffer_atr30` is re-frozen under this rule (one pre-registered
  pass per amendment).
  **Honesty note (from the round-2 re-review):** Amendment 1 as first implemented
  was a latency rule, not a span rule — 67.6% of armed hunts still had their
  depth delivered by the single bar after the extreme, and two documents
  overclaimed "fixed structurally". Amendment 2 resolves the discrepancy by
  DECISION, not by measurement: one-bar delivery is accepted as a design choice.
  The feasibility study's single-bar-noise concern therefore REMAINS a live,
  pre-registered suspicion for V2 in `docs/validation.md` — the burden of proof
  is unchanged.

### 4. TRIGGER (closed 30s candle, leg direction)

Enabled individually (`use_engulfing`, `use_hammer`, `use_doji_star`). Proportions
are internal constants, not parameters (dial bloat burned a search ledger before):

- **Engulfing** (long form): prior bar bearish, current bar bullish, current body
  engulfs prior body (`open ≤ prev close`, `close ≥ prev open`). Short form mirrored.
- **Hammer** (long) / **shooting star** (short): lower (upper) shadow ≥ 2× body,
  opposite shadow ≤ 0.3× range, body in the top (bottom) third of the range.
- **Doji star**: body ≤ 0.15× range, printed at the current pullback extreme.
  Direction comes from the leg; the confirmation stop supplies the directional
  proof a doji lacks on its own.

### 5. ENTRY

- Stop-market order `entry_offset_ticks` beyond the trigger candle's extreme
  (high for longs, low for shorts). One working entry at a time.
- Unfilled after `entry_ttl_bars` 30s bars → cancel, keep hunting (same attempt not
  consumed until filled; an attempt is consumed by a **fill**, not by a trigger).

### 6. EXITS

- **Stop loss:** beyond the pullback extreme by `stop_buffer_atr30` (data-calibrated;
  see Calibration — NOT a token 1–2 ticks, per Javier's explicit correction).
- **Target:** `target_r` × risk (risk = entry − stop distance).
- Brackets are hand-movable (NT8 side): the strategy adopts dragged SL/TP.
- Optional breakeven at `breakeven_at_r` (0 = off) with `be_offset_ticks`.
- Session backstop: flatten at `flatten_hhmm` (default 1558 = 15:58 ET), lockout after.

### 7. RE-ENTRY

- `max_attempts_per_leg` = 2: if the first fill stops out and the leg is still
  alive, one more trigger may be taken in the same leg. No third attempt.

## NT8 implementation notes

- Primary series 30s; `AddDataSeries(BarsPeriodType.Minute, 15)` for zones.
  **No `OnMarketData`** — keeps Strategy Analyzer and Playback compatibility
  without Tick Replay (LatigoBreak lesson).
- Managed approach. In-flight flags are set **before** every `Enter*`/`Exit*`
  (nt8-order-event-race invariant).
- Brackets = the validated LatigoBreak v3 pattern: `ExitLongStopMarket`/
  `ExitLongLimit` (+ short mirrors), `isLiveUntilCancelled: true`, submitted from
  `OnExecutionUpdate` at the real fill price; **Order references**
  (`_stopOrder`/`_targetOrder`, nulled before each own re-submit) distinguish the
  strategy's cancel-replace echoes from a real hand cancel; hand-cancel warning is
  deferred 1 s (`CheckBracketCancels`, only `InPosition`). Hand-moved brackets are
  adopted into `_stopPx`/`_targetPx`; a hand-cancelled target stays cancelled.
- Chart drawings: zone boxes (15m), trigger-candle marker, pullback-extreme dot
  (the stop anchor) — so Playback shows exactly what the strategy sees.
- JSONL evidence corpus per episode (zone, leg, pullback, trigger, entry, exit,
  MFE/MAE) — same discipline as LatigoBreak v4's corpus.
- 15m session template note: the added 15m series must produce bars aligned to the
  09:30 grid (standard ETH template does). `compare_mirror.py` catches drift.

## PropSim implementation notes

- `propsim/pullback_zone.py`, installed by copying to `~/.prop-sim/strategies/`;
  must pass `plugins.py --check` (AST allowlist, tick-index contract).
- 15m and 30s bars built from the same ALL-tape ticks (`build_bars`, 900/30 s).
- Entry stop and stop loss resolve through the engine's pessimistic fills (a stop
  fills at the worse of level and breaching print — existing selfcheck invariant).
- `contracts`, `target_r`, and every other dial present; RTH filter is the
  engine default (09:30–16:00), matching the strategy's window exactly.
- Episode dump mirrors the NT8 JSONL schema.

## Mirror contract

- **Closed parameter list.** Every NT8 property exists in the plugin in snake_case
  with the same default. Neither side may grow a dial the other lacks.
- `research/compare_mirror.py` joins both corpora on (date, direction, zone) and
  requires: same trade set, entry/stop/target within 1 tick.
- **Fidelity gate: no backtest number is believed until the mirror agrees on ≥5
  Market Replay sessions.** Known divergence sources to check first when it
  disagrees: ATR seeding, first-session warmup, 15m bar alignment, tie-breaking
  when two triggers fire on the same bar.

## Parameters (closed list, both sides)

| Group | Parameter | Default | Calibrated? |
|---|---|---|---|
| Zones | `zone_pivot_k` | 3 | no |
| | `zone_min_touches` | 2 | no |
| | `zone_width_atr15` | **0.30** | **yes** — p60, frozen 2026-08-05 |
| | `zone_expiry_sessions` | 2 sessions | no |
| | `zone_break_atr15` | 0.25 | no |
| Leg | `leg_min_atr15` | **0.40** | **yes** — p40, frozen 2026-08-05 |
| | `leg_timeout_min` | 60 | no |
| | `max_attempts_per_leg` | 2 | no |
| Pullback | `impulse_min_atr30` | **2.70** | **yes** — p50, frozen 2026-08-05 |
| | `pullback_min_atr30` | **1.15** | **yes** — p30, frozen 2026-08-05 |
| Triggers | `use_engulfing` / `use_hammer` / `use_doji_star` | true / true / true | no |
| Entry | `entry_offset_ticks` | 2 | no |
| | `entry_ttl_bars` | 6 | no |
| Exits | `stop_buffer_atr30` | **1.30** | **yes** — p80, re-frozen 2026-08-05 under Amendment 2 |
| | `target_r` | 1.5 | no |
| | `breakeven_at_r` / `be_offset_ticks` | 0 (off) / 4 | no |
| Size/guards | `contracts` | 1 | no |
| | `daily_loss_r` | 0 (off; in R, never dollars) | no |
| Session | window fixed 09:30–16:00 ET; `flatten_hhmm` | 1558 | no |

## Calibration — DONE and FROZEN 2026-08-05 (no P&L optimization)

`research/calibrate.py` ran once on the PropSim ALL tape (238 RTH sessions of real
NQ ticks, 2025-08-03 → 2026-08-04), reporting the last 30 sessions and the full
sample side by side. Every default is a **percentile of market behavior**; no
profit, win rate or R multiple is computed anywhere in that file. Values are the
full-sample figure snapped to 0.05, warmup bars of each session excluded.

| Dial | Rule | Recent 30 | Full 238 (raw, n) | Ratio | **Frozen** |
|---|---|---|---|---|---|
| `zone_width_atr15` | p60 nearest later bar approach to a live pivot | 0.30 | 0.301, n=547 | 1.00 | **0.30** |
| `leg_min_atr15` | p40 max departure from the zone edge ≤30 min after a touch | 0.30 | 0.390, n=2836 | 1.33 | **0.40** |
| `impulse_min_atr30` | p50 leg extension at pullbacks that made a NEW extreme | 3.20 | 2.707, n=1633 | 1.19 | **2.70** |
| `pullback_min_atr30` | p30 retracement depth of those same pullbacks | 1.15 | 1.139, n=1633 | 1.00 | **1.15** |
| `stop_buffer_atr30` | p80 adverse pierce past the trigger-time pullback extreme | 1.20 | 1.307, n=310 | 1.08 | **1.30** |

Pre-registered regime gate (>2× disagreement between the two windows blocks a
freeze): **PASS**, worst ratio 1.33. Dependency order was single-pass
`zone_width → leg_min → impulse/pullback → stop_buffer`; no dial was re-picked
after seeing a downstream result.

`stop_buffer_atr30` has been **re-frozen once per amendment**, each time alone and
pre-registered: 1.30 → 1.25 under Amendment 1, then **1.25 → 1.30 under Amendment 2**
(raw 1.307, CI [0.95, 1.66], n=310; recent 1.20, ratio 1.08 — gate PASS). Each
amendment changes which pullbacks reach a trigger at all, so it changes the pierce
population. The other four dials reproduce to the digit under both — the amendments
gate WHEN the hunt may arm, not the retracement distributions they are measured from.

What the frozen dials imply under Amendment 2, full sample: **4.73 episodes/session,
1.40 fills/session**, risk per trade p50 **127 ticks ($637 at 1 NQ)**, p90 227 ticks
($1,137), max 609 ticks ($3,044). A $1,200 prop daily-loss limit absorbs roughly *one*
p90 stop-out (≈95% of it) or two median ones — 1 contract only on NQ at that envelope,
MNQ below a $50k account, and the risk manager has veto.

### Audit trail (keep — this is the durable record)

- **The gate tripped once, and it was a measurement bug, not a regime.** The first
  chain used a swing-rejection form of M1, which put `zone_width` at 0.70, starved
  the recent window's pierce sample to n=22, and produced `stop_buffer` 0.40 vs 1.15
  — a **2.87× trip**. The bootstrap CIs overlapped, diagnosing sample starvation
  rather than a regime shift. M1 was corrected upstream on structural grounds (a
  *bar* is what `zones` tests, not a swing) and the chain re-derived once; the same
  dial then agreed at 1.00×. `calibrate.py` prints the alternative swing-chain column
  permanently so the fork stays auditable.
- **M1 could not be measured as originally specified.** "Distribution across later
  touches" is circular — whether a bar *is* a touch is what the width decides — and
  the answer just tracked the assumed neighbourhood (p60 ≈ 0.5 × cap, because
  approach distances are near-uniform: **15m pivot levels show no measurable
  clustering of later approaches at ATR15 resolution**). Replaced with one cap-free
  order statistic per level. The metric has three known slacks vs what `zones`
  scores (reveal-bar vs approach-bar ATR; close-outside-near-edge vs close-on-pivot-
  side; `min_touches`=2 vs first approach) — documented in the function, left
  uncorrected because closing them changes the eligible population (n 547 → 894)
  rather than correcting a reading of this one.
- **Dossier reconciliation.** Task 2's "median 149 ticks, max 230" reproduces on its
  slice — the **last 10 sessions of NQ 09-26 (2026-07-23 → 2026-08-05)** under
  provisional dials: min 59, median 149, p90 320, max 456. The max moved because the
  attempt-2 and flat-to-flat gates changed the fill set there (6 → 7 fills). It is
  not the ALL-sample figure and was never comparable to one.

### Findings that survive the freeze (carry into `docs/validation.md`)

1. **One-bar delivery is ACCEPTED BY DECISION, not fixed — and it is the majority
   case.** Amendment 1 was a latency rule, not a span rule: it delayed arming without
   changing who delivered the depth. Amendment 2 replaces it with an exact window and
   the owner's ruling that sharp pullbacks are the *desired* mode ("1 mecha vale, tope
   2 barras"). Measured under Amendment 2: the arming lag is 2 by construction (100%),
   and **75.0% of armed hunts had the floor already cleared by the single bar at
   ext+1** (82.1% at the provisional floor). The window concentrates one-bar delivery
   rather than reducing it. This is a design choice with a live risk attached — see
   `docs/validation.md`, where the feasibility study's single-bar-noise suspicion
   stays pre-registered for V2. The burden of proof is unchanged.
2. **Zone touches are mostly drift, not rejections.** A band of 0.30 × ATR15 touches
   60% of pivot levels, but catching a *confirmed swing rejection* 60% of the time
   would need ≈1.5 × ATR15 — five times wider. The zone premise is weaker than the
   design assumed. This one is **not** addressed by any amendment.
3. **Early-session ATR is elevated by real volatility, not mainly by the gap.** The
   gap is large where it lands (mean 30s *true* range 186.1 pts at bar 0 vs a mean
   *range* of 38.8 pts), but the raw range — which cannot contain a gap — is itself
   elevated all through the early session (38.8 → 23.9 → 21.0 → 11.4 pts by position
   bucket). Excluding warmup bars moves `leg_min` and `pullback` not at all,
   `impulse` by 0.05 and `zone_width` by 0.10 on the full sample; the exceptions are
   `stop_buffer` (frozen 1.25 vs warm-in 1.10) and the *recent-window* `impulse`
   (3.20 vs warm-in 2.65), so the earlier blanket claim of "no dial by more than
   0.10" was wrong.

Any later change to a frozen value is a new pre-registered run, not a tweak.

## Validation (pre-registered order)

1. **V0 — instruments work.** `.cs` compiles via `nt8c`; plugin passes
   `plugins.py --check`; both sides' selfchecks green.
2. **V1 — mirror fidelity.** ≥5 Market Replay sessions, same trades, ≤1 tick.
   Nothing downstream is meaningful until V1 passes.
3. **V2 — first honest look.** One PropSim backtest on ALL (single config, frozen
   calibrated defaults, pessimistic fills) + Javier's Playback sessions. Results —
   whatever they are — recorded in `docs/validation.md` with the 2026-08-05 study
   as prior. Search-ledger discipline applies to any further parameter exploration.
4. Deploy flow: Claude copies the `.cs` to NT8 Custom; Javier presses F5 (standing
   workspace rule).

## Repo layout

```
PullbackZone/
  README.md                     what it is, honest-use note (sim/Playback), status
  LICENSE                       MIT, Javier Lora
  ninjascript/PullbackZoneStrategy.cs
  propsim/pullback_zone.py
  research/calibrate.py         percentile studies → frozen defaults
  research/compare_mirror.py    NT8 ↔ PropSim fidelity gate
  docs/specs/                   this spec
  docs/validation.md            pre-registered protocol + results (append-only)
  .gitignore                    *.jsonl corpora, __pycache__, data/
```

## Out of scope for v1 (explicitly)

Overnight/ETH windows (18:00/20:00), instruments other than NQ/MNQ, trailing
exits, zone-based targets, order-flow gates (BigPrints-style), any parameter grid
search. Each returns only as its own pre-registered follow-up.

## Imported lessons (do not re-learn)

- Daily guards in **R**, never dollars (LatigoBreak governor inverted the edge at size).
- 30s ATR-sized risk has a violent tail — `daily_loss_r` exists for prop envelopes;
  the risk manager has veto before anything touches real money.
- Passive fills flatter backtests; PropSim's pessimistic fills are the referee.
- In-flight order flags before submit; Order references for bracket echo filtering.
- Trigger features must be computed pre-fill only (the study's tag_delta corner was
  64% post-fill volume — pure hindsight).
