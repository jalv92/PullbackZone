<div align="center">

<h1>PullbackZone</h1>

<p>
  <b>An automated pullback-continuation strategy for NQ, built twice from one closed spec.</b><br>
  A NinjaTrader 8 strategy and a PropSim plugin implement the identical rules — every parameter,
  every default, every threshold — and a fidelity gate must show they trade identically before
  either side's backtest number is believed.
</p>

<p>
  <a href="#status">Status</a> ·
  <a href="#how-it-trades">How it trades</a> ·
  <a href="#install">Install</a> ·
  <a href="#validation">Validation</a> ·
  <a href="#license">License</a>
</p>

<p>
  <img src="https://img.shields.io/badge/status-research-orange?style=flat-square" alt="">
  <img src="https://img.shields.io/badge/platform-NinjaTrader%208-1f6feb?style=flat-square" alt="">
  <img src="https://img.shields.io/badge/instrument-NQ-f7931a?style=flat-square" alt="">
  <img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="">
</p>

<img src="docs/assets/hero.png" width="100%" alt="PullbackZone — 15m zones, leg, and trigger detection drawn on an NQ 30-second chart">

</div>

---

## Status

**This is a sim / Playback laboratory, not a validated strategy.** Nothing here has
traded real or simulated money to a conclusion. The build is done and both
implementations compile and pass their own selfchecks (V0), but the mirror-fidelity
gate that has to pass before either side's numbers mean anything (V1) has not run
yet, and the first backtest (V2) is locked behind it.

| Stage | Gate | Status |
|---|---|---|
| V0 — instruments work | `.cs` compiles; plugin passes the sandbox check; both sides' selfchecks green | ✅ done |
| V1 — mirror fidelity | ≥5 Market Replay sessions, same trades, entry/stop/target ≤1 tick, zero unexplained mismatches | ⏳ pending |
| V2 — first honest look | One PropSim backtest on the full tape, frozen defaults, then the pre-registered `deliver1` split | 🔒 locked until V1 passes |

Full pre-registered protocol, playback checklist, and the append-only results log:
[`docs/validation.md`](docs/validation.md).

**The honest prior this strategy has to survive.** A 2026-08-05 feasibility study
(238 RTH sessions of real NQ ticks) killed the *naive* version of this trade three
independent ways: a passive limit at a broken 30-second pivot level carries zero
discriminating information, and its apparent 60.8% win rate was an intrabar
lookahead artifact — corrected, it was 48.5% against a 50% breakeven, a loss after
costs. PullbackZone is structurally different (a reversal-candle trigger with a
confirmation-stop entry, 15-minute zone provenance, selective legs capped at 2
attempts), so testing it is legitimate. But the burden of proof is on the
strategy, not on the reader, and one of its own frozen thresholds — 75% of armed
pullbacks deliver their whole depth in a single 30-second bar, by design — is the
exact shape of trap that produced the earlier study's phantom edge. That split is
pre-registered as the first thing V2 has to survive; see `docs/validation.md`.

## What it is

- **One strategy, two mirrored implementations.** `ninjascript/PullbackZoneStrategy.cs`
  (NinjaTrader 8) and `propsim/pullback_zone.py` (PropSim plugin) — same rules,
  same names, same defaults.
- **A closed 22-parameter list.** Every dial on one side exists on the other, in
  snake_case, with an identical default. Neither side may grow a parameter the
  other lacks.
- **Data-calibrated defaults, not curve-fit ones.** Five of the 22 parameters are
  set from percentiles of real NQ tape behavior (`research/calibrate.py`) — the
  script computes no profit, win rate, or R multiple anywhere. It measures how the
  market moves, not how the strategy would have performed.
- **Gate-arbitrated fidelity.** `research/compare_mirror.py` joins both sides'
  trade corpora and requires the same trade set within 1 tick before any backtest
  number is allowed to be believed.

## How it trades

RTH only (09:30–16:00 ET), long and short, on 30-second bars with a 15-minute
context series:

1. **Zone** — a 15-minute swing pivot that has been respected ≥2 times becomes a
   support/resistance band.
2. **Leg** — price touches a zone, then departs from it by a calibrated multiple
   of 15m ATR; that departure is the leg, direction away from the zone.
3. **Pullback** — after the leg extends far enough, a counter-move arms a
   trigger hunt. The hunt arms in an exact 2-bar window from the leg's extreme —
   a one-bar wick delivering the whole retracement is accepted by design, a
   slower retracement is excluded.
4. **Trigger** — a classic reversal candle (engulfing, hammer/shooting star, or
   doji star) closes inside the pullback, in the leg's direction.
5. **Entry** — a stop-market order beyond the trigger candle's extreme, live for
   a fixed number of bars, then cancelled if unfilled.
6. **Exits** — a stop beyond the pullback extreme by a calibrated buffer, and a
   fixed R-multiple target; both brackets are hand-movable in NT8 Playback, and
   the strategy adopts a dragged stop or target. A session flatten closes
   everything at 15:58 ET.
7. **Re-entry** — at most one more attempt per leg after the first stops out,
   while the leg is still alive.

Full rules, every threshold, and the calibration method are in
[`docs/specs/2026-08-05-pullbackzone-design.md`](docs/specs/2026-08-05-pullbackzone-design.md).

## Install

### NinjaTrader 8

Copy `ninjascript/PullbackZoneStrategy.cs` into
`Documents\NinjaTrader 8\bin\Custom\Strategies\`, then open the NinjaScript
Editor and press F5 to compile. Chart requirements:

- Primary series: **NQ, 30-Second** bars.
- Session template: **RTH** (09:30–16:00 ET) — the strategy folds its own 15-minute
  zone series on top; do not add a second data series by hand.

### PropSim

Copy `propsim/pullback_zone.py` into `~/.prop-sim/strategies/`.

**Requires PropSim at or after commit `de0abdf`** ("allow numpy imports in the
sandbox allowlist") — an older PropSim checkout rejects this plugin at
`plugins.py --check` because the file imports `numpy`.

### Setup requirements

| Requirement | Value | Why |
|---|---|---|
| Timezone | ET (America/New_York) | Every session boundary (09:30 open, 15:58 flatten) is stated in ET |
| Session template (NT8) | RTH | Must match PropSim's `rth_only=True` tape slice exactly, or the 15m grid drifts |
| Series history depth | Both series load the same range, matched to the PropSim dump slice | A longer 15m history gives NT8's zones/ATR a warm-up PropSim never had |
| `Contracts` | 1, for any V1 gate session | The gate compares trade-for-trade; sizing is a separate decision |
| `BreakevenAtR` | 0 (off) | PropSim's exit model omits the breakeven stop — a non-zero value breaks the mirror (accepted delta) |
| `DailyLossR` | 0 (off) | PropSim's episode generation is precomputed and can't see prior closures — a non-zero value breaks the mirror (accepted delta) |

## Validation

`docs/validation.md` is the append-only, pre-registered validation log: the full
V0/V1/V2 protocol, the 10-step Playback checklist for recording a Market Replay
session, the pre-registered suspicions carried into any backtest, and the kill
criteria. **A negative V2 result gets published here and in the status table
above, in the same terms a positive one would have been.** No number from this
repo is quoted anywhere without its instrument, period, and replay-vs-live
provenance attached.

## Repo map

```
PullbackZone/
  README.md                     this file
  LICENSE                       MIT, Javier Lora
  ninjascript/PullbackZoneStrategy.cs   NT8 strategy
  propsim/pullback_zone.py              PropSim plugin (pattern core + Strategy subclass)
  research/calibrate.py         percentile studies -> frozen defaults
  research/dump_episodes.py     PropSim episode corpus dump (mirror input)
  research/compare_mirror.py    NT8 <-> PropSim fidelity gate (V1)
  docs/specs/                   design spec, amendments, calibration audit trail
  docs/validation.md            pre-registered protocol + results (append-only)
  .gitignore                    *.jsonl corpora, __pycache__, data/
```

## License

MIT — see [`LICENSE`](LICENSE).
