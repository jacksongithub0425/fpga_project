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

IT DID MOVE, AND THIS FILE USED TO SAY IT DID NOT.  Until 2026-08-20 the
comparison read two columns of the loop table -- achieved II and ITERATION
latency -- and both are identical in all three solutions for norm_cols (II 4,
iteration latency 98).  Its actual LATENCY is not:

    norm_cols   b2ctl  97 ~ 3361      shadow  99 ~ 3363      b0b  97 ~ 3361
    module      b2ctl  99 ~ 3363      shadow 101 ~ 3365      b0b  99 ~ 3363

A pipelined loop's latency is not a function of the two columns that were
being compared, so checking those two and reporting "no loop moved" was a
check that could not fail for this defect class.  The shadow's comparison
costs +2 cycles per norm_cols call, norm_cols runs once per OUTPUT ROW, and
therefore `shadow - b2ctl` overstates the pass by 2*rh while `b0b - shadow`
understates the removal by the same 2*rh.

WHAT THAT DOES AND DOES NOT INVALIDATE.  It cancels in the NET: b2ctl and b0b
have identical norm_cols schedules, so `b0b - b2ctl` -- the frozen law -- is
uncontaminated.  Only the two HALVES are affected, in equal and opposite
amounts.  See sw/tme_b0b_ab.py, which no longer publishes either half as a
measurement of the pass or of the removal.

The comparison now reads all four scheduling columns plus the module wrapper's
own latency, and --assert holds the result against a RECORDED inventory of the
one known difference rather than against "none".  A new difference fails; the
known one changing size fails; the known one disappearing fails too, because
that would mean the prose above is stale.

    python tme_b0b_synth.py            # three-way comparison
    python tme_b0b_synth.py --assert   # exit 1 on any UNRECORDED schedule move
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

# THE INVENTORY OF SHARED-LOOP MOVEMENT THIS BUILD IS KNOWN TO HAVE.
#
# (loop, solution A, solution B, column) -> (value in A, value in B)
#
# One entry, and it is the shadow's comparison rescheduling the loop it sits
# in.  The two rows are the same fact read on the two bounds of the same loop:
# norm_cols is 2 cycles longer in `shadow` than in either of the solutions
# without a comparison in it.  The pair (b2ctl, b0b) is deliberately ABSENT --
# those two agree, which is what keeps the net law clean.
#
# This is a record of a measurement, not a tolerance.  Do not add an entry to
# make a gate pass; an entry here is a statement that the difference has been
# looked at, explained, and accounted for downstream.
KNOWN_SCHEDULE_MOVES = {
    ("tme_top_Pipeline_norm_cols/norm_cols", "b2ctl", "shadow",
     "latency_min"): ("97", "99"),
    ("tme_top_Pipeline_norm_cols/norm_cols", "b2ctl", "shadow",
     "latency_max"): ("3361", "3363"),
}


def _key(entry) -> tuple:
    """(loop, a, b, column) from a (loop, a, b, column, va, vb) difference."""
    return (entry[0], entry[1], entry[2], entry[3])


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

    # The module's own latency row.  The last cell is the pipeline TYPE, and it
    # is not always one word: a pipelined module says "loop pipeline stp", so
    # `\w+` matched only the unpipelined ones and every pipelined submodule
    # silently parsed as having no latency at all.  That mattered: the module
    # wrapper is the second, independent view of the norm_cols difference this
    # file exists to detect, and it was reading as absent.
    m = re.search(r"\|\s*(\d+)\|\s*(\d+)\|\s*[\d.]+ \w+\|\s*[\d.]+ \w+\|"
                  r"\s*(\d+)\|\s*(\d+)\|\s*[\w ]+\|", txt)
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
                  f"lat {i['latency_min']:>8} ~ {i['latency_max']:>10}  "
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
            # ALL FOUR SCHEDULING COLUMNS.  Comparing only II and iteration
            # latency is what let norm_cols through: a pipelined loop's own
            # latency is not determined by those two, and norm_cols moved by
            # +2 on both bounds with both of them unchanged.
            for s in shared[1:]:
                for col in ("ii_achieved", "iteration_latency",
                            "latency_min", "latency_max"):
                    if per[s][col] != ref[col]:
                        (containers_changed if container else moved).append(
                            (key, shared[0], s, col, ref[col], per[s][col]))
    print()

    print("SHARED MODULE LATENCY")
    print("-" * 74)
    # The module wrapper is a SECOND view of the same question, read from a
    # different report.  norm_cols shows the +2 in both, which is why it can be
    # quoted as a property of the design rather than of one parser.
    mod_moved = []
    for m in mods:
        per = {s: data[s]["modules"][m] for s in have if m in data[s]["modules"]}
        lat = {s: (r.get("latency_min"), r.get("latency_max"))
               for s, r in per.items()}
        if len(per) < 2 or all(v == (None, None) for v in lat.values()):
            continue
        vals = set(lat.values())
        flag = "DIFFERS" if len(vals) > 1 else "same   "
        print(f"  {flag}  {m:<44} "
              + "  ".join(f"{s}={lat[s][0]}~{lat[s][1]}" for s in per))
        if len(vals) > 1:
            base = [s for s in have if s in per][0]
            for s in list(per)[1:]:
                if lat[s] != lat[base]:
                    mod_moved.append((m, base, s, lat[base], lat[s]))
    print()

    print("DID ANY SHARED PIPELINED LOOP MOVE?")
    print("-" * 74)
    rc = 0
    if len(have) < 2:
        # With one report there is no "shared loop", so "no loop moved" would
        # be true and worthless.  Say which it is.
        print("  UNANSWERED — only one solution has been synthesised, so no")
        print("  loop is shared.  Build the others before reading this.")
        if args.strict:
            rc = 1
    else:
        # RECORDED, NOT ASSUMED.  The gate used to demand that nothing moved,
        # which was a claim about the design; it is now an inventory of what
        # DID move, which is a claim about this build.  Anything outside the
        # inventory fails, and so does the inventory drifting -- if norm_cols
        # stops differing by exactly +2 the narrative in this file and in
        # tme_b0b_ab.py is stale and must be rewritten before anyone quotes it.
        unrecorded = [e for e in moved if _key(e) not in KNOWN_SCHEDULE_MOVES]
        wrong_size = [e for e in moved
                      if _key(e) in KNOWN_SCHEDULE_MOVES
                      and KNOWN_SCHEDULE_MOVES[_key(e)] != (e[4], e[5])]
        seen = {_key(e) for e in moved}
        vanished = [k for k in KNOWN_SCHEDULE_MOVES
                    if k[1] in have and k[2] in have and k not in seen]

        if moved:
            print("  YES.  Shared PIPELINED leaf loops that differ:")
            for key, a, b, col, va, vb in moved:
                tag = "recorded" if _key((key, a, b, col, va, vb)) \
                    in KNOWN_SCHEDULE_MOVES else "UNRECORDED"
                print(f"    {key:<38} {col:<18} {a}={va} {b}={vb}   [{tag}]")
            print()
            print("  WHAT THAT MEANS FOR THE HALVES.  norm_cols is the loop the")
            print("  shadow's per-position comparison lives in, and it runs once")
            print("  per OUTPUT ROW.  So `shadow - b2ctl` is the hoisted pass")
            print("  PLUS 2*rh, and `b0b - shadow` is the removal MINUS 2*rh.")
            print("  Neither half is the thing its name says it is.")
            print()
            print("  WHAT IT DOES NOT TOUCH.  b2ctl and b0b agree on norm_cols,")
            print("  so `b0b - b2ctl` carries none of this.  The frozen net law")
            print("  is unaffected; only its split is.")
        else:
            print("  No.  Every PIPELINED loop present in more than one solution")
            print("  matches on all four scheduling columns, so a latency")
            print("  difference between solutions is the loops that EXIST in one")
            print("  and not the other.")
        if unrecorded:
            print()
            print("  UNRECORDED MOVES — the narrative here does not cover these:")
            for key, a, b, col, va, vb in unrecorded:
                print(f"    {key} {col}: {a}={va} {b}={vb}")
            rc = 1
        if wrong_size:
            print()
            print("  A RECORDED MOVE CHANGED SIZE — re-read the prose before")
            print("  quoting any half:")
            for key, a, b, col, va, vb in wrong_size:
                want = KNOWN_SCHEDULE_MOVES[_key((key, a, b, col, va, vb))]
                print(f"    {key} {col}: now {a}={va} {b}={vb}, "
                      f"recorded {want}")
            rc = 1
        if vanished:
            print()
            print("  A RECORDED MOVE IS GONE.  That is good news and stale")
            print("  documentation at the same time; the halves may now be")
            print("  separable and nothing here says so yet:")
            for k in vanished:
                print(f"    {k[0]} {k[3]}: {k[1]} vs {k[2]}")
            rc = 1
    if mod_moved:
        print()
        print("  Module wrappers that differ (the same question, read from the")
        print("  submodule reports rather than the top-level loop table):")
        for m, a, b, la, lb in mod_moved:
            print(f"    {m}: {a}={la[0]}~{la[1]}  {b}={lb[0]}~{lb[1]}")
    if containers_changed:
        print()
        print("  Non-pipelined CONTAINER loops that changed, as they must:")
        for key, a, b, col, va, vb in containers_changed:
            print(f"    {key} {col}: {a}={va} {b}={vb}")
        print("  Their iteration latency is the sum of what is inside them,")
        print("  so a pass added to or removed from slide_v changes them by")
        print("  definition.  That is the change being measured.")
    if args.json:
        args.json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    return rc if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
