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
term is therefore MEASURED.  It was then RUN ON THE BOARD on 2026-08-20
(logs/b2_board_20260819/): phase_s 7/7 and hw 9/9 at a gated 125.0000 MHz,
exercising tile counts {1, 3, 4, 6, 38, 52} rather than only the T = 6 the
cosim and B1's session covered.

B0b IS implemented as of 2026-08-20 (b0b_sources/tme_top.b0b.cpp) and BOTH
of its terms are measured -- the hoisted pass and the removal it pays for,
separately, because a third `shadow` solution was built to separate them.
It has NOT been routed and has NOT run on silicon.

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
        pinned twelve-case suite.  It has RUN ON SILICON (2026-08-20,
        logs/b2_board_20260819/) at its own observed 125.0000 MHz: phase_s 7/7
        and hw 9/9, so unlike B1's phase_s-only session the tile count is
        exercised to T = 52 rather than only T = 6.  Only the summation over
        the workload is projected.
  B0b   RTL EXISTS (hls/template_match/b0b_sources/tme_top.b0b.cpp).  BOTH
        its terms are measured -- paired RTL co-simulation of three solutions,
        14/14 transactions exact on each of the two differences -- and its
        correctness was established before the measurement by a `shadow` build
        that carried both computations and compared them at 2,911,495 result
        positions over 100 invocations in five C-simulation suites, with a
        mutant gate proving that comparison can fail.  ROUTED, AND IT DOES
        NOT CLOSE -- WNS -0.051470 ns at 8.000 ns, binding on a `seg`
        register feeding the MAC's DSP input inside correlation_core, a file
        B0b does not edit.  NOT on silicon.  The CYCLE term is unaffected by
        that (a cosim latency does not depend on the clock); the CONVERSION
        to seconds is.  Its old [1, 3] II bracket is WITHDRAWN and DID NOT
        CONTAIN the answer; see the B0b section below.

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
  cycle-validated workload projection, silicon-corroborated
                  B2 20.405 s/page.  Read every word, as for B1.  The RTL
                  EXISTS, its CYCLE TERM is measured (paired RTL co-simulation
                  against `b1`, 14/14 transactions exact) and it is now
                  silicon-corroborated (board session 2026-08-20,
                  logs/b2_board_20260819/): FCLK0 gated at 125.0000 MHz, the
                  routed 8.000 ns image, phase_s 7/7 and hw 9/9 -- score within
                  +/-0.005 and exact location, per the tolerance above -- with
                  a verified re-invocation after each suite's largest case.
                  The 231-element shift register that routed with only
                  0.011710 ns of slack does run at 125 MHz on this part.

                  B2'S SILICON COVERAGE IS BROADER THAN B1'S, and that is the
                  part worth quoting.  B1's session was phase_s only, so every
                  case had T = 6.  B2 ran the hw suite too, which reaches
                  T = 38 (stress-max-envelope) and T = 52 (stress-max-result),
                  the compiled maximum, and moves the full 251,740 B single
                  transfer.  Since B2 is an INDEXING change whose behaviour is
                  "what tile t inherits from tile t-1", tile count is the axis
                  it was most likely to be wrong on, and that axis is now
                  exercised in silicon rather than only in C simulation.

                  The PAGE FIGURE is still a projection, summing that term over
                  20,680 modelled trials.  NO PAGE HAS BEEN RUN, on any
                  hardware, at any clock.  "20.405 s/page" is not a measured
                  page time and must never be written as one.
  cosim-measured term, no silicon
                  B0b 17.726036 s/page.  ONE measured NET term, from THREE
                  solutions built 2026-08-20: a `b2ctl` control byte-identical
                  to B2's build inputs, a `shadow` that adds the hoisted pass
                  without removing anything, and `b0b` itself.  b0b - b2ctl is
                  the frozen law and it is clean: csynth puts every shared loop
                  in those two at the same schedule.  The control reproduced
                  B2's published term on all 14 transactions in the same
                  comparison.

                  THE TWO HALVES ARE NOT SEPARATELY OWNED, AND THAT CLAIM WAS
                  WITHDRAWN ON 2026-08-20.  The shadow carries a comparison
                  inside norm_cols and it reschedules that loop by +2 cycles a
                  call (97 ~ 3361 -> 99 ~ 3363; the module wrapper agrees at
                  99 ~ 3363 -> 101 ~ 3365).  norm_cols runs once per output
                  row, so shadow - b2ctl overstates the pass by 2*rh and
                  b0b - shadow understates the removal by the same 2*rh.  The
                  two cancel in the net, which is why the frozen number is
                  untouched.  What survives as a component claim is the
                  per-(output row, template row) coefficient tw + rw + 24 --
                  its regressor is rh*th and the nuisance is proportional to
                  rh, so no amount of comparator moves it.  What does NOT
                  survive is the pass's S*(pw + 30) + 5 and the removal's
                  3 per output row.  See the B0b section below.

                  THIS TIER IS NOT THE SAME AS B1'S AND B2'S.  Those two closed
                  8.000 ns and ran on silicon.  B0b's routed run VIOLATED the
                  constraint (WNS -0.051470 ns, TNS -0.051470 ns), and it has
                  not run on silicon.  Its cycle claim is as strong as theirs;
                  its clock claim is a NEGATIVE result.  Read that both ways:
                  17.726036 s/page is a conversion at 125 MHz and this core has
                  no closing build at 125 MHz -- and equally, ONE run at default
                  effort with no directive or seed sweep does not establish that
                  B0b CANNOT close 8 ns.

                  THE OLD ENDPOINTS DID NOT BRACKET IT.  17.743731 (II=1) and
                  18.035794 (II=3) are WITHDRAWN, and the measurement is BELOW
                  BOTH.  Two projected inputs were wrong in opposite
                  directions: the hoisted pass costs 25% more than the II=1
                  endpoint assumed -- csynth confirms the II really is 1, so
                  the whole miss is per-scan overhead that was never modelled
                  -- while the REMOVAL is worth more than the four-way split
                  attributed to it, and that dominates.  So the third
                  consecutive optimistic projection was rescued by a second,
                  larger error pointing the other way.  Read that as a reason
                  to distrust unmeasured sub-terms, not as a reason to trust
                  the endpoints.
  unproved        the combined image at 125 MHz, and end-to-end page latency.
                  ("the modified core at 125 MHz" left this line on 2026-08-19:
                  the B1 image routed at 8.000 ns and ran 7/7 on the board with
                  FCLK0 gated at 125.0000 MHz.)

125 MHz being board-demonstrated raises the CONVERSION RATE from cycles to
seconds out of assumption and into measurement.  It does NOT promote B1 / B2 /
B0b from projections to results.

B0b: MEASURED, AND WHAT THE MEASUREMENT OVERTURNED
--------------------------------------------------
Implemented and measured 2026-08-20.  Evidence: logs/b0b_20260820/, adjudicated
by sw/tme_b0b_ab.py, with correctness from a shadow build and its mutant gate.

    B0b = B2 - [rh*th*(tw + rw + 24) + 3*rh] + [S*(pw + 30) + 5]
                                               S = th + 2*(rh - 1)

    B2                     20.405164783778   measured term, summed
    - D2, b0b - shadow      2.848889358222   the removal PLUS a comparator
    + D1, shadow - b2ctl    0.169760466889   the pass PLUS the same comparator
    -> B0b                 17.726035892444   aggregate 79,767,161,516 cycles

    the comparator, both lines  0.000588231111   = 2*rh over the corpus

THE TWO CORPUS COMPONENTS WERE WRONG UNTIL 2026-08-20, AND THE NET HID IT.
They were published as 3.088545 and 0.409416.  Both are 0.239655641778 s/page
too large -- the same offset, because both had been computed as a difference
against an intermediate that was not this file's B0b_base.  A shared offset
cancels in B2 - removed + added, so the net total stayed exactly right and
nothing pointed at the components.  They are now DERIVED by check() from
page_cycles_expr and asserted against FROZEN["b0b"], so prose and arithmetic
cannot drift apart again.  Two wrong numbers whose errors cancel is the failure
mode a correct total is worst at revealing.

THREE SOLUTIONS, BECAUSE B0b IS TWO CHANGES.  Adding a pass and deleting the
loops it replaces would have been one indivisible difference against a single
control.  A `shadow` solution -- the pass computed ALONGSIDE the loops, nothing
removed -- splits them.

THE SPLIT IS NOT CLEAN, AND THE CLAIM THAT IT WAS IS WITHDRAWN.  This file used
to say csynth confirmed it, on the grounds that every shared pipelined leaf
loop had the same II and the same ITERATION latency in all three.  Both are
true and neither was the right question: a pipelined loop's own latency is not
determined by those two columns, and norm_cols -- where the shadow's comparison
lives -- moves from 97 ~ 3361 to 99 ~ 3363 with II and iteration latency
unchanged.  sw/tme_b0b_synth.py compared only the two columns that could not
move, so it reported "no loop moved" for a difference it could not see.  It now
reads all four and holds them against a recorded inventory.

WHAT THE +2 COSTS, AND WHAT IT DOES NOT.  norm_cols runs once per OUTPUT ROW,
so the shadow carries +2*rh that belongs to the observation and not to the
design:

    D1 = shadow - b2ctl = the pass    + comparator
    D2 = b0b    - shadow = -(removal) - comparator
    D  = b0b    - b2ctl  = the pass   - removal        <- comparator-free

b2ctl and b0b have identical norm_cols schedules, so D carries none of it.  The
NET LAW IS UNAFFECTED.  Only its split is, and equally in both directions.

WHY NO COMPARATOR-FREE CONTROL WAS BUILT.  Because there cannot be one.  A
shadow solution exists to hold BOTH copies of the statistics live at once; a
copy that nothing reads is dead code and Vitis deletes it, at which point the
solution is either b2ctl or b0b and measures nothing.  So some consumer must
read both copies, and the cheapest such consumer is exactly the two array reads
and the compare that are already there.  Moving them into a loop of their own
trades a +2 inside norm_cols for a whole new loop region; dropping the compare
and selecting between the copies instead keeps both reads and so keeps the
cost.  D1 and D2 are therefore unidentifiable BY THIS METHOD, in principle
rather than by oversight.

WHAT SURVIVES ANYWAY, AND IT IS THE PART THAT MATTERED.  The nuisance is
proportional to rh.  W's regressor is rh*th, and th varies across the suite, so
no rh-proportional term can move it:

    tw + rw + 24 per (output row, template row)   IDENTIFIED
    3 per output row                              = removal's own share + 2
    S*(pw + 30) + 5                               = the pass + 2*rh

The pre-registration is refuted either way -- it predicted tw + rw + 21 and
nothing per output row, and 24 is what the data give with or without the
comparator.  Under the csynth attribution (comparator = 2 per output row
exactly, nothing per invocation) the decontaminated pair is

    pass    = S*(pw + 29) + th + 3
    removal = rh*th*(tw + rw + 24) + rh

which reproduces the identical net.  IT IS NOT FROZEN.  It rests on a csynth
schedule rather than on a co-simulation, and it cannot exclude a further
per-invocation constant, which trades against the +3 with no way to tell them
apart.  Quote the net; quote W; quote neither half as a component cost.

TWO PROJECTIONS DIED HERE, AND ONLY ONE OF THEM WAS ABOUT B0b.

1.  THE II WAS RIGHT AND THE ENDPOINTS WERE STILL WRONG.  csynth puts the
    hoisted pass's two loops at II=1 with iteration latencies 7 and 14 --
    IDENTICAL to the isq_init and isq_slide they replace, which is what the
    source set out to preserve.  But the endpoints modelled the pass as N*I and
    nothing else, and the measurement is S*(pw + 30) + 5: 30 cycles per scan of
    pipeline flush and call overhead, 5 per invocation.  Over the cosim suite
    that is +25% on the pass.  The II was never the risky part; the constant
    that was not modelled at all was.

2.  THE WINDOW-STATISTICS ATTRIBUTION WAS WRONG, AND THAT IS THE BIGGER
    FINDING.  The model split the fitted per-(output row, template row) cost
    3*tw + 3*rw + 33 four ways and called it "fully attributed".  Only the SUM
    of that split ever had evidence.  The removal measures one of the four
    directly, and it is `tw + rw + 24` per (output row, template row) PLUS 3
    per output row -- not `tw + rw + 21` and nothing per output row.  So:

      * the other three terms sum to 2*tw + 2*rw + 9, not 2*tw + 2*rw + 12;
      * WHICH of the three was over-attributed is NOT ESTABLISHED.  PER_ROW_TERMS
        now carries an explicit `unattributed_correction` of -3 rather than
        silently shaving it off whichever line looked least defended;
      * 3 cycles per output row came out of the 5*rw + 99 term, whose internal
        split had no evidence either.
      * "FULLY ATTRIBUTED" IS WITHDRAWN.

    Note tw + rw = pw + 1, so the measured statistics cost is rh*th*(pw + 25) +
    3*rh: it depends on the PATCH WIDTH alone.  That is what the two loops
    actually scan -- tw priming iterations plus rw - 1 sliding ones -- so the
    measured form is the one the source predicts and the attributed one was
    not.

THE SHAPES ARE MEASURED; THE MECHANISMS ARE NOT.  Fourteen transactions pin +30
per scan, +5 per call, +3 per (output row, template row) over the old
attribution, and +3 per output row.  WHY each is there is not established -- no
experiment here separates pipeline flush from call overhead from loop-region
control, exactly as none separated B1's T + 1 or B2's 2*(T - 1).  Do not quote a
cause.  The 3*rh in particular is an accounting fact about where the cycles
went, not a claim that reset_acc dropping two array writes is what produced it.

THE WITHDRAWN ENDPOINTS DID NOT BRACKET THE ANSWER.  17.743730541333 (II=1) and
18.035794052889 (II=3) are withdrawn, and 17.726036 is BELOW BOTH.  The two
errors above point in opposite directions and the removal wins.  A range check
would have reported "inside the bracket" for a wrong implementation, which this
file already records once; here the correct implementation lands OUTSIDE it.
check() asserts the measurement is below both endpoints, so a future revision
cannot quietly restore the range and call it confirmed.

THE ITERATION COUNT SURVIVED UNCHANGED.  b0b_count_pass_iterations still gives

    I = pw * [th + 2*(rh - 1)]  ==  pw * (2*ph - th)

    max Phase-S trial   311 * (96 + 2*63)  =      69,042 iterations
    whole 20,680-trial corpus              = 657,142,901 iterations

and FROZEN["b0b_count_pass"] is untouched.  It encodes an ALGORITHM CHOICE, not
a lower bound: it assumes each row scan performs the horizontal rolling scan.
A fused subtract-and-add loop would cost pw*ph iterations -- FEWER -- but reads
four patch pixels per iteration against a 2-port cyclic-partitioned BRAM, and
collides whenever tw is a multiple of PAR_COLS.  The implementation uses
separate scans deliberately; see b0b_sources/README.md.

B0b IS NOT A UNIFORM IMPROVEMENT.  At rh == 1 it LOSES, by exactly 5*th + 2
cycles: one output row has nothing to reuse vertically and the pass still pays
its per-scan overhead.  Derived, asserted in check(), and visible on the
4x4/4x4 direct transaction as +22.  Over the real workload it wins by
2.679 s/page.

WHAT B0b DOES NOT FIX.  It removes only the window statistics.  The other
2*tw + 2*rw + 9 survives untouched, and its internal split is now explicitly
UNMEASURED:

    template-row staging            2*tw + 3   <- attributed, not measured
    correlation writeback/control   2*rw + 8   <- attributed, not measured
    accum_rows FSM transition       1          <- attributed, not measured
    unattributed correction        -3          <- what the measurement forces
                                    ---------
                                    2*tw + 2*rw + 9

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


# Variants whose per-invocation term comes from paired RTL co-simulation rather
# than from a projection.  sw/tme_b0b_ab.py reads this to decide whether
# checking the declared model against a measurement is a real check or a
# comparison of a projection with the thing that would replace it.
MEASURED_VARIANTS = ("cur", "B1", "B2", "B0b")


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
    elif variant in ("B2", "B0b_base", "B0b"):
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

    if variant in ("B0b_base", "B0b"):
        # B0b DELETES the repeated window statistics.  Paired RTL
        # co-simulation of the `b0b` solution against the `shadow` one -- the
        # same fourteen invocations, the same pinned vectors
        # (sw/tme_b0b_ab.py, 2026-08-20) -- gives
        #
        #     -D2 = rh*th*(tw + rw + 24) + 3*rh
        #
        # THAT IS THE DIFFERENCE, NOT THE DELETION.  The shadow also carries a
        # comparator inside norm_cols worth 2 cycles per output row, so the
        # deletion's own cost is this minus 2*rh.  The rh*th coefficient is
        # unaffected (an rh-proportional nuisance cannot move it); the 3 per
        # output row is.  The measured pair is used here because it is what
        # was measured and because its offset cancels exactly against the pass
        # term below -- see b0b_delta(), which asserts that.
        #
        # exactly, on 14/14 transactions.  Note tw + rw = pw + 1, so this is
        # rh*th*(pw + 25) + 3*rh: the statistics cost depends on the PATCH
        # WIDTH alone, which is what the two loops they replace actually scan
        # (tw priming iterations plus rw - 1 sliding ones).
        #
        # THE PROJECTION WAS tw + rw + 21 PER (OUTPUT ROW, TEMPLATE ROW) AND
        # NOTHING PER OUTPUT ROW.  It is short in both places.  That figure was
        # never measured -- it was one term of a four-way split of the fitted
        # 3*tw + 3*rw + 33, and only the SUM of that split had evidence.  Two
        # consequences, both recorded rather than smoothed over:
        #
        #   1. The other three terms now sum to 2*tw + 2*rw + 9, not
        #      2*tw + 2*rw + 12.  WHICH of the three was over-attributed is
        #      NOT established; the measurement constrains their sum only.
        #      "Fully attributed" is withdrawn -- see PER_ROW_TERMS.
        #   2. Three cycles per OUTPUT ROW belonged to the statistics and were
        #      inside the 5*rw + 99 term.  That term's internal split had no
        #      evidence either; 3 of the 99 now has some.
        stat = (3 * tw + 3 * rw + 33) - (tw + rw + 24)      # 2*tw + 2*rw + 9
        per_row = 5 * rw + 96                               # 99 - 3
    else:
        stat = 3 * tw + 3 * rw + 33
        per_row = 5 * rw + 99

    total = pw * ph + 24 + rh * per_row + rh * th * (tile + stat)

    if variant == "B0b":
        # The hoisted, vertically-reused pass that replaces them.  ALSO
        # MEASURED, from the `shadow` solution against `b2ctl` in the same
        # comparison:
        #
        #     D1 = S * (pw + 30) + 5,        S = th + 2*(rh - 1)
        #
        # exact on 14/14 -- and again this is the DIFFERENCE.  It is the pass
        # plus the shadow's comparator, +2*rh, the same 2*rh that D2 above is
        # short by.  The two cancel here, which is why the total is a
        # measurement even though neither line is a component cost.
        #
        # S*pw is the derived iteration count I (one scan is
        # tw + (rw - 1) = pw iterations); the +30 per scan and +5 per
        # invocation are the overhead the frozen endpoints assumed away.
        #
        # THE FROZEN ENDPOINTS ASSUMED pass = N*I FOR N IN {1, 3}, and csynth
        # says N really is 1 -- scan_init and scan_slide come out at II=1 with
        # iteration latencies 7 and 14, IDENTICAL to the isq_init and isq_slide
        # they replace.  The 25% miss is entirely the per-scan constant, which
        # was not modelled at all.  See the module docstring: the endpoints did
        # not bracket the answer, and it is the REMOVAL being under-attributed
        # that pulled the result back under them.
        total += (th + 2 * (rh - 1)) * (pw + 30) + 5

    return total


# ---------------------------------------------------------------------------
# 1b. THE B0b PASS AND THE ATTRIBUTION IT CORRECTED
# ---------------------------------------------------------------------------
# B0b = B2 - D2 + D1, where D1 and D2 are the two MEASURED co-simulation
# differences.  The NET is measured; the two differences are measured; what
# neither of them is, is a component cost -- each carries the shadow build's
# comparator, +2 cycles per output row, with opposite signs.
#
#     B2                                        20.405164783778 s/page
#     - D2   rh*th*(pw+25) + 3*rh                2.848889358222 s/page
#     + D1   S*(pw+30) + 5                       0.169760466889 s/page
#     -> B0b                                    17.726035892444 s/page
#
# THE TWO COMPONENT FIGURES WERE WRONG AND THE NET WAS RIGHT.  3.088545 and
# 0.409416 stood here until 2026-08-20; both are 0.239655641778 too large,
# the same offset, so B2 - removed + added came out exact.  check() now
# derives both from page_cycles_expr and asserts them against FROZEN, which
# is the only arrangement in which prose this specific stays true.
#
# WITHDRAWN: 17.743730541333 (II=1) and 18.035794052889 (II=3), and the
# 17.597699 "B0b base" they were built on.  The measurement is 17.726036,
# which is BELOW BOTH.  Read that carefully -- the pair was published as a
# bracket and it did not contain the answer:
#
#   * the hoisted pass costs MORE than the II=1 endpoint assumed, by 25% on
#     the cosim suite.  csynth says the II really is 1 (scan_init and
#     scan_slide come out at II=1 with iteration latencies 7 and 14, the same
#     as the isq_init and isq_slide they replace), so the whole miss is the
#     +30-per-scan and +5-per-invocation overhead that was not modelled;
#   * the REMOVAL is worth more than the model attributed, and that dominates.
#
# THE SECOND HALF IS THE MORE IMPORTANT FINDING.  It is not about B0b at all:
# it says the four-way split of the fitted per-(output row, template row) term
# was wrong, in a way no assertion here could have caught, because only the
# SUM of the four had evidence.


# The per-(output row, template row) term, split into the four parts it is
# made of.  ONE OF THE FOUR IS NOW MEASURED; the other three are not, and
# their individual values are NO LONGER CLAIMED.
#
# What check() proves is what it always proved: that the four expressions sum
# to the fitted 3*tw + 3*rw + 33.  What it can no longer prove -- because it is
# no longer true -- is that removing the window statistics leaves the other
# three unchanged at 2*tw + 2*rw + 12.  Paired co-simulation of `b0b` against
# `shadow` measured the removal directly:
#
#     -D2 = rh*th*(tw + rw + 24) + 3*rh
#
# exact on 14/14 transactions.  So the other three sum to 2*tw + 2*rw + 9.
#
# READ THE TWO HALVES OF THAT DIFFERENTLY.  D2 is `b0b - shadow`, and the
# shadow carries a comparator worth 2 cycles per OUTPUT ROW, so D2 is the
# removal minus that comparator.  A nuisance proportional to rh cannot move a
# coefficient whose regressor is rh*th when th varies, so tw + rw + 24 IS the
# removal's own per-(output row, template row) cost.  The 3 per OUTPUT ROW is
# not: it is the removal's own share plus the comparator's 2, and the split
# between them rests on a csynth schedule rather than on this measurement.
#
# WHICH of the remaining three was over-attributed by 3 is NOT ESTABLISHED.
# The measurement constrains their sum and nothing else.  The three
# expressions below are kept at their previous values so that the sum
# identity still holds and so that the history is legible, but each is now
# labelled with what it is: an unmeasured share.  DO NOT quote any of them as
# a measured cost.  "Fully attributed" is WITHDRAWN.
#
# No assertion can check English against code, so if you change a term here,
# re-read the module docstring by hand.
PER_ROW_TERMS = {
    # MEASURED (tme_b0b_ab.py check 3b, 14/14).  Note tw + rw = pw + 1, so
    # this is pw + 25: the statistics cost depends on the PATCH WIDTH alone,
    # which is what the two loops actually scan -- tw priming iterations plus
    # rw - 1 sliding ones.  There is a companion 3 per output row; see
    # PER_OUTPUT_ROW_STATISTICS below.
    "window_statistics": lambda tw, rw: tw + rw + 24,
    # UNMEASURED SHARES.  These three sum to 2*tw + 2*rw + 9, which IS
    # measured (it is what survives the removal).  Their individual split is a
    # 2026-08-17 attribution that has never been tested and is now known to be
    # 3 too large in total.
    "template_row_staging": lambda tw, rw: 2 * tw + 3,       # measured crit path
    "correlation_writeback_control": lambda tw, rw: 2 * rw + 8,
    "accum_rows_fsm_transition": lambda tw, rw: 1,
    # The bookkeeping entry that makes the sum work out.  It is not a fifth
    # mechanism -- it is the -3 the measurement says the three lines above
    # collectively overstate by.  Naming it is better than silently shaving 3
    # off whichever of them looked least defended.
    "unattributed_correction": lambda tw, rw: -3,
}

# THE PER-OUTPUT-ROW TERM OF D2, WHICH IS NOT THE SAME AS THE STATISTICS' OWN.
# 3 is what `b0b - shadow` gives, and the shadow's comparator is 2 of it (see
# FROZEN["b0b"]["comparator_cycles_per_output_row"], a csynth reading, not a
# co-simulation).  So the statistics' own share is 1 IF that reading is right
# and IF there is no further per-invocation constant, and neither is
# established.  The model uses 3 because 3 is what was MEASURED and because the
# offset cancels against the pass term; see B0B_SPLIT_NUISANCE_PER_OUTPUT_ROW.
#
# The claim that "3 of the 99 now has some evidence" is correspondingly
# weakened: what has evidence is that D2 carries 3 per output row.
PER_OUTPUT_ROW_STATISTICS = 3

# The comparator the `shadow` build needs in order to be observable at all, in
# cycles per output row, as located by csynth: norm_cols 97 ~ 3361 in b2ctl and
# b0b, 99 ~ 3363 in shadow, with II and iteration latency identical in all
# three.  It enters D1 as +2*rh and D2 as -2*rh and CANCELS in the net.
#
# Every figure in this file uses the MEASURED split, so this constant changes
# nothing that is computed.  It exists so that the alternative split can be
# written down and asserted to give the same total -- which is the proof that
# the split is not identified, rather than a claim that either half is right.
B0B_SPLIT_NUISANCE_PER_OUTPUT_ROW = 2


def b0b_delta(pw: int, ph: int, tw: int, th: int, decontaminated: bool = False):
    """What B0b changes about B2, by either of the two candidate splits.

    Both return the SAME integer for every geometry.  That identity is the
    content: the co-simulation constrains the sum and not the split, so a
    "decontaminated" reading of the halves is a relabelling of the same law,
    not a second measurement of it.

    `decontaminated=False` is the measured pair, D1 and D2 as co-simulated.
    `decontaminated=True` moves 2*rh from D1 to D2, which is where csynth puts
    the shadow's comparator -- pass = S*(pw + 29) + th + 3 and removal =
    rh*th*(tw + rw + 24) + rh.  NOT FROZEN; see the module docstring.
    """
    rw, rh = pw - tw + 1, ph - th + 1
    S = th + 2 * (rh - 1)
    W = FROZEN["b0b"]["d2_W"]
    if decontaminated:
        c = FROZEN["b0b"]["d2_c_per_output_row"] - B0B_SPLIT_NUISANCE_PER_OUTPUT_ROW
        removal = rh * th * (tw + rw + W) + c * rh
        pass_ = S * (pw + FROZEN["b0b"]["d1_k_per_scan"] - 1) + th + 3
    else:
        removal = (rh * th * (tw + rw + W)
                   + FROZEN["b0b"]["d2_c_per_output_row"] * rh)
        pass_ = (S * (pw + FROZEN["b0b"]["d1_k_per_scan"])
                 + FROZEN["b0b"]["d1_m_per_call"])
    return pass_ - removal


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
    """WITHDRAWN.  Use cycles(..., "B0b"), which is measured.

    This priced B0b as `B2 - rh*th*(tw+rw+21) + N*I` for a projected N.  Both
    of those inputs turned out to be wrong, in opposite directions:

      * N really is 1 -- csynth puts the hoisted pass's two loops at II=1,
        with the same iteration latencies as the loops they replace -- but the
        pass carries +30 per scan and +5 per invocation that this expression
        does not model, so N*I understates it by 25% on the cosim suite;
      * `tw + rw + 21` understates the removal.  The measurement is
        `rh*th*(tw + rw + 24) + 3*rh`.

    Net, the endpoints this produced (17.743730541333 and 18.035794052889)
    BRACKETED NOTHING: the measured figure is 17.726036, below both.  Keeping
    the function callable would let a caller reproduce a withdrawn number.
    """
    raise ValueError(
        "cycles_b0b() is withdrawn: its projected II and its projected "
        "window-statistics attribution were both wrong, and the endpoints it "
        "produced (17.743730541333 / 18.035794052889) do not contain the "
        "measured 17.726035892444.  Call cycles(pw, ph, tw, th, 'B0b'), which "
        "is the paired-cosim-measured term.  The iteration count itself is "
        "unchanged and still lives in b0b_count_pass_iterations().")


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

# B2 ON SILICON -- a SEPARATE table, deliberately.  BOARD_MEASUREMENTS above is
# the UNCHANGED core (`cur`) at two clocks, and its two-clock linearity ratio is
# a claim about that core; folding B2 rows into it would silently redefine both.
# These rows are the B2 image, one clock, and they are checked against
# cycles(..., "B2") rather than the default variant.
#
# Session: logs/b2_board_20260819/, 2026-08-20, commit 729582e.  FCLK0 gated at
# 125.0000 MHz, expected-HWH-VLNV TermCountB2:hls:tme_top:0.2, ten-file checksum
# gate, both suites, verified re-invocation and DMA halt, RESTORE_VERIFIED.
#
# WHAT THESE ROWS ADD OVER THE COSIM.  The co-simulation pinned the term exactly
# but only to T = 6.  The last two rows are T = 38 and T = 52 -- the compiled
# maximum -- so the tile-count axis is now silicon-exercised.  Agreement there is
# the strongest single number in this file: a 2.059 s measurement against a
# 2.0572 s model is 0.09%.
#
# THE RESIDUALS ARE CONSISTENT WITH FIXED OVERHEAD.  Every one is positive and
# lands between +0.0016 and +0.0025 s, with no trend against case size, which is
# what a fixed per-invocation DMA setup / marshalling / polling cost would look
# like -- the same effect BOARD_MEASUREMENTS shows for `cur`.  CONSISTENT WITH is
# the whole claim: nothing here isolates the cause, and no experiment in this
# file separates DMA setup from PS scheduling or from a small constant the term
# itself is missing.  It is why the seven-case phase_s total is 0.408 s against a
# 0.394 s core term, but "why" is inference, not measurement.
BOARD_MEASUREMENTS_B2 = [
    ("phase-s-min-templ",     ( 99,  67,   4,  4), 125e6, 0.004, "2026-08-20 B2 phase_s"),
    ("phase-s-origin",        (147,  94,  52, 31), 125e6, 0.020, "2026-08-20 B2 phase_s"),
    ("phase-s-workload-mode", (147,  94,  52, 31), 125e6, 0.020, "2026-08-20 B2 phase_s"),
    ("phase-s-workload-wide", (259, 105, 164, 42), 125e6, 0.050, "2026-08-20 B2 phase_s"),
    ("phase-s-final-cell",    (215, 157, 120, 94), 125e6, 0.088, "2026-08-20 B2 phase_s"),
    ("phase-s-workload-max",  (215, 157, 120, 94), 125e6, 0.088, "2026-08-20 B2 phase_s"),
    ("phase-s-max",           (311, 159, 216, 96), 125e6, 0.138, "2026-08-20 B2 phase_s"),
    ("stress-max-envelope",   (820, 307, 216, 96), 125e6, 2.059, "2026-08-20 B2 hw, T=38"),
    ("stress-max-result",     (820, 307,   4,  4), 125e6, 0.063, "2026-08-20 B2 hw, T=52"),
]

# The B1 board session measured the same seven phase_s cases on the B1 image at
# the same gated clock (logs/b1_board_20260818/03_run.txt).  Retained here so the
# B1-vs-B2 wall-time comparison is a pair of frozen tables rather than a sentence
# quoting one of them from memory, and b2_evidence_crosscheck() re-reads BOTH
# totals from their transcripts so neither can drift here unnoticed.
#
# WHAT WAS ACTUALLY CONSTANT: the three vector files (byte-identical), the gated
# clock, and the timed execution path.  NOT the runner -- B1 ran f7b00b0e and B2
# ran ca62cbbc, which adds a finite-FCLK guard and an expected-HWH-VLNV check.
# Both complete before ap_start and neither is inside a timed region, so the
# pairing holds; "only the bitstream differed" would be wrong and is the claim
# B1's own session could make against Priority 3 but this one cannot.
BOARD_PHASE_S_B1_SECONDS = 0.544
BOARD_PHASE_S_B2_SECONDS = 0.408

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
    # THE B2 BOARD SESSION, frozen on the same footing as `board` above and for
    # the same reason: BOARD_TOLERANCE_S is far too loose to catch 2.059 edited
    # to 2.058, so the measured seconds are pinned by exact comparison.
    "board_b2": {
        # Exact duplicate of BOARD_MEASUREMENTS_B2, by (case, clock) -> seconds.
        "measurements": {
            ("phase-s-min-templ",     125e6): 0.004,
            ("phase-s-origin",        125e6): 0.020,
            ("phase-s-workload-mode", 125e6): 0.020,
            ("phase-s-workload-wide", 125e6): 0.050,
            ("phase-s-final-cell",    125e6): 0.088,
            ("phase-s-workload-max",  125e6): 0.088,
            ("phase-s-max",           125e6): 0.138,
            ("stress-max-envelope",   125e6): 2.059,
            ("stress-max-result",     125e6): 0.063,
        },
        # The gate itself, so a later reader does not have to trust prose.
        "session": {
            "measured_fclk0_mhz": 125.0,
            "routed_period_ns": 8.000,
            "routed_wns_ns": 0.011710,
            "hwh_vlnv": "TermCountB2:hls:tme_top:0.2",
            "phase_s_passed": 7,
            "phase_s_total": 7,
            "hw_passed": 9,
            "hw_total": 9,
            # Tile counts the hw suite reaches that no B2 co-simulation did.
            "max_tile_count": 52,
            "envelope_tile_count": 38,
            # WHAT THE SIXTEEN CASES ACTUALLY SAMPLE.  Not "every tile count
            # from 1 to 52" -- that was written once and is false.  These six
            # values are what the two suites contain, recomputed from the
            # transcript's case geometries by b2_evidence_crosscheck().
            "tile_counts_sampled": (1, 3, 4, 6, 38, 52),
            "max_single_transfer_bytes": 251740,
        },
        # The paired wall-time comparison, same stimulus and clock, B1 image vs
        # B2 image.  DERIVED from the two totals rather than transcribed.
        "phase_s_pair": {
            "b1_seconds": 0.544,
            "b2_seconds": 0.408,
            "saving_seconds": 0.136,
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
    # B0b, frozen EXACTLY, on B1's and B2's argument: the aggregate is an
    # integer and is asserted with ==, because TOL = 5e-4 on a 36-page average
    # spans +/- 2,250,000 cycles.
    #
    # The NET is measured, by paired RTL co-simulation over the same fourteen
    # invocations (sw/tme_b0b_ab.py, 2026-08-20).  Three solutions were built,
    # not two, because B0b is two changes: `shadow` adds the hoisted pass
    # without removing anything, so shadow - b2ctl and b0b - shadow are the two
    # halves of b0b - b2ctl.
    #
    # THE HALVES ARE NOT COMPONENT COSTS.  The keys below are named for the
    # DIFFERENCES they were fitted to -- d1_*, d2_* -- and not for the pass and
    # the removal, which is what they used to be called.  The shadow's
    # comparator reschedules norm_cols by +2 cycles a call and norm_cols runs
    # once per output row, so d1 carries +2*rh that the pass does not and d2 is
    # short by the same.  They cancel in the aggregate, which is why it is
    # frozen and they are not quotable as costs.  See the module docstring and
    # sw/tme_b0b_synth.py, whose recorded inventory is what pins the +2.
    "b0b": {
        "aggregate_cycles": 79767161516,        # exact, asserted with ==
        "s_per_page": 17.726035892444,          # derived from the integer
        # D1 = shadow - b2ctl, measured: S*(pw + k) + m with S = th + 2*(rh-1).
        # S*pw is the derived iteration count; k and m are the overhead the
        # withdrawn endpoints assumed away PLUS the comparator.  Neither k nor
        # m is a property of the pass: shifting 2*rh out of D1 turns this into
        # S*(pw + 29) + th + 3, which fits the same 14/14 exactly.
        "d1_k_per_scan": 30,
        "d1_m_per_call": 5,
        # D2 = b0b - shadow, measured: -(rh*th*(tw + rw + W) + c*rh).  The
        # model attributed W = 21 and c = 0.
        #
        # W IS IDENTIFIED AND c IS NOT.  The comparator's nuisance is
        # proportional to rh; W's regressor is rh*th and th varies across the
        # suite, so no rh-proportional term can move W.  c is the removal's own
        # share plus the comparator's 2.
        "d2_W": 24,
        "d2_c_per_output_row": 3,
        # csynth, not co-simulation: norm_cols is 97 ~ 3361 in b2ctl and b0b
        # and 99 ~ 3363 in shadow, with II and iteration latency identical in
        # all three.  This is what makes the split UNIDENTIFIED rather than
        # merely unmeasured -- and it is also why no comparator-free control
        # was built: a shadow solution needs both copies of the statistics
        # live, an unread copy is deleted as dead code, and the cheapest
        # consumer that keeps both live is the comparator already there.
        "comparator_cycles_per_output_row": 2,
        "comparator_source": "csynth loop table, not co-simulation",
        # The corpus components, ASSERTED rather than described.  Both were
        # written in prose as 3.088545 and 0.409416 until 2026-08-20; both were
        # 0.239655641778 too large, the same offset, so the net stayed exact
        # and nothing caught it.  check() now derives each from
        # page_cycles_expr and compares it here.
        "d2_s_per_page": 2.848889358222,
        "d1_s_per_page": 0.169760466889,
        "comparator_s_per_page": 0.000588231111,
        "withdrawn_component_s_per_page": (3.088545, 0.409416),
        # Both withdrawn endpoints, kept so the assertion can prove the
        # measurement is OUTSIDE the interval they defined rather than inside
        # it.  A range check would have passed a wrong answer; this one fails
        # if anyone ever re-derives the range and calls it confirmed.
        "withdrawn_at_1_cyc": 17.743730541333,
        "withdrawn_at_3_cyc": 18.035794052889,
        "withdrawn_base": 17.597699,
        # The saving over B2 at the largest Phase-S trial, 311x159 / 216x96.
        "phase_s_max_cycles": 14950652,
        "phase_s_max_delta_cycles": -1988869,   # against B2
        # B0b is a REGRESSION at rh == 1, by exactly 5*th + 2 cycles: with one
        # output row there is nothing to reuse vertically, and the hoisted pass
        # still pays its per-scan overhead.  Derived, and confirmed on the
        # 4x4/4x4 direct transaction (th = 4 -> +22).
        "rh1_regression_cycles": "5*th + 2",
        # ROUTED 2026-08-20, AND IT DOES NOT CLOSE.  Same default Vivado flow
        # that gave B1 +0.134571 ns and B2 +0.011710 ns at the same 8.000 ns
        # constraint -- no strategy or directive override in any of the three,
        # which is what makes them comparable.
        #
        # The binding path is a `seg` register feeding the MAC's DSP input
        # INSIDE correlation_core -- structurally the path B2 bound on, and a
        # file B0b does not edit.  B2's evidence named this risk in advance:
        # B0b inherits 12 ps on a path it does not touch, and disturbing
        # placement around it is a plausible way to lose it.  It lost it.
        #
        # CONSEQUENCE FOR EVERY B0b FIGURE HERE: the s/page is a conversion at
        # 125 MHz, and this core has no routed build at 125 MHz.  The CYCLE
        # term is unaffected -- it is a zero-stall RTL schedule and does not
        # depend on the clock -- but "B0b at 125 MHz" is not supported.
        #
        # NOT ESTABLISHED: that B0b cannot close 8 ns.  One run, default
        # effort, no directive or seed sweep and no post-route phys_opt.
        "routed_period_ns": 8.000,
        "routed_wns_ns": -0.051470,
        "routed_tns_ns": -0.051470,
        "routed_closes": False,
        "routed_luts": 21176,
        "routed_ffs": 24887,
        "routed_bram": 115,
        "routed_dsp": 34,
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
        # MEASURED, 2026-08-20.  Both of B0b's terms come from paired RTL
        # co-simulation now; nothing about it is projected.  The BINDING freeze
        # is the exact integer aggregate in FROZEN["b0b"] above.
        #
        # WHAT THIS REPLACED.  The endpoints 17.743730541333 (II=1) and
        # 18.035794052889 (II=3), and the 17.597699 base under them, are
        # WITHDRAWN.  They were published as a bracket, and 17.726036 is below
        # both of them -- so the bracket did not contain the answer.  Two
        # projected inputs, wrong in opposite directions:
        #   * the hoisted pass costs 25% MORE than the II=1 endpoint assumed
        #     (the II really is 1; the miss is per-scan overhead), and
        #   * the removal is worth MORE than the model attributed, which
        #     dominates.
        # The second is a finding about the old four-way split of the fitted
        # per-row term, not about B0b.  See PER_ROW_TERMS.
        "B0b": 17.726,
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
            "B0b": s_page("per_trial", "B0b"),
        },
        # B0b's two co-simulation DIFFERENCES, priced over the same corpus.
        # Computed here rather than inside check() so they are printed, put in
        # the JSON freeze, and asserted -- all three.  They were none of those
        # things until 2026-08-20, which is how two wrong figures survived in
        # the prose while the net they sum to stayed exactly right.
        "b0b_stats_removed": page_cycles_expr(_b0b_d2_expr),
        "b0b_pass_added": page_cycles_expr(_b0b_d1_expr),
        "b0b_comparator": page_cycles_expr(_b0b_comparator_expr),
    }


def _b0b_live(pw, ph, tw, th):
    return pw - tw + 1 >= 1 and ph - th + 1 >= 1


def _b0b_d2_expr(pw, ph, tw, th):
    """-D2 = the b0b - shadow difference: the removal PLUS the comparator."""
    if not _b0b_live(pw, ph, tw, th):
        return 0
    rw, rh = pw - tw + 1, ph - th + 1
    return (rh * th * (tw + rw + FROZEN["b0b"]["d2_W"])
            + FROZEN["b0b"]["d2_c_per_output_row"] * rh)


def _b0b_d1_expr(pw, ph, tw, th):
    """D1 = the shadow - b2ctl difference: the pass PLUS the comparator."""
    if not _b0b_live(pw, ph, tw, th):
        return 0
    return ((th + 2 * (ph - th)) * (pw + FROZEN["b0b"]["d1_k_per_scan"])
            + FROZEN["b0b"]["d1_m_per_call"])


def _b0b_comparator_expr(pw, ph, tw, th):
    """The shadow's comparator alone: +2 per output row, in BOTH differences."""
    if not _b0b_live(pw, ph, tw, th):
        return 0
    return B0B_SPLIT_NUISANCE_PER_OUTPUT_ROW * (ph - th + 1)


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


# One `--- cases ---` row of a board transcript:
#   [7] stress-max-envelope  stress  patch 820x307 templ 216x96  gold ... 2.059 s (
# Both column layouts the runner emits are covered -- `@( 51, 33)` with padding
# and `@(604,211)` without -- because the middle of the line is skipped and the
# seconds are anchored on the " s (" that precedes the throughput figure.
_BOARD_CASE_RE = re.compile(
    r"^\[(\d+)\]\s+(\S+)\s+\S+\s+patch\s+(\d+)x(\d+)\s+templ\s+(\d+)x(\d+)\s+"
    r".*?\s([0-9]+\.[0-9]+) s \(")


def parse_board_cases(text):
    """tag -> ((pw, ph, tw, th), seconds) for every case row in a transcript.

    The frozen tables and FROZEN are checked against EACH OTHER in check(),
    which catches an edit to one of them.  It does not catch a consistent edit
    to both, and that is exactly the drift a freeze is supposed to prevent.
    The retained transcript is the third party neither can be edited into
    agreement with, so the geometries and wall times are re-read from it.
    """
    rows = {}
    for line in text.splitlines():
        m = _BOARD_CASE_RE.match(line)
        if m:
            rows[m.group(2)] = ((int(m.group(3)), int(m.group(4)),
                                 int(m.group(5)), int(m.group(6))),
                                float(m.group(7)))
    return rows


def b0b_evidence_crosscheck():
    """Re-read the B0b routed report, if present.

    B0b's routed run VIOLATED its constraint, so this checks the opposite of
    what B1's and B2's crosschecks check.  That asymmetry is deliberate: a
    negative result is exactly the kind that decays into "we routed it" if
    nothing re-reads the report, and the failure is the load-bearing fact --
    every B0b s/page here is a conversion at a clock this core has no closing
    build for.

    Returns (failures, checked_count).  An absent report yields ([], 0) --
    never a pass.
    """
    root = Path(__file__).resolve().parents[2]
    if not (root / "logs").is_dir():          # clean checkout: sw/ is a sibling
        root = Path(__file__).resolve().parents[1]
    routed = root / "logs" / "b0b_20260820" / "b0b_post_route_wns.txt"
    util = root / "logs" / "b0b_20260820" / "b0b_post_route_utilization.rpt"
    if not routed.exists():
        return [], 0

    fail, checked = [], 0
    f = FROZEN["b0b"]
    txt = routed.read_text(errors="replace")

    for label, key, pat, tol in (
            ("period", "routed_period_ns",
             r"constrained period\s*:\s*([0-9.]+)\s*ns", 1e-9),
            ("WNS", "routed_wns_ns",
             r"post-route WNS\s*:\s*([0-9.+-]+)\s*ns", 5e-7),
            ("TNS", "routed_tns_ns",
             r"post-route TNS\s*:\s*([0-9.+-]+)\s*ns", 5e-7)):
        m = re.search(pat, txt)
        if not m:
            fail.append("B0b routed report: no {} line".format(label))
            continue
        checked += 1
        if abs(float(m.group(1)) - f[key]) > tol:
            fail.append("B0b routed {}: report says {}, frozen {}".format(
                label, m.group(1), f[key]))

    # THE VERDICT, read from the `verdict :` LINE rather than by searching the
    # whole file.  These reports carry a long explanatory tail that quotes the
    # extractor's "all constraints met" for comparison, so a substring search
    # over the body finds that phrase in a report whose own verdict is the
    # opposite.  That is not a hypothetical: it is what the first version of
    # this check did, and it failed on the very report it was written for.
    checked += 1
    m = re.search(r"^\s*verdict\s*:\s*(.+?)\s*$", txt, re.M)
    if not m:
        fail.append("B0b routed report: no verdict line")
    elif m.group(1) != "CONSTRAINTS VIOLATED":
        fail.append("B0b routed verdict is '{}', frozen as violating -- if this "
                    "build now closes, FROZEN['b0b']['routed_closes'] and every "
                    "caveat attached to it are stale".format(m.group(1)))
    checked += 1
    if f["routed_closes"] is not False:
        fail.append("FROZEN['b0b']['routed_closes'] is not False")

    # THE RETAINED PROSE IS STALE AND MUST STAY MARKED AS SUCH.  The report was
    # generated by a branch with no failed-timing case, so its body still says
    # "the LOGIC closes at the probed period" about a run that violated its
    # constraint.  The generator is fixed
    # (vivado/tme_standalone/build_tme_standalone.tcl), but this artifact is
    # NOT regenerated -- the numbers in it are the ones that were measured.  A
    # correction header was prepended instead, and if anyone strips it the
    # report reverts to claiming the opposite of its own verdict.
    checked += 1
    if "CORRECTION 2026-08-20" not in txt:
        fail.append("B0b routed report has lost its correction header: the "
                    "retained body says the logic CLOSES at the probed period "
                    "and the verdict line says CONSTRAINTS VIOLATED.  Restore "
                    "the header or regenerate the report with the fixed "
                    "generator.")
    elif "So this result says the LOGIC closes" in txt and \
            "STALE 1" not in txt:
        fail.append("B0b routed report still carries the 'closes' prose "
                    "without the passage being marked stale")

    # The binding path is INSIDE correlation_core, which B0b does not edit.
    # That is what makes this a placement-sensitivity result rather than a
    # consequence of the source change, so the report settles it rather than
    # the prose.
    checked += 1
    m = re.search(r"binding path.*?\n\s*from:\s*(\S+)", txt, re.S)
    if not m:
        fail.append("B0b routed report: no binding path")
    elif "correlation_core" not in m.group(1):
        fail.append("B0b binding path is not inside correlation_core: "
                    + m.group(1))

    if util.exists():
        u = util.read_text(errors="replace")
        for label, key, pat in (
                ("LUT", "routed_luts", r"\|\s*Slice LUTs\s*\|\s*(\d+)"),
                ("FF", "routed_ffs", r"\|\s*Slice Registers\s*\|\s*(\d+)"),
                ("BRAM", "routed_bram", r"\|\s*Block RAM Tile\s*\|\s*(\d+)"),
                ("DSP", "routed_dsp", r"\|\s*DSPs\s*\|\s*(\d+)")):
            m = re.search(pat, u)
            if not m:
                fail.append("B0b utilisation: no {} row".format(label))
                continue
            checked += 1
            if int(m.group(1)) != f[key]:
                fail.append("B0b routed {}: report says {}, frozen {}".format(
                    label, m.group(1), f[key]))

    return fail, checked


def b2_evidence_crosscheck():
    """Re-read the B2 routed report and board transcript, if present.

    Until 2026-08-20 this checked strictly less than b1_evidence_crosscheck,
    because B2 had no board transcript to read.  It now has one, and it carries
    MORE than B1's: B1's session ran phase_s only, so this reads two suite
    tallies and two re-invocation checks rather than one of each.

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

    # THE BOARD TRANSCRIPT.  Two suites, so every tally below is checked TWICE
    # and a transcript carrying only one of them fails -- that asymmetry with
    # B1 is the whole reason the hw suite was run, and it must not be possible
    # to lose it by quoting a phase_s-only transcript here.
    # WHERE THE BOARD TRANSCRIPT LIVES.  Unlike every other evidence directory,
    # logs/b2_board_20260819/ is canonical in the UPLOAD worktree -- the board
    # protocol was prepared and committed there directly, which is why
    # tme_b1_manifest.resolve() special-cases exactly this prefix.  `root` above
    # points at the outer development tree, so look there first and then fall
    # back to the upload tree.  In a clone the two are the same directory and
    # this costs one stat.
    run = root / "logs" / "b2_board_20260819" / "03_run.txt"
    if not run.exists():
        alt = (Path(__file__).resolve().parents[1] / "logs"
               / "b2_board_20260819" / "03_run.txt")
        if alt.exists():
            run = alt
    if run.exists():
        sess = FROZEN["board_b2"]["session"]
        txt = run.read_text(errors="replace")

        clocks = re.findall(r"FCLK0 gate: PASS.*?([0-9]+\.[0-9]+) MHz", txt)
        if len(clocks) != 2:
            fail.append("B2 board transcript: {} passing FCLK0 gate lines, "
                        "expected 2 (one per suite)".format(len(clocks)))
        for got in clocks:
            checked += 1
            if abs(float(got) - sess["measured_fclk0_mhz"]) > 1e-4:
                fail.append("B2 board clock: transcript says {} MHz, frozen "
                            "{}".format(got, sess["measured_fclk0_mhz"]))

        # The VLNV gate. It checks HWH metadata consistency, NOT fabric
        # readback -- see the session plan -- but a transcript that does not
        # carry it is not the gated run this freeze describes.
        vlnv = re.findall(r"expected-HWH-VLNV gate: PASS.*?is (\S+)", txt)
        if len(vlnv) != 2:
            fail.append("B2 board transcript: {} passing HWH-VLNV gate lines, "
                        "expected 2".format(len(vlnv)))
        for got in vlnv:
            checked += 1
            if got != sess["hwh_vlnv"]:
                fail.append("B2 board VLNV: transcript says {}, frozen "
                            "{}".format(got, sess["hwh_vlnv"]))

        tallies = [(int(a), int(b))
                   for a, b in re.findall(r"(\d+)/(\d+) cases passed", txt)]
        want = [(sess["phase_s_passed"], sess["phase_s_total"]),
                (sess["hw_passed"], sess["hw_total"])]
        checked += 1
        if tallies != want:
            fail.append("B2 board tallies: transcript says {}, frozen "
                        "{}".format(tallies, want))

        reinvokes = re.findall(r"re-invocation check: (\w+)", txt)
        checked += 1
        if reinvokes != ["PASS", "PASS"]:
            fail.append("B2 board transcript: re-invocation checks are {}, "
                        "expected two PASSes".format(reinvokes))

        # The wrapper's own verdict, and the restoration it gates on.
        checked += 1
        if "B2_GATE_PASS" not in txt:
            fail.append("B2 board transcript: no B2_GATE_PASS line")
        checked += 1
        if "RESTORE_VERIFIED" not in txt:
            fail.append("B2 board transcript: no RESTORE_VERIFIED line")
        checked += 1
        if "CHECKSUM_GATE_PASS 10/10" not in txt:
            fail.append("B2 board transcript: no 10/10 checksum gate line")
        # The maximum single transfer, re-read rather than trusted.
        checked += 1
        m = re.search(r"3\.1 EXERCISED:.*?moved ([\d,]+) B", txt)
        if not m:
            fail.append("B2 board transcript: no exercised max-transfer line")
        elif int(m.group(1).replace(",", "")) != sess["max_single_transfer_bytes"]:
            fail.append("B2 board max transfer: transcript says {}, frozen "
                        "{}".format(m.group(1), sess["max_single_transfer_bytes"]))

        # EVERY ROW OF THE MEASUREMENT TABLE, RE-READ.  check() compares
        # BOARD_MEASUREMENTS_B2 against FROZEN["board_b2"] element-wise, which
        # fails on an edit to one of them and PASSES on a consistent edit to
        # both.  This is the pass that makes that impossible: the geometry and
        # the wall time of every frozen row must be the ones the transcript
        # records, and the transcript is not something the freeze can rewrite.
        rows = parse_board_cases(txt)
        checked += 1
        want_rows = sess["phase_s_total"] + sess["hw_total"]
        if len(rows) != want_rows:
            fail.append("B2 board transcript: {} case rows, expected {} "
                        "({} phase_s + {} hw) -- a short transcript is a "
                        "truncated capture, not a smaller suite".format(
                            len(rows), want_rows, sess["phase_s_total"],
                            sess["hw_total"]))
        # WHICH TILE COUNTS THE SESSION ACTUALLY SAMPLED, from all sixteen
        # cases rather than the nine timed rows.  This is the assertion behind
        # "tile counts {1, 3, 4, 6, 38, 52}"; without it that set is prose.
        checked += 1
        sampled = tuple(sorted({-(-(g[0] - g[2] + 1) // 16)
                                for _tag, (g, _s) in rows.items()}))
        if sampled != tuple(sess["tile_counts_sampled"]):
            fail.append("B2 board tile counts: transcript samples {} != frozen "
                        "{}".format(sampled, tuple(sess["tile_counts_sampled"])))

        for name, geom, _hz, meas, note in BOARD_MEASUREMENTS_B2:
            if name not in rows:
                fail.append("B2 board transcript: frozen row {} has no case "
                            "line [{}]".format(name, note))
                continue
            t_geom, t_secs = rows[name]
            checked += 1
            if t_geom != geom:
                fail.append("B2 board {}: transcript geometry {} != frozen "
                            "{}".format(name, t_geom, geom))
            if t_secs != meas:
                fail.append("B2 board {}: transcript {} s != frozen {} s -- the "
                            "table and FROZEN agree with each other but not "
                            "with the run".format(name, t_secs, meas))

        # The two phase_s totals, summed from the transcripts rather than
        # trusted.  B1's comes from ITS OWN transcript: the pair is the headline
        # comparison, and a frozen 0.544 that no longer matches the B1 session
        # would otherwise sit here indefinitely.
        checked += 1
        ps_b2 = round(sum(secs for tag, (_g, secs) in rows.items()
                          if tag.startswith("phase-s-")), 3)
        if ps_b2 != FROZEN["board_b2"]["phase_s_pair"]["b2_seconds"]:
            fail.append("B2 phase_s total: transcript sums to {} != frozen "
                        "{}".format(ps_b2,
                                    FROZEN["board_b2"]["phase_s_pair"]["b2_seconds"]))

        b1_run = root / "logs" / "b1_board_20260818" / "03_run.txt"
        if b1_run.exists():
            b1_rows = parse_board_cases(b1_run.read_text(errors="replace"))
            checked += 1
            ps_b1 = round(sum(secs for tag, (_g, secs) in b1_rows.items()
                              if tag.startswith("phase-s-")), 3)
            if ps_b1 != FROZEN["board_b2"]["phase_s_pair"]["b1_seconds"]:
                fail.append("B1 phase_s total: its transcript sums to {} != "
                            "frozen {}".format(
                                ps_b1,
                                FROZEN["board_b2"]["phase_s_pair"]["b1_seconds"]))
            # The comparison is only paired if both ran the same geometries.
            checked += 1
            shared = {t: g for t, (g, _s) in b1_rows.items()
                      if t.startswith("phase-s-")}
            mismatched = sorted(
                t for t, g in shared.items()
                if t in rows and rows[t][0] != g)
            if mismatched:
                fail.append("B1/B2 phase_s pairing: {} ran different geometries "
                            "in the two sessions".format(mismatched))
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

    # THE B2 BOARD SESSION.  Same two-layer treatment as the `cur` rows above:
    # the SET is frozen so a dropped row fails, each measured value is frozen
    # EXACTLY, and the model is then held to BOARD_TOLERANCE_S -- against
    # cycles(..., "B2"), which is the whole point of a separate table.
    live_b2 = {(n, hz): m for n, _g, hz, m, _t in BOARD_MEASUREMENTS_B2}
    frozen_b2 = FROZEN["board_b2"]["measurements"]
    if len(BOARD_MEASUREMENTS_B2) != len(frozen_b2):
        fail.append("board_b2: {} measurement rows != frozen {}".format(
            len(BOARD_MEASUREMENTS_B2), len(frozen_b2)))
    for key in sorted(set(live_b2) | set(frozen_b2), key=lambda k: (k[0], k[1])):
        if key not in live_b2:
            fail.append("board_b2: frozen row {} @ {:g}MHz is MISSING from "
                        "BOARD_MEASUREMENTS_B2".format(key[0], key[1] / 1e6))
        elif key not in frozen_b2:
            fail.append("board_b2: row {} @ {:g}MHz is not frozen".format(
                key[0], key[1] / 1e6))
        elif live_b2[key] != frozen_b2[key]:
            fail.append("board_b2: {} @ {:g}MHz measured {} != frozen {}".format(
                key[0], key[1] / 1e6, live_b2[key], frozen_b2[key]))

    # THE RESIDUAL BOUND, AND WHY IT IS NOT "STRICTLY POSITIVE".  An earlier
    # revision asserted resid > 0 and justified it as "a negative residual would
    # falsify the term".  That was too strong.  Wall times print to milliseconds,
    # so a measurement carries +/-0.0005 s of rounding on its own: a TRUE
    # residual of exactly zero can print negative, and a small negative residual
    # inside that floor falsifies nothing.  The floor is therefore the lower
    # bound, and only an excursion BELOW it -- silicon beating the cycle term by
    # more than rounding can explain -- is treated as a failure.
    B2_PRINT_FLOOR_S = 0.0005          # wall times print to milliseconds
    B2_RESIDUAL_MAX_S = 0.003
    for name, geom, hz, meas, note in BOARD_MEASUREMENTS_B2:
        model_s = cycles(*geom, "B2") / hz
        resid = meas - model_s
        if abs(resid) > BOARD_TOLERANCE_S:
            fail.append("board_b2 {} {} @ {:g}MHz: model {:.4f}s vs measured "
                        "{}s (> {}s) [{}]".format(name, geom, hz / 1e6, model_s,
                                                  meas, BOARD_TOLERANCE_S, note))
        if not -B2_PRINT_FLOOR_S <= resid <= B2_RESIDUAL_MAX_S:
            fail.append("board_b2 {} residual {:+.4f}s is outside "
                        "[-{}, {}] -- below the print floor means silicon beat "
                        "the cycle term by more than rounding explains [{}]".format(
                            name, resid, B2_PRINT_FLOOR_S, B2_RESIDUAL_MAX_S, note))

    # The paired phase_s wall totals, DERIVED from the rows rather than trusted.
    pair = FROZEN["board_b2"]["phase_s_pair"]
    ps_b2_total = round(sum(m for n, _g, _hz, m, _t in BOARD_MEASUREMENTS_B2
                            if n.startswith("phase-s-")), 3)
    if ps_b2_total != pair["b2_seconds"]:
        fail.append("board_b2 phase_s total: rows sum to {} != frozen {}".format(
            ps_b2_total, pair["b2_seconds"]))
    if ps_b2_total != BOARD_PHASE_S_B2_SECONDS:
        fail.append("board_b2 phase_s total: rows sum to {} != module constant "
                    "{}".format(ps_b2_total, BOARD_PHASE_S_B2_SECONDS))
    if round(pair["b1_seconds"] - pair["b2_seconds"], 3) != pair["saving_seconds"]:
        fail.append("board_b2 phase_s saving: {} - {} != frozen {}".format(
            pair["b1_seconds"], pair["b2_seconds"], pair["saving_seconds"]))
    if pair["b1_seconds"] != BOARD_PHASE_S_B1_SECONDS:
        fail.append("board_b2 phase_s B1 total: frozen {} != module constant "
                    "{}".format(pair["b1_seconds"], BOARD_PHASE_S_B1_SECONDS))

    # The session's geometry claims, RECOMPUTED.  "T = 52 is the compiled
    # maximum" and "251,740 B" are the two facts that make the hw suite worth
    # running; neither may sit here as a transcribed number.
    sess = FROZEN["board_b2"]["session"]
    for key, geom in (("envelope_tile_count", (820, 307, 216, 96)),
                      ("max_tile_count",      (820, 307,   4,  4))):
        pw, _ph, tw, _th = geom
        got = -(-(pw - tw + 1) // 16)
        if got != sess[key]:
            fail.append("board_b2 {}: {} tiles from {} != frozen {}".format(
                key, got, geom, sess[key]))
    if 820 * 307 != sess["max_single_transfer_bytes"]:
        fail.append("board_b2 max_single_transfer_bytes: 820*307 = {} != frozen "
                    "{}".format(820 * 307, sess["max_single_transfer_bytes"]))
    if sess["phase_s_passed"] != sess["phase_s_total"] or             sess["hw_passed"] != sess["hw_total"]:
        fail.append("board_b2 session: the gate is only evidence if both suites "
                    "were clean; frozen {}/{} and {}/{}".format(
                        sess["phase_s_passed"], sess["phase_s_total"],
                        sess["hw_passed"], sess["hw_total"]))

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
    b0b_fail, b0b_n = b0b_evidence_crosscheck()
    fail.extend(b0b_fail)
    results["crosschecks_run"] = log_n + rpt_n + b1_n + b2_n + b0b_n
    # Name the sources that were NOT available.  A smaller count is easy to
    # miss; "board transcripts: absent" is not.  Neither is a failure -- the
    # frozen literals still stand alone -- but a clone that checked two values
    # must not read as one that checked six.
    results["crosscheck_sources"] = {
        "P2 board transcripts (logs/board_125mhz_gate/)":
            "read" if log_n else "absent",
        "P2 routed report (post_route_wns.txt)": "read" if rpt_n else "absent",
        "B1 routed + board (logs/b1_*/)": "read" if b1_n else "absent",
        # B2 now has BOTH, and its board coverage exceeds B1's: two suites
        # rather than one, reaching T = 52 instead of only T = 6.
        "B2 routed + board, 2 suites (logs/b2_*/)":
            "read" if b2_n else "absent",
        # B0b has a routed report and NO board transcript, and its routed run
        # VIOLATED the constraint.  The crosscheck re-reads the failure for the
        # same reason the others re-read a pass: a negative result that nobody
        # re-reads is the kind that decays into "we routed it".
        "B0b routed, DOES NOT CLOSE (logs/b0b_20260820/)":
            "read" if b0b_n else "absent",
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

    # B0b: the EXACT integer aggregate first, floats derived from it.  Same
    # rule as B1's and B2's -- TOL on a 36-page average spans 2.25 million
    # cycles, more than several of the terms this file records.
    b0b = FROZEN["b0b"]
    agg = page_cycles(templates, scales, "per_trial", "B0b")
    if agg != b0b["aggregate_cycles"]:
        fail.append("B0b aggregate: {} cycles != frozen {}".format(
            agg, b0b["aggregate_cycles"]))
    derived = agg / PAGES / TARGET_CLOCK_HZ
    if abs(derived - b0b["s_per_page"]) > 1e-9:
        fail.append("B0b s/page: {:.12f} != frozen {}".format(
            derived, b0b["s_per_page"]))
    if abs(results["s_page"]["B0b"]
           - FROZEN["s_per_page_at_125mhz"]["B0b"]) > TOL:
        fail.append("B0b: {:.4f} s/page != frozen {}".format(
            results["s_page"]["B0b"], FROZEN["s_per_page_at_125mhz"]["B0b"]))

    # THE WITHDRAWN ENDPOINTS DID NOT BRACKET THE ANSWER, and that is asserted
    # rather than merely written down.  A future revision that "restores the
    # range" would be reintroducing exactly the failure mode this file already
    # records once: a range check cannot separate a correct implementation
    # from a wrong one that lands inside it.  Here the measurement lands
    # OUTSIDE, below both, so the range was not even conservative.
    if not (derived < b0b["withdrawn_at_1_cyc"] < b0b["withdrawn_at_3_cyc"]):
        fail.append("B0b: the measured {:.12f} is no longer below both "
                    "withdrawn endpoints {} / {} -- if that changed, the "
                    "narrative in this file is stale".format(
                        derived, b0b["withdrawn_at_1_cyc"],
                        b0b["withdrawn_at_3_cyc"]))

    # The measured B0b term, re-derived from its two measured halves rather
    # than from cycles() -- so this is a second path to the same number, not a
    # restatement of the first.
    for pw, ph, tw, th in ((311, 159, 216, 96), (88, 39, 24, 16), (4, 4, 4, 4)):
        rw, rh = pw - tw + 1, ph - th + 1
        S = th + 2 * (rh - 1)
        want = (cycles(pw, ph, tw, th, "B2")
                - (rh * th * (tw + rw + b0b["d2_W"])
                   + b0b["d2_c_per_output_row"] * rh)
                + S * (pw + b0b["d1_k_per_scan"]) + b0b["d1_m_per_call"])
        got = cycles(pw, ph, tw, th, "B0b")
        if got != want:
            fail.append("B0b term at {}: cycles()={} != halves={}".format(
                (pw, ph, tw, th), got, want))
        # THE SPLIT IS NOT IDENTIFIED, AND THIS IS WHERE THAT IS PROVED RATHER
        # THAN DESCRIBED.  Moving the shadow's comparator out of D1 and into D2
        # -- 2*rh, where csynth puts it -- gives a different pair of component
        # laws and the SAME net.  A reader who prefers one pair is choosing a
        # functional form, not reading a measurement.
        meas = b0b_delta(pw, ph, tw, th, decontaminated=False)
        deco = b0b_delta(pw, ph, tw, th, decontaminated=True)
        if meas != deco:
            fail.append("B0b split at {}: measured pair gives {} but the "
                        "decontaminated pair gives {} -- if these ever differ, "
                        "the two are no longer the same law and the docstring "
                        "is wrong".format((pw, ph, tw, th), meas, deco))
        if cycles(pw, ph, tw, th, "B2") + meas != got:
            fail.append("B0b delta at {}: B2 + b0b_delta() != cycles(B0b)"
                        .format((pw, ph, tw, th)))
    if cycles(*PHASE_S_GEOMETRY, "B0b") != b0b["phase_s_max_cycles"]:
        fail.append("B0b phase-s max: {} != frozen {}".format(
            cycles(*PHASE_S_GEOMETRY, "B0b"), b0b["phase_s_max_cycles"]))
    if (cycles(*PHASE_S_GEOMETRY, "B0b") - cycles(*PHASE_S_GEOMETRY, "B2")
            != b0b["phase_s_max_delta_cycles"]):
        fail.append("B0b phase-s max delta != frozen "
                    + str(b0b["phase_s_max_delta_cycles"]))

    # B0b LOSES at rh == 1, by exactly 5*th + 2.  Derived, and asserted rather
    # than described: a single output row has nothing to reuse vertically, and
    # the hoisted pass still pays its per-scan overhead.  Anyone quoting B0b as
    # a uniform improvement is wrong, and this is where that gets caught.
    for tw, th in ((4, 4), (216, 96), (16, 12)):
        pw, ph = tw + 7, th          # rh == 1 by construction
        d = cycles(pw, ph, tw, th, "B0b") - cycles(pw, ph, tw, th, "B2")
        if d != 5 * th + 2:
            fail.append("B0b at rh=1, tw={} th={}: delta {} != 5*th+2 = {}"
                        .format(tw, th, d, 5 * th + 2))

    # The per-row sub-terms must still account for the whole fitted term.  What
    # is NO LONGER asserted is that the non-statistics part is 2*tw + 2*rw + 12
    # -- the measurement says it is 2*tw + 2*rw + 9, and WHICH of the three
    # unmeasured shares was over-attributed is not established.  The dictionary
    # carries an explicit `unattributed_correction` of -3 so the sum still
    # closes without pretending the -3 has been localised.
    for tw, rw in ((216, 96), (4, 1), (109, 512), (16, 49)):
        parts = {k: f(tw, rw) for k, f in PER_ROW_TERMS.items()}
        if sum(parts.values()) != 3 * tw + 3 * rw + 33:
            fail.append("per-row attribution at tw={} rw={}: parts sum to {} != "
                        "3*tw+3*rw+33 = {}".format(
                            tw, rw, sum(parts.values()), 3 * tw + 3 * rw + 33))
        survives = sum(v for k, v in parts.items() if k != "window_statistics")
        if survives != 2 * tw + 2 * rw + 9:
            fail.append("post-B0b survivor at tw={} rw={}: {} != 2*tw+2*rw+9 = "
                        "{} (this is MEASURED now, not attributed)".format(
                            tw, rw, survives, 2 * tw + 2 * rw + 9))
        if parts["window_statistics"] != tw + rw + FROZEN["b0b"]["d2_W"]:
            fail.append("window_statistics at tw={} rw={} is not the measured "
                        "tw+rw+{}".format(tw, rw, FROZEN["b0b"]["d2_W"]))

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

    # The decomposition, priced over the whole corpus and re-derived from the
    # MEASURED halves.  Each half is a separate page_cycles_expr, so the three
    # numbers are computed independently and their identity is a check rather
    # than a restatement.
    stats = results["b0b_stats_removed"]
    hoisted = results["b0b_pass_added"]
    comparator = results["b0b_comparator"]
    lhs = results["s_page"]["B2"] - stats + hoisted
    if abs(lhs - results["s_page"]["B0b"]) > 1e-9:
        fail.append("B0b decomposition: B2 - D2 + D1 = {:.12f} != B0b "
                    "{:.12f}".format(lhs, results["s_page"]["B0b"]))

    # THE TWO COMPONENT FIGURES ARE NOW ASSERTED, NOT NARRATED.  They stood in
    # this file's prose as 3.088545 and 0.409416, each 0.239655641778 too
    # large.  The offset was the SAME in both, so the net came out exact and no
    # check here could see it -- the decomposition test above passes for any
    # pair of numbers that differ by the right amount.  Pinning each one
    # separately is the only arrangement that catches a shared offset.
    for key, got in (("d2_s_per_page", stats), ("d1_s_per_page", hoisted),
                     ("comparator_s_per_page", comparator)):
        want = FROZEN["b0b"][key]
        if abs(got - want) > 1e-9:
            fail.append("B0b {}: {:.12f} != frozen {}".format(key, got, want))
    for wrong in FROZEN["b0b"]["withdrawn_component_s_per_page"]:
        if abs(stats - wrong) < 1e-6 or abs(hoisted - wrong) < 1e-6:
            fail.append("B0b components: {} is one of the WITHDRAWN prose "
                        "figures -- restoring it would restore the shared "
                        "offset that a correct net total conceals".format(wrong))
    # (stats/hoisted/comparator come from evaluate(), so the numbers asserted
    # above are the same objects that were printed and serialised.)

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
    sess = FROZEN["board_b2"]["session"]
    pair = FROZEN["board_b2"]["phase_s_pair"]
    print("  B2 board session   the B2 image itself, {} MHz gated, {}:".format(
        sess["measured_fclk0_mhz"], sess["hwh_vlnv"]))
    for name, geom, hz, meas, note in BOARD_MEASUREMENTS_B2:
        pw, ph, tw, th = geom
        model_s = cycles(*geom, "B2") / hz
        print("    {:<22} {:>3}x{:<3}/{:>3}x{:<2} T={:<2}  model {:8.4f}s  "
              "meas {:6.3f}s  {:+.1f} ms".format(
                  name, pw, ph, tw, th, -(-(pw - tw + 1) // 16),
                  model_s, meas, (meas - model_s) * 1e3))
    print("    residuals are all positive and trendless -- CONSISTENT WITH fixed")
    print("    per-invocation overhead, though nothing here isolates its cause.")
    print("    phase_s totals, same stimulus and clock (runner differs outside the")
    print("    timed path): B1 {:.3f}s -> B2 {:.3f}s ({:.3f}s saved)".format(
        pair["b1_seconds"], pair["b2_seconds"], pair["saving_seconds"]))
    print("    tile counts SAMPLED by the 16 cases: {} -- vs T=6 alone in the"
          .format(list(sess["tile_counts_sampled"])))
    print("    cosim and in B1's session.  Six values, not a sweep of 1..{}."
          .format(sess["max_tile_count"]))
    print()
    print("PROJECTION - initial trials only, s/page @ {:g} MHz".format(TARGET_CLOCK_HZ / 1e6))
    print("-" * 78)
    print("  The CLOCK is board-demonstrated (above), and so is B1's ARCHITECTURE.")
    print("  What is NOT demonstrated is any PAGE, for any variant:")
    print("  Phase S is a driver change with no RTL.")
    print("  B0b IS implemented: both of its terms cosim-measured against a b2ctl")
    print("  control and a shadow build, 14/14 exact, correctness checked at")
    print("  2,911,495 result positions.  BUT ITS ROUTED RUN DOES NOT CLOSE")
    print("  8.000 ns -- WNS -0.051470 ns at the same default flow that gave B1")
    print("  +0.135 and B2 +0.012 -- so its 125 MHz column is a conversion at a")
    print("  clock this core has no closing build for.  Not on silicon.")
    print("  B2 IS implemented: cycle term cosim-measured against B1, 14/14 exact,")
    print("  and RUN ON THE BOARD at its OWN observed 125.0000 MHz -- phase_s 7/7 and")
    print("  hw 9/9, reaching T = 52 where the cosim reached only T = 6.")
    print("  B1 IS implemented: cycle term cosim-measured, routed at 8.000 ns, and RUN")
    print("  ON THE BOARD at its OWN observed 125.0000 MHz -- phase_s only, so T = 6.")
    print("  Neither borrows the unmodified core's clock.  Every s/page below is still")
    print("  a projection: NO PAGE HAS BEEN RUN, for any variant, at any clock.")
    labels = [
        ("pl_full_context", "TODAY'S PL: side-common full context", "deployed; 622x300 / 622x224"),
        ("current_core", "CPU per-base context patch policy", "silicon-anchored formula"),
        ("shared_roi_int_quirk", "Phase S, per-base ROI, int() quirk", "historical figure only"),
        ("shared_roi", "Phase S, per-base ROI, rounded", "no RTL; corrected"),
        ("side_common_roi", "Phase S, one ROI per side", "no RTL; side-common reading"),
        ("per_trial_roi", "Phase S, per-trial ROI", "no RTL; needs driver change"),
        ("B1", "  + B1 runtime segment width", "cosim-validated TERM; page projected"),
        ("B2", "  + B2 overlap reuse", "cosim+board TERM to T=52; page projected"),
        ("B0b", "  + B0b hoisted window statistics", "cosim-measured TERM; DOES NOT CLOSE 8 ns"),
    ]
    for key, name, note in labels:
        v = r["s_page"][key]
        print("  {:<38} {:8.3f}   {:8.1f} @31.25MHz   {}".format(
            name, v, v * (TARGET_CLOCK_HZ / BOARD_CLOCK_HZ), note))
    print()
    # PRINTED BECAUSE THEY WERE ONCE ONLY NARRATED.  check() computes these
    # three and asserts them; the prose in this file carried two DIFFERENT
    # numbers for months because nothing put the computed ones in front of a
    # reader.  A derived figure that is never shown is a figure nobody checks.
    if "b0b_stats_removed" in r:
        print("  B0b's two co-simulation differences, over the same corpus:")
        print("    B2                                  {:.12f}".format(
            r["s_page"]["B2"]))
        print("    - D2  (b0b - shadow)                {:.12f}".format(
            r["b0b_stats_removed"]))
        print("    + D1  (shadow - b2ctl)              {:.12f}".format(
            r["b0b_pass_added"]))
        print("    = B0b                               {:.12f}".format(
            r["s_page"]["B0b"]))
        print("    of which the shadow's comparator    {:.12f}  (in BOTH "
              "lines, cancels)".format(r["b0b_comparator"]))
        print("    Neither difference is a component cost: D1 is the pass plus")
        print("    2*rh of comparator and D2 is the removal minus the same.")
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
              "B0b EXACT aggregate + the two corpus components + the "
              "unidentified split + the rh=1 regression + the withdrawn "
              "endpoints not bracketing it".format(
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
