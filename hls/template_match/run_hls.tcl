# Vitis HLS 2025.2 project script for template_match_core
#
# Run from the Vitis HLS Tcl console, or from the shell:
#   vitis-run.bat --mode hls --tcl run_hls.tcl
#
# Generate the golden suites first (hls/.venv python):
#   python tme_generate_golden.py

set project_name "template_match"
set part_number  "xc7z020clg400-1"   ;# Arty Z7-020

open_project -reset $project_name
set_top tme_top

add_files tme_top.cpp
add_files tme_top.h
add_files correlation_core.cpp

add_files -tb tme_tb.cpp
add_files -tb [glob -nocomplain tb_tme_*.bin tb_tme_*.txt]

open_solution -reset "solution1"
set_part $part_number
create_clock -period 5ns -name default   ;# 200 MHz — see the timing note below

# C simulation: full manifest suite, score AND exact-location asserts.
csim_design

# The same vectors sw/tme_standalone_bringup.py sends to the board: the cosim
# five plus the 820x307 stress-max-envelope case.  Running them here is cheap
# insurance — it means a board failure is a hardware finding rather than a bad
# golden, and it is the ONLY pre-silicon check the 251,740-byte §3.1 case gets.
# (It deliberately does not go to cosim: ~190M cycles is hours of xsim, and an
# RTL simulation contains no DMA, so it cannot test a DMA length bound anyway.)
csim_design -argv "hw"

# Synthesize to RTL and check resource/timing estimates
csynth_design

# C/RTL co-simulation.  The -argv "cosim" is required: it switches the
# testbench to the small cosim manifest — anything only in the csim manifest
# never reaches RTL.
cosim_design -rtl verilog -argv "cosim"

# ---------------------------------------------------------------------------
# NO export_design HERE — ON PURPOSE.
#
# This script used to end with export_design -version "1.0", packaging an
# IP-catalog entry labelled as a release on every verification run.  Of the
# three things that were wrong with that, one is now fixed and two remain:
#
#   1. TIMING — RESOLVED as a gate (2026-08-04).  The standalone image routes
#      with post-route WNS +3.537 ns against the 20 ns period it is actually
#      constrained at (contract §8), all constraints met.  HLS still estimates
#      6.547 ns against the 5.000 ns target below, and that stays true — but
#      5 ns is a 200 MHz ambition nothing here requires.  KEEP the 5 ns
#      constraint: the headroom comes from synthesising tight and clocking
#      slow, and re-targeting HLS to the board period would let the scheduler
#      grow the critical path to refill it.  Re-read the estimate off the
#      current csynth report before citing it.  Still open: how high the clock
#      can go — see package_provisional.tcl item 1.
#   2. VERIFICATION — RESOLVED.  tme_tb.cpp is manifest-driven and asserts
#      score AND exact location per case; the suite covers a unique nonzero
#      match, the final result row/column (§4.4), patch==template equality,
#      and the 820x307/216x96 maximum-storage case at near-maximum window
#      energies.  (The rewrite this validated also replaced arithmetic that
#      wrapped at real magnitudes — the old ap_fixed accumulators and Q16.16
#      normalisation only ever passed csim because the sole golden was an
#      all-zero patch.)  A third suite, selected by -argv "hw" above, carries
#      the cosim cases plus both 820x307 stress cases to silicon; they are the
#      only tests of contract §3.1's 251,740-byte single DMA transfer and of
#      the maximum 817x304 result map, and running them here rules out a bad
#      golden before the board run.
#   3. INTEGRATION — still open, and now the ONLY thing holding 1.0.  The matcher still takes scalar
#      patch_w/patch_h; it does not consume the extractor's per-patch pixel
#      TLAST or the §6.2 metadata geometry.  Workable for bring-up under
#      §7.1 PS sequencing (the PS reads metadata and writes the scalars per
#      candidate), but not the contract's end state.
#
# Packaging stays opt-in and honestly labelled: package_provisional.tcl
# exports 0.x with the caveats in its description.  Restore export here only
# when 3 is actually resolved.
# ---------------------------------------------------------------------------

close_project
