// PullbackZoneStrategy — 15-minute S/R zone -> 30-second leg -> pullback ->
// reversal-candle trigger. NQ, RTH only (09:30-16:00 ET).
//
// TASK 5 SCOPE: DETECTION ONLY. This build submits NO orders. It draws what it
// sees and appends one JSONL row per episode so research/compare_mirror.py can
// hold it against the PropSim side. Orders, brackets and the flatten arrive in
// Task 6.
//
// THE MIRROR IS THE CONTRACT. propsim/pullback_zone.py is the reviewed,
// calibrated implementation of this pattern; every rule below is a line-by-line
// port of it and where the two disagree THAT FILE WINS. Comments name the
// Python construct each block came from.
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
// Zone folding runs from the PRIMARY branch, not from BarsInProgress 1. When a
// 30s bar and a 15m bar close on the same timestamp, PropSim treats the 15m bar
// as already closed (`searchsorted(tc15, tc30, "right") - 1`), while NT8's
// dispatch order between two series closing at the same instant is not
// something this code should bet on. Folding 15m bars from the 30s branch —
// every bar whose close time is at or before this 30s close, exactly once —
// reproduces PropSim's rule whichever way NT8 dispatches. No lookahead: a
// time-based bar is stamped at its close, so a 15m bar stamped 09:45:00 holds
// only ticks before 09:45:00 and is complete when the 30s bar stamped 09:45:00
// closes.
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
            public bool DeadDrawn;
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
            public int Fills = 0;                // Task 6 increments this on an entry fill
            public int Block = -1;               // no trigger at or before this bar
            public DateTime T0;                  // arm_ts / t0
            public DateTime Day;                 // leg["day"], calendar date
        }

        private readonly List<Zone> _zones = new List<Zone>();
        private readonly List<Cand> _cands = new List<Cand>();
        private Leg _leg;
        private int _zoneSeq;

        private int _zoneBarDone = -1;           // last 15m bar index folded in
        private int _n15;                        // 15m bars folded (the ATR recursion's i)
        private double _atr15, _prev15Close;
        private Series<double> _atr30Series;

        private DateTime _prevDay30 = DateTime.MinValue;
        private DateTime _lastBarTime = DateTime.MinValue;
        private int _cutoffSecs;
        private int _barSecs = 30;

        // Rewind fence. Playback rewinds replay bars the strategy has already
        // logged; stamping every corpus row with the epoch lets Task 7 drop the
        // discarded pass instead of joining a session twice (LatigoBreak lesson
        // — fence by epoch, not by a boolean).
        private int _epoch;

        private static readonly object _corpusLock = new object();
        private string _corpusPath;
        private readonly HashSet<string> _drawTags = new HashSet<string>();

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "PullbackZoneStrategy";
                Description = "Pullback-continuation off a 15m S/R zone, triggered by a reversal candle on 30s bars. Task 5 build: DETECTION ONLY, no orders. Mirror of propsim/pullback_zone.py — see docs/specs/2026-08-05-pullbackzone-design.md.";
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
            _epoch++;
        }

        private string Tag(string t)
        {
            _drawTags.Add(t);
            return t;
        }

        protected override void OnBarUpdate()
        {
            // All work happens on the 30s branch, zone folding included (header
            // note): the 15m branch would tie zone availability to NT8's
            // same-timestamp dispatch order.
            if (BarsInProgress != 0 || CurrentBar < 0)
                return;

            DateTime t = Time[0];
            if (t < _lastBarTime)                    // Playback rewind: hard reset
                ResetAll(true);
            _lastBarTime = t;

            // ATR30 first and unconditionally: PropSim builds the whole atr30
            // array before its loop, so every bar advances the recursion even
            // when the bar is skipped below.
            _atr30Series[0] = ComputeAtr30();

            if (CurrentBars[Zone15Idx] < 0)
                return;

            FoldClosedZoneBars();                    // 15m: pivots, touches, births, deaths, merges

            // A touch does not survive the overnight gap. A zone outlives the
            // session but "price touched this and then left" is one continuous
            // intraday event.
            if (t.Date != _prevDay30)
            {
                _prevDay30 = t.Date;
                foreach (Zone z in _zones)
                    z.Touched = false;
            }

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
        private void FoldClosedZoneBars()
        {
            int last = CurrentBars[Zone15Idx];
            bool folded = false;
            for (int j = _zoneBarDone + 1; j <= last; j++)
            {
                int ago = last - j;
                if (Times[Zone15Idx][ago] > Time[0])
                    break;                           // not closed yet
                FoldZoneBar(ago);
                _zoneBarDone = j;
                folded = true;
            }
            if (folded)
                DrawZones();
        }

        private void FoldZoneBar(int ago)
        {
            double h = Highs[Zone15Idx][ago], l = Lows[Zone15Idx][ago], c = Closes[Zone15Idx][ago];
            DateTime t = Times[Zone15Idx][ago];

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
            if (ago + 2 * k <= CurrentBars[Zone15Idx])
            {
                double ph = Highs[Zone15Idx][ago + k], pl = Lows[Zone15Idx][ago + k];
                bool hiMax = true, loMin = true;
                int hiEq = 0, loEq = 0;
                for (int w = 0; w <= 2 * k; w++)
                {
                    double wh = Highs[Zone15Idx][ago + w], wl = Lows[Zone15Idx][ago + w];
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
                    Corpus(why == "attempts" ? "no_attempt_left" : "leg_died", null,
                           0, 0, 0, a15, a30);
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

            // `block` is this leg's own re-arm gate after a fill (Task 6).
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
            // unresolved, no leg may generate an entry — not even a different
            // leg at a different zone. Inert in this build (nothing submits
            // orders, so the position is always flat); Task 6 adds the
            // working-entry half of the condition.
            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            double off = EntryOffsetTicks * TickSize;
            double entryStop = dir > 0 ? High[0] + off : Low[0] - off;
            double stopPx = _leg.Pull - dir * StopBufferAtr30 * a30;
            double risk = Math.Abs(stopPx - entryStop);
            if (!(risk > 0 && risk <= SanityStopTicks * TickSize))
                return;
            double targetPx = entryStop + dir * TargetR * risk;

            // Task 5 logs the accepted trigger; Task 6 turns it into "filled" or
            // "expired" once an order exists to answer that question.
            Corpus("trigger", kind, entryStop, stopPx, targetPx, a15, a30);

            if (ShowDrawings && ChartControl != null)
            {
                if (dir > 0)
                    Draw.TriangleUp(this, Tag("PZ_T" + CurrentBar), false, 0, Low[0] - 4 * TickSize, Brushes.Lime);
                else
                    Draw.TriangleDown(this, Tag("PZ_T" + CurrentBar), false, 0, High[0] + 4 * TickSize, Brushes.Red);
            }
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
            foreach (Zone z in _zones)
            {
                if (z.Dead && z.DeadDrawn)
                    continue;
                Brush b = z.Dead ? Brushes.Gray : (z.PivotHigh ? Brushes.OrangeRed : Brushes.DodgerBlue);
                Draw.Rectangle(this, Tag("PZ_Z" + z.Id), false,
                    z.BornTime, z.Px + z.HalfW,
                    z.Dead ? z.DiedTime : Time[0], z.Px - z.HalfW,
                    b, b, z.Dead ? 4 : 12);
                if (z.Dead)
                    z.DeadDrawn = true;
            }
        }

        // --- JSONL episode corpus -------------------------------------------

        private static string J(double v)
        {
            return v.ToString("0.######", CultureInfo.InvariantCulture);
        }

        // One episode row. Schema = the Python `_ep` keys in order, plus the
        // `date`/`source` that dump_episodes.py adds on its side, plus `epoch`
        // so a Playback rewind's discarded pass can be dropped by the joiner.
        // Terminal kinds carry no entry geometry (0.0 / -1 / null), same as the
        // Python.
        private void Corpus(string kind, string trigKind, double entryStop, double stopPx,
                            double targetPx, double a15, double a30)
        {
            if (!WriteCorpus || _leg == null)
                return;
            bool trig = trigKind != null;
            StringBuilder sb = new StringBuilder(512);
            sb.Append("{\"kind\":\"").Append(kind).Append("\"")
              .Append(",\"dir\":").Append(_leg.Dir)
              .Append(",\"zone_px\":").Append(J(_leg.Z.Px))
              .Append(",\"zone_touches\":").Append(_leg.Z.Touches)
              .Append(",\"leg_arm_ts\":").Append(_leg.T0.Ticks.ToString(CultureInfo.InvariantCulture))
              .Append(",\"trig_ts\":").Append(trig ? Time[0].Ticks.ToString(CultureInfo.InvariantCulture) : "-1")
              .Append(",\"trig_kind\":").Append(trig ? "\"" + trigKind + "\"" : "null")
              .Append(",\"attempt\":").Append(Math.Min(_leg.Fills + 1, MaxAttemptsPerLeg))
              .Append(",\"entry_stop_px\":").Append(J(entryStop))
              .Append(",\"entry_tick\":-1")
              .Append(",\"pull_ext_px\":").Append(J(_leg.Pull))
              .Append(",\"stop_px\":").Append(J(stopPx))
              .Append(",\"target_px\":").Append(J(targetPx))
              .Append(",\"atr30\":").Append(J(a30))
              .Append(",\"atr15\":").Append(J(a15))
              .Append(",\"date\":\"").Append(Time[0].ToString("yyyy-MM-dd", CultureInfo.InvariantCulture)).Append("\"")
              .Append(",\"source\":\"nt8\"")
              .Append(",\"epoch\":").Append(_epoch)
              .Append(",\"instrument\":\"").Append(Instrument.MasterInstrument.Name).Append("\"}");
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
        [Display(Name = "Max attempts per leg", Description = "Fills allowed per leg. The second is granted only if the first STOPS OUT (Task 6).", GroupName = "02. Leg", Order = 2)]
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
        [Display(Name = "Breakeven at (R)", Description = "Move the stop to entry at this R. 0 = off. Inert until Task 6.", GroupName = "06. Exits", Order = 2)]
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
