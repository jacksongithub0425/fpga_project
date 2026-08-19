#!/usr/bin/env python3
"""Priority 4 (B1): adjudicate the shortened segment load against measurement.

B1 replaces `correlation_core`'s compile-time segment load

    SEG_W   = PAR_COLS + MAX_TEMPL_W       232 pixels, every tile, always

with the runtime-required

    seg_len = PAR_COLS + tw - 1            19 pixels at tw=4, 231 at tw=216

`tme_cycle_model.cycles(..., "B1")` PREDICTS what that costs.  This file is
what decides whether the prediction was right, and it does so from a PAIRED
RTL co-simulation: the same twelve-case suite, plus the two direct-test
invocations, run through `template_match_b1_cur/cur` (unmodified RTL) and
`template_match_b1_b1/b1` (shortened load).  The two transaction reports are
compared to each other, not just to the model.

WHY PAIRED, AND WHY IT MATTERS.  B1's claim is a DIFFERENCE.  An absolute
agreement between one measurement and one model number can be produced by two
compensating errors; the difference cannot, because every term the change does
not touch -- the patch load, the reset/norm per output row, the window
statistics, the template staging -- cancels exactly.  The naive expectation is

    naive_delta = rh * th * T * (232 - (tw + 15))
                = rh * th * T * (217 - tw)

"the shortened load costs precisely the pixels it skips", with no fitted
constant in it at all.  THE RTL DOES NOT DO THAT, and finding out is what this
file was for.  The measured saving is short of the naive one by exactly

    T + 1   cycles per (output row, template row)

on 14/14 transactions.  THAT SHAPE IS MEASURED; the mechanism behind it is
not.  The natural reading is a per-tile loop-exit test plus a per-call bound
setup, and it is only a reading: replacing the exit test with a hoisted clamped
bound produced a BYTE-IDENTICAL transaction report (solution `b1b`), which is
evidence against the exit test being the whole of it.  Call it dynamic-bound
overhead.  So the measured tile term is

    tile = T * (2*tw + 41) + 1          rather than the projected T*(2*tw + 40)

and the workload projection is 26.334292108222 s/page rather than the
26.239696410444 that was projected before any B1 RTL existed -- a miss of
0.094595697778, optimistic, in the direction that flattered the change.  Both
are PROJECTIONS over 20,680 modelled trials; neither is a measured page time.

WHAT IS ASSERTED HERE is the measured form, per transaction, plus the residual
structure that justifies it: `naive - measured == T + 1` is checked separately
from the closed form, so a future edit that happened to shift both by the same
amount still fails.  Two corrected constants against fourteen independent
measurements spanning T in {1,2,3,5,6} and tw in {4,16,20,24,100,216} leaves
the form over-determined by twelve.

    python tme_b1_ab.py                    # print the comparison
    python tme_b1_ab.py --assert           # exit 1 on any drift
    python tme_b1_ab.py --json out.json    # machine-readable

Run it with the HLS venv python, from anywhere:

    C:/Users/lychee/Desktop/FPGA/hls/.venv/Scripts/python.exe tme_b1_ab.py

WHAT THIS FILE DOES NOT ESTABLISH.  A cosim latency is a zero-stall RTL
schedule, not a board measurement.  It licenses the CYCLE claim and nothing
about the clock: whether the shortened load still closes 8.000 ns is a routed
implementation result, and whether the page-level 26.334 s/page follows is a
projection over the 20,680-trial workload that no hardware has run.  See
tme_cycle_model.py's evidence hierarchy -- B1 moves from "architectural
projection" to "cycle-validated", one tier, not to "measured".
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tme_cycle_model as M                                    # noqa: E402

HLS = Path("C:/Users/lychee/Desktop/FPGA/hls/template_match")
# One HLS project per variant, `template_match_b1_<sol>`, each holding a single
# solution of the same name.  They used to share one `template_match_b1`, which
# could not be re-run safely: `open_project` without -reset reopens what is on
# disk and `add_files` accumulates, so a re-run stacked sources rather than
# replacing them.  Splitting them means a paired measurement can be rebuilt
# from scratch, which is the only thing that makes a PAIR worth anything.
MANIFEST = HLS / "tb_tme_cases_b1.txt"

# tme_tb.cpp runs run_direct_tests() BEFORE the manifest loop, and two of those
# tests invoke the DUT.  They therefore occupy transactions 0 and 1 of every
# cosim report, ahead of the manifest cases -- the same layout the frozen
# solution1 report has.  Their geometries are hard-coded in the testbench.
DIRECT = [
    ((12, 10, 4, 4), "direct flat-templ-4x4"),
    ((4, 4, 4, 4), "direct min-nonflat dt=15"),
]

# What `cur` costs per tile, and what B1 changes it to.  Both are read off
# tme_cycle_model rather than restated, so a change there cannot silently
# desync from the assertion here.
SEG_W_CUR = 232                 # PAR_COLS + MAX_TEMPL_W
PAR_COLS = 16


def read_transactions(path: Path) -> list[int]:
    """Latencies from a Vitis HLS result.transaction.rpt, in order."""
    if not path.exists():
        raise SystemExit(f"no transaction report at {path}\n"
                         f"  run:  TME_SOLUTION=<sol> vitis-run.bat --mode hls "
                         f"--tcl run_hls_b1.tcl")
    rows = []
    for line in path.read_text().splitlines():
        m = re.match(r"\s*transaction\s+(\d+):\s+(\d+)\s", line)
        if m:
            rows.append((int(m.group(1)), int(m.group(2))))
    rows.sort()
    for i, (idx, _) in enumerate(rows):
        if idx != i:
            raise SystemExit(f"{path}: transaction indices are not 0..n-1")
    return [lat for _, lat in rows]


def read_manifest(path: Path) -> list[tuple]:
    """(pw, ph, tw, th, tag) per case, in the order the testbench runs them."""
    lines = path.read_text().splitlines()
    n = int(lines[0].split()[0])
    out = []
    for row in lines[1:1 + n]:
        f = row.split()
        out.append((int(f[1]), int(f[2]), int(f[3]), int(f[4]), f[-1]))
    return out


def fitted_tile_overhead(measured: int, pw: int, ph: int, tw: int, th: int,
                         seg_len: int) -> float:
    """Solve the model for the per-tile constant this measurement implies.

    The model's tile term is T*(tw + seg_len + k): one MAC pass of tw, one
    segment load of seg_len, and a constant k for pipeline flush and tile
    control.  `cur` has seg_len = 232 and k = 25.  Everything else in the
    formula is untouched by B1, so inverting it isolates k -- which is what
    turns a disagreement into a diagnosis instead of a bare mismatch.
    """
    rw, rh = pw - tw + 1, ph - th + 1
    T = math.ceil(rw / PAR_COLS)
    per_row = (measured - pw * ph - 24 - rh * (5 * rw + 99)) / (rh * th)
    tile = per_row - (3 * tw + 3 * rw + 33)
    return tile / T - tw - seg_len


def build_rows(cur: list[int], b1: list[int] | None) -> list[dict]:
    cases = [(g, tag) for g, tag in DIRECT]
    for pw, ph, tw, th, tag in read_manifest(MANIFEST):
        cases.append(((pw, ph, tw, th), tag))
    if len(cur) != len(cases):
        raise SystemExit(
            f"the cur report has {len(cur)} transactions but the testbench "
            f"runs {len(cases)} DUT invocations ({len(DIRECT)} direct + "
            f"{len(cases) - len(DIRECT)} manifest).  Either the manifest and "
            f"the report are from different runs, or run_direct_tests() "
            f"changed how many times it calls the DUT.")
    if b1 is not None and len(b1) != len(cases):
        raise SystemExit(f"the b1 report has {len(b1)} transactions, not "
                         f"{len(cases)} -- the two runs used different vectors")

    rows = []
    for i, ((pw, ph, tw, th), tag) in enumerate(cases):
        rw, rh = pw - tw + 1, ph - th + 1
        T = math.ceil(rw / PAR_COLS)
        seg_b1 = PAR_COLS + tw - 1
        row = dict(
            i=i, tag=tag, pw=pw, ph=ph, tw=tw, th=th, rw=rw, rh=rh, T=T,
            seg_cur=SEG_W_CUR, seg_b1=seg_b1,
            model_cur=M.cycles(pw, ph, tw, th, "cur"),
            # The PUBLISHED B1 term, read from the model and never rewritten
            # from a measurement.  See fit_bound_overhead.
            model_b1_declared=M.cycles(pw, ph, tw, th, "B1"),
            meas_cur=cur[i],
            naive_delta=rh * th * T * (SEG_W_CUR - seg_b1),
            # The DYNAMIC-BOUND OVERHEAD the measurement found: rh*th*(T + 1).
            # The shape is measured; the mechanism is not.  fit_bound_overhead
            # re-derives the two constants from the data and overwrites this,
            # so a change in the RTL shows up as a fit that no longer reads
            # (1, 1) rather than as a silently retained assumption.
            bound_overhead=rh * th * (T + 1),
        )
        row["cur_residual"] = row["meas_cur"] - row["model_cur"]
        row["k_cur"] = fitted_tile_overhead(row["meas_cur"], pw, ph, tw, th,
                                            SEG_W_CUR)
        if b1 is not None:
            row["meas_b1"] = b1[i]
            row["declared_residual"] = (row["meas_b1"]
                                        - row["model_b1_declared"])
            row["measured_delta"] = row["meas_cur"] - row["meas_b1"]
            row["k_b1"] = fitted_tile_overhead(row["meas_b1"], pw, ph, tw, th,
                                               seg_b1)
            row["speedup"] = row["meas_cur"] / row["meas_b1"]
        rows.append(row)
    return rows


def fit_bound_overhead(rows: list[dict]) -> None:
    """Solve the per-tile and per-call overhead from two transactions.

    naive - measured == rh*th*(per_tile*T + per_call), so two transactions with
    DIFFERENT T pin both constants; every other transaction is then a test of
    them, not an input to them.

    THIS DOES NOT TOUCH `model_b1_declared`, AND THAT IS THE POINT.  An earlier
    revision of this file overwrote the declared model with the value implied by
    the fit before asserting, which made that residual a comparison between a
    measurement and a quantity derived from that same measurement -- zero by
    construction on 14/14, and it would have stayed zero if tme_cycle_model's B1
    variant said something else entirely.  The two quantities are now kept apart
    and checked separately:

      model_b1_declared   tme_cycle_model.cycles(..., "B1") -- the PUBLISHED
                          number, independent of anything measured here.  If
                          this disagrees, the model is wrong (or this solution
                          is not the one the model describes).
      model_b1_fitted     meas_cur minus the two-parameter fit.  If this
                          disagrees, the overhead is not of the form
                          rh*th*(a*T + b) at all.

    They can fail independently, and each failure means something different.
    """
    by_t = {}
    for r in rows:
        short = (r["naive_delta"] - r["measured_delta"]) / (r["rh"] * r["th"])
        by_t.setdefault(r["T"], short)
    ts = sorted(by_t)
    if len(ts) < 2:
        raise SystemExit("the suite has only one tile count; the per-tile and "
                         "per-call overheads cannot be separated")
    t0, t1 = ts[0], ts[-1]
    per_tile = (by_t[t1] - by_t[t0]) / (t1 - t0)
    per_call = by_t[t0] - per_tile * t0
    for r in rows:
        r["fit_per_tile"] = per_tile
        r["fit_per_call"] = per_call
        r["bound_overhead"] = int(round(
            r["rh"] * r["th"] * (per_tile * r["T"] + per_call)))
        r["fitted_delta"] = r["naive_delta"] - r["bound_overhead"]
        r["model_b1_fitted"] = r["meas_cur"] - r["fitted_delta"]
        r["fitted_residual"] = r["meas_b1"] - r["model_b1_fitted"]


def report(rows: list[dict], have_b1: bool, sol: str = "b1") -> int:
    bad = 0
    print("PAIRED RTL CO-SIMULATION -- template_match_b1_{cur,%s}" % sol)
    print()
    print(f"  {'#':>2} {'case':<22} {'patch':>9} {'templ':>8} {'rw':>4} "
          f"{'T':>3} {'seg':>4} {'measured cur':>13} {'model cur':>12} {'d':>4}")
    for r in rows:
        flag = "" if r["cur_residual"] == 0 else "  <-- MISMATCH"
        if r["cur_residual"] != 0:
            bad += 1
        print(f"  {r['i']:>2} {r['tag']:<22} {r['pw']:4d}x{r['ph']:<4d} "
              f"{r['tw']:3d}x{r['th']:<4d} {r['rw']:4d} {r['T']:3d} "
              f"{r['seg_cur']:4d} {r['meas_cur']:13,} {r['model_cur']:12,} "
              f"{r['cur_residual']:4d}{flag}")
    print()
    print(f"  the unmodified RTL reproduces the `cur` model on "
          f"{sum(1 for r in rows if r['cur_residual'] == 0)}/{len(rows)} "
          f"transactions -- this is the control, and it is what makes a `b1` "
          f"residual attributable to the change")

    if not have_b1:
        print()
        print(f"  no `{sol}` solution yet.  Apply the correlation_core edit, "
              f"then:")
        print(f"    TME_SOLUTION={sol} vitis-run.bat --mode hls --tcl "
              f"run_hls_b1.tcl")
        return bad

    print()
    print(f"  TWO INDEPENDENT CHECKS.  `declared` is tme_cycle_model's published "
          f"B1 term; `fitted` is\n  this suite's own two-parameter fit.  They "
          f"are computed from different things and\n  can fail separately -- "
          f"neither is derived from the other.")
    print()
    print(f"  {'#':>2} {'case':<22} {'seg':>5} {'measured b1':>12} "
          f"{'declared':>12} {'d':>5} {'fitted':>12} {'d':>5} {'x':>6}")
    for r in rows:
        flag = ""
        if r["declared_residual"] != 0:
            flag += "  <-- DECLARED MISMATCH"
            bad += 1
        if r["fitted_residual"] != 0:
            flag += "  <-- FITTED MISMATCH"
            bad += 1
        print(f"  {r['i']:>2} {r['tag']:<22} {r['seg_b1']:5d} "
              f"{r['meas_b1']:12,} {r['model_b1_declared']:12,} "
              f"{r['declared_residual']:5d} {r['model_b1_fitted']:12,} "
              f"{r['fitted_residual']:5d} {r['speedup']:6.3f}{flag}")

    n_dec = sum(1 for r in rows if r["declared_residual"] == 0)
    n_fit = sum(1 for r in rows if r["fitted_residual"] == 0)
    print()
    print(f"  DECLARED model (tme_cycle_model B1): {n_dec}/{len(rows)} exact")
    print(f"  FITTED   form  (this suite alone)  : {n_fit}/{len(rows)} exact")
    print(f"  fitted constants: {rows[0]['fit_per_tile']:+g} per tile, "
          f"{rows[0]['fit_per_call']:+g} per call")
    print()
    print(f"  {'case':<22} {'naive delta':>12} {'bound cost':>11} "
          f"{'measured':>11} {'naive-meas':>11} {'T+1':>5}")
    struct_bad = 0
    for r in rows:
        short = r["naive_delta"] - r["measured_delta"]
        want = r["rh"] * r["th"] * (r["T"] + 1)
        if short != want:
            struct_bad += 1
        print(f"  {r['tag']:<22} {r['naive_delta']:12,} "
              f"{r['bound_overhead']:11,} {r['measured_delta']:11,} "
              f"{short:11,} {'ok' if short == want else 'DRIFT':>5}")
    del want
    if struct_bad:
        print(f"  {struct_bad} transaction(s) no longer short by exactly "
              f"rh*th*(T+1) -- the attribution of the overhead to the tile "
              f"exit test\n  and the call setup no longer holds, and the "
              f"corrected form is a bare fit again.")
        bad += struct_bad
    else:
        print(f"  Every transaction is short of the naive saving by exactly "
              f"rh*th*(T + 1).  The SHAPE\n  -- one term per tile, one per "
              f"call -- is what these fourteen measurements pin.\n  The "
              f"MECHANISM is not: a hoisted clamped bound with no exit test at "
              f"all gives a\n  byte-identical report (--sol b1b), so do not "
              f"quote `i >= seg_len` as the cause.\n  Dynamic-bound overhead "
              f"is what it is; B2 and B0b must measure their own.")

    ks_cur = {round(r["k_cur"], 9) for r in rows}
    print()
    print(f"  fitted per-tile overhead k for `cur`: {sorted(ks_cur)}")
    if ks_cur != {25.0}:
        print(f"  the UNMODIFIED core no longer fits its own published tile "
              f"term; nothing below is\n  attributable to B1 until that is "
              f"explained.")
        bad += 1
    else:
        print(f"  one value, and the published one -- the control is intact, "
              f"so the b1 rows above\n  are attributable to the change and "
              f"not to the harness.")

    tot_cur = sum(r["meas_cur"] for r in rows)
    tot_b1 = sum(r["meas_b1"] for r in rows)
    print()
    print(f"  suite total: {tot_cur:,} -> {tot_b1:,} cycles "
          f"({tot_cur / tot_b1:.3f}x on this suite).  The suite is NOT the "
          f"workload;\n  the page figure comes from tme_cycle_model over the "
          f"20,680-trial trace, not from here.")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assert", dest="do_assert", action="store_true",
                    help="exit 1 if any measured value drifts from the model")
    ap.add_argument("--json", type=Path, help="write the comparison as JSON")
    ap.add_argument("--project-root", dest="project_root", type=Path,
                    default=HLS, help="directory holding the per-variant "
                                      "projects template_match_b1_<sol>")
    ap.add_argument("--sol", default="b1",
                    help="which B1-side solution to adjudicate: `b1` is the "
                         "`if (i >= seg_len) break` form, `b1b` the hoisted "
                         "clamped bound.  The control is always `cur`.")
    args = ap.parse_args()

    def rpt(sol):
        return (args.project_root / ("template_match_b1_" + sol) / sol /
                "sim" / "report" / "verilog" / "result.transaction.rpt")

    cur = read_transactions(rpt("cur"))
    b1_path = rpt(args.sol)
    b1 = read_transactions(b1_path) if b1_path.exists() else None

    rows = build_rows(cur, b1)
    if b1 is not None:
        # The overhead the runtime bound costs back is a PROPERTY OF THE FORM,
        # so it is derived from the measurements of the solution actually
        # under test rather than assumed to be the `b1` one.  Fitting it is
        # legitimate only because the residual is then checked for STRUCTURE:
        # a per-tile and a per-call constant must reproduce all 14, which two
        # free parameters cannot do by accident across T in {1,2,3,5,6}.
        fit_bound_overhead(rows)
    bad = report(rows, b1 is not None, args.sol)

    if args.json:
        args.json.write_text(json.dumps(
            dict(rows=rows, have_b1=b1 is not None, failures=bad),
            indent=2), newline="\n")
        print(f"\nwrote {args.json}")

    if args.do_assert:
        if bad:
            print(f"\nFAIL: {bad} discrepancy/ies", file=sys.stderr)
            return 1
        print("\nOK -- three independent checks passed:\n"
              "  1. the control reproduces tme_cycle_model's published `cur` "
              "tile term (k = 25);\n"
              "  2. every measured B1 latency matches the DECLARED model "
              "T*(2*tw + 41) + 1;\n"
              "  3. every shortfall against the naive segment-length "
              "arithmetic fits rh*th*(a*T + b)\n     with (a, b) = (1, 1), "
              "fitted here and not read from the model.\n"
              "Cycle counts only.  No page time is measured by this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
