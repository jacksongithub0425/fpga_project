#!/usr/bin/env python3
"""Priority 6 (B0b): read the three csynth reports side by side.

Priority 6 asks for "the actual count-pass II and latency from synthesis".
That is here, and so is the question a co-simulation difference cannot answer
on its own: WHICH LOOPS MOVED.

`shadow` is `b2ctl` plus the hoisted pass AND a comparison inside norm_cols.
If the comparison changed norm_cols' schedule, then `shadow - b2ctl` is the
pass plus rh times that change, and reading it as "what the pass costs" would
be wrong.  The loop tables settle it by inspection instead of by assumption:
if every loop b2ctl has is unchanged in shadow, the difference is the new
module and nothing else.  If any of them moved, this prints which.

    python tme_b0b_synth.py            # three-way comparison
    python tme_b0b_synth.py --assert   # exit 1 if a shared loop moved
    python tme_b0b_synth.py --json out.json

WHAT A csynth NUMBER IS.  An estimate from the scheduler, not a measurement:
the II is what the scheduler achieved, the latency bounds come from
LOOP_TRIPCOUNT pragmas (so `max` is a pragma-driven fiction wherever a bound
is runtime), and the timing estimate is pre-route.  The cycle claims in this
priority rest on RTL co-simulation (sw/tme_b0b_ab.py) and the clock claim on
a routed report.  What csynth is authoritative about is the SCHEDULE: II,
pipelining, and which loops exist.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tme_b1_ab import HLS                                      # noqa: E402

SOLUTIONS = ("b2ctl", "shadow", "b0b")


def syn_dir(sol: str) -> Path:
    return HLS / f"template_match_b0b_{sol}" / sol / "syn" / "report"


def parse_report(path: Path) -> dict:
    """Timing, latency, loop table and utilisation from one csynth report."""
    txt = path.read_text(errors="replace")
    out: dict = {"path": str(path), "loops": {}}

    m = re.search(r"\|ap_clk\s*\|\s*([\d.]+) ns\|\s*([\d.]+) ns\|"
                  r"\s*([\d.]+) ns\|", txt)
    if m:
        out["clk_target_ns"] = float(m.group(1))
        out["clk_estimated_ns"] = float(m.group(2))
        out["clk_uncertainty_ns"] = float(m.group(3))

    m = re.search(r"\|\s*(\d+)\|\s*(\d+)\|\s*[\d.]+ \w+\|\s*[\d.]+ \w+\|"
                  r"\s*(\d+)\|\s*(\d+)\|\s*\w+\|", txt)
    if m:
        out["latency_min"] = int(m.group(1))
        out["latency_max"] = int(m.group(2))

    # The loop table.  Rows look like
    #   |- slide_v      |   496|  4651106065| 495 ~ 15299691|  -|  -| 1 ~ 304| no|
    #   |o scan_slide   |    ...           |              1|  1|  1|1 ~ 816|yes|
    # ONE table, not everything after the marker.  The utilisation tables
    # further down have the same pipe layout and would otherwise parse as
    # loops named "Memory" and "Total"; a report reader that invents loops is
    # worse than one that finds none.  Stop at the table's closing border,
    # counting the three `+---+` rules a Vitis table has.
    sec = txt.split("* Loop:")
    if len(sec) > 1:
        rules = 0
        for line in sec[1].splitlines():
            if line.strip().startswith("+-"):
                rules += 1
                if rules >= 3:
                    break
                continue
            m = re.match(r"\s*\|([-+o ]*)\s*([A-Za-z_][\w]*)\s*\|(.*)\|\s*$",
                         line)
            if not m:
                continue
            depth = len(m.group(1).replace("|", "").rstrip()) - 1
            name = m.group(2)
            cells = [c.strip() for c in m.group(3).split("|")]
            if len(cells) < 7 or name in ("Loop Name",):
                continue
            out["loops"][name] = {
                "depth": max(depth, 0),
                "latency_min": cells[0],
                "latency_max": cells[1],
                "iteration_latency": cells[2],
                "ii_achieved": cells[3],
                "ii_target": cells[4],
                "trip_count": cells[5],
                "pipelined": cells[6],
            }

    m = re.search(r"\|Total\s*\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|",
                  txt)
    if m:
        out["bram18k"], out["dsp"], out["ff"], out["lut"] = (
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)))
    return out


def collect(sol: str) -> dict | None:
    d = syn_dir(sol)
    if not (d / "tme_top_csynth.rpt").exists():
        return None
    res = {"top": parse_report(d / "tme_top_csynth.rpt"), "modules": {}}
    for rpt in sorted(d.glob("*_csynth.rpt")):
        if rpt.name == "tme_top_csynth.rpt":
            continue
        res["modules"][rpt.name[:-len("_csynth.rpt")]] = parse_report(rpt)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assert", dest="strict", action="store_true")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    data = {s: collect(s) for s in SOLUTIONS}
    have = [s for s in SOLUTIONS if data[s]]
    if not have:
        raise SystemExit(
            "no csynth reports found.  Build them with:\n"
            "  TME_B0B_SOLUTION={b2ctl|shadow|b0b} vitis-run.bat --mode hls "
            "--tcl run_hls_b0b.tcl")

    print("PRIORITY 6 (B0b) — synthesis, three solutions")
    print("=" * 74)
    print("present: " + ", ".join(have))
    print()

    print("TOP LEVEL")
    print("-" * 74)
    hdr = f"{'':<22}" + "".join(f"{s:>16}" for s in have)
    print(hdr)
    for key, label in (("clk_estimated_ns", "timing estimate ns"),
                       ("latency_min", "latency min"),
                       ("bram18k", "BRAM18K"), ("dsp", "DSP"),
                       ("ff", "FF"), ("lut", "LUT")):
        row = f"{label:<22}"
        for s in have:
            v = data[s]["top"].get(key)
            row += f"{v if v is not None else '-':>16}"
        print(row)
    print()
    print("  csynth's `latency max` is omitted: it is driven by")
    print("  LOOP_TRIPCOUNT pragmas wherever a bound is runtime, so it is a")
    print("  pragma-derived figure and not comparable across a source change")
    print("  that alters loop structure.  The cycle claims come from cosim.")
    print()

    print("MODULES")
    print("-" * 74)
    mods = sorted({m for s in have for m in data[s]["modules"]})
    for m in mods:
        where = [s for s in have if m in data[s]["modules"]]
        print(f"  {m:<28} present in: {', '.join(where)}")
    print()

    print("LOOP SCHEDULES")
    print("-" * 74)
    all_loops: dict[str, dict] = {}
    for s in have:
        for scope, rep in ([("tme_top", data[s]["top"])]
                           + [(k, v) for k, v in data[s]["modules"].items()]):
            for name, info in rep["loops"].items():
                all_loops.setdefault(f"{scope}/{name}", {})[s] = info
    moved, containers_changed = [], []
    for key in sorted(all_loops):
        per = all_loops[key]
        print(f"  {key}")
        for s in have:
            if s not in per:
                print(f"      {s:<8} —")
                continue
            i = per[s]
            print(f"      {s:<8} II {i['ii_achieved']:>4}  "
                  f"iter-lat {i['iteration_latency']:>16}  "
                  f"trip {i['trip_count']:>10}  pipelined {i['pipelined']}")
        shared = [s for s in have if s in per]
        if len(shared) > 1:
            ref = per[shared[0]]
            # A NON-PIPELINED loop's iteration latency is the sum of whatever
            # is inside it, so slide_v and accum_rows MUST change when a pass
            # is added or removed inside them -- that is the change, not a
            # side effect of it.  Reporting those as "moved" would drown the
            # only question worth asking: did a PIPELINED LEAF loop, which
            # neither solution edits, get rescheduled?  norm_cols is the one
            # that matters here, because the shadow's comparison lives in it.
            container = ref["ii_achieved"] in ("-", "")
            for s in shared[1:]:
                if (per[s]["ii_achieved"] != ref["ii_achieved"]
                        or per[s]["iteration_latency"]
                        != ref["iteration_latency"]):
                    (containers_changed if container else moved).append(
                        (key, shared[0], s))
    print()

    print("DID ANY SHARED PIPELINED LOOP MOVE?")
    print("-" * 74)
    if len(have) < 2:
        # With one report there is no "shared loop", so "no loop moved" would
        # be true and worthless.  Say which it is.
        print("  UNANSWERED — only one solution has been synthesised, so no")
        print("  loop is shared.  Build the others before reading this.")
    elif not moved:
        print("  No.  Every PIPELINED loop present in more than one solution")
        print("  has the same achieved II and the same iteration latency in")
        print("  all of them, so a latency difference between solutions is")
        print("  the loops that EXIST in one and not the other — which is")
        print("  what makes the co-simulation difference attributable.")
        print()
        print("  In particular norm_cols is unchanged, so the shadow's")
        print("  per-position comparison did not reschedule the loop it sits")
        print("  in, and `shadow - b2ctl` is the added pass rather than the")
        print("  pass plus rh times a norm_cols change.")
    else:
        for key, a, b in moved:
            print(f"  {key}: differs between {a} and {b}")
        print()
        print("  A shared PIPELINED loop that moved means a solution")
        print("  difference is not only the added or removed pass.  Quote the")
        print("  co-simulation difference with that named, or measure around")
        print("  it.")
    if containers_changed:
        print()
        print("  Non-pipelined CONTAINER loops that changed, as they must:")
        for key, a, b in containers_changed:
            print(f"    {key}: {a} vs {b}")
        print("  Their iteration latency is the sum of what is inside them,")
        print("  so a pass added to or removed from slide_v changes them by")
        print("  definition.  That is the change being measured.")
    if args.json:
        args.json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    if args.strict and moved:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
