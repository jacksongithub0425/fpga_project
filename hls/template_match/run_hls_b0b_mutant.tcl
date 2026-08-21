# Priority 6 (B0b): build ONE mutated shadow core and C-simulate it.
#
#   TME_B0B_MUT_NAME=<name> TME_B0B_MUT_SRC=<path> \
#       vitis-run.bat --mode hls --tcl run_hls_b0b_mutant.tcl
#
# Driven by sw/tme_b0b_mutants.py, which writes the mutated source, invokes
# this, and reads the pass/fail out of the C-simulation log.
#
# WHY THIS IS NOT run_hls_b0b.tcl WITH ANOTHER FLAG.  That script gates every
# input on a pinned SHA-256, which is the whole reason its reports are
# evidence.  A mutant is by definition an unpinned source, so letting it
# through the pinned path would mean weakening the gate that makes the real
# measurement mean something.  Separate script, separate project prefix
# (`template_match_b0b_mut_*`), and NO csynth or cosim: a mutant produces a
# verdict, never a number anyone quotes.
#
# The mutated file is a copy under b0b_sources/mutants/, never the pinned
# snapshot itself.

set part_number "xc7z020clg400-1"

if {![info exists ::env(TME_B0B_MUT_NAME)] || ![info exists ::env(TME_B0B_MUT_SRC)]} {
    error "set TME_B0B_MUT_NAME and TME_B0B_MUT_SRC (see sw/tme_b0b_mutants.py)"
}
set mut_name $::env(TME_B0B_MUT_NAME)
set mut_src  $::env(TME_B0B_MUT_SRC)
if {![file exists $mut_src]} { error "mutant source $mut_src is missing" }

# Refuse to compile the pinned snapshots through this path.  A mutant run
# writes no evidence, so a mix-up would be silent.
foreach pinned [glob -nocomplain b0b_sources/*.cpp] {
    if {[file normalize $pinned] eq [file normalize $mut_src]} {
        error "$mut_src is a PINNED snapshot — mutants must be copies under\
               b0b_sources/mutants/, not the evidence itself"
    }
}

set project_name "template_match_b0b_mut_$mut_name"
puts "run_hls_b0b_mutant.tcl: mutant '$mut_name' from $mut_src"
puts "run_hls_b0b_mutant.tcl: project $project_name (csim only, no csynth, no cosim)"

open_project -reset $project_name
set_top tme_top

add_files $mut_src                          -cflags "-I[pwd]"
add_files b1_sources/correlation_core.b2.cpp -cflags "-I[pwd]"
add_files tme_top.h

add_files -tb tme_tb.cpp
add_files -tb [glob -nocomplain tb_tme_*.bin tb_tme_*.txt]

open_solution -reset "mut"
set_part $part_number
create_clock -period 5ns -name default

# The b1 suite: twelve banking-boundary cases plus the two direct tests, and
# fast enough to sweep a set of mutants through.  rh spans 6..24 there, so a
# vertical-reuse defect has room to show; the corners in `-argv b0b` carry the
# csim manifest with them and are too slow for a sweep.
csim_design -argv "b1"

close_project
