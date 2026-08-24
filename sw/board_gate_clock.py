#!/usr/bin/env python3
"""Board gate 6: the COUNTED clock check — is the fabric really at 100 MHz?

RUN THIS ON THE BOARD, after board_gate_extract.py PASSES:

    sudo -E python3 board_gate_clock.py --overlay three_stage_combined.bit \
                                        --variant combined_b2_100

WHY THIS GATE EXISTS.  The preflight's clock check reads
`pynq.ps.Clocks.fclk0_mhz`, and that is a **divisor/register read-back**: PYNQ
computes it from its PLL model and the FCLK0 divisors in the SLCR.  It is
exactly the right check for the 62.5 MHz trap, which corrupts those divisors —
but it is not an edge count.  A PLL that failed to lock, or a clock that ran
at a rate the divisors do not describe, would still read a perfect 100.0.

Nothing else in gates 1-5 closes that.  They are all correctness gates, and
every one of them would pass at whatever clock the fabric happened to run at:
a slow matcher returns the same score, just later.  So the only independent
evidence available is TIME — the matcher's cycle count at a fixed geometry is
known exactly, so its wall time measures the clock.

THE PROBE.  Two invocations over the SAME 251,740-byte patch envelope,
differing only in the template:

    long   820x307 patch / 216x96 template
    short  820x307 patch /   4x4  template

Both are cases already in the pinned `hw` manifest (indices 7 and 8), so this
gate adds no vectors of its own and its arithmetic is checked against the same
golden scores gate 4 checks.

TWO READINGS, AND THE SECOND IS THE LOAD-BEARING ONE.

  absolute      t_long against L_long / f.  Simple, but contaminated by the
                fixed per-invocation overhead — arming two MM2S channels,
                writing four scalars, the polling granularity.  That overhead
                was +1.6 to +2.5 ms across the nine cases of the 2026-08-20
                B2 board session, always positive, so the absolute reading can
                only ever be LATE and its band has to be one-sided and loose.

  differential  (L_long - L_short) / (t_long - t_short).  Both probes program
                the identical 251,740-byte patch MM2S and differ only in a
                template transfer of 20,736 vs 16 bytes, so the per-invocation
                overhead very nearly cancels and what is left is 249,549,328
                modelled cycles of fabric time.  This is the number to quote.

Neither is a cycle counter.  The RTL has none, and this gate does not pretend
otherwise: the cycle totals are MODELLED, from the cycle law that RTL
co-simulation pinned, and what is measured is PS-side wall time.  What the
gate establishes is that the modelled cycles and the measured seconds are
consistent with the variant's board frequency and with no other divisor rung.

THE RUNG CHECK IS WHAT MAKES A LOOSE BAND SAFE.  Every frequency this board
can produce is 1000 / d for an integer divisor product d, so the reachable
neighbours of 100 MHz are 111.11 (d=9) and 90.91 (d=11) — 11% and 9% away —
and the trap rung is 62.5 (d=16).  A 0.5% band around the differential
reading cannot reach any of them, and the gate prints the nearest rung it
found, so a failure names the frequency the board actually ran at.

`--variant` decides the expected frequency AND the cycle law: the baseline and
`combined_current_100` carry `TermCount` (the `cur` law), `combined_b2_100`
carries `TermCountB2` (the `B2` law).  Reading the wrong law would be a way to
pass at the wrong clock, so it is derived from the variant table's VLNV and
never from a flag of its own.

Needs on the board, same directory as gate 4 (it reuses gate 4's helpers and
gate 4's three `hw` fixtures, hashed against `GATE4_VECTORS.sha256`):

    three_stage_combined.bit / .hwh
    board_expect.py, board_gate_extract.py, tme_driver.py,
    tme_standalone_bringup.py, safe_teardown.py
    GATE4_VECTORS.sha256, tb_tme_cases_hw.txt, tb_tme_patches_hw.bin,
    tb_tme_templs_hw.bin

Exit status: 0 = passed, 1 = a check FAILED (the clock, or the model, is not
what the variant says), 2 = could not run.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

import safe_teardown
import board_expect as X

try:
    from board_gate_extract import (GateError, Report, SetupError,
                                    load_hw_manifest, verify_fixtures)
except Exception as _exc:                                    # noqa: BLE001
    print(f"CANNOT RUN: board_gate_extract.py did not import "
          f"({type(_exc).__name__}: {_exc}) — gate 6 reuses its Report, its "
          f"fixture verification and its manifest loader, so copy it next to "
          f"this script.")
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# The cycle law.
#
# Transcribed from sw/tme_cycle_model.py's `cycles()`, which is where it is
# derived, commented and cross-checked against RTL co-simulation.  It is
# repeated here rather than imported because tme_cycle_model.py is a 150 KB
# analysis tool that discovers a corpus at import time and has no business on
# the board — but a transcription is a place for a typo to hide, so
# `test_board_gate_clock.py` asserts these two functions against
# `tme_cycle_model.cycles()` for every case in the hw manifest.
#
# NOT A HARDWARE CYCLE COUNTER.  These are the cycle counts RTL co-simulation
# measured for this core at this geometry.  Quote any total derived from them
# as MODELLED, never as measured hardware cycles.
# ---------------------------------------------------------------------------

def _common(pw: int, ph: int, tw: int, th: int):
    rw = pw - tw + 1
    rh = ph - th + 1
    if rw < 1 or rh < 1:
        return None
    return rw, rh, math.ceil(rw / 16)


def cycles(pw: int, ph: int, tw: int, th: int, law: str) -> int:
    """Cycles for one tme_top invocation at this geometry, by matcher law."""
    parts = _common(pw, ph, tw, th)
    if parts is None:
        return 0
    rw, rh, T = parts
    if law == "cur":
        tile = T * (tw + 257)
    elif law == "B2":
        tile = T * (tw + 44) + (tw - 2)
    else:
        raise ValueError(f"unknown matcher cycle law: {law!r}")
    stat = 3 * tw + 3 * rw + 33
    per_row = 5 * rw + 99
    return pw * ph + 24 + rh * per_row + rh * th * (tile + stat)


# Which cycle law each variant's matcher obeys.  Derived from the variant
# table's VLNV rather than stored twice: a build whose matcher changed without
# this mapping changing is a build this gate must refuse, not one it should
# silently time against the wrong law.
_LAW_BY_CORE = {"TermCount": "cur", "TermCountB2": "B2"}


def law_for(cfg: dict) -> str:
    core = cfg["matcher_vlnv"].split(":")[0]
    if core not in _LAW_BY_CORE:
        raise SetupError(
            f"no cycle law is pinned for matcher core {core!r} "
            f"({cfg['matcher_vlnv']}). This gate times the fabric against a "
            f"co-simulated cycle count; without the right law it would "
            f"compare a measured second against the wrong model and could "
            f"pass at the wrong clock. Add the law, with its provenance, "
            f"before using this gate on a new core.")
    return _LAW_BY_CORE[core]


# ---------------------------------------------------------------------------
# Probe geometries and bands.
# ---------------------------------------------------------------------------

LONG_GEOM = (820, 307, 216, 96)         # hw case 7, stress-max-envelope
SHORT_GEOM = (820, 307, 4, 4)           # hw case 8, stress-max-result
PATCH_BYTES = 820 * 307                 # 251,740 — identical for both

# Absolute residual band, seconds.  The upper bound is the fixed
# per-invocation overhead this gate cannot remove; 10 ms is four times the
# largest residual of the nine-case 2026-08-20 session (+2.5 ms) and is still
# 0.4% of the long probe at 100 MHz, nowhere near a divisor rung.  The lower
# bound is timer noise only: a measured time BELOW the modelled one means the
# fabric ran faster than the variant claims, which is a finding, not slack.
ABS_RESIDUAL_MIN = -0.0005
ABS_RESIDUAL_MAX = 0.010

# Differential band, as a fraction of the expected board frequency.  The
# nearest reachable rungs are 1000/9 = 111.11 and 1000/11 = 90.91, so 0.5%
# cannot reach one.  Widening this past a few percent would defeat the gate.
DIFF_TOL_FRAC = 0.005

SCORE_TOL = 0.005                       # 4.6, the same tolerance as gate 4

DEFAULT_REPS = 5


def nearest_rung(mhz: float, max_div: int = 64) -> tuple:
    """The (divisor, frequency) of the closest 1000/d rung to `mhz`.

    Every frequency this board can produce is 1000 / (div0 * div1) — see
    board_expect.BOARD_IO_PLL_MHZ — so naming the rung turns "the timing was
    off" into "the fabric ran at 1000/16".
    """
    best = min(range(1, max_div + 1),
               key=lambda d: abs(X.BOARD_IO_PLL_MHZ / d - mhz))
    return best, X.BOARD_IO_PLL_MHZ / best


def pick_probe(cases, geom, what: str):
    """The manifest case with exactly this geometry.  Ambiguity is fatal."""
    hits = [c for c in cases if (c.pw, c.ph, c.tw, c.th) == geom]
    if len(hits) != 1:
        raise SetupError(
            f"the hw manifest holds {len(hits)} cases at {geom[0]}x{geom[1]} "
            f"/ {geom[2]}x{geom[3]} ({what}); this gate needs exactly one. "
            f"The manifest changed — reconcile it before timing against it.")
    return hits[0]


def run_probe(pl, c, patches: bytes, templs: bytes, reps: int, rep: Report,
              label: str) -> float:
    """Run one probe `reps` times; return the MINIMUM elapsed time.

    The minimum, not the mean: every contaminant on this path is additive and
    positive (a scheduler preemption, an interrupt, a page fault), so the
    smallest of several runs is the closest to the fabric's own time, and the
    mean would drift with system load in the direction that flatters a SLOW
    clock.

    Every repetition's score and location are checked too.  A timing figure
    from an invocation that computed the wrong answer measures nothing.
    """
    patch = np.frombuffer(patches, dtype=np.uint8, count=c.patch_bytes,
                          offset=c.patch_off).reshape(c.ph, c.pw)
    templ = np.frombuffer(templs, dtype=np.uint8, count=c.templ_bytes,
                          offset=c.templ_off).reshape(c.th, c.tw)
    times = []
    for i in range(reps):
        score, x, y, secs = pl.match_template(patch, templ)
        ok = abs(score - c.score) <= SCORE_TOL and (x, y) == (c.x, c.y)
        rep.require(ok,
                    f"{label} rep {i + 1}/{reps} computed the golden result",
                    f"dut {score:+.6f} @({x},{y}) vs gold {c.score:+.6f} "
                    f"@({c.x},{c.y}), {secs:.4f} s")
        times.append(secs)
    lo, hi = min(times), max(times)
    print(f"  {label}: min {lo:.4f} s, max {hi:.4f} s, spread "
          f"{(hi - lo) * 1e3:.2f} ms over {reps} reps")
    return lo


def phase_probe(pl, data_dir: Path, cfg: dict, law: str, reps: int,
                rep: Report) -> dict:
    """Time both probes and check the clock three ways."""
    cases, patches, templs = load_hw_manifest(data_dir)
    long_c = pick_probe(cases, LONG_GEOM, "long probe")
    short_c = pick_probe(cases, SHORT_GEOM, "short probe")

    rep.require(long_c.patch_bytes == PATCH_BYTES
                and short_c.patch_bytes == PATCH_BYTES,
                f"both probes program the same {PATCH_BYTES:,} B patch MM2S",
                f"{long_c.patch_bytes:,} / {short_c.patch_bytes:,} B")

    l_long = cycles(*LONG_GEOM, law)
    l_short = cycles(*SHORT_GEOM, law)
    l_diff = l_long - l_short
    f_mhz = cfg["board_mhz"]

    print(f"\n--- modelled cycles ({law} law) ---")
    print(f"  long   {LONG_GEOM[0]}x{LONG_GEOM[1]}/"
          f"{LONG_GEOM[2]}x{LONG_GEOM[3]}   {l_long:>12,} cyc -> "
          f"{l_long / (f_mhz * 1e6):.4f} s at {f_mhz} MHz")
    print(f"  short  {SHORT_GEOM[0]}x{SHORT_GEOM[1]}/"
          f"{SHORT_GEOM[2]}x{SHORT_GEOM[3]}     {l_short:>12,} cyc -> "
          f"{l_short / (f_mhz * 1e6):.4f} s at {f_mhz} MHz")
    print(f"  diff                          {l_diff:>12,} cyc -> "
          f"{l_diff / (f_mhz * 1e6):.4f} s at {f_mhz} MHz")
    print("  MODELLED cycles, from the co-simulated cycle law — the RTL has "
          "no cycle counter.")

    print(f"\n--- measuring ({reps} reps each) ---")
    t_long = run_probe(pl, long_c, patches, templs, reps, rep, "long ")
    t_short = run_probe(pl, short_c, patches, templs, reps, rep, "short")

    # -- reading 1: absolute ------------------------------------------------
    predicted = l_long / (f_mhz * 1e6)
    residual = t_long - predicted
    print("\n--- absolute ---")
    print(f"  measured {t_long:.4f} s, modelled {predicted:.4f} s, residual "
          f"{residual * 1e3:+.2f} ms (band {ABS_RESIDUAL_MIN * 1e3:+.1f} .. "
          f"{ABS_RESIDUAL_MAX * 1e3:+.1f} ms)")
    rep.require(ABS_RESIDUAL_MIN <= residual <= ABS_RESIDUAL_MAX,
                f"the long probe's wall time matches {f_mhz} MHz "
                f"(one-sided: per-invocation overhead is additive)",
                f"{residual * 1e3:+.2f} ms")

    # -- reading 2: differential -------------------------------------------
    dt = t_long - t_short
    rep.require(dt > 0, "the long probe took longer than the short one",
                f"{dt * 1e3:+.2f} ms")
    implied = l_diff / (dt * 1e6)
    err = (implied - f_mhz) / f_mhz
    print("\n--- differential (per-invocation overhead cancels) ---")
    print(f"  dt {dt:.4f} s over {l_diff:,} cyc -> implied "
          f"{implied:.4f} MHz vs expected {f_mhz} MHz ({err * 100:+.3f}%)")
    rep.require(abs(err) <= DIFF_TOL_FRAC,
                f"the implied fabric clock is within "
                f"{DIFF_TOL_FRAC * 100:.1f}% of {f_mhz} MHz",
                f"{implied:.4f} MHz ({err * 100:+.3f}%)")

    # -- reading 3: the rung ------------------------------------------------
    div, rung = nearest_rung(implied)
    print(f"  nearest divisor rung: 1000/{div} = {rung:.4f} MHz")
    rep.require(abs(rung - f_mhz) <= X.CLOCK_TOL_MHZ,
                f"the nearest 1000/d rung to the implied clock is {f_mhz} MHz",
                f"1000/{div} = {rung:.4f} MHz")

    return {"t_long": t_long, "t_short": t_short, "l_long": l_long,
            "l_short": l_short, "implied_mhz": implied,
            "residual_s": residual, "rung_div": div, "law": law,
            "expected_mhz": f_mhz}


# ---------------------------------------------------------------------------

def selftest(variant_name: str) -> int:
    """Check the law, the bands and the rung arithmetic with no board."""
    print(f"board_gate_clock self-test (variant {variant_name})\n")
    rep = Report()
    try:
        cfg = X.variant(variant_name)
    except Exception as exc:                                 # noqa: BLE001
        print(f"CANNOT RUN: {exc}")
        return 2
    law = law_for(cfg)
    rep.check(law in ("cur", "B2"), "variant maps to a pinned cycle law",
              f"{cfg['matcher_vlnv']} -> {law}")

    l_long = cycles(*LONG_GEOM, law)
    l_short = cycles(*SHORT_GEOM, law)
    rep.check(l_long > l_short > 0,
              "the long probe models more cycles than the short one",
              f"{l_long:,} vs {l_short:,}")

    f = cfg["board_mhz"]
    rep.check(abs(X.board_mhz(cfg["div_product"]) - f) <= X.CLOCK_TOL_MHZ,
              "the variant's board_mhz is the one its divisors produce",
              f"1000/{cfg['div_product']} = "
              f"{X.board_mhz(cfg['div_product']):.4f}")

    # The separation that makes a loose band safe: no reachable rung may land
    # inside the differential tolerance.
    div, _ = nearest_rung(f)
    worst = min(abs(X.BOARD_IO_PLL_MHZ / d - f) / f
                for d in range(1, 65) if d != div)
    rep.check(worst > DIFF_TOL_FRAC * 2,
              "the nearest OTHER divisor rung is well outside the "
              "differential band",
              f"nearest other rung is {worst * 100:.2f}% away, band is "
              f"{DIFF_TOL_FRAC * 100:.1f}%")

    # And that the absolute band cannot reach one either.
    span = ABS_RESIDUAL_MAX / (l_long / (f * 1e6))
    rep.check(span < worst,
              "the absolute band is narrower than the gap to the nearest "
              "other rung",
              f"band is {span * 100:.3f}% of the modelled time, gap is "
              f"{worst * 100:.2f}%")

    print(f"\n{rep.checks - len(rep.failures)}/{rep.checks} checks passed")
    if rep.failures:
        print("SELF-TEST FAILED: " + "; ".join(rep.failures))
        return 1
    print(f"PASS: {law} law, {f} MHz expected; long probe models "
          f"{l_long:,} cyc = {l_long / (f * 1e6):.4f} s, differential "
          f"{l_long - l_short:,} cyc = "
          f"{(l_long - l_short) / (f * 1e6):.4f} s.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--overlay", default="three_stage_combined.bit")
    ap.add_argument("--data-dir", default=".",
                    help="directory holding the tb_tme_*_hw.* vectors")
    ap.add_argument("--variant", default=X.DEFAULT_VARIANT,
                    help="build variant: decides the expected board clock AND "
                         "the matcher cycle law")
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS,
                    help="repetitions per probe; the MINIMUM is used")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.variant)

    if args.reps < 1:
        print("CANNOT RUN: --reps must be at least 1")
        return 2

    try:
        cfg = X.variant(args.variant)
        law = law_for(cfg)
        verify_fixtures(Path(args.data_dir))
    except SetupError as exc:
        print(f"CANNOT RUN: {exc}")
        return 2
    except Exception as exc:                                 # noqa: BLE001
        print(f"CANNOT RUN: {type(exc).__name__}: {exc}")
        return 2

    try:
        import tme_driver as driver
    except Exception as exc:                                 # noqa: BLE001
        print(f"CANNOT RUN: tme_driver.py did not import "
              f"({type(exc).__name__}: {exc})")
        return 2

    print(f"board_gate_clock — overlay {args.overlay}, variant {args.variant}")
    print(f"expecting {cfg['board_mhz']} MHz (1000/{cfg['div_product']}), "
          f"matcher {cfg['matcher_vlnv']} -> {law} cycle law")

    try:
        pl = driver.PLPipeline(args.overlay, timeout_s=args.timeout)
    except Exception as exc:                                 # noqa: BLE001
        print(f"CANNOT RUN: the overlay would not load "
              f"({type(exc).__name__}: {exc}).")
        return 2

    try:
        armed = safe_teardown.arm_teardown_protection()
    except safe_teardown.TeardownUnprotected as exc:
        print(f"CANNOT RUN: {exc}")
        return safe_teardown.teardown(pl, args.overlay, 2)
    print(f"  teardown protection: ignoring {', '.join(armed)}")

    rep = Report()
    status = 0
    out = None
    try:
        out = phase_probe(pl, Path(args.data_dir), cfg, law, args.reps, rep)
    except GateError as exc:
        print(f"\nCHECK FAILED at: {exc}")
        status = 1
    except SetupError as exc:
        print(f"\nCANNOT RUN: {exc}")
        status = 2
    except Exception as exc:                                 # noqa: BLE001
        print(f"\nERROR {type(exc).__name__}: {exc}")
        status = 1
    finally:
        status = safe_teardown.teardown(pl, args.overlay, status)

    print("\n" + "=" * 72)
    if status == 0 and not rep.failures and out:
        print(f"CLOCK GATE PASSED ({rep.checks} checks): the differential "
              f"probe implies {out['implied_mhz']:.4f} MHz against an "
              f"expected {out['expected_mhz']} MHz "
              f"(1000/{out['rung_div']}), absolute residual "
              f"{out['residual_s'] * 1e3:+.2f} ms.")
        print("This is an INDEPENDENT check on the fabric clock: the "
              "preflight's Clocks.fclk0_mhz is a divisor read-back, whereas "
              "this is wall time against a co-simulated cycle count. The "
              "cycle totals are MODELLED — the RTL has no cycle counter — "
              "and what was measured is PS-side wall time.")
        return 0
    print(f"CLOCK GATE FAILED ({len(rep.failures)} of {rep.checks} checks): "
          + "; ".join(rep.failures[:6]))
    return status or 1


if __name__ == "__main__":
    sys.exit(main())
