#!/usr/bin/env python3
"""PullbackZone V1 mirror gate — join the NT8 Playback corpus against the
PropSim episode corpus (research/dump_episodes.py) and decide PASS/FAIL.

THE ARBITER. No backtest number is believed until this passes (plan's "no
number is believed" rule). Every classification bucket below cites the plan
delta it encodes — see docs/plans/2026-08-05-pullbackzone.md, "Known mirror
deltas" 1-14.

Stdlib json ONLY: .NET tick ints exceed 2**53 (a JS-based tool would
silently round them); Python's arbitrary-precision ints round-trip exactly
through json — _assert_int_roundtrip() is the proof, not a formality.
"""
import argparse
import json
import sys
from pathlib import Path

TICK = 0.25
_TPS = 10_000_000                # .NET ticks per second
RTH_START_S = 9 * 3600 + 30 * 60  # 09:30
RTH_END_S = 16 * 3600             # 16:00
TOL_S = 30.0 + 1e-6              # delta 8: INCLUSIVE 30s join window

# Vocabulary (plan delta 10 retired the detection-only "trigger" kind; the
# joiner never special-cases it — an unknown kind is a sanity-gate reject).
PS_KINDS = {"filled", "expired", "leg_died", "no_attempt_left"}
NT_KINDS = PS_KINDS | {"exit"}    # "exit" is NT8-only (file header); ignored
                                   # by the episode join, used only for the
                                   # exit-price side-channel below.


class SanityGateError(Exception):
    pass


def _sec_of_day(ts):
    # NET_EPOCH_S (0001-01-01 -> 1970-01-01) is an exact multiple of 86400,
    # so it drops out of a mod-86400 read entirely — no need to carry it.
    return (ts // _TPS) % 86400


def _assert_int_roundtrip():
    big = 638_540_123_456_789_012          # > 2**53, a real .NET-tick-sized int
    assert json.loads(json.dumps(big)) == big
    assert isinstance(json.loads(json.dumps(big)), int)


def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def dedup_nt8(rows):
    """Rewind fence: keep only rows whose epoch equals the max epoch seen for
    their (date, trig_ts) group. A stale replayed pass is superseded whole —
    ties (same run, e.g. a "filled" + its "exit" sharing one trig_ts) all
    keep the max epoch together and all survive."""
    best = {}
    for r in rows:
        key = (r.get("date"), r.get("trig_ts"))
        e = r.get("epoch", 0)
        if key not in best or e > best[key]:
            best[key] = e
    return [r for r in rows if r.get("epoch", 0) == best[(r.get("date"), r.get("trig_ts"))]]


def sanity_check(nt8_rows, ps_rows):
    if not nt8_rows or not ps_rows:
        raise SanityGateError(f"empty corpus: nt8={len(nt8_rows)} rows, propsim={len(ps_rows)} rows")
    for r in ps_rows:
        if r.get("kind") not in PS_KINDS:
            raise SanityGateError(f"propsim: unknown kind {r.get('kind')!r}")
    for r in nt8_rows:
        if r.get("kind") not in NT_KINDS:
            raise SanityGateError(f"nt8: unknown kind {r.get('kind')!r}")
    for label, rows in (("propsim", ps_rows), ("nt8", nt8_rows)):
        for r in rows:
            ts = r.get("trig_ts", -1)
            if ts is None or ts < 0:
                ts = r.get("leg_arm_ts", -1)          # terminal rows carry no trigger
            if ts is None or ts < 0:
                continue
            sod = _sec_of_day(ts)
            if not (RTH_START_S <= sod <= RTH_END_S):
                raise SanityGateError(
                    f"{label}: a trigger/arm timestamp lands at {sod}s of day "
                    f"(outside 09:30-16:00 ET) — looks like a time-zone "
                    f"misconfig, not a real RTH print"
                )


def _bucket_key(r):
    return (r.get("date"), r.get("dir"), round(r.get("zone_px", 0.0) / TICK))


def _tick_delta(a, b):
    return abs(a - b) / TICK


def _greedy_match(a_rows, b_rows, ts_key, tol_s=TOL_S):
    """Pair rows from a (propsim) / b (nt8) sharing a (date, dir, zone) bucket,
    nearest ts_key first, one-to-one. Returns (pairs, leftover_a, leftover_b)."""
    buckets_a, buckets_b = {}, {}
    for r in a_rows:
        buckets_a.setdefault(_bucket_key(r), []).append(r)
    for r in b_rows:
        buckets_b.setdefault(_bucket_key(r), []).append(r)

    pairs, left_a, left_b = [], [], []
    for key in set(buckets_a) | set(buckets_b):
        ra, rb = buckets_a.get(key, []), buckets_b.get(key, [])
        cands = []
        for i, x in enumerate(ra):
            tx = x.get(ts_key)
            if tx is None or tx < 0:
                continue
            for j, y in enumerate(rb):
                ty = y.get(ts_key)
                if ty is None or ty < 0:
                    continue
                dt = abs(tx - ty) / _TPS
                if dt <= tol_s:
                    cands.append((dt, i, j))
        cands.sort(key=lambda c: c[0])
        used_a, used_b = set(), set()
        for dt, i, j in cands:
            if i in used_a or j in used_b:
                continue
            used_a.add(i)
            used_b.add(j)
            pairs.append((ra[i], rb[j], dt))
        left_a += [x for i, x in enumerate(ra) if i not in used_a]
        left_b += [y for j, y in enumerate(rb) if j not in used_b]
    return pairs, left_a, left_b


def classify_pair(ps, nt):
    """Trigger-anchored pairs (kind in filled/expired on both sides)."""
    pk, nk = ps["kind"], nt["kind"]
    if pk == "filled" and nk == "filled":
        deltas = {
            "entry": _tick_delta(ps["entry_stop_px"], nt["entry_stop_px"]),
            "stop": _tick_delta(ps["stop_px"], nt["stop_px"]),
            "target": _tick_delta(ps["target_px"], nt["target_px"]),
        }
        if all(v <= 1.0 + 1e-6 for v in deltas.values()):
            return "MATCHED", deltas
        return "UNEXPLAINED_PRICE", deltas
    if pk == "filled" and nk == "expired":
        # delta 5: NT8 living forward can time out one bar earlier/later than
        # PropSim's tape-order resolution at the exact TTL boundary.
        if nt.get("reason") == "ttl":
            return "DELTA5_TTL_JITTER", None
        # delta 11 (conservative half): NT8 cancelled the working entry when
        # its leg died / the flatten fired / the leg was superseded, before
        # the print PropSim (resolving inside the trigger bar) already saw.
        return "DELTA11_EARLY_CANCEL", None
    if pk == "expired" and nk == "expired":
        return "MATCHED_NO_FILL", None
    # PropSim never saw the print reach entry, but NT8 filled — no delta
    # citation runs this direction; a real problem.
    return "UNEXPLAINED", None


def _delta12_hit(ps_row, pairs1, exit_by_trig):
    """PropSim's attempt-2 episode with no NT8 counterpart, where attempt 1's
    NT8 fill was closed by hand or the session flatten. Spec 7 grants attempt
    2 only after a real stop-out; PropSim's tape-only _resolve_exit cannot see
    a manual close, so it grants one PropSim never should have (delta 12).

    Same schema limit as DELTA7/DELTA11: bucket-key matching carries no real
    leg identity, only (date, dir, zone). Not fixable in the joiner alone —
    would need a leg id in both corpora (tracked in the plan's delta ledger,
    not this file's job to invent)."""
    key = _bucket_key(ps_row)
    for ps1, nt1, _ in pairs1:
        if ps1.get("attempt") == 1 and nt1["kind"] == "filled" and _bucket_key(ps1) == key:
            ex = exit_by_trig.get(nt1.get("trig_ts"))
            if ex and ex.get("reason") in ("manual", "flatten"):
                return True
    return False


# entry_ttl_bars=6 (PARAMS_DEFAULT, frozen) * 30s = 180s: PropSim's OWN busy
# window (pullback_zone.py:544, `t_ttl = t_trig + ttl*30*_TPS`) is anchored at
# the cancelled entry's ORIGINAL trig_ts and ends there, full stop — PropSim
# never even sees the early cancel (that's delta 11's whole premise), so
# NT8's re-arm delay does not move the window's endpoint. No delta-8 slack
# added: both timestamps here are NT8-side, same clock, nothing to offset.
# Delta 11's extra fill must land INSIDE that window, not merely "sometime
# after" a hunt_reset — an unbounded match would let a genuine bug hide
# behind this bucket forever.
_DELTA11_BOUND_S = 6 * 30


def _delta11_hunt_reset(nt_row, nt_ep_all):
    """NT8-EXTRA fill inside a window PropSim had marked busy: the nearest
    earlier row in this leg's bucket is an entry cancelled for a hunt reset,
    not a TTL or leg death (delta 11, non-conservative half), AND the fill
    lands within the re-arm+TTL window that reset could plausibly open."""
    key = _bucket_key(nt_row)
    cands = [r for r in nt_ep_all
             if _bucket_key(r) == key and r.get("trig_ts", -1) >= 0
             and r.get("trig_ts") < nt_row.get("trig_ts", -1)]
    if not cands:
        return False
    prev = max(cands, key=lambda r: r["trig_ts"])
    if prev["kind"] != "expired" or prev.get("reason") != "hunt_reset":
        return False
    return (nt_row["trig_ts"] - prev["trig_ts"]) <= _DELTA11_BOUND_S * _TPS


def check_exits(nt_exit_rows, pairs1):
    """Exit-price side-channel: NT8's "exit" row against PropSim's implied
    exit (the matched filled episode's stop_px/target_px — PropSim logs no
    resolved-exit fields of its own)."""
    filled_ps_by_trig = {nt["trig_ts"]: ps for ps, nt, _ in pairs1 if nt["kind"] == "filled"}
    out = []
    for ex in nt_exit_rows:
        ps = filled_ps_by_trig.get(ex.get("trig_ts"))
        reason = ex.get("reason")
        if ps is None or reason not in ("stop", "target"):
            out.append((ex, "NO_IMPLIED_EXIT", None))     # flatten/manual: no PropSim price to check
            continue
        implied = ps["stop_px"] if reason == "stop" else ps["target_px"]
        dt = _tick_delta(ex["exit_px"], implied)          # the .cs always writes this; a
                                                            # missing field is malformed data,
                                                            # not a case to paper over
        if dt <= 1.0 + 1e-6:
            out.append((ex, "MATCHED_EXIT", dt))
        elif reason == "target" and ex["dir"] * (ex["exit_px"] - implied) > 0:
            # delta 13: a stop-market entry filling through its price on a gap
            # can round-trip the frozen target instantly — fill-slippage, not
            # a pattern failure. Mechanism-bound: the fill must actually land
            # BEYOND target in the trade's own direction, or this is a real
            # mismatch (a target undershoot has no delta-13 explanation).
            out.append((ex, "DELTA13_GAP_SLIPPAGE", dt))
        else:
            out.append((ex, "UNEXPLAINED_EXIT", dt))
    return out


def run_gate(nt8_rows_raw, ps_rows):
    sanity_check(nt8_rows_raw, ps_rows)
    nt8_rows = dedup_nt8(nt8_rows_raw)
    nt_exit = [r for r in nt8_rows if r["kind"] == "exit"]
    nt_ep = [r for r in nt8_rows if r["kind"] != "exit"]

    ps_trig = [r for r in ps_rows if r["kind"] in ("filled", "expired")]
    nt_trig = [r for r in nt_ep if r["kind"] in ("filled", "expired")]
    ps_term = [r for r in ps_rows if r["kind"] in ("leg_died", "no_attempt_left")]
    nt_term = [r for r in nt_ep if r["kind"] in ("leg_died", "no_attempt_left")]

    pairs1, left_ps1, left_nt1 = _greedy_match(ps_trig, nt_trig, "trig_ts")
    pairs2, left_ps2, left_nt2 = _greedy_match(ps_term, nt_term, "leg_arm_ts")

    buckets = {}

    def add(label, item):
        buckets.setdefault(label, []).append(item)

    for ps, nt, dt in pairs1:
        label, deltas = classify_pair(ps, nt)
        add(label, dict(ps=ps, nt=nt, dt_s=dt, deltas=deltas))
    for ps, nt, dt in pairs2:
        add("MATCHED_TERMINAL", dict(ps=ps, nt=nt, dt_s=dt))

    ps_bucket_set = {_bucket_key(r) for r in ps_rows}
    nt_bucket_set = {_bucket_key(r) for r in nt_ep}
    unmatched_ps = left_ps1 + left_ps2
    unmatched_nt = left_nt1 + left_nt2

    # delta 14: end-of-tape terminal row — one PropSim-only row per date is
    # expected (the slice ends with a leg still alive; NT8 has no equivalent
    # moment). Pick it as the leftover terminal row with the latest arm time.
    eot_by_date = {}
    for r in unmatched_ps:
        if r["kind"] not in ("leg_died", "no_attempt_left"):
            continue
        if _bucket_key(r) not in nt_bucket_set:
            continue                      # delta 7 takes this one, below
        d = r["date"]
        if d not in eot_by_date or r.get("leg_arm_ts", -1) > eot_by_date[d].get("leg_arm_ts", -1):
            eot_by_date[d] = r
    eot_ids = {id(r) for r in eot_by_date.values()}

    exit_by_trig = {r.get("trig_ts"): r for r in nt_exit}

    for r in unmatched_ps:
        if _bucket_key(r) not in nt_bucket_set:
            # delta 7 signature: the whole leg never appears on the other
            # side — report prominently, it means bar-boundary misalignment.
            add("DELTA7_NEVER_ARMED_NT8", dict(row=r))
        elif id(r) in eot_ids:
            add("DELTA14_EOT_TERMINAL", dict(row=r))
        elif r["kind"] == "filled" and r.get("attempt") == 2 and _delta12_hit(r, pairs1, exit_by_trig):
            add("DELTA12_MANUAL_NO_ATTEMPT2", dict(row=r))
        else:
            add("UNEXPLAINED", dict(row=r, side="propsim"))

    for r in unmatched_nt:
        if _bucket_key(r) not in ps_bucket_set:
            add("DELTA7_NEVER_ARMED_PROPSIM", dict(row=r))
        elif r["kind"] == "filled" and _delta11_hunt_reset(r, nt_ep):
            add("DELTA11_HUNT_RESET_EXTRA", dict(row=r))
        else:
            add("UNEXPLAINED", dict(row=r, side="nt8"))

    for ex, label, dt in check_exits(nt_exit, pairs1):
        add(label, dict(row=ex, dt_s=dt))

    return buckets


# Buckets where a PropSim `filled` episode is legitimately explained without a
# clean NT8 match (docs/validation.md: "Known accepted divergences ... do not
# count against the 95%"). DELTA7 is deliberately excluded: unlike 5/11/12
# (positive corroboration -- a matched pair plus reason evidence from the NT8
# side), DELTA7 is pure absence, and it's the designated symptom of a template/
# alignment/chunking setup error -- it keeps its own bucket for diagnosis but
# still counts against the rate. DELTA11_HUNT_RESET_EXTRA and DELTA13/14 are
# also excluded: NT8-side extras or exit-side/terminal rows, never a PropSim
# `filled` row that needs excusing from the denominator.
ACCEPTED_DELTA_FILL_LABELS = (
    "DELTA5_TTL_JITTER", "DELTA11_EARLY_CANCEL", "DELTA12_MANUAL_NO_ATTEMPT2",
)


def verdict(buckets, ps_rows):
    """PASS is MATCHED / (PropSim fills minus the ones excused by an accepted
    delta), not MATCHED / all PropSim fills -- the raw rate mechanically fails
    the moment a single accepted delta shows up in a small session set."""
    total_filled = sum(1 for r in ps_rows if r["kind"] == "filled")
    matched = len(buckets.get("MATCHED", []))
    accepted = sum(
        1
        for label in ACCEPTED_DELTA_FILL_LABELS
        for item in buckets.get(label, [])
        if (item.get("ps") or item.get("row") or {}).get("kind") == "filled"
    )
    denom = total_filled - accepted
    raw_rate = (matched / total_filled * 100.0) if total_filled else 0.0
    adj_rate = (matched / denom * 100.0) if denom else 0.0
    unexplained = (buckets.get("UNEXPLAINED", []) + buckets.get("UNEXPLAINED_PRICE", [])
                   + buckets.get("UNEXPLAINED_EXIT", []))
    passed = total_filled > 0 and denom > 0 and adj_rate >= 95.0 and not unexplained
    return passed, raw_rate, adj_rate, total_filled, matched, accepted, unexplained


def _row_date(item):
    for k in ("ps", "nt", "row"):
        if k in item and item[k].get("date"):
            return item[k]["date"]
    return "?"


LABEL_ORDER = [
    "MATCHED", "UNEXPLAINED_PRICE", "MATCHED_NO_FILL", "MATCHED_TERMINAL",
    "DELTA5_TTL_JITTER", "DELTA7_NEVER_ARMED_NT8", "DELTA7_NEVER_ARMED_PROPSIM",
    "DELTA11_EARLY_CANCEL", "DELTA11_HUNT_RESET_EXTRA", "DELTA12_MANUAL_NO_ATTEMPT2",
    "DELTA13_GAP_SLIPPAGE", "DELTA14_EOT_TERMINAL", "MATCHED_EXIT",
    "NO_IMPLIED_EXIT", "UNEXPLAINED_EXIT", "UNEXPLAINED",
]

CHECKLIST = """
Gate FAILED. First suspects (plan "Known mirror deltas" 7 and 9):
  1. Bar-boundary alignment — primary series must be exactly 30 Second on an
     RTH session template; a one-30s-bar 15m fold lag flips arm -> never-arm
     for Amendment 2's `i == ext_i + 2` window (delta 7/9).
  2. ATR seeding — both sides' Wilder recursion must start from bar 0 with no
     NaN warmup, reaching across session breaks exactly (wilder_atr's own
     docstring; LatigoBreakStrategy.cs:1009-1021 is the ported recursion).
  3. Session template — the NT8 chart must be RTH-only (09:30-16:00 ET), the
     same slice PropSim's tape.slice_range(rth_only=True) produces.
  4. Series history coverage — the 15m series must not reach further back
     than the 30s one (a longer 15m history gives NT8 zones/ATR15 warmup
     PropSim never had).
"""


def print_report(buckets, ps_rows, nt8_rows):
    dates = sorted({r["date"] for r in ps_rows} | {r["date"] for r in nt8_rows})
    print(f"\n=== PullbackZone V1 mirror gate — {len(dates)} session(s) ===\n")
    for d in dates:
        print(f"-- {d} --")
        for label in LABEL_ORDER:
            n = sum(1 for it in buckets.get(label, []) if _row_date(it) == d)
            if n:
                print(f"  {label:<28} {n}")
    print()

    ticks_hist = {}
    for label in ("MATCHED", "UNEXPLAINED_PRICE"):
        for it in buckets.get(label, []):
            for v in (it.get("deltas") or {}).values():
                b = round(v)
                ticks_hist[b] = ticks_hist.get(b, 0) + 1
    if ticks_hist:
        print("price-delta histogram (ticks, rounded):")
        for b in sorted(ticks_hist):
            print(f"  {b:>3}t: {'#' * ticks_hist[b]} ({ticks_hist[b]})")
        print()

    for label in ("UNEXPLAINED", "UNEXPLAINED_PRICE", "UNEXPLAINED_EXIT"):
        for it in buckets.get(label, []):
            print(f"[{label}] full dump:")
            for side_key in ("ps", "nt", "row"):
                if side_key in it:
                    print(f"  {side_key}: {json.dumps(it[side_key], sort_keys=True)}")
            print()


def main(argv=None):
    ap = argparse.ArgumentParser(description="PullbackZone V1 mirror gate: NT8 Playback vs PropSim episode corpus")
    ap.add_argument("--nt8", help="NT8 Playback JSONL corpus")
    ap.add_argument("--propsim", help="dump_episodes.py JSONL corpus")
    ap.add_argument("--propsim-contract", default=None,
                     help="contract passed to dump_episodes.py's --contract for the "
                          "--propsim file (delta 6: warns if ALL — spans rolls, not a "
                          "single Replay session's slice)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        _selftest()
        return 0

    if not a.nt8 or not a.propsim:
        ap.error("--nt8 and --propsim are required (or use --selftest)")

    if a.propsim_contract and a.propsim_contract.strip().upper() == "ALL":
        print("WARNING: --propsim-contract ALL spans rolls — dump_episodes.py's ALL "
              "default is NOT a single Replay session's slice (plan delta 6). Re-dump "
              "with the explicit contract Javier's Replay session used.", file=sys.stderr)

    _assert_int_roundtrip()
    nt8_raw = load_jsonl(a.nt8)
    ps_rows = load_jsonl(a.propsim)
    try:
        buckets = run_gate(nt8_raw, ps_rows)
    except SanityGateError as e:
        print(f"SANITY GATE REJECTED: {e}", file=sys.stderr)
        return 2

    print_report(buckets, ps_rows, dedup_nt8(nt8_raw))
    passed, raw_rate, adj_rate, total, matched, accepted, unexplained = verdict(buckets, ps_rows)
    print(f"filled episodes matched: {matched}/{total} raw={raw_rate:.1f}%, "
          f"adjusted={adj_rate:.1f}% (excludes {accepted} accepted-delta fill(s), "
          f"denom {total - accepted}), unexplained rows: {len(unexplained)}")
    if passed:
        print("PASS")
        return 0
    print("FAIL")
    print(CHECKLIST)
    return 1


# --------------------------------------------------------------- selftest
def _mk_ts(date_str, hh, mm, ss=0):
    from datetime import date
    y, m, d = (int(x) for x in date_str.split("-"))
    days = (date(y, m, d) - date(1, 1, 1)).days
    return (days * 86400 + hh * 3600 + mm * 60 + ss) * _TPS


def _mk_row(source, kind, date_str, dir_, zone_px, trig_hhmm, reason=None, attempt=1, exit_px=None):
    trig_ts = _mk_ts(date_str, *trig_hhmm)
    row = dict(kind=kind, dir=dir_, zone_px=zone_px, zone_touches=2,
               leg_arm_ts=trig_ts - 5 * 30 * _TPS, trig_ts=trig_ts, trig_kind="engulfing",
               attempt=attempt, entry_stop_px=zone_px + 5.0, entry_tick=-1 if source == "nt8" else 100,
               pull_ext_px=zone_px - 2.0, stop_px=zone_px - 4.0, target_px=zone_px + 11.0,
               atr30=12.5, atr15=30.0, date=date_str, source=source)
    if source == "nt8":
        row["epoch"] = 1
        row["instrument"] = "NQ"
    if reason is not None:
        row["reason"] = reason
    if exit_px is not None:
        row["exit_px"] = exit_px
    return row


def _selftest():
    import tempfile

    _assert_int_roundtrip()
    print("int roundtrip OK")

    # (a) perfect match -> PASS, exit 0. +20s trig offset stays inside the
    # inclusive 30s window (delta 8's systematic (0,30] offset).
    ps_a = _mk_row("propsim", "filled", "2026-08-04", 1, 21100.0, (10, 5, 0))
    nt_a = _mk_row("nt8", "filled", "2026-08-04", 1, 21100.0, (10, 5, 20))
    with tempfile.TemporaryDirectory() as td:
        nt_p, ps_p = Path(td) / "nt8.jsonl", Path(td) / "ps.jsonl"
        nt_p.write_text(json.dumps(nt_a) + "\n")
        ps_p.write_text(json.dumps(ps_a) + "\n")
        rc = main(["--nt8", str(nt_p), "--propsim", str(ps_p)])
    assert rc == 0, rc
    print("selftest (a) perfect match -> PASS OK")

    # (b) 2-tick stop mismatch -> FAIL, exit 1, row dumped in UNEXPLAINED_PRICE.
    nt_b = dict(nt_a, stop_px=nt_a["stop_px"] + 2 * TICK)
    with tempfile.TemporaryDirectory() as td:
        nt_p, ps_p = Path(td) / "nt8.jsonl", Path(td) / "ps.jsonl"
        nt_p.write_text(json.dumps(nt_b) + "\n")
        ps_p.write_text(json.dumps(ps_a) + "\n")
        rc = main(["--nt8", str(nt_p), "--propsim", str(ps_p)])
    assert rc == 1, rc
    buckets_b = run_gate([nt_b], [ps_a])
    assert len(buckets_b.get("UNEXPLAINED_PRICE", [])) == 1
    assert buckets_b["UNEXPLAINED_PRICE"][0]["deltas"]["stop"] > 1.0
    print("selftest (b) 2-tick mismatch -> FAIL, row dumped OK")

    # (c) delta-11 expired/reason=leg_died vs filled -> expected, not unexplained.
    ps_c = _mk_row("propsim", "filled", "2026-08-04", 1, 21200.0, (11, 0, 0))
    nt_c = _mk_row("nt8", "expired", "2026-08-04", 1, 21200.0, (11, 0, 15), reason="leg_died")
    buckets_c = run_gate([nt_c], [ps_c])
    assert len(buckets_c.get("DELTA11_EARLY_CANCEL", [])) == 1
    assert not buckets_c.get("UNEXPLAINED") and not buckets_c.get("UNEXPLAINED_PRICE")
    print("selftest (c) delta-11 leg_died -> expected-delta, not unexplained OK")

    # (d) out-of-RTH trig_ts -> sanity gate rejection, exit 2.
    ps_d = _mk_row("propsim", "filled", "2026-08-04", 1, 21100.0, (20, 0, 0))
    nt_d = _mk_row("nt8", "filled", "2026-08-04", 1, 21100.0, (20, 0, 10))
    with tempfile.TemporaryDirectory() as td:
        nt_p, ps_p = Path(td) / "nt8.jsonl", Path(td) / "ps.jsonl"
        nt_p.write_text(json.dumps(nt_d) + "\n")
        ps_p.write_text(json.dumps(ps_d) + "\n")
        rc = main(["--nt8", str(nt_p), "--propsim", str(ps_p)])
    assert rc == 2, rc
    print("selftest (d) out-of-RTH -> sanity gate rejection, exit 2 OK")

    # (e) delta-11 bound at the actual boundary (180s = entry_ttl_bars*30,
    # PropSim's own busy-window length; <= is inclusive): +179s -> inside,
    # DELTA11_HUNT_RESET_EXTRA; +181s -> outside, UNEXPLAINED. Two separate
    # zone buckets so each fill's "nearest preceding row" is unambiguously
    # its own hunt_reset, not the other case's fill.
    d5 = "2026-08-04"
    nt_reset_in = _mk_row("nt8", "expired", d5, 1, 21300.0, (9, 31, 0), reason="hunt_reset")
    nt_in = _mk_row("nt8", "filled", d5, 1, 21300.0, (9, 33, 59))           # +179s: inside 180s
    nt_reset_out = _mk_row("nt8", "expired", d5, 1, 21400.0, (9, 31, 0), reason="hunt_reset")
    nt_out = _mk_row("nt8", "filled", d5, 1, 21400.0, (9, 34, 1))          # +181s: outside 180s
    # A same-bucket propsim row far off in time keeps the bucket "present" on
    # the propsim side, so delta7 (whole leg absent) doesn't preempt the bound check.
    ps_dummy_in = _mk_row("propsim", "expired", d5, 1, 21300.0, (14, 0, 0))
    ps_dummy_out = _mk_row("propsim", "expired", d5, 1, 21400.0, (14, 0, 0))
    buckets_e = run_gate([nt_reset_in, nt_in, nt_reset_out, nt_out],
                          [ps_dummy_in, ps_dummy_out])
    assert any(it["row"] is nt_in for it in buckets_e.get("DELTA11_HUNT_RESET_EXTRA", []))
    assert any(it["row"] is nt_out for it in buckets_e.get("UNEXPLAINED", []))
    print("selftest (e) delta-11 hunt-reset bound (180s, inclusive) at +179s/+181s OK")

    # (f) delta-13 precondition: exit(reason=target) landing 3 ticks SHORT of
    # target_px (an undershoot, not "beyond target on a gap") must stay
    # UNEXPLAINED_EXIT, never DELTA13_GAP_SLIPPAGE.
    ps_f = _mk_row("propsim", "filled", d5, 1, 21500.0, (12, 0, 0))
    nt_f = _mk_row("nt8", "filled", d5, 1, 21500.0, (12, 0, 10))
    ex_f = _mk_row("nt8", "exit", d5, 1, 21500.0, (12, 0, 10),
                    reason="target", exit_px=nt_f["target_px"] - 3 * TICK)
    ex_row, label_f, dt_f = check_exits([ex_f], [(ps_f, nt_f, 0.0)])[0]
    assert label_f == "UNEXPLAINED_EXIT", label_f
    assert dt_f == 3.0, dt_f
    print("selftest (f) delta-13 target undershoot -> UNEXPLAINED_EXIT, not gap-slippage OK")

    # (g) verdict() denominator: an accepted delta must not count against the
    # 95% (docs/validation.md). 5 exact matches + 1 legitimate early-cancel
    # (PropSim filled, NT8 expired/leg_died) -> raw 5/6=83.3%, adjusted 5/5=100%,
    # PASS despite the raw rate sitting well under the 95% bar.
    d6 = "2026-08-06"
    ps_g, nt_g = [], []
    for i in range(5):
        zpx = 21000.0 + i * 20
        ps_g.append(_mk_row("propsim", "filled", d6, 1, zpx, (9, 40 + i, 0)))
        nt_g.append(_mk_row("nt8", "filled", d6, 1, zpx, (9, 40 + i, 10)))
    zpx_cancel = 21200.0
    ps_g.append(_mk_row("propsim", "filled", d6, 1, zpx_cancel, (10, 0, 0)))
    nt_g.append(_mk_row("nt8", "expired", d6, 1, zpx_cancel, (10, 0, 15), reason="leg_died"))
    buckets_g = run_gate(nt_g, ps_g)
    passed_g, raw_g, adj_g, total_g, matched_g, accepted_g, unexp_g = verdict(buckets_g, ps_g)
    assert total_g == 6 and matched_g == 5 and accepted_g == 1, (total_g, matched_g, accepted_g)
    assert abs(raw_g - 500.0 / 6.0) < 0.05, raw_g
    assert adj_g == 100.0, adj_g
    assert passed_g and not unexp_g, (passed_g, unexp_g)
    print("selftest (g) 1/6 accepted-delta fill -> raw 83.3%, adjusted 100%, PASS OK")

    # (g) inverse guard: a genuine mismatch (PropSim expired, NT8 filled --
    # "PropSim never saw the print reach entry, but NT8 filled") still fails
    # the gate even with 20/20 matched fills and a 100% raw/adjusted rate --
    # the zero-UNEXPLAINED hard rule is never waived by the rate.
    d7 = "2026-08-07"
    ps_h, nt_h = [], []
    for i in range(20):
        zpx = 22000.0 + i * 20
        ps_h.append(_mk_row("propsim", "filled", d7, 1, zpx, (10, i, 0)))
        nt_h.append(_mk_row("nt8", "filled", d7, 1, zpx, (10, i, 10)))
    zpx_mismatch = 22500.0
    ps_h.append(_mk_row("propsim", "expired", d7, 1, zpx_mismatch, (11, 0, 0), reason="ttl"))
    nt_h.append(_mk_row("nt8", "filled", d7, 1, zpx_mismatch, (11, 0, 10)))
    buckets_h = run_gate(nt_h, ps_h)
    passed_h, raw_h, adj_h, total_h, matched_h, accepted_h, unexp_h = verdict(buckets_h, ps_h)
    assert total_h == 20 and matched_h == 20, (total_h, matched_h)
    assert len(unexp_h) == 1, unexp_h
    assert not passed_h, (passed_h, raw_h, adj_h)
    print("selftest (g) inverse guard: 1 UNEXPLAINED among 20 matched -> still FAIL OK")

    # (h) delta 7 counts AGAINST the rate (reviewer-adjudicated false-PASS
    # probe, collapsed): 5 exact matches + 1 ps-fill whose bucket never
    # appears on the NT8 side at all (DELTA7_NEVER_ARMED_NT8 -- pure absence,
    # unlike 5/11/12's positive NT8-side corroboration) -> adjusted 5/6 =
    # 83.3%, still FAIL.
    d8 = "2026-08-08"
    ps_i, nt_i = [], []
    for i in range(5):
        zpx = 23000.0 + i * 20
        ps_i.append(_mk_row("propsim", "filled", d8, 1, zpx, (10, i, 0)))
        nt_i.append(_mk_row("nt8", "filled", d8, 1, zpx, (10, i, 10)))
    ps_i.append(_mk_row("propsim", "filled", d8, 1, 23500.0, (11, 0, 0)))  # never-armed leg
    buckets_i = run_gate(nt_i, ps_i)
    passed_i, raw_i, adj_i, total_i, matched_i, accepted_i, unexp_i = verdict(buckets_i, ps_i)
    assert total_i == 6 and matched_i == 5 and accepted_i == 0, (total_i, matched_i, accepted_i)
    assert abs(adj_i - 500.0 / 6.0) < 0.05, adj_i
    assert not passed_i and not unexp_i, (passed_i, unexp_i)
    print("selftest (h) delta-7 counts against the rate -> adjusted 83.3%, FAIL OK")

    print("compare_mirror selftest: ALL OK")


if __name__ == "__main__":
    sys.exit(main())
