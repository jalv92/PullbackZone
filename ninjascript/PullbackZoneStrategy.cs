// PullbackZoneStrategy — 15-minute S/R zone -> 30-second leg -> pullback ->
// reversal-candle trigger. NQ, RTH only (09:30-16:00 ET).
//
// Detection + orders: a stop entry beyond the trigger candle with a WALL-CLOCK
// TTL, live-until-cancelled brackets that survive being dragged by hand, a
// second attempt per leg only after the first one STOPS OUT, a session flatten
// and an optional daily-R lockout.
//
// THE MIRROR IS THE CONTRACT. propsim/pullback_zone.py is the reviewed,
// calibrated implementation of this pattern; every rule below is a line-by-line
// port of it and where the two disagree THAT FILE WINS. Comments name the
// Python construct each block came from.
//
// THE ORDER LAYER decides HOW those entries execute; WHICH entries exist and at
// WHAT prices is settled by the detection half and frozen into `_pend` at the
// trigger bar's close. Nothing below ever recomputes a price from later data —
// the brackets are submitted from the fill event (LatigoBreak's mechanism) but
// AT THE FROZEN PRICES, not off the fill, because PropSim's `_resolve_exit`
// resolves against those absolute prices. Prices are rounded to the tick grid
// only at submission; the corpus logs the unrounded geometry the Python
// computes, so the mirror gate compares like with like (the two differ by at
// most half a tick, inside the gate's 1-tick tolerance).
//
// CORPUS VOCABULARY. Rows are now the Python's: "filled" / "expired" /
// "leg_died" / "no_attempt_left" (plan delta 10 retired the detection-only
// "trigger" row). Two additions the Python has no use for and the joiner may
// ignore: an "exit" row (reason stop/target/flatten/manual + exit price), which
// is what V1 checks the exit prices against, and a `reason` field on "expired"
// telling a TTL death from a cancel.
//
// MIRROR DELTA 11 (new here, plan-documented): PropSim resolves an entry's fate
// inside the trigger bar's own iteration, so its order always works the full
// TTL. This side has to live forward in time, and a working entry belonging to
// a leg that has just died — or to a pullback a new leg extreme has superseded
// — is cancelled instead of left resting. Geometry makes the second case all
// but unreachable (a new extreme is beyond the entry stop, so the stop fills
// first) and the first is bounded by the 3-minute TTL. `reason` on the expired
// row is how Task 7 measures it rather than assuming.
//
// CHART REQUIREMENTS — the mirror is void without them:
//   * Primary series = 30 Second. Amendment 2 arms the hunt at EXACTLY
//     `ext_i + 2` 30s bars, so a bar-boundary disagreement with PropSim flips
//     arm -> never-arm instead of merely delaying it (plan delta 7).
//   * An RTH session template (09:30-16:00 ET), so both grids equal PropSim's
//     RTH slice. 09:30 is a whole multiple of 30 s AND of 900 s, so the two
//     series stay aligned to the open all session.
//   * NT8's global time zone = US Eastern. Corpus timestamps are raw .NET ticks
//     and Task 7 joins them against the PropSim tape's ET ticks.
//   * Both series must cover the same history. PropSim builds its 30s and 15m
//     bars from ONE tape slice, so a 15m series reaching further back than the
//     30s one gives NT8 zones (and an ATR15 warmup) PropSim never had.
// DataLoaded logs a warning if the primary series is not 30 Second.
//
// ATR: hand-rolled Wilder on BOTH series (nt8c cannot resolve the ATR() system
// wrapper — workspace gotcha). The recursion is PropSim's `wilder_atr`, which
// is the textbook Wilder seed and NOT LatigoBreak's ComputeAtr: the seed here
// is the mean of tr[0..i] INCLUDING bar 0 (tr[0] = high - low), divided by
// i + 1. LatigoBreak divides i terms by i and skips bar 0; copying that would
// put the two mirror sides on different ATRs for the first 14 bars of the
// loaded data and every threshold is an ATR multiple.
//
// Zone folding runs from the PRIMARY branch, not from BarsInProgress 1, and
// reads the 15m series by ABSOLUTE index. When a 30s bar and a 15m bar close on
// the same timestamp, PropSim treats the 15m bar as already closed
// (`searchsorted(tc15, tc30, "right") - 1`), but NT8 processes the PRIMARY
// series first on a shared timestamp — so both the 15m branch and the barsAgo
// accessors (which ride the secondary's processing pointer) would hand back the
// PREVIOUS 15m bar at that moment, one bar late, every 15m boundary of every
// session. `BarsArray[1].GetHigh(j)` and friends read the series itself rather
// than the pointer. No lookahead: a time-based bar is stamped at its close, so
// a 15m bar stamped 09:45:00 holds only ticks before 09:45:00 and is complete
// when the 30s bar stamped 09:45:00 closes — and the fold loop's time guard is
// what enforces that. See FoldClosedZoneBars (plan delta 9).
#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class PullbackZoneStrategy : Strategy
    {
        private const int Zone15Idx = 1;

        // Internal constants, NOT parameters — the closed mirror list carries no
        // ATR-period dial and no candle-proportion dial (dial bloat burned a
        // search ledger before). Same status and same values as the Python.
        private const int ZoneAtrN15 = 14;          // _ZONE_ATR_N15
        private const int AtrN30 = 14;              // _ATR_N30
        private const double HammerShadowBody = 2.0;    // _HAMMER_SHADOW_BODY
        private const double HammerOppShadowRng = 0.3;  // _HAMMER_OPP_SHADOW_RNG
        private const double DojiBodyRng = 0.15;        // _DOJI_BODY_RNG

        // A data-integrity guard, not a parameter (_SANITY_STOP_TICKS): a stop
        // this wide can only come from a hole in the tape.
        private const int SanityStopTicks = 1200;

        private const string SigEntry = "PZ_Entry";
        private const string SigStop = "PZ_Stop";
        private const string SigTarget = "PZ_Target";
        private const string SigFlatten = "PZ_Flatten";

        private sealed class Zone
        {
            public int Id;
            public double Px, HalfW;
            public int Touches;
            public bool PivotHigh;
            public bool Touched;                 // z["touched"] — resets every session
            public bool Dead;
            public DateTime BornTime, DiedTime;
            public DateTime BornDay;             // day15[born_i], as a calendar date
        }

        // A revealed pivot accumulating touches; not a zone until touch
        // #zone_min_touches confirms it (`cands` in the Python).
        private sealed class Cand
        {
            public double Px, HalfW;
            public int Touches;
            public bool PivotHigh;
        }

        private sealed class Leg
        {
            public Zone Z;
            public int Dir;
            public double ArmPx, Ext, Pull;
            public bool Hunt, Impulse;
            public int ExtBar;                   // ext_i, primary bar index
            public int Fills = 0;                // consumed by a FILL, never by a trigger
            public int Block = -1;               // no trigger at or before this bar
            public DateTime T0;                  // arm_ts / t0
            public DateTime Day;                 // leg["day"], calendar date
        }

        // One episode's frozen fields. A corpus row is written when its outcome
        // is KNOWN (the fill, the TTL death, the exit), which is always later
        // than the trigger bar that decided its contents and can be later than
        // the leg's own death — so the row carries its own copy and never reads
        // `_leg` at write time. `Owner` is identity, not data: it answers "is
        // the leg alive now the same leg that placed this order".
        private sealed class Row
        {
            public Leg Owner;
            public int Dir, ZoneTouches, Attempt;
            public double ZonePx, EntryStop, PullExt, StopPx, TargetPx, Atr30, Atr15;
            public long ArmTicks, TrigTicks;
            public string TrigKind, Date;
        }

        private readonly List<Zone> _zones = new List<Zone>();
        private readonly List<Cand> _cands = new List<Cand>();
        private Leg _leg;
        private int _zoneSeq;

        // --- order state -----------------------------------------------------
        // `_entryPending` is the single source of truth for "an entry of ours is
        // in flight": it is set BEFORE the submit, so an event that fires
        // in-stack cannot beat it, and it is cleared by name in the two handlers.
        // `_entryOrder` exists only to be cancelled and may briefly hold an
        // already-dead order (the assignment lands after an in-stack event) —
        // never gate on it.
        private Order _entryOrder, _stopOrder, _targetOrder;
        private bool _entryPending, _flattenPending;
        private string _cancelReason;            // non-null = our cancel is already out
        private DateTime _entryDeadline = DateTime.MaxValue;
        private Row _pend;                       // working entry
        private Row _open;                       // the fill that owns the open position

        // Live bracket prices, tick-rounded — synced from OnOrderUpdate, so a
        // hand-dragged stop or target updates them too.
        private double _stopPx, _targetPx;
        private double _entryFillPx, _riskPts;
        private bool _beApplied;
        private DateTime _stopCancelAt = DateTime.MinValue, _targetCancelAt = DateTime.MinValue;

        private bool _lockout;
        private double _dayR;                    // closed-trade R this session

        private int _zoneBarDone = -1;           // last 15m bar index folded in
        private int _n15;                        // 15m bars folded (the ATR recursion's i)
        private double _atr15, _prev15Close;
        private Series<double> _atr30Series;

        private DateTime _prevDay30 = DateTime.MinValue;
        private DateTime _lastBarTime = DateTime.MinValue;
        private int _cutoffSecs;
        private int _barSecs = 30;
        private const int RthOpenSecs = 9 * 3600 + 30 * 60;
        private bool _ethWarned;

        // Rewind fence. Playback rewinds replay bars the strategy has already
        // logged; stamping every corpus row lets Task 7 drop the discarded pass
        // instead of joining a session twice (LatigoBreak lesson — fence by
        // epoch, not by a boolean). A wall-clock stamp rather than a counter, so
        // the corpus stays self-describing across separate RUNS too: a per-
        // instance counter restarts at the same value every launch and two runs
        // of the same session would be indistinguishable.
        private long _epoch;

        private static readonly object _corpusLock = new object();
        private string _corpusPath;
        private readonly HashSet<string> _drawTags = new HashSet<string>();

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "PullbackZoneStrategy";
                Description = "Pullback-continuation off a 15m S/R zone, triggered by a reversal candle on 30s bars: stop entry with a wall-clock TTL, hand-movable brackets, one re-entry after a stop-out. Sim/Playback laboratory until the mirror gate passes. Mirror of propsim/pullback_zone.py — see docs/specs/2026-08-05-pullbackzone-design.md.";
                Calculate = Calculate.OnBarClose;   // decisions on closed bars; resting orders act intrabar
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsInstantiatedOnEachOptimizationIteration = false;
                // 0, not a warmup count: PropSim gates on "both ATRs positive"
                // and nothing else, and both are defined from bar 0.
                BarsRequiredToTrade = 0;

                // FROZEN DEFAULTS — PARAMS_DEFAULT, one property per snake_case
                // key. The five calibrated dials came from percentiles of market
                // behaviour with no P&L anywhere in the derivation; changing one
                // is a new pre-registered run, not a tweak.
                ZonePivotK = 3;
                ZoneMinTouches = 2;
                ZoneWidthAtr15 = 0.30;
                ZoneExpirySessions = 2;
                ZoneBreakAtr15 = 0.25;

                LegMinAtr15 = 0.40;
                LegTimeoutMin = 60;
                MaxAttemptsPerLeg = 2;

                ImpulseMinAtr30 = 2.70;
                PullbackMinAtr30 = 1.15;

                UseEngulfing = true;
                UseHammer = true;
                UseDojiStar = true;

                EntryOffsetTicks = 2;
                EntryTtlBars = 6;

                StopBufferAtr30 = 1.30;
                TargetR = 1.5;
                BreakevenAtR = 0.0;
                BeOffsetTicks = 4;

                Contracts = 1;
                DailyLossR = 0.0;
                FlattenHhmm = 1558;

                ShowDrawings = true;
                WriteCorpus = true;
            }
            else if (State == State.Configure)
            {
                AddDataSeries(BarsPeriodType.Minute, 15);   // BarsInProgress == 1
            }
            else if (State == State.DataLoaded)
            {
                _atr30Series = new Series<double>(this);
                _cutoffSecs = (FlattenHhmm / 100) * 3600 + (FlattenHhmm % 100) * 60;
                _barSecs = BarsPeriod.BarsPeriodType == BarsPeriodType.Second
                    ? BarsPeriod.Value
                    : (BarsPeriod.BarsPeriodType == BarsPeriodType.Minute ? BarsPeriod.Value * 60 : 0);
                if (_barSecs != 30)
                {
                    Log(Name + ": primary series is not 30 Second — the PropSim mirror is void on this chart (Amendment 2's ext_i+2 window counts 30s bars).",
                        Cbi.LogLevel.Warning);
                    if (_barSecs <= 0)
                        _barSecs = 30;
                }
                ResetAll(false);
            }
        }

        // removeDrawings: true only on a Playback rewind — the discarded pass's
        // objects get wiped. A session rollover keeps history on the chart.
        private void ResetAll(bool removeDrawings)
        {
            if (removeDrawings)
            {
                foreach (string tag in _drawTags)
                    RemoveDrawObject(tag);
                _drawTags.Clear();
            }

            _zones.Clear();
            _cands.Clear();
            _leg = null;
            _zoneSeq = 0;
            _zoneBarDone = -1;
            _n15 = 0;
            _atr15 = 0;
            _prev15Close = 0;
            _prevDay30 = DateTime.MinValue;
            _epoch = DateTime.UtcNow.Ticks;

            // Order trackers. The discarded pass's orders are NOT cancelled from
            // here (LatigoBreak does the same): a rewind replaces the account
            // too, and a cancel aimed at a vanished order only muddies the log.
            // Dropping `_pend`/`_open` drops their unwritten rows with them —
            // the pass they belong to is being thrown away.
            _entryOrder = null; _stopOrder = null; _targetOrder = null;
            _entryPending = false; _flattenPending = false;
            _cancelReason = null;
            _entryDeadline = DateTime.MaxValue;
            _pend = null; _open = null;
            _stopPx = 0; _targetPx = 0;
            _entryFillPx = 0; _riskPts = 0;
            _beApplied = false;
            _stopCancelAt = DateTime.MinValue; _targetCancelAt = DateTime.MinValue;
            _lockout = false;
            _dayR = 0;
        }

        private string Tag(string t)
        {
            _drawTags.Add(t);
            return t;
        }

        protected override void OnBarUpdate()
        {
            // All work happens on the 30s branch, zone folding included (header
            // note): NT8 processes the primary FIRST on a shared timestamp, so
            // a 15m branch would deliver every zone one 30s bar late.
            if (BarsInProgress != 0 || CurrentBar < 0)
                return;

            DateTime t = Time[0];
            if (t < _lastBarTime)                    // Playback rewind: hard reset
                ResetAll(true);
            _lastBarTime = t;

            // An ETH template feeds the overnight session into both ATR
            // recursions and into the pivot windows, which voids the mirror.
            // Detection only, once per run — the strategy does not gate on it.
            if (!_ethWarned && (BarStartSecs() < RthOpenSecs || BarStartSecs() >= 16 * 3600))
            {
                _ethWarned = true;
                Log(Name + ": a primary bar opened outside 09:30-16:00 ET — this looks like an ETH session template, not the RTH one PropSim mirrors. Both ATR recursions and every zone are contaminated on this chart.",
                    Cbi.LogLevel.Warning);
            }

            // ATR30 first and unconditionally: PropSim builds the whole atr30
            // array before its loop, so every bar advances the recursion even
            // when the bar is skipped below.
            _atr30Series[0] = ComputeAtr30();

            // No CurrentBars[Zone15Idx] guard: that reads the same processing
            // pointer the fold deliberately avoids, and at the first 15m bar of
            // the run it still says -1 while the series already holds the bar.
            // An empty series has Count 0 and the fold loop simply does nothing.
            FoldClosedZoneBars();                    // 15m: pivots, touches, births, deaths, merges

            // A touch does not survive the overnight gap. A zone outlives the
            // session but "price touched this and then left" is one continuous
            // intraday event.
            if (t.Date != _prevDay30)
            {
                _prevDay30 = t.Date;
                foreach (Zone z in _zones)
                    z.Touched = false;
                // Both lockouts (flatten backstop, daily R) last "until the next
                // session" and nothing else clears them.
                _lockout = false;
                _dayR = 0;
            }

            // Orders first: the flatten, the TTL and the breakeven must run on
            // every bar, including the ones detection returns early from (no
            // closed 15m bar yet, an ATR still zero). A position outlives all of
            // that and stays managed.
            ManageOrders();

            if (_zoneBarDone < 0)                    // jj < 0: no closed 15m bar yet
                return;
            double a15 = _atr15, a30 = _atr30Series[0];
            if (!(a15 > 0 && a30 > 0))
                return;

            if (UpdateLeg(a15, a30))
                HuntTrigger(a15, a30);
        }

        // --- 15m zone engine (Python `zones`) -------------------------------

        // Fold every 15m bar that has closed at or before this 30s bar's close,
        // exactly once, in order.
        //
        // Bars.Count spans the WHOLE loaded series, future bars included, so the
        // `> Time[0]` guard is the ONLY thing standing between this loop and
        // lookahead. IT MUST NEVER BE WEAKENED.
        private void FoldClosedZoneBars()
        {
            Bars b15 = BarsArray[Zone15Idx];
            bool folded = false;
            for (int j = _zoneBarDone + 1; j < b15.Count; j++)
            {
                if (b15.GetTime(j) > Time[0])
                    break;                           // not closed yet
                FoldZoneBar(b15, j);
                _zoneBarDone = j;
                folded = true;
            }
            if (folded)
            {
                DrawZones();                         // greys the dead ones one last time
                _zones.RemoveAll(z => z.Dead);       // the 30s hot path only walks live zones
            }
        }

        private void FoldZoneBar(Bars b15, int j)
        {
            double h = b15.GetHigh(j), l = b15.GetLow(j), c = b15.GetClose(j);
            DateTime t = b15.GetTime(j);

            // wilder_atr on the 15m series. TrueRange reaches ACROSS the session
            // break, exactly like NinjaTrader's own recursion — deliberate, and
            // the one convention both mirror sides already reproduce.
            double tr = _n15 == 0
                ? h - l
                : Math.Max(h - l, Math.Max(Math.Abs(h - _prev15Close), Math.Abs(l - _prev15Close)));
            _atr15 = _n15 < ZoneAtrN15
                ? (_atr15 * _n15 + tr) / (_n15 + 1)
                : _atr15 + (tr - _atr15) / ZoneAtrN15;
            _prev15Close = c;
            _n15++;

            double a = _atr15;
            if (!(a > 0))                            // `if not finite_a: continue`
                return;

            // Reveals at this bar: the pivot sits k bars back and is confirmed
            // by this close. Strict-unique max/min over the 2k+1 window; highs
            // are offered before lows, as in the Python's reveal dict.
            int k = ZonePivotK;
            if (j - 2 * k >= 0)
            {
                double ph = b15.GetHigh(j - k), pl = b15.GetLow(j - k);
                bool hiMax = true, loMin = true;
                int hiEq = 0, loEq = 0;
                for (int w = j - 2 * k; w <= j; w++)
                {
                    double wh = b15.GetHigh(w), wl = b15.GetLow(w);
                    if (wh > ph) hiMax = false;
                    else if (wh == ph) hiEq++;
                    if (wl < pl) loMin = false;
                    else if (wl == pl) loEq++;
                }
                if (hiMax && hiEq == 1) RevealPivot(ph, true, a);
                if (loMin && loEq == 1) RevealPivot(pl, false, a);
            }

            // Touches. A candidate revealed on THIS bar can be touched by it —
            // the Python's touch loop runs after the reveal block, over the same
            // list. Promotion removes the candidate and appends the zone.
            for (int i = 0; i < _cands.Count; )
            {
                Cand cd = _cands[i];
                double loEdge = cd.Px - cd.HalfW, hiEdge = cd.Px + cd.HalfW;
                bool touched = cd.PivotHigh
                    ? (h >= loEdge && c < loEdge)
                    : (l <= hiEdge && c > hiEdge);
                if (!touched)
                {
                    i++;
                    continue;
                }
                cd.Touches++;
                if (cd.Touches >= ZoneMinTouches)
                {
                    _zones.Add(new Zone
                    {
                        Id = _zoneSeq++,
                        Px = cd.Px,
                        HalfW = cd.HalfW,
                        Touches = cd.Touches,
                        PivotHigh = cd.PivotHigh,
                        BornTime = t,
                        BornDay = t.Date,
                    });
                    _cands.RemoveAt(i);
                }
                else i++;
            }

            // Deaths. The zone born on this very bar is in the list and IS
            // tested — same as the Python, where `out` already holds it.
            foreach (Zone z in _zones)
            {
                if (z.Dead)
                    continue;
                double loEdge = z.Px - z.HalfW, hiEdge = z.Px + z.HalfW;
                bool broke = z.PivotHigh
                    ? (c > hiEdge + ZoneBreakAtr15 * a)
                    : (c < loEdge - ZoneBreakAtr15 * a);
                // Expiry counts CALENDAR days, not trading sessions: day15 is a
                // day index, so a Friday zone is dead on Monday. Deliberate.
                if (broke || (t.Date - z.BornDay).Days >= ZoneExpirySessions)
                {
                    z.Dead = true;
                    z.DiedTime = t;
                }
            }
        }

        // Merge rule: no new candidate within one band-width of a LIVE zone or
        // of an existing candidate — the older one keeps its identity and touch
        // count. Half-width is frozen at the ATR15 of the reveal bar, because a
        // zone is a fixed box on the chart and its edges cannot drift.
        private void RevealPivot(double px, bool isHigh, double a)
        {
            double hw = ZoneWidthAtr15 * a;
            foreach (Zone z in _zones)
                if (!z.Dead && Math.Abs(z.Px - px) < hw + z.HalfW)
                    return;
            foreach (Cand cd in _cands)
                if (Math.Abs(cd.Px - px) < hw + cd.HalfW)
                    return;
            _cands.Add(new Cand { Px = px, HalfW = hw, Touches = 0, PivotHigh = isHigh });
        }

        // --- 30s leg engine (Python `episodes`, the per-bar body) -----------

        // Returns true when this bar may look for a trigger candle.
        private bool UpdateLeg(double a15, double a30)
        {
            int sod = BarStartSecs();

            // Leg death. Gates NEW entries only: a position opened by this leg
            // outlives it and belongs to its brackets (spec 2, last bullet).
            if (_leg != null)
            {
                int d = _leg.Dir;
                Zone z0 = _leg.Z;
                string why = null;
                if (_leg.Fills >= MaxAttemptsPerLeg) why = "attempts";
                else if (Time[0].Date != _leg.Day || sod >= _cutoffSecs) why = "session";
                else if (Time[0] - _leg.T0 >= TimeSpan.FromMinutes(LegTimeoutMin)) why = "timeout";
                else if (Math.Abs(Close[0] - z0.Px) <= z0.HalfW) why = "reentry";
                else
                {
                    foreach (Zone z in _zones)
                    {
                        if (z.Dead || z == z0 || d * (z.Px - z0.Px) <= 0)
                            continue;
                        bool reached = d < 0 ? Low[0] <= z.Px + z.HalfW : High[0] >= z.Px - z.HalfW;
                        if (reached) { why = "destination"; break; }
                    }
                }
                if (why != null)
                {
                    Corpus(why == "attempts" ? "no_attempt_left" : "leg_died",
                           Snap(null, 0, 0, 0, a15, a30));
                    // A resting entry belongs to the leg that placed it (delta
                    // 11): the leg is gone, so is its order. Ordered before the
                    // null so `_pend.Owner` can still be compared.
                    if (_pend != null && _pend.Owner == _leg)
                        CancelEntry("leg_died");
                    _leg = null;
                }
            }

            foreach (Zone z in _zones)
                if (!z.Dead && Math.Abs(Close[0] - z.Px) <= z.HalfW)
                    z.Touched = true;

            // Leg arming: a touched zone departed from by leg_min_atr15. One leg
            // at a time. Whether or not one arms, the bar ends here — extension
            // is measured FROM the arming point.
            if (_leg == null && sod < _cutoffSecs)
            {
                double gap = LegMinAtr15 * a15;
                foreach (Zone z in _zones)
                {
                    if (z.Dead || !z.Touched)
                        continue;
                    int d;
                    if (Close[0] > z.Px + z.HalfW + gap) d = 1;
                    else if (Close[0] < z.Px - z.HalfW - gap) d = -1;
                    else continue;
                    z.Touched = false;               // this touch is spent on this leg
                    _leg = new Leg
                    {
                        Z = z,
                        Dir = d,
                        ArmPx = Close[0],
                        Ext = Close[0],
                        Pull = Close[0],
                        ExtBar = CurrentBar,
                        T0 = Time[0],
                        Day = Time[0].Date,
                    };
                    break;
                }
                return false;
            }
            if (_leg == null)
                return false;

            int dir = _leg.Dir;

            // Extension and pullback. A bar that makes a new leg extreme RESETS
            // the pullback and is NOT also credited with its own opposite wick:
            // inside one 30s bar the order of the high and the low is unknown,
            // and assuming the convenient one is the intrabar lookahead this
            // project exists to refuse.
            double ext = dir > 0 ? High[0] : Low[0];
            if (dir * (ext - _leg.Ext) > 0)
            {
                _leg.Ext = ext;
                _leg.Pull = ext;
                _leg.ExtBar = CurrentBar;
                _leg.Hunt = false;
                // The hunt window has been reset, so the pullback the working
                // entry was priced from no longer exists (delta 11). All but
                // unreachable: a new extreme lies BEYOND the entry stop, which
                // therefore filled on the way — this is the belt to that brace.
                if (_pend != null && _pend.Owner == _leg)
                    CancelEntry("hunt_reset");
            }
            else
            {
                double cnt = dir > 0 ? Low[0] : High[0];
                if (dir * (cnt - _leg.Pull) < 0)
                    _leg.Pull = cnt;
            }

            if (!_leg.Impulse)
                _leg.Impulse = dir * (_leg.Ext - _leg.ArmPx) >= ImpulseMinAtr30 * a30;

            // AMENDMENT 2 — THE FAST-PULLBACK WINDOW. The hunt arms at EXACTLY
            // one bar: the close of ext_i + 2, on the pullback extreme known
            // through that bar. Both consequences are intended — a single bar's
            // wick MAY deliver the whole depth, and depth arriving at ext_i + 3
            // or later never arms for that extreme, which is what excludes slow
            // grinds. `==`, not `>=`: a `>=` would only delay arming.
            if (_leg.Impulse && !_leg.Hunt && CurrentBar == _leg.ExtBar + 2)
            {
                _leg.Hunt = dir * (_leg.Ext - _leg.Pull) >= PullbackMinAtr30 * a30;
                // One dot per armed hunt, at the stop anchor — the pullback
                // extreme the stop is measured from.
                if (_leg.Hunt && ShowDrawings && ChartControl != null)
                    Draw.Dot(this, Tag("PZ_P" + CurrentBar), false, 0, _leg.Pull, Brushes.Goldenrod);
            }

            // `block` is this leg's own re-arm gate after a fill: int.MaxValue
            // while the position is open, then the exit bar on a stop-out and
            // int.MaxValue forever on any other exit (spec 7).
            return _leg.Hunt && CurrentBar > _leg.Block;
        }

        private void HuntTrigger(double a15, double a30)
        {
            int dir = _leg.Dir;

            // Trigger. The order IS the tie-break when one bar matches two:
            // engulfing, then hammer, then doji.
            string kind;
            if (UseEngulfing && CandleEngulfing(dir)) kind = "engulfing";
            else if (UseHammer && CandleHammer(dir)) kind = "hammer";
            else if (UseDojiStar && CandleDoji()
                     && Math.Abs((dir < 0 ? High[0] : Low[0]) - _leg.Pull) < 1e-9) kind = "doji";
            else return;                             // ...doji must print AT the pullback extreme

            // No entry whose working window could still be alive at the flatten.
            // PropSim compares the bar's LAST TICK second; on a grid-aligned
            // cutoff that is this bar's START second with a `>=` (see
            // BarStartSecs).
            if (BarStartSecs() + EntryTtlBars * _barSecs >= _cutoffSecs)
                return;

            // FLAT TO FLAT, GLOBALLY: while any earlier fill's position is still
            // unresolved OR our entry order is still working, no leg may
            // generate an entry — not even a different leg at a different zone.
            // This is PropSim's `busy` gate: `t_trig < busy` there covers both
            // the open position (busy = the exit) and the unfilled order living
            // out its TTL (busy = t_ttl).
            if (Position.MarketPosition != MarketPosition.Flat || _entryPending || _lockout)
                return;

            double off = EntryOffsetTicks * TickSize;
            double entryStop = dir > 0 ? High[0] + off : Low[0] - off;
            double stopPx = _leg.Pull - dir * StopBufferAtr30 * a30;
            double risk = Math.Abs(stopPx - entryStop);
            if (!(risk > 0 && risk <= SanityStopTicks * TickSize))
                return;
            double targetPx = entryStop + dir * TargetR * risk;

            // The row is written when the order's fate is known — "filled" at
            // the fill, "expired" when it dies unfilled (plan delta 10 retired
            // the detection-only "trigger" row). Its contents are frozen HERE.
            PlaceEntry(dir, Snap(kind, entryStop, stopPx, targetPx, a15, a30));

            if (ShowDrawings && ChartControl != null)
            {
                if (dir > 0)
                    Draw.TriangleUp(this, Tag("PZ_T" + CurrentBar), false, 0, Low[0] - 4 * TickSize, Brushes.Lime);
                else
                    Draw.TriangleDown(this, Tag("PZ_T" + CurrentBar), false, 0, High[0] + 4 * TickSize, Brushes.Red);
            }
        }

        // --- order layer -----------------------------------------------------
        //
        // Every Enter*/Exit*/CancelOrder call below is preceded by its tracker
        // mutation (the nt8-order-event-race invariant): Playback can deliver
        // OnOrderUpdate / OnExecutionUpdate synchronously, in-stack, BEFORE the
        // submitting call returns, and a tracker written afterwards is a tracker
        // the handler read stale.

        private void PlaceEntry(int dir, Row r)
        {
            _pend = r;
            // WALL CLOCK, not a bar count: a hole in the tape (the tape has
            // documented 3,500-second jumps) must expire the order, and bar
            // i + EntryTtlBars can be an hour later across one.
            _entryDeadline = Time[0].AddSeconds(EntryTtlBars * _barSecs);
            _cancelReason = null;
            _entryPending = true;                    // BEFORE the submit
            double px = Instrument.MasterInstrument.RoundToTickSize(r.EntryStop);
            _entryOrder = dir > 0
                ? EnterLongStopMarket(0, true, Contracts, px, SigEntry)
                : EnterShortStopMarket(0, true, Contracts, px, SigEntry);
        }

        // Cancelling does NOT consume the leg's attempt — only a fill does. The
        // "expired" row is written when the order actually reports dead, in
        // OnOrderUpdate, so every death (TTL, leg death, flatten, rejection)
        // leaves exactly one row.
        private void CancelEntry(string why)
        {
            if (!_entryPending || _entryOrder == null || _cancelReason != null)
                return;
            _cancelReason = why;                     // BEFORE the call: it is also the "already sent" flag
            CancelOrder(_entryOrder);
        }

        // Runs on every 30s bar, before detection and before its early returns:
        // a position and a working order outlive the leg that opened them.
        private void ManageOrders()
        {
            // Session backstop. On the bar's NOMINAL close (ToTime), which is
            // where PropSim's _resolve_exit puts its flatten timestamp.
            if (!_lockout && ToTime(Time[0]) >= FlattenHhmm * 100)
                Lockout("session flatten " + FlattenHhmm.ToString(CultureInfo.InvariantCulture));

            // One Exit call is not guaranteed to fill — retry until flat.
            if (_lockout && Position.MarketPosition != MarketPosition.Flat && !_flattenPending)
                FlattenNow();

            if (_entryPending && Time[0] >= _entryDeadline)
                CancelEntry("ttl");

            if (_open != null)
            {
                CheckBracketCancels(Time[0]);
                ManagePosition();
            }
        }

        // Live-until-cancelled Exit brackets, submitted on the entry execution
        // and resized on later ones. The strategy never re-asserts them, so a
        // stop or target dragged by hand in Chart Trader STAYS where you put it
        // (the LatigoBreak v3 lesson: Set*Stop/Set*Profit would be re-asserted).
        // Prices are the FROZEN ones — see the file header.
        private void SubmitBrackets(Order entry)
        {
            int qty = entry.Filled;
            if (qty <= 0 || _open == null)
                return;
            if (_stopPx == 0)
            {
                _stopPx = Instrument.MasterInstrument.RoundToTickSize(_open.StopPx);
                _targetPx = Instrument.MasterInstrument.RoundToTickSize(_open.TargetPx);
            }
            SubmitExits(_open.Dir, qty, _stopPx);
        }

        // BOTH legs, always together, and never before the trackers they read.
        // Under OCO a stop cancel-replace kills the target leg too, so a
        // re-submit that touched only the stop would silently leave the trade
        // without a target (the v4 lesson). The refs are nulled first so the
        // replaced orders' in-stack Cancelled echoes cannot match the current
        // references in OnOrderUpdate. A `_targetPx` of 0 means a hand-cancelled
        // target — it stays cancelled.
        private void SubmitExits(int d, int qty, double stopPx)
        {
            _stopOrder = null; _targetOrder = null;
            if (d > 0)
            {
                _stopOrder = ExitLongStopMarket(0, true, qty, stopPx, SigStop, SigEntry);
                if (_targetPx > 0)
                    _targetOrder = ExitLongLimit(0, true, qty, _targetPx, SigTarget, SigEntry);
            }
            else
            {
                _stopOrder = ExitShortStopMarket(0, true, qty, stopPx, SigStop, SigEntry);
                if (_targetPx > 0)
                    _targetOrder = ExitShortLimit(0, true, qty, _targetPx, SigTarget, SigEntry);
            }
        }

        // Deferred hand-cancel detector: a bracket Cancelled event only counts
        // as "by hand" if the position is still open a second later. Our own
        // replaces never reach here (reference check in OnOrderUpdate) and a
        // closing fill's OCO cancel is cleared by the went-flat bookkeeping
        // first. On 30s bars "a second later" is the next bar close.
        private void CheckBracketCancels(DateTime t)
        {
            if (_stopCancelAt != DateTime.MinValue && (t - _stopCancelAt).TotalSeconds >= 1)
            {
                _stopCancelAt = DateTime.MinValue;
                Print(Name + ": PZ_Stop cancelled by hand — the position is unprotected on that side.");
            }
            if (_targetCancelAt != DateTime.MinValue && (t - _targetCancelAt).TotalSeconds >= 1)
            {
                _targetCancelAt = DateTime.MinValue;
                _targetPx = 0;                       // respect it: breakeven must not resurrect the target
                Print(Name + ": PZ_Target cancelled by hand — take profit removed.");
            }
        }

        // Breakeven, off by default. R is the FROZEN risk (|entry stop - stop|),
        // the same unit PropSim measures in; the stop itself goes to the REAL
        // average fill so the trade is actually flat, not merely mirror-flat.
        // Unmirrored by construction — plan delta 3 warns that enabling this
        // makes PropSim's exit resolution (and with it the attempt-2 grants)
        // wrong, so it stays 0 for the V1 gate.
        private void ManagePosition()
        {
            if (BreakevenAtR <= 0 || _beApplied || _riskPts <= 0
                || Position.MarketPosition == MarketPosition.Flat)
                return;
            int d = _open.Dir;
            if (d * (Close[0] - _open.EntryStop) < BreakevenAtR * _riskPts)
                return;
            double bePx = Instrument.MasterInstrument.RoundToTickSize(
                Position.AveragePrice + d * BeOffsetTicks * TickSize);
            // Never backwards, never at or past a working target — a big offset
            // on a small risk would otherwise invert the bracket.
            if (d > 0 ? (bePx <= _stopPx || (_targetPx > 0 && bePx >= _targetPx))
                      : (bePx >= _stopPx || (_targetPx > 0 && bePx <= _targetPx)))
                return;
            // Trackers BEFORE the submits (the v4 echo lesson: a stale tracker
            // made the breakeven's own echo print as "moved by hand").
            _stopPx = bePx;
            _beApplied = true;
            SubmitExits(d, Position.Quantity, bePx);
            Print(Name + ": breakeven armed at " + J(bePx) + ".");
        }

        private void Lockout(string why)
        {
            if (_lockout)
                return;
            _lockout = true;
            Print(Name + ": " + why + " — locked out until the next session.");
            CancelEntry("lockout");
            if (Position.MarketPosition != MarketPosition.Flat && !_flattenPending)
                FlattenNow();
        }

        private void FlattenNow()
        {
            // Two-arg overload on purpose: ExitLong(string) alone is
            // fromEntrySignal, NOT a signal name (the BigPrints bug). An empty
            // fromEntrySignal attaches the exit to every entry.
            _flattenPending = true;                  // BEFORE the Exit*
            if (Position.MarketPosition == MarketPosition.Long)
                ExitLong(SigFlatten, "");
            else if (Position.MarketPosition == MarketPosition.Short)
                ExitShort(SigFlatten, "");
            else
                _flattenPending = false;
        }

        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition, string orderId, DateTime time)
        {
            if (execution.Order == null)
                return;
            string n = execution.Order.Name;

            if (n == SigEntry)
            {
                // Anything that leaves us holding contracts counts: full fill,
                // partial fill, or a cancel after a partial.
                OrderState st = execution.Order.OrderState;
                if (st != OrderState.Filled && st != OrderState.PartFilled
                    && !(st == OrderState.Cancelled && execution.Order.Filled > 0))
                    return;
                _entryPending = false;               // name-gated clear
                _entryOrder = null;

                if (_open == null)                   // first execution of this entry
                {
                    _open = _pend;
                    _pend = null;
                    if (_open == null)               // a fill with no snapshot: rewound pass
                        return;
                    _entryFillPx = price;
                    _riskPts = Math.Abs(_open.EntryStop - _open.StopPx);
                    Corpus("filled", _open, null, price);
                    // The attempt is consumed HERE, by the fill. `Block` holds
                    // the leg off until the exit answers whether it stopped out
                    // (PropSim decides the same thing at the same moment, from
                    // _resolve_exit).
                    if (_leg != null && _open.Owner == _leg)
                    {
                        _leg.Fills++;
                        _leg.Block = int.MaxValue;
                    }
                }
                if (_lockout)
                {
                    // The lockout landed while this entry was in flight: close
                    // it on the fill event itself, no brackets, no extra bar of
                    // exposure (the LatigoBreak lockout-fill lesson).
                    if (!_flattenPending)
                        FlattenNow();
                    return;
                }
                SubmitBrackets(execution.Order);     // prices on the first, resize on later ones
                return;
            }

            // Went flat. Gated on `_open` — the epoch fence's job here: a stale
            // exit event around a Playback rewind finds `_open` null (ResetAll
            // dropped it) and books nothing against the fresh pass.
            if (_open == null || Position.MarketPosition != MarketPosition.Flat)
                return;

            string reason = n == SigStop ? "stop"
                          : n == SigTarget ? "target"
                          : (n == SigFlatten || n == "Exit on session close") ? "flatten"
                          : "manual";
            _flattenPending = false;
            Corpus("exit", _open, reason, price);
            // ponytail: one entry price, one exit price. At Contracts > 1 with
            // partial fills this is the first fill against the last exit rather
            // than a weighted average — a guard's arithmetic, not the ledger's.
            if (_riskPts > 0)
                _dayR += _open.Dir * (price - _entryFillPx) / _riskPts;

            // SPEC 7: attempt 2 exists only after attempt 1 STOPS OUT. On any
            // other exit this leg is done entering, forever. PropSim derives the
            // same verdict from the print order; here the exit event says it.
            // `Block` = "no trigger at or before this bar": with the exit landing
            // inside the bar now forming, the last CLOSED 30s bar is the block,
            // so the next close may hunt again — PropSim's `searchsorted(tc30,
            // exit_ts, "right") - 1` lands on the same bar (accepted delta 4
            // covers the tie at an exact bar close).
            //
            // CurrentBars[0], never CurrentBar: outside OnBarUpdate the bare
            // property resolves against whichever series ran last, and the 15m
            // one would hand back a wholly different (much smaller) index.
            if (_leg != null && _open.Owner == _leg)
                _leg.Block = reason == "stop" ? CurrentBars[0] : int.MaxValue;

            _open = null;
            _stopPx = 0; _targetPx = 0;
            _entryFillPx = 0; _riskPts = 0;
            _beApplied = false;
            _stopOrder = null; _targetOrder = null;
            _stopCancelAt = DateTime.MinValue; _targetCancelAt = DateTime.MinValue;

            // Daily guard: an internal R tally, never dollars. PropSim's
            // entries() is precomputed and cannot know closures, so it cannot
            // mirror this dial (accepted delta 1) — it defaults to 0 = off and
            // the V1 gate runs with it off.
            if (DailyLossR > 0 && _dayR <= -DailyLossR)
                Lockout("daily loss " + J(_dayR) + "R");
        }

        protected override void OnOrderUpdate(Order order, double limitPrice, double stopPrice,
            int quantity, int filled, double averageFillPrice, OrderState orderState,
            DateTime time, ErrorCode error, string comment)
        {
            if (order == null)
                return;

            if (order.Name == SigFlatten
                && (orderState == OrderState.Rejected || orderState == OrderState.Cancelled))
            {
                _flattenPending = false;             // ManageOrders retries next bar
                return;
            }

            if (order.Name == SigStop || order.Name == SigTarget)
            {
                // Events for orders that are not the CURRENT references are
                // echoes of our own cancel-replaces — ignored wholesale.
                if (!ReferenceEquals(order, _stopOrder) && !ReferenceEquals(order, _targetOrder))
                    return;
                if (orderState == OrderState.Working || orderState == OrderState.Accepted)
                {
                    // Adopt a hand-dragged bracket so the breakeven guards stay
                    // honest about where the protection actually is.
                    double p = order.Name == SigStop ? stopPrice : limitPrice;
                    double tracked = order.Name == SigStop ? _stopPx : _targetPx;
                    if (p > 0 && Math.Abs(p - tracked) >= TickSize * 0.5)
                    {
                        Print(Name + ": " + order.Name + " moved by hand to " + J(p) + " — adopted.");
                        if (order.Name == SigStop) _stopPx = p; else _targetPx = p;
                    }
                }
                else if (orderState == OrderState.Cancelled
                         && Position.MarketPosition != MarketPosition.Flat
                         && !_flattenPending && !_lockout)
                {
                    if (order.Name == SigStop) _stopCancelAt = time;
                    else _targetCancelAt = time;
                }
                return;
            }

            if (order.Name != SigEntry || !_entryPending)
                return;
            if (orderState != OrderState.Rejected && orderState != OrderState.Cancelled)
                return;
            _entryPending = false;
            _entryOrder = null;
            if (filled == 0 && _pend != null)
            {
                // The order died without filling. PropSim's word for that is
                // "expired"; `reason` says whether the TTL ran out or something
                // cancelled it early (delta 11).
                Corpus("expired", _pend,
                       _cancelReason ?? orderState.ToString().ToLowerInvariant(), 0);
                _pend = null;
            }
            _cancelReason = null;
        }

        // --- candle predicates (Python candle_*) -----------------------------

        private bool CandleEngulfing(int d)
        {
            if (CurrentBar < 1)
                return false;
            double bPrev = Close[1] - Open[1], bCur = Close[0] - Open[0];
            if (d > 0)
                return bPrev < 0 && bCur > 0 && Open[0] <= Close[1] && Close[0] >= Open[1]
                       && Math.Abs(bCur) >= Math.Abs(bPrev);
            return bPrev > 0 && bCur < 0 && Open[0] >= Close[1] && Close[0] <= Open[1]
                   && Math.Abs(bCur) >= Math.Abs(bPrev);
        }

        private bool CandleHammer(int d)
        {
            double rng = High[0] - Low[0];
            if (rng <= 0)
                return false;
            double body = Math.Abs(Close[0] - Open[0]);
            double lower = Math.Min(Open[0], Close[0]) - Low[0];
            double upper = High[0] - Math.Max(Open[0], Close[0]);
            if (d > 0)
                return lower >= HammerShadowBody * body
                       && upper <= HammerOppShadowRng * rng
                       && Math.Min(Open[0], Close[0]) >= High[0] - rng / 3.0;
            return upper >= HammerShadowBody * body
                   && lower <= HammerOppShadowRng * rng
                   && Math.Max(Open[0], Close[0]) <= Low[0] + rng / 3.0;
        }

        private bool CandleDoji()
        {
            double rng = High[0] - Low[0];
            return rng > 0 && Math.Abs(Close[0] - Open[0]) <= DojiBodyRng * rng;
        }

        // --- helpers ---------------------------------------------------------

        // wilder_atr on the primary series. Same recursion as the 15m side.
        private double ComputeAtr30()
        {
            if (CurrentBar == 0)
                return High[0] - Low[0];             // tr[0], and the seed is tr[0]/1
            double tr = Math.Max(High[0] - Low[0],
                Math.Max(Math.Abs(High[0] - Close[1]), Math.Abs(Low[0] - Close[1])));
            double prev = _atr30Series[1];
            if (CurrentBar < AtrN30)
                return (prev * CurrentBar + tr) / (CurrentBar + 1);
            return prev + (tr - prev) / AtrN30;
        }

        // PropSim reads the bar's LAST TICK second-of-day; NT8 stamps the bar at
        // its close. Both cutoffs compared against it (flatten, TTL) sit on the
        // 30s grid, so this bar's START second gives the identical verdict for
        // every tick the bar could hold — with `>=` on the TTL comparison, where
        // PropSim's is `>` on a second strictly inside the slot.
        private int BarStartSecs()
        {
            return (int)Time[0].TimeOfDay.TotalSeconds - _barSecs;
        }

        private void DrawZones()
        {
            if (!ShowDrawings || ChartControl == null)
                return;
            // Dead zones get this one final grey draw and are then pruned by
            // the caller, so the box stays on the chart while the object does
            // not stay in the hot path.
            foreach (Zone z in _zones)
            {
                Brush b = z.Dead ? Brushes.Gray : (z.PivotHigh ? Brushes.OrangeRed : Brushes.DodgerBlue);
                Draw.Rectangle(this, Tag("PZ_Z" + z.Id), false,
                    z.BornTime, z.Px + z.HalfW,
                    z.Dead ? z.DiedTime : Time[0], z.Px - z.HalfW,
                    b, b, z.Dead ? 4 : 12);
            }
        }

        // --- JSONL episode corpus -------------------------------------------

        private static string J(double v)
        {
            return v.ToString("0.######", CultureInfo.InvariantCulture);
        }

        // Freeze this bar's episode fields. Called at the trigger (with the
        // entry geometry) and at a leg's death (without it — terminal kinds
        // carry 0.0 / -1 / null, same as the Python `_ep`).
        private Row Snap(string trigKind, double entryStop, double stopPx, double targetPx,
                         double a15, double a30)
        {
            return new Row
            {
                Owner = _leg,
                Dir = _leg.Dir,
                ZonePx = _leg.Z.Px,
                ZoneTouches = _leg.Z.Touches,
                ArmTicks = _leg.T0.Ticks,
                TrigTicks = trigKind == null ? -1L : Time[0].Ticks,
                TrigKind = trigKind,
                // Capped at the max, as in the Python: a fill can only ever be
                // attempt 1..max, so the cap binds on terminal rows alone.
                Attempt = Math.Min(_leg.Fills + 1, MaxAttemptsPerLeg),
                EntryStop = entryStop,
                PullExt = _leg.Pull,
                StopPx = stopPx,
                TargetPx = targetPx,
                Atr30 = a30,
                Atr15 = a15,
                Date = Time[0].ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
            };
        }

        // One episode row. Schema = the Python `_ep` keys in order, plus the
        // `date`/`source` that dump_episodes.py adds on its side, plus `epoch`
        // so a Playback rewind's discarded pass can be dropped by the joiner.
        // `reason`/`px` are the NT8-only tail (file header): the exit row's
        // cause and price, the expired row's cause, the filled row's real fill.
        //
        // Reads NOTHING off the bar series — it is called from OnExecutionUpdate
        // too, where `BarsInProgress` is undefined and `Time[0]` is a trap in a
        // multi-series strategy. Everything time-shaped is already in the Row.
        private void Corpus(string kind, Row r, string reason = null, double px = 0)
        {
            if (!WriteCorpus || r == null)
                return;
            StringBuilder sb = new StringBuilder(512);
            sb.Append("{\"kind\":\"").Append(kind).Append("\"")
              .Append(",\"dir\":").Append(r.Dir.ToString(CultureInfo.InvariantCulture))
              .Append(",\"zone_px\":").Append(J(r.ZonePx))
              .Append(",\"zone_touches\":").Append(r.ZoneTouches.ToString(CultureInfo.InvariantCulture))
              .Append(",\"leg_arm_ts\":").Append(r.ArmTicks.ToString(CultureInfo.InvariantCulture))
              .Append(",\"trig_ts\":").Append(r.TrigTicks.ToString(CultureInfo.InvariantCulture))
              .Append(",\"trig_kind\":").Append(r.TrigKind == null ? "null" : "\"" + r.TrigKind + "\"")
              .Append(",\"attempt\":").Append(r.Attempt.ToString(CultureInfo.InvariantCulture))
              .Append(",\"entry_stop_px\":").Append(J(r.EntryStop))
              .Append(",\"entry_tick\":-1")
              .Append(",\"pull_ext_px\":").Append(J(r.PullExt))
              .Append(",\"stop_px\":").Append(J(r.StopPx))
              .Append(",\"target_px\":").Append(J(r.TargetPx))
              .Append(",\"atr30\":").Append(J(r.Atr30))
              .Append(",\"atr15\":").Append(J(r.Atr15))
              .Append(",\"date\":\"").Append(r.Date).Append("\"")
              .Append(",\"source\":\"nt8\"")
              .Append(",\"epoch\":").Append(_epoch.ToString(CultureInfo.InvariantCulture))
              .Append(",\"instrument\":\"").Append(Instrument.MasterInstrument.Name).Append("\"");
            if (reason != null)
                sb.Append(",\"reason\":\"").Append(reason).Append("\"");
            if (px > 0)
                sb.Append(kind == "filled" ? ",\"fill_px\":" : ",\"exit_px\":").Append(J(px));
            sb.Append("}");
            CorpusAppend(sb.ToString());
        }

        private void CorpusAppend(string json)
        {
            try                                      // logging must never break trading
            {
                if (_corpusPath == null)
                    _corpusPath = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                        "PullbackZone", "pz_corpus.jsonl");
                lock (_corpusLock)
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(_corpusPath));
                    File.AppendAllText(_corpusPath, json + Environment.NewLine);
                }
            }
            catch { }
        }

        #region Properties
        [NinjaScriptProperty, Range(1, 10)]
        [Display(Name = "Pivot K (15m bars)", Description = "Swing pivot lookback/forward on 15m bars. A pivot is usable only from its confirming bar's close.", GroupName = "01. Zones", Order = 0)]
        public int ZonePivotK { get; set; }

        [NinjaScriptProperty, Range(1, 5)]
        [Display(Name = "Min touches", Description = "Touches before a pivot level becomes a zone. Touch = a 15m bar enters the band and closes back on the original side.", GroupName = "01. Zones", Order = 1)]
        public int ZoneMinTouches { get; set; }

        [NinjaScriptProperty, Range(0.05, 2.0)]
        [Display(Name = "Zone half-width (x ATR15)", Description = "CALIBRATED (p60, frozen 2026-08-05). Half-width of the zone band, frozen at the ATR15 of the pivot's reveal bar.", GroupName = "01. Zones", Order = 2)]
        public double ZoneWidthAtr15 { get; set; }

        [NinjaScriptProperty, Range(1, 20)]
        [Display(Name = "Expiry (calendar days)", Description = "A zone dies when the calendar day advances this far from its birth day — a Friday zone is dead on Monday.", GroupName = "01. Zones", Order = 3)]
        public int ZoneExpirySessions { get; set; }

        [NinjaScriptProperty, Range(0.0, 2.0)]
        [Display(Name = "Clean break (x ATR15)", Description = "A 15m close beyond the far edge by more than this kills the zone.", GroupName = "01. Zones", Order = 4)]
        public double ZoneBreakAtr15 { get; set; }

        [NinjaScriptProperty, Range(0.1, 3.0)]
        [Display(Name = "Leg arm (x ATR15)", Description = "CALIBRATED (p40, frozen 2026-08-05). Departure from the zone EDGE, on a 30s close, that arms a leg.", GroupName = "02. Leg", Order = 0)]
        public double LegMinAtr15 { get; set; }

        [NinjaScriptProperty, Range(5, 390)]
        [Display(Name = "Leg timeout (minutes)", Description = "A leg stops arming entries after this long. An open position outlives its leg.", GroupName = "02. Leg", Order = 1)]
        public int LegTimeoutMin { get; set; }

        [NinjaScriptProperty, Range(1, 5)]
        [Display(Name = "Max attempts per leg", Description = "Fills allowed per leg. The second is granted only if the first STOPS OUT; a target or a flatten ends the leg's entries.", GroupName = "02. Leg", Order = 2)]
        public int MaxAttemptsPerLeg { get; set; }

        [NinjaScriptProperty, Range(0.5, 8.0)]
        [Display(Name = "Impulse (x ATR30)", Description = "CALIBRATED (p50, frozen 2026-08-05). Extension from the arming point before a pullback counts.", GroupName = "03. Pullback", Order = 0)]
        public double ImpulseMinAtr30 { get; set; }

        [NinjaScriptProperty, Range(0.2, 5.0)]
        [Display(Name = "Pullback depth (x ATR30)", Description = "CALIBRATED (p30, frozen 2026-08-05). Counter-move from the leg extreme that arms the hunt, measured at EXACTLY ext_i + 2 (Amendment 2).", GroupName = "03. Pullback", Order = 1)]
        public double PullbackMinAtr30 { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Engulfing", GroupName = "04. Triggers", Order = 0)]
        public bool UseEngulfing { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Hammer / shooting star", GroupName = "04. Triggers", Order = 1)]
        public bool UseHammer { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Doji star", Description = "Body <= 0.15x range AND printed at the current pullback extreme.", GroupName = "04. Triggers", Order = 2)]
        public bool UseDojiStar { get; set; }

        [NinjaScriptProperty, Range(0, 20)]
        [Display(Name = "Entry offset (ticks)", Description = "Stop entry this far beyond the trigger candle's extreme.", GroupName = "05. Entry", Order = 0)]
        public int EntryOffsetTicks { get; set; }

        [NinjaScriptProperty, Range(1, 40)]
        [Display(Name = "Entry TTL (30s bars)", Description = "Working life of the entry order, as WALL CLOCK seconds (bars x 30) — a hole in the tape must expire it.", GroupName = "05. Entry", Order = 1)]
        public int EntryTtlBars { get; set; }

        [NinjaScriptProperty, Range(0.05, 3.0)]
        [Display(Name = "Stop buffer (x ATR30)", Description = "CALIBRATED (p80, re-frozen 2026-08-05 under Amendment 2). Stop beyond the pullback extreme.", GroupName = "06. Exits", Order = 0)]
        public double StopBufferAtr30 { get; set; }

        [NinjaScriptProperty, Range(0.5, 6.0)]
        [Display(Name = "Target (R)", GroupName = "06. Exits", Order = 1)]
        public double TargetR { get; set; }

        [NinjaScriptProperty, Range(0.0, 5.0)]
        [Display(Name = "Breakeven at (R)", Description = "Move the stop to the entry fill at this R of unrealized run. 0 = off — and it must STAY off for the mirror gate: PropSim's exit resolution does not model a breakeven stop (delta 3).", GroupName = "06. Exits", Order = 2)]
        public double BreakevenAtR { get; set; }

        [NinjaScriptProperty, Range(0, 40)]
        [Display(Name = "Breakeven offset (ticks)", GroupName = "06. Exits", Order = 3)]
        public int BeOffsetTicks { get; set; }

        [NinjaScriptProperty, Range(1, 100)]
        [Display(Name = "Contracts", GroupName = "07. Size and guards", Order = 0)]
        public int Contracts { get; set; }

        [NinjaScriptProperty, Range(0.0, 20.0)]
        [Display(Name = "Daily loss (R)", Description = "Stop for the day at this loss, in R — never dollars. 0 = off. NT8-side only; PropSim cannot mirror it (accepted delta 1).", GroupName = "07. Size and guards", Order = 1)]
        public double DailyLossR { get; set; }

        [NinjaScriptProperty, Range(0, 2359)]
        [Display(Name = "Flatten (ET HHMM)", Description = "Session backstop. No trigger is accepted whose entry window could still be working at this time.", GroupName = "07. Size and guards", Order = 2)]
        public int FlattenHhmm { get; set; }

        // Presentation switches, not signal dials — they change nothing the
        // mirror compares, so the closed parameter list is intact.
        [NinjaScriptProperty]
        [Display(Name = "Show drawings", GroupName = "08. Diagnostics", Order = 0)]
        public bool ShowDrawings { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Write JSONL corpus", Description = "Append one episode row per state change to Documents\\PullbackZone\\pz_corpus.jsonl. Turn OFF for optimization runs — thousands of rows would drown the mirror gate's Playback corpus.", GroupName = "08. Diagnostics", Order = 1)]
        public bool WriteCorpus { get; set; }
        #endregion
    }
}
