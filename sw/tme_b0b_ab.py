#!/usr/bin/env python3
"""Priority 6 (B0b): adjudicate the hoisted window-statistics pass.

B0b stops recomputing SI and SI^2 for every (output row, template row) pair.
The window over rows [v, v+th) and the window over rows [v+1, v+1+th) share
th-1 rows, so B0b maintains the per-column accumulators ACROSS output rows: at
v == 0 it scans all th rows, and at every later v it subtracts the row leaving
the window and adds the row entering it.  One scan is tw + (rw - 1) = pw
iterations, so the pass is

    S = th + 2*(rh - 1)          scans
    I = S * pw                   iterations   ==  pw * (2*ph - th)

THREE SOLUTIONS, BECAUSE B0b IS TWO CHANGES
-------------------------------------------
Adding a pass and deleting the loops it replaces are separate things, and a
single control-versus-variant difference would fold them together.  So:

    b2ctl   the shipped core, byte-identical to B2's build inputs
    shadow  b2ctl PLUS the hoisted pass, compared at every result position
    b0b     the hoisted pass only; the repeated loops deleted

    D1 = shadow - b2ctl      the hoisted pass PLUS the shadow's comparator
    D2 = b0b    - shadow     -(the repeated loops) MINUS the same comparator
    D  = b0b    - b2ctl      what B0b changes about the shipped core

ONLY D IS OWNED.  The split into D1 and D2 is NOT, and the claim that it was
is withdrawn as of 2026-08-20.

The shadow's comparison lives inside norm_cols, and it reschedules that loop:
97 ~ 3361 in b2ctl and b0b, 99 ~ 3363 in shadow, with achieved II (4) and
iteration latency (98) identical in all three.  sw/tme_b0b_synth.py used to
compare only those two columns and therefore reported "no loop moved" for a
difference it could not see; it now reads all four and pins the +2 in a
recorded inventory.  norm_cols runs once per OUTPUT ROW, so

    D1 = pass + 2*rh          D2 = -(removal) - 2*rh          D unchanged

b2ctl and b0b agree on norm_cols, so D -- the frozen law -- carries none of it.

THE ALTERNATIVE FITS EXACTLY, WHICH IS THE POINT.  Shifting 2*rh across gives

    D1 - 2*rh = S*(pw + 29) + th + 3
    D2 + 2*rh = -[rh*th*(tw + rw + 24) + rh]

and these reproduce all fourteen transactions just as exactly, because they are
the same fourteen numbers rearranged.  The co-simulation constrains the SUM.
Preferring one pair over the other is choosing a functional form.

NO COMPARATOR-FREE CONTROL WAS BUILT, BECAUSE THERE CANNOT BE ONE.  A shadow
solution exists to keep BOTH copies of the statistics live at once.  A copy
nothing reads is dead code and Vitis deletes it, at which point the solution is
b2ctl or b0b and measures nothing.  Some consumer must therefore read both
copies, and the cheapest one is the two array reads and the compare already
present -- a separate comparison loop trades +2 inside norm_cols for a whole
loop region, and selecting between the copies instead of comparing them keeps
both reads and so keeps the cost.  D1 and D2 are unidentifiable BY THIS METHOD,
in principle rather than by oversight.

WHAT SURVIVES, AND IT IS THE PART THAT MATTERED.  The nuisance is proportional
to rh.  W's regressor is rh*th and th varies across the suite, so no
rh-proportional term can move W: `tw + rw + 24` per (output row, template row)
is the removal's own cost, and the pre-registered `tw + rw + 21` is refuted
with or without the comparator.  What does not survive is the pass's
(k, m) = (30, 5) and the removal's 3 per output row.

WHAT THE MEASUREMENT OVERTURNED
-------------------------------
D2 was PRE-REGISTERED (logs/b0b_20260820/PREDICTION.txt, committed before the
first build) as `-rh*th*(tw + rw + 21)`, zero free parameters, straight from
the model's four-way split of the fitted per-row term.  IT IS REFUTED: the
measurement is `-(rh*th*(tw + rw + 24) + 3*rh)`, exact on 14/14.

That is a finding about the MODEL, not about B0b.  Only the SUM of that
four-way split ever had evidence; this is the first time one of its terms has
been measured, and it is 3 too small per (output row, template row) with a
further 3 per output row hidden in the 5*rw + 99 term.  The consequences are
recorded in tme_cycle_model.PER_ROW_TERMS, including the part that is NOT
established: which of the other three shares was over-attributed.

D1's shape was pre-registered as `S*(pw + k) + m` with (k, m) to be fitted, and
the frozen endpoints correspond to k = m = 0.  Measured: k = 30, m = 5.  csynth
says the II itself really is 1, so the entire miss is per-scan overhead that
was never modelled.

Net, 17.726036 s/page -- BELOW both withdrawn endpoints, because the two errors
point in opposite directions and the removal wins.

SIX CHECKS, AND THEY FAIL FOR DIFFERENT REASONS
------------------------------------------------
  1. THE CONTROL IS INTACT.  Every b2ctl transaction must equal
     tme_cycle_model.cycles(..., "B2") exactly.  If it does not, the three
     reports are not a comparison and nothing below is attributable.
  2. D1's SHAPE.  D1 must be S*(pw + k) + m for one (k, m) across every
     transaction.  Two free parameters, solved from the first two transactions
     with different S; every other transaction then TESTS them.  This is a
     shape check on the DIFFERENCE and says nothing about the pass.
  3. D2's SHAPE.  D2 must be -(rh*th*(tw + rw + W) + c*rh) for one (W, c),
     solved the same way and tested the same way.  W is identified; c is not.
  3b. THE SPLIT IS NOT IDENTIFIED, DEMONSTRATED.  Shifting the comparator's
     2*rh from one half to the other must fit every transaction exactly and
     leave D untouched.  If it ever stops doing so, the algebra in this
     docstring is wrong and someone has learned something.
  4. THE FIT MATCHES THE FREEZE.  The four fitted constants must equal
     FROZEN["b0b"].  Checks 2 and 3 establish the SHAPE from this run's data;
     this one says the shape has not drifted from what was published.
  5. THE DECLARED MODEL.  Every b0b transaction must equal
     cycles(..., "B0b") -- the PUBLISHED number, computed from (pw, ph, tw, th)
     with zero free parameters.
  6. CHECKS 2/3 AND 5 ARE INDEPENDENT.  --negative-control perturbs the
     declared model and confirms that check 5 fails while checks 2 and 3 still
     pass.  tme_b1_ab.py records having once made a fit and a declared model
     into the same check by overwriting one from the other, which made the
     residual zero by construction.

COUNTING THE CONSTRAINTS HONESTLY.  Under check 2's shape the residual against
S*pw depends on S alone, so transactions sharing an S restate one equation.
The b1 suite plus the two direct tests spans S in {4, 16, 18, 30, 42, 62}: six
independent equations against two free parameters, so FOUR surplus constraints
-- not twelve.  The other eight transactions sit at an already-constrained S
with a different pw, so they test that the residual is a function of S alone.
Check 3's regressors are rh*th and rh, which vary independently because th
varies; check 5 has zero free parameters over thirteen distinct geometries.

    python tme_b0b_ab.py                     # print the comparison
    python tme_b0b_ab.py --assert            # exit 1 on any drift
    python tme_b0b_ab.py --negative-control  # prove the fits and the model are separate
    python tme_b0b_ab.py --json out.json

Run it with the HLS venv python, from anywhere.

WHAT THIS FILE DOES NOT ESTABLISH.  A cosim latency is a zero-stall RTL
schedule.  It licenses the CYCLE claim and nothing about the clock -- B0b has
NOT been routed and has NOT run on silicon -- and nothing about a page, since
the s/page figures sum a per-trial term over 20,680 modelled trials that no
hardware has run.  It also says nothing about CORRECTNESS: that is the shadow
build's job, and its evidence is the csim logs plus tme_b0b_mutants.py, not a
latency.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tme_cycle_model as M                                    # noqa: E402
# The report reader, the case manifest and the two direct-test geometries are
# B1's and are reused rather than restated: a second copy is a second thing to
# keep in step, and the transaction layout is a property of tme_tb.cpp, not of
# any variant.
from tme_b1_ab import (DIRECT, HLS, MANIFEST,                  # noqa: E402
                       read_manifest, read_transactions)

SOLUTIONS = ("b2ctl", "shadow", "b0b")

# What logs/b0b_20260820/PREDICTION.txt registered before the first build.
# Kept so the miss can be quoted against the pre-registration rather than
# against a baseline reconstructed afterwards -- the distinction B2's evidence
# had to be relabelled over.
PREREGISTERED_D2_W = 21
PREREGISTERED_D2_C = 0


def report_path(sol: str) -> Path:
    """Where run_hls_b0b.tcl leaves the transaction report for a solution."""
    return (HLS / f"template_match_b0b_{sol}" / sol / "sim" / "report"
            / "verilog" / "result.transaction.rpt")


def geometry_rows() -> list[dict]:
    """(pw, ph, tw, th) per cosim transaction, in report order."""
    rows = []
    for (pw, ph, tw, th), tag in DIRECT:
        rows.append(dict(pw=pw, ph=ph, tw=tw, th=th, tag=tag))
    for pw, ph, tw, th, tag in read_manifest(MANIFEST):
        rows.append(dict(pw=pw, ph=ph, tw=tw, th=th, tag=tag))
    return rows


def enrich(rows: list[dict]) -> list[dict]:
    for r in rows:
        pw, ph, tw, th = r["pw"], r["ph"], r["tw"], r["th"]
        r["rw"] = rw = pw - tw + 1
        r["rh"] = rh = ph - th + 1
        r["S"] = th + 2 * (rh - 1)
        r["I"] = M.b0b_count_pass_iterations(pw, ph, tw, th)
        r["b2_model"] = M.cycles(pw, ph, tw, th, "B2")
        # What the pre-registration predicted for D2.  Zero free parameters.
        r["d2_prereg"] = -(rh * th * (tw + rw + PREREGISTERED_D2_W)
                           + PREREGISTERED_D2_C * rh)
    return rows


def attach(rows: list[dict], lat: dict[str, list[int]]) -> list[dict]:
    for i, r in enumerate(rows):
        for sol in SOLUTIONS:
            if sol in lat:
                r[sol] = lat[sol][i]
        if "shadow" in r and "b2ctl" in r:
            r["D1"] = r["shadow"] - r["b2ctl"]
            # Residual against the withdrawn II=1 endpoint, which is D1 == I.
            r["D1_resid"] = r["D1"] - r["I"]
        if "b0b" in r and "shadow" in r:
            r["D2"] = r["b0b"] - r["shadow"]
        if "b0b" in r and "b2ctl" in r:
            # THE HEADLINE.  D1 and D2 are the two halves; D is what B0b
            # actually changes about the shipped core, and it is the only one
            # of the three that involves no shadow build.  D == D1 + D2 is an
            # identity, not a check.
            r["D"] = r["b0b"] - r["b2ctl"]
    return rows


def _two_distinct(rows: list[dict], key) -> list[dict]:
    """First two rows with distinct `key`, in REPORT ORDER so nothing is tuned."""
    seen: dict = {}
    for r in rows:
        seen.setdefault(key(r), r)
        if len(seen) == 2:
            return list(seen.values())
    return []


def fit_d1(rows: list[dict]) -> tuple[int, int] | None:
    """Solve D1 = S*(pw + k) + m from two transactions with different S.

    The residual against S*pw is S*k + m, linear in S, so two rows at different
    S determine (k, m) and every other row is a test rather than an input.
    """
    if any("D1_resid" not in r for r in rows):
        return None
    pair = _two_distinct(rows, lambda r: r["S"])
    if len(pair) != 2:
        return None
    (r1, r2) = pair
    k = (r1["D1_resid"] - r2["D1_resid"]) / (r1["S"] - r2["S"])
    m = r1["D1_resid"] - r1["S"] * k
    return _int_pair(k, m)


def fit_d2(rows: list[dict]) -> tuple[int, int] | None:
    """Solve -D2 = rh*th*(tw + rw + W) + c*rh from two transactions.

    Regressors rh*th and rh; they are independent because th varies across the
    suite.  Solved from the first pair whose 2x2 determinant is non-zero, again
    in report order.
    """
    if any("D2" not in r for r in rows):
        return None
    for i, ra in enumerate(rows):
        for rb in rows[i + 1:]:
            a1, b1 = ra["rh"] * ra["th"], ra["rh"]
            a2, b2 = rb["rh"] * rb["th"], rb["rh"]
            det = a1 * b2 - a2 * b1
            if det == 0:
                continue
            y1 = -ra["D2"] - a1 * (ra["tw"] + ra["rw"])
            y2 = -rb["D2"] - a2 * (rb["tw"] + rb["rw"])
            W = (y1 * b2 - y2 * b1) / det
            c = (a1 * y2 - a2 * y1) / det
            return _int_pair(W, c)
    return None


def _int_pair(a, b):
    if float(a).is_integer() and float(b).is_integer():
        return int(a), int(b)
    return a, b


def d1_of(r, k, m):
    return r["S"] * (r["pw"] + k) + m


def d2_of(r, W, c):
    return -(r["rh"] * r["th"] * (r["tw"] + r["rw"] + W) + c * r["rh"])


def report(rows: list[dict], have: dict[str, bool], strict: bool) -> int:
    fail = []
    froz = M.FROZEN["b0b"]

    print("PRIORITY 6 (B0b) - hoisted window statistics, paired RTL "
          "co-simulation")
    print("=" * 78)
    print("solutions present: " + ", ".join(s for s in SOLUTIONS if have[s]))
    print()

    # ---- check 1: the control ------------------------------------------
    print("CHECK 1 - the control is intact (b2ctl == the published B2 term)")
    print("-" * 78)
    if not have["b2ctl"]:
        print("  b2ctl report absent - SKIPPED, and nothing below is "
              "attributable without it")
        fail.append("check 1: no b2ctl report")
    else:
        bad = 0
        for i, r in enumerate(rows):
            if r["b2ctl"] != r["b2_model"]:
                bad += 1
                print(f"  [{i:2d}] {r['tag']:<26} measured {r['b2ctl']:>10d} "
                      f"!= model {r['b2_model']:>10d}")
        print(f"  {len(rows) - bad}/{len(rows)} transactions reproduce "
              f"cycles(..., 'B2') exactly")
        if bad:
            fail.append(f"check 1: {bad} control transaction(s) drifted")
    print()

    # ---- the table ------------------------------------------------------
    print("PER-TRANSACTION")
    print("-" * 78)
    print(f"{'#':<3}{'geometry':<18}{'S':>5}{'I':>8}{'b2ctl':>10}"
          f"{'D1':>9}{'D1-I':>8}{'D2':>10}{'D2 prereg':>11}{'D':>10}")
    for i, r in enumerate(rows):
        geo = f"{r['pw']}x{r['ph']}/{r['tw']}x{r['th']}"
        print(f"{i:<3}{geo:<18}{r['S']:>5}{r['I']:>8}"
              f"{r.get('b2ctl', 0):>10}"
              f"{r.get('D1', 0):>9}{r.get('D1_resid', 0):>8}"
              f"{r.get('D2', 0):>10}{r['d2_prereg']:>11}{r.get('D', 0):>10}")
    print()
    print("  D1 = shadow - b2ctl   the hoisted pass PLUS the shadow's")
    print("                        comparator (+2 per output row)")
    print("  D2 = b0b    - shadow  -(the deleted loops) MINUS the same")
    print("  D  = b0b    - b2ctl   what B0b changes about the shipped core;")
    print("                        D == D1 + D2 is an identity, not a check.")
    print("                        D is the ONLY one of the three that is")
    print("                        comparator-free, and the only one frozen.")
    print()

    km = fit_d1(rows) if have["shadow"] and have["b2ctl"] else None
    wc = fit_d2(rows) if have["shadow"] and have["b0b"] else None

    # ---- check 2: D1's shape -------------------------------------------
    print("CHECK 2 - D1 = S*(pw + k) + m, one (k, m) for every transaction")
    print("          (a shape check on the DIFFERENCE; D1 is not the pass)")
    print("-" * 78)
    if km is None:
        print("  needs both b2ctl and shadow reports - SKIPPED")
        if strict:
            fail.append("check 2: reports missing")
    else:
        k, m = km
        print(f"  fitted from the first two distinct S:  k = {k}   m = {m}")
        bad = [i for i, r in enumerate(rows) if d1_of(r, k, m) != r["D1"]]
        for i in bad:
            print(f"  [{i:2d}] {rows[i]['tag']:<26} D1 {rows[i]['D1']:>9d} != "
                  f"{d1_of(rows[i], k, m):>9d}")
        print(f"  {len(rows) - len(bad)}/{len(rows)} transactions match "
              f"D1 = S*(pw + {k}) + {m}")
        if bad:
            fail.append(f"check 2: {len(bad)} transaction(s) off the fitted "
                        f"shape")
        tot_I = sum(r["I"] for r in rows)
        tot_D1 = sum(r["D1"] for r in rows)
        print()
        print(f"  The WITHDRAWN II=1 endpoint is k = m = 0, i.e. D1 = I.  Over")
        print(f"  these {len(rows)} transactions that form gives {tot_I:,} "
              f"cycles; the")
        print(f"  measurement is {tot_D1:,} - {tot_D1 - tot_I:+,} "
              f"({(tot_D1 / tot_I - 1) * 100:+.2f}%).")
        print(f"  csynth says the II really IS 1 (scan_init and scan_slide at")
        print(f"  II=1, iteration latencies 7 and 14, the same as the isq_init")
        print(f"  and isq_slide they replace), so the miss is entirely the")
        print(f"  per-scan constant that was never modelled.")
        print()
        print(f"  k = {k} AND m = {m} ARE NOT PROPERTIES OF THE PASS.  D1 carries")
        print(f"  the shadow's comparator, +2 per output row.  Take it out and")
        print(f"  the same fourteen numbers read S*(pw + {k - 1}) + th + 3 -- a")
        print(f"  th-dependent constant instead of a fixed one.  See check 3b.")
    print()

    # ---- check 3: D2's shape -------------------------------------------
    print("CHECK 3 - D2 = -(rh*th*(tw + rw + W) + c*rh), one (W, c) for every")
    print("          transaction")
    print("-" * 78)
    if wc is None:
        print("  needs both shadow and b0b reports - SKIPPED")
        if strict:
            fail.append("check 3: reports missing")
    else:
        W, c = wc
        print(f"  fitted:  W = {W}   c = {c}")
        bad = [i for i, r in enumerate(rows) if d2_of(r, W, c) != r["D2"]]
        for i in bad:
            print(f"  [{i:2d}] {rows[i]['tag']:<26} D2 {rows[i]['D2']:>10d} != "
                  f"{d2_of(rows[i], W, c):>10d}")
        print(f"  {len(rows) - len(bad)}/{len(rows)} transactions match")
        if bad:
            fail.append(f"check 3: {len(bad)} transaction(s) off the fitted "
                        f"shape")
        print()
        print(f"  THE PRE-REGISTRATION IS REFUTED, and this is the finding.")
        print(f"  logs/b0b_20260820/PREDICTION.txt registered "
              f"W = {PREREGISTERED_D2_W}, c = {PREREGISTERED_D2_C}")
        print(f"  before the first build, straight from the model's four-way")
        print(f"  split of the fitted per-row term.  Measured: W = {W}, c = {c}.")
        miss = sum(r["D2"] - r["d2_prereg"] for r in rows)
        print(f"  Over these {len(rows)} transactions the prediction is short "
              f"by {-miss:,} cycles.")
        print(f"  Only the SUM of that four-way split ever had evidence.  This")
        print(f"  is the first of its four terms to be measured, and the other")
        print(f"  three now sum to 2*tw + 2*rw + 9 rather than + 12.  WHICH of")
        print(f"  them was over-attributed is NOT established.")
        print()
        print(f"  W = {W} SURVIVES THE COMPARATOR AND c = {c} DOES NOT.  The")
        print(f"  nuisance is proportional to rh; W's regressor is rh*th and th")
        print(f"  varies across the suite, so nothing rh-proportional can move")
        print(f"  W.  The refutation above therefore holds either way.  c is")
        print(f"  the removal's own share plus the comparator's 2.")
        print(f"  Note tw + rw = pw + 1, so the measured cost is "
              f"rh*th*(pw + {W + 1}) + {c}*rh:")
        print(f"  it depends on the PATCH WIDTH alone, which is what the two")
        print(f"  deleted loops actually scan (tw priming + rw - 1 sliding).")
    print()

    # ---- check 3b: the split is not identified --------------------------
    print("CHECK 3b - the D1/D2 split is NOT identified, and here is the proof")
    print("-" * 78)
    if km is None or wc is None:
        print("  needs all three reports - SKIPPED")
        if strict:
            fail.append("check 3b: reports missing")
    else:
        k, m = km
        W, c = wc
        nu = M.B0B_SPLIT_NUISANCE_PER_OUTPUT_ROW
        print(f"  csynth locates the shadow's comparator in norm_cols at +{nu}")
        print(f"  cycles per call, and norm_cols runs once per output row.  So")
        print(f"  shift {nu}*rh from D1 to D2 and refit:")
        print()
        print(f"    as measured       D1 = S*(pw + {k}) + {m}")
        print(f"                      D2 = -[rh*th*(tw + rw + {W}) + {c}*rh]")
        print(f"    comparator out    D1 = S*(pw + {k - 1}) + th + 3")
        print(f"                      D2 = -[rh*th*(tw + rw + {W}) + "
              f"{c - nu}*rh]")
        print()
        bad1 = bad2 = badD = 0
        for r in rows:
            rh, S, th = r["rh"], r["S"], r["th"]
            alt1 = S * (r["pw"] + k - 1) + th + 3
            alt2 = -(rh * th * (r["tw"] + r["rw"] + W) + (c - nu) * rh)
            if alt1 != r["D1"] - nu * rh:
                bad1 += 1
            if alt2 != r["D2"] + nu * rh:
                bad2 += 1
            if alt1 + alt2 != r["D"]:
                badD += 1
        n = len(rows)
        print(f"  {n - bad1}/{n} transactions match the decontaminated D1 form")
        print(f"  {n - bad2}/{n} transactions match the decontaminated D2 form")
        print(f"  {n - badD}/{n} transactions still sum to the SAME D")
        if bad1 or bad2 or badD:
            fail.append("check 3b: the decontaminated pair does not reproduce "
                        "the data -- the algebra in this file's docstring is "
                        "wrong")
        print()
        print("  BOTH PAIRS FIT EXACTLY, because they are the same fourteen")
        print("  numbers rearranged.  The co-simulation constrains D and not")
        print("  the split, so NEITHER pair is a measurement of the pass or of")
        print("  the removal.  The decontaminated one is the leading candidate")
        print("  -- it is where csynth puts the comparator -- and it is NOT")
        print("  frozen: it rests on a scheduler estimate rather than on RTL,")
        print("  and it cannot exclude a further per-invocation constant, which")
        print("  trades against the +3 with no way to tell them apart.")
        print()
        print("  A COMPARATOR-FREE CONTROL WOULD SETTLE IT AND CANNOT EXIST.")
        print("  The shadow's job is to keep both copies of the statistics")
        print("  live; an unread copy is dead code and gets deleted, leaving")
        print("  b2ctl or b0b.  Some consumer must read both, and the two array")
        print("  reads already there are the cheapest one.")
    print()

    # ---- check 4: the fit matches the freeze ----------------------------
    print("CHECK 4 - the fitted constants match FROZEN['b0b']")
    print("-" * 78)
    if km is None or wc is None:
        print("  needs all three reports - SKIPPED")
        if strict:
            fail.append("check 4: reports missing")
    else:
        # Named for the DIFFERENCES they were fitted to, not for the pass and
        # the removal.  The rename is the withdrawal: `pass_k_per_scan` said
        # the fit was a property of the pass, and it is not.
        pairs = (("d1_k_per_scan", km[0]), ("d1_m_per_call", km[1]),
                 ("d2_W", wc[0]), ("d2_c_per_output_row", wc[1]))
        for name, got in pairs:
            want = froz[name]
            ok = got == want
            print(f"  {name:<28} fitted {got:>6}   frozen {want:>6}   "
                  f"{'OK' if ok else '**DRIFT**'}")
            if not ok:
                fail.append(f"check 4: {name} fitted {got} != frozen {want}")
    print()

    # ---- check 5: the declared model ------------------------------------
    print("CHECK 5 - every b0b transaction == cycles(..., 'B0b')")
    print("-" * 78)
    if not have["b0b"]:
        print("  b0b report absent - SKIPPED")
        if strict:
            fail.append("check 5: no b0b report")
    elif "B0b" not in getattr(M, "MEASURED_VARIANTS", ()):
        print("  tme_cycle_model has no measured 'B0b' variant - SKIPPED.")
        if strict:
            fail.append("check 5: no declared B0b variant in the model")
    else:
        bad = 0
        for i, r in enumerate(rows):
            pred = M.cycles(r["pw"], r["ph"], r["tw"], r["th"], "B0b")
            if pred != r["b0b"]:
                bad += 1
                print(f"  [{i:2d}] {r['tag']:<26} measured {r['b0b']:>10d} "
                      f"!= model {pred:>10d} ({r['b0b'] - pred:+d})")
        print(f"  {len(rows) - bad}/{len(rows)} transactions match the "
              f"declared model, zero free parameters")
        if bad:
            fail.append(f"check 5: {bad} transaction(s) off the declared model")
    print()

    # ---- B0b is not a uniform improvement -------------------------------
    if have["b0b"] and have["b2ctl"]:
        print("WHERE B0b LOSES")
        print("-" * 78)
        losers = [(i, r) for i, r in enumerate(rows) if r["D"] > 0]
        if losers:
            for i, r in losers:
                print(f"  [{i:2d}] {r['tag']:<26} rh = {r['rh']:<4} "
                      f"D = {r['D']:+d}   (5*th + 2 = {5 * r['th'] + 2})")
        print("  B0b is a REGRESSION at rh == 1, by exactly 5*th + 2 cycles:")
        print("  a single output row has nothing to reuse vertically and the")
        print("  hoisted pass still pays its per-scan overhead.  Derived, and")
        print("  asserted in tme_cycle_model.check().  Anyone quoting B0b as a")
        print("  uniform improvement is wrong.")
        print()

    # ---- the consequence -------------------------------------------------
    # Computed HERE from this run's own fits, not read back from the model.
    # Check 5 compares the model against the measurement; this section says
    # what the measurement implies, so the two are independent statements and a
    # disagreement between them is visible rather than absorbed.
    if km is not None and wc is not None:
        k, m = km
        W, c = wc
        print("WHOLE-WORKLOAD CONSEQUENCE, from this run's fits")
        print("-" * 78)
        print(f"  per invocation:  B0b = B2 - [rh*th*(tw + rw + {W}) + {c}*rh]")
        print(f"                             + [S*(pw + {k}) + {m}]")
        print(f"                   S   = th + 2*(rh - 1)")
        print(f"  The two bracketed terms are D2 and D1, so each carries the")
        print(f"  shadow's comparator with opposite sign.  Their SUM is what is")
        print(f"  being priced here, and the comparator is not in it.")
        print()

        def b0b_expr(pw, ph, tw, th):
            rw, rh = pw - tw + 1, ph - th + 1
            if rw < 1 or rh < 1:
                return 0
            S = th + 2 * (rh - 1)
            return (M.cycles(pw, ph, tw, th, "B2")
                    - (rh * th * (tw + rw + W) + c * rh)
                    + S * (pw + k) + m)

        s_page = M.page_cycles_expr(b0b_expr)
        sp = M.FROZEN["s_per_page_at_125mhz"]
        print(f"  s/page over the 20,680-trial corpus at 125 MHz: "
              f"{s_page:.12f}")
        print(f"    vs frozen B0b aggregate  {froz['s_per_page']:.12f}  "
              f"({s_page - froz['s_per_page']:+.12f})")
        for name in ("withdrawn_at_1_cyc", "withdrawn_at_3_cyc"):
            print(f"    vs {name:<20} {froz[name]:.12f}  "
                  f"({s_page - froz[name]:+.12f})")
        # Against B2's EXACT aggregate-derived figure, not the rounded 20.405
        # in the s_per_page_at_125mhz table.  Six decimal places on a 36-page
        # average is a 2.25-million-cycle tolerance, and quoting a saving to
        # three decimals against a rounded baseline is how that drifts.
        b2_exact = M.FROZEN["b2"]["s_per_page"]
        print(f"    vs measured B2           {b2_exact:.12f}  "
              f"({s_page - b2_exact:+.12f})")
        if not (s_page < froz["withdrawn_at_1_cyc"]):
            fail.append("the measured s/page is no longer below the withdrawn "
                        "II=1 endpoint - the narrative here is stale")
        else:
            print()
            print("  THE WITHDRAWN ENDPOINTS DID NOT BRACKET THIS.  The result")
            print("  is below both.  The pass costs more than the II=1")
            print("  endpoint assumed and the removal is worth more than the")
            print("  model attributed; the second error is larger and points")
            print("  the other way.")
        print()
        print("  THIS IS NOT A PAGE TIME.  It sums a per-trial term over")
        print("  20,680 MODELLED trials.  No page has been run on any")
        print("  hardware at any clock, B0b has not been routed, and this run")
        print("  adds no board evidence.")
        print()

    if fail:
        print("FAILURES")
        print("-" * 78)
        for f in fail:
            print("  " + f)
        return 1
    print("all checks that could run PASSED")
    return 0


def negative_control(rows: list[dict]) -> int:
    """Prove the fits and the declared model are not the same check.

    tme_b1_ab.py records having once overwritten a declared model from a fit,
    which made the residual zero by construction.  Perturbing the declared
    model must break check 5 while leaving checks 2 and 3 untouched, because
    neither of them reads it.
    """
    if "B0b" not in getattr(M, "MEASURED_VARIANTS", ()):
        print("negative control needs a declared 'B0b' variant in the model")
        return 1
    km, wc = fit_d1(rows), fit_d2(rows)
    if km is None or wc is None:
        print("negative control needs all three reports")
        return 1
    print("NEGATIVE CONTROL - perturb the declared model by +7 cycles per")
    print("(output row, template row) and confirm check 5 fails while checks 2")
    print("and 3 still pass.")
    print("-" * 78)
    bad2 = sum(1 for r in rows if d1_of(r, *km) != r["D1"])
    bad3 = sum(1 for r in rows if d2_of(r, *wc) != r["D2"])
    bad5 = 0
    for r in rows:
        perturbed = (M.cycles(r["pw"], r["ph"], r["tw"], r["th"], "B0b")
                     + r["rh"] * r["th"] * 7)
        if perturbed != r["b0b"]:
            bad5 += 1
    n = len(rows)
    print(f"  check 2 with the perturbation in place: {n - bad2}/{n} still pass")
    print(f"  check 3 with the perturbation in place: {n - bad3}/{n} still pass")
    print(f"  check 5 with the perturbation in place: {n - bad5}/{n} pass")
    ok = (bad2 == 0 and bad3 == 0 and bad5 == n)
    print("  INDEPENDENT" if ok else "  NOT INDEPENDENT - investigate")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assert", dest="strict", action="store_true",
                    help="exit 1 unless every check ran and passed")
    ap.add_argument("--negative-control", action="store_true")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    rows = enrich(geometry_rows())
    lat, have = {}, {}
    for sol in SOLUTIONS:
        p = report_path(sol)
        have[sol] = p.exists()
        if have[sol]:
            got = read_transactions(p)
            if len(got) != len(rows):
                raise SystemExit(
                    f"{p}: {len(got)} transactions, expected {len(rows)} "
                    f"(2 direct + {len(rows) - 2} manifest).  The suite or the "
                    f"testbench changed; the reports are not comparable.")
            lat[sol] = got
    if not any(have.values()):
        raise SystemExit(
            "no transaction reports found.  Build them with:\n"
            "  TME_B0B_SOLUTION={b2ctl|shadow|b0b} vitis-run.bat --mode hls "
            "--tcl run_hls_b0b.tcl")
    rows = attach(rows, lat)

    if args.negative_control:
        return negative_control(rows)

    rc = report(rows, have, args.strict)
    if args.json:
        args.json.write_text(json.dumps({"rows": rows, "have": have}, indent=2),
                             encoding="utf-8")
        print(f"\nwrote {args.json}")
    return rc if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
