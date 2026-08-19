# Package the B1 solution as an IP, so Vivado can be asked for routed timing.
#
#   cd hls/template_match
#   vitis-run.bat --mode hls --tcl package_b1.tcl
#
# THIS IS NOT A RELEASE, and it is not a re-packaging of 0.2.  It exports the
# `b1` solution of `template_match_b1_b1` — the correlation_core whose segment
# load is the runtime PAR_COLS + tw - 1 rather than the compiled 232 — under
# its own version string, so a bitstream built from it cannot be confused with
# the 0.2 image that is on the board and that every existing board measurement
# was taken on.
#
# WHY IT EXISTS.  B1's headline is "26.334 s/page AT 125 MHz" (26.240 was the
# projection this RTL withdrew).  The clock half
# of that sentence was established for the UNMODIFIED core (routed at 8.000 ns,
# WNS +0.064 ns, observed at 125.0 MHz on the board — see
# logs/tme_125_timing/PRIORITY2_EVIDENCE.md).  +0.064 ns is 0.8% of the period:
# there is no slack to spend on faith, so a changed correlation_core has to be
# re-implemented before the figure may be quoted at that clock at all.  This
# file is what feeds that implementation.
#
# It deliberately does NOT re-run csim or cosim: the solution it opens already
# carries them, and re-running would either repeat 20 minutes of xsim or, worse,
# quietly package a solution whose reports came from a different source revision.
# Run `TME_SOLUTION=b1 run_hls_b1.tcl` first; this reads what that produced.

set project_name "template_match_b1_b1"
set solution     "b1"

open_project $project_name
# No -reset on either: -reset here would discard the synthesis and the
# transaction report that sw/tme_b1_ab.py adjudicates B1 from.
open_solution $solution

# NOTE ON PROVENANCE.  export_design OVERWRITES $project_name/$solution/impl/ip.
# On 2026-08-18 that happened AFTER the Vivado build had already read that
# directory, so the packaged IP retained on disk post-dates the bitstream it is
# supposed to have produced and is not the same bytes.  If you re-package to
# build a NEW image, build it from this export; if you are only re-reading old
# evidence, do not run this file at all -- read vivado_b1_125.log, which names
# both the IP repository and the resolved VLNV.

export_design -format ip_catalog \
              -description "PROVISIONAL, B1 — Terminal Matching Engine (TM_CCOEFF_NORMED, exact integer sums + float sqrt; template streams RAW uint8), with correlation_core's per-tile segment load shortened from the compiled 232 pixels to the runtime PAR_COLS + tw - 1. Functionally identical to 0.2 by construction and by paired RTL co-simulation over result widths 1/15/16/17/31/32/33/95/96 plus a lane-15 pair; the change is a cycle-count change only. As of 2026-08-19 an image built from this export HAS run on the board (logs/b1_board_20260818/, 7/7 at a gated 125.0000 MHz, routed 8.000 ns WNS +0.135 ns); it is still NOT a release IP and is not the 0.2 image the earlier measurements were taken on." \
              -vendor "TermCountB1" \
              -version "0.2"

# The VENDOR carries the distinction, not the version, and that is forced on us:
# `-version "0.2b1"` was tried first and the exported component.xml came out
# carrying version 1.0 instead.  What is ESTABLISHED is the substitution -- the
# requested string was not what was written, and export_design reported success.
# "a VLNV version must be numeric" is the obvious explanation and is NOT tested
# here; no other malformed version was tried.  Either way the lesson is the
# same and it is the reason for the check below: this flow can write a VLNV
# field you did not ask for and still exit 0.  1.0 is the version this project has explicitly reserved for
# a release, so the failure mode was to publish a B1 build under the release
# identity.  `TermCountB1:hls:tme_top:0.2` cannot be confused with the shipped
# `TermCount:hls:tme_top:0.2` and cannot be mistaken for 1.0 either.
#
# Verify rather than trust: read the VLNV back out of the generated
# component.xml and fail if any field is not what was asked for.
set want_vendor  TermCountB1
set want_library hls
set want_name    tme_top
set want_version 0.2
# The solution directory is $project_name/$solution relative to this script's
# working directory -- get_property SOLUTION_DIR is not available in the
# vitis-run HLS Tcl interpreter, which is how the first version of this
# check failed AFTER a successful export.
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

close_project
