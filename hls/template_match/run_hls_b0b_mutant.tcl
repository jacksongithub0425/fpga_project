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
#
# EVERY PATH BELOW IS ANCHORED TO THIS SCRIPT'S DIRECTORY, NOT TO [pwd].
# Relative paths were resolved against the process working directory, which is
# only hls/template_match when the caller happens to have set it there.  Run
# the script by an explicit path from anywhere else and the inputs are looked
# for in the wrong tree.  One of those lookups failed OPEN rather than closed:
# the pinned-snapshot guard globbed `b0b_sources/*.cpp` with -nocomplain, so a
# wrong working directory made it match nothing and the guard silently stopped
# guarding.  A guard that disappears when the environment shifts is worse than
# no guard, because its presence in the source is what everyone reads.

set here [file dirname [file normalize [info script]]]

set part_number "xc7z020clg400-1"

if {![info exists ::env(TME_B0B_MUT_NAME)] || ![info exists ::env(TME_B0B_MUT_SRC)]} {
    error "set TME_B0B_MUT_NAME and TME_B0B_MUT_SRC (see sw/tme_b0b_mutants.py)"
}
set mut_name $::env(TME_B0B_MUT_NAME)
set mut_src  $::env(TME_B0B_MUT_SRC)
# The driver passes a path relative to hls/template_match.  Resolve it against
# this script rather than against [pwd] so the same value works from any caller.
if {[file pathtype $mut_src] eq "relative"} {
    set mut_src [file join $here $mut_src]
}
set mut_src [file normalize $mut_src]
if {![file exists $mut_src]} { error "mutant source $mut_src is missing" }

# Refuse to compile the pinned snapshots through this path.  A mutant run
# writes no evidence, so a mix-up would be silent.
#
# FAIL CLOSED.  b0b_sources/ holds the three pinned tme_top snapshots; an empty
# glob means this script is not looking where it thinks it is, and continuing
# would run the comparison it was supposed to refuse.
set pinned_list [glob -nocomplain -directory [file join $here b0b_sources] *.cpp]
if {[llength $pinned_list] == 0} {
    error "no pinned snapshots found in [file join $here b0b_sources] --\
           refusing to run, because the guard below cannot do its job and\
           would pass a pinned source through as if it were a mutant"
}
foreach pinned $pinned_list {
    if {[file normalize $pinned] eq $mut_src} {
        error "$mut_src is a PINNED snapshot — mutants must be copies under\
               b0b_sources/mutants/, not the evidence itself"
    }
}

set project_name "template_match_b0b_mut_$mut_name"
puts "run_hls_b0b_mutant.tcl: mutant '$mut_name' from $mut_src"
puts "run_hls_b0b_mutant.tcl: anchored at $here (cwd is [pwd])"
puts "run_hls_b0b_mutant.tcl: project $project_name (csim only, no csynth, no cosim)"

open_project -reset [file join $here $project_name]
set_top tme_top

add_files $mut_src                                          -cflags "-I$here"
add_files [file join $here b1_sources/correlation_core.b2.cpp] -cflags "-I$here"
add_files [file join $here tme_top.h]

add_files -tb [file join $here tme_tb.cpp]
# The vectors are generated, gitignored, and MUST be present: an empty -tb list
# would leave csim reading nothing and reporting a clean pass, which is the one
# verdict this script must never invent.
set tb_data [glob -nocomplain -directory $here tb_tme_*.bin tb_tme_*.txt]
if {[llength $tb_data] == 0} {
    error "no tb_tme_* vectors in $here -- generate them first:\
           python tme_generate_production.py --suite b1"
}
add_files -tb $tb_data

open_solution -reset "mut"
set_part $part_number
create_clock -period 5ns -name default

# The b1 suite: twelve banking-boundary cases plus the two direct tests, and
# fast enough to sweep a set of mutants through.  rh spans 6..24 there, so a
# vertical-reuse defect has room to show; the corners in `-argv b0b` carry the
# csim manifest with them and are too slow for a sweep.
csim_design -argv "b1"

close_project
