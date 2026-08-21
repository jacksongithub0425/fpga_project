# Package the B0b solution as an IP, so Vivado can be asked for routed timing.
#
#   cd hls/template_match
#   vitis-run.bat --mode hls --tcl package_b0b.tcl
#
# THIS IS NOT A RELEASE.  It exports the `b0b` solution of
# `template_match_b0b_b0b` -- the core whose window statistics are maintained
# across output rows instead of recomputed per template row -- under its own
# vendor string, so a bitstream built from it cannot be confused with the
# shipping 0.2 image, nor with Priority 4's B1 image, nor with Priority 5's B2
# image, nor with the measurements taken on any of them.
#
# WHY IT EXISTS.  B0b's headline is a cycle term, and a cycle term becomes a
# second only at a clock.  The clock this project quotes -- 125 MHz -- is a
# ROUTED result for the unmodified core, for B1 and for B2, not a property of
# the source.  B2 closed 8.000 ns with 0.011710 ns to spare: 0.15% of the
# period, an eighth of B1's margin.  B0b INHERITS that margin on a path inside
# correlation_core that B0b does not itself edit -- but csynth already reports
# it adding DSPs and LUTs, and placement around a 12 ps path is not something
# an argument settles.  Route it early and read the number.
#
# ORDER OF OPERATIONS, AND WHY IT IS WRITTEN DOWN HERE.  Priority 4 left a
# provenance gap worth not repeating: package_b1.tcl was re-run AFTER the
# Vivado build and overwrote the impl/ip directory Vivado had already read, so
# the packaged IP retained on disk post-dates the bitstream and is not the same
# bytes.  The fix is procedural, not technical -- EXPORT FIRST, BUILD SECOND,
# AND DO NOT RE-EXPORT AFTERWARDS.  If a rebuild is needed, re-export and
# rebuild as a pair.
#
# It deliberately does NOT re-run csim or cosim: the solution it opens already
# carries them, and re-running would either repeat the xsim time or, worse,
# quietly package a solution whose reports came from a different source.
# Run `TME_B0B_SOLUTION=b0b run_hls_b0b.tcl` first; this reads what that
# produced.

set project_name "template_match_b0b_b0b"
set solution     "b0b"

open_project $project_name
# No -reset on either: -reset here would discard the synthesis and the
# transaction report that sw/tme_b0b_ab.py adjudicates B0b from.
open_solution $solution

export_design -format ip_catalog \
              -description "PROVISIONAL, B0b — Terminal Matching Engine (TM_CCOEFF_NORMED, exact integer sums + float sqrt; template streams RAW uint8), with the per-(output row, template row) window-statistics loops replaced by ONE vertically-reused pass: the first output row scans all th patch rows, and every later output row subtracts the row leaving the window and adds the row entering it. Includes B1 (the runtime segment length) and B2 (horizontal overlap reuse) and is a strict superset of both. Carries the general sum of I and sum of I squared, not a foreground count, so the patch pixel domain is unrestricted and every existing vector stays valid. Functionally identical to the B2 solution by construction and by measurement: a shadow build computed both paths side by side and compared them at 2,911,495 result positions over 100 invocations in five C-simulation suites with zero disagreements, and sw/tme_b0b_mutants.py shows that comparison detects each of five deliberate defects. The change is a cycle-count change only. Not a release IP, not the 0.2 image the shipping measurements were taken on, and not Priority 4's or Priority 5's image." \
              -vendor "TermCountB0b" \
              -version "0.2"

# The VENDOR carries the distinction, not the version, and that is forced on us.
# package_b1.tcl records the reason: `-version "0.2b1"` was tried there and the
# exported component.xml came out carrying 1.0 instead, with export_design
# reporting success.  What is ESTABLISHED is the substitution, not its cause --
# "a VLNV version must be numeric" is the obvious explanation and was never
# tested.  Either way this flow can write a VLNV field you did not ask for and
# still exit 0, which is why the check below exists rather than being trusted.
set want_vendor  TermCountB0b
set want_library hls
set want_name    tme_top
set want_version 0.2
# The solution directory is $project_name/$solution relative to this script's
# working directory -- get_property SOLUTION_DIR is not available in the
# vitis-run HLS Tcl interpreter, which is how package_b1.tcl's first version of
# this check failed AFTER a successful export.
set comp [file join $project_name $solution impl ip component.xml]
if {![file exists $comp]} {
    error "export_design produced no component.xml at $comp"
}
set fh [open $comp r]
set xml [read $fh]
close $fh
# All FOUR VLNV fields.  Checking three of four leaves `library` free to move
# unnoticed, and build_tme_standalone matches on it just as strictly.
foreach {field want} [list vendor $want_vendor library $want_library \
                           name $want_name version $want_version] {
    if {![regexp "<spirit:$field>(\[^<\]*)<" $xml -> got]} {
        error "component.xml has no spirit:$field"
    }
    if {$got ne $want} {
        error "export_design wrote spirit:$field = '$got', not '$want'.\n\
               Vitis rewrites a VLNV field it does not like INSTEAD OF failing,\
               so this check is the only thing standing between a bad -version\
               and a bitstream published under the wrong identity."
    }
}
puts "packaged ${want_vendor}:${want_library}:${want_name}:${want_version} -\
      all four VLNV fields verified against component.xml"
puts "IP repo for Vivado: [file join [pwd] $project_name $solution impl ip]"
puts "NEXT, IN THIS ORDER, AND DO NOT RE-EXPORT AFTERWARDS:"
puts "  TME_IP_REPO=<the path above> TME_HLS_VLNV=${want_vendor}:hls:tme_top:0.2 \\"
puts "  TME_FCLK_MHZ=125 TME_BUILD_ROOT=<build dir> vivado -mode batch \\"
puts "    -source vivado/tme_standalone/build_tme_standalone.tcl -notrace"

close_project
