#!/usr/bin/env python3
"""Frozen cycle model and workload evidence for the template-matching engine.

Priority 0 artifact.  Every performance claim in the phase plan is regenerated
here from source rather than transcribed, so that "Phase S is 17.784x" is a
statement this file can be asked to re-prove rather than a number in a document.

    python tme_cycle_model.py                 # print the evidence
    python tme_cycle_model.py --assert        # re-prove the frozen figures, exit 1 on drift
    python tme_cycle_model.py --json out.json # dump the machine-readable freeze

Run it with the HLS venv python (it imports the detector for the workload):

    C:/Users/lychee/Desktop/FPGA/hls/.venv/Scripts/python.exe tme_cycle_model.py --assert

WHAT IS MEASURED AND WHAT IS MODELLED
-------------------------------------
TWO variants are backed by silicon: `cur` and `B1`.

`cur` reproduces every one of the nine RTL-cosim transactions exactly and lands
within 2.6 ms of all four board measurements, at two PL clocks (31.25 MHz
shipping, 125 MHz probe).

`B1` was implemented, co-simulated against `cur` as a paired A/B, routed at
8.000 ns (WNS +0.134571 ns) and RUN ON THE BOARD on 2026-08-19 -- 7/7 cases at
an observed, fail-closed-gated 125.0000 MHz, using a runner and vectors
byte-identical to the Priority 3 session that measured the unmodified core, so
only the bitstream differed.  Six workload-width cases moved by the modelled
amount to within the +/-1 ms print floor.

Board agreement is nevertheless COARSE: +/-1 ms at 125 MHz is +/-125,000
cycles, three orders of magnitude looser than the co-simulation.  The board
says the term is right at workload widths; the cosim is what makes it exact.

`B2` was implemented and co-simulated as a paired A/B against `b1` on
2026-08-19 -- 14/14 transactions exact, control intact on all 14.  Its tile
term is therefore MEASURED.  It has NOT been run on the board and has no
board session of its own.

B0b remains unimplemented and its term is unmeasured.

ALL page-level figures here -- for EVERY variant, `cur` included -- are
PROJECTIONS: each sums a per-invocation cycle term over the 20,680 modelled
trials.  NO PAGE FIGURE HERE IS A MEASURED PAGE TIME.  No page has been run on
hardware at any clock.

They are not, however, all projections of the same kind, and an earlier revision
of this docstring flattened them by saying "no RTL implementing them exists".
That is now false for B1 and has been since 2026-08-18:

  B1    RTL EXISTS (hls/template_match/b1_sources/correlation_core.b1.cpp).  Its
        TILE TERM is measured -- paired RTL co-simulation, 14/14 transactions
        exact -- and silicon-corroborated, 7/7 vectors at a gated 125.0 MHz on
        2026-08-19.  Only the summation over the workload is projected.
  B2    RTL EXISTS (hls/template_match/b1_sources/correlation_core.b2.cpp).
        Its TILE TERM is measured -- paired RTL co-simulation against `b1`,
        14/14 transactions exact -- and its C/RTL co-simulation passes the
        pinned twelve-case suite.  NO BOARD SESSION EXISTS: unlike B1, nothing
        has run this core on silicon.  Only the summation over the workload is
        projected, and the clock it is summed at is borrowed from the routed
        result rather than observed on a board running THIS core.
  B0b   no RTL, and its count-pass II is bracketed [1, 3] rather than known, so
        it is quoted as two endpoints and never as one number.

Read the hierarchy below rather than this paragraph if the two ever disagree
again: the hierarchy is what the assertions are written against.

THE EVIDENCE HIERARCHY (do not collapse these tiers when quoting)
-----------------------------------------------------------------
  measured        unchanged standalone matcher: routed at 8.000 ns, observed
                  at 125.0 MHz on the board, 9/9 vectors, the four wall times
                  in BOARD_MEASUREMENTS below.  WHAT A BOARD "PASS" IS: the
                  runner requires |score - gold| <= 0.005 and an EXACT (x, y).
                  The location is exact; the score is checked against a
                  tolerance.  Nowhere in this file does a vector count assert
                  score equality, and none should be read as doing so.
  silicon-anchored the `cur` cycle formula, now validated at two clocks
  core-only projection  Phase S at the current schedule: 36.476 s/page
  cycle-validated workload projection
                  B1 26.334 s/page.  Read every word: the RTL EXISTS and its
                  CYCLE TERM is measured (paired RTL co-simulation, 14/14
                  transactions exact) and silicon-corroborated (board session
                  2026-08-19, 7/7 at a gated 125.0000 MHz -- score within
                  +/-0.005 and exact location, per the tolerance above -- same
                  vectors and runner as the Priority 3 run of the unmodified
                  core, only the bitstream changed; six workload-width cases
                  moved by the
                  modelled amount to within the +/-1 ms print floor).  The PAGE
                  FIGURE is still a projection, summing that term over 20,680
                  modelled trials.  NO PAGE HAS BEEN RUN, on any hardware, at
                  any clock.  "26.334 s/page" is not a measured page time and
                  must never be written as one.

                  Note what the board did NOT do: agreement there is at +/-1 ms
                  = +/-125,000 cycles, three orders of magnitude coarser than
                  the co-simulation.  The board says the term is right at
                  workload widths; the cosim is what makes it exact.
  cycle-validated, not silicon-corroborated
                  B2 20.405 s/page.  The RTL EXISTS and its cycle term is
                  measured (paired RTL co-simulation against `b1`, 14/14
                  transactions exact), but NO BOARD HAS RUN IT.  That is one
                  tier below B1, which has a board session, and the difference
                  is not cosmetic: B2 adds a 231-element shift register with a
                  per-register enable to a design that routed 8.000 ns with
                  0.134571 ns of slack.  Do not quote B2 at 125 MHz as though
                  the clock were established for it.
  architectural projection  B0b 17.743731 / 18.035794 at II=1 and II=3 on the
                  count pass -- no RTL exists, and the II itself is projected
                  rather than measured.  Both endpoints MOVED when B2 was
                  measured (from 17.513998 / 17.806062), because B0b is defined
                  as B2 minus the window statistics plus the count pass and so
                  inherits B2's term whole.  B1 and B2 are now two warnings
                  about this tier rather than one: B1's projected term was
                  optimistic by T+1 per (output row, template row).  Against
                  B2's pre-RTL projection the miss is 3T-1: B1's T+1 recurs,
                  plus an additional 2*(T-1).  Neither measured correction is
                  a correction anyone may apply to B0b in advance.
  unproved        the combined image at 125 MHz, and end-to-end page latency.
                  ("the modified core at 125 MHz" left this line on 2026-08-19:
                  the B1 image routed at 8.000 ns and ran 7/7 on the board with
                  FCLK0 gated at 125.0000 MHz.)

125 MHz being board-demonstrated raises the CONVERSION RATE from cycles to
seconds out of assumption and into measurement.  It does NOT promote B1 / B2 /
B0b from projections to results.

B0b: WHAT CHANGED, AND WHAT IS STILL PROJECTED
----------------------------------------------
An earlier revision of this file modelled B0b as `stat = 2*tw + 2*rw + 33` --
"hoisting removes 1x of the fitted 3*(tw+rw)" -- and reported 17.652, with an
"optimistic" variant reporting 12.604.  BOTH ARE WITHDRAWN.  That attribution
was a guess at how the fitted term splits, and it survived review only because
the assertion checked that 17.652 fell inside [17.514, 17.806] rather than
checking the endpoints themselves.  A range check cannot distinguish a correct
implementation from a wrong one that lands inside the range.

B0b is now modelled as what it actually does -- delete the window-statistics
sub-term and replace it with one hoisted, vertically-reused count pass:

    B0b(N) = B2 - rh*th*(tw + rw + 21) + count_pass(N)

    B2                     20.405165        MEASURED term, summed
    window statistics       2.807466        derived; the subtraction is asserted
    B0b base               17.597699        derived  (variant="B0b_base")
    count_pass(II=1)        0.146031755778  derived
    count_pass(II=3)        0.438095267333  derived
    -> endpoints           17.743730541333 and 18.035794052889

WITHDRAWN: 17.513998132444 / 17.806061644000, and the 17.367966 base they were
built on.  Nothing about the count pass changed -- the two count_pass terms
above are the same numbers they always were.  What changed is B2, which B0b
sits on top of.  Its miss versus the pinned pre-B2 projection is 3*T - 1 per
(output row, template row); 2*(T-1) is only the additional cost versus the
control-naive schedule.

Every term is now computed rather than transcribed.  The count pass follows the
no-row-cache algorithm -- initial vertical position scans th patch rows, one
horizontal row-count scan costs tw + (rw - 1) = pw iterations, and each of the
rh - 1 subsequent vertical shifts scans the outgoing and incoming rows at 2*pw:

    I = pw * [th + 2*(rh - 1)]  ==  pw * (2*ph - th)

    max Phase-S trial   311 * (96 + 2*63)  =      69,042 iterations
    whole 20,680-trial corpus              = 657,142,901 iterations

WHAT IS STILL PROJECTED, AND IT IS NOT THE ITERATION COUNT.  The MULTIPLIER is.
`scheduled_cycles_per_iteration` is an ACHIEVED INITIATION INTERVAL -- pipeline
throughput, not operator latency -- and pipeline setup/drain plus FSM overhead
are not modelled at all.  They stay unmodelled until synthesis reports the real
II.  1 and 3 bracket the plausible range; neither is a prediction of which one
obtains, and Priority 6 shadow mode is what decides.

The iteration count also encodes an ALGORITHM CHOICE, not a lower bound over
all implementations: it assumes each row scan performs the horizontal rolling
count.  A vertical-column-only implementation needs a further horizontal pass
and costs more; caching row counts instead equals exactly pw*ph for the stated
cached-row algorithm (because th + (rh - 1) == ph, the coefficient-1 form of
the expression above IS pw*ph).  If the RTL does either, this formula is wrong
and the endpoints move.

WHAT B0b DOES NOT FIX.  It removes only the tw + rw + 21 window-statistics
sub-term.  The other 2*tw + 2*rw + 12 survives untouched, and it is FULLY
ATTRIBUTED -- nothing in the per-(output row, template row) cost is unaccounted
for any more:

    template-row staging            2*tw + 3   <- the MEASURED critical path
    correlation writeback/control   2*rw + 8
    accum_rows FSM transition       1
                                    ---------
                                    2*tw + 2*rw + 12

Template-row staging is the binding path in the routed 8 ns build
(templ_buf BRAM output -> the partitioned t_row registers, logic level 0), so
it is the term that constrains the clock rather than the cycle count.  It needs
its own fix and B0b is not it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. THE CYCLE MODEL
# ---------------------------------------------------------------------------
# Zero-stall latency of one tme_top invocation, in PL clock cycles.
#
#   L = pw*ph + 24 + rh*(5*rw + 99) + rh*th*[ T*(tw+257) + 3*tw + 3*rw + 33 ]
#
# Term by term, against hls/template_match/tme_top.cpp:
#   pw*ph               load_patch, II=1
#   24                  template load + dt/n_px setup (constant at these sizes)
#   rh*(5*rw + 99)      per output row: reset_acc (II=1) + norm_cols (II=4)
#   rh*th*T*(tw+257)    correlation_core: T tiles, each a 232-deep load_seg
#                       plus a tw-deep MAC pipeline plus flush
#   rh*th*(3*tw+3*rw+33)
#                       t_row staging + isq_init + isq_slide, recomputed for
#                       every template row of every output row.  Fully split --
#                       note the four constants are 3 + 21 + 8 + 1 = 33, so the
#                       +33 is ACCOUNTED FOR INSIDE this split and is not a
#                       separate loop-overhead term:
#                         template-row staging          2*tw + 3   <- crit path
#                         window statistics             tw + rw + 21  <- B0b
#                         correlation writeback/control 2*rw + 8
#                         accum_rows FSM transition     1
#                       B0b removes ONLY the window statistics; the remaining
#                       2*tw + 2*rw + 12 survives and needs its own fix.
#
# T = ceil(rw/16) is the tile count at PAR_COLS=16.


def cycles(pw: int, ph: int, tw: int, th: int, variant: str = "cur") -> int:
    """Cycles for one tme_top invocation at this geometry."""
    rw = pw - tw + 1
    rh = ph - th + 1
    if rw < 1 or rh < 1:
        return 0
    T = math.ceil(rw / 16)

    if variant == "cur":
        # 232-cycle load_seg per tile, unconditionally (SEG_W = PAR_COLS + MAX_TEMPL_W).
        tile = T * (tw + 257)
    elif variant == "B1":
        # load_seg shortened to the runtime-required PAR_COLS + tw - 1.
        #
        # MEASURED, not projected.  This term was predicted as
        # T*(tw + (16+tw-1) + 25) = T*(2*tw + 40) and that prediction was
        # WRONG BY T + 1 cycles per (output row, template row) -- optimistic,
        # in the direction that flatters the change.  Paired RTL co-simulation
        # of the same 14 invocations through the unmodified and the shortened
        # correlation_core (sw/tme_b1_ab.py, 2026-08-18) gives
        #
        #     tile = T * (2*tw + 41) + 1
        #
        # exactly, on 14/14 transactions spanning T in {1,2,3,5,6} and tw in
        # {4,16,20,24,100,216}.  Count the constraints honestly.  Under the
        # fitted form the residual depends only on T, so same-T transactions
        # restate one equation: FIVE independent equations, two free
        # parameters, THREE surplus constraints.  Of the nine remaining
        # observations, eight sit at an already-constrained T but a DIFFERENT
        # geometry and so test geometry invariance (that the residual is a
        # function of T alone); exactly one is a true repeat.  What carries the
        # most weight is the separate ZERO-parameter check of this closed form
        # against all thirteen distinct geometries.
        #
        # WHAT IS MEASURED is the SHAPE of the overhead: it scales as
        # T + 1 per (output row, template row), i.e. one term proportional to
        # the tile count and one constant per correlation_core call.  That much
        # the fourteen transactions pin.
        #
        # WHY it is there is NOT established.  Replacing the per-iteration
        # `i >= seg_len` predicate with a hoisted clamped bound changed nothing
        # at all (solution `b1b`, byte-identical report), so the SOURCE-LEVEL
        # FORM of the test is ruled out.  Nothing more: `b1b` still carries a
        # runtime loop bound (`i < seg_n`), so no experiment here separates
        # runtime-bounded control from any other cause the two share.  Call it
        # dynamic-bound overhead and leave it unlocalized; do not quote a
        # mechanism as established.
        #
        # B2 and B0b must be measured on their own.  Nothing here licenses
        # assuming they pay T + 1, or that they pay only T + 1.
        tile = T * (2 * tw + 41) + 1
    elif variant in ("B2", "B0b_base"):
        # Overlap reuse: tile 0 loads the full seg_len, each later tile slides
        # the overlap down by PAR_COLS and refills only PAR_COLS = 16 new
        # pixels.
        #
        # MEASURED, not projected.  Paired RTL co-simulation of the b2 solution
        # against the b1 one -- the same fourteen invocations, the same pinned
        # vectors, only correlation_core changed (sw/tme_b2_ab.py, 2026-08-19)
        # -- gives
        #
        #     tile = T * (tw + 44) + tw - 2
        #
        # exactly, on 14/14 transactions spanning T in {1,2,3,5,6} and tw in
        # {4,16,20,24,100,216}.  The control reproduced its own published B1
        # term on all 14 in the same comparison, which is what makes the
        # difference attributable to the reuse.
        #
        # WHAT THIS REPLACED, AND WHAT THE MISS WAS.  The projected term was
        # T*(tw + 41) + (tw - 1) -- written in the same style as B1's withdrawn
        # projection, i.e. "the reuse saves exactly the pixels it does not
        # re-read", counting 25 per tile and nothing per call.  It is retained
        # in ancestor commit e762cbf before the B2 source/build commit.  That is
        # repository ordering, not an external timestamp.  The second
        # candidate, B1's measured
        # term minus the naive saving, is a BASELINE computed after the fact by
        # tme_b2_ab.py; the two differ by rh*th*(T + 1).  THE MEASUREMENT
        # MATCHED NEITHER.  The shortfall against the naive reuse arithmetic is
        #
        #     rh * th * 2 * (T - 1)      i.e. (a, b) = (+2, -2) in a*T + b
        #
        # BUT THAT IS THE SHORTFALL AGAINST A BASELINE THAT ALREADY CARRIES
        # B1's CORRECTION.  Measured against the retained pre-RTL projection --
        # the like-for-like comparison, since that is the one B1 also made --
        # the miss is rh*th*(3T - 1) = rh*th*((T + 1) + 2*(T - 1)).  B1's
        # T + 1 DID recur; at T = 1 it is the whole miss.  What is new is the
        # additional 2*(T - 1).  Quote the shortfall only with its baseline
        # named, and see tme_b2_ab.py check 4, which enforces this.
        #
        # THE SHAPE IS MEASURED; THE MECHANISM IS NOT.  Two cycles per tile
        # beyond the pixels, minus two per call, is what fourteen transactions
        # pin.  Whether that is the shift's own state, the t == 0 branch, or
        # the extra loop region is NOT established -- no experiment here
        # separates them, exactly as with B1's T + 1.  Do not quote a cause,
        # and do not assume B0b pays this shape either.
        #
        # CONSEQUENCE WORTH KNOWING: the saving over B1 is (T - 1)*(tw - 3) per
        # (output row, template row).  It is ZERO at T = 1 -- a single-tile
        # invocation has nothing to reuse and the measurement confirms an exact
        # tie on all five such transactions -- and positive for every legal
        # geometry with more than one tile, since contract 4.1 puts tw >= 4.
        # Unlike B1, which LOSES at tw = 216, B2 is never a regression.
        tile = T * (tw + 44) + (tw - 2)
    else:
        raise ValueError("unknown variant: " + variant)

    if variant == "B0b_base":
        # B0b DELETES the window-statistics sub-term (tw + rw + 21) from the
        # per-(output row, template row) cost and replaces it with one hoisted,
        # vertically-reused count pass costed separately by cycles_b0b().
        #   (3*tw + 3*rw + 33) - (tw + rw + 21)  =  2*tw + 2*rw + 12
        # This is the DELETION ONLY; it is not a runnable B0b on its own.
        stat = 2 * tw + 2 * rw + 12
    else:
        stat = 3 * tw + 3 * rw + 33

    return pw * ph + 24 + rh * (5 * rw + 99) + rh * th * (tile + stat)


# ---------------------------------------------------------------------------
# 1b. THE B0b COUNT PASS
# ---------------------------------------------------------------------------
# B0b(N) = B2 - window_statistics + count_pass(N), where N is the achieved
# cycles per iteration of the new hoisted pass.  Two of the three terms are
# computed from the model above and asserted exactly:
#
#     B2                                    20.405165 s/page  (measured term)
#     window statistics  rh*th*(tw+rw+21)    2.807466 s/page
#     B0b base  (variant="B0b_base")        17.597699 s/page
#
# The third is now DERIVED as well -- see b0b_count_pass_iterations below.  What
# remains projected is the MULTIPLIER, not the iteration count.


# The per-(output row, template row) term, split into the four parts it is
# actually made of.  check() proves that THESE FOUR EXPRESSIONS sum to
# 3*tw + 3*rw + 33 and that dropping the window statistics leaves
# 2*tw + 2*rw + 12.  That is all it proves.  It does NOT verify that the prose
# in the module docstring, or the comment above, still describes these
# expressions -- no assertion can check English against code.  If you change a
# term here, re-read both by hand.
PER_ROW_TERMS = {
    "template_row_staging": lambda tw, rw: 2 * tw + 3,       # measured crit path
    "window_statistics": lambda tw, rw: tw + rw + 21,        # what B0b removes
    "correlation_writeback_control": lambda tw, rw: 2 * rw + 8,
    "accum_rows_fsm_transition": lambda tw, rw: 1,
}


def b0b_count_pass_iterations(pw: int, ph: int, tw: int, th: int) -> int:
    """Iterations of the hoisted, vertically-reused foreground-count pass.

    From the no-row-cache algorithm:

      * the initial vertical position scans th patch rows;
      * one horizontal row-count scan costs tw + (rw - 1) = pw iterations;
      * every subsequent vertical position scans the outgoing and the incoming
        row, so 2*pw, and there are rh - 1 such shifts.

          I = pw * [th + 2*(rh - 1)]  ==  pw * (2*ph - th)

    The identity holds because rh = ph - th + 1.  Both forms are asserted.

    ASSUMPTIONS THIS ENCODES.  Each row scan performs the HORIZONTAL ROLLING
    COUNT.  A vertical-column-only implementation would need a further
    horizontal pass and cost more; caching row counts instead costs exactly
    pw*ph for the stated cached-row algorithm -- note that is the coefficient-1
    form of the expression above, since th + (rh - 1) == ph.  If the eventual
    RTL does either, this function is wrong and the endpoints move; it is not a
    bound over all possible implementations.
    """
    rw, rh = pw - tw + 1, ph - th + 1
    if rw < 1 or rh < 1:
        return 0
    return pw * (th + 2 * (rh - 1))


def cycles_b0b(pw: int, ph: int, tw: int, th: int,
               scheduled_cycles_per_iteration: int) -> int:
    """B2 with the repeated window statistics replaced by one hoisted pass.

    `scheduled_cycles_per_iteration` is the ACHIEVED INITIATION INTERVAL of the
    count pass -- throughput, not operator latency.  IT IS PROJECTED, NOT
    MEASURED: pipeline setup/drain and FSM overhead are not modelled and stay
    unmodelled until synthesis reports the real II.  Priority 6 shadow mode is
    what supplies the true value; 1 and 3 bracket the plausible range and are
    frozen as endpoints, not as a prediction of which one obtains.
    """
    rw, rh = pw - tw + 1, ph - th + 1
    if rw < 1 or rh < 1:
        return 0
    old_stats = rh * th * (tw + rw + 21)
    return (cycles(pw, ph, tw, th, "B2")
            - old_stats
            + scheduled_cycles_per_iteration
            * b0b_count_pass_iterations(pw, ph, tw, th))


# ---------------------------------------------------------------------------
# 2. VALIDATION EVIDENCE
# ---------------------------------------------------------------------------
# hls/template_match/template_match/solution1/sim/report/verilog/result.transaction.rpt
# paired with tb_tme_cases_cosim.txt.  (pw, ph, tw, th) -> reported latency.
# The report holds NINE transactions.  Transaction 0 is a direct 12x10 / 4x4
# invocation that is not in tb_tme_cases_cosim.txt (which lists seven cases);
# transaction 1 is the 4x4/4x4 bound case.  Omitting either makes the docstring
# claim of "every RTL-cosim transaction" false, so the count is asserted below.
COSIM_TRANSACTIONS = [
    ((12, 10, 4, 4), 10476),      # transaction 0, direct minimum-context call
    ((64, 48, 64, 48), 29552),    # equality, identical
    ((80, 56, 20, 14), 855044),   # peak, final corner
    ((64, 48, 16, 12), 601904),   # peak, interior
    ((64, 48, 16, 12), 601904),   # degenerate, blank
    ((40, 30, 4, 4), 110304),     # edge, minimum 4x4
    ((64, 48, 64, 48), 29552),    # equality, different
    ((64, 48, 64, 48), 29552),    # equality, negative
    ((4, 4, 4, 4), 1380),         # transaction 1, bound_case minimum geometry
]
COSIM_TRANSACTIONS_IN_REPORT = 9    # result.transaction.rpt holds 0..8

# Board wall-clock measurements of the UNCHANGED standalone matcher, taken by
# sw/tme_standalone_bringup.py.  (geometry, clock, measured seconds, provenance)
#
# The 31.25 MHz row is the shipping image (2026-08-09).  The two 125 MHz rows
# are the Priority 2 clock probe (2026-08-17), raw transcript in
# logs/board_125mhz_gate/02_vectors_raw.txt lines 50-51; that probe measured
# Clocks.fclk0_mhz = 125.0 rather than requesting it.
# EVERY retained board timing for this core, not a selection.  Both stress
# cases were run at both clocks, which is what makes the linearity claim below
# testable in two regimes rather than one.
BOARD_MEASUREMENTS = [
    ("stress-max-envelope", (820, 307, 216, 96), 31.25e6, 13.362, "2026-08-07 shipping image"),
    ("stress-max-result",   (820, 307,   4,  4), 31.25e6,  0.676, "2026-08-07 shipping image"),
    ("stress-max-envelope", (820, 307, 216, 96), 125e6,    3.342, "2026-08-17 125 MHz probe"),
    ("stress-max-result",   (820, 307,   4,  4), 125e6,    0.171, "2026-08-17 125 MHz probe"),
]
# The board prints seconds rounded to milliseconds, so 0.5 ms of every residual
# is quantisation.  Residuals are all POSITIVE and grow as the compute shrinks:
# fixed DMA, control and polling overhead the cycle model does not describe.
#
# BOARD_TOLERANCE_S bounds model-vs-silicon agreement.  It does NOT freeze the
# measured values -- a 0.171 quietly edited to 0.170 stays inside it.  The
# measured seconds are frozen by exact comparison against FROZEN["board"]
# instead; do not rely on this tolerance to catch a transcription error.
BOARD_TOLERANCE_S = 0.005       # model must land within this

BOARD_FULL_ENVELOPE = (820, 307, 216, 96)
BOARD_CLOCK_HZ = 31.25e6        # the shipping image's measured PL rate
BOARD_SECONDS = 13.362          # measured, full envelope, 31.25 MHz

# 125 MHz is BOARD-DEMONSTRATED for the unchanged standalone matcher: routed at
# 8.000 ns with WNS +0.064 ns, and observed as Clocks.fclk0_mhz = 125.0 with
# 9/9 vectors passing.
#
# LINEARITY IS A COMPUTE-DOMINATED CLAIM, NOT A UNIVERSAL ONE.  The two clocks
# give two ratios, and only one of them is 4:
#
#   stress-max-envelope  13.362 / 3.342 = 3.998205   -0.045%  compute-dominated
#   stress-max-result     0.676 / 0.171 = 3.953216   -1.170%  overhead-dominated
#
# The envelope case is ~99.95% core cycles, so its ratio measures the clock.
# The 4x4 case moves the same 251,740 B patch but computes ~20x less, so its
# fixed DMA/control/polling cost is a visible share of the wall time and does
# NOT scale with the PL clock.  Quote "wall time is linear in clock" for the
# COMPUTE-DOMINATED case only; both ratios are asserted below so the
# distinction cannot be quietly dropped.
#
# What this licenses is the cycles-to-seconds conversion at 125 MHz for the
# unchanged core.  It says nothing about a modified core or the combined image.
TARGET_CLOCK_HZ = 125e6

# The tree root, resolved rather than assumed.  On the development machine this
# file lives at FPGA/.github-upload/sw/ and the evidence at FPGA/logs/; in a
# checkout sw/ and logs/ are siblings.  Hard-coding either made the corroboration
# passes below silently report ABSENT in the other, which reads exactly like
# "the transcripts are gone" and is how a portable tool stops corroborating
# anything without ever failing.
_SW_DIR = Path(__file__).resolve().parent
TREE_ROOT = (_SW_DIR.parents[1] if _SW_DIR.parent.name == ".github-upload"
             else _SW_DIR.parent)

PHASE_S_GEOMETRY = (311, 159, 216, 96)


# ---------------------------------------------------------------------------
# 3. THE WORKLOAD
# ---------------------------------------------------------------------------
# Candidate endpoints over the 36-page corpus, split by side.  The total is
# corroborated independently by the committed baseline manifest's `candidates`
# column, which sums to 738 across the same 36 pages.
CANDIDATES_LEFT = 371
CANDIDATES_RIGHT = 367
REFINEMENT_CALLS = 808
PAGES = 36


def png_size(path: Path):
    """(w, h) from a PNG IHDR, without needing cv2."""
    head = path.open("rb").read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: " + str(path))
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def template_files():
    """Absolute paths of the template PNGs the workload is built from.

    Same discovery the workload uses, exposed separately so a capture can hash
    the actual template bitmaps.  The template SET is what fixes the trial
    count, so a trace that does not pin it cannot prove which workload it
    measured.  Returns [] if the detector cannot be imported.
    """
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    try:
        import terminal_counter_endpoint_first as det
    except Exception:                                          # noqa: BLE001
        return []
    out = []
    for side in ("left", "right"):
        for kind in ("male", "female", "ferrule"):
            key = kind + "_" + side
            base = here / det.STANDARD_TEMPLATE_DIRS[key] / (key + ".png")
            out.extend(det.discover_template_paths(str(base)))
    # de-duplicate, preserving order: left and right can share a bitmap
    seen, uniq = set(), []
    for p in out:
        rp = str(Path(p).resolve())
        if rp not in seen:
            seen.add(rp)
            uniq.append(rp)
    return uniq


def discover_workload():
    """Template set and scale list, taken from the detector itself.

    Imported rather than transcribed so the model cannot silently drift from
    the code it claims to model.  Falls back to a filesystem scan when the
    detector's dependencies (cv2/fitz) are unavailable.
    """
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    try:
        import terminal_counter_endpoint_first as det
        scales = tuple(det.MATCH_SCALES)
        templates = {}
        for side in ("left", "right"):
            paths = []
            for kind in ("male", "female", "ferrule"):
                key = kind + "_" + side
                base = here / det.STANDARD_TEMPLATE_DIRS[key] / (key + ".png")
                paths.extend(det.discover_template_paths(str(base)))
            templates[side] = [png_size(Path(p)) for p in paths]
        source = "imported from terminal_counter_endpoint_first"
    except Exception as exc:                                  # noqa: BLE001
        scales = (0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.35, 1.50)
        templates = {}
        for side in ("left", "right"):
            paths = []
            for kind, d in (("male", "male_ter"), ("female", "female_ter"),
                            ("ferrule", "ferrule_ter")):
                stem = kind + "_" + side
                paths.extend(sorted(
                    p for p in (here / d).iterdir()
                    if p.is_file() and p.suffix.lower() == ".png"
                    and (p.stem == stem or p.stem.startswith(stem + "_"))
                ))
            templates[side] = [png_size(p) for p in paths]
        source = "filesystem scan (detector import failed: " + type(exc).__name__ + ")"
    return scales, templates, source


def patch_geometry(max_tw: int, max_th: int):
    """Exact integer patch envelope, mirroring build_endpoint_patch / patch_extract_core."""
    outward_w = (max_tw * 12) // 5
    inward_w = (max_tw * 7) // 5
    return outward_w + inward_w, (max_th * 16) // 5


def trials(templates, scales, side, roi):
    """Yield (tw, th, pw, ph) for every trial one candidate of this side runs.

    The ROI variants differ in WHAT IS SHARED, and the labels matter because
    three of them were previously misdescribed:

    roi="current"     the CPU's PER-BASE context patch, sized once per template
                      base at max(scales).  These run 399x224 to 619x300 --
                      this is NOT the 820x307 compiled MAX_PATCH envelope, and
                      NOT what today's PL driver sends (see below).
    roi="shared_int"  one 96x64-search ROI per template BASE, max-scale
                      geometry taken with int() truncation.  That truncation is
                      the CPU quirk Priority 3 calls out; it is preserved here
                      only to reproduce the historical figure.
    roi="shared"      the same per-BASE sharing with correct rounded geometry.
                      This is the honest per-base number.
    roi="side_common" one ROI per SIDE, sized to the largest template of that
                      side -- the reading under which a single ROI serves every
                      template, which is what "today's driver" would mean if the
                      driver could not re-size between bases.
    roi="pl_full_context"
                      TODAY'S PL DRIVER: one FULL-CONTEXT patch per SIDE, sized
                      from that side's largest max-scale template.  Left is
                      622x300, right 622x224.  This is what the PL actually
                      receives now, and it is the only row here describing
                      deployed behaviour rather than a proposal.
    roi="per_trial"   one 96x64-search ROI per TRIAL (needs the driver change).

    EXACTLY ONE OF THESE IS TODAY'S PL DRIVER: roi="pl_full_context".  Every
    other row is a proposal or a CPU-side policy.

    In particular roi="current" is the CPU's PER-BASE context policy, and its
    333.413 s/page is the cost of that policy under the silicon-anchored
    formula -- NOT a measurement of what the PL does today, and it must not be
    labelled "today's PL".  roi="side_common" is a 96x64-search ROI shared
    across a side, which is a different thing again from the full-context patch
    the driver actually sends.

    And note what even the pl_full_context row is: MODELLED MATCHER TIME for
    deployed driver geometry.  It is not measured, and it is not page time --
    it excludes refinement, DMA, extraction and all PS work.
    """
    side_tw = max(round(w * max(scales)) for w, _ in templates[side])
    side_th = max(round(h * max(scales)) for _, h in templates[side])
    for w, h in templates[side]:
        trunc_tw, trunc_th = int(w * max(scales)), int(h * max(scales))
        round_tw, round_th = round(w * max(scales)), round(h * max(scales))
        full_pw, full_ph = patch_geometry(trunc_tw, trunc_th)
        for sc in scales:
            tw, th = max(4, round(w * sc)), max(4, round(h * sc))
            if roi == "current":
                pw, ph = full_pw, full_ph
            elif roi == "shared_int":
                pw, ph = trunc_tw + 95, trunc_th + 63
            elif roi == "shared":
                pw, ph = round_tw + 95, round_th + 63
            elif roi == "side_common":
                pw, ph = side_tw + 95, side_th + 63
            elif roi == "pl_full_context":
                pw, ph = patch_geometry(side_tw, side_th)
            elif roi == "per_trial":
                pw, ph = tw + 95, th + 63
            else:
                raise ValueError(roi)
            if tw >= pw or th >= ph:
                continue
            yield tw, th, pw, ph


def page_cycles_expr(expr, roi="per_trial"):
    """Corpus s/page for an arbitrary per-trial cycle expression.

    Used to price a single sub-term of the cost model (e.g. the window
    statistics B0b deletes) so the decomposition can be asserted rather than
    asserted-by-assertion.
    """
    scales, templates, _ = discover_workload()
    total = 0
    for side, n in (("left", CANDIDATES_LEFT), ("right", CANDIDATES_RIGHT)):
        total += n * sum(expr(pw, ph, tw, th)
                         for tw, th, pw, ph in trials(templates, scales, side, roi))
    return total / PAGES / TARGET_CLOCK_HZ


def page_cycles(templates, scales, roi, variant, optimistic=False):
    """Total initial-trial matcher cycles for the whole 36-page corpus."""
    if optimistic:
        raise ValueError(
            "the optimistic B0b attribution was withdrawn: it double-counted "
            "the staging and remainder terms and produced the rejected 12.604")

    def fn(pw, ph, tw, th):
        return cycles(pw, ph, tw, th, variant=variant)
    total = 0
    for side, n in (("left", CANDIDATES_LEFT), ("right", CANDIDATES_RIGHT)):
        per = sum(fn(pw, ph, tw, th) for tw, th, pw, ph in trials(templates, scales, side, roi))
        total += n * per
    return total


# ---------------------------------------------------------------------------
# 4. THE FROZEN FIGURES
# ---------------------------------------------------------------------------
FROZEN = {
    "workload": {
        "pages": 36,
        "candidates_left": 371,
        "candidates_right": 367,
        "initial_trials": 20680,
        "refinement_calls": 808,
    },
    "board": {
        # Exact duplicate of BOARD_MEASUREMENTS, by (case, clock) -> seconds.
        # check() compares the two element-wise, so an edit to one and not the
        # other is a failure rather than a silent redefinition of the evidence.
        "measurements": {
            ("stress-max-envelope", 31.25e6): 13.362,
            ("stress-max-result",   31.25e6):  0.676,
            ("stress-max-envelope", 125e6):     3.342,
            ("stress-max-result",   125e6):     0.171,
        },
        # Priority 2 probe, 2026-08-17.  The clock is measured, not requested.
        "probe": {
            "measured_fclk0_mhz": 125.0,
            "routed_period_ns": 8.000,
            "routed_wns_ns": 0.063836,
            "vectors_passed": 9,
            "vectors_total": 9,
        },
        # Derived from the rows above, not transcribed.  Only the first is 4.
        "clock_linearity": {
            "stress-max-envelope": 3.998205,   # compute-dominated
            "stress-max-result":   3.953216,   # overhead-dominated
        },
    },
    "phase_s": {
        "max_cycles": 23476737,
        "speedup": 17.784,
    },
    "b0b_count_pass": {
        # Derived, not transcribed: I = pw*(th + 2*(rh-1)) == pw*(2*ph - th).
        "max_phase_s_trial_iterations": 69042,      # 311 * (96 + 2*63)
        "corpus_iterations": 657142901,             # all 20,680 initial trials
        "s_per_page_at_1_cyc": 0.146031755778,
        "s_per_page_at_3_cyc": 0.438095267333,
    },
    # B1, frozen EXACTLY.  Everything else in this file is frozen as a rounded
    # s/page against TOL = 5e-4, which is the right granularity for a figure
    # that is only ever quoted to three decimals -- but at 36 pages and 125 MHz
    # that tolerance spans +/- 2,250,000 cycles, which is more than the entire
    # measured dynamic-bound overhead this revision exists to record.  So the
    # aggregate is frozen as an INTEGER and asserted with ==.
    #
    # The aggregate is the sum over the whole 20,680-trial workload of the
    # measured tile term; it is a PROJECTION built from a measured term, not a
    # measured page time.
    "b1": {
        "aggregate_cycles": 118504314487,       # exact, asserted with ==
        "s_per_page": 26.334292108222222,       # aggregate / 36 / 125e6
        # What the model said before any B1 RTL existed, and by how much it was
        # wrong.  Frozen so the correction cannot be quietly rounded away: the
        # miss is against the EXACT old projection, not against the 26.240 that
        # was itself a rounded freeze.
        # EXACT, and integers first.  A projection quoted only as a float is
        # a projection whose miss can drift by whatever the rounding hid; see
        # the B2 block below, where a 20.175432 written to six places put the
        # frozen miss 1,687 cycles away from the truth.  Both endpoints and
        # the difference are frozen as CYCLE COUNTS, and the s/page figures are
        # derived from them rather than transcribed alongside them.
        "withdrawn_aggregate_cycles": 118078633847,
        "withdrawn_projection": 26.23969641044444,
        "projection_miss_cycles": 425680640,
        "projection_miss": 0.09459569777777778,
        # The cost at the compiled maximum template width, where B1 LOSES.
        "phase_s_max_cycles": 23482881,
        "phase_s_max_delta_cycles": 6144,
        # The B1 implementation and silicon results, frozen so
        # b1_evidence_crosscheck() can re-read them out of the retained
        # artifacts rather than agreeing with itself.  These are the numbers
        # the docstring's "cycle-validated workload projection" tier rests on.
        "routed_period_ns": 8.000,
        "routed_wns_ns": 0.134571,
        "board_fclk_mhz": 125.0000,
        "board_cases": 7,
    },
    # B2, frozen EXACTLY, for the same reason B1 is: TOL = 5e-4 on a per-page
    # average spans +/- 2,250,000 cycles, and the whole overhead this block
    # records is smaller than that.
    #
    # Measured by paired RTL co-simulation against the `b1` solution on
    # 2026-08-19 (sw/tme_b2_ab.py, 14/14 transactions exact, control intact on
    # all 14).  The PAGE FIGURE is still a projection: it sums the measured
    # term over 20,680 modelled trials and no page has been run anywhere.
    #
    # NOTE WHAT IS NOT HERE.  B1's block carries routed_wns_ns, board_fclk_mhz
    # and board_cases.  B2 has NO board session, so those fields do not exist
    # rather than holding a borrowed number.
    "b2": {
        "aggregate_cycles": 91823241527,        # exact, asserted with ==
        "s_per_page": 20.405164783777778,       # aggregate / 36 / 125e6
        # What the model said before any B2 RTL existed, and by how much it
        # was wrong.  Optimistic again, and by more than B1's miss -- 0.2297
        # s/page against 0.0946.
        #
        # FROZEN AS INTEGERS, AND THIS ONE HAD TO BE CORRECTED.  The withdrawn
        # projection used to sit here as the rounded 20.175432, and the miss
        # beside it was that float subtracted from the measured s/page --
        # 0.22973278377777717, which is 1,687 CYCLES away from the real
        # difference.  Rounding a page average to six places is a 2.25-million-
        # cycle tolerance, so a withdrawn number quoted that way cannot pin its
        # own miss.  The withdrawn AGGREGATE is what the pre-B2 model actually
        # summed over the same 20,680 trials, and it is recomputable: load
        # logs/b2_20260819/tme_cycle_model.py.pre_b2 and call page_cycles on
        # the workload this file discovers.  That snapshot reproduces THIS
        # file's `cur` and `B1` aggregates exactly, which is what says the two
        # differ in the B2 term and nothing else.
        "withdrawn_aggregate_cycles": 90789445687,
        "withdrawn_projection": 20.17543237488889,
        "projection_miss_cycles": 1033795840,
        "projection_miss": 0.2297324088888889,
        # TWO candidate terms, and the measurement matched NEITHER
        # (tme_b2_ab.py --predict).  The pre-RTL projection was T*(tw+41)+(tw-1)
        # -- committed 2026-08-17 in e762cbf, before the B2 source existed;
        # B1's measured term minus the naive reuse saving would have been
        # T*(tw+42)+tw.  The RTL is T*(tw+44)+(tw-2).
        #
        # READ THE DECOMPOSITION, NOT JUST THE SHORTFALL.  Against the pre-RTL
        # projection -- the analogue of the projection B1 withdrew -- the miss
        # is
        #
        #     3T - 1 = (T + 1) + 2*(T - 1)   per (output row, template row)
        #
        # exact on 14/14.  The (T + 1) is B1's own correction and it RECURS:
        # at T = 1 the second term vanishes and the entire miss IS B1's (T+1),
        # on all five single-tile transactions.  The 2*(T - 1) below is the
        # ADDITIONAL miss, measured against control-naive, a baseline that
        # already carries B1's term.  "B1's overhead did not recur" is FALSE
        # and was written here once; tme_b2_ab.py's check 4 now fails if the
        # decomposition ever stops holding.
        "shortfall_per_tile": 2,
        "shortfall_per_call": -2,
        # Unlike B1, B2 WINS at the compiled maximum template width.  The
        # saving over B1 is (T-1)*(tw-3) per (output row, template row), which
        # is zero only at T = 1 and positive for every legal tw >= 4.
        "phase_s_max_cycles": 16939521,
        "phase_s_max_delta_cycles": -6543360,   # against B1, not against `cur`
        # Routed 2026-08-19 at the same 8.000 ns B1 was probed at.  IT CLOSES,
        # AND IT ALMOST DOES NOT: 0.011710 ns is 0.15% of the period, against
        # B1's 0.134571 ns (1.7%).  Read it as "this run closed", not as "B2
        # closes 8 ns" -- a re-run with a different seed is not obliged to.
        #
        # The BINDING PATH ALSO MOVED, and that is the part with consequences.
        # B1 bound on templ_buf -> t_row, outside correlation_core; B2 binds on
        # its own shift register feeding the MAC's DSP input.  So the next
        # change to correlation_core -- B0b -- INHERITS 0.012 ns of margin on a
        # path inside the block it edits, rather than B1's 0.135 ns on a path
        # somewhere else.
        #
        # SAY THAT PRECISELY.  B0b deletes the window statistics and hoists a
        # count pass; it does NOT modify the seg-shift -> DSP path itself, and
        # nothing here predicts that it will.  What it can do is perturb
        # placement and routing around a path with 12 ps to give.  That is a
        # reason to route B0b EARLY -- the margin is small enough that a
        # placement disturbance is a plausible way to lose it -- not a reason
        # to say B0b attacks the critical path.
        "routed_period_ns": 8.000,
        "routed_wns_ns": 0.011710,
        "routed_binding_path_in_core": True,
        # Post-route, against B1's 14,792 LUT / 18,483 FF / 115 BRAM / 34 DSP.
        # BRAM and DSP are unchanged; the shift register is pure fabric.
        "routed_luts": 20694,
        "routed_ffs": 24409,
    },
    "s_per_page_at_125mhz": {
        "pl_full_context": 631.930606,    # today's PL, side-common full context
        "shared_roi_int_quirk": 60.764,   # historical, int() truncation
        "shared_roi": 61.301,             # corrected per-base sharing
        "side_common_roi": 94.697,        # one ROI per side
        "per_trial_roi": 36.476,
        # Measured tile term (see cycles()).  This s/page entry is checked at
        # TOL like its neighbours; the BINDING freeze for B1 is the exact
        # integer cycle count in FROZEN["b1"] below, because a 5e-4 tolerance on
        # a per-page average hides 2.25 million cycles.
        "B1": 26.334,
        # Measured tile term, same status as B1's and same caveat: the BINDING
        # freeze is the exact integer aggregate in FROZEN["b2"] above.  20.175
        # was the projection this replaced; see that block.
        "B2": 20.405,
        # Endpoints, not a range: 17.652 used to sit inside [17.514, 17.806]
        # and pass, which let the discarded attribution survive.  Each endpoint
        # is now checked on its own.
        #
        # THESE MOVED WHEN B2 WAS MEASURED, and they had to.  B0b is DEFINED as
        # B2 minus the window statistics plus the count pass, so B2's projected
        # tile term was load-bearing for both endpoints.  The 17.513998 /
        # 17.806062 pair is WITHDRAWN -- not because the count-pass arithmetic
        # changed (it did not; FROZEN["b0b_count_pass"] is untouched) but
        # because its base did.  What is still projected here is the count
        # pass's II, exactly as before; what is no longer projected is
        # everything underneath it.
        "B0b_base": 17.597699,
        "B0b_at_1_cyc": 17.743730541333,
        "B0b_at_3_cyc": 18.035794052889,
    },
}
TOL = 5e-4


def evaluate():
    scales, templates, source = discover_workload()
    full = cycles(*BOARD_FULL_ENVELOPE)
    phase_s = cycles(*PHASE_S_GEOMETRY)

    def s_page(roi, variant, optimistic=False):
        return page_cycles(templates, scales, roi, variant, optimistic) / PAGES / TARGET_CLOCK_HZ

    return {
        "template_source": source,
        "scales": scales,
        "templates": templates,
        "full_envelope_cycles": full,
        "full_envelope_seconds_board": full / BOARD_CLOCK_HZ,
        "board_checks": [
            (name, geom, hz, meas, cycles(*geom) / hz, note)
            for name, geom, hz, meas, note in BOARD_MEASUREMENTS
        ],
        "clock_ratios": measured_clock_ratios(),
        "phase_s_cycles": phase_s,
        "phase_s_speedup": full / phase_s,
        "s_page": {
            "current_core": s_page("current", "cur"),
            "pl_full_context": s_page("pl_full_context", "cur"),
            "shared_roi_int_quirk": s_page("shared_int", "cur"),
            "shared_roi": s_page("shared", "cur"),
            "side_common_roi": s_page("side_common", "cur"),
            "per_trial_roi": s_page("per_trial", "cur"),
            "B1": s_page("per_trial", "B1"),
            "B2": s_page("per_trial", "B2"),
            "B0b_base": s_page("per_trial", "B0b_base"),
            "B0b_at_1_cyc": page_cycles_expr(
                lambda pw, ph, tw, th: cycles_b0b(pw, ph, tw, th, 1)),
            "B0b_at_3_cyc": page_cycles_expr(
                lambda pw, ph, tw, th: cycles_b0b(pw, ph, tw, th, 3)),
        },
    }


def measured_clock_ratios():
    """Wall-time ratio per case across the two clocks, DERIVED from the rows."""
    by_case = {}
    for name, _geom, hz, meas, _note in BOARD_MEASUREMENTS:
        by_case.setdefault(name, {})[hz] = meas
    out = {}
    for name, per_hz in by_case.items():
        if len(per_hz) == 2:
            (hz_lo, s_lo), (hz_hi, s_hi) = sorted(per_hz.items())
            out[name] = (s_lo / s_hi, hz_hi / hz_lo)
    return out


def b1_evidence_crosscheck():
    """Re-read the B1 routed report and board transcript, if they are present.

    Priority 2's evidence has been corroborated from source since it was
    frozen; B1's was not, even after the docstring started calling B1
    silicon-backed.  An evidence tier that no assertion touches is a claim, not
    a check.  This closes that for the two numbers that carry the tier: the
    routed verdict at 8.000 ns and the observed board clock.

    Returns (failures, checked_count).  Absent artifacts yield ([], 0) -- never
    a pass -- exactly like board_log_crosscheck().
    """
    root = Path(__file__).resolve().parents[2]
    if not (root / "logs").is_dir():          # clean checkout: sw/ is a sibling
        root = Path(__file__).resolve().parents[1]
    routed = root / "logs" / "b1_20260818" / "b1_post_route_wns.txt"
    run = root / "logs" / "b1_board_20260818" / "03_run.txt"
    if not (routed.exists() and run.exists()):
        return [], 0

    fail, checked = [], 0
    b1f = FROZEN["b1"]

    txt = routed.read_text(errors="replace")
    m = re.search(r"constrained period\s*:\s*([0-9.]+)\s*ns", txt)
    if not m:
        fail.append("B1 routed report: no constrained period line")
    else:
        checked += 1
        if abs(float(m.group(1)) - b1f["routed_period_ns"]) > 1e-9:
            fail.append("B1 routed period: report says {} ns, frozen {}".format(
                m.group(1), b1f["routed_period_ns"]))
    m = re.search(r"post-route WNS\s*:\s*([0-9.+-]+)\s*ns", txt)
    if not m:
        fail.append("B1 routed report: no post-route WNS line")
    else:
        checked += 1
        if abs(float(m.group(1)) - b1f["routed_wns_ns"]) > 5e-7:
            fail.append("B1 routed WNS: report says {} ns, frozen {}".format(
                m.group(1), b1f["routed_wns_ns"]))
    checked += 1
    if "all constraints met" not in txt:
        fail.append("B1 routed report does not say 'all constraints met'")

    txt = run.read_text(errors="replace")
    m = re.search(r"FCLK0 gate: PASS.*?([0-9]+\.[0-9]+) MHz", txt)
    if not m:
        fail.append("B1 board transcript: no passing FCLK0 gate line")
    else:
        checked += 1
        if abs(float(m.group(1)) - b1f["board_fclk_mhz"]) > 1e-4:
            fail.append("B1 board clock: transcript says {} MHz, frozen {}".format(
                m.group(1), b1f["board_fclk_mhz"]))
    m = re.search(r"(\d+)/(\d+) cases passed", txt)
    if not m:
        fail.append("B1 board transcript: no case tally")
    else:
        checked += 1
        if (int(m.group(1)), int(m.group(2))) != (b1f["board_cases"],
                                                  b1f["board_cases"]):
            fail.append("B1 board cases: transcript says {}/{}, frozen {}/{}".format(
                m.group(1), m.group(2), b1f["board_cases"], b1f["board_cases"]))
    checked += 1
    if "re-invocation check: PASS" not in txt:
        fail.append("B1 board transcript: no passing re-invocation check")
    return fail, checked


def b2_evidence_crosscheck():
    """Re-read the B2 routed report, if it is present.

    B2 has NO board transcript to read -- it was never run on silicon -- so
    this checks strictly less than b1_evidence_crosscheck does, and the
    asymmetry is the point rather than an omission.  What it can check is that
    the frozen routed numbers are the ones in the report.

    Returns (failures, checked_count).  An absent report yields ([], 0) --
    never a pass.
    """
    root = Path(__file__).resolve().parents[2]
    if not (root / "logs").is_dir():          # clean checkout: sw/ is a sibling
        root = Path(__file__).resolve().parents[1]
    routed = root / "logs" / "b2_20260819" / "b2_post_route_wns.txt"
    util = root / "logs" / "b2_20260819" / "b2_post_route_utilization.rpt"
    if not routed.exists():
        return [], 0

    fail, checked = [], 0
    b2f = FROZEN["b2"]
    txt = routed.read_text(errors="replace")

    m = re.search(r"constrained period\s*:\s*([0-9.]+)\s*ns", txt)
    if not m:
        fail.append("B2 routed report: no constrained period line")
    else:
        checked += 1
        if abs(float(m.group(1)) - b2f["routed_period_ns"]) > 1e-9:
            fail.append("B2 routed period: report says {} ns, frozen {}".format(
                m.group(1), b2f["routed_period_ns"]))
    m = re.search(r"post-route WNS\s*:\s*([0-9.+-]+)\s*ns", txt)
    if not m:
        fail.append("B2 routed report: no post-route WNS line")
    else:
        checked += 1
        if abs(float(m.group(1)) - b2f["routed_wns_ns"]) > 5e-7:
            fail.append("B2 routed WNS: report says {} ns, frozen {}".format(
                m.group(1), b2f["routed_wns_ns"]))
    checked += 1
    if "all constraints met" not in txt:
        fail.append("B2 routed report does not say 'all constraints met'")

    # The binding path moved INTO correlation_core.  That is a claim the report
    # can settle, and it is the one that governs what B0b inherits, so it is
    # checked rather than asserted in prose.
    checked += 1
    m = re.search(r"binding path.*?\n\s*from:\s*(\S+)", txt, re.S)
    if not m:
        fail.append("B2 routed report: no binding path")
    elif ("correlation_core" in m.group(1)) != b2f["routed_binding_path_in_core"]:
        fail.append("B2 binding path: report says {}, frozen "
                    "routed_binding_path_in_core={}".format(
                        m.group(1), b2f["routed_binding_path_in_core"]))

    if util.exists():
        u = util.read_text(errors="replace")
        for key, label in (("routed_luts", "Slice LUTs"),
                           ("routed_ffs", "Slice Registers")):
            m = re.search(r"\|\s*" + label + r"\s*\|\s*(\d+)\s*\|", u)
            if not m:
                fail.append("B2 utilisation report: no '{}' row".format(label))
            else:
                checked += 1
                if int(m.group(1)) != b2f[key]:
                    fail.append("B2 {}: report says {}, frozen {}".format(
                        label, m.group(1), b2f[key]))
    return fail, checked


def board_log_crosscheck():
    """Corroborate the frozen probe metadata against the retained transcripts.

    The literals in FROZEN are the freeze; this re-reads them from
    logs/board_125mhz_gate/ so a transcription error shows up as a mismatch
    rather than as agreement with itself.  Returns (failures, checked_count);
    an absent log directory yields ([], 0), never a pass.

    That directory IS committed as of 2026-08-19 -- it was not before, and this
    pass reported ABSENT from every clean checkout, re-deriving two of six
    values while looking like it had run.
    """
    root = TREE_ROOT / "logs" / "board_125mhz_gate"
    clock_log, vec_log = root / "01_load_and_clock.txt", root / "02_vectors_raw.txt"
    if not (clock_log.exists() and vec_log.exists()):
        return [], 0

    fail, checked = [], 0
    pr = FROZEN["board"]["probe"]

    text = clock_log.read_text(encoding="utf-8", errors="replace")
    mo = re.search(r"fclk0 AFTER\s*=\s*([0-9.]+)", text)
    checked += 1
    if not mo:
        fail.append("log: no 'fclk0 AFTER' line in " + clock_log.name)
    elif float(mo.group(1)) != pr["measured_fclk0_mhz"]:
        fail.append("log: fclk0 AFTER = {} but frozen says {}".format(
            mo.group(1), pr["measured_fclk0_mhz"]))

    text = vec_log.read_text(encoding="utf-8", errors="replace")
    mo = re.search(r"(\d+)/(\d+) cases passed", text)
    checked += 1
    if not mo:
        fail.append("log: no 'N/M cases passed' line in " + vec_log.name)
    elif (int(mo.group(1)), int(mo.group(2))) != (pr["vectors_passed"], pr["vectors_total"]):
        fail.append("log: {}/{} cases passed but frozen says {}/{}".format(
            mo.group(1), mo.group(2), pr["vectors_passed"], pr["vectors_total"]))

    # Every 125 MHz wall time must appear verbatim in the vector transcript.
    for name, _geom, hz, meas, _note in BOARD_MEASUREMENTS:
        if hz != 125e6:
            continue
        checked += 1
        needle = "{:.3f} s".format(meas)
        if not any(name in ln and needle in ln for ln in text.splitlines()):
            fail.append("log: {} {:.3f} s not found in {}".format(
                name, meas, vec_log.name))
    return fail, checked


def routed_report_crosscheck():
    """Corroborate the frozen routed timing against post_route_wns.txt."""
    # The committed copy first: it is the one a clone has, and it is the one
    # MANIFEST-style evidence can bind.  The Vivado build root is the fallback
    # because it lives outside the repository and only exists on the machine
    # that ran the implementation.
    rpt = TREE_ROOT / "logs" / "tme_125_timing" / "post_route_wns.txt"
    if not rpt.exists():
        rpt = Path("C:/Users/lychee/tc25/vivado_project/tme_standalone_125"
                   "/overlay_output/post_route_wns.txt")
    if not rpt.exists():
        return [], 0
    text = rpt.read_text(encoding="utf-8", errors="replace")
    fail, checked, pr = [], 0, FROZEN["board"]["probe"]
    for label, pat, want in (
            ("constrained period", r"constrained period\s*:\s*([0-9.]+) ns", pr["routed_period_ns"]),
            ("post-route WNS", r"post-route WNS\s*:\s*([0-9.]+) ns", pr["routed_wns_ns"])):
        checked += 1
        mo = re.search(pat, text)
        if not mo:
            fail.append("routed report: no '{}' line".format(label))
        elif abs(float(mo.group(1)) - want) > 1e-9:
            fail.append("routed report: {} = {} but frozen says {}".format(
                label, mo.group(1), want))
    return fail, checked


def check(results):
    fail = []

    if len(COSIM_TRANSACTIONS) != COSIM_TRANSACTIONS_IN_REPORT:
        fail.append("cosim: {} transactions modelled but the report holds {} -- "
                    "the docstring claims every one".format(
                        len(COSIM_TRANSACTIONS), COSIM_TRANSACTIONS_IN_REPORT))
    for geom, expect in COSIM_TRANSACTIONS:
        got = cycles(*geom)
        if got != expect:
            fail.append("cosim {}: model {} != reported {}".format(geom, got, expect))

    # (a) The SET of measurements is frozen: a dropped row is a failure, not a
    #     silently shorter loop.  (b) Each measured value is frozen EXACTLY --
    #     BOARD_TOLERANCE_S bounds model-vs-silicon agreement and is far too
    #     loose to catch 0.171 edited to 0.170.
    live = {(name, hz): meas for name, _g, hz, meas, _n in BOARD_MEASUREMENTS}
    frozen_rows = FROZEN["board"]["measurements"]
    if len(BOARD_MEASUREMENTS) != len(frozen_rows):
        fail.append("board: {} measurement rows != frozen {}".format(
            len(BOARD_MEASUREMENTS), len(frozen_rows)))
    for key in sorted(set(live) | set(frozen_rows), key=lambda k: (k[0], k[1])):
        if key not in live:
            fail.append("board: frozen row {} @ {:g}MHz is MISSING from "
                        "BOARD_MEASUREMENTS".format(key[0], key[1] / 1e6))
        elif key not in frozen_rows:
            fail.append("board: row {} @ {:g}MHz is not frozen".format(
                key[0], key[1] / 1e6))
        elif live[key] != frozen_rows[key]:
            fail.append("board: {} @ {:g}MHz measured {} != frozen {}".format(
                key[0], key[1] / 1e6, live[key], frozen_rows[key]))

    for name, geom, hz, meas, note in BOARD_MEASUREMENTS:
        model_s = cycles(*geom) / hz
        if abs(model_s - meas) > BOARD_TOLERANCE_S:
            fail.append("board {} {} @ {:g}MHz: model {:.4f}s vs measured {}s "
                        "(> {}s) [{}]".format(name, geom, hz / 1e6, model_s, meas,
                                              BOARD_TOLERANCE_S, note))

    # Linearity, DERIVED from the rows above rather than transcribed.  It is a
    # COMPUTE-DOMINATED claim: the envelope case must track the clock, and the
    # overhead-dominated 4x4 case must visibly NOT.  Asserting both directions
    # stops "wall time is linear in clock" from being restated unqualified.
    ratios = measured_clock_ratios()
    frozen_lin = FROZEN["board"]["clock_linearity"]
    if set(ratios) != set(frozen_lin):
        fail.append("clock linearity: cases {} != frozen {}".format(
            sorted(ratios), sorted(frozen_lin)))
    for name, (got, want) in sorted(ratios.items()):
        if abs(got - frozen_lin.get(name, -1)) > 5e-7:
            fail.append("clock linearity {}: {:.6f} != frozen {}".format(
                name, got, frozen_lin.get(name)))
        dev = abs(got - want) / want
        if name == "stress-max-envelope" and dev > 1e-3:
            fail.append("clock linearity {}: {:.6f} vs clock ratio {:g} is "
                        "{:.4f}% (> 0.1%) -- the compute-dominated case must "
                        "track the clock".format(name, got, want, dev * 100))
        if name == "stress-max-result" and dev <= 1e-3:
            fail.append("clock linearity {}: {:.6f} now tracks the clock to "
                        "{:.4f}%; the fixed-overhead caveat this file states is "
                        "no longer supported by the data".format(name, got, dev * 100))

    log_fail, log_n = board_log_crosscheck()
    fail.extend(log_fail)
    rpt_fail, rpt_n = routed_report_crosscheck()
    fail.extend(rpt_fail)
    b1_fail, b1_n = b1_evidence_crosscheck()
    fail.extend(b1_fail)
    b2_fail, b2_n = b2_evidence_crosscheck()
    fail.extend(b2_fail)
    results["crosschecks_run"] = log_n + rpt_n + b1_n + b2_n
    # Name the sources that were NOT available.  A smaller count is easy to
    # miss; "board transcripts: absent" is not.  Neither is a failure -- the
    # frozen literals still stand alone -- but a clone that checked two values
    # must not read as one that checked six.
    results["crosscheck_sources"] = {
        "P2 board transcripts (logs/board_125mhz_gate/)":
            "read" if log_n else "absent",
        "P2 routed report (post_route_wns.txt)": "read" if rpt_n else "absent",
        "B1 routed + board (logs/b1_*/)": "read" if b1_n else "absent",
        # B2 has a routed report and NO board transcript.  The label says so,
        # so a reader cannot mistake "read" here for the same thing as B1's.
        "B2 routed only, no board (logs/b2_20260819/)":
            "read" if b2_n else "absent",
    }

    scales, templates, _ = discover_workload()
    n = (CANDIDATES_LEFT * sum(1 for _ in trials(templates, scales, "left", "per_trial"))
         + CANDIDATES_RIGHT * sum(1 for _ in trials(templates, scales, "right", "per_trial")))
    if n != FROZEN["workload"]["initial_trials"]:
        fail.append("workload: {} initial trials != frozen {}".format(
            n, FROZEN["workload"]["initial_trials"]))

    if results["phase_s_cycles"] != FROZEN["phase_s"]["max_cycles"]:
        fail.append("phase S: {} cycles != frozen {}".format(
            results["phase_s_cycles"], FROZEN["phase_s"]["max_cycles"]))
    if abs(results["phase_s_speedup"] - FROZEN["phase_s"]["speedup"]) > 1e-3:
        fail.append("phase S speedup: {:.4f} != frozen {}".format(
            results["phase_s_speedup"], FROZEN["phase_s"]["speedup"]))

    # B1's binding freeze: the exact integer aggregate, and the exact
    # arithmetic that turns it into the quoted s/page.  A drift of ONE cycle
    # anywhere in the 20,680 trials fails here, where the TOL check below would
    # not notice two million.
    b1f = FROZEN["b1"]
    scales_b1, templates_b1, _ = discover_workload()
    agg = page_cycles(templates_b1, scales_b1, "per_trial", "B1")
    if agg != b1f["aggregate_cycles"]:
        fail.append("B1 aggregate: {:,} cycles != frozen {:,} (delta {:+,})".format(
            agg, b1f["aggregate_cycles"], agg - b1f["aggregate_cycles"]))
    if abs(agg / PAGES / TARGET_CLOCK_HZ - b1f["s_per_page"]) > 1e-12:
        fail.append("B1 s/page: {!r} != frozen {!r}".format(
            agg / PAGES / TARGET_CLOCK_HZ, b1f["s_per_page"]))
    # The withdrawn endpoint and the miss are checked as INTEGERS first.  A
    # float identity here passes on numbers that are two thousand cycles apart,
    # which is how the B2 block came to carry a miss derived from a rounded
    # projection; see FROZEN["b2"].
    if (b1f["aggregate_cycles"] - b1f["withdrawn_aggregate_cycles"]
            != b1f["projection_miss_cycles"]):
        fail.append("B1 projection miss: {:,} - {:,} != frozen {:,} cycles".format(
            b1f["aggregate_cycles"], b1f["withdrawn_aggregate_cycles"],
            b1f["projection_miss_cycles"]))
    for k, c in (("withdrawn_projection", "withdrawn_aggregate_cycles"),
                 ("projection_miss", "projection_miss_cycles")):
        if b1f[k] != b1f[c] / PAGES / TARGET_CLOCK_HZ:
            fail.append("B1 {}: frozen {!r} is not {} / 36 / 125e6 = {!r}".format(
                k, b1f[k], c, b1f[c] / PAGES / TARGET_CLOCK_HZ))
    if abs((b1f["s_per_page"] - b1f["withdrawn_projection"])
           - b1f["projection_miss"]) > 1e-12:
        fail.append("B1 projection miss: frozen {!r} is not measured minus "
                    "withdrawn".format(b1f["projection_miss"]))
    ps_b1 = cycles(*PHASE_S_GEOMETRY, "B1")
    if ps_b1 != b1f["phase_s_max_cycles"]:
        fail.append("B1 phase-s-max: {:,} != frozen {:,}".format(
            ps_b1, b1f["phase_s_max_cycles"]))
    if ps_b1 - FROZEN["phase_s"]["max_cycles"] != b1f["phase_s_max_delta_cycles"]:
        fail.append("B1 phase-s-max delta: {:+,} != frozen {:+,} -- B1 is a NET "
                    "LOSS at tw = MAX_TEMPL_W and that must stay visible".format(
                        ps_b1 - FROZEN["phase_s"]["max_cycles"],
                        b1f["phase_s_max_delta_cycles"]))

    # B2's binding freeze, on the same footing as B1's and for the same
    # reason.  The phase-s delta is against B1, because B2 is B1 plus the reuse
    # and that is the difference the co-simulation measured.
    b2f = FROZEN["b2"]
    agg2 = page_cycles(templates_b1, scales_b1, "per_trial", "B2")
    if agg2 != b2f["aggregate_cycles"]:
        fail.append("B2 aggregate: {:,} cycles != frozen {:,} (delta {:+,})".format(
            agg2, b2f["aggregate_cycles"], agg2 - b2f["aggregate_cycles"]))
    if abs(agg2 / PAGES / TARGET_CLOCK_HZ - b2f["s_per_page"]) > 1e-12:
        fail.append("B2 s/page: {!r} != frozen {!r}".format(
            agg2 / PAGES / TARGET_CLOCK_HZ, b2f["s_per_page"]))
    # Recompute the withdrawn endpoint with the pinned pre-B2 implementation,
    # rather than proving only that three literals in this file agree with one
    # another.  Use the workload already discovered above: the snapshot lives
    # under logs/, so its own detector-relative discovery cannot find the
    # sibling templates in a split development tree.  Its page_cycles() still
    # supplies every cycle expression and aggregation rule under test.
    pre_b2_path = (Path(__file__).resolve().parents[1] / "logs" /
                   "b2_20260819" / "tme_cycle_model.py.pre_b2")
    try:
        import runpy
        pre_b2 = runpy.run_path(str(pre_b2_path))
        pre_b2_agg = pre_b2["page_cycles"](
            templates_b1, scales_b1, "per_trial", "B2")
    except Exception as exc:                              # noqa: BLE001
        fail.append("B2 withdrawn aggregate: could not execute pinned pre-B2 "
                    "model {} ({})".format(pre_b2_path, exc))
    else:
        if pre_b2_agg != b2f["withdrawn_aggregate_cycles"]:
            fail.append(
                "B2 withdrawn aggregate: pinned pre-B2 model recomputed "
                "{:,}, frozen {:,} (delta {:+,})".format(
                    pre_b2_agg, b2f["withdrawn_aggregate_cycles"],
                    pre_b2_agg - b2f["withdrawn_aggregate_cycles"]))
    # THE WITHDRAWN PROJECTION, at cycle resolution.  This is the check that
    # was missing when the withdrawn figure was frozen as the rounded 20.175432
    # and the miss was computed from it: the float identity below passed on a
    # miss that was 1,687 cycles wrong.  Integers first, floats derived.
    if (b2f["aggregate_cycles"] - b2f["withdrawn_aggregate_cycles"]
            != b2f["projection_miss_cycles"]):
        fail.append("B2 projection miss: {:,} - {:,} != frozen {:,} cycles".format(
            b2f["aggregate_cycles"], b2f["withdrawn_aggregate_cycles"],
            b2f["projection_miss_cycles"]))
    for k, c in (("withdrawn_projection", "withdrawn_aggregate_cycles"),
                 ("projection_miss", "projection_miss_cycles")):
        if b2f[k] != b2f[c] / PAGES / TARGET_CLOCK_HZ:
            fail.append("B2 {}: frozen {!r} is not {} / 36 / 125e6 = {!r}".format(
                k, b2f[k], c, b2f[c] / PAGES / TARGET_CLOCK_HZ))
    if abs((b2f["s_per_page"] - b2f["withdrawn_projection"])
           - b2f["projection_miss"]) > 1e-12:
        fail.append("B2 projection miss: frozen {!r} is not measured minus "
                    "withdrawn".format(b2f["projection_miss"]))
    ps_b2 = cycles(*PHASE_S_GEOMETRY, "B2")
    if ps_b2 != b2f["phase_s_max_cycles"]:
        fail.append("B2 phase-s-max: {:,} != frozen {:,}".format(
            ps_b2, b2f["phase_s_max_cycles"]))
    if ps_b2 - b1f["phase_s_max_cycles"] != b2f["phase_s_max_delta_cycles"]:
        fail.append("B2 phase-s-max delta vs B1: {:+,} != frozen {:+,} -- B2 "
                    "WINS at tw = MAX_TEMPL_W where B1 lost, and that must "
                    "stay visible".format(
                        ps_b2 - b1f["phase_s_max_cycles"],
                        b2f["phase_s_max_delta_cycles"]))
    # The measured tile term itself, independent of any workload sum: the
    # closed form must be what cycles() actually computes, at every geometry
    # the co-simulation covered plus the Phase-S maximum.  Without this the
    # term lives only in a comment and in the file that reads the reports.
    for pw, ph, tw, th in ((12, 10, 4, 4), (232, 13, 216, 8), (130, 21, 100, 12),
                           (114, 27, 20, 12), (88, 39, 24, 16), (311, 159, 216, 96)):
        rw, rh = pw - tw + 1, ph - th + 1
        T = math.ceil(rw / 16)
        want = (pw * ph + 24 + rh * (5 * rw + 99)
                + rh * th * (T * (tw + 44) + tw - 2 + 3 * tw + 3 * rw + 33))
        if cycles(pw, ph, tw, th, "B2") != want:
            fail.append("B2 tile term at {}: cycles()={:,} != T*(tw+44)+tw-2 "
                        "closed form {:,}".format((pw, ph, tw, th),
                                                  cycles(pw, ph, tw, th, "B2"),
                                                  want))
        # And the saving over B1, which is what makes B2 never a regression.
        if (cycles(pw, ph, tw, th, "B1") - cycles(pw, ph, tw, th, "B2")
                != rh * th * (T - 1) * (tw - 3)):
            fail.append("B2 saving over B1 at {} is not rh*th*(T-1)*(tw-3)"
                        .format((pw, ph, tw, th)))

    for key in ("pl_full_context", "shared_roi_int_quirk", "shared_roi",
                "side_common_roi", "per_trial_roi", "B1", "B2"):
        got, want = results["s_page"][key], FROZEN["s_per_page_at_125mhz"][key]
        if abs(got - want) > TOL:
            fail.append("{}: {:.4f} s/page != frozen {}".format(key, got, want))

    # B0b: assert the derived base and BOTH endpoints individually.  A range
    # check is what previously let a wrong implementation pass.  The endpoints
    # are now derived end-to-end, so the tolerance is tight.
    for key, tol in (("B0b_base", 2e-6), ("B0b_at_1_cyc", 1e-9), ("B0b_at_3_cyc", 1e-9)):
        got, want = results["s_page"][key], FROZEN["s_per_page_at_125mhz"][key]
        if abs(got - want) > tol:
            fail.append("{}: {:.12f} s/page != frozen {}".format(key, got, want))

    # The four per-row sub-terms must account for the whole fitted term, with
    # nothing left over.  "Unattributed remainder" was a real gap and is closed.
    for tw, rw in ((216, 96), (4, 1), (109, 512), (16, 49)):
        parts = {k: f(tw, rw) for k, f in PER_ROW_TERMS.items()}
        if sum(parts.values()) != 3 * tw + 3 * rw + 33:
            fail.append("per-row attribution at tw={} rw={}: parts sum to {} != "
                        "3*tw+3*rw+33 = {}".format(
                            tw, rw, sum(parts.values()), 3 * tw + 3 * rw + 33))
        survives = sum(v for k, v in parts.items() if k != "window_statistics")
        if survives != 2 * tw + 2 * rw + 12:
            fail.append("post-B0b survivor at tw={} rw={}: {} != 2*tw+2*rw+12 = "
                        "{}".format(tw, rw, survives, 2 * tw + 2 * rw + 12))

    # The count pass itself: both algebraic forms, the maximum Phase-S trial,
    # and the corpus total.  A change to the iteration shape must show up here
    # rather than only as a shifted endpoint.
    cp = FROZEN["b0b_count_pass"]
    for pw, ph, tw, th in ((311, 159, 216, 96), (820, 307, 216, 96), (12, 10, 4, 4)):
        a = b0b_count_pass_iterations(pw, ph, tw, th)
        b = pw * (2 * ph - th)
        if a != b:
            fail.append("count pass {}: pw*(th+2*(rh-1))={} != pw*(2*ph-th)={}".format(
                (pw, ph, tw, th), a, b))
    got = b0b_count_pass_iterations(*PHASE_S_GEOMETRY)
    if got != cp["max_phase_s_trial_iterations"]:
        fail.append("count pass, max Phase-S trial: {} != frozen {}".format(
            got, cp["max_phase_s_trial_iterations"]))
    total_iters = round(page_cycles_expr(b0b_count_pass_iterations)
                        * PAGES * TARGET_CLOCK_HZ)
    if total_iters != cp["corpus_iterations"]:
        fail.append("count pass, corpus: {} iterations != frozen {}".format(
            total_iters, cp["corpus_iterations"]))
    for n, key in ((1, "s_per_page_at_1_cyc"), (3, "s_per_page_at_3_cyc")):
        got = page_cycles_expr(
            lambda pw, ph, tw, th, n=n: n * b0b_count_pass_iterations(pw, ph, tw, th))
        if abs(got - cp[key]) > 1e-9:
            fail.append("count pass @ {} cyc: {:.12f} s/page != frozen {}".format(
                n, got, cp[key]))

    # The decomposition itself: B2 - window statistics must equal the B0b base.
    stats = page_cycles_expr(lambda pw, ph, tw, th: (
        (ph - th + 1) * th * (tw + (pw - tw + 1) + 21)
        if pw - tw + 1 >= 1 and ph - th + 1 >= 1 else 0))
    lhs = results["s_page"]["B2"] - stats
    if abs(lhs - results["s_page"]["B0b_base"]) > 1e-9:
        fail.append("B0b decomposition: B2 - stats = {:.6f} != B0b base {:.6f}".format(
            lhs, results["s_page"]["B0b_base"]))
    results["b0b_stats_removed"] = stats

    return fail


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--assert", dest="do_assert", action="store_true",
                    help="re-prove the frozen figures; exit 1 on any drift")
    ap.add_argument("--json", metavar="PATH", help="write the freeze as JSON")
    args = ap.parse_args()

    r = evaluate()

    print("TEMPLATE-MATCHING ENGINE - FROZEN EVIDENCE")
    print("=" * 78)
    print("templates      " + r["template_source"])
    for side in ("left", "right"):
        dims = " ".join("{}x{}".format(w, h) for w, h in r["templates"][side])
        print("  {:<5}        {} templates: {}".format(side, len(r["templates"][side]), dims))
    print("scales         {}: {}".format(
        len(r["scales"]), ", ".join("{:.2f}".format(s) for s in r["scales"])))
    print("workload       {} left + {} right candidates over {} pages".format(
        CANDIDATES_LEFT, CANDIDATES_RIGHT, PAGES))
    print("               {} initial trials, {} refinement calls".format(
        FROZEN["workload"]["initial_trials"], REFINEMENT_CALLS))
    print()
    print("VALIDATION (silicon-backed)")
    print("-" * 78)
    print("  cosim              {} transactions reproduced exactly".format(len(COSIM_TRANSACTIONS)))
    print("  board wall time    unchanged standalone matcher, {} runs at {} clocks:".format(
        len(r["board_checks"]), len({hz for _, _, hz, _, _, _ in r["board_checks"]})))
    for name, geom, hz, meas, model_s, note in r["board_checks"]:
        pw, ph, tw, th = geom
        print("    {:<20} {:>3}x{:<3}/{:>3}x{:<2} @ {:>6.2f} MHz  model {:8.4f}s  meas {:6.3f}s"
              "  {:+.3f} ms {:+.4f}%".format(
                  name, pw, ph, tw, th, hz / 1e6, model_s, meas,
                  (meas - model_s) * 1e3, (meas - model_s) / model_s * 100))
    print("  clock linearity    derived from the rows above; only the "
          "compute-dominated case is 4x:")
    for name, (got, want) in sorted(r["clock_ratios"].items()):
        verdict = "tracks the clock" if abs(got - want) / want <= 1e-3 else "FIXED OVERHEAD visible"
        print("    {:<20} ratio {:.6f}  vs {:g}  ->  {:+.4f}%   {}".format(
            name, got, want, (got - want) / want * 100, verdict))
    print("  full envelope      {:,} cycles".format(r["full_envelope_cycles"]))
    print("  Phase S 311x159    {:,} cycles  = {:.3f}x".format(
        r["phase_s_cycles"], r["phase_s_speedup"]))
    print()
    print("PROJECTION - initial trials only, s/page @ {:g} MHz".format(TARGET_CLOCK_HZ / 1e6))
    print("-" * 78)
    print("  The CLOCK is board-demonstrated (above), and so is B1's ARCHITECTURE.")
    print("  What is NOT demonstrated is any PAGE, for any variant:")
    print("  Phase S is a driver change with no RTL; B0b is unimplemented RTL.")
    print("  B2 IS implemented: cycle term cosim-measured against B1, 14/14 exact.")
    print("  It has NO BOARD SESSION -- its clock is borrowed, not observed.")
    print("  B1 IS implemented: cycle term cosim-measured, routed at 8.000 ns, and RUN")
    print("  ON THE BOARD at its OWN observed 125.0000 MHz -- it does not borrow the")
    print("  unmodified core's clock.  Every s/page below is still a projection.")
    labels = [
        ("pl_full_context", "TODAY'S PL: side-common full context", "deployed; 622x300 / 622x224"),
        ("current_core", "CPU per-base context patch policy", "silicon-anchored formula"),
        ("shared_roi_int_quirk", "Phase S, per-base ROI, int() quirk", "historical figure only"),
        ("shared_roi", "Phase S, per-base ROI, rounded", "no RTL; corrected"),
        ("side_common_roi", "Phase S, one ROI per side", "no RTL; side-common reading"),
        ("per_trial_roi", "Phase S, per-trial ROI", "no RTL; needs driver change"),
        ("B1", "  + B1 runtime segment width", "cosim-validated TERM; page projected"),
        ("B2", "  + B2 overlap reuse", "cosim-validated TERM; NO board run"),
        ("B0b_base", "  + B0b, stats term deleted", "deletion only, no count pass"),
        ("B0b_at_1_cyc", "  + B0b count pass @ II=1", "iterations derived; II projected"),
        ("B0b_at_3_cyc", "  + B0b count pass @ II=3", "iterations derived; II projected"),
    ]
    for key, name, note in labels:
        v = r["s_page"][key]
        print("  {:<38} {:8.3f}   {:8.1f} @31.25MHz   {}".format(
            name, v, v * (TARGET_CLOCK_HZ / BOARD_CLOCK_HZ), note))
    print()
    print("  Excludes refinement, DMA, extraction and PS work.")
    print("  10 s/page is not reached by any modelled matcher change at the current trial count.")
    print("  Neither the combined image nor an end-to-end page has been demonstrated at 125 MHz.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "frozen": FROZEN,
            "recomputed": {
                "full_envelope_cycles": r["full_envelope_cycles"],
                "phase_s_cycles": r["phase_s_cycles"],
                "phase_s_speedup": r["phase_s_speedup"],
                "s_page": r["s_page"],
            },
        }, indent=2), encoding="utf-8")
        print("\nwrote " + args.json)

    if args.do_assert:
        fail = check(r)
        print()
        if fail:
            print("FROZEN EVIDENCE DRIFTED")
            for f in fail:
                print("  FAIL  " + f)
            return 1
        n = r.get("crosschecks_run", 0)
        print("frozen evidence re-proved: cosim x{}, {} board runs at {} clocks, "
              "clock linearity, workload, Phase S, ROI variants, "
              "B1 EXACT aggregate + phase-s-max delta, "
              "B2 EXACT aggregate + routed WNS, "
              "B0b decomposition + both endpoints".format(
                  len(COSIM_TRANSACTIONS), len(BOARD_MEASUREMENTS),
                  len({hz for _, _, hz, _, _ in BOARD_MEASUREMENTS})))
        srcs = r.get("crosscheck_sources", {})
        print("source corroboration: {} value(s) re-read from source".format(n))
        for name, state in sorted(srcs.items()):
            print("  {:<46} {}".format(name, state.upper()))
        if any(v == "absent" for v in srcs.values()):
            print("  Absent sources were NOT checked.  The frozen literals still")
            print("  stand on their own, but this run did not re-derive them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
