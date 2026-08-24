#!/usr/bin/env python3
"""Priority 5 (B2): adjudicate horizontal overlap reuse against measurement.

B2 stops re-reading the whole segment for every tile.  Tile 0 still loads
seg_len = PAR_COLS + tw - 1 pixels; every later tile slides the overlap down by
PAR_COLS and refills only the PAR_COLS pixels that are new.

THE CONTROL IS `b1`, NOT `cur`, and that is the whole design of this file.  B2
is B1 plus the reuse, so pairing it against the unmodified core would fold two
independent changes into one difference and leave neither attributable.  Against
`b1` the only thing that moved is how seg gets its contents; every other term --
the patch load, the reset/norm per output row, the window statistics, the
template staging, the MAC schedule, the writeback -- cancels exactly.

    naive_delta = rh * th * (T - 1) * (seg_len - PAR_COLS)
                = rh * th * (T - 1) * (tw - 1)

"the reuse saves precisely the pixels it does not re-read", with no fitted
constant in it.  Whether the RTL does that is the question, and B1 is the
reason to doubt it: B1's equally obvious arithmetic was short by T + 1 cycles
per (output row, template row), optimistic, in the direction that flattered the
change.  Nothing about that result licenses assuming B2 pays the same, or pays
only the same -- so this file fits the shortfall's shape from the B2 data alone
and reports it, rather than testing a borrowed constant.

FOUR CHECKS, AND THEY FAIL FOR DIFFERENT REASONS
------------------------------------------------
  1. THE CONTROL IS INTACT.  Every `b1` transaction must still reproduce
     tme_cycle_model's published B1 term, T*(2*tw + 41) + 1.  If it does not,
     the pair is not a pair and nothing below is attributable to B2.
  2. THE DECLARED MODEL.  Every measured `b2` latency must equal
     tme_cycle_model.cycles(..., "B2") -- the PUBLISHED number, computed from
     (pw, ph, tw, th) with zero free parameters.  A failure here means the model
     is wrong, or this solution is not the one it describes.
  3. THE FITTED SHAPE.  The shortfall against naive_delta must be
     rh*th*(a*T + b) for one (a, b) across every transaction.  Two free
     parameters, solved from two transactions with different T; every other
     transaction is then a test of them rather than an input to them.
  4. THE DECOMPOSITION (--assert only, needs the pre-B2 snapshot).  The miss
     against the PRE-RTL PROJECTION must be rh*th*(3T - 1), and 3T - 1 must be
     (T + 1) + 2*(T - 1) -- B1's correction plus an ADDITIONAL term, not
     instead of it.  At T = 1 the second term is zero and the whole miss must
     be B1's (T + 1).  This check exists because the natural summary of check 3
     -- "the shortfall is 2*(T-1), not B1's T+1" -- reads as "B1's overhead did
     not recur", and that is false: 2*(T-1) is measured against control-naive,
     a baseline that ALREADY contains B1's term.  See predict().

Checks 2 and 3 are computed from different things and neither is derived from
the other -- the mistake tme_b1_ab.py records having made once, where the
declared model was overwritten from the fit and the residual became zero by
construction.  `--negative-control` re-proves the independence here by
perturbing the declared model and confirming that check 2 fails while check 3
still passes.

COUNTING THE CONSTRAINTS.  Under a fit whose residual depends only on T, the
transactions sharing a T restate one equation.  The suite spans T in
{1, 2, 3, 5, 6}: five independent equations against two free parameters, so
THREE surplus constraints -- not thirteen.  Of the rest, eight sit at an
already-constrained T but a different geometry and so test geometry invariance;
one is a true repeat (transactions 12 and 13 share 47x21 / 16x12).  Check 2
does not share this weakness: zero free parameters over thirteen distinct
geometries.

    python tme_b2_ab.py                     # print the comparison
    python tme_b2_ab.py --assert            # exit 1 on any drift
    python tme_b2_ab.py --negative-control  # prove checks 2 and 3 are separate
    python tme_b2_ab.py --predict           # reconstruct the two pre-RTL baselines
    python tme_b2_ab.py --json out.json

Run it with the HLS venv python, from anywhere.

WHAT THIS FILE DOES NOT ESTABLISH.  A cosim latency is a zero-stall RTL
schedule.  It licenses the CYCLE claim and nothing about the clock -- whether
the shift register still closes 8.000 ns is a routed implementation result --
and nothing about a page, since the s/page figures sum the term over 20,680
modelled trials that no hardware has run.  See tme_cycle_model.py's evidence
hierarchy.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tme_cycle_model as M                                    # noqa: E402
# The report reader, the case manifest and the two direct-test geometries are
# B1's and are reused rather than restated: a second copy is a second thing to
# keep in step, and the transaction layout is a property of tme_tb.cpp, not of
# either variant.
from tme_b1_ab import (DIRECT, HLS, MANIFEST, PAR_COLS,        # noqa: E402
                       read_manifest, read_transactions)


def seg_len(tw: int) -> int:
    """Pixels tile 0 loads.  Later tiles refill PAR_COLS of these."""
    return PAR_COLS + tw - 1


def build_rows(ctl: list[int], b2: list[int] | None,
               ctl_variant: str) -> list[dict]:
    cases = [(g, tag) for g, tag in DIRECT]
    for pw, ph, tw, th, tag in read_manifest(MANIFEST):
        cases.append(((pw, ph, tw, th), tag))
    if len(ctl) != len(cases):
        raise SystemExit(
            f"the control report has {len(ctl)} transactions but the testbench "
            f"runs {len(cases)} DUT invocations ({len(DIRECT)} direct + "
            f"{len(cases) - len(DIRECT)} manifest).  Either the manifest and "
            f"the report are from different runs, or run_direct_tests() "
            f"changed how many times it calls the DUT.")
    if b2 is not None and len(b2) != len(cases):
        raise SystemExit(f"the b2 report has {len(b2)} transactions, not "
                         f"{len(cases)} -- the two runs used different vectors")

    rows = []
    for i, ((pw, ph, tw, th), tag) in enumerate(cases):
        rw, rh = pw - tw + 1, ph - th + 1
        T = math.ceil(rw / PAR_COLS)
        row = dict(
            i=i, tag=tag, pw=pw, ph=ph, tw=tw, th=th, rw=rw, rh=rh, T=T,
            seg=seg_len(tw),
            model_ctl=M.cycles(pw, ph, tw, th, ctl_variant),
            model_b2_declared=M.cycles(pw, ph, tw, th, "B2"),
            meas_ctl=ctl[i],
            # Pixels the reuse does not re-read: (T - 1) later tiles, each
            # skipping seg_len - PAR_COLS = tw - 1 of them.
            naive_delta=rh * th * (T - 1) * (tw - 1),
        )
        row["ctl_residual"] = row["meas_ctl"] - row["model_ctl"]
        if b2 is not None:
            row["meas_b2"] = b2[i]
            row["declared_residual"] = row["meas_b2"] - row["model_b2_declared"]
            row["measured_delta"] = row["meas_ctl"] - row["meas_b2"]
            row["shortfall"] = row["naive_delta"] - row["measured_delta"]
            row["speedup"] = row["meas_ctl"] / row["meas_b2"]
        rows.append(row)
    return rows


def fit_reuse_overhead(rows: list[dict]) -> tuple[float, float]:
    """Solve per-tile and per-call overhead from the shortfall.

    shortfall == rh*th*(a*T + b), so two transactions with DIFFERENT T pin both
    constants and every other transaction becomes a test of them.  This does
    NOT touch `model_b2_declared`: the two quantities stay independent so that
    they can fail separately and mean different things when they do.
    """
    by_t: dict[int, float] = {}
    for r in rows:
        by_t.setdefault(r["T"], r["shortfall"] / (r["rh"] * r["th"]))
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
        r["fitted_overhead"] = int(round(
            r["rh"] * r["th"] * (per_tile * r["T"] + per_call)))
        r["fitted_delta"] = r["naive_delta"] - r["fitted_overhead"]
        r["model_b2_fitted"] = r["meas_ctl"] - r["fitted_delta"]
        r["fitted_residual"] = r["meas_b2"] - r["model_b2_fitted"]
    return per_tile, per_call


def implied_tile_term(r: dict) -> float:
    """The per-(output row, template row) tile term this measurement implies.

    Inverts L = pw*ph + 24 + rh*(5*rw + 99) + rh*th*(tile + 3*tw + 3*rw + 33).
    Reported so a mismatch is a diagnosis rather than a bare number.
    """
    per_row = ((r["meas_b2"] - r["pw"] * r["ph"] - 24
                - r["rh"] * (5 * r["rw"] + 99)) / (r["rh"] * r["th"]))
    return per_row - (3 * r["tw"] + 3 * r["rw"] + 33)


def declared_tile_term(r: dict) -> float:
    """The tile term tme_cycle_model's B2 variant implies at this geometry.

    Recovered by inverting the same formula implied_tile_term inverts, so the
    two are directly comparable and neither restates a term as a literal.
    """
    per_row = ((r["model_b2_declared"] - r["pw"] * r["ph"] - 24
                - r["rh"] * (5 * r["rw"] + 99)) / (r["rh"] * r["th"]))
    return per_row - (3 * r["tw"] + 3 * r["rw"] + 33)


def report(rows: list[dict], have_b2: bool, sol: str, ctl: str) -> int:
    bad = 0
    print(f"PAIRED RTL CO-SIMULATION -- template_match_b1_{{{ctl},{sol}}}")
    print()
    print(f"  {'#':>2} {'case':<22} {'patch':>9} {'templ':>8} {'rw':>4} "
          f"{'T':>3} {'seg':>4} {'measured ' + ctl:>13} {'model ' + ctl:>12} "
          f"{'d':>4}")
    for r in rows:
        flag = "" if r["ctl_residual"] == 0 else "  <-- MISMATCH"
        if r["ctl_residual"] != 0:
            bad += 1
        print(f"  {r['i']:>2} {r['tag']:<22} {r['pw']:4d}x{r['ph']:<4d} "
              f"{r['tw']:3d}x{r['th']:<4d} {r['rw']:4d} {r['T']:3d} "
              f"{r['seg']:4d} {r['meas_ctl']:13,} {r['model_ctl']:12,} "
              f"{r['ctl_residual']:4d}{flag}")
    print()
    n_ctl = sum(1 for r in rows if r["ctl_residual"] == 0)
    print(f"  the `{ctl}` control reproduces its published term on "
          f"{n_ctl}/{len(rows)} transactions -- this is what makes a `{sol}` "
          f"residual\n  attributable to the overlap reuse and not to the "
          f"harness")

    if not have_b2:
        print()
        print(f"  no `{sol}` solution yet:")
        print(f"    TME_SOLUTION={sol} vitis-run.bat --mode hls --tcl "
              f"run_hls_b1.tcl")
        return bad

    print()
    print(f"  TWO INDEPENDENT CHECKS.  `declared` is tme_cycle_model's "
          f"published B2 term; `fitted`\n  is this suite's own two-parameter "
          f"fit of the shortfall.  Neither is derived from\n  the other, and "
          f"they fail for different reasons.")
    print()
    print(f"  {'#':>2} {'case':<22} {'measured ' + sol:>12} {'declared':>12} "
          f"{'d':>6} {'fitted':>12} {'d':>6} {'x':>6}")
    for r in rows:
        flag = ""
        if r["declared_residual"] != 0:
            flag += "  <-- DECLARED MISMATCH"
            bad += 1
        if r["fitted_residual"] != 0:
            flag += "  <-- FITTED MISMATCH"
            bad += 1
        print(f"  {r['i']:>2} {r['tag']:<22} {r['meas_b2']:12,} "
              f"{r['model_b2_declared']:12,} {r['declared_residual']:6d} "
              f"{r['model_b2_fitted']:12,} {r['fitted_residual']:6d} "
              f"{r['speedup']:6.3f}{flag}")

    n_dec = sum(1 for r in rows if r["declared_residual"] == 0)
    n_fit = sum(1 for r in rows if r["fitted_residual"] == 0)
    print()
    print(f"  DECLARED model (tme_cycle_model B2): {n_dec}/{len(rows)} exact")
    print(f"  FITTED   form  (this suite alone)  : {n_fit}/{len(rows)} exact")
    print(f"  fitted constants: {rows[0]['fit_per_tile']:+g} per tile, "
          f"{rows[0]['fit_per_call']:+g} per call")

    print()
    print(f"  {'case':<22} {'naive saving':>13} {'measured':>12} "
          f"{'shortfall':>11} {'fit':>11} {'ok':>5}")
    struct_bad = 0
    for r in rows:
        want = r["fitted_overhead"]
        ok = r["shortfall"] == want
        if not ok:
            struct_bad += 1
        print(f"  {r['tag']:<22} {r['naive_delta']:13,} "
              f"{r['measured_delta']:12,} {r['shortfall']:11,} {want:11,} "
              f"{'ok' if ok else 'DRIFT':>5}")
    if struct_bad:
        print(f"  {struct_bad} transaction(s) do not fit one (a, b) -- the "
              f"shortfall is not of the form\n  rh*th*(a*T + b) at all, and "
              f"the corrected term is a bare fit rather than a shape.")
        bad += struct_bad

    print()
    # The tile term each measurement implies, minus the one the model declares.
    # Read off the model rather than restated, so this line cannot go stale the
    # way a literal term does when the model is corrected.
    ks = sorted({round(implied_tile_term(r) - declared_tile_term(r), 6)
                 for r in rows})
    print(f"  implied tile term minus tme_cycle_model's declared B2 term: {ks}")
    print("  ({0.0} means the model already carries the measured term; any "
          "other single")
    print("  value means the correction is one constant shape across every "
          "geometry)")

    tot_c = sum(r["meas_ctl"] for r in rows)
    tot_b = sum(r["meas_b2"] for r in rows)
    print()
    print(f"  suite total: {tot_c:,} -> {tot_b:,} cycles "
          f"({tot_c / tot_b:.3f}x on this suite).  The suite is NOT the "
          f"workload;\n  the page figure comes from tme_cycle_model over the "
          f"20,680-trial trace, not from here.")
    return bad


# The model file AS IT STOOD BEFORE B2 WAS MEASURED.  --predict reads its
# `cycles(..., "B2")`, not the live model's.
#
# THIS INDIRECTION IS THE WHOLE POINT AND IT WAS ADDED AFTER A NEAR MISS.  The
# first version of --predict read the LIVE tme_cycle_model, which was true
# right up until the model was corrected with the measured term -- at which
# moment the "prediction" silently became the answer and every difference it
# printed collapsed to zero.  A mode whose claim quietly stops holding is worse
# than no mode: it still prints a table.  The withdrawn projection now comes
# from a file that cannot be updated, because it is a snapshot.
PRE_B2_MODEL = "logs/b2_20260819/tme_cycle_model.py.pre_b2"


def _load_pre_b2():
    """Import the retained pre-measurement model under its own module name."""
    import importlib.util
    root = HLS.parents[1]
    path = root / PRE_B2_MODEL
    if not path.exists():
        raise SystemExit(
            f"no pre-B2 model snapshot at {path}\n"
            f"  --predict deliberately does NOT read the live model: once the\n"
            f"  measured term is in it, the 'prediction' becomes the answer.\n"
            f"  The snapshot is pinned in logs/b2_20260819/MANIFEST.sha256;\n"
            f"  restore it with `tme_b2_manifest.py --mirror` or from git.")
    # An explicit SourceFileLoader is required: the snapshot is named
    # `.py.pre_b2` so nothing can import it by accident, and importlib cannot
    # infer a loader from an unregistered suffix.
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("tme_cycle_model_pre_b2", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def predict(ctl: list[int], ctl_name: str, ctl_variant: str) -> int:
    """The two candidate B2 terms, from the PRE-B2 model and the control report.

    THIS READS NO b2 REPORT.  "The projection was optimistic by X" is worth
    nothing if the projection was written after seeing the answer, and this
    function CANNOT see the answer: its inputs are the retained pre-measurement
    copy of tme_cycle_model and the retained `b1` transaction report, so anyone
    can recompute these numbers at any time and get the same ones.

    WHAT DOES AND DOES NOT PREDATE THE RTL IN REPOSITORY ANCESTRY.

      * The PRE-RTL PROJECTION is retained in ancestor commit e762cbf:
        tile = T*(tw + 41) + (tw - 1), with the 20.175432 page figure beside
        it.  The B2 source/build appears later in the retained history.  This
        establishes repository ordering only.  An unsigned, unpushed commit is
        not an external timestamp, and its author/commit date does not prove
        when a third party could first observe the content.
      * THIS TOOL AND ITS SNAPSHOT ARE NOT.  logs/b2_20260819/
        tme_cycle_model.py.pre_b2 was written at 19:19:15 and PREDICTION.txt at
        19:19:23, while the b2 transaction report already existed at 19:10:13.
        Both POSTDATE the measurement.  The snapshot is a reconstruction of the
        pre-RTL model, and what makes it usable is not its mtime but that its
        CONTENT is checkable against e762cbf and that --assert refuses to run
        if it ever starts carrying the measured term.
      * THE CONTROL-NAIVE CANDIDATE DOES NOT PREDATE THE RESULT.  It is
        computed here, by this file, after the fact.  It is a useful BASELINE
        -- "what B1's measured term plus perfect reuse would have given" -- and
        it is not a prediction anyone made in advance.

    Call this mode a RECONSTRUCTION of the pre-RTL baselines.  Say that the
    projection is present in earlier commit ancestry; do not turn local commit
    metadata into an external timestamp claim.

      pre-RTL projection
                  the snapshot's cycles(..., "B2"), i.e.
                  tile = T*(tw + 41) + (tw - 1) -- the analogue of the
                  projection B1 WITHDREW for being optimistic by T + 1 per
                  (output row, template row).
      control-naive
                  the control's MEASURED latency minus exactly the pixels the
                  reuse skips, rh*th*(T - 1)*(tw - 1).  Equivalent to
                  tile = T*(tw + 42) + tw when the control is `b1`.  Note what
                  this baseline already contains: B1's measured term, hence
                  B1's own T + 1 correction.

    The two differ by rh*th*(T + 1) -- B1's shape and B1's sign.

    THE MEASUREMENT LANDED ON NEITHER, and the decomposition is the result:

        measured - pre-RTL projection = 3T - 1 = (T + 1) + 2*(T - 1)

    per (output row, template row), exact on 14/14.  BOTH TERMS MATTER.  The
    (T + 1) is B1's correction and it RECURS -- at T = 1 the second term is
    zero and the entire miss IS B1's (T + 1), which is what the five
    single-tile transactions show.  The 2*(T - 1) is the ADDITIONAL miss, and
    it is additional only relative to control-naive, a baseline that already
    carries B1's correction.  Quoting 2*(T - 1) without naming that baseline
    turns "B1's overhead recurred and was compounded" into "B1's overhead did
    not recur", which is the opposite of what these numbers say.
    """
    pre = _load_pre_b2()
    rows = build_rows(ctl, None, ctl_variant)
    for r in rows:
        r["model_b2_declared"] = pre.cycles(r["pw"], r["ph"], r["tw"],
                                            r["th"], "B2")
    print("RECONSTRUCTED PRE-RTL B2 BASELINES -- computed from the pre-B2 "
          "model snapshot")
    print(f"and the `{ctl_name}` report alone.  No b2 report is read by this "
          f"mode, so these")
    print("numbers are recomputable at any time.")
    print()
    print("  PROVENANCE, stated exactly.  The pre-RTL projection is retained ")
    print("  in ancestor commit e762cbf before the B2 source/build commit. "
          "That is repository")
    print("  ordering, not an external timestamp: unsigned, unpushed commit "
          "dates do not prove")
    print("  third-party observability.  THIS TOOL IS LATER: the snapshot "
          "(19:19:15) and")
    print("  PREDICTION.txt (19:19:23) both postdate the b2 transaction report "
          "(19:10:13).")
    print("  They are a RECONSTRUCTION whose content is checkable against "
          "e762cbf, not a")
    print("  record written before the answer.  control-naive is computed here "
          "and was")
    print("  never predicted by anyone.")
    print()
    print(f"  {'case':<22} {'geom':>18} {'rw':>4} {'T':>2} "
          f"{'pre-RTL proj':>12} {'control-naive':>14} {'diff':>7}")
    tot_d = tot_n = 0
    for r in rows:
        dec = r["model_b2_declared"]
        naive = r["meas_ctl"] - r["naive_delta"]
        tot_d += dec
        tot_n += naive
        print(f"  {r['tag']:<22} {r['pw']:5d}x{r['ph']:<4d}{r['tw']:4d}x"
              f"{r['th']:<4d} {r['rw']:4d} {r['T']:2d} {dec:12,} {naive:14,} "
              f"{naive - dec:7,}")
    print(f"  {'suite total':<22} {'':18} {'':4} {'':2} {tot_d:12,} "
          f"{tot_n:14,} {tot_n - tot_d:7,}")
    print()
    print("  pre-RTL proj   tile = T*(tw+41) + (tw-1), read from " + PRE_B2_MODEL)
    print("  control-naive  measured `%s` - rh*th*(T-1)*(tw-1)" % ctl_name)
    print("  the gap is rh*th*(T + 1) on every transaction -- B1's shape, "
          "and its sign")
    print()
    print("  MEASURED (tme_b2_ab.py, 2026-08-19): tile = T*(tw+44) + (tw-2),")
    print("  i.e. NEITHER of the above.  Against the pre-RTL projection the "
          "miss decomposes")
    print("  as  3T - 1 = (T + 1) + 2*(T - 1)  per (output row, template "
          "row), exact on 14/14.")
    print("  The (T + 1) is B1's correction and it RECURS -- at T = 1 it is "
          "the WHOLE miss.")
    print("  The 2*(T - 1) is additional, and additional only against "
          "control-naive, which")
    print("  already carries B1's term.  Do not read this as \"B1's overhead "
          "did not recur\".")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assert", dest="do_assert", action="store_true",
                    help="exit 1 if any measured value drifts from the model")
    ap.add_argument("--json", type=Path, help="write the comparison as JSON")
    ap.add_argument("--project-root", dest="project_root", type=Path,
                    default=HLS)
    ap.add_argument("--sol", default="b2", help="the B2-side solution")
    ap.add_argument("--control", default="b1",
                    help="the paired control (default b1 -- B2 is B1 plus the "
                         "reuse, so `cur` would fold two changes together)")
    ap.add_argument("--negative-control", action="store_true",
                    help="perturb the declared model and confirm check 2 "
                         "fails while check 3 still passes")
    ap.add_argument("--predict", action="store_true",
                    help="print the two candidate B2 terms WITHOUT reading any "
                         "b2 report -- see predict()")
    args = ap.parse_args()

    def rpt(sol):
        return (args.project_root / ("template_match_b1_" + sol) / sol /
                "sim" / "report" / "verilog" / "result.transaction.rpt")

    ctl_variant = {"cur": "cur", "b1": "B1"}.get(args.control)
    if ctl_variant is None:
        raise SystemExit(f"--control {args.control} has no cycle-model variant")

    ctl = read_transactions(rpt(args.control))
    if args.predict:
        return predict(ctl, args.control, ctl_variant)
    b2_path = rpt(args.sol)
    b2 = read_transactions(b2_path) if b2_path.exists() else None

    if args.negative_control:
        if b2 is None:
            raise SystemExit("--negative-control needs a built b2 solution")
        real = M.cycles

        def perturbed(pw, ph, tw, th, variant="cur"):
            n = real(pw, ph, tw, th, variant)
            return n + 7 if variant == "B2" else n
        M.cycles = perturbed
        rows = build_rows(ctl, b2, ctl_variant)
        fit_reuse_overhead(rows)
        M.cycles = real
        n_dec = sum(1 for r in rows if r["declared_residual"] == 0)
        n_fit = sum(1 for r in rows if r["fitted_residual"] == 0)
        print("NEGATIVE CONTROL: declared B2 model perturbed by +7 cycles")
        print(f"  check 2 (declared) exact on {n_dec}/{len(rows)}  "
              f"-- must be 0/{len(rows)}")
        print(f"  check 3 (fitted)   exact on {n_fit}/{len(rows)}  "
              f"-- must be {len(rows)}/{len(rows)}")
        ok = (n_dec == 0 and n_fit == len(rows))
        print("  the two checks are INDEPENDENT" if ok else
              "  THE CHECKS ARE NOT INDEPENDENT -- one is derived from the "
              "other")
        return 0 if ok else 1

    rows = build_rows(ctl, b2, ctl_variant)
    if b2 is not None:
        fit_reuse_overhead(rows)
    bad = report(rows, b2 is not None, args.sol, args.control)

    if args.do_assert and b2 is not None:
        # The pre-B2 snapshot must still be the PRE-measurement model.  If
        # someone refreshes it from the live file, --predict silently starts
        # printing the answer as the prediction -- the exact failure this
        # indirection was added to prevent, so it is checked rather than
        # trusted.
        pre = _load_pre_b2()
        r0 = rows[5]                       # b1-w017: T = 2, so the terms differ
        g = (r0["pw"], r0["ph"], r0["tw"], r0["th"])
        if pre.cycles(*g, "B2") == M.cycles(*g, "B2"):
            print("", file=sys.stderr)
            print("the pre-B2 model snapshot carries the MEASURED B2 term, "
                  "not the withdrawn one:", file=sys.stderr)
            print("--predict would print the answer as its own prediction.  "
                  "Restore the snapshot.", file=sys.stderr)
            bad += 1

        # CHECK 4: THE DECOMPOSITION, machine-checked rather than asserted in
        # prose.  The miss against the pre-RTL projection must be exactly
        # rh*th*(3T - 1), and 3T - 1 must be exactly (T + 1) + 2*(T - 1) --
        # i.e. B1's correction PLUS an additional 2*(T - 1), not instead of it.
        # A revision that quietly restates this as "B1's overhead did not
        # recur" has to make this check fail first.
        n_dec = n_t1 = 0
        for r in rows:
            gg = (r["pw"], r["ph"], r["tw"], r["th"])
            n, T = r["rh"] * r["th"], r["T"]
            miss = r["meas_b2"] - pre.cycles(*gg, "B2")
            if miss != n * (3 * T - 1) or 3 * T - 1 != (T + 1) + 2 * (T - 1):
                print("", file=sys.stderr)
                print(f"decomposition failed at {r['tag']}: miss {miss:,} "
                      f"!= rh*th*(3T-1) = {n * (3 * T - 1):,}", file=sys.stderr)
                bad += 1
            else:
                n_dec += 1
            # At a single tile the 2*(T-1) term vanishes and the ENTIRE miss is
            # B1's (T + 1).  This is the observation the corrected claim rests
            # on, so it is checked separately rather than folded into the line
            # above.
            if T == 1:
                if miss != n * (T + 1):
                    print("", file=sys.stderr)
                    print(f"T=1 miss at {r['tag']} is not rh*th*(T+1)",
                          file=sys.stderr)
                    bad += 1
                else:
                    n_t1 += 1
        print()
        print(f"  DECOMPOSITION  miss vs pre-RTL projection = "
              f"rh*th*(3T-1) = rh*th*((T+1) + 2*(T-1)) : "
              f"{n_dec}/{len(rows)} exact")
        print(f"                 of which T = 1, where the whole miss is "
              f"B1's (T+1)      : {n_t1}/"
              f"{sum(1 for r in rows if r['T'] == 1)} exact")

    if args.json:
        args.json.write_text(json.dumps(
            dict(rows=rows, control=args.control, sol=args.sol,
                 have_b2=b2 is not None, failures=bad),
            indent=2), newline="\n")
        print(f"\nwrote {args.json}")

    if args.do_assert:
        if bad:
            print(f"\nFAIL: {bad} discrepancy/ies", file=sys.stderr)
            return 1
        print("\nOK -- four independent checks passed:\n"
              f"  1. the `{args.control}` control still reproduces its "
              f"published tile term;\n"
              "  2. every measured B2 latency matches the DECLARED model in "
              "tme_cycle_model;\n"
              "  3. every shortfall against the naive reuse arithmetic fits "
              "one rh*th*(a*T + b),\n     fitted here and not read from the "
              "model;\n"
              "  4. the miss against the pre-RTL projection is "
              "rh*th*((T+1) + 2*(T-1)) --\n     B1's correction RECURS and a "
              "further 2*(T-1) is added to it.\n"
              "Cycle counts only.  No page time and no clock is measured by "
              "this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
