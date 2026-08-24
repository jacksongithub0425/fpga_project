# Priority 6 (B0b) A/B build for template_match_core — Vitis HLS 2025.2
#
#   TME_B0B_SOLUTION=b2ctl  vitis-run.bat --mode hls --tcl run_hls_b0b.tcl
#   TME_B0B_SOLUTION=shadow vitis-run.bat --mode hls --tcl run_hls_b0b.tcl
#   TME_B0B_SOLUTION=b0b    vitis-run.bat --mode hls --tcl run_hls_b0b.tcl
#
# WHY THIS IS A SEPARATE SCRIPT FROM run_hls_b1.tcl.  That script varies
# `correlation_core.cpp` and treats `tme_top.cpp` as a live input.  B0b varies
# `tme_top.cpp` — the window statistics live there — so the two files swap
# roles: here `tme_top.<variant>.cpp` is the pinned variable and
# `correlation_core.b2.cpp` is the pinned constant.  Bolting a second axis
# onto run_hls_b1.tcl would have made every existing TME_SOLUTION value
# ambiguous about which tme_top it compiled, which is precisely the failure
# b1_sources/README.md records having had once already.
#
# THE CONTROL IS `b2ctl` AND IT IS BYTE-EXACT.  b0b_sources/tme_top.b2.cpp is
# a copy of the tme_top.cpp that logs/b2_20260819/MANIFEST.sha256 records as
# B2's build input (bcccd44c…), and correlation_core.b2.cpp is the same
# snapshot run_hls_b1.tcl compiles for TME_SOLUTION=b2.  So the control here
# is the same pair of files B2 was measured from, and it must reproduce B2's
# published term or the pair is not a pair.
#
# THREE SOLUTIONS, TWO DIFFERENCES:
#
#   b2ctl   the shipped core.  Window statistics recomputed for every
#           (output row, template row).
#   shadow  b2ctl PLUS the hoisted, vertically-reused pass, run alongside and
#           compared at every result position.  Nothing removed.
#             shadow - b2ctl  =  what the count pass costs
#   b0b     the hoisted pass ONLY; the repeated statistics loops are gone.
#             b0b - shadow    =  what the repeated loops cost
#
# The second difference matters as much as the first: sw/tme_cycle_model.py
# attributes `tw + rw + 21` per (output row, template row) to the window
# statistics, and that attribution has never been measured — it is one term of
# a split that was only ever checked for summing to the fitted total.  The
# b0b - shadow difference measures it directly.
#
# ONE PROJECT PER VARIANT, each opened with -reset, for the reason
# run_hls_b1.tcl spells out at length: `open_project` without -reset reopens
# what is on disk and `add_files` accumulates into hls.app, so a shared
# project can silently compile two cores; and -reset on a shared project
# deletes the sibling solutions, which are the other half of a paired
# measurement.
#
# Generate the vectors first, with the hls/.venv python:
#   python tme_generate_production.py --suite b1

set part_number "xc7z020clg400-1"   ;# Arty Z7-020

set solution "b2ctl"
if {[info exists ::env(TME_B0B_SOLUTION)]} { set solution $::env(TME_B0B_SOLUTION) }

# The pinned tme_top snapshot per solution.  A solution name with no entry is
# rejected rather than falling back to the working tree: an unpinned variant
# cannot be reproduced and its transaction report proves nothing.
array set snapshot {
    b2ctl  {tme_top.b2.cpp      bcccd44c187bd7c008d8de7bbd87dfa33c1d7bf72c1fbfbae1b8046bb30f59db}
    shadow {tme_top.shadow.cpp  0d598d29bf73019673ce29e2300a95993dae427e71ac464b9712467c9485ef04}
    b0b    {tme_top.b0b.cpp     5e791fe322d153f06e7686684dc89b51b0dd79c268e2e4bea9d92dadc2092102}
}
if {![info exists snapshot($solution)]} {
    error "TME_B0B_SOLUTION='$solution' has no pinned source in b0b_sources/.\
           Known: [lsort [array names snapshot]].  Add a snapshot and its\
           SHA-256 here before building a new variant."
}

# The constant half of the pair.  correlation_core is NOT what B0b changes, so
# it is pinned to the same snapshot B2 was measured from and verified here for
# the same reason the variable half is.
set core_snap "b1_sources/correlation_core.b2.cpp"
set core_sha  "c8c7b0882af33214da5f7bbedca9ff9b985629517ddada37a9eacaacaec5d8ce"
# tme_top.h carries the accumulator widths every one of these terms depends on.
# It was an unrecorded live input to B1's measurement and was pinned only
# afterwards; pin it up front here.
set hdr_file  "tme_top.h"
set hdr_sha   "19b15033530f96ad7ad04b0ef24414428295ca02634faacab04751b541b2178b"

proc sha256_of {path} {
    return [string tolower [lindex [split [exec sha256sum $path]] 0]]
}

proc verify {path want what} {
    if {![file exists $path]} { error "pinned $what $path is missing" }
    set got [sha256_of $path]
    if {$got ne [string tolower $want]} {
        error "$path has SHA-256 $got, pinned [string tolower $want].\
               These snapshots are immutable evidence: edit the working-tree\
               file instead and add a NEW snapshot if a new variant needs\
               measuring."
    }
    puts "run_hls_b0b.tcl: $what $path — verified"
    return $got
}

lassign $snapshot($solution) snap_file snap_sha
set snap [file join b0b_sources $snap_file]
set got [verify $snap $snap_sha "tme_top snapshot"]
verify $core_snap $core_sha "correlation_core snapshot"
verify $hdr_file  $hdr_sha  "header"

# The vectors are half of the measurement.  A paired report only means what it
# says if both runs saw the same stimulus.
set vec_digests tb_tme_b1.sha256
if {![file exists $vec_digests]} {
    error "$vec_digests is missing — regenerate the suite with\
           `python tme_generate_production.py --suite b1`, which writes it."
}
foreach line [split [string trim [read [open $vec_digests r]]] "\n"] {
    if {[string trim $line] eq ""} { continue }
    lassign [regexp -inline {^([0-9a-fA-F]{64})\s+\*?(.+)$} [string trim $line]] _ want name
    if {$want eq ""} { error "unparsable line in $vec_digests: $line" }
    if {![file exists $name]} { error "vector $name is missing" }
    set have [sha256_of $name]
    if {$have ne [string tolower $want]} {
        error "vector $name has SHA-256 $have, pinned [string tolower $want].\
               The transaction reports were measured against the pinned bytes."
    }
    puts "run_hls_b0b.tcl: vector $name — verified"
}

set project_name "template_match_b0b_$solution"
puts "run_hls_b0b.tcl: solution '$solution' from $snap"
puts "run_hls_b0b.tcl: project $project_name (isolated per variant, -reset)"
puts "run_hls_b0b.tcl: tme_top snapshot, correlation_core snapshot, header and vectors were all pre-build digest-gated; tme_tb.cpp is the only live input"

open_project -reset $project_name
set_top tme_top

# Both cores come from a pinned snapshot.  The tme_top snapshot lives one
# directory below tme_top.h and csim compiles from inside the solution tree,
# so `#include "tme_top.h"` needs the include path pointed here rather than a
# staged copy — a copy would be a second file to keep identical to the
# evidence.
add_files $snap      -cflags "-I[pwd]"
add_files $core_snap -cflags "-I[pwd]"
add_files $hdr_file

add_files -tb tme_tb.cpp
add_files -tb [glob -nocomplain tb_tme_*.bin tb_tme_*.txt]

open_solution -reset $solution
set_part $part_number
# 5 ns, the same constraint every previous A/B used.  Not a claim that 200 MHz
# closes — see run_hls.tcl.  The point is that all three solutions are
# scheduled against an IDENTICAL target, so a difference between them is the
# source change and not a re-targeted scheduler.
create_clock -period 5ns -name default

# The banking-boundary suite, which is also the cosim stimulus.
csim_design -argv "b1"

# The broad-geometry csim suites.  These are where the shadow comparison earns
# its keep: `csim` reaches 820x307 with a 4x4 template (rh = 304 vertical
# shifts, the deepest reuse chain in any suite) and 216x96 at the maximum
# envelope, neither of which the RTL cosim can afford.  Opt-in because `prod`
# is ~1.5e9 MACs per case.
if {[info exists ::env(TME_B0B_CSIM_BROAD)] && $::env(TME_B0B_CSIM_BROAD) == "1"} {
    csim_design -argv "csim"
    csim_design -argv "hw"
    csim_design -argv "b0b"
}
if {[info exists ::env(TME_B0B_PROD)] && $::env(TME_B0B_PROD) == "1"} {
    csim_design -argv "prod"
}

# A csim-only smoke path, for iterating on the source before spending a
# csynth + cosim on it.  It produces no evidence: a run under this flag has
# no transaction report and no synthesis numbers, and the evidence manifest
# records the full run.
if {[info exists ::env(TME_B0B_CSIM_ONLY)] && $::env(TME_B0B_CSIM_ONLY) == "1"} {
    puts "run_hls_b0b.tcl: TME_B0B_CSIM_ONLY=1 — stopping before csynth"
    close_project
    exit 0
}

csynth_design

cosim_design -rtl verilog -argv "b1"

close_project
