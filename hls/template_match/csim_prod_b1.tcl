# Run the PRODUCTION-geometry suite in C simulation against the B1 source.
#
#   cd hls/template_match
#   vitis-run.bat --mode hls --tcl csim_prod_b1.tcl
#
# WHY SEPARATELY.  The B1 suite's lane-15 pair is built at 24x16 in an 88x39
# patch, which is what RTL co-simulation can afford.  The production suite runs
# the SAME construction at 24x16/200x60 and at the full left-bank 164x94 in
# 622x300 — the widest template a lane-15 pair can be built at under
# LANE15_ALPHA — plus the exact/near ties, the negative score, the flat region
# and the 817x304 maximum result map.  None of that fits in xsim, but all of it
# runs in C, and C is where a wrong seg_len shows up just as plainly: the
# mutation is an indexing defect, not a timing one.
#
# ITS OWN RESET PROJECT.  This used to open `template_match_b1` and
# `add_files correlation_core.cpp` — the WORKING-TREE core — into the same
# project run_hls_b1.tcl builds.  Two defects in one line: `add_files`
# accumulates, so after the A/B script moved to pinned snapshots this would
# have put two cores in the project; and the source it added was not the one
# the co-simulation measured, so a pass here said nothing about the RTL under
# test.  It now builds `template_match_b1_prod` from the SAME pinned b1
# snapshot, verified by digest, and touches no other project.  It is isolated,
# not hermetic: tme_top.cpp/.h and tme_tb.cpp are compiled live and are only
# manifest-bound after the run.
#
# Generate the vectors first if they are absent:
#   python tme_generate_production.py            (writes the prod package)

set project_name "template_match_b1_prod"
set part_number  "xc7z020clg400-1"

# The b1 snapshot and its digest, duplicated from run_hls_b1.tcl deliberately:
# two independent records of the same number is the point of a pinned source.
set snap_file "correlation_core.b1.cpp"
set snap_sha  "e33fe219af77a0e4b79c225a0b7b60f8a3181a186012e87b8f08d303f54c4a51"

set snap [file join b1_sources $snap_file]
if {![file exists $snap]} { error "pinned source $snap is missing" }
set got [string tolower [lindex [split [exec sha256sum $snap]] 0]]
if {$got ne $snap_sha} {
    error "b1_sources/$snap_file has SHA-256 $got, pinned $snap_sha.\
           This suite is only evidence about the co-simulated RTL if it\
           compiles the same bytes that RTL came from."
}
puts "csim_prod_b1.tcl: $snap — sha256 $got verified"

# THE STIMULUS, TOO.  Verifying the source and not the vectors is half a
# verification: this suite's whole claim is "the B1 core reproduces the
# production goldens", and that is a statement about a specific 1.6 MB of
# pixels.  The blobs are gitignored and regenerate from pinned seeds, so the
# thing that says they are the right pixels is tb_tme_prod.sha256 -- which is
# committed precisely so a regeneration can be checked rather than trusted.
#
# All FOUR entries are checked, including tb_tme_counts_prod.txt.  That file is
# not read by the testbench, but it is the 0.3 acceptance sidecar for these
# exact cases; if it has drifted from the blobs then the package is internally
# inconsistent and this run should not be quoted as evidence for either.
set rec "tb_tme_prod.sha256"
if {![file exists $rec]} {
    error "$rec is missing. Regenerate the production package with           `python tme_generate_production.py` -- the blobs are gitignored, so           the digest record is the only thing that says which pixels these are."
}
set fh [open $rec r]
set rec_lines [split [string trim [read $fh]] "\n"]
close $fh
if {[llength $rec_lines] != 4} {
    error "$rec lists [llength $rec_lines] files, expected 4           (cases/patches/templs/counts)."
}
set n_ok 0
foreach line $rec_lines {
    set want [lindex $line 0]
    set name [lindex $line end]
    if {![file exists $name]} {
        error "$name is listed in $rec but absent. Run               `python tme_generate_production.py` to regenerate the package."
    }
    set have [string tolower [lindex [split [exec sha256sum $name]] 0]]
    if {$have ne $want} {
        error "$name has SHA-256 $have, pinned $want in $rec.               A production suite built from different pixels is not evidence               about the goldens this core is being held to."
    }
    puts "csim_prod_b1.tcl: $name — sha256 verified"
    incr n_ok
}
puts "csim_prod_b1.tcl: all $n_ok production inputs verified against $rec"

open_project -reset $project_name
set_top tme_top

add_files tme_top.cpp
add_files tme_top.h
# Same include-path fix as run_hls_b1.tcl: the snapshot sits one level below
# tme_top.h and csim compiles from inside the solution tree.
add_files $snap -cflags "-I[pwd]"

add_files -tb tme_tb.cpp
add_files -tb [glob -nocomplain tb_tme_*.bin tb_tme_*.txt]

open_solution -reset "prod"
set_part $part_number
create_clock -period 5ns -name default

csim_design -argv "prod"

close_project
