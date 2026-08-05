#!/usr/bin/env python3
"""Dump PullbackZone episodes from the PropSim tape as JSONL (mirror corpus)."""
import argparse, json, sys
from pathlib import Path
import numpy as np
PROPSIM = Path(__file__).resolve().parents[2] / "PropSim"
sys.path.insert(0, str(PROPSIM))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "propsim"))
import tape
from pullback_zone import episodes, PARAMS_DEFAULT

ap = argparse.ArgumentParser()
ap.add_argument("--contract", default="ALL")
ap.add_argument("--start"); ap.add_argument("--end")
ap.add_argument("--out", required=True)
a = ap.parse_args()
t = tape.load_cache(a.contract, a.start, a.end)
t = tape.slice_range(t, a.start, a.end, rth_only=True)
with open(a.out, "w") as f:
    n = 0
    for e in episodes(t, dict(PARAMS_DEFAULT)):
        # trig_ts defaults to -1 for terminal episodes with no trigger (leg_died,
        # no_attempt_left) -- `or` does not fall through here, -1 is truthy.
        anchor = e["trig_ts"] if e["trig_ts"] >= 0 else e["leg_arm_ts"]
        e["date"] = tape.date_str(int(tape.day_index(np.array([anchor]))[0]))
        e["source"] = "propsim"
        f.write(json.dumps(e) + "\n")
        n += 1
print("wrote", n, "episodes to", a.out)
