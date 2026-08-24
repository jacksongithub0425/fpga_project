# Run the PRODUCTION-geometry suite in C simulation against the B2 source.
#
#   cd hls/template_match
#   vitis-run.bat --mode hls --tcl csim_prod_b2.tcl
#
# WHY THIS EXISTS AT ALL.  Priority 5 measured B2 on the `b1` co-simulation
# suite, and that suite tops out at T = 6 tiles (b1-w095/w096-tw020, rw = 95
# and 96).  The reuse rewrite is an INDEXING change whose whole behaviour is
# "what tile t inherits from tile t-1", so the tile count is the axis along
# which it is most likely to be wrong -- and six tiles is not a sample of an
# axis the RTL is compiled to run to 52.  The production suite reaches
# prod-max-result at 820x307 / 4x16, i.e. rw = 817 and T = 52, the compiled
# maximum, plus 29 tiles at the full left-bank geometry.  Until this runs, "B2
# is functionally correct" is a claim about one ninth of the tile-count range.
#
# WHAT IT IS NOT.  C simulation, not co-simulation: it exercises the SOURCE,
# not the RTL.  That is the right trade here for the same reason
# csim_prod_b1.tcl gives -- these maps do not fit in xsim, and a wrong overlap,
# a wrong refill base or a dropped seg[seg_len-1] is an indexing defect that
# shows up identically in C.  A timing-dependent defect would not, and this
# suite says nothing about one.
#
# ITS OWN RESET PROJECT, and deliberately NOT csim_prod_b1.tcl with a
# variant switch: that file is pinned in logs/b1_20260818/MANIFEST.sha256 as
# Priority 4 evidence, and editing it would rewrite a record a finished
# measurement rests on.  This builds `template_match_b2_prod` and touches no
# other project.  Do not call the run hermetic: correlation_core and the
# vectors are pre-build digest-gated, but tme_top.cpp/.h and tme_tb.cpp are
# compiled live and are only manifest-bound after the run.
#
# Generate the vectors first if they are absent:
#   python tme_generate_production.py            (writes the prod package)

set project_name "template_match_b2_prod"
set part_number  "xc7z020clg400-1"

# The b2 snapshot and its digest, duplicated from run_hls_b1.tcl deliberately:
# two independent records of the same number is the point of a pinned source.
set snap_file "correlation_core.b2.cpp"
set snap_sha  "c8c7b0882af33214da5f7bbedca9ff9b985629517ddada37a9eacaacaec5d8ce"

set snap [file join b1_sources $snap_file]
if {![file exists $snap]} { error "pinned source $snap is missing" }
set got [string tolower [lindex [split [exec sha256sum $snap]] 0]]
if {$got ne $snap_sha} {
    error "b1_sources/$snap_file has SHA-256 $got, pinned $snap_sha.\
           This suite is only evidence about the co-simulated RTL if it\
           compiles the same bytes that RTL came from."
}
puts "csim_prod_b2.tcl: $snap — sha256 $got verified"

# THE STIMULUS, TOO.  Same rule and same reason as csim_prod_b1.tcl: this
# suite's claim is "the B2 core reproduces the production goldens", which is a
# statement about a specific 1.6 MB of pixels.  The blobs are gitignored and
# regenerate from pinned seeds, so tb_tme_prod.sha256 is the thing that says
# they are the right pixels.  All FOUR entries are checked, including
# tb_tme_counts_prod.txt: it is not read by the testbench, but it is the 0.3
# acceptance sidecar for these exact cases, and if it has drifted from the
# blobs the package is internally inconsistent.
#
# These are the SAME four digests csim_prod_b1.tcl checks.  That is the point:
# a B1-vs-B2 comparison of production behaviour is only a comparison if both
# ran on the same bytes.
set rec "tb_tme_prod.sha256"
if {![file exists $rec]} {
    error "$rec is missing. Regenerate the production package with\
           `python tme_generate_production.py` -- the blobs are gitignored, so\
           the digest record is the only thing that says which pixels these are."
}
set fh [open $rec r]
set rec_lines [split [string trim [read $fh]] "\n"]
close $fh
if {[llength $rec_lines] != 4} {
    error "$rec lists [llength $rec_lines] files, expected 4\
           (cases/patches/templs/counts)."
}
set n_ok 0
foreach line $rec_lines {
    set want [lindex $line 0]
    set name [lindex $line end]
    if {![file exists $name]} {
        error "$name is listed in $rec but absent. Run\
               `python tme_generate_production.py` to regenerate the package."
    }
    set have [string tolower [lindex [split [exec sha256sum $name]] 0]]
    if {$have ne $want} {
        error "$name has SHA-256 $have, pinned $want in $rec.\
               A production suite built from different pixels is not evidence\
               about the goldens this core is being held to."
    }
    puts "csim_prod_b2.tcl: $name — sha256 verified"
    incr n_ok
}
puts "csim_prod_b2.tcl: all $n_ok production inputs verified against $rec"

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
