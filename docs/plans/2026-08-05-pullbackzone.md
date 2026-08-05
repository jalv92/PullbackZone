# PullbackZone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the PullbackZone strategy twice from one closed spec — a PropSim plugin and an NT8 strategy — with data-calibrated defaults and a mirror-fidelity gate, per `docs/specs/2026-08-05-pullbackzone-design.md`.

**Architecture:** A pure-Python pattern core (zones from 15m pivots, legs, pullbacks, reversal-candle triggers) drives both a PropSim `Strategy` subclass (tick-index entries, pessimistic fills) and a NinjaScript port (closed-bar decisions, resting orders, hand-movable brackets). Calibration freezes the five data-derived defaults from tape percentiles BEFORE any backtest. A JSONL episode corpus on both sides feeds the mirror gate.

**Tech Stack:** Python 3 + numpy (PropSim house style, selfcheck-driven), NinjaScript C# (NT8, compiled via `nt8c`), PropSim modules `tape.py`/`engine.py`/`plugins.py` at `../PropSim/`.

## Global Constraints

- All produced text/code/comments/docs in English (workspace hard rule).
- All price thresholds in ATR fractions or ticks — never raw points (Pullback post-mortem).
- Parameter list is CLOSED: every dial exists on both sides, same snake_case names, same defaults. Neither side may grow a dial the other lacks.
- No `OnMarketData` in the .cs (keeps Analyzer/Playback compatibility without Tick Replay).
- Calibration picks percentiles of market behavior; it never optimizes P&L.
- Decisions on CLOSED bars only (30s triggers, 15m zones); only resting orders act intrabar.
- In-flight order flags set BEFORE every `Enter*`/`Exit*`/`CancelOrder` (nt8-order-event-race).
- Corpora (`*.jsonl`) stay gitignored.
- PropSim path used by research scripts: `PROPSIM = Path(__file__).resolve().parents[1].parent / "PropSim"` (both repos live under `projects/Trading/`).
- Commits: conventional prefixes (`feat:`, `test:`, `docs:`, `research:`), end with the Co-Authored-By Claude line.

## Known mirror deltas (accepted, documented — do not "fix" silently)

1. `daily_loss_r > 0` couples future entries to prior outcomes; PropSim's `entries()` is precomputed and cannot know closures. Default is 0 (off); the V1 mirror gate runs with it off. Same class as LatigoBreak delta 3.
2. Second attempt per leg: NT8 knows when attempt 1's position closed; PropSim generates candidates without knowing. Expected to be rare (attempt 1's stop sits at the pullback extreme; a second trigger usually forms after it would have resolved). The mirror gate MEASURES this instead of assuming.

---

### Task 1: Pattern core — ATR, pivots, zones, trigger candles (`propsim/pullback_zone.py`)

**Files:**
- Create: `propsim/pullback_zone.py` (pure functions + selfcheck section; the `Strategy` subclass arrives in Task 2)

**Interfaces:**
- Consumes: `numpy` only (file must stay plugin-sandbox-clean: imports limited to `math`/`numpy` — `plugins.py` AST allowlist).
- Produces (exact signatures, used by Tasks 2–4):
  - `TICK = 0.25`
  - `wilder_atr(h, l, c, n, day) -> np.ndarray` — Wilder ATR over closed bars, NaN warmup, **resets at each session boundary** (`day` = per-bar day index array; true range never reaches across the overnight gap).
  - `pivots(h, l, k) -> (hi_idx, lo_idx)` — int arrays of strict-unique swing bars; a pivot at bar `j` is usable only from bar `j + k` on.
  - `zones(b15, day15, p) -> list[dict]` with keys `px, half_w, born_i, died_i, touches, pivot_high` — `born_i` = 15m bar index at whose CLOSE the zone becomes usable (touch #`zone_min_touches` confirmed), `died_i` = 15m bar index of clean break or expiry (`10**9` if alive at end).
  - `candle_engulfing(o, h, l, c, i, d) -> bool`, `candle_hammer(o, h, l, c, i, d) -> bool` (d=+1 hammer / d=-1 shooting star), `candle_doji(o, h, l, c, i) -> bool`.

- [ ] **Step 1: Create the file with constants and failing selfcheck for candles**

```python
#!/usr/bin/env python3
"""PullbackZone pattern core + PropSim plugin.

Spec: docs/specs/2026-08-05-pullbackzone-design.md. Parameter list is CLOSED
and mirrors ninjascript/PullbackZoneStrategy.cs one-to-one.
Sandbox rule: imports limited to math/numpy so plugins.py --check passes.
"""
import math
import numpy as np

TICK = 0.25

# Candle proportions are internal constants, not parameters (dial bloat
# burned a search ledger before -- see the spec).
_HAMMER_SHADOW_BODY = 2.0     # long shadow >= 2x body
_HAMMER_OPP_SHADOW_RNG = 0.3  # opposite shadow <= 0.3x range
_DOJI_BODY_RNG = 0.15         # doji body <= 0.15x range


def candle_engulfing(o, h, l, c, i, d):
    """Bullish (d=+1) or bearish (d=-1) engulfing at closed bar i."""
    if i < 1:
        return False
    b_prev, b_cur = c[i - 1] - o[i - 1], c[i] - o[i]
    if d > 0:
        return (b_prev < 0 and b_cur > 0 and o[i] <= c[i - 1]
                and c[i] >= o[i - 1] and abs(b_cur) >= abs(b_prev))
    return (b_prev > 0 and b_cur < 0 and o[i] >= c[i - 1]
            and c[i] <= o[i - 1] and abs(b_cur) >= abs(b_prev))


def candle_hammer(o, h, l, c, i, d):
    """Hammer (d=+1) / shooting star (d=-1) at closed bar i."""
    rng = h[i] - l[i]
    if rng <= 0:
        return False
    body = abs(c[i] - o[i])
    lower = min(o[i], c[i]) - l[i]
    upper = h[i] - max(o[i], c[i])
    if d > 0:
        return (lower >= _HAMMER_SHADOW_BODY * body
                and upper <= _HAMMER_OPP_SHADOW_RNG * rng
                and min(o[i], c[i]) >= h[i] - rng / 3.0)
    return (upper >= _HAMMER_SHADOW_BODY * body
            and lower <= _HAMMER_OPP_SHADOW_RNG * rng
            and max(o[i], c[i]) <= l[i] + rng / 3.0)


def candle_doji(o, h, l, c, i):
    rng = h[i] - l[i]
    return rng > 0 and abs(c[i] - o[i]) <= _DOJI_BODY_RNG * rng
```

Selfcheck (same file, bottom, `if __name__ == "__main__"` + `--selfcheck` arg like every PropSim module): fixture arrays with one true and one false case per predicate:

```python
def _selfcheck_candles():
    o = np.array([10.0, 11.0, 10.0, 10.9, 10.0])
    c = np.array([9.0, 10.0, 11.5, 11.0, 10.05])
    h = np.array([10.5, 11.2, 11.6, 11.0, 10.1])
    l = np.array([8.9, 9.9, 9.9, 8.0, 9.0])
    assert candle_engulfing(o, h, l, c, 2, +1)          # bull engulfs bar 1
    assert not candle_engulfing(o, h, l, c, 1, +1)      # prior bar not engulfed
    assert candle_hammer(o, h, l, c, 3, +1)             # long lower shadow
    assert not candle_hammer(o, h, l, c, 2, +1)
    assert candle_doji(o, h, l, c, 4)                   # body 0.05 vs range 1.1
    assert not candle_doji(o, h, l, c, 2)
    print("candles OK")
```

- [ ] **Step 2: Run `python3 propsim/pullback_zone.py --selfcheck`, verify the candle block passes and nothing else exists yet**

- [ ] **Step 3: Add `wilder_atr` + `pivots` with failing selfcheck first**

Test first (append to selfcheck): a 2-session fixture where the true range at the session boundary must NOT use the prior session's close; a pivot fixture with a tie that must be rejected:

```python
def _selfcheck_atr_pivots():
    n = 20
    h = np.full(n, 101.0); l = np.full(n, 100.0); c = np.full(n, 100.5)
    day = np.concatenate([np.zeros(10, int), np.ones(10, int)])
    c[9] = 100.5
    l[10] = 90.0   # would be a giant TR only if the gap leaked across sessions
    h[10] = 91.0; c[10] = 90.5
    atr = wilder_atr(h, l, c, 5, day)
    assert abs(atr[9] - 1.0) < 1e-9                     # steady 1-pt bars
    assert abs(atr[16] - 1.0) < 1e-9                    # reset: no gap contamination
    hh = np.array([1, 2, 5, 2, 1, 5, 5, 1, 2.0])
    ll = hh - 1
    hi, lo = pivots(hh, ll, 2)
    assert list(hi) == [2]                              # bar 5 ties with 6 -> rejected
    print("atr/pivots OK")
```

Implementation: `wilder_atr` = per-session split (`np.flatnonzero(np.diff(day)) + 1`), TR of bar i uses `c[i-1]` only within the session, simple-mean seed over the first `n` TRs, Wilder smoothing after, NaN until seeded. `pivots` = for each `j` in `k..len-k-1`: strict unique max/min of the `2k+1` window (reuse the exact window logic from the feasibility study's `study.py` in workspace `tmp/pbstudy/`, which passed its no-lookahead selfcheck).

- [ ] **Step 4: Run selfcheck — atr/pivots pass**

- [ ] **Step 5: Add `zones()` with failing selfcheck first**

```python
def zones(b15, day15, p):
    """15m S/R zones. Returns dicts usable point-in-time via born_i/died_i.

    born_i: the zone exists from the CLOSE of the 15m bar that lands touch
    #zone_min_touches (pivot itself already confirmed k bars earlier).
    died_i: first 15m bar whose close crosses the far edge by more than
    zone_break_atr15 * ATR15, or born_i + expiry; 10**9 while alive.
    A new pivot within one band-width of a live zone merges into it (the old
    zone keeps its identity and touch count).
    """
```

Selfcheck fixture: hand-built 15m arrays where a pivot high at bar 5 gets touches at bars 9 and 13 (highs enter `px ± half_w`, closes back below) → `born_i == 13`; a clean break at bar 20 (`close > px + half_w + 0.25*atr`) → `died_i == 20`. Assert both indices exactly; assert a third pivot at the same price does NOT create a second zone (merge).

- [ ] **Step 6: Run selfcheck — zones pass**

- [ ] **Step 7: Commit**

```bash
git add propsim/pullback_zone.py
git commit -m "feat: pattern core — session-reset ATR, strict pivots, 15m zones, trigger candles"
```

---

### Task 2: State machine + PropSim `Strategy` subclass (same file)

**Files:**
- Modify: `propsim/pullback_zone.py`

**Interfaces:**
- Consumes: Task 1 functions; PropSim `engine.Strategy`, `engine.Param` (imported ONLY under a `try/except ImportError` guard so the file stays importable standalone for research; the class body references them lazily — see Step 3).
- Produces:
  - `PARAMS_PROVISIONAL: dict[str, float]` — the closed parameter dict with provisional values for the five calibrated dials, single source of truth for defaults.
  - `episodes(tape, p) -> list[dict]` — full episode log, keys: `kind` (`"filled" | "expired" | "leg_died" | "no_attempt_left"`), `dir` (+1/−1), `zone_px`, `zone_touches`, `leg_arm_ts`, `trig_ts`, `trig_kind` (`"engulfing" | "hammer" | "doji"`), `attempt` (1|2), `entry_stop_px`, `entry_tick` (int tick index, −1 if never filled), `pull_ext_px`, `stop_px`, `target_px`, `atr30`, `atr15`. Times are .NET ticks (ints).
  - `class PullbackZone(Strategy)` — `name = "pullback_zone"`, `uses_ticks = True`, `full_session = False`, `entries()` returning `(et int64, dr int8, st float64, tg float64)` (4-tuple; breakeven variant returns the 6-tuple `(et, dr, st, tg, None, be)` only when `breakeven_at_r > 0`), `risk_ticks()` returning `_SANITY_STOP_TICKS = 1200`.

- [ ] **Step 1: Write the parameter dict (closed list — copy verbatim, these names ARE the NT8 property names in snake_case)**

```python
# Provisional values marked CALIBRATE are frozen by research/calibrate.py
# (Task 4) and then updated HERE and in the spec table. Never sweep them.
PARAMS_PROVISIONAL = dict(
    zone_pivot_k=3, zone_min_touches=2,
    zone_width_atr15=0.25,          # CALIBRATE
    zone_expiry_sessions=2, zone_break_atr15=0.25,
    leg_min_atr15=0.50,             # CALIBRATE
    leg_timeout_min=60, max_attempts_per_leg=2,
    impulse_min_atr30=2.0,          # CALIBRATE
    pullback_min_atr30=1.0,         # CALIBRATE
    use_engulfing=1, use_hammer=1, use_doji_star=1,
    entry_offset_ticks=2, entry_ttl_bars=6,
    stop_buffer_atr30=0.50,         # CALIBRATE
    target_r=1.5, breakeven_at_r=0.0, be_offset_ticks=4,
    contracts=1, daily_loss_r=0.0, flatten_hhmm=1558,
)
```

- [ ] **Step 2: Write the failing selfcheck for `episodes()` — synthetic full episode**

Build a synthetic tick tape (4-prints-per-bar helper, same trick as `tmp/pbstudy/study.py::_mk_ticks`) spanning one RTH session of 15m/30s bars that contains, in order: a 15m pivot high at 110 with two touches (zone born), price departing downward past `leg_min` (leg armed, dir=−1), an impulse ≥ `impulse_min_atr30`, a pullback ≥ `pullback_min_atr30`, a shooting-star 30s bar (trigger), then a print below `trigger_low − entry_offset_ticks*TICK` within `entry_ttl_bars` (fill), then continuation. Assert:

```python
eps = episodes(t, PARAMS_PROVISIONAL)
filled = [e for e in eps if e["kind"] == "filled"]
assert len(filled) == 1
e = filled[0]
assert e["dir"] == -1 and e["trig_kind"] == "hammer"     # shooting star = hammer d=-1
assert abs(e["zone_px"] - 110.0) < 1e-6 and e["attempt"] == 1
assert e["stop_px"] > e["entry_stop_px"] > e["target_px"]  # short geometry
# stop = pullback extreme + buffer:
assert abs(e["stop_px"] - (e["pull_ext_px"] + PARAMS_PROVISIONAL["stop_buffer_atr30"] * e["atr30"])) < 1e-6
# no-lookahead invariant (the 10f discipline): truncate the tape one tick
# after the fill -> same stop/target on the filled episode.
t2 = {k: v[: e["entry_tick"] + 2] for k, v in t.items()}
e2 = [x for x in episodes(t2, PARAMS_PROVISIONAL) if x["kind"] == "filled"][0]
assert (e2["stop_px"], e2["target_px"]) == (e["stop_px"], e["target_px"])
```

- [ ] **Step 3: Implement `episodes()` and the class**

Shape (one pass over 30s closed bars per session; zones precomputed from 15m bars; tick windows only for fills):

```python
def episodes(tape, p):
    b30 = _build_bars(tape, 30)      # local rebar (numpy-only copy of
    b15 = _build_bars(tape, 900)     # tape.build_bars -- sandbox: no imports)
    # map each 15m zone's born/died bar CLOSE times onto 30s bar indices
    # per session: iterate 30s bars i:
    #   ZONE interaction: close inside band -> zone "touched_recently"
    #   LEG: first close beyond band edge by leg_min_atr15*atr15 away from a
    #        recently-touched zone -> arm leg (dir, arm_px=c[i], arm_i=i)
    #   IMPULSE: leg extreme (running max/min of h/l) - arm_px >= impulse_min_atr30*atr30
    #   PULLBACK: counter-move from leg extreme >= pullback_min_atr30*atr30 -> hunt on;
    #        track pull_ext (running extreme of the pullback)
    #   TRIGGER: hunt on, closed bar matches an enabled candle in leg dir ->
    #        entry_stop_px = h[i] + off (long) / l[i] - off (short);
    #        scan ticks in the next entry_ttl_bars bars for the first print
    #        beyond entry_stop_px (np.searchsorted window; the print IS the
    #        pessimistic fill price the engine will use);
    #        filled -> emit episode, consume attempt; unfilled -> emit "expired",
    #        attempt NOT consumed, keep hunting
    #   LEG DEATH: close back inside origin zone / next zone reached /
    #        leg_timeout_min / attempts exhausted -> emit terminal episode kind
    # RTH + flatten: no trigger accepted whose entry window would extend past
    # flatten_hhmm (parse HHMM -> seconds-of-day; compare via sec_of_day of bar t)
```

`entries()` wraps it:

```python
def entries(self, bars, tape, p):
    eps = [e for e in episodes(tape, p) if e["kind"] == "filled"]
    if not eps:
        return _EMPTY4
    et = np.array([e["entry_tick"] for e in eps], np.int64)
    dr = np.array([e["dir"] for e in eps], np.int8)
    st = np.array([e["stop_px"] for e in eps])
    tg = np.array([e["target_px"] for e in eps])
    if p.get("breakeven_at_r", 0) > 0:
        be = np.full(len(et), p["breakeven_at_r"] / (1.0 + p["target_r"]))
        return et, dr, st, tg, None, be
    return et, dr, st, tg
```

`params` dict of `Param(...)` built from `PARAMS_PROVISIONAL` (flags and structural ints `fixed=True`; the five CALIBRATE dials also `fixed=True` — they are frozen by calibration, not by the optimizer).

- [ ] **Step 4: Run selfcheck — episodes block passes (plus all Task 1 blocks still green)**

- [ ] **Step 5: Negative selfchecks (write, run, pass)**

Three cheap asserts on mutated copies of the fixture: (a) remove the second zone touch → no leg, no episodes; (b) shrink the impulse below threshold → no trigger accepted; (c) move the trigger candle after `flatten_hhmm` → no fill emitted.

- [ ] **Step 6: Commit**

```bash
git add propsim/pullback_zone.py
git commit -m "feat: episode state machine + PropSim Strategy subclass (closed param list)"
```

---

### Task 3: Plugin check, research runner, episode corpus dump

**Files:**
- Create: `research/dump_episodes.py`
- Test: plugin sandbox check + a real-tape smoke slice

**Interfaces:**
- Consumes: `episodes(tape, p)`, `PARAMS_PROVISIONAL`; PropSim `tape.load_cache/slice_range`, `plugins.py --check`.
- Produces: `research/dump_episodes.py --contract "NQ 09-26" --start D --end D --out FILE.jsonl` writing one JSON object per episode with the Task 2 keys plus `date` and `source: "propsim"` — the schema `compare_mirror.py` (Task 7) joins on.

- [ ] **Step 1: Sandbox check of the plugin file**

Run: `python3 "../PropSim/plugins.py" --check propsim/pullback_zone.py`
Expected: clean pass (imports are math/numpy only; if the checker flags the `try/except` engine import, move the Strategy subclass behind `if TYPE CHECK` is NOT allowed — instead keep the subclass in the same file but make the import `try: from engine import Strategy, Param / except ImportError: Strategy = object; Param = None`, which the AST allowlist accepts as module-level try/except — verify, and if the allowlist still rejects it, split the class into the runner side and keep only pure functions + a `build(engine_mod)` factory in the plugin file; the spec's closed-list contract is unaffected).

- [ ] **Step 2: Write `research/dump_episodes.py` (~40 lines)**

```python
#!/usr/bin/env python3
"""Dump PullbackZone episodes from the PropSim tape as JSONL (mirror corpus)."""
import argparse, json, sys
from pathlib import Path
PROPSIM = Path(__file__).resolve().parents[2] / "PropSim"
sys.path.insert(0, str(PROPSIM))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "propsim"))
import tape
from pullback_zone import episodes, PARAMS_PROVISIONAL

ap = argparse.ArgumentParser()
ap.add_argument("--contract", default="ALL")
ap.add_argument("--start"); ap.add_argument("--end")
ap.add_argument("--out", required=True)
a = ap.parse_args()
t = tape.load_cache(a.contract, a.start, a.end)
t = tape.slice_range(t, a.start, a.end, rth_only=True)
with open(a.out, "w") as f:
    for e in episodes(t, dict(PARAMS_PROVISIONAL)):
        e["date"] = tape.date_str(int(tape.day_index(
            np.array([e["trig_ts"] or e["leg_arm_ts"]]))[0]))
        e["source"] = "propsim"
        f.write(json.dumps(e) + "\n")
print("wrote", a.out)
```

(Import `numpy as np` at top; keep exact key names — Task 7 depends on them.)

- [ ] **Step 3: Smoke run on a real session**

Run: `python3 research/dump_episodes.py --contract "NQ 09-26" --start 2026-08-04 --end 2026-08-04 --out /tmp/pz_smoke.jsonl && wc -l /tmp/pz_smoke.jsonl`
Expected: runs clean; a plausible episode count for one RTH session (single digits to low tens, NOT hundreds — if hundreds, the leg gating is broken; stop and inspect before proceeding).

- [ ] **Step 4: Commit**

```bash
git add research/dump_episodes.py
git commit -m "research: episode corpus dump (PropSim side of the mirror)"
```

---

### Task 4: Calibration — freeze the five data-derived defaults

**Files:**
- Create: `research/calibrate.py`
- Modify: `propsim/pullback_zone.py` (PARAMS_PROVISIONAL → frozen values), `docs/specs/2026-08-05-pullbackzone-design.md` (parameter table)

**Interfaces:**
- Consumes: `episodes()` internals — import the pure pieces (`zones`, `pivots`, `wilder_atr`) and the state machine with **structural provisional thresholds**; `tape.load_cache("ALL")`.
- Produces: a printed calibration table (last 30 sessions vs full sample, side by side) and the frozen values written into `PARAMS_PROVISIONAL` (renamed `PARAMS_DEFAULT` at this point) and the spec.

- [ ] **Step 1: Write the four measurements (percentiles of behavior, never P&L)**

```python
# 1. zone_width_atr15: among 15m pivot levels, distribution of |retest
#    extreme - pivot| / ATR15 across later touches -> p60 (band that captures
#    the typical respectful touch without swallowing the chart).
# 2. leg_min_atr15: distribution of the max excursion away from a touched
#    zone within 30 min of the touch -> p40 as the arming floor (below it,
#    departures usually chop back).
# 3. impulse_min_atr30 / pullback_min_atr30: joint distribution of leg
#    extensions and their retracements (in ATR30 units) for armed legs ->
#    impulse floor = p50 of extensions that later made a NEW extreme;
#    pullback floor = p30 of retracement depths among those same legs.
#    MUST land well above one 30s bar's own range (the feasibility study's
#    trap: 1.0 x ATR30 ~= a single bar, P(range >= ATR) = 39%) -- assert the
#    chosen impulse floor >= 1.5 in ATR30 units or flag loudly.
# 4. stop_buffer_atr30: among pullbacks whose leg DID continue (a new leg
#    extreme after the trigger), distribution of the adverse pierce beyond
#    the pullback extreme before continuation -> p80 (Javier's requirement:
#    the buffer reflects real NQ volatility, not token ticks).
```

- [ ] **Step 2: Run on last 30 sessions AND full ALL sample, print both columns**

Run: `python3 research/calibrate.py`
Expected: a table with the four measurements × two windows + chosen value per dial. If the two windows disagree wildly (>2× on any dial), stop and show Javier before freezing — regime sensitivity is his call.

- [ ] **Step 3: Freeze — update `PARAMS_DEFAULT` in `propsim/pullback_zone.py`, update the spec table (replace "TBD by calibration"), re-run the module selfcheck (fixture thresholds may need the same constants — the fixture builds its own geometry from the params, so it must still pass unchanged)**

- [ ] **Step 4: Commit**

```bash
git add research/calibrate.py propsim/pullback_zone.py docs/specs/2026-08-05-pullbackzone-design.md
git commit -m "research: calibrate and freeze the five data-derived defaults (percentiles, no P&L)"
```

---

### Task 5: NT8 strategy part 1 — series, zones, detection, drawings, corpus (no orders)

**Files:**
- Create: `ninjascript/PullbackZoneStrategy.cs`

**Interfaces:**
- Consumes: frozen defaults from Task 4 (copied as `SetDefaults` values — property names are the PascalCase twins of the snake_case params).
- Produces: the full detection layer later tasks hang orders on — `_zones` (List<Zone>), `_leg` (current Leg state), `OnBarUpdate` split by `BarsInProgress` (0 = 30s primary, 1 = 15m), JSONL writer `FlowNote(string kind, ...)` appending to `%USERPROFILE%\Documents\PullbackZone\pz_corpus.jsonl`.

- [ ] **Step 1: Skeleton with both series and state (compiles via nt8c before any logic)**

```csharp
public class PullbackZoneStrategy : Strategy
{
    private class Zone { public double Px, HalfW; public int Touches; public bool PivotHigh; public bool Dead; public DateTime Born; }
    private class Leg  { public Zone Origin; public int Dir; public double ArmPx, Extreme, PullExt; public DateTime ArmedAt; public int Attempts; public bool HuntOn; }

    protected override void OnStateChange()
    {
        if (State == State.SetDefaults)
        {
            Name = "PullbackZoneStrategy";
            Calculate = Calculate.OnBarClose;              // decisions on closed bars; resting orders act intrabar
            EntriesPerDirection = 1;
            // frozen defaults from Task 4 go here, one property per snake_case param
        }
        else if (State == State.Configure)
            AddDataSeries(BarsPeriodType.Minute, 15);       // BarsInProgress == 1
    }

    protected override void OnBarUpdate()
    {
        if (CurrentBars[0] < BarsRequiredToTrade || CurrentBars[1] < ZonePivotK * 2 + 1) return;
        if (BarsInProgress == 1) { UpdateZones(); return; } // 15m close: pivots, touches, births, deaths, merges
        if (BarsInProgress != 0) return;
        UpdateLeg();                                        // 30s close: leg arm/extreme/pullback/death
        HuntTrigger();                                      // candle predicates -> Task 6 places the entry order
    }
}
```

Port `UpdateZones`/`UpdateLeg` and the three candle predicates from `propsim/pullback_zone.py` line by line — same names, same proportions, same session-reset Wilder ATR (compute both ATR15 and ATR30 manually; `nt8c` cannot resolve the system `ATR()` indicator — known gotcha, same workaround as LatigoBreak/Apertura4HMSS).

- [ ] **Step 2: Compile**

Run: edit triggers the `nt8c` PostToolUse hook; or explicitly `nt8c build` per `docs/tooling/nt8c.md`.
Expected: clean compile (the hook output in-session).

- [ ] **Step 3: Drawings + corpus writer**

- Zone boxes: `Draw.Rectangle(this, "PZzone"+id, ...)` spanning born→now at `Px ± HalfW`, updated on 15m closes; dead zones grey out.
- Trigger marker: `Draw.TriangleUp/Down` on the trigger bar; pullback-extreme dot (`Draw.Dot`) at the stop anchor.
- `FlowNote(...)`: locked, flushed JSONL append — port the writer shape from `LatigoBreakStrategy.cs` (the v4 corpus writer: lock object, `File.AppendAllText` with a `StreamWriter` flush, epoch-stamped). Schema = Task 3's keys + `source: "nt8"`.
- Rewind fence: epoch counter incremented on `State.Transition`/rewind so stale in-flight notes are dropped (LatigoBreak lesson — fence by epoch, not a boolean).

- [ ] **Step 4: Compile + visual smoke in NT8 (human step)**

Deploy: copy the `.cs` to the NT8 Custom folder (standing rule: Claude copies, Javier presses F5), load on a 30s NQ chart, confirm zone boxes appear where a hand-drawn 15m level would be and triggers mark sensible candles. No orders yet, so this is safe on any connection.

- [ ] **Step 5: Commit**

```bash
git add ninjascript/PullbackZoneStrategy.cs
git commit -m "feat(nt8): detection layer — 15m zones, legs, triggers, drawings, JSONL corpus (no orders)"
```

---

### Task 6: NT8 orders — entry stop with TTL, v3 brackets, re-entry, flatten, daily guard

**Files:**
- Modify: `ninjascript/PullbackZoneStrategy.cs`

**Interfaces:**
- Consumes: Task 5 state (`_leg`, trigger events); the proven bracket pattern in `projects/Trading/LatigoBreak/LatigoBreakStrategy.cs` (Order-reference echo filtering, deferred hand-cancel warning, adoption of dragged brackets) — port it, do not reinvent it.
- Produces: complete tradeable strategy (sim/Playback).

- [ ] **Step 1: Entry order + TTL**

```csharp
private Order _entryOrder; private int _entryBar; private double _pendStop, _pendTarget;

private void PlaceEntry(int dir, double trigExtreme)
{
    double off = EntryOffsetTicks * TickSize;
    _pendingEntry = true;                                  // in-flight flag BEFORE submit
    _entryBar = CurrentBars[0];
    if (dir > 0) _entryOrder = EnterLongStopMarket(0, true, Contracts, trigExtreme + off, "PZ_Entry");
    else         _entryOrder = EnterShortStopMarket(0, true, Contracts, trigExtreme - off, "PZ_Entry");
}
// in OnBarUpdate (BIP 0): unfilled entry older than EntryTtlBars -> CancelOrder(_entryOrder)
// (flag before the call); cancellation does NOT consume the leg attempt.
```

Stop/target prices are computed AT TRIGGER TIME (pullback extreme ± StopBufferAtr30 × atr30; target = entry ± TargetR × risk) into `_pendStop/_pendTarget` so the fill handler never recomputes from later data.

- [ ] **Step 2: Brackets on fill — port the LatigoBreak v3/v4 pattern verbatim**

From `LatigoBreakStrategy.cs`: `OnExecutionUpdate` submits `ExitLongStopMarket/ExitLongLimit` (+ short mirrors, `isLiveUntilCancelled: true`, signals `PZ_Stop`/`PZ_Target`) at the real fill price; keep `_stopOrder`/`_targetOrder` references, **null them before each own re-submit** so in-stack echoes never match; adoption of hand-dragged brackets into `_stopPx/_targetPx` via `OnOrderUpdate`; hand-cancelled target stays cancelled (`_targetPx = 0`); deferred 1-s `CheckBracketCancels` warning, only `InPosition`; went-flat bookkeeping gated by epoch (stale-event fence). Attempt is consumed HERE (on entry fill), and `_leg.Attempts++`.

- [ ] **Step 3: Breakeven (optional, off by default) + flatten + daily guard**

- Breakeven: if `BreakevenAtR > 0` and unrealized run ≥ `BreakevenAtR × risk` → move stop to entry ± `BeOffsetTicks` (tracker updated BEFORE the change-order call — the v4 echo lesson).
- Flatten: at `FlattenHhmm` (`ToTime(Time[0]) >= FlattenHhmm * 100`) cancel entry, flatten position, lockout until next session.
- Daily guard: if `DailyLossR > 0`, accumulate closed-trade R (from `SystemPerformance.AllTrades` of the session or an internal tally at each flat); when the sum ≤ −`DailyLossR`, lockout for the day. Note in code: PropSim cannot mirror this dial (accepted delta 1); it defaults to 0.

- [ ] **Step 4: Compile + Playback session (human step)**

Deploy the `.cs`; Javier runs one Market Replay session with `Contracts=1` on sim. Checklist to verify by hand: entry stop appears only after a trigger, TTL cancels it, brackets appear at the fill, dragging SL/TP is adopted, flatten fires at 15:58, corpus lines appear for every state change.

- [ ] **Step 5: Commit**

```bash
git add ninjascript/PullbackZoneStrategy.cs
git commit -m "feat(nt8): entry stop with TTL, movable v3 brackets, re-entry cap, flatten, daily R guard"
```

---

### Task 7: Mirror gate — `research/compare_mirror.py` (V1)

**Files:**
- Create: `research/compare_mirror.py`

**Interfaces:**
- Consumes: two JSONL corpora (`source: "nt8"` from Playback sessions; `source: "propsim"` from `dump_episodes.py` on the same dates).
- Produces: console report + exit code (0 = PASS) — the V1 gate: same trade set, entry/stop/target within 1 tick, on ≥5 Replay sessions.

- [ ] **Step 1: Write the join + report (~60 lines)**

Join key: `(date, dir, round(zone_px / 0.25), trig_ts within ±30 s)`. For each matched pair report `Δentry, Δstop, Δtarget` in ticks; list unmatched episodes per side with their `kind`. PASS = ≥95% of filled episodes matched AND every matched Δ ≤ 1 tick. Print the three known divergence sources checklist (ATR seeding, first-session warmup, 15m bar alignment) when it fails.

- [ ] **Step 2: Dry-run the joiner on synthetic corpora (write two tiny JSONL fixtures inline in a `--selftest` mode: one perfect match, one 2-tick mismatch → PASS then FAIL). Run both, verify exit codes.**

- [ ] **Step 3: Commit**

```bash
git add research/compare_mirror.py
git commit -m "research: NT8<->PropSim mirror gate (V1 fidelity join)"
```

- [ ] **Step 4: V1 execution (human-in-the-loop, not this session):** Javier records ≥5 Market Replay sessions with the NT8 side; same dates dumped from PropSim; gate must PASS before any backtest number is quoted. Findings — including accepted deltas actually observed — go to `docs/validation.md`.

---

### Task 8: README, validation protocol, deploy

**Files:**
- Create: `README.md` (invoke the `readme-craft` skill — public repo, the README is the project's face)
- Create: `docs/validation.md`
- Deploy: copy `ninjascript/PullbackZoneStrategy.cs` to the NT8 Custom strategies folder

- [ ] **Step 1: `docs/validation.md`** — pre-registered protocol verbatim from the spec (V0/V1/V2 + the honest prior: the 2026-08-05 feasibility study killed the naive limit-at-level version; this design's additions are the hypothesis under test). Append-only results log.

- [ ] **Step 2: README via `readme-craft`** — what it is, the two mirrored implementations, honest-use note (sim/Playback laboratory until validation says otherwise), install (NT8 import + PropSim plugin copy), the Playback checklist from Task 6, current gate status table.

- [ ] **Step 3: Deploy + final commit + push**

```bash
git add README.md docs/validation.md
git commit -m "docs: README + pre-registered validation protocol"
git push
```

Copy the `.cs` to `/mnt/c/Users/javlo/Documents/NinjaTrader 8/bin/Custom/Strategies/` (Claude copies, Javier presses F5 — standing rule).

---

## Self-review notes (kept for the executor)

- Spec coverage: zones/leg/pullback/trigger/entry/exits/re-entry/session → Tasks 1–2 (Python) and 5–6 (C#); calibration → Task 4; mirror contract → Tasks 3+7; validation/README → Task 8. Out-of-scope list untouched by any task. ✓
- The five calibrated dials appear as `CALIBRATE` provisionals in Task 2 and are frozen in Task 4 — the spec's "TBD by calibration" cells are intentional until then.
- Type consistency: episode dict keys defined once (Task 2 Interfaces) and reused by Tasks 3 and 7; param names identical across Task 2 (snake_case) and Tasks 5–6 (PascalCase twins).
- Order of work puts every backtestable number AFTER calibration and BEHIND the V1 mirror gate, matching the spec's "no number is believed" rule.
