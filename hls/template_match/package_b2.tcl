# Package the B2 solution as an IP, so Vivado can be asked for routed timing.
#
#   cd hls/template_match
#   vitis-run.bat --mode hls --tcl package_b2.tcl
#
# THIS IS NOT A RELEASE.  It exports the `b2` solution of `template_match_b1_b2`
# -- the correlation_core that slides its segment by PAR_COLS and refills only
# the new pixels -- under its own vendor string, so a bitstream built from it
# cannot be confused with the shipping 0.2 image, nor with Priority 4's B1
# image, nor with the measurements taken on either.
#
# WHY IT EXISTS.  B2's headline is a cycle term, and a cycle term becomes a
# second only at a clock.  The clock this project quotes -- 125 MHz -- is a
# ROUTED result for the unmodified core and for B1, not a property of the
# source, and B2 adds a 231-element shift register with a per-register enable
# to a design whose B1 routing closed 8.000 ns with 0.134571 ns to spare.  That
# is 1.7% of the period.  Nothing about B1's slack transfers to B2 by argument;
# it has to be re-implemented and re-read.
#
# ORDER OF OPERATIONS, AND WHY IT IS WRITTEN DOWN HERE.  Priority 4 left a
# provenance gap worth not repeating: package_b1.tcl was re-run AFTER the
# Vivado build and overwrote the impl/ip directory Vivado had already read, so
# the packaged IP retained on disk post-dates the bitstream and is not the same
# bytes.  The fix is procedural, not technical -- EXPORT FIRST, BUILD SECOND,
# AND DO NOT RE-EXPORT AFTERWARDS.  If a rebuild is needed, re-export and
# rebuild as a pair.  There is nothing in this script that can enforce that;
# it is the operator's job, which is exactly why it is stated at the top.
#
# It deliberately does NOT re-run csim or cosim: the solution it opens already
# carries them, and re-running would either repeat the xsim time or, worse,
# quietly package a solution whose reports came from a different source.
# Run `TME_SOLUTION=b2 run_hls_b1.tcl` first; this reads what that produced.

set project_name "template_match_b1_b2"
set solution     "b2"

open_project $project_name
# No -reset on either: -reset here would discard the synthesis and the
# transaction report that sw/tme_b2_ab.py adjudicates B2 from.
open_solution $solution

export_design -format ip_catalog \
              -description "PROVISIONAL, B2 — Terminal Matching Engine (TM_CCOEFF_NORMED, exact integer sums + float sqrt; template streams RAW uint8), with correlation_core's per-tile segment load replaced by horizontal overlap reuse: tile 0 loads the runtime PAR_COLS + tw - 1 pixels, every later tile slides the overlap down by PAR_COLS and refills only PAR_COLS new pixels. Includes B1 (the runtime segment length) and is a strict superset of it. Functionally identical to 0.2 by construction, by C simulation and by paired RTL co-simulation against the B1 solution over result widths 1/15/16/17/31/32/33/95/96, two exact ties and a lane-15 pair; the change is a cycle-count change only. The vector suite's adequacy for the NEW defect classes overlap reuse introduces was established before the build, not assumed: sw/tme_b2_mutants.py shows the pinned suite breaks on each of six mutants for all 256 possible stale-register fills. Not a release IP, not the 0.2 image the shipping measurements were taken on, and not Priority 4's B1 image." \
              -vendor "TermCountB2" \
              -version "0.2"

# The VENDOR carries the distinction, not the version, and that is forced on us.
# package_b1.tcl records the reason: `-version "0.2b1"` was tried there and the
# exported component.xml came out carrying 1.0 instead, with export_design
# reporting success. What is ESTABLISHED is the substitution, not its cause --
# "a VLNV version must be numeric" is the obvious explanation and was never
# tested. Either way this flow can write a VLNV field you did not ask for and
# still exit 0, which is why the check below exists rather than being trusted.
# 1.0 is the version reserved for a release, so the failure mode was to publish
# an experimental build under the release identity.
set want_vendor  TermCountB2
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
