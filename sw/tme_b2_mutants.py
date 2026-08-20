#!/usr/bin/env python3
"""Priority 5 (B2): does the pinned b1 vector suite detect a B2 defect?

B2 adds HORIZONTAL OVERLAP REUSE to correlation_core.  Tile 0 still loads the
whole segment; every later tile slides the overlap down by PAR_COLS and refills
only the PAR_COLS pixels that are new.  That replaces a re-read with CARRIED
STATE, and carried state is the thing a vector suite is least likely to probe
by accident -- so before spending an RTL co-simulation on it, this file asks
whether the suite that will be run can actually fail.

WHY THIS FILE EXISTS AT ALL, AND WHY IT IS NOT A NEW SUITE.  B2's measurement
has to be PAIRED against B1's, and a pair is only worth something if both
halves saw the same stimulus.  The b1 suite is pinned (tb_tme_b1.sha256) and
its `cur` and `b1` transaction reports are retained, so running B2 through it
gives a three-way comparison at zero stimulus risk.  Adding cases would have
broken that.  The question then becomes whether the OLD suite is adequate for
the NEW defect classes, and that is a question with an answer rather than a
matter of taste.  It is answered here, before the build, not asserted after it.

WHAT IS ESTABLISHED
-------------------
Six defect classes, each an edit a reasonable person might make to the
overlap-reuse code, are each broken by at least one case in the pinned suite
for ALL 256 possible stale register fills -- an unconditional detection, the
same standard build_lane15 is held to.  Two further variations are shown to be
INERT: they change no result anywhere, so they are not defects and no suite
could or should catch them.

Two structural arguments are re-proved rather than asserted, because each
explains a column of the table that would otherwise be a bare count:

  * the out-of-patch PAD VALUE is unreachable (only masked lanes ever read it),
    while the `idx < pw` GUARD is still required for memory safety;
  * a cross-invocation reuse defect damages EXACTLY the output columns
    u < tw - 1 and self-heals beyond them, so only a case whose argmax lies
    there can detect it.  That is why `no-full-tile0` is caught by eight of the
    twelve cases rather than all twelve, and it is a fact about GEOMETRY, not
    about this suite's luck.

WHAT IS NOT ESTABLISHED
-----------------------
This is a Python transcription of the C++, not the C++ and certainly not the
RTL.  It can only show that the SUITE discriminates the behaviours it models;
it cannot show that Vitis compiled the source into one of them.  That is what
csim and cosim are for, and this file is a precondition for believing them, not
a substitute.  A mutant this file cannot express is a mutant it says nothing
about.

    python tme_b2_mutants.py                 # print the detection table
    python tme_b2_mutants.py --assert        # exit 1 if the suite went blind
    python tme_b2_mutants.py --selftest      # re-prove the two structural arguments
    python tme_b2_mutants.py --json out.json

Run it with the HLS venv python, from anywhere:

    C:/Users/lychee/Desktop/FPGA/hls/.venv/Scripts/python.exe tme_b2_mutants.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

# Resolved rather than hard-coded, so this runs from a fresh clone as well as
# from the split development tree -- same rule as tme_b1_ab.py.
_SW = Path(__file__).resolve().parent
_ROOT = _SW.parents[1] if _SW.parent.name == ".github-upload" else _SW.parent
HLS = _ROOT / "hls" / "template_match"

PAR_COLS = 16
MAX_PATCH_W = 820
MAX_TEMPL_W = 216
SEG_W = PAR_COLS + MAX_TEMPL_W - 1          # 231, the real register file

# tme_tb.cpp: a case passes when the score is within MAX_SCORE_ERR AND the
# location is EXACT.  The score is a tolerance and the location is not -- see
# the board-pass note in tme_cycle_model.py; the same asymmetry applies here.
SCORE_TOL = 0.005


# ---------------------------------------------------------------------------
# 1. The transcription
# ---------------------------------------------------------------------------

def sim(patch, templ, stale=0, *, shift_by=PAR_COLS, refill_n=PAR_COLS,
        refill_off=0, refill_u0=0, skip_first_full=False, guard_shift=True,
        pad="zero"):
    """The sti (ΣTI) map correlation_core's B2 form accumulates.

    Deliberately loop-for-loop with the C++ and deliberately slow.  The
    register file is the REAL one -- SEG_W = 231 elements, of which only
    seg[0 .. seg_len-1] are ever written -- because a mutant that reads above
    seg_len-1 reads a register nothing has written, and only a full-size array
    makes the stale fill mean anything there.

    Knobs, all defaulting to the shipped behaviour:

      shift_by        PAR_COLS is correct; 15 and 17 are off-by-one slides
      refill_n        PAR_COLS is correct; 15 drops seg[seg_len-1], which only
                      lane 15 on the template's last column ever reads
      refill_off      0 is correct; +1 leaves seg[tw-1] holding the old tile
      refill_u0       0 is correct; -PAR_COLS refills from the previous tile's
                      column base, i.e. slides the data but not the source
      skip_first_full reuse seg across tile 0 -- across rows AND across calls
      guard_shift     False copies the full SEG_W-PAR_COLS span unguarded,
                      reading above seg_len-1.  Expected INERT; see INERT below
      pad             "zero" is what the source does; "clamp" repeats the last
                      real pixel instead.  Expected INERT; see INERT below
    """
    p = patch.astype(np.int64)
    t = templ.astype(np.int64)
    ph, pw = p.shape
    th, tw = t.shape
    rh, rw = ph - th + 1, pw - tw + 1
    seg_len = PAR_COLS + tw - 1
    overlap = seg_len - PAR_COLS

    def fetch(line, idx):
        if idx < pw:
            return line[idx]
        return line[pw - 1] if pad == "clamp" else 0

    sti = np.zeros((rh, rw), np.int64)
    # One register file for the whole run: it persists between tiles, between
    # template rows, between output rows and between INVOCATIONS, exactly as
    # the hardware's registers do.  That is what makes skip_first_full a
    # meaningful mutant rather than a local one.
    seg = np.full(SEG_W, stale, np.int64)
    for v in range(rh):
        for y in range(th):
            line = p[v + y]
            trow = t[y]
            for ti, u0 in enumerate(range(0, rw, PAR_COLS)):
                if ti == 0 and not skip_first_full:
                    for i in range(seg_len):
                        seg[i] = fetch(line, u0 + i)
                else:
                    src = seg.copy()
                    hi = overlap if guard_shift else SEG_W - PAR_COLS
                    for i in range(hi):
                        j = i + shift_by
                        seg[i] = src[j] if j < SEG_W else stale
                    for k in range(refill_n):
                        j = overlap + refill_off + k
                        if j < SEG_W:
                            seg[j] = fetch(line, u0 + refill_u0 + j)
                # The 16 lanes of one tile: lane p accumulates seg[p:p+tw]·trow.
                win = np.lib.stride_tricks.sliding_window_view(
                    seg[:PAR_COLS + tw - 1], tw)          # (PAR_COLS, tw)
                lanes = win @ trow
                n = min(PAR_COLS, rw - u0)                # writeback mask
                sti[v, u0:u0 + n] += lanes[:n]
    return sti


def reference(patch, templ):
    """Direct valid cross-correlation: the answer any tiling must reproduce."""
    p = patch.astype(np.int64)
    t = templ.astype(np.int64)
    ph, pw = p.shape
    th, tw = t.shape
    rh, rw = ph - th + 1, pw - tw + 1
    out = np.zeros((rh, rw), np.int64)
    for v in range(rh):
        for u in range(rw):
            out[v, u] = int((p[v:v + th, u:u + tw] * t).sum())
    return out


# ---------------------------------------------------------------------------
# 2. The DUT's reduction, over a supplied ΣTI map
# ---------------------------------------------------------------------------

def _win_sums(a, th, tw):
    c = np.pad(np.cumsum(np.cumsum(a, 0), 1), ((1, 0), (1, 0)))
    return c[th:, tw:] - c[:-th, tw:] - c[th:, :-tw] + c[:-th, :-tw]


def dut(patch, templ, sti):
    """(x, y, score) tme_top 0.2 reports, given this ΣTI map.

    Only ΣTI comes from correlation_core, so ΣI, ΣI² and the template terms are
    recomputed exactly here: a correlation_core defect must show up through the
    numerator alone, and modelling the rest from the same (possibly wrong) map
    would hide exactly the cancellations worth knowing about.

    float32, strict `>` from a -2.0f seed, clamped to [-1, 1] -- i.e. a
    row-major first-occurrence argmax, which is what np.argmax gives.
    """
    p = patch.astype(np.int64)
    t = templ.astype(np.int64)
    ph, pw = p.shape
    th, tw = t.shape
    rh, rw = ph - th + 1, pw - tw + 1
    n = tw * th
    si = _win_sums(p, th, tw)
    sii = _win_sums(p * p, th, tw)
    st, stt = int(t.sum()), int((t * t).sum())
    dt_f = np.float32(n * stt - st * st)
    num = n * sti - st * si
    di = n * sii - si * si
    with np.errstate(divide="ignore", invalid="ignore"):
        prod = (dt_f * di.astype(np.float32)).astype(np.float32)
        score = (num.astype(np.float32)
                 / np.sqrt(np.where(prod == 0, np.float32(1.0), prod)
                           ).astype(np.float32))
    score = np.where(di == 0, np.float32(0.0), score).astype(np.float32)
    score = np.clip(score, np.float32(-1.0), np.float32(1.0)).astype(np.float32)
    flat = int(np.argmax(score))
    gy, gx = divmod(flat, rw)
    return gx, gy, float(score[gy, gx])


# ---------------------------------------------------------------------------
# 3. The suite
# ---------------------------------------------------------------------------

def load_suite(name: str = "b1") -> list[dict]:
    man = HLS / f"tb_tme_cases_{name}.txt"
    if not man.exists():
        raise SystemExit(
            f"no case manifest at {man}\n"
            f"  The b1 vector suite regenerates from pinned seeds:\n"
            f"    cd {HLS}\n"
            f"    <hls venv>/python.exe tme_generate_production.py --suite b1\n"
            f"  then `sha256sum -c tb_tme_b1.sha256` should report three OKs.")
    lines = man.read_text().splitlines()
    nb = int(lines[0].split()[0])
    pb = (HLS / f"tb_tme_patches_{name}.bin").read_bytes()
    tb = (HLS / f"tb_tme_templs_{name}.bin").read_bytes()
    out = []
    for row in lines[1:1 + nb]:
        f = row.split()
        pw, ph, tw, th = (int(f[1]), int(f[2]), int(f[3]), int(f[4]))
        po, to = int(f[5]), int(f[6])
        out.append(dict(
            tag=f[-1], pw=pw, ph=ph, tw=tw, th=th,
            score=float(f[7]), x=int(f[8]), y=int(f[9]),
            patch=np.frombuffer(pb, np.uint8, pw * ph, po).reshape(ph, pw),
            templ=np.frombuffer(tb, np.uint8, tw * th, to).reshape(th, tw)))
    return out


# The five ways the overlap-reuse code can be got wrong, as edits rather than
# as prose.  Each must be broken UNCONDITIONALLY -- for every one of the 256
# values a stale register could hold -- by at least one case in the suite.
DEFECTS = {
    "shift_by=15":    dict(shift_by=15),
    "shift_by=17":    dict(shift_by=17),
    "refill_n=15":    dict(refill_n=15),
    "refill_off=+1":  dict(refill_off=1),
    "refill_u0=-16":  dict(refill_u0=-PAR_COLS),
    "no-full-tile0":  dict(skip_first_full=True),
}

# Variations that are NOT defects.  Listing them is not padding the table: an
# undetected change is only reassuring once you know whether it changed
# anything, and each of these has a reason it cannot.
#
#   unguarded-shift   copies the full SEG_W-PAR_COLS span instead of stopping
#                     at the overlap.  Everything it writes above the overlap
#                     is either refilled immediately or sits at an index >=
#                     seg_len that no lane reads.  The guard in the shipped
#                     source is there so the C++ does not READ uninitialised
#                     storage, not because the values matter.
#
#   pad=clamp         replaces the out-of-patch zeros with the last real pixel.
#                     Inert for a sharper reason, re-proved by --selftest: an
#                     index >= pw is only ever read by a lane whose output
#                     column is >= rw, and those lanes are masked at writeback.
#                     The `idx < pw` GUARD is still required -- the largest
#                     index it stops is pw + 14 = 834 against patch_line[820],
#                     so removing it is an out-of-bounds read.  What is inert
#                     is the VALUE it substitutes, not the test itself.
INERT = {
    "unguarded-shift": dict(guard_shift=False),
    "pad=clamp":       dict(pad="clamp"),
}

STALE_ALL = range(256)


def _passes(c: dict, sti) -> bool:
    gx, gy, sc = dut(c["patch"], c["templ"], sti)
    return (gx, gy) == (c["x"], c["y"]) and abs(sc - c["score"]) <= SCORE_TOL


def _n_stale_breaking(c: dict, kw: dict) -> int:
    """How many of the 256 stale fills make this case FAIL under this mutant.

    Most mutants read registers the PREVIOUS TILE wrote, so their result does
    not depend on the fill at all; two evaluations detect that and settle all
    256 at once.  Only a mutant that reaches above seg_len-1 -- where nothing
    has ever been written -- needs the sweep, and then it gets the full one.
    """
    a = sim(c["patch"], c["templ"], 0, **kw)
    b = sim(c["patch"], c["templ"], 255, **kw)
    if np.array_equal(a, b):
        return 0 if _passes(c, a) else 256
    return sum(0 if _passes(c, sim(c["patch"], c["templ"], s, **kw)) else 1
               for s in STALE_ALL)


# ---------------------------------------------------------------------------
# 4. The padding argument, re-proved rather than asserted
# ---------------------------------------------------------------------------

def selftest_padding() -> tuple[int, int]:
    """Out-of-patch segment elements are read only by MASKED lanes.

    seg[i] holds patch column u0 + i.  Lane p of the tile at u0 reads
    seg[p + x] for x < tw and contributes to output column u = u0 + p, which is
    written back only when u < rw = pw - tw + 1.  So for any lane that is NOT
    masked, u0 + p + x <= (rw - 1) + (tw - 1) = pw - 1: every index it reads is
    inside the patch row.  The substituted pad VALUE is therefore unreachable.

    The GUARD is a different question and has the opposite answer.  The largest
    index it has to stop is u0_max + seg_len - 1 = pw + 14, which at pw = 820
    is 834 -- past the end of patch_line[MAX_PATCH_W].  Removing the test is an
    out-of-bounds read even though the value it produces could not matter.

    Returns (violations, worst_index); violations must be 0.
    """
    bad = 0
    worst = 0
    for tw in list(range(4, 30)) + [100, 215, MAX_TEMPL_W]:
        for pw in range(tw, MAX_PATCH_W + 1):
            rw = pw - tw + 1
            T = -(-rw // PAR_COLS)
            seg_len = tw + PAR_COLS - 1
            for t in range(T):
                u0 = t * PAR_COLS
                worst = max(worst, u0 + seg_len - 1)
                for p in range(PAR_COLS):
                    if u0 + p >= rw:
                        continue                 # masked: never written back
                    for x in (0, tw - 1):        # extremes bracket the range
                        if u0 + p + x >= pw:
                            bad += 1
    return bad, worst


def selftest_self_healing(cases: list[dict]) -> tuple[int, list]:
    """A cross-invocation reuse defect SELF-HEALS after the first tw-1 columns.

    This is why `no-full-tile0` is caught by eight of the twelve cases and not
    by all twelve, and it is worth deriving rather than reporting as a bare
    count -- the same geometry will govern any later variant that carries state
    between calls, B0b included.

    Under `skip_first_full`, tile 0 shifts instead of loading, so seg[i] holds
    inherited data for i < overlap = tw - 1 and correct data above that (the
    refill always writes seg[tw-1 .. tw+14] from the right columns).  Tile 1
    shifts again: seg[i] = old seg[i + PAR_COLS], which is correct wherever
    i + 16 >= tw - 1.  So the damaged prefix shrinks by PAR_COLS per tile, and
    at tile t it is i < tw - 1 - 16*t.

    Lane p of tile t reads seg[p .. p+tw-1] and writes output column
    u = 16*t + p, so it is damaged only when p < tw - 1 - 16*t, i.e. when

        u = 16*t + p  <  tw - 1

    independently of t.  THE DAMAGED SET IS EXACTLY THE OUTPUT COLUMNS
    u < tw - 1, however many tiles the map has.

    The consequence for test design is sharp: a case detects this defect only
    if its argmax lies in the first tw - 1 columns.  Wide templates with peaks
    at the far right -- which is where the WIDTH sweep deliberately puts them,
    to test the tile break -- are blind to it by construction, and no amount of
    extra width cases would help.

    Returns (violations, per-case rows).  Violations must be 0.
    """
    bad = 0
    rows = []
    for c in cases:
        ref = sim(c["patch"], c["templ"], 0)
        mut = sim(c["patch"], c["templ"], 0, skip_first_full=True)
        diff = ref != mut
        cols = np.flatnonzero(diff.any(axis=0))
        max_u = int(cols.max()) if cols.size else -1
        limit = c["tw"] - 1
        in_bound = max_u < limit
        predicted = c["x"] < limit           # argmax inside the damaged set
        observed = not _passes(c, mut)
        if not in_bound or predicted != observed:
            bad += 1
        rows.append(dict(tag=c["tag"], tw=c["tw"], max_u=max_u, limit=limit,
                         argmax_x=c["x"], predicted=predicted,
                         observed=observed, in_bound=in_bound))
    return bad, rows


# ---------------------------------------------------------------------------
# 5. Report
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assert", dest="do_assert", action="store_true",
                    help="exit 1 if the suite fails to detect any defect")
    ap.add_argument("--selftest", action="store_true",
                    help="re-prove the padding and self-healing arguments")
    ap.add_argument("--suite", default="b1")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    t0 = time.time()
    bad = 0
    cases = load_suite(args.suite)          # the self-healing selftest reads it

    if args.selftest or args.do_assert:
        viol, worst = selftest_padding()
        print(f"PADDING ARGUMENT: {viol} unmasked lane(s) read past the patch "
              f"row (must be 0)")
        print(f"  largest index the `idx < pw` guard stops: {worst} against "
              f"patch_line[{MAX_PATCH_W}] -- the guard is required for MEMORY "
              f"SAFETY,\n  the value it substitutes is unreachable")
        print()
        if viol:
            bad += 1

        heal_bad, heal = selftest_self_healing(cases)
        print("SELF-HEALING ARGUMENT: a cross-invocation reuse defect damages "
              "EXACTLY the output")
        print("columns u < tw - 1, however many tiles the map has -- so a case "
              "detects it only if")
        print("its argmax lies there.  This is what explains the "
              "`no-full-tile0` column below.")
        print()
        print(f"  {'case':<22} {'tw-1':>5} {'max damaged u':>14} "
              f"{'argmax u':>9} {'predicted':>10} {'observed':>9}")
        for r in heal:
            mark = "" if (r["in_bound"] and r["predicted"] == r["observed"]) \
                else "   <-- ARGUMENT FAILS"
            print(f"  {r['tag']:<22} {r['limit']:5d} {r['max_u']:14d} "
                  f"{r['argmax_x']:9d} "
                  f"{'detects' if r['predicted'] else 'blind':>10} "
                  f"{'detects' if r['observed'] else 'blind':>9}{mark}")
        print()
        if heal_bad:
            bad += heal_bad
        if args.selftest and not args.do_assert:
            return 1 if bad else 0

    # The honest implementation first.  Without this the table below would be
    # a list of ways to break something that was never right.
    print(f"HONEST B2 against the pinned `{args.suite}` suite")
    honest = {}
    for c in cases:
        base = sim(c["patch"], c["templ"], 0)
        ok_ref = np.array_equal(base, reference(c["patch"], c["templ"]))
        ok_stale = all(np.array_equal(sim(c["patch"], c["templ"], s), base)
                       for s in (255, 7, 128))
        ok_gold = _passes(c, base)
        honest[c["tag"]] = dict(reference=ok_ref, stale_independent=ok_stale,
                                golden=ok_gold)
        if not (ok_ref and ok_stale and ok_gold):
            bad += 1
            print(f"  {c['tag']:<22} ref={ok_ref} stale_indep={ok_stale} "
                  f"golden={ok_gold}   <-- FAIL")
    n_ok = sum(1 for v in honest.values() if all(v.values()))
    print(f"  {n_ok}/{len(cases)} cases: the overlap-reuse map equals the "
          f"direct cross-correlation, is\n  independent of the stale fill, and "
          f"reproduces the golden (x, y) and score.")
    print()

    every = dict(DEFECTS)
    every.update(INERT)
    print(f"  {'case':<22} {'rw':>4} {'T':>2} " +
          " ".join(f"{m:>16s}" for m in every))
    best = {m: 0 for m in every}
    rows = []
    for c in cases:
        rw = c["pw"] - c["tw"] + 1
        T = -(-rw // PAR_COLS)
        cells, rec = [], {}
        for m, kw in every.items():
            n = _n_stale_breaking(c, kw)
            rec[m] = n
            best[m] = max(best[m], n)
            cells.append("-" if n == 0
                         else ("ALL 256" if n == 256 else f"{n}/256"))
        rows.append(dict(tag=c["tag"], rw=rw, T=T, breaks=rec))
        print(f"  {c['tag']:<22} {rw:4d} {T:2d} " +
              " ".join(f"{x:>16s}" for x in cells))

    print()
    print("  A cell counts the stale register fills for which the case FAILS "
          "the testbench\n  check.  `ALL 256` is an unconditional detection: "
          "no value the register file\n  could be holding lets that defect "
          "through.")
    print()
    for m in DEFECTS:
        ok = best[m] == 256
        print(f"  DEFECT {m:<15} best case breaks {best[m]:3d}/256 fills"
              + ("" if ok else "   <-- SUITE IS BLIND TO THIS DEFECT"))
        if not ok:
            bad += 1
    for m in INERT:
        ok = best[m] == 0
        print(f"  INERT  {m:<15} "
              + ("confirmed inert -- changes no result on any case"
                 if ok else
                 f"CHANGED A RESULT on {best[m]}/256 fills -- it is not inert "
                 f"and must be reclassified"))
        if not ok:
            bad += 1

    if args.json:
        args.json.write_text(json.dumps(
            dict(suite=args.suite, honest=honest, rows=rows, best=best,
                 failures=bad), indent=2), newline="\n")
        print(f"\nwrote {args.json}")

    print(f"\n[{time.time() - t0:.1f}s]")
    if args.do_assert:
        if bad:
            print(f"FAIL: {bad} problem(s)", file=sys.stderr)
            return 1
        print("OK -- the pinned suite detects all "
              f"{len(DEFECTS)} B2 defect classes unconditionally, the two "
              "inert\nvariations are inert, and the honest implementation "
              "reproduces every golden.\nThis is a property of the PYTHON "
              "transcription and of the SUITE.  It says nothing\nabout what "
              "Vitis compiled -- that is what csim and cosim are for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
