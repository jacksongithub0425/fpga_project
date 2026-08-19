# Priority 4 (B1) A/B build for template_match_core — Vitis HLS 2025.2
#
#   TME_SOLUTION=cur vitis-run.bat --mode hls --tcl run_hls_b1.tcl
#   TME_SOLUTION=b1  vitis-run.bat --mode hls --tcl run_hls_b1.tcl
#   TME_SOLUTION=b1b vitis-run.bat --mode hls --tcl run_hls_b1.tcl
#
# ONE PROJECT PER VARIANT.  This script used to put all three solutions in a
# single `template_match_b1` project, and that was not safe to re-run.
# `open_project` without -reset REOPENS the project on disk, and `add_files`
# ACCUMULATES into hls.app rather than replacing.  The retained hls.app still
# named the working-tree `correlation_core.cpp`, because that is what an
# earlier version of this script added; re-running the pinned-snapshot version
# on top of it would have left BOTH files in the project.  -reset on the shared
# project was not an option either, since it deletes the sibling solutions'
# reports — the only copies of the other half of a paired measurement.
#
# So each variant now owns `template_match_b1_<variant>` and opens it with
# -reset.  A run cannot inherit anything from a previous one, of its own
# variant or any other, and no run can destroy another variant's evidence.
# The cost is three project trees instead of one; the benefit is that "re-run
# all three" is a safe, ordinary thing to do.
#
# WHY THE SOURCE IS PINNED PER SOLUTION.  B1's claim is a DIFFERENCE, so the
# evidence is a paired measurement — and a paired measurement is worthless if
# the pair cannot be reproduced.  An earlier version of this script added the
# working-tree `correlation_core.cpp` for every solution, which meant that once
# B1 was applied, re-running `TME_SOLUTION=cur` would have rebuilt the CONTROL
# out of the B1 source and quietly reported a zero difference.  The control was
# only ever correct because it happened to be run first.
#
# Each solution compiles an IMMUTABLE SNAPSHOT from b1_sources/, verified by
# SHA-256 before it is added:
#
#   cur   correlation_core.cur.cpp   the unmodified core, == git eb1c8ac
#   b1    correlation_core.b1.cpp    runtime seg_len, `if (i >= seg_len) break`
#   b1b   correlation_core.b1b.cpp   hoisted clamped bound, no exit test
#
# The snapshots are the record; `correlation_core.cpp` in the working tree is
# the SHIPPED file and is deliberately NOT read here.  b1_sources/README.md
# records how each was produced and which of them a given transaction report
# came from.
#
# The `template_match/solution1` project is never opened by this script.  It
# holds the result.transaction.rpt that sw/tme_cycle_model.py validates the
# `cur` cycle formula against — nine transactions reproduced exactly — and
# re-running cosim there with modified RTL would overwrite the only artifact
# anchoring the model to silicon.
#
# Generate the vectors first, with the hls/.venv python:
#   python tme_generate_production.py --suite b1

set part_number "xc7z020clg400-1"   ;# Arty Z7-020

set solution "cur"
if {[info exists ::env(TME_SOLUTION)]} { set solution $::env(TME_SOLUTION) }

# The pinned snapshot and its expected digest.  A solution name with no entry
# here is rejected rather than silently falling back to the working tree.
array set snapshot {
    cur {correlation_core.cur.cpp  9ca36c4733a93302121a6e0aceeb57085d41047d6b84c05c3f9bc8aacb699d2b}
    b1  {correlation_core.b1.cpp   e33fe219af77a0e4b79c225a0b7b60f8a3181a186012e87b8f08d303f54c4a51}
    b1b {correlation_core.b1b.cpp  17e3b1ec8169c61d8e91c6ed005cb8e53e5afc7d4bd590ec32dcc254f9519242}
}
if {![info exists snapshot($solution)]} {
    error "TME_SOLUTION='$solution' has no pinned source in b1_sources/.\
           Known: [lsort [array names snapshot]].  Add a snapshot and its\
           SHA-256 here before building a new variant — an unpinned solution\
           cannot be reproduced and its transaction report proves nothing."
}

proc sha256_of {path} {
    return [string tolower [lindex [split [exec sha256sum $path]] 0]]
}

lassign $snapshot($solution) snap_file snap_sha
set snap [file join b1_sources $snap_file]
if {![file exists $snap]} {
    error "pinned source $snap is missing"
}
set got [sha256_of $snap]
if {$got ne $snap_sha} {
    error "b1_sources/$snap_file has SHA-256 $got, pinned $snap_sha.\
           The snapshots are immutable evidence: edit correlation_core.cpp in\
           the working tree instead, and add a NEW snapshot if a new variant\
           needs measuring."
}

# The vectors are half of the measurement.  A paired report only means what it
# says if both runs saw the same stimulus, so the b1 suite is verified here for
# the same reason the source is — and a regenerated suite has to be re-pinned
# deliberately rather than drifting in under the same file names.
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
    puts "run_hls_b1.tcl: vector $name — verified"
}

set project_name "template_match_b1_$solution"
puts "run_hls_b1.tcl: solution '$solution' from $snap"
puts "run_hls_b1.tcl: sha256 $got — verified"
puts "run_hls_b1.tcl: project $project_name (hermetic, -reset)"

# -reset is safe HERE and only here: this project holds exactly one variant, so
# the only thing it can delete is the previous run of the same variant.
open_project -reset $project_name
set_top tme_top

add_files tme_top.cpp
add_files tme_top.h
# The snapshot lives in b1_sources/, one level below tme_top.h, and csim
# compiles from inside the solution tree — so `#include "tme_top.h"` does not
# resolve on its own.  Point the include path at this directory rather than
# staging a copy next to the header: a copy would be a second file that has to
# be kept identical to the snapshot, and the snapshot is the evidence.
add_files $snap -cflags "-I[pwd]"

add_files -tb tme_tb.cpp
add_files -tb [glob -nocomplain tb_tme_*.bin tb_tme_*.txt]

open_solution -reset $solution
set_part $part_number
# 5 ns, the same constraint the frozen build used.  Not a claim that 200 MHz
# closes — see run_hls.tcl's note.  The point here is that every solution is
# scheduled against an IDENTICAL target, so a difference between them is the
# RTL change and not a re-targeted scheduler.
create_clock -period 5ns -name default

# The banking-boundary suite: result widths 1, 15, 16, 17, 31, 32, 33, 95, 96,
# two exact ties, and the lane-15 pair that detects the seg_len-1 off-by-one.
csim_design -argv "b1"

# The production-geometry suite carries build_lane15 at 200x60 AND at the full
# 622x300 left-bank shape, which is the widest template a lane-15 pair can be
# built at.  Slow in csim (~1.5e9 MACs per case), so it is opt-in.
if {[info exists ::env(TME_B1_PROD)] && $::env(TME_B1_PROD) == "1"} {
    csim_design -argv "prod"
}

csynth_design

cosim_design -rtl verilog -argv "b1"

close_project
