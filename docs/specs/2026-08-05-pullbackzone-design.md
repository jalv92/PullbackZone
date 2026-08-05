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
  `zone_break_atr15 × ATR15` (clean break), or after `zone_expiry` (age).
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

- **Stop loss:** beyond the pullback extreme by `stop_buffer` (data-calibrated;
  see Calibration — NOT a token 1–2 ticks, per Javier's explicit correction).
- **Target:** `target_r` × risk (risk = entry − stop distance).
- Brackets are hand-movable (NT8 side): the strategy adopts dragged SL/TP.
- Optional breakeven at `breakeven_at_r` (0 = off) with `be_offset_ticks`.
- Session backstop: flatten at `flatten_time` (default 15:58 ET), lockout after.

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
| | `zone_width_atr15` | TBD by calibration | **yes** |
| | `zone_expiry` | 2 sessions | no |
| | `zone_break_atr15` | 0.25 | no |
| Leg | `leg_min_atr15` | TBD by calibration | **yes** |
| | `leg_timeout_min` | 60 | no |
| | `max_attempts_per_leg` | 2 | no |
| Pullback | `impulse_min_atr30` | TBD by calibration | **yes** |
| | `pullback_min_atr30` | TBD by calibration | **yes** |
| Triggers | `use_engulfing` / `use_hammer` / `use_doji_star` | true / true / true | no |
| Entry | `entry_offset_ticks` | 2 | no |
| | `entry_ttl_bars` | 6 | no |
| Exits | `stop_buffer` | TBD by calibration | **yes** |
| | `target_r` | 1.5 | no |
| | `breakeven_at_r` / `be_offset_ticks` | 0 (off) / 4 | no |
| Size/guards | `contracts` | 1 | no |
| | `daily_loss_r` | 0 (off; in R, never dollars) | no |
| Session | window fixed 09:30–16:00 ET; `flatten_time` | 15:58 | no |

## Calibration (before defaults freeze — no P&L optimization)

`research/calibrate.py` runs once on the real tape (last 30 sessions AND the full
238-session sample, reported side by side) and picks defaults as **percentiles of
market behavior, never by profit**:

- `stop_buffer`: p80 of the adverse pierce beyond the pullback extreme among legs
  that DID continue (Javier's explicit requirement: the buffer must reflect real NQ
  volatility, not a token tick count).
- `impulse_min_atr30`, `pullback_min_atr30`: percentile floors such that the
  impulse is a genuine multi-bar move (the feasibility study showed 1.0 × ATR30s ≈
  one bar's range — too small; the default must clear single-bar noise).
- `zone_width_atr15`, `leg_min_atr15`: distribution of retest distances around 15m
  pivot levels.

Chosen values are written back into this spec and frozen. Any later change is a
new pre-registered run, not a tweak.

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
