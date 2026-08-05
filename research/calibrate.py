#!/usr/bin/env python3
"""Freeze the five data-derived PullbackZone defaults from PERCENTILES OF
MARKET BEHAVIOUR.

No profit, expectancy, win rate or R multiple is computed anywhere in this
file, and none may be added. The five dials are picked from the geometry of
the tape so that a later backtest is a test of the pattern rather than a
readout of the search that produced it. `risk_envelope` reports the stop
DISTANCE the frozen values imply -- a property of the setup, not of its
outcome -- because a percentile rule can quietly produce an untradeable risk
envelope and that has to be visible before the freeze, not after.

Reproducible: same tape in, same table out. Every stage re-derives its dial
from the tape and OVERRIDES it before the next stage runs, so re-running this
after the freeze reproduces the table instead of reading the answers back out
of PARAMS_DEFAULT.

Dependency order (ONE pass, deliberately not iterated to convergence -- that
would be tuning under another name):

    zone_width_atr15 -> leg_min_atr15 -> impulse/pullback_min_atr30
                                      -> stop_buffer_atr30

Each stage consumes the frozen output of the ones above it and nothing below.

ATR WARMUP. `wilder_atr` has no session reset (house rule, mirrored on both
sides), so the first bar of every RTH session carries the overnight gap in its
true range and the recursion decays it over roughly n bars. Every measurement
below therefore carries a `warm` flag per observation and the table reports
both variants; the frozen column excludes them.

Usage: python3 research/calibrate.py [--contract ALL] [--recent 30] [--end DATE]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "PropSim"))
sys.path.insert(0, str(ROOT / "propsim"))
import tape                                                    # noqa: E402
from pullback_zone import (PARAMS_DEFAULT, TICK, _ATR_N30,     # noqa: E402
                           _ZONE_ATR_N15, _build_bars, _day_index,
                           _sec_of_day, candle_doji, candle_engulfing,
                           candle_hammer, episodes, pivots, wilder_atr, zones)

_TPS = 10_000_000
POINT_USD = 20.0                    # NQ, dollars per index point
TICK_USD = POINT_USD * TICK         # $5 a tick

# The percentile each dial is frozen at. These are the pre-registered rules
# from the task brief; they are inputs to this script, not results of it.
P_ZONE_WIDTH = 60      # band that holds the typical respectful touch
P_LEG_MIN = 40         # below it, departures usually chop back
P_IMPULSE = 50         # a median continuation impulse
P_PULLBACK = 30        # a shallow-but-real continuation pullback
P_STOP = 80            # the buffer survives 80% of the pierces it will see

CAP_ATR15 = 1.0        # M1: how far off a level still counts as an approach
M2_WINDOW_MIN = 30     # M2: minutes after a touch the departure is measured
WARM = 14              # ATR warmup bars per session (= both ATR periods)
GRID = 0.05            # frozen values round to this (a default, not a fit)

SHOW = (10, 20, 30, 40, 50, 60, 70, 80, 90)

# The pre-calibration values, pinned here rather than read from PARAMS_DEFAULT
# so the before/after rows keep meaning something once the freeze lands.
PROVISIONAL = dict(zone_width_atr15=0.25, leg_min_atr15=0.50,
                   impulse_min_atr30=2.0, pullback_min_atr30=1.0,
                   stop_buffer_atr30=0.50)


# ------------------------------------------------------------------ helpers
def pctl(v, q):
    return float(np.percentile(v, q)) if len(v) else float("nan")


def boot_ci(v, q, draws=2000, seed=0):
    """95% bootstrap interval for a percentile -- how much of a two-window
    disagreement is regime and how much is just a short sample. Seeded, so the
    table stays reproducible."""
    v = np.asarray(v, float)
    if len(v) < 4:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    s = np.percentile(rng.choice(v, (draws, len(v)), replace=True), q, axis=1)
    return float(np.percentile(s, 2.5)), float(np.percentile(s, 97.5))


def snap(x):
    return round(round(x / GRID) * GRID, 4)


def _warm_mask(day):
    """True on the first WARM bars of each session."""
    n = len(day)
    if not n:
        return np.zeros(0, bool)
    starts = np.flatnonzero(np.concatenate(([True], np.diff(day) != 0)))
    i = np.arange(n)
    return (i - starts[np.searchsorted(starts, i, "right") - 1]) < WARM


def prep(t):
    """The two bar series, both ATRs and the 30s->15m map -- exactly as
    `episodes` builds them, so the percentiles describe the same bars the
    strategy will see."""
    b30, b15 = _build_bars(t, 30), _build_bars(t, 900)
    ts = t["ts"]
    tc30, tc15 = ts[b30["end"] - 1], ts[b15["end"] - 1]
    day30, day15 = _day_index(tc30), _day_index(tc15)
    return dict(
        b30=b30, b15=b15, tc30=tc30, tc15=tc15, day30=day30, day15=day15,
        sod30=_sec_of_day(tc30),
        atr30=wilder_atr(b30["h"], b30["l"], b30["c"], _ATR_N30),
        atr15=wilder_atr(b15["h"], b15["l"], b15["c"], _ZONE_ATR_N15),
        j15=np.searchsorted(tc15, tc30, "right") - 1,
        warm30=_warm_mask(day30), warm15=_warm_mask(day15),
        sessions=int(len(np.unique(day30))))


def zones_on_30s(D, p):
    """`zones` plus the 30s index window `episodes` maps them onto."""
    zs = zones(D["b15"], D["day15"], p)
    n30, n15 = len(D["b30"]["c"]), len(D["b15"]["c"])
    tc30, tc15 = D["tc30"], D["tc15"]
    for z in zs:
        z["i0"] = int(np.searchsorted(tc30, tc15[z["born_i"]], "left"))
        z["i1"] = (n30 if z["died_i"] >= n15
                   else int(np.searchsorted(tc30, tc15[z["died_i"]], "left")))
        z["touched"] = False
    zs.sort(key=lambda z: z["i0"])
    return zs


# ------------------------------------------------------- M1: zone_width_atr15
def _approaches(D, p):
    """Per live pivot level, every later 15m bar that approaches it from its
    own side and closes back on that side, as (distance / ATR15, is_swing,
    warm). A level stops contributing once a close clears it by
    `zone_break_atr15` -- measured off the pivot price rather than the band
    edge, which is the one place the band's own width may not enter -- or once
    `zone_expiry_sessions` have passed.

    Yields one array per pivot, nearest first, so callers can take either the
    whole population or a per-pivot order statistic.
    """
    h, l, c, a = D["b15"]["h"], D["b15"]["l"], D["b15"]["c"], D["atr15"]
    day, warm, n = D["day15"], D["warm15"], len(D["b15"]["c"])
    k, brk = int(p["zone_pivot_k"]), float(p["zone_break_atr15"])
    life = int(p["zone_expiry_sessions"])
    hi, lo = pivots(h, l, k)
    for idxs, ext, sign in ((hi, h, 1.0), (lo, l, -1.0)):
        peers = np.zeros(n, bool)
        peers[idxs] = True
        for j in idxs:
            j, born = int(j), int(j) + k
            P = float(ext[j])
            last = int(np.searchsorted(day, day[born] + life, "left"))
            if last <= born + 1:
                continue
            m = np.arange(born + 1, last)
            dead = np.flatnonzero(sign * (c[m] - P) > brk * a[m])
            if len(dead):
                m = m[:dead[0]]
            m = m[(a[m] > 0) & (sign * (c[m] - P) < 0)]
            if not len(m):
                continue
            d = np.abs(ext[m] - P) / a[m]
            o = np.argsort(d, kind="stable")
            yield d[o], peers[m][o], warm[m][o]


def m1_zone_width(D, p, swings=False):
    """The NEAREST later approach to each pivot level, in ATR15s. One
    observation per level, so it is cap-free, and it reads as the dial it sets:
    a band of half-width w gives this pivot at least one touch when w >= the
    value, so the CDF approximates "fraction of pivot levels a band of width w
    touches at all" and p60 is roughly the width that reaches 60% of them.

    Approximates, NOT "iff" -- three slacks separate this metric from what
    `zones` scores:
      1. ATR: the band is frozen at the ATR15 of the pivot's REVEAL bar, this
         normalises by the ATR15 of the approach bar.
      2. Rejection test: `zones` wants the close outside the NEAR EDGE, this
         wants it on the pivot's side -- they differ inside the band itself.
      3. Count: a zone needs `zone_min_touches` (2), this takes the first.
    Left uncorrected on purpose. Closing slack 1 and 2 means solving for the
    smallest w that makes a bar both reach the near edge and close outside it,
    which also changes WHICH pivots are eligible (n 547 -> 894 on the full
    sample) -- so it is a different population, not a corrected reading of this
    one, and the p60 of the two is not comparable. The dial rests on the
    percentile of the metric as defined here.

    `swings=False` (the frozen form) scores any later bar, which is literally
    what `zones` tests -- `h[i] >= edge and c[i] < edge`, a bar, not a swing.
    `swings=True` demands a confirmed swing rejection instead. The two answer
    different questions and the gap between them is itself a finding, so both
    chains are reported; the frozen one is the one the code implements.

    Why not the brief's literal "distribution across later touches": whether a
    bar IS a touch is exactly what the width decides, so that population can
    only be formed by first assuming a neighbourhood, and
    `m1_approaches_capped` shows the answer then just tracks the assumption
    (p60 ~= 0.5 x cap -- approach distances are near-uniform, i.e. 15m pivot
    levels show no measurable clustering of later approaches at ATR15
    resolution). One order statistic per level removes the free parameter.
    """
    vals, warms = [], []
    for d, is_sw, warm in _approaches(D, p):
        k = np.flatnonzero(is_sw) if swings else np.arange(len(d))
        if len(k):
            vals.append(d[k[0]])
            warms.append(bool(warm[k[0]]))
    return np.array(vals), np.array(warms, bool)


def m1_approaches_capped(D, p, cap=CAP_ATR15):
    """The brief's literal form, kept for the robustness table: every approach
    within `cap` x ATR15. Reported to show its cap dependence, not to freeze."""
    vals, warms = [], []
    for d, _is_sw, warm in _approaches(D, p):
        k = d <= cap
        vals.append(d[k])
        warms.append(warm[k])
    if not vals:
        return np.zeros(0), np.zeros(0, bool)
    return np.concatenate(vals), np.concatenate(warms)


# ---------------------------------------------------------- M2: leg_min_atr15
def m2_leg_min(D, p):
    """Max departure from the zone EDGE within 30 minutes of a touch, in
    ATR15s -- the exact quantity `episodes` compares against `leg_min_atr15`
    (`c30 > px + half_w + leg_min * a15`), so p40 of this distribution is
    "the floor 40% of touches never clear".

    One observation per touch EVENT (the first bar of a run of closes inside
    the band), bounded by the zone's own life and by the session: a touch does
    not survive the overnight gap, same rule as the state machine.
    """
    b30, tc30, day30 = D["b30"], D["tc30"], D["day30"]
    c30, a15, j15 = b30["c"], D["atr15"], D["j15"]
    n30 = len(c30)
    a_at = np.where(j15 >= 0, a15[np.maximum(j15, 0)], np.nan)
    win = M2_WINDOW_MIN * 60 * _TPS
    vals, warms = [], []
    for z in zones_on_30s(D, p):
        inside = False
        for i in range(max(z["i0"], 0), min(z["i1"], n30)):
            if abs(c30[i] - z["px"]) > z["half_w"]:
                inside = False
                continue
            if inside:
                continue
            inside = True
            end = min(int(np.searchsorted(tc30, tc30[i] + win, "right")),
                      z["i1"], n30)
            sl = slice(i, end)
            dep = np.abs(c30[sl] - z["px"]) - z["half_w"]
            ok = (day30[sl] == day30[i]) & (dep > 0) & (a_at[sl] > 0)
            v = np.where(ok, dep / np.where(a_at[sl] > 0, a_at[sl], 1.0), 0.0)
            vals.append(float(v.max()) if len(v) else 0.0)
            warms.append(bool(D["warm30"][i]))
    return np.array(vals), np.array(warms, bool)


# ------------------------ M3/M4: impulse, pullback, stop buffer (one leg walk)
def leg_walk(D, p, floors=None):
    """Every retracement inside every armed leg, and what became of it.

    Same arming, same leg-death gates and the same refusal to credit a
    new-extreme bar with its own counter wick as `episodes`. What it does NOT
    reproduce is the order state: no fill, no `busy` gate, no attempt cap, so a
    leg's later pullbacks stay observable instead of being hidden behind a
    position. Calibration wants the population of pullbacks the market offers,
    not the subset one order state happened to let through.

    `floors=(impulse, pullback)` additionally runs the trigger hunt, which is
    what M4 needs: the stop anchors on the pullback extreme AS IT STOOD AT THE
    TRIGGER BAR, so the pierce cannot be measured without knowing that bar.
    Rows: ext_norm (extension from the arming point at the leg extreme, ATR30s
    there), depth_norm (deepest retracement reached, ATR30s), continued (a new
    leg extreme followed), pierce (adverse move past the trigger-time pullback
    extreme before that new extreme, ATR30s; nan when no trigger fired).
    """
    b30 = D["b30"]
    o30, h30, l30, c30 = b30["o"], b30["h"], b30["l"], b30["c"]
    atr30, atr15, j15 = D["atr30"], D["atr15"], D["j15"]
    tc30, day30, sod30, warm30 = D["tc30"], D["day30"], D["sod30"], D["warm30"]
    n30 = len(c30)
    zs = zones_on_30s(D, p)
    hh = int(p["flatten_hhmm"])
    cutoff = (hh // 100) * 3600 + (hh % 100) * 60
    timeout = int(p["leg_timeout_min"] * 60 * _TPS)
    imp_f, pb_f = floors if floors else (None, None)

    rows = []
    leg, live, zi, prev_day, n_legs = None, [], 0, -1, 0

    def start_ep(px, i, a30):
        leg.update(ext=px, ext_i=i, pull=px, depth=0.0, trig_i=-1, hunt_i=-1,
                   anchor=0.0, pierce=0.0,
                   ext_norm=leg["dir"] * (px - leg["arm_px"]) / a30)

    def flush(continued):
        if leg is None or leg["depth"] <= 0:
            return
        rows.append((leg["ext_norm"], leg["depth"], continued,
                     leg["pierce"] if leg["trig_i"] >= 0 else np.nan,
                     bool(warm30[leg["ext_i"]]),
                     leg["hunt_i"] - leg["ext_i"] if leg["hunt_i"] >= 0 else -1))

    for i in range(n30):
        while zi < len(zs) and zs[zi]["i0"] <= i:
            live.append(zs[zi])
            zi += 1
        if live and any(i >= z["i1"] for z in live):
            live = [z for z in live if i < z["i1"]]
        if day30[i] != prev_day:
            prev_day = int(day30[i])
            for z in zs:
                z["touched"] = False
        jj = int(j15[i])
        if jj < 0:
            continue
        a15v, a30v = float(atr15[jj]), float(atr30[i])
        if not (a15v > 0 and a30v > 0):
            continue

        if leg is not None:
            d, z0 = leg["dir"], leg["z"]
            dead = (day30[i] != leg["day"] or sod30[i] >= cutoff
                    or tc30[i] - leg["t0"] >= timeout
                    or abs(c30[i] - z0["px"]) <= z0["half_w"]
                    or any(z is not z0 and d * (z["px"] - z0["px"]) > 0
                           and (l30[i] <= z["px"] + z["half_w"] if d < 0
                                else h30[i] >= z["px"] - z["half_w"])
                           for z in live))
            if dead:
                flush(False)
                leg = None

        for z in live:
            if abs(c30[i] - z["px"]) <= z["half_w"]:
                z["touched"] = True

        if leg is None and sod30[i] < cutoff:
            gap = p["leg_min_atr15"] * a15v
            for z in live:
                if not z["touched"]:
                    continue
                if c30[i] > z["px"] + z["half_w"] + gap:
                    d = 1
                elif c30[i] < z["px"] - z["half_w"] - gap:
                    d = -1
                else:
                    continue
                z["touched"] = False
                leg = dict(z=z, dir=d, arm_px=float(c30[i]), impulse=False,
                           t0=int(tc30[i]), day=int(day30[i]))
                start_ep(float(c30[i]), i, a30v)
                n_legs += 1
                break
            continue

        if leg is None:
            continue
        d = leg["dir"]
        e = float(h30[i] if d > 0 else l30[i])
        adverse = float(l30[i] if d > 0 else h30[i])
        # A resting stop sees this bar's adverse extreme whether or not the bar
        # also made a new leg extreme, and intrabar order is unknowable -- the
        # pessimistic read (the stop was hit) is the house convention.
        if leg["trig_i"] >= 0 and i > leg["trig_i"]:
            leg["pierce"] = max(leg["pierce"],
                                d * (leg["anchor"] - adverse) / a30v)
        if d * (e - leg["ext"]) > 0:
            flush(True)
            start_ep(e, i, a30v)
            continue
        if d * (adverse - leg["pull"]) < 0:
            leg["pull"] = adverse
        leg["depth"] = max(leg["depth"], d * (leg["ext"] - leg["pull"]) / a30v)
        if floors is None:
            continue
        if not leg["impulse"]:
            leg["impulse"] = d * (leg["ext"] - leg["arm_px"]) >= imp_f * a30v
        # Spec amendment 2026-08-05: the hunt arms no earlier than the close of
        # the SECOND bar after the leg extreme. Mirrors `episodes` exactly --
        # the measurement population has to be the one the machine trades.
        if not leg["impulse"] or leg["depth"] < pb_f or i - leg["ext_i"] < 2:
            continue
        if leg["hunt_i"] < 0:
            leg["hunt_i"] = i           # bars from the extreme to the arming
        if leg["trig_i"] >= 0:
            continue
        if p["use_engulfing"] and candle_engulfing(o30, h30, l30, c30, i, d):
            pass
        elif p["use_hammer"] and candle_hammer(o30, h30, l30, c30, i, d):
            pass
        elif (p["use_doji_star"] and candle_doji(o30, h30, l30, c30, i)
              and abs((h30[i] if d < 0 else l30[i]) - leg["pull"]) < 1e-9):
            pass
        else:
            continue
        leg["trig_i"], leg["anchor"] = i, leg["pull"]
    flush(False)
    if not rows:
        return dict(legs=n_legs, **{k: np.zeros(0) for k in
                                    ("ext", "depth", "cont", "pierce",
                                     "warm", "lag")})
    a = np.array(rows, dtype=object)
    return dict(legs=n_legs, ext=a[:, 0].astype(float),
                depth=a[:, 1].astype(float), cont=a[:, 2].astype(bool),
                pierce=a[:, 3].astype(float), warm=a[:, 4].astype(bool),
                lag=a[:, 5].astype(int))


# ------------------------------------------------------------------- staging
def stage(v, warm, q, drop_warm=True):
    """One dial: the distribution, the raw percentile and the frozen value."""
    m = ~warm if drop_warm else np.ones(len(v), bool)
    x = np.asarray(v)[m]
    raw = pctl(x, q)
    return dict(n=int(len(x)), raw=raw, value=snap(raw), ci=boot_ci(x, q),
                dist={s: pctl(x, s) for s in SHOW})


def calibrate(D, drop_warm=True, m1_swings=False):
    """The one pass, in dependency order. Each stage overrides its dial in `p`
    before the next stage reads it, so nothing downstream ever sees the value
    that happens to be sitting in PARAMS_DEFAULT."""
    p = dict(PARAMS_DEFAULT)
    out = {}

    v, w = m1_zone_width(D, p, swings=m1_swings)
    out["zone_width_atr15"] = stage(v, w, P_ZONE_WIDTH, drop_warm)
    p["zone_width_atr15"] = out["zone_width_atr15"]["value"]

    v, w = m2_leg_min(D, p)
    out["leg_min_atr15"] = stage(v, w, P_LEG_MIN, drop_warm)
    p["leg_min_atr15"] = out["leg_min_atr15"]["value"]

    r = leg_walk(D, p)
    c = r["cont"]
    out["impulse_min_atr30"] = stage(r["ext"][c], r["warm"][c], P_IMPULSE,
                                     drop_warm)
    out["pullback_min_atr30"] = stage(r["depth"][c], r["warm"][c], P_PULLBACK,
                                      drop_warm)
    p["impulse_min_atr30"] = out["impulse_min_atr30"]["value"]
    p["pullback_min_atr30"] = out["pullback_min_atr30"]["value"]

    r2 = leg_walk(D, p, floors=(p["impulse_min_atr30"], p["pullback_min_atr30"]))
    ok = r2["cont"] & np.isfinite(r2["pierce"])
    out["stop_buffer_atr30"] = stage(r2["pierce"][ok], r2["warm"][ok], P_STOP,
                                     drop_warm)
    p["stop_buffer_atr30"] = out["stop_buffer_atr30"]["value"]
    return out, p, r, r2


DIALS = (("zone_width_atr15", P_ZONE_WIDTH), ("leg_min_atr15", P_LEG_MIN),
         ("impulse_min_atr30", P_IMPULSE), ("pullback_min_atr30", P_PULLBACK),
         ("stop_buffer_atr30", P_STOP))


# ------------------------------------------------------------------ reporting
def risk_envelope(t, p):
    """Stop DISTANCE under the frozen dials. Not a P&L metric: it is how far
    the stop sits from the entry, which is decided at the trigger bar and is
    the thing a prop daily-loss limit has to accommodate."""
    eps = episodes(t, dict(p))
    f = [e for e in eps if e["kind"] == "filled"]
    r = np.array([abs(e["stop_px"] - e["entry_stop_px"]) / TICK for e in f])
    return eps, f, r


def bar_range_rate(D, x):
    """P(a single 30s bar's own range >= x * ATR30) -- the feasibility study's
    trap: a floor a lone wick can clear is not a floor."""
    b, m = D["b30"], ~D["warm30"] & (D["atr30"] > 0)
    return float(np.mean((b["h"][m] - b["l"][m]) >= x * D["atr30"][m]))


def dist_line(name, d, n):
    cells = " ".join(f"{d[s]:6.2f}" for s in SHOW)
    return f"  {name:<20} n={n:<7} {cells}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", default="ALL")
    ap.add_argument("--recent", type=int, default=30, help="sessions in the "
                    "recent window")
    ap.add_argument("--end", default=None, help="pin the last session (ISO)")
    a = ap.parse_args()

    full = tape.slice_range(tape.load_cache(a.contract, None, a.end),
                            None, a.end, rth_only=True)
    days = np.unique(tape.day_index(full["ts"]))
    d0 = days[max(len(days) - a.recent, 0)]
    i0 = int(np.searchsorted(full["ts"],
                             (int(d0) * 86400 + 62135596800) * _TPS, "left"))
    recent = {k: v[i0:] for k, v in full.items()}
    wins = [(f"last {a.recent}", recent), (f"full {len(days)}", full)]

    print(f"PullbackZone calibration -- contract {a.contract}, "
          f"{tape.date_str(days[0])} .. {tape.date_str(days[-1])}, "
          f"{len(days)} RTH sessions")
    print(f"recent window starts {tape.date_str(int(d0))}\n")

    res = {}
    for label, t in wins:
        D = prep(t)
        out, p, r, r2 = calibrate(D)
        out_w, _, _, _ = calibrate(D, drop_warm=False)
        out_s, _, _, _ = calibrate(D, m1_swings=True)
        res[label] = dict(D=D, out=out, warm=out_w, swing=out_s, p=p, t=t,
                          r=r, r2=r2)

    print("=" * 100)
    print("DISTRIBUTIONS (ATR multiples; ATR-warmup bars excluded)")
    print(f"  {'measurement':<20} {'':<9} " + " ".join(f"p{s:<5}" for s in SHOW))
    for label, _ in wins:
        print(f"-- {label} sessions")
        for k, _q in DIALS:
            s = res[label]["out"][k]
            print(dist_line(k, s["dist"], s["n"]))

    print("\n" + "=" * 100)
    print("FROZEN VALUES -- pre-registered percentile per dial, two windows")
    print(f"  {'dial':<20} {'pct':>4} {'recent':>7} {'[95% CI]':>15} "
          f"{'full':>7} {'[95% CI]':>15} {'ratio':>6} {'warm-in r/f':>12} "
          f"{'raw full':>9}")
    blocked = []
    for k, q in DIALS:
        a_, b_ = res[wins[0][0]]["out"][k], res[wins[1][0]]["out"][k]
        rv, fv = a_["value"], b_["value"]
        wr = res[wins[0][0]]["warm"][k]["value"]
        wf = res[wins[1][0]]["warm"][k]["value"]
        ratio = max(rv, fv) / min(rv, fv) if min(rv, fv) > 0 else float("inf")
        overlap = a_["ci"][0] <= b_["ci"][1] and b_["ci"][0] <= a_["ci"][1]
        if ratio > 2.0:
            blocked.append((k, rv, fv, ratio, overlap))
        # `raw` is the unsnapped percentile. Printed because quoting a snapped
        # value as the raw one in a provenance comment is an easy, silent lie.
        print(f"  {k:<20} p{q:<3} {rv:7.2f} "
              f"[{a_['ci'][0]:5.2f},{a_['ci'][1]:6.2f}] {fv:7.2f} "
              f"[{b_['ci'][0]:5.2f},{b_['ci'][1]:6.2f}] {ratio:6.2f} "
              f"{wr:5.2f} /{wf:5.2f} {b_['raw']:9.3f}  n={b_['n']}")
    print("\n  frozen = the FULL-sample value (the bigger sample); the recent "
          "window is the\n  regime-disagreement gate, and >2x on any dial "
          "blocks the freeze.")
    if blocked:
        for k, rv, fv, r, ov in blocked:
            print(f"\n  *** BLOCKED: {k} -- {rv:.2f} vs {fv:.2f} = {r:.2f}x"
                  f"  (bootstrap CIs {'OVERLAP' if ov else 'DISJOINT'})")
        print("  A dial whose two CIs overlap is disagreeing about a "
              "percentile it does not\n  have the sample to pin, which is a "
              "different problem from a regime shift.")
    else:
        print("  gate: PASS (no dial disagrees by more than 2x)")

    frozen = {k: res[wins[1][0]]["out"][k]["value"] for k, _ in DIALS}
    imp = frozen["impulse_min_atr30"]
    print(f"\n  impulse floor >= 1.5 ATR30 (clears single-bar noise): "
          f"{imp:.2f} -> {'OK' if imp >= 1.5 else '*** FLAG: TOO SMALL ***'}")

    print("\n" + "=" * 100)
    print("ROBUSTNESS 1 -- the literal 'all later touches' form of M1 is a cap")
    print("artifact (p60 tracks 0.5 x cap); the frozen form takes one order")
    print("statistic per level instead and has no cap.")
    for label, _ in wins:
        D = res[label]["D"]
        p = dict(PARAMS_DEFAULT)
        caps = "  ".join(
            f"cap {c}: p60={stage(*m1_approaches_capped(D, p, c), P_ZONE_WIDTH)['value']:.2f}"
            for c in (0.5, 1.0, 2.0))
        print(f"  {label:<10} {caps}")

    print("\nROBUSTNESS 2 -- the WHOLE chain re-derived with M1 demanding a")
    print("confirmed swing rejection instead of any bar. zone_width feeds")
    print("everything below it, so this is the full cost of that one choice.")
    print(f"  {'dial':<20} {'frozen (bar touch)':>20} {'alt (swing rej.)':>20}")
    for k, _q in DIALS:
        print(f"  {k:<20} {res[wins[1][0]]['out'][k]['value']:20.2f} "
              f"{res[wins[1][0]]['swing'][k]['value']:20.2f}")
    print("  A bar touch is what `zones` actually tests, so that is the frozen")
    print("  column. The gap between them says most bar-level zone touches are")
    print("  drift through the level, not rejections at it.")

    print("\n" + "=" * 100)
    print("SESSION-OPEN VOLATILITY (mean by bar position in session).")
    print("ATR carries the overnight gap (no session reset); the raw bar RANGE")
    print("cannot. Where both are elevated the open is genuinely faster and")
    print("dropping warmup bars is about the gap alone.")
    for label, _ in wins:
        D = res[label]["D"]
        for nm, key, day, bars in (("ATR30s/rng", "atr30", "day30", "b30"),
                                   ("ATR15m/rng", "atr15", "day15", "b15")):
            v, d = D[key], D[day]
            rng = D[bars]["h"] - D[bars]["l"]
            starts = np.flatnonzero(np.concatenate(([True], np.diff(d) != 0)))
            i = np.arange(len(d))
            pos = i - starts[np.searchsorted(starts, i, "right") - 1]
            cells = []
            for lo, hi in ((0, 1), (1, WARM), (WARM, 2 * WARM),
                           (2 * WARM, 10 ** 9)):
                m = (pos >= lo) & (pos < hi)
                if not m.any():
                    continue
                cells.append(f"[{lo}-{hi if hi < 10**8 else 'end'}) "
                             f"{v[m].mean():.1f}/{rng[m].mean():.1f}")
            print(f"  {label:<10} {nm}: " + "  ".join(cells))
        # How much of the first bar's true range is the overnight gap itself.
        for nm, bars, day in (("30s", "b30", "day30"), ("15m", "b15", "day15")):
            b, d = D[bars], D[day]
            h, l, c = b["h"], b["l"], b["c"]
            prev = np.concatenate(([c[0]], c[:-1]))
            tr = np.maximum(h - l, np.maximum(np.abs(h - prev),
                                              np.abs(l - prev)))
            f0 = np.concatenate(([True], np.diff(d) != 0))
            print(f"  {label:<10} {nm} bar 0 of session: mean range "
                  f"{(h - l)[f0].mean():.1f} vs mean TRUE range "
                  f"{tr[f0].mean():.1f} (the difference IS the gap)")

    print("\n" + "=" * 100)
    print("SINGLE-BAR ARMING RATE  P(one 30s bar's own range >= x * ATR30)")
    print("The feasibility study's trap: a floor a lone wick clears is not a "
          "floor.")
    pb, im = frozen["pullback_min_atr30"], frozen["impulse_min_atr30"]
    for label, _ in wins:
        D = res[label]["D"]
        print(f"  {label:<10} pullback floor {PROVISIONAL['pullback_min_atr30']:.2f}"
              f" -> {bar_range_rate(D, PROVISIONAL['pullback_min_atr30']):6.1%}"
              f"   now {pb:.2f} -> {bar_range_rate(D, pb):6.1%}"
              f"   |  impulse floor {im:.2f} -> {bar_range_rate(D, im):6.1%}")
    print("\n  ...and IN SITU: how many bars after the leg extreme the hunt")
    print("  actually armed. The >=2-bar amendment makes lag 1 structurally")
    print("  impossible, so this is the shape of what is left:")
    p_fro = dict(PARAMS_DEFAULT, **frozen)
    for label, _ in wins:
        D = res[label]["D"]
        for nm, floor in ((f"provisional {PROVISIONAL['pullback_min_atr30']:.2f}",
                           PROVISIONAL["pullback_min_atr30"]),
                          (f"frozen {pb:.2f}", pb)):
            lag = leg_walk(D, p_fro, floors=(im, floor))["lag"]
            lag = lag[lag > 0]
            if not len(lag):
                continue
            hist = "  ".join(f"lag{v}: {np.mean(lag == v):5.1%}"
                             for v in (1, 2, 3, 4))
            print(f"    {label:<10} {nm:<18} armed {len(lag):5}  {hist}  "
                  f"lag>=5: {np.mean(lag >= 5):5.1%}  median {int(np.median(lag))}")

    print("\n" + "=" * 100)
    print("WHAT THE VALUES IMPLY (behaviour and stop DISTANCE, never P&L)")
    cands = [("provisional", dict(PARAMS_DEFAULT, **PROVISIONAL))]
    sb_recent = res[wins[0][0]]["out"]["stop_buffer_atr30"]["value"]
    cands.append((f"calibrated (buf {frozen['stop_buffer_atr30']:.2f}, full)",
                  dict(PARAMS_DEFAULT, **frozen)))
    if sb_recent != frozen["stop_buffer_atr30"]:
        cands.append((f"calibrated (buf {sb_recent:.2f}, recent)",
                      dict(PARAMS_DEFAULT, **dict(frozen,
                                                  stop_buffer_atr30=sb_recent))))
    for label, t in wins:
        D = res[label]["D"]
        ns = D["sessions"]
        d30 = D["day30"]
        srng = np.array([(D["b30"]["h"][d30 == u].max()
                          - D["b30"]["l"][d30 == u].min())
                         for u in np.unique(d30)])
        print(f"-- {label} sessions ({ns} in window, median session range "
              f"{np.median(srng):.0f} pts)")
        for nm, pv in cands:
            eps, f, risk = risk_envelope(t, pv)
            kinds = {}
            for e in eps:
                kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
            hw = pv["zone_width_atr15"] * D["atr15"].mean()
            print(f"   [{nm}]  zone half-width ~{hw:.0f} pts "
                  f"({2*hw/np.median(srng):.0%} of a session's range as a band)")
            print(f"     episodes {len(eps)} ({len(eps)/ns:.2f}/session)  "
                  + "  ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
            print(f"     fills {len(f)} ({len(f)/ns:.2f}/session)")
            # The research walk must be arming the SAME legs the strategy does,
            # or the percentiles describe a different animal. Exact equality is
            # not owed: `episodes` can also kill a leg on a spent attempt cap,
            # which frees it to arm another one the walk never sees.
            ep_legs = kinds.get("leg_died", 0) + kinds.get("no_attempt_left", 0)
            wk_legs = leg_walk(D, pv)["legs"]
            print(f"     legs armed: state machine {ep_legs}, research walk "
                  f"{wk_legs} ({wk_legs/max(ep_legs, 1):.1%} -- see note)")
            if len(risk):
                print("     risk/trade ticks: "
                      + " ".join(f"p{s}={pctl(risk, s):.0f}"
                                 for s in (10, 50, 90))
                      + f" max={risk.max():.0f}   $ (1 NQ): "
                      + " ".join(f"p{s}=${pctl(risk, s)*TICK_USD:,.0f}"
                                 for s in (10, 50, 90))
                      + f" max=${risk.max()*TICK_USD:,.0f}")
    print("\n(no P&L, win rate or expectancy is computed in this file "
          "-- by design)")


if __name__ == "__main__":
    main()
