#!/usr/bin/env python3
"""PullbackZone pattern core + PropSim plugin.

Spec: docs/specs/2026-08-05-pullbackzone-design.md. Parameter list is CLOSED
and mirrors ninjascript/PullbackZoneStrategy.cs one-to-one.
Sandbox rule: imports limited to math/numpy so plugins.py --check passes.
"""
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


def wilder_atr(h, l, c, n):
    """Wilder ATR over closed bars -- NinjaTrader's TrueRange/ATR recursion
    exactly (house rule, copied from PropSim's `engine._atr_wilder`, itself
    `LatigoBreakStrategy.cs:1009-1021`): a plain mean of the true ranges
    seen so far until n bars exist, then `atr += (tr - atr) / n`. Defined
    from bar 0, no NaN warmup.

    No session reset: TrueRange reaches across the boundary, same as NT8's
    own recursion. Deliberate, not an oversight -- unlike LatigoBreak (where
    the reset never mattered), PullbackZone's 15m ATR window spans the
    09:30 open every session, so this is the one convention both mirror
    sides already reproduce exactly; a hand-rolled session reset would have
    to be implemented twice and is exactly where mirrors diverge.
    """
    prev = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    tr[0] = h[0] - l[0]                 # no previous close to reach for
    out = np.empty(len(tr))
    run = 0.0
    for i in range(len(tr)):
        if i < n:
            run = (run * i + tr[i]) / (i + 1)
        else:
            run += (tr[i] - run) / n
        out[i] = run
    return out


def pivots(h, l, k):
    """Strict-unique swing pivots. A pivot at bar j is usable from j+k on."""
    n = len(h)
    hi, lo = [], []
    for j in range(k, n - k):
        win_h = h[j - k:j + k + 1]
        hi_max = win_h.max()
        if h[j] == hi_max and (win_h == hi_max).sum() == 1:
            hi.append(j)
        win_l = l[j - k:j + k + 1]
        lo_min = win_l.min()
        if l[j] == lo_min and (win_l == lo_min).sum() == 1:
            lo.append(j)
    return np.array(hi, dtype=np.int64), np.array(lo, dtype=np.int64)


# ATR period for the zone band width / break threshold. Not one of the
# closed mirrored parameters (docs/specs/2026-08-05-pullbackzone-design.md
# lists no ATR-period dial for zones) -- an internal constant, same status
# as the candle proportions above.
_ZONE_ATR_N15 = 14


def zones(b15, day15, p):
    """15m S/R zones. Returns dicts usable point-in-time via born_i/died_i.

    born_i: the 15m bar index at whose CLOSE the zone becomes usable (touch
    #zone_min_touches confirmed; the pivot itself was already confirmed
    zone_pivot_k bars earlier).
    died_i: first 15m bar whose close crosses the far edge by more than
    zone_break_atr15 * ATR15, or the first bar of the zone_expiry-th session
    after birth (day15[i] >= day15[born_i] + zone_expiry -- sessions, not
    bars: the design spec's default is "2 sessions"); 10**9 while alive.
    A new pivot within one band-width of a live zone merges into it (the old
    zone keeps its identity and touch count).

    Half-width is frozen at the ATR15 read when the pivot is first revealed
    (bar j+k) -- a zone is drawn as a fixed box on the NT8 chart, so its
    edges cannot drift with every later bar's ATR.
    """
    h, l, c = b15["h"], b15["l"], b15["c"]
    n = len(c)
    k = int(p["zone_pivot_k"])
    min_touches = int(p["zone_min_touches"])
    width_atr = float(p["zone_width_atr15"])
    break_atr = float(p["zone_break_atr15"])
    expiry = int(p["zone_expiry"])

    atr15 = wilder_atr(h, l, c, _ZONE_ATR_N15)
    hi_idx, lo_idx = pivots(h, l, k)
    reveal = {}
    for j in hi_idx:
        reveal.setdefault(int(j) + k, []).append((float(h[j]), True))
    for j in lo_idx:
        reveal.setdefault(int(j) + k, []).append((float(l[j]), False))

    cands = []   # pivots armed, accumulating touches, not yet a zone
    out = []     # promoted (born) zones, the function's return value

    for i in range(n):
        a = atr15[i]
        finite_a = np.isfinite(a) and a > 0

        if finite_a:
            for px, is_high in reveal.get(i, []):
                hw = width_atr * a
                if any(z["died_i"] == 10**9 and abs(z["px"] - px) < hw + z["half_w"]
                       for z in out):
                    continue
                if any(abs(cd["px"] - px) < hw + cd["half_w"] for cd in cands):
                    continue
                cands.append(dict(px=px, half_w=hw, touches=0, pivot_high=is_high))

        if not finite_a:
            continue

        for cd in list(cands):
            lo_edge, hi_edge = cd["px"] - cd["half_w"], cd["px"] + cd["half_w"]
            touched = ((h[i] >= lo_edge and c[i] < lo_edge) if cd["pivot_high"]
                       else (l[i] <= hi_edge and c[i] > hi_edge))
            if not touched:
                continue
            cd["touches"] += 1
            if cd["touches"] >= min_touches:
                out.append(dict(px=cd["px"], half_w=cd["half_w"], born_i=i,
                                 died_i=10**9, touches=cd["touches"],
                                 pivot_high=cd["pivot_high"]))
                cands.remove(cd)

        for z in out:
            if z["died_i"] != 10**9:
                continue
            lo_edge, hi_edge = z["px"] - z["half_w"], z["px"] + z["half_w"]
            broke = ((c[i] > hi_edge + break_atr * a) if z["pivot_high"]
                      else (c[i] < lo_edge - break_atr * a))
            if broke or (day15[i] >= day15[z["born_i"]] + expiry):
                z["died_i"] = i

    return out


# ---------------------------------------------------------------- selfcheck
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


def _selfcheck_atr_pivots():
    n = 20
    h = np.full(n, 101.0); l = np.full(n, 100.0); c = np.full(n, 100.5)
    h[10:] = 91.0; l[10:] = 90.0; c[10:] = 90.5
    atr = wilder_atr(h, l, c, 5)
    assert abs(atr[9] - 1.0) < 1e-9                     # steady 1-pt bars
    assert atr[10] > 2.0                                # the gap DOES hit TR once
    assert 1.0 < atr[16] < atr[10]                      # ...and decays afterwards
    h2 = h.copy(); h2[13] = 95.0
    assert wilder_atr(h2, l, c, 5)[16] > atr[16]        # intra-session jump registers
    hh = np.array([1, 2, 5, 2, 1, 5, 5, 1, 2.0])
    ll = hh - 1
    hi, lo = pivots(hh, ll, 2)
    assert list(hi) == [2]                              # bar 5 ties with 6 -> rejected
    print("atr/pivots OK")


def _selfcheck_zones():
    n = 45
    h = np.full(n, 104.0); l = np.full(n, 103.0); c = np.full(n, 103.5)
    day = np.zeros(n, dtype=int)
    h[0:18] = 100.5; l[0:18] = 99.5; c[0:18] = 100.0    # flat warmup, seeds ATR(14)
    h[20] = 110.0; l[20] = 109.0; c[20] = 109.5          # pivot high (k=2 -> usable at 22)
    h[26] = 109.5; l[26] = 108.0; c[26] = 108.5          # touch 1: wicks in, closes back out
    h[32] = 109.5; l[32] = 108.0; c[32] = 108.5          # touch 2 -> born here
    h[34] = 110.2; l[34] = 109.5; c[34] = 109.8          # 3rd pivot, same price -> must merge
    h[40] = 112.0; l[40] = 111.3; c[40] = 111.9          # close clears the far edge -> dies here

    p = dict(zone_pivot_k=2, zone_min_touches=2, zone_width_atr15=0.5,
              zone_break_atr15=0.25, zone_expiry=100)
    z = zones(dict(h=h, l=l, c=c), day, p)
    assert len(z) == 1, f"the 3rd pivot at the same price created a second zone: {z}"
    assert z[0]["born_i"] == 32, z[0]
    assert z[0]["died_i"] == 40, z[0]
    assert z[0]["touches"] == 2, z[0]

    # zone_expiry counts SESSIONS via day15, not bars (spec default "2
    # sessions"): born on day 0 (bar 32), 3 sessions total, nothing else
    # kills it -- must die at the first bar of day 0 + 2 = day 2, well
    # before the (now moot) clean-break bar at 40 is even reached.
    day3 = np.concatenate([np.zeros(33, int), np.ones(6, int), np.full(6, 2, int)])
    p3 = dict(p, zone_expiry=2)
    z3 = zones(dict(h=h, l=l, c=c), day3, p3)
    assert z3[0]["born_i"] == 32, z3[0]
    assert z3[0]["died_i"] == 39, z3[0]                 # first bar of day 2
    print("zones OK")


if __name__ == "__main__":
    _selfcheck_candles()
    _selfcheck_atr_pivots()
    _selfcheck_zones()
