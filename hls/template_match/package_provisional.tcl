# Opt-in provisional packaging for tme_top (template_match_core).
#   cd hls/template_match
#   vitis-run.bat --mode hls --tcl package_provisional.tcl
#
# THIS IS NOT A RELEASE.  It exists so Vivado implementation can proceed —
# which is the only way to get real timing numbers for this core — without the
# default verification flow (run_hls.tcl) silently publishing a "1.0" IP.
# That purpose is now served: the implementation happened and the numbers are
# below.  Of the three blockers the original banner listed, TWO are resolved;
# version stays 0.x until item 3 is:
#
#   1. TIMING — RESOLVED as a gate (2026-08-04), by measurement rather than
#      argument.  The standalone image built from this IP
#      (vivado/tme_standalone/build_tme_standalone.tcl) routes with
#      post-route WNS +3.537 ns against the 20 ns period it is actually
#      constrained at — all constraints met, fully routed, 0 routing errors,
#      16.463 ns of that launch-to-capture budget consumed (setup, uncertainty
#      and skew included; the worst path's own data delay is 16.332 ns),
#      ≈+15.5 ns at the board's 32 ns.
#
#      HLS still estimates 6.547 ns against the 5.000 ns target, and that is
#      still true — but 5 ns is a 200 MHz ambition nothing in this pipeline
#      has ever required, and it was never the number that decided whether
#      the design works.  KEEP the 5 ns constraint anyway: the headroom comes
#      from synthesising tight and clocking slow, and re-targeting HLS to the
#      board period would let the scheduler grow the path to refill it.
#
#      What is NOT settled is how high the clock can go.  The 16.332 ns data
#      delay is not a floor — >93% of it is routing at 3 logic levels, in a
#      build that met its constraint with room to spare and so stopped
#      optimising.  The
#      binding path is correlation_core's fully-partitioned seg[] register
#      file, not the arithmetic.  Re-implement at the target period before
#      quoting any maximum frequency.
#
#   2. TESTBENCH — RESOLVED (2026-08-04).  tme_tb.cpp is manifest-driven and
#      asserts score AND exact best-match location per case; the suite covers
#      a unique nonzero match, the final result row/column (§4.4), both
#      equality axes, negative scores, flat windows, and the 820x307/216x96
#      maximum-storage case at near-maximum window energies.  csim 23/23,
#      cosim 7/7 (RTL), and `csim -argv hw` 9/9, plus the §4.6 direct DUT
#      tests and width witnesses ahead of the manifest loop in every suite.
#      SILICON, 2026-08-07: the same 9 vectors now pass on hardware, 9/9,
#      score and exact location per case, with the 251,740 B §3.1 transfer
#      moved in one go, the 817x304 result map's final cell hit, and a clean
#      re-invocation after the largest case.  The old warning here said the
#      `hw` suite was a C SIMULATION and that one must never write "hw 9/9" —
#      that rule existed because there was no hardware result to confuse it
#      with, and it is retired now that there is one.  Distinguish them by
#      name: "csim -argv hw" for the simulation, "silicon" for the board.
#      (The exported -description string below is frozen at
#      the numbers of the packaged 0.x IP and is deliberately NOT updated.)
#      The rewrite this validated also
#      replaced arithmetic that wrapped at real magnitudes and streams the
#      template as RAW uint8 (the old int8+128 encoding wrapped for binary
#      templates).
#
#      The third suite is new: `tb_tme_cases_hw.txt` is what
#      sw/tme_standalone_bringup.py sends to the board — the cosim cases plus
#      two 820x307 stress cases: stress-max-envelope, whose 251,740-byte patch
#      is the only thing exercising contract §3.1's single-DMA-transfer bound,
#      and stress-max-result, the only case filling the 817x304 result map
#      MAX_RESULT_W/H are sized for.  Neither bound is testable in csim or
#      cosim; running the suite through csim only rules out a bad golden.
#
#   3. FRAMING AND GEOMETRY INTEGRATION — OPEN.  tme_top still takes scalar
#      patch_w/patch_h.  It does not consume patch_extract_core's per-patch
#      pixel TLAST or the §6.2 metadata geometry.  Workable for bring-up under
#      §7.1 PS sequencing (the PS reads metadata, writes the scalars per
#      candidate — the return port now lives in the single CTRL bundle, so the
#      PS can actually sequence it), but not the contract's end state.
#
# What IS settled: the core fits the part, and now with measured numbers
# rather than estimated ones.  MAX_PATCH is the exact 820x307 envelope
# (contract §3).  HLS reports 224 BRAM18K of 280 (80%), 33 DSP (15%), 18.2k FF
# (17%), 34.6k LUT (64%) for the core alone; the routed standalone image —
# core PLUS both DMAs, both SmartConnects and the PS — measures 115 BRAM tiles
# (82%), 34 DSP (15%), 18.1k FF (17%) and 14.7k LUT (28%).  Note the last
# pair: HLS over-estimated LUTs by ~2.4x.  BRAM is the resource that is
# genuinely tight, exactly as §3 says.
#
# Bump the minor field for a distinct provisional revision; do NOT renumber to
# 1.0 until item 3 above is done.
#
# NOTE ON THE DESCRIPTION BELOW: it is the text baked into the 0.2 zip that
# the current bitstream was built from, and it is left as-is deliberately so
# the string and the shipped artifact agree.  Its "DOES NOT MEET 5 ns TIMING"
# remains literally true of the HLS estimate; item 1 above is what to read for
# what that does and does not mean.  Refresh it when the version is bumped.

set project_name "template_match_provisional"
set part_number  "xc7z020clg400-1"

open_project -reset $project_name
set_top tme_top

add_files tme_top.cpp
add_files tme_top.h
add_files correlation_core.cpp

open_solution -reset "solution1"
set_part $part_number
create_clock -period 5ns -name default

csynth_design

export_design -format ip_catalog \
              -description "PROVISIONAL - Terminal Matching Engine (TM_CCOEFF_NORMED, exact integer sums + float sqrt; template streams RAW uint8). Verified: csim 21/21 and RTL cosim 5/5 with score AND exact-location asserts. Fits the part (224/280 BRAM18K, 33 DSP) but DOES NOT MEET 5 ns TIMING: HLS estimate 6.547 ns - fine at the 31.25 MHz bring-up clock, gates raising the clock only. Per-patch framing and transmitted geometry from patch_extract_core are not consumed yet. Not a release IP - do not use in a production bitstream." \
              -vendor "TermCount" \
              -version "0.2"

close_project
