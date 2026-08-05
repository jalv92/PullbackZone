#!/usr/bin/env python3
"""PullbackZone pattern core + PropSim plugin.

Spec: docs/specs/2026-08-05-pullbackzone-design.md. Parameter list is CLOSED
and mirrors ninjascript/PullbackZoneStrategy.cs one-to-one.

Sandbox: the loader injects `np` and `tp` (and Strategy/Param) rather than
letting a plugin import anything, but numpy imports are allowed anyway (the
injected `np` binding grants nothing an explicit import doesn't) as of
PropSim commit de0abdf. Passes `plugins.py --check` clean. The import exists
so this file runs its own selfchecks standalone.
"""
import numpy as np

# The PropSim sandbox hands a plugin `Strategy`, `Param` and `np` in its
# namespace instead of letting it import them. Standalone (running this file
# for its selfchecks, or from research/) they are simply absent, so stand-ins
# are defined and the class below degrades to a plain object. NameError rather
# than `try: from engine import ...` because plugins.py's AST check walks the
# WHOLE tree -- an import inside a try/except is still an import node, and is
# rejected exactly like a bare one (verified against plugins.ast_check).
try:
    Strategy
except NameError:
    class Strategy:                      # pragma: no cover - sandbox stand-in
        pass

    class Param:                         # pragma: no cover - sandbox stand-in
        def __init__(self, default, lo, hi, desc, fixed=False):
            self.default, self.lo, self.hi = default, lo, hi
            self.desc, self.fixed = desc, fixed

TICK = 0.25
_TPS = 10_000_000                      # .NET ticks per second
_NET_EPOCH_S = 62135596800             # seconds from 0001-01-01 to 1970-01-01

# The five CALIBRATED dials are FROZEN. `research/calibrate.py` picked each
# from a percentile of market BEHAVIOUR on the PropSim ALL tape -- 238 RTH
# sessions of real NQ ticks, 2025-08-03 .. 2026-08-04, ATR-warmup bars of each
# session excluded -- with no profit metric anywhere in the derivation. Every
# value below is the FULL-sample figure snapped to 0.05; the last-30-session
# window agreed within 1.33x on all five (the pre-registered gate was 2x).
# Re-running calibrate.py on the same tape reprints the same table.
#
# Changing one of these is a new pre-registered run, not a tweak. Never sweep
# them: a search would re-open the decision with the one criterion the
# calibration deliberately refused.
PARAMS_DEFAULT = dict(
    zone_pivot_k=3, zone_min_touches=2,
    # p60 of the nearest later bar approach to a live 15m pivot, ATR15s
    # (raw 0.30, 95% CI [0.26, 0.36], n=547): the half-width at which 60% of
    # pivot levels are touched at all.
    zone_width_atr15=0.30,
    zone_expiry_sessions=2, zone_break_atr15=0.25,
    # p40 of the max departure from a zone EDGE within 30 min of a touch
    # (raw 0.40, CI [0.38, 0.41], n=2836): below it departures chop back.
    leg_min_atr15=0.40,
    leg_timeout_min=60, max_attempts_per_leg=2,
    # p50 of the leg extension at pullbacks that went on to a NEW extreme
    # (raw 2.71, CI [2.56, 2.89], n=1633). Clears single-bar noise by a wide
    # margin: only 0.4% of 30s bars have a range this big on their own.
    impulse_min_atr30=2.70,
    # p30 of the retracement depth of those same continuation pullbacks
    # (raw 1.15, CI [1.10, 1.19]). KNOWN WEAKNESS, see docs/validation.md:
    # 66% of the hunts this floor arms are still armed by ONE bar of
    # counter-move. The floor is what the pre-registered rule returned; making
    # a pullback span 2+ bars is a spec change, not a calibration.
    pullback_min_atr30=1.15,
    use_engulfing=1, use_hammer=1, use_doji_star=1,
    entry_offset_ticks=2, entry_ttl_bars=6,
    # p80 of the adverse pierce past the trigger-time pullback extreme among
    # pullbacks that did continue (raw 1.28, CI [0.94, 1.58], n=412). 2.6x the
    # provisional -- Javier's "real NQ volatility, not token ticks", measured.
    stop_buffer_atr30=1.30,
    target_r=1.5, breakeven_at_r=0.0, be_offset_ticks=4,
    contracts=1, daily_loss_r=0.0, flatten_hhmm=1558,
)

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
    zone_break_atr15 * ATR15, or the first bar of the zone_expiry_sessions-th
    session after birth (day15[i] >= day15[born_i] + zone_expiry_sessions --
    sessions, not bars: the design spec's default is "2 sessions"); 10**9
    while alive.
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
    expiry = int(p["zone_expiry_sessions"])

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


# ATR period for the 30s side (impulse, pullback, stop buffer). Internal
# constant for the same reason as _ZONE_ATR_N15: the closed parameter list
# carries no ATR-period dial.
_ATR_N30 = 14

# A data-integrity guard, NOT a parameter -- the LatigoBreak rule (engine.py
# delta 4): a tunable that silently drops trades is a second strategy wearing
# the first one's name. A stop this wide can only come from a hole in the tape.
_SANITY_STOP_TICKS = 1200

_EMPTY4 = (np.array([], np.int64), np.array([], np.int8),
           np.array([]), np.array([]))


# --------------------------------------------------------------- rebar
# Local, numpy-only copies of tape.day_index / sec_of_day / build_bars. The
# sandbox injects neither, and `episodes` needs 30s AND 15m bars off the same
# ticks -- the engine hands a strategy exactly one bar size.
def _day_index(ts):
    return (ts // _TPS - _NET_EPOCH_S) // 86400


def _sec_of_day(ts):
    return (ts // _TPS - _NET_EPOCH_S) % 86400


def _build_bars(tape, secs):
    """OHLC per (day, time-slot), so no bar spans a session gap."""
    ts, px = tape["ts"], tape["px"]
    if not len(ts):
        z = np.array([])
        return dict(t=z, o=z, h=z, l=z, c=z, start=np.array([], np.int64),
                    end=np.array([], np.int64))
    slot = _day_index(ts) * (86400 // secs + 1) + _sec_of_day(ts) // secs
    starts = np.concatenate(([0], np.flatnonzero(np.diff(slot)) + 1))
    ends = np.concatenate((starts[1:], [len(ts)]))
    return dict(t=ts[starts], o=px[starts], c=px[ends - 1],
                h=np.maximum.reduceat(px, starts),
                l=np.minimum.reduceat(px, starts), start=starts, end=ends)


def _ep(kind, leg, a15, a30, trig_ts=-1, trig_kind=None, entry_stop=0.0,
        entry_tick=-1, stop_px=0.0, target_px=0.0):
    """One episode row. Terminal kinds carry no entry geometry (0.0/-1/None);
    only "filled" and "expired" do.

    `attempt` is capped at max_attempts_per_leg: a fill can only ever be
    attempt 1..max (the leg dies the moment the last one is consumed), so the
    cap binds on terminal rows alone, where it reads as the last attempt used
    rather than a phantom third one the contract does not allow."""
    z = leg["z"]
    return dict(kind=kind, dir=leg["dir"], zone_px=z["px"],
                zone_touches=z["touches"], leg_arm_ts=leg["arm_ts"],
                trig_ts=trig_ts, trig_kind=trig_kind,
                attempt=min(leg["fills"] + 1, leg["max_att"]),
                entry_stop_px=entry_stop, entry_tick=entry_tick,
                pull_ext_px=leg["pull"], stop_px=stop_px, target_px=target_px,
                atr30=a30, atr15=a15)


def _resolve_exit(ts, px, et, d, stop_px, target_px, flat_ts):
    """When does this fill's position go flat, and did it go flat at its stop?

    Pessimistic, as everywhere else: a tie goes to the stop. Bounded by the
    SESSION FLATTEN rather than by the leg's timeout, because a position
    outlives the leg that opened it and is managed by its brackets and the
    flatten backstop alone (spec 2, last bullet).

    Models the original stop, the target and the flatten ONLY: it omits the
    engine's breakeven stop, its 240-minute position horizon and its tape-gap
    exit, all of which exit EARLIER -- the safe direction for a gate that
    blocks new entries. Revisit before enabling `breakeven_at_r`: a breakeven
    exit would come back here labelled "stopped", granting an attempt 2 the
    spec does not.
    """
    seg = px[et + 1:int(np.searchsorted(ts, flat_ts, "right"))]
    if d > 0:
        s = np.flatnonzero(seg <= stop_px + 1e-9)
        t = np.flatnonzero(seg >= target_px - 1e-9)
    else:
        s = np.flatnonzero(seg >= stop_px - 1e-9)
        t = np.flatnonzero(seg <= target_px + 1e-9)
    si = int(s[0]) if len(s) else len(seg)
    ti = int(t[0]) if len(t) else len(seg)
    k = min(si, ti)
    if k == len(seg):
        return int(flat_ts), False          # neither bracket: flattened
    return int(ts[et + 1 + k]), si == k


def episodes(tape, p):
    """The whole state machine, as a research log: every leg the tape armed and
    what became of it. `entries()` is a filter over this, so a backtest and a
    dumped corpus can never describe two different strategies.

    One pass over CLOSED 30s bars. Zones come precomputed from 15m bars and are
    mapped onto 30s indices by CLOSE TIME, so a zone becomes usable at the 30s
    bar that closes with its confirming 15m bar and not one bar earlier. Ticks
    are read for one thing only: whether the resting stop entry filled.

    EVERY FIELD OF AN EPISODE IS FROZEN AT THE TRIGGER BAR'S CLOSE -- prices,
    both ATRs, the pullback extreme. The tick scan afterwards contributes the
    fill index and nothing else. That is the invariant the truncation selfcheck
    exists to hold: an earlier study on this same pattern had its entire
    apparent edge come from reading intrabar order at the fill.
    """
    ts, px = tape["ts"], tape["px"]
    b30 = _build_bars(tape, 30)
    b15 = _build_bars(tape, 900)
    n30, n15 = len(b30["c"]), len(b15["c"])
    if n30 < 2 or n15 < 2:
        return []
    zs = zones(b15, _day_index(b15["t"]), p)
    if not zs:
        return []

    o30, h30, l30, c30 = b30["o"], b30["h"], b30["l"], b30["c"]
    atr30 = wilder_atr(h30, l30, c30, _ATR_N30)
    atr15 = wilder_atr(b15["h"], b15["l"], b15["c"], _ZONE_ATR_N15)
    tc15, tc30 = ts[b15["end"] - 1], ts[b30["end"] - 1]
    # The last 15m bar CLOSED at or before this 30s bar's close. Equal times
    # are the same tick, so the 15m bar that ends there is already closed.
    j15 = np.searchsorted(tc15, tc30, "right") - 1
    sod, dayn = _sec_of_day(tc30), _day_index(tc30)

    for z in zs:
        z["i0"] = int(np.searchsorted(tc30, tc15[z["born_i"]], "left"))
        z["i1"] = (n30 if z["died_i"] >= n15
                   else int(np.searchsorted(tc30, tc15[z["died_i"]], "left")))
        z["touched"] = False
    zs.sort(key=lambda z: z["i0"])

    hh = int(p["flatten_hhmm"])
    cutoff = (hh // 100) * 3600 + (hh % 100) * 60
    ttl = int(p["entry_ttl_bars"])
    off = int(p["entry_offset_ticks"]) * TICK
    max_att = int(p["max_attempts_per_leg"])
    timeout = int(p["leg_timeout_min"] * 60 * _TPS)
    max_risk = _SANITY_STOP_TICKS * TICK

    # `busy`: the timestamp this strategy is next flat at. Global, across all
    # legs -- see the flat-to-flat note at the trigger.
    out, leg, live, zi, prev_day, busy = [], None, [], 0, -1, -1
    for i in range(n30):
        while zi < len(zs) and zs[zi]["i0"] <= i:
            live.append(zs[zi])
            zi += 1
        if live and any(i >= z["i1"] for z in live):
            live = [z for z in live if i < z["i1"]]
        # A touch does not survive the overnight gap. A zone outlives the
        # session (zone_expiry_sessions defaults to 2) but "price touched this
        # and then left" is an intraday observation -- carrying it across the
        # break would arm a leg on yesterday's touch and this morning's open.
        if dayn[i] != prev_day:
            prev_day = int(dayn[i])
            for z in zs:
                z["touched"] = False
        jj = int(j15[i])
        if jj < 0:
            continue
        a15, a30 = float(atr15[jj]), float(atr30[i])
        if not (a15 > 0 and a30 > 0):
            continue

        # --- leg death. Gates NEW entries only: a position opened by this leg
        # outlives it and belongs to its brackets (spec 2, last bullet).
        if leg is not None:
            d, z0 = leg["dir"], leg["z"]
            if leg["fills"] >= max_att:
                why = "attempts"
            elif dayn[i] != leg["day"] or sod[i] >= cutoff:
                why = "session"
            elif tc30[i] - leg["t0"] >= timeout:
                why = "timeout"
            elif abs(c30[i] - z0["px"]) <= z0["half_w"]:
                why = "reentry"
            elif any(z is not z0 and d * (z["px"] - z0["px"]) > 0
                     and (l30[i] <= z["px"] + z["half_w"] if d < 0
                          else h30[i] >= z["px"] - z["half_w"]) for z in live):
                why = "destination"
            else:
                why = None
            if why:
                out.append(_ep("no_attempt_left" if why == "attempts"
                               else "leg_died", leg, a15, a30))
                leg = None

        for z in live:
            if abs(c30[i] - z["px"]) <= z["half_w"]:
                z["touched"] = True

        # --- leg arming: a touched zone departed from by leg_min_atr15. One
        # leg at a time, as in NT8 where there is one state machine.
        if leg is None and sod[i] < cutoff:
            gap = p["leg_min_atr15"] * a15
            for z in live:
                if not z["touched"]:
                    continue
                if c30[i] > z["px"] + z["half_w"] + gap:
                    d = 1
                elif c30[i] < z["px"] - z["half_w"] - gap:
                    d = -1
                else:
                    continue
                z["touched"] = False        # this touch is spent on this leg
                leg = dict(z=z, dir=d, arm_px=float(c30[i]), ext=float(c30[i]),
                           pull=float(c30[i]), hunt=False, impulse=False,
                           fills=0, max_att=max_att, block=-1, t0=int(tc30[i]),
                           arm_ts=int(tc30[i]), day=int(dayn[i]))
                break
            continue        # extension is measured FROM the arming point

        if leg is None:
            continue
        d = leg["dir"]

        # --- extension and pullback. A bar that makes a new leg extreme
        # RESETS the pullback and does not also get credited with its own
        # opposite wick: inside one 30s bar the order of the high and the low
        # is unknown, and assuming the convenient one is the intrabar
        # lookahead this project exists to refuse.
        ext = float(h30[i] if d > 0 else l30[i])
        if d * (ext - leg["ext"]) > 0:
            leg["ext"] = leg["pull"] = ext
            leg["hunt"] = False
        else:
            cnt = float(l30[i] if d > 0 else h30[i])
            if d * (cnt - leg["pull"]) < 0:
                leg["pull"] = cnt
        if not leg["impulse"]:
            leg["impulse"] = (d * (leg["ext"] - leg["arm_px"])
                              >= p["impulse_min_atr30"] * a30)
        if leg["impulse"] and not leg["hunt"]:
            leg["hunt"] = (d * (leg["ext"] - leg["pull"])
                           >= p["pullback_min_atr30"] * a30)
        if not leg["hunt"] or i <= leg["block"]:
            continue        # `block`: this leg's own re-arm gate after a fill
                            # (see the attempt bookkeeping below). "One working
                            # entry at a time" is the global `busy` gate now.

        # --- trigger. Order is the spec's, and it is the tie-break when one
        # bar matches two: engulfing, then hammer, then doji.
        if p["use_engulfing"] and candle_engulfing(o30, h30, l30, c30, i, d):
            kind = "engulfing"
        elif p["use_hammer"] and candle_hammer(o30, h30, l30, c30, i, d):
            kind = "hammer"
        elif (p["use_doji_star"] and candle_doji(o30, h30, l30, c30, i)
              and abs((h30[i] if d < 0 else l30[i]) - leg["pull"]) < 1e-9):
            kind = "doji"                   # ...printed AT the pullback extreme
        else:
            continue
        # No entry whose working window could still be alive at the flatten.
        if sod[i] + ttl * 30 > cutoff:
            continue
        # FLAT TO FLAT, GLOBALLY: while any earlier fill's position is still
        # unresolved, or an entry order is still working, no leg may generate
        # an entry -- not even a different leg at a different zone. The engine
        # drops overlaps silently (resolve's `free_at`), which would make the
        # episode log and the backtest two different strategies; NT8 would be
        # worse still, where an opposite-direction Enter* while in position
        # REVERSES. Suppressed triggers get no episode row, same as the
        # flatten-window and sanity-guard skips above.
        t_trig = int(tc30[i])
        if t_trig < busy:
            continue

        entry_stop = float(h30[i] + off if d > 0 else l30[i] - off)
        stop_px = float(leg["pull"] - d * p["stop_buffer_atr30"] * a30)
        risk = abs(stop_px - entry_stop)
        if not 0 < risk <= max_risk:
            continue
        target_px = float(entry_stop + d * p["target_r"] * risk)

        # The entry works for entry_ttl_bars of WALL CLOCK, not that many bar
        # slots. Bar i+6 can be an hour later across a hole in the tape -- the
        # engine documents a 3,538-second jump on 2026-07-17 -- and an order
        # left working across one is not the order NT8 would have had.
        t_ttl = t_trig + ttl * 30 * _TPS
        a = int(np.searchsorted(ts, t_trig, "right"))
        b = int(np.searchsorted(ts, t_ttl, "right"))
        et = -1
        if b > a:
            seg = px[a:b]
            hit = (np.flatnonzero(seg >= entry_stop - 1e-9) if d > 0
                   else np.flatnonzero(seg <= entry_stop + 1e-9))
            if len(hit):
                et = a + int(hit[0])
        out.append(_ep("filled" if et >= 0 else "expired", leg, a15, a30,
                       trig_ts=t_trig, trig_kind=kind,
                       entry_stop=entry_stop, entry_tick=et, stop_px=stop_px,
                       target_px=target_px))
        if et < 0:
            busy = t_ttl               # the order worked its whole life
            continue
        # An attempt is consumed by a FILL, not by a trigger -- and spec 7
        # grants the second one only "if the first fill STOPS OUT". The same
        # resolution answers both questions, so it is done once: when did this
        # position close, and did it close at its stop?
        #
        #   busy until the exit  -> nobody enters while it is open (above);
        #   stopped out          -> THIS leg may hunt again from the next bar
        #                           to CLOSE after the stop print, with a fresh
        #                           trigger candle;
        #   target or flatten    -> no further attempt on this leg, ever.
        #
        # `block` carries the per-leg half because it already means "no trigger
        # at or before this bar" -- which is what makes the gate real. The
        # `hunt = False` that used to sit here was a no-op: the pullback
        # conditions still held, so the next bar re-armed it immediately.
        #
        # Not lookahead: re-arming at bar `rearm` reads only prints that had
        # already printed when that bar closed.
        leg["fills"] += 1
        exit_ts, stopped = _resolve_exit(
            ts, px, et, d, stop_px, target_px,
            (leg["day"] * 86400 + _NET_EPOCH_S + cutoff) * _TPS)
        busy = exit_ts
        leg["block"] = (int(np.searchsorted(tc30, exit_ts, "right")) - 1
                        if stopped else n30)

    if leg is not None:
        i = n30 - 1
        out.append(_ep("no_attempt_left" if leg["fills"] >= max_att
                       else "leg_died", leg, float(atr15[max(int(j15[i]), 0)]),
                       float(atr30[i])))
    return out


class PullbackZone(Strategy):
    """Pullback-continuation off a 15m S/R zone, triggered by a reversal candle
    and entered on a confirmation stop. Spec:
    docs/specs/2026-08-05-pullbackzone-design.md.

    PARAMETER NAMES ARE THE NT8 PROPERTY NAMES IN SNAKE_CASE AND THE LIST IS
    CLOSED, the LatigoBreak rule: an extra dial on one side silently makes the
    PropSim run and the Market Replay run two different experiments.

    `daily_loss_r` is inert here and it is not an oversight: NT8 enforces it,
    while entry generation is path-independent and cannot know a trade's
    outcome (LatigoBreak delta 3). It is 0/off at the frozen defaults;
    `compare_mirror.py` is what catches it if it ever is not.

    `be_offset_ticks` is the one public name for the breakeven offset, but
    `engine.backtest` reads `breakeven_offset_ticks` (engine.py:1242), so
    `entries` aliases it across rather than growing a second dial.
    """
    name, label = "pullback_zone", "PullbackZone (15m zone -> pullback -> reversal candle)"
    uses_ticks = True
    full_session = False                # RTH only, 09:30-16:00 ET

    params = {
        "zone_pivot_k": Param(3, 1, 10, "swing pivot lookback/forward, 15m bars",
                              fixed=True),
        "zone_min_touches": Param(2, 1, 5, "touches before a pivot is a zone",
                                  fixed=True),
        # The five CALIBRATE dials are fixed for the same reason a window is:
        # research/calibrate.py picks them from percentiles of market
        # behaviour, never from P&L. Sweeping them re-opens that decision with
        # the one criterion the calibration deliberately refused.
        "zone_width_atr15": Param(0.30, 0.05, 2.0, "zone half-width, ATR15s",
                                  fixed=True),
        "zone_expiry_sessions": Param(2, 1, 20, "sessions a zone survives",
                                      fixed=True),
        "zone_break_atr15": Param(0.25, 0.0, 2.0, "close beyond the far edge "
                                                  "that kills a zone, ATR15s"),
        "leg_min_atr15": Param(0.40, 0.1, 3.0, "departure from the zone edge "
                                               "that arms a leg, ATR15s",
                               fixed=True),
        "leg_timeout_min": Param(60, 5, 390, "a leg stops arming entries after "
                                             "this long, minutes", fixed=True),
        "max_attempts_per_leg": Param(2, 1, 5, "fills allowed per leg",
                                      fixed=True),
        "impulse_min_atr30": Param(2.70, 0.5, 8.0, "extension from the arming "
                                                   "point before a pullback "
                                                   "counts, ATR30s", fixed=True),
        "pullback_min_atr30": Param(1.15, 0.2, 5.0, "counter-move from the leg "
                                                    "extreme that arms the "
                                                    "hunt, ATR30s", fixed=True),
        "use_engulfing": Param(1, 0, 1, "engulfing trigger", fixed=True),
        "use_hammer": Param(1, 0, 1, "hammer / shooting-star trigger", fixed=True),
        "use_doji_star": Param(1, 0, 1, "doji-star trigger", fixed=True),
        "entry_offset_ticks": Param(2, 0, 20, "stop entry beyond the trigger "
                                              "candle's extreme, ticks", fixed=True),
        "entry_ttl_bars": Param(6, 1, 40, "working life of the entry, 30s bars",
                                fixed=True),
        "stop_buffer_atr30": Param(1.30, 0.05, 3.0, "stop beyond the pullback "
                                                    "extreme, ATR30s", fixed=True),
        "target_r": Param(1.5, 0.5, 6.0, "target as a multiple of risk"),
        "breakeven_at_r": Param(0.0, 0.0, 5.0, "move the stop to entry at this "
                                               "R; 0 = off", fixed=True),
        "be_offset_ticks": Param(4, 0, 40, "ticks past entry the breakeven stop "
                                           "sits", fixed=True),
        "contracts": Param(1, 1, 100, "position size, contracts", fixed=True),
        "daily_loss_r": Param(0.0, 0.0, 20.0, "stop for the day at this loss, R; "
                                              "0 = off (NT8 side)", fixed=True),
        "flatten_hhmm": Param(1558, 0, 2359, "session flatten, ET HHMM",
                              fixed=True),
    }

    def risk_ticks(self, p) -> float:
        return _SANITY_STOP_TICKS

    def entries(self, bars, tape, p):
        # `bars` is deliberately unused. This setup needs TWO series (30s and
        # 15m) off the same ticks and the engine builds exactly one, so both
        # are rebuilt in `episodes`. The consequence is worth knowing before
        # someone reads a sweep: the engine's --tf changes nothing here.
        #
        # The engine reads its breakeven offset under a name the closed list
        # does not carry; this is the only place to hand it over.
        p["breakeven_offset_ticks"] = p["be_offset_ticks"]
        eps = [e for e in episodes(tape, p) if e["kind"] == "filled"]
        if not eps:
            return _EMPTY4
        et = np.array([e["entry_tick"] for e in eps], np.int64)
        dr = np.array([e["dir"] for e in eps], np.int8)
        st = np.array([e["stop_px"] for e in eps])
        tg = np.array([e["target_px"] for e in eps])
        if p.get("breakeven_at_r", 0) > 0:
            # The engine's sixth array is a PRICE per trade, not a fraction
            # (engine.resolve: "the first tick to reach it moves the stop to
            # the entry fill"). R is measured off the signal's own risk, which
            # is why it is reconstructed here rather than passed as a ratio.
            be = np.array([e["entry_stop_px"] + e["dir"] * p["breakeven_at_r"]
                           * abs(e["stop_px"] - e["entry_stop_px"]) for e in eps])
            return et, dr, st, tg, None, be
        return et, dr, st, tg


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
              zone_break_atr15=0.25, zone_expiry_sessions=100)
    z = zones(dict(h=h, l=l, c=c), day, p)
    assert len(z) == 1, f"the 3rd pivot at the same price created a second zone: {z}"
    assert z[0]["born_i"] == 32, z[0]
    assert z[0]["died_i"] == 40, z[0]
    assert z[0]["touches"] == 2, z[0]

    # zone_expiry_sessions counts SESSIONS via day15, not bars (spec default
    # "2 sessions"): born on day 0 (bar 32), 3 sessions total, nothing else
    # kills it -- must die at the first bar of day 0 + 2 = day 2, well
    # before the (now moot) clean-break bar at 40 is even reached.
    day3 = np.concatenate([np.zeros(33, int), np.ones(6, int), np.full(6, 2, int)])
    p3 = dict(p, zone_expiry_sessions=2)
    z3 = zones(dict(h=h, l=l, c=c), day3, p3)
    assert z3[0]["born_i"] == 32, z3[0]
    assert z3[0]["died_i"] == 39, z3[0]                 # first bar of day 2
    print("zones OK")


def _fx_bars(leg_low=99.0, touch2=True, fast_depart=False, after="continue"):
    """The 30s bar path of the episode fixture: one RTH session containing a
    15m pivot high at exactly 110.0, two touches, a short leg, an impulse, a
    pullback, a shooting star and a fill.

    Blocks 0..16 are exactly 30 bars each, so a block index IS a 15m bar
    index; after the zone is born the layout stops caring.

    `after` picks what happens once attempt 1 has filled: "continue" runs the
    trade down and offers no second setup, "stopout" walks price back through
    attempt 1's stop and then offers a fresh trigger, "target" reaches attempt
    1's target first and then offers the same fresh trigger. The last two are
    the spec-7 gate: only "stopout" may produce an attempt 2.
    """
    b = []

    def q(x):
        return round(x / TICK) * TICK

    def one(o, h, l, c):
        b.append((q(o), q(h), q(l), q(c)))

    def flat(n, px, w=0.5):
        for _ in range(n):
            one(px, px + w, px - w, px)

    def ramp(n, a, z, w=0.25):
        for k in range(n):
            o = a + (z - a) * k / n
            c = a + (z - a) * (k + 1) / n
            one(o, max(o, c) + w, min(o, c) - w, c)

    def rise(a, z):
        """Bullish bars of body 0.5. No candle predicate can fire on one, so a
        pullback can be walked up without tripping a trigger."""
        x = a
        while x < z - 1e-9:
            one(x, x + 0.75, x - 0.25, x + 0.5)
            x += 0.5
        return x

    def star(top, fill=True):
        """A shooting star topping at `top`, preceded by a bullish bar whose
        body is as large (so engulfing cannot claim the star first), and
        optionally the bar that fills the entry stop on its OPENING print."""
        one(top - 1.0, top, top - 1.25, top - 0.5)
        one(top - 1.5, top, top - 2.25, top - 2.0)
        if fill:
            # The fill being the opening print is what gives the truncation
            # assert its teeth: cutting the tape one tick later leaves this bar
            # half-formed, so any field read from the bar the fill lands in
            # changes. Fill on a bar's third print and the cut lands past its
            # close, which is a check that cannot fail.
            one(top - 3.0, top - 2.75, top - 3.5, top - 3.25)

    def hammer(bot, fill=True):
        """`star` mirrored: a hammer bottoming at `bot` for a LONG leg. Keep
        `bot + 2.25` under the leg's running high, or the hammer sets a new leg
        extreme and resets the very pullback it is supposed to end."""
        one(bot + 1.0, bot + 1.25, bot, bot + 0.5)
        one(bot + 1.5, bot + 2.25, bot, bot + 2.0)
        if fill:
            one(bot + 3.0, bot + 3.5, bot + 2.75, bot + 3.25)

    for _ in range(6):                      # 0-5   flat warmup, seeds ATR15
        flat(30, 100.0)
    ramp(30, 100.0, 108.0)                  # 6     approach
    ramp(15, 108.0, 109.75)                 # 7     PIVOT HIGH: highs top at
    ramp(15, 109.75, 107.5)                 #       exactly 110.00 (w=0.25)
    for _ in range(3):                      # 8-10  bar 10 reveals the pivot
        flat(30, 107.0)
    ramp(15, 107.0, 109.75)                 # 11    TOUCH 1: wick to 110,
    ramp(15, 109.75, 106.5)                 #       close back below
    for _ in range(2):                      # 12-13
        flat(30, 106.5)
    if touch2:                              # 14    TOUCH 2 -> zone born
        ramp(15, 106.5, 109.75)
        ramp(15, 109.75, 106.5)
    else:
        flat(30, 106.5)
    ramp(10, 106.5, 109.75)                 # 15    retest AFTER birth: one 30s
    one(109.75, 110.0, 109.75, 110.0)       #       bar CLOSES at 110 (in band)
    if fast_depart:
        # One bar straight through the departure threshold, so the leg ARMS at
        # its own extreme and there is no impulse left to make. The marking
        # time afterwards keeps a REAL range: wickless bars have a TrueRange of
        # zero, which decays ATR30s toward nothing and drags the impulse
        # threshold down under even a 0.25-point "impulse".
        one(110.0, 110.0, 104.0, 104.0)     # 15
        flat(18, 104.0, w=0.25)
        flat(30, 104.0, w=0.25)             # 16    stands in for the impulse
    else:
        ramp(19, 110.0, 104.0)              # 15    departs -> leg arms
        # Wickless: a wick on a descending bar reaches back above the running
        # low, and at the frozen pullback_min (1.15 x ATR30s) one such wick is
        # still enough to arm the hunt mid-impulse -- calibration measured that
        # weakness rather than removing it (66% of armed hunts arm on a single
        # bar), so the fixture must keep dodging it to isolate what it tests.
        ramp(30, 104.0, leg_low, w=0.0)     # 16    impulse
    lvl = rise(leg_low, leg_low + 2.0)      # 17+   pullback
    star(lvl + 1.0)                         # ATTEMPT 1: trigger + fill
    bot = lvl - 2.25                        # the fill bar's close

    if after == "continue":
        ramp(20, bot, lvl - 5.0, w=0.0)     # runs on down, no second setup
    elif after == "stopout":
        # Back up through attempt 1's stop (pull extreme 102.00 + half an
        # ATR30s, so ~102.3), but NOT before offering a trigger-shaped candle
        # that the gate must ignore because the stop has not been hit yet.
        star(rise(bot, lvl - 0.25) + 1.0, fill=False)    # tops ~101.75: IGNORED
        star(rise(lvl - 1.25, lvl + 3.0) + 1.0)          # tops ~105: ATTEMPT 2
    elif after == "target":
        # Attempt 1's target (~94.7) comes first, so the identical trigger
        # below must produce nothing at all.
        ramp(12, bot, lvl - 8.0, w=0.0)
        star(rise(lvl - 8.0, lvl - 4.0) + 1.0)
    elif after == "crossleg":
        # A SECOND zone, and a leg at it that overlaps leg A's open position.
        # Leg A is short from 99.25 with its stop at 102.31 and its target at
        # 94.66, so everything below stays inside that band until the release
        # is wanted -- otherwise A resolves early and there is nothing to
        # overlap with. Leg A itself times out at bar 585, long before leg B
        # arms, because only one leg runs at a time.
        ramp(13, bot, 95.5, w=0.0)          # 17    dips to 95.5: PIVOT LOW
        ramp(10, 95.5, 97.5, w=0.0)         #       back up inside the block
        flat(90, 98.0, w=0.25)              # 18-20 lows 97.75; reveals at 20
        for _ in range(2):                  # 21-22 two touches -> zone B born
            ramp(10, 98.0, 95.5, w=0.0)
            ramp(20, 95.5, 98.0, w=0.0)
        ramp(10, 98.0, 95.5, w=0.0)         # 23    a 30s CLOSE inside the band
        top = rise(95.5, 101.5)             #       departs -> LEG B arms long
        hammer(98.25)                       # trigger while A is open: DROPPED
        rise(top + 0.25, 103.0)             # through 102.31: A stops, gate opens
        hammer(101.0)                       # ...and now the same setup FILLS
    return b


def _fx_tape(bars30, sod0=9 * 3600 + 30 * 60, day0=20000, split_at=None):
    """Four prints per 30s bar -- open, both extremes in path order, close.

    `split_at` restarts the clock on the NEXT session at that bar index, which
    is how a fixture puts a zone touch and the departure from it on opposite
    sides of an overnight gap."""
    ts, px = [], []
    base = (int(day0) * 86400 + _NET_EPOCH_S + int(sod0)) * _TPS
    nxt = base + 86400 * _TPS
    for k, (o, h, l, c) in enumerate(bars30):
        t0 = (base + k * 30 * _TPS if split_at is None or k < split_at
              else nxt + (k - split_at) * 30 * _TPS)
        mid = (h, l) if c < o else (l, h)
        for dt, v in zip((0, 7, 14, 21), (o, mid[0], mid[1], c)):
            ts.append(t0 + dt * _TPS)
            px.append(v)
    n = len(ts)
    return dict(ts=np.array(ts, np.int64), px=np.array(px, np.float64),
                vol=np.ones(n, np.int64), side=np.zeros(n, np.int8))


def _selfcheck_episodes():
    t = _fx_tape(_fx_bars())
    eps = episodes(t, PARAMS_DEFAULT)
    filled = [e for e in eps if e["kind"] == "filled"]
    assert len(filled) == 1, [e["kind"] for e in eps]
    e = filled[0]
    assert e["dir"] == -1 and e["trig_kind"] == "hammer", e
    assert abs(e["zone_px"] - 110.0) < 1e-6 and e["attempt"] == 1, e
    assert e["stop_px"] > e["entry_stop_px"] > e["target_px"], e
    # stop = pullback extreme + buffer:
    assert abs(e["stop_px"] - (e["pull_ext_px"]
                               + PARAMS_DEFAULT["stop_buffer_atr30"]
                               * e["atr30"])) < 1e-6, e
    # no-lookahead invariant (the 10f discipline): truncate the tape one tick
    # after the fill -> same stop/target on the filled episode. It BITES:
    # every field above is frozen at the trigger bar's close, so anything
    # reading the bar the fill lands in (a forming-bar ATR, a pullback extreme
    # extended past the trigger) changes here and nowhere else.
    t2 = {k: v[: e["entry_tick"] + 2] for k, v in t.items()}
    e2 = [x for x in episodes(t2, PARAMS_DEFAULT) if x["kind"] == "filled"][0]
    assert (e2["stop_px"], e2["target_px"]) == (e["stop_px"], e["target_px"])
    print("episodes OK")


def _selfcheck_episodes_negative():
    """Three mutations of the fixture, each isolating one gate. Every one of
    them pairs "nothing fired" with a control that fires, because a check that
    only ever asserts an empty list passes just as happily when the fixture
    stopped producing a setup at all."""
    p = PARAMS_DEFAULT

    # (a) no second touch -> the pivot never becomes a zone, so nothing arms.
    # The retest in block 15 does supply a second 15m touch, but it lands in
    # the same bar the zone is born in and a zone is only touchable from its
    # birth bar on -- so no 30s close is ever inside the band.
    assert episodes(_fx_tape(_fx_bars(touch2=False)), p) == []

    # (b) the leg arms at its own extreme -> the impulse gate refuses it. The
    # leg is there (it died, so it lived), and dropping ONLY impulse_min_atr30
    # brings the very same trigger back, which is what makes this a test of the
    # impulse gate rather than of the fixture.
    eps = episodes(_fx_tape(_fx_bars(leg_low=104.0, fast_depart=True)), p)
    assert [e for e in eps if e["kind"] == "leg_died"], eps
    assert not [e for e in eps if e["kind"] in ("filled", "expired")], eps
    loose = dict(p, impulse_min_atr30=0.05)
    assert [e for e in episodes(_fx_tape(_fx_bars(leg_low=104.0,
                                                  fast_depart=True)), loose)
            if e["kind"] == "filled"]

    # (c) the same session shifted so the trigger lands past flatten_hhmm. The
    # shift is a whole number of 15m bars -- shift by anything else and the 15m
    # grid moves under the fixture, which is a different tape, not a later one.
    base = _fx_bars()
    e = [x for x in episodes(_fx_tape(base), p) if x["kind"] == "filled"][0]
    trig_sod = int(_sec_of_day(np.array([e["trig_ts"]], np.int64))[0])
    hh = int(p["flatten_hhmm"])
    cutoff = (hh // 100) * 3600 + (hh % 100) * 60
    late = _fx_tape(base, sod0=9 * 3600 + 30 * 60
                    + 900 * ((cutoff - trig_sod) // 900 + 2))
    assert not [x for x in episodes(late, p) if x["kind"] == "filled"]
    # ...and it is the flatten that refused it, not the tape: same ticks, one
    # parameter moved.
    assert [x for x in episodes(late, dict(p, flatten_hhmm=2359))
            if x["kind"] == "filled"]

    # (d) a touch does not cross the overnight gap. Split the session between
    # the retest and the departure: same bars, same order, no leg.
    bars = _fx_bars()
    assert episodes(_fx_tape(bars, split_at=463), p) == []
    assert [x for x in episodes(_fx_tape(bars), p) if x["kind"] == "filled"]
    print("episodes (negative) OK")


def _selfcheck_attempt_gate():
    """Spec 7: the second attempt exists only if the first fill STOPS OUT."""
    p = PARAMS_DEFAULT

    t = _fx_tape(_fx_bars(after="stopout"))
    eps = episodes(t, p)
    a1 = [e for e in eps if e["kind"] == "filled" and e["attempt"] == 1]
    a2 = [e for e in eps if e["kind"] == "filled" and e["attempt"] == 2]
    assert len(a1) == 1 and len(a2) == 1, [(e["kind"], e["attempt"]) for e in eps]
    # The trigger-shaped candle printed BEFORE the stop-out produced nothing:
    # two entry rows in total, not three.
    assert len([e for e in eps if e["kind"] in ("filled", "expired")]) == 2, eps
    seg = t["px"][a1[0]["entry_tick"] + 1:]
    k = int(np.flatnonzero(seg >= a1[0]["stop_px"] - 1e-9)[0])
    stop_ts = int(t["ts"][a1[0]["entry_tick"] + 1 + k])
    assert a2[0]["trig_ts"] > stop_ts, (a2[0]["trig_ts"], stop_ts)
    # ...and the leg then dies of exhausted attempts, not of anything else.
    assert [e for e in eps if e["kind"] == "no_attempt_left"], eps

    # Target first -> no attempt 2, despite an identical trigger-shaped candle.
    eps = episodes(_fx_tape(_fx_bars(after="target")), p)
    assert len([e for e in eps if e["kind"] == "filled"]) == 1, eps
    assert not [e for e in eps if e["attempt"] == 2
                and e["kind"] in ("filled", "expired")], eps
    print("attempt gate OK")


def _selfcheck_cross_leg_gate():
    """Flat to flat, GLOBALLY: a second leg at a second zone may not enter
    while an earlier leg's position is still open, and may once it is not."""
    p = PARAMS_DEFAULT
    t = _fx_tape(_fx_bars(after="crossleg"))
    eps = episodes(t, p)
    f = [e for e in eps if e["kind"] == "filled"]
    assert len(f) == 2, [(e["kind"], e["zone_px"], e["dir"]) for e in eps]
    a, bl = f
    assert (a["zone_px"], a["dir"]) == (110.0, -1), a
    assert (bl["zone_px"], bl["dir"]) == (95.5, 1), bl    # a DIFFERENT leg
    # When does A go flat? At its stop -- price never reaches its target here.
    seg = t["px"][a["entry_tick"] + 1:]
    a_exit = int(t["ts"][a["entry_tick"] + 1
                         + int(np.flatnonzero(seg >= a["stop_px"] - 1e-9)[0])])
    # Nothing at all is generated in between, by either leg. The fixture does
    # print a tradeable leg-B trigger in that window (entry stop 101.00, and a
    # bar opens at 101.25 inside its TTL) -- it produces no row because of the
    # gate, not because there was nothing to suppress.
    assert not [e for e in eps if e["kind"] in ("filled", "expired")
                and a["trig_ts"] < e["trig_ts"] < a_exit], eps
    assert bl["trig_ts"] > a_exit, (bl["trig_ts"], a_exit)
    print("cross-leg gate OK")


def _selfcheck_ttl_wall_clock():
    """entry_ttl_bars is wall clock: a hole in the tape must expire the entry,
    not carry it to whatever bar index happens to be six slots later."""
    p = PARAMS_DEFAULT
    bars = _fx_bars()
    t = _fx_tape(bars)
    e = [x for x in episodes(t, p) if x["kind"] == "filled"][0]
    # Open an hour-wide hole immediately before the fill. Same prints, same
    # prices, same bar ORDER -- only the clock moves, so an entry bounded by
    # bar indices still fills and one bounded by wall clock cannot.
    hole = dict(t)
    hole["ts"] = t["ts"].copy()
    hole["ts"][e["entry_tick"]:] += 3600 * _TPS
    got = [x for x in episodes(hole, p) if x["trig_ts"] == e["trig_ts"]]
    assert got and got[0]["kind"] == "expired", got
    print("ttl wall clock OK")


def _selfcheck_strategy():
    """The engine's contract, checked here rather than discovered by
    plugins.check_output on a real tape."""
    s = PullbackZone()
    assert s.risk_ticks(PARAMS_DEFAULT) == _SANITY_STOP_TICKS
    for k in s.params:                      # the closed list, both directions
        assert k in PARAMS_DEFAULT, k
    for k, v in PARAMS_DEFAULT.items():
        assert k in s.params, k
        assert s.params[k].lo <= v <= s.params[k].hi, k
        assert s.params[k].default == v, k

    # A COPY: entries() writes the breakeven-offset alias into the dict it is
    # handed (the only channel the engine reads it on), and PARAMS_DEFAULT
    # is the module's source of truth for the closed list -- letting a run
    # grow a key in it would make the round-trip above pass or fail depending
    # on what ran first.
    t = _fx_tape(_fx_bars())
    pp = dict(PARAMS_DEFAULT)
    res = s.entries(None, t, pp)
    assert pp["breakeven_offset_ticks"] == pp["be_offset_ticks"]
    assert len(res) == 4
    et, dr, st, tg = res
    assert et.dtype == np.int64 and dr.dtype == np.int8
    assert len(et) == len(dr) == len(st) == len(tg) == 1
    assert 0 <= et[0] < len(t["ts"])
    fill = t["px"][et[0]]                   # a short's stop sits ABOVE its fill
    assert st[0] > fill > tg[0], (st[0], fill, tg[0])

    # Breakeven returns the engine's 6-tuple, and the sixth array is a PRICE
    # between the entry and the target -- not a fraction.
    et2, dr2, st2, tg2, lim, be = s.entries(None, t, dict(PARAMS_DEFAULT,
                                                          breakeven_at_r=1.0))
    assert lim is None and len(be) == len(et2)
    ep = [x for x in episodes(t, PARAMS_DEFAULT) if x["kind"] == "filled"][0]
    risk = abs(ep["stop_px"] - ep["entry_stop_px"])
    assert abs(be[0] - (ep["entry_stop_px"] - 1.0 * risk)) < 1e-9, be[0]
    assert tg2[0] < be[0] < st2[0], be[0]
    print("strategy OK")


if __name__ == "__main__":
    _selfcheck_candles()
    _selfcheck_atr_pivots()
    _selfcheck_zones()
    _selfcheck_episodes()
    _selfcheck_episodes_negative()
    _selfcheck_attempt_gate()
    _selfcheck_cross_leg_gate()
    _selfcheck_ttl_wall_clock()
    _selfcheck_strategy()
