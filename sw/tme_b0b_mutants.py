#!/usr/bin/env python3
"""Priority 6 (B0b): can the shadow comparison actually fail?

The shadow build computes the hoisted, vertically-reused window statistics
ALONGSIDE the loops they replace and compares them at every result position.
Across the b1, csim, hw, b0b and prod suites it reported zero mismatches.

"Zero mismatches" is also what a comparison that cannot fail would report.
The C simulation already prints the number of positions actually compared per
invocation, which rules out the comparison never running -- but not a
comparison that runs and is blind.  This file rules that out the other way, by
BREAKING the hoisted pass on purpose and requiring the shadow to notice.

HOW A MUTANT IS DETECTED.  The shadow build reports a disagreement through the
existing result registers, as an unreachable score of -3.0f at
(0xFFFF, 0xFFFF).  Nothing else is needed: tme_tb.cpp already checks score
bits and exact location, so a mutant that changes any window statistic
anywhere in the suite turns into testbench failures.  A mutant is DETECTED
when csim exits non-zero, and BLIND when it passes.

WHAT THIS DOES AND DOES NOT COVER
---------------------------------
The sweep runs `csim_design -argv b1`: twelve banking-boundary cases plus the
two direct DUT tests, spanning rh in {1, 6, 7, 10, 16, 24} and th in
{4, 8, 12, 16}.  It is the fast suite, and vertical reuse has room to show
there.  It is NOT the corner suite: `-argv b0b` carries the whole csim
manifest with it (three and a half minutes a run) and is too slow to sweep.
So this file establishes that the SHADOW MECHANISM detects these defect
classes on this suite; the corners are evidence about the implementation, not
about the mechanism.

THE CONTROLS MATTER AS MUCH AS THE MUTANTS.  Two entries below must PASS:

  * `none` -- the unmutated shadow.  If it failed, every "detected" verdict
    would be meaningless.
  * `swap_sub_add` -- the incoming row added BEFORE the outgoing row is
    subtracted.  That is a real reordering of the arithmetic and it is
    CORRECT: modular addition commutes, and the only thing the shipped order
    buys is that every intermediate is the exact th-1-row sum, so no
    accumulator can go negative even transiently.  Reversing it makes the
    intermediate a th+1-row sum, which also fits (97*216*255 = 5,342,760 <
    2^23 and 97*216*255^2 = 1,362,403,800 < 2^31).  A gate that flagged this
    would be flagging a difference that is not a defect.  It is here to show
    the gate discriminates rather than just failing everything it is handed.

    Read that precisely: it shows the ORDER claim in the source is about
    which argument carries the correctness, not about the result.

    python tme_b0b_mutants.py            # run the sweep
    python tme_b0b_mutants.py --assert   # exit 1 unless every verdict is right
    python tme_b0b_mutants.py --list     # show the edits without building

Each run builds a scratch project `template_match_b0b_mut_<name>` and leaves
it in place; they are gitignored build output, not evidence.  The mutated
sources go to hls/template_match/b0b_sources/mutants/, never over a pinned
snapshot -- run_hls_b0b_mutant.tcl refuses if handed one.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tme_b1_ab import HLS                                      # noqa: E402

SNAPSHOT = HLS / "b0b_sources" / "tme_top.shadow.cpp"
MUTDIR = HLS / "b0b_sources" / "mutants"
TCL = "run_hls_b0b_mutant.tcl"
VITIS = Path(r"C:\AMDDesignTools\2025.2\Vitis\bin\vitis-run.bat")

# name -> (must_be_detected, [(old, new), ...], why)
#
# Every `old` is required to appear EXACTLY ONCE in the snapshot.  That is not
# tidiness: `if (u >= rw) break;` occurs in both the hoisted scan and the
# shipped isq_slide, and a mutation that silently hit the wrong one would be
# testing the thing it is supposed to be the control for.
MUTANTS: dict[str, tuple] = {
    "none": (False, [], "the unmutated shadow — the positive control"),
    "sub_off_by_one": (True, [
        ("window_row_scan(patch_buf[v - 1], sii_shadow, si_shadow,",
         "window_row_scan(patch_buf[v], sii_shadow, si_shadow,"),
    ], "subtract row v instead of the row leaving the window, v-1"),
    "add_off_by_one": (True, [
        ("window_row_scan(patch_buf[v + th - 1], sii_shadow, si_shadow,",
         "window_row_scan(patch_buf[v + th], sii_shadow, si_shadow,"),
    ], "add row v+th instead of the row entering the window, v+th-1"),
    "no_set": (True, [
        ("tw, rw, dy == 0 ? B0B_SET : B0B_ADD);",
         "tw, rw, B0B_ADD);"),
    ], "never SET, so each invocation accumulates onto the previous one's "
       "leftovers — the cross-invocation defect"),
    "sub_becomes_add": (True, [
        ("window_row_scan(patch_buf[v - 1], sii_shadow, si_shadow,\n"
         "                            tw, rw, B0B_SUB);",
         "window_row_scan(patch_buf[v - 1], sii_shadow, si_shadow,\n"
         "                            tw, rw, B0B_ADD);"),
    ], "add the outgoing row instead of subtracting it"),
    "short_slide": (True, [
        ("#pragma HLS LOOP_TRIPCOUNT min=1 max=816 avg=400\n"
         "        if (u >= rw) break;",
         "#pragma HLS LOOP_TRIPCOUNT min=1 max=816 avg=400\n"
         "        if (u >= rw - 1) break;"),
    ], "stop the scan one column short, leaving the last output column "
       "stale"),
    "swap_sub_add": (False, [
        ("window_row_scan(patch_buf[v - 1], sii_shadow, si_shadow,\n"
         "                            tw, rw, B0B_SUB);\n"
         "            window_row_scan(patch_buf[v + th - 1], sii_shadow, "
         "si_shadow,\n"
         "                            tw, rw, B0B_ADD);",
         "window_row_scan(patch_buf[v + th - 1], sii_shadow, si_shadow,\n"
         "                            tw, rw, B0B_ADD);\n"
         "            window_row_scan(patch_buf[v - 1], sii_shadow, "
         "si_shadow,\n"
         "                            tw, rw, B0B_SUB);"),
    ], "CONTROL, must NOT be detected: add before subtract is correct, it "
       "only makes the intermediate a th+1-row sum instead of a th-1-row one"),
}


def build(name: str, edits: list[tuple[str, str]]) -> Path:
    src = SNAPSHOT.read_text(encoding="utf-8")
    for old, new in edits:
        n = src.count(old)
        if n != 1:
            raise SystemExit(
                f"mutant {name}: anchor occurs {n} times, expected exactly "
                f"once:\n---\n{old}\n---\n"
                f"An anchor that matches twice would mutate the wrong loop, "
                f"and an anchor that matches zero times would produce a "
                f"mutant identical to the control that then 'passes'.")
        src = src.replace(old, new, 1)
    MUTDIR.mkdir(parents=True, exist_ok=True)
    out = MUTDIR / f"tme_top.mut_{name}.cpp"
    out.write_text(src, encoding="utf-8", newline="\n")
    return out


def run(name: str, path: Path) -> tuple[bool, str]:
    """True if csim DETECTED a defect (exited non-zero)."""
    env = dict(os.environ, TME_B0B_MUT_NAME=name, TME_B0B_MUT_SRC=str(
        path.relative_to(HLS)).replace("\\", "/"))
    p = subprocess.run([str(VITIS), "--mode", "hls", "--tcl", TCL],
                       cwd=HLS, env=env, capture_output=True, text=True)
    log = p.stdout + p.stderr
    (MUTDIR / f"csim_{name}.log").write_text(log, encoding="utf-8")
    passed = "TESTBENCH PASSED" in log and "CSim done with 0 errors" in log
    failed = ("TESTBENCH FAILED" in log
              or "CSim failed" in log
              or "csim.exe" in log and "ERROR" in log)
    if passed == failed:
        return None, ("indeterminate: the log says neither a clean pass nor a "
                      "clean failure — see " f"{MUTDIR / f'csim_{name}.log'}")
    return (not passed), ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assert", dest="strict", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", help="run one mutant by name")
    args = ap.parse_args()

    if not SNAPSHOT.exists():
        raise SystemExit(f"pinned shadow snapshot missing: {SNAPSHOT}")

    names = [args.only] if args.only else list(MUTANTS)
    if args.list:
        # build() is called for its side effect of VALIDATING every anchor.
        # An anchor that matches zero times, or twice, is the failure mode
        # this whole file is most exposed to: it would produce a "mutant"
        # that is not the defect it claims to be, and its verdict would then
        # be meaningless in the reassuring direction.
        for n in names:
            must, edits, why = MUTANTS[n]
            build(n, edits)
            print(f"{n:<18} {'must detect' if must else 'must PASS':<12} {why}")
            for old, new_ in edits:
                for line in old.splitlines():
                    print(f"    - {line}")
                for line in new_.splitlines():
                    print(f"    + {line}")
        print()
        print(f"every anchor matched exactly once in {SNAPSHOT.name}")
        return 0

    print("PRIORITY 6 (B0b) — shadow-comparison adequacy")
    print("=" * 74)
    print(f"snapshot: {SNAPSHOT}")
    print(f"suite:    csim_design -argv b1  (12 manifest + 2 direct)")
    print()
    wrong = []
    for n in names:
        must, edits, why = MUTANTS[n]
        path = build(n, edits)
        detected, note = run(n, path)
        if detected is None:
            verdict, ok = "INDETERMINATE", False
        else:
            verdict = "detected" if detected else "passed"
            ok = (detected == must)
        print(f"  {n:<18} {'must detect' if must else 'must PASS ':<12} "
              f"-> {verdict:<14} {'OK' if ok else '**WRONG**'}")
        if note:
            print(f"      {note}")
        if not ok:
            wrong.append(n)
    print()
    if wrong:
        print("WRONG VERDICTS: " + ", ".join(wrong))
        print("A must-detect mutant that passes means the shadow comparison is")
        print("blind to that defect class on this suite.  A control that fails")
        print("means the gate flags a non-defect.")
        return 1 if args.strict else 0
    print(f"all {len(names)} verdicts as required — the shadow comparison")
    print("detects every must-detect defect class on this suite, and passes")
    print("the two controls.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
