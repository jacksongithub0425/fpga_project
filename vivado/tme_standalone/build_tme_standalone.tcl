# Build the standalone template_match_core PL image, and read real slack off it.
#
#   vivado -mode batch -source build_tme_standalone.tcl -notrace
#
# or from the Vivado 2025.2 GUI Tcl console:
#   source {C:/Users/lychee/Desktop/FPGA/vivado/tme_standalone/build_tme_standalone.tcl}
#
# Overrides, set before sourcing:
#   set ::env(TME_IP_REPO)         {.../template_match_provisional/solution1/impl/ip}
#   set ::env(TME_BUILD_ROOT)      {C:/Users/lychee/tc25/vivado_project/tme_standalone}
#   set ::env(TME_BUILD_BITSTREAM) 0     ;# stop after the BD, for a guided pass
#   set ::env(TME_JOBS)            12
#
# -----------------------------------------------------------------------------
# WHAT THIS IMAGE IS
#
# One core, its two DMAs, and nothing else — the same shape as the extractor's
# standalone image (contract §8), so a failure has one place to be.  Per
# contract §7.1, `tme_top` exports a single coherent AXI4-Lite slave: start/done,
# the four geometry scalars and the three result registers all live in
# `s_axi_CTRL`, and there are no raw `ap_start`/`ap_done` top-level pins.  That
# is what makes PS sequencing possible at all, and it is why this design needs
# no wrapper RTL.
#
#   PS M_AXI_GP0 --smartconnect_lite--> axi_dma_patch  S_AXI_LITE
#                                  |--> axi_dma_templ  S_AXI_LITE
#                                  \--> tme_top_0      s_axi_CTRL
#
#   axi_dma_patch M_AXIS_MM2S (8-bit) --> tme_top_0 patch_stream
#   axi_dma_templ M_AXIS_MM2S (8-bit) --> tme_top_0 templ_stream
#
#   axi_dma_patch M_AXI_MM2S --smartconnect_mem--> PS S_AXI_HP0
#   axi_dma_templ M_AXI_MM2S --/
#
# Both DMAs are MM2S-ONLY: both matcher streams are PL inputs, and the results
# come back over AXI4-Lite, not over a stream.  There is no S2MM anywhere in
# this design, and adding one would be a change to the result ABI (§5.1/§6.3),
# not a wiring convenience.
#
# -----------------------------------------------------------------------------
# TWO SETTINGS THAT ARE DELIBERATE, NOT DEFAULTS
#
# 1. `c_sg_length_width = 18`.  This sets the DMA's single-transfer ceiling to
#    2^18-1 = 262,143 bytes, which is contract §3.1's bound and the reason
#    `stress-max-envelope` (820 x 307 = 251,740 B) is a meaningful test rather
#    than a big number.  It MATCHES the extractor's standalone image, on
#    purpose: the point is to reproduce the platform's constraint, not to
#    engineer around it.  Widening this to 26 bits is a legitimate future
#    decision (§3.1 says so) but it must be deliberate, and it invalidates the
#    headroom figure the bring-up script prints.
#
# 2. The PS7 is configured WITHOUT a board preset, requesting 50 MHz on FCLK0 —
#    again matching the extractor image.  Read the note in
#    `report_post_route_timing` before interpreting any WNS number this
#    produces: the frequency Vivado constrains and the frequency the board runs
#    are NOT the same, and that difference is worth more than the WNS itself.

namespace eval ::tme_standalone {
    variable project_name  tme_standalone
    variable bd_name       tme_bd
    variable part_name     xc7z020clg400-1
    # The core to instantiate.  Overridable with TME_HLS_VLNV so an
    # experimental core can be implemented for timing WITHOUT being packaged
    # under the shipping identity -- Priority 4's B1 build is
    # TermCountB1:hls:tme_top:0.2, exported by
    # hls/template_match/package_b1.tcl.  The DEFAULT does not move: an
    # invocation that names no VLNV builds the same core every recorded WNS
    # was measured on, and post_route_wns.txt records which one was used.
    variable hls_vlnv [expr {[info exists ::env(TME_HLS_VLNV)]
                             && [string length $::env(TME_HLS_VLNV)] > 0
                             ? $::env(TME_HLS_VLNV)
                             : {TermCount:hls:tme_top:0.2}}]

    # Requested FCLK0.  See report_post_route_timing for why this is not the
    # frequency the board runs at.
    #
    # Overridable with TME_FCLK_MHZ for the Priority 2 clock probe.  The default
    # stays 50 because that is what the board image is built at and what every
    # recorded WNS so far is measured against; a probe at another frequency is a
    # separate build into a separate TME_BUILD_ROOT, never a redefinition of the
    # shipping image.  Note the PS7 does not honour an arbitrary request: it
    # picks integer divisors off its assumed 1600 MHz IO PLL, so the CONSTRAINED
    # period is whatever Vivado computes and is reported back in post_route_wns.txt.
    # Read that file for the period the number belongs to; never assume it.
    variable fclk_mhz [expr {[info exists ::env(TME_FCLK_MHZ)]
                             && [string length $::env(TME_FCLK_MHZ)] > 0
                             ? $::env(TME_FCLK_MHZ) : 50}]

    # Contract §3.1.  Changing this changes what the bring-up suite proves.
    variable sg_length_width 18
}

proc ::tme_standalone::fail {message} {
    puts stderr "\nTME_STANDALONE_ERROR: $message\n"
    return -code error $message
}

proc ::tme_standalone::env_or {name default} {
    if {[info exists ::env($name)] && [string length $::env($name)] > 0} {
        return $::env($name)
    }
    return $default
}


proc ::tme_standalone::file_sha256 {path} {
    # Windows certutil is the only hash tool guaranteed present beside Vivado.
    # Returns lowercase hex, or "" if it could not be computed -- the caller
    # decides whether that is fatal.  Never let a hashing failure look like a
    # hash match.
    if {![file exists $path]} { return "" }
    if {[catch {exec certutil -hashfile [file nativename $path] SHA256} out]} {
        return ""
    }
    foreach line [split $out "\n"] {
        set line [string trim $line]
        if {[regexp {^[0-9a-fA-F ]{64,}$} $line]} {
            return [string tolower [string map {" " ""} $line]]
        }
    }
    return ""
}


# ---------------------------------------------------------------------------
# OPERATOR-SUPPLIED BOARD OBSERVATION  (report-only metadata)
# ---------------------------------------------------------------------------
# A routed result cannot state a board frequency; only a board measurement can.
# These inputs let a measurement be RECORDED in the generated note without the
# script ever inferring one.  They are optional and default unset; when unset
# the report keeps its "closes at <period> in implementation, not a board
# frequency" caveat verbatim.
#
#   TME_OBSERVED_BOARD_MHZ         e.g. 125.0 -- what Clocks.fclk0_mhz READ
#   TME_OBSERVED_EVIDENCE          path to the retained transcript proving it
#   TME_OBSERVED_BITSTREAM_SHA256  sha256 of the .bit that was measured
#
# All three or none: a bare frequency with no evidence path and no bitstream
# binding is an assertion, not a measurement, and this script will not print
# one.  The hash is checked against the bitstream actually being handed off, so
# a measurement cannot be carried over to a different build.  The generated
# text labels the value OPERATOR-SUPPLIED at the point of use.
proc ::tme_standalone::observed_board_clock {} {
    set mhz [env_or TME_OBSERVED_BOARD_MHZ ""]
    set evidence [env_or TME_OBSERVED_EVIDENCE ""]
    set sha [string tolower [env_or TME_OBSERVED_BITSTREAM_SHA256 ""]]
    if {$mhz eq "" && $evidence eq "" && $sha eq ""} { return {} }
    if {$mhz eq "" || $evidence eq "" || $sha eq ""} {
        fail "TME_OBSERVED_BOARD_MHZ, TME_OBSERVED_EVIDENCE and \
TME_OBSERVED_BITSTREAM_SHA256 must be set together or not at all.  A board \
frequency without an evidence path and a bitstream hash is an assertion, not \
a measurement."
    }
    if {![string is double -strict $mhz] || $mhz <= 0} {
        fail "TME_OBSERVED_BOARD_MHZ must be a positive number, got: $mhz"
    }
    if {![regexp {^[0-9a-f]{64}$} $sha]} {
        fail "TME_OBSERVED_BITSTREAM_SHA256 must be 64 hex characters, got: $sha"
    }
    return [list mhz $mhz evidence $evidence sha $sha]
}

proc ::tme_standalone::script_dir {} {
    return [file normalize [file dirname [info script]]]
}

# -----------------------------------------------------------------------------

proc ::tme_standalone::build_bd {} {
    variable bd_name
    variable hls_vlnv
    variable fclk_mhz
    variable sg_length_width

    create_bd_design $bd_name

    # ---- Processing system -------------------------------------------------
    set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 \
                processing_system7_0]
    apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
        -config {make_external "FIXED_IO, DDR" apply_board_preset "0" \
                 Master "Disable" Slave "Disable"} $ps
    set_property -dict [list \
        CONFIG.PCW_FPGA_FCLK0_ENABLE {1} \
        CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ $fclk_mhz \
        CONFIG.PCW_USE_S_AXI_HP0 {1} \
    ] $ps

    set rst [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 \
                 proc_sys_reset_0]

    # ---- The matcher -------------------------------------------------------
    if {[llength [get_ipdefs -quiet $hls_vlnv]] == 0} {
        fail "HLS IP '$hls_vlnv' is not in the catalogue. Run\n  \
              cd hls/template_match && vitis-run.bat --mode hls --tcl \
              package_provisional.tcl\nand point TME_IP_REPO at the resulting \
              solution1/impl/ip directory."
    }
    set tme [create_bd_cell -type ip -vlnv $hls_vlnv tme_top_0]

    # ---- Two MM2S-only DMAs ------------------------------------------------
    # c_include_s2mm 0: there is no return stream in this design.
    # c_m_axis_mm2s_tdata_width 8: tme_top's AXIS ports are ap_axiu<8,1,1,1>,
    #   i.e. one pixel per beat.  The DMA drives TDATA/TKEEP/TLAST only; the
    #   core's TUSER/TID/TDEST inputs tie off, which is harmless because
    #   tme_top reads none of them (it does not even look at TLAST — it reads
    #   exactly patch_w*patch_h beats, which is precisely the framing gap
    #   §7.1 leaves open).
    foreach {name} {axi_dma_patch axi_dma_templ} {
        set dma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 $name]
        set_property -dict [list \
            CONFIG.c_include_sg {0} \
            CONFIG.c_include_mm2s {1} \
            CONFIG.c_include_s2mm {0} \
            CONFIG.c_include_mm2s_dre {0} \
            CONFIG.c_sg_length_width $sg_length_width \
            CONFIG.c_m_axi_mm2s_data_width {64} \
            CONFIG.c_m_axis_mm2s_tdata_width {8} \
        ] $dma
    }

    # ---- Interconnect ------------------------------------------------------
    set sc_lite [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 \
                     smartconnect_lite]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {3}] $sc_lite

    set sc_mem [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 \
                    smartconnect_mem]
    set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}] $sc_mem

    # ---- Clock and reset ---------------------------------------------------
    set clk  [get_bd_pins processing_system7_0/FCLK_CLK0]
    set rstn [get_bd_pins proc_sys_reset_0/peripheral_aresetn]

    connect_bd_net $clk [get_bd_pins proc_sys_reset_0/slowest_sync_clk]
    connect_bd_net [get_bd_pins processing_system7_0/FCLK_RESET0_N] \
                   [get_bd_pins proc_sys_reset_0/ext_reset_in]

    foreach pin {processing_system7_0/M_AXI_GP0_ACLK
                 processing_system7_0/S_AXI_HP0_ACLK
                 axi_dma_patch/s_axi_lite_aclk
                 axi_dma_patch/m_axi_mm2s_aclk
                 axi_dma_templ/s_axi_lite_aclk
                 axi_dma_templ/m_axi_mm2s_aclk
                 tme_top_0/ap_clk
                 smartconnect_lite/aclk
                 smartconnect_mem/aclk} {
        connect_bd_net $clk [get_bd_pins $pin]
    }
    foreach pin {axi_dma_patch/axi_resetn
                 axi_dma_templ/axi_resetn
                 tme_top_0/ap_rst_n
                 smartconnect_lite/aresetn
                 smartconnect_mem/aresetn} {
        connect_bd_net $rstn [get_bd_pins $pin]
    }

    # ---- AXI4-Lite: PS -> three slaves --------------------------------------
    connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] \
                        [get_bd_intf_pins smartconnect_lite/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins smartconnect_lite/M00_AXI] \
                        [get_bd_intf_pins axi_dma_patch/S_AXI_LITE]
    connect_bd_intf_net [get_bd_intf_pins smartconnect_lite/M01_AXI] \
                        [get_bd_intf_pins axi_dma_templ/S_AXI_LITE]
    connect_bd_intf_net [get_bd_intf_pins smartconnect_lite/M02_AXI] \
                        [get_bd_intf_pins tme_top_0/s_axi_CTRL]

    # ---- Memory: both DMAs -> HP0 -------------------------------------------
    connect_bd_intf_net [get_bd_intf_pins axi_dma_patch/M_AXI_MM2S] \
                        [get_bd_intf_pins smartconnect_mem/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins axi_dma_templ/M_AXI_MM2S] \
                        [get_bd_intf_pins smartconnect_mem/S01_AXI]
    connect_bd_intf_net [get_bd_intf_pins smartconnect_mem/M00_AXI] \
                        [get_bd_intf_pins processing_system7_0/S_AXI_HP0]

    # ---- Streams -------------------------------------------------------------
    connect_bd_intf_net [get_bd_intf_pins axi_dma_patch/M_AXIS_MM2S] \
                        [get_bd_intf_pins tme_top_0/patch_stream]
    connect_bd_intf_net [get_bd_intf_pins axi_dma_templ/M_AXIS_MM2S] \
                        [get_bd_intf_pins tme_top_0/templ_stream]

    assign_bd_address
    regenerate_bd_layout
    validate_bd_design
    save_bd_design

    # The PS-side offsets here are what the bring-up script's overlay lookup
    # resolves to; print them so a mismatch is visible in the build log rather
    # than only as a failed register read on the board.
    puts "\n---- address map ----"
    foreach space [get_bd_addr_spaces] {
        foreach seg [get_bd_addr_segs -quiet -of_objects $space] {
            set off [get_property -quiet OFFSET $seg]
            if {$off eq ""} { continue }
            puts [format "  %-56s %s +%s" $seg $off \
                      [get_property -quiet RANGE $seg]]
        }
    }
}

# -----------------------------------------------------------------------------

proc ::tme_standalone::report_post_route_timing {out_dir} {
    variable project_name
    variable fclk_mhz

    open_run impl_1 -name impl_1

    set summary_rpt [file join $out_dir post_route_timing_summary.rpt]
    report_timing_summary -delay_type min_max -report_unconstrained \
        -check_timing_verbose -max_paths 10 -file $summary_rpt
    report_timing -delay_type max -sort_by slack -max_paths 20 -nworst 1 \
        -file [file join $out_dir post_route_worst_paths.rpt]
    report_utilization -file [file join $out_dir post_route_utilization.rpt]

    set wns [get_property STATS.WNS [get_runs impl_1]]
    set tns [get_property STATS.TNS [get_runs impl_1]]
    set whs [get_property STATS.WHS [get_runs impl_1]]
    set ths [get_property STATS.THS [get_runs impl_1]]
    # Endpoint counts are not exposed as run properties; TNS is the summary
    # that matters and 0.000 means nothing failed.  Per-endpoint detail lives
    # in post_route_timing_summary.rpt.
    set verdict [expr {$tns == 0 && $ths == 0 \
                       ? "all constraints met" : "CONSTRAINTS VIOLATED"}]
    # EVERY SENTENCE OF PROSE BELOW THAT DESCRIBES THE OUTCOME BRANCHES ON
    # THIS.  It used to branch on nothing: the clock-probe note said "this
    # result says the LOGIC closes at the probed period" and "this build met
    # its constraint with room to spare" unconditionally, so a run that
    # VIOLATED its constraint generated a report claiming it had closed.  B0b
    # (logs/b0b_20260820/) is the run that exposed it -- WNS -0.051470 ns, and
    # a report saying it closed.  Prose that cannot say the negative result is
    # not a report, and a reader who quotes it is not the one at fault.
    set met [expr {$tns == 0 && $ths == 0}]

    # The clock the WNS is actually measured against.  Reporting WNS without
    # this is how a number gets misread — see the note written into the file
    # below.
    set clk [lindex [get_clocks -quiet] 0]
    set period "unknown"
    set clk_name "unknown"
    if {[llength $clk] == 1} {
        set clk_name [get_property NAME $clk]
        set period [get_property PERIOD $clk]
    }

    # WHICH BUILD IS THIS?  Derive it from the OPENED DESIGN, never from
    # TME_FCLK_MHZ.  That variable describes the request this *invocation*
    # would make, and in TME_REPORT_ONLY mode no request is made at all: it
    # defaults to 50 while the project on disk may be the 125 MHz probe, which
    # made the generated note tell the shipping image's story about a probe
    # build.  The constrained period is a property of the routed design and
    # cannot disagree with the artifact being described.
    set design_fclk_mhz "unknown"
    if {$period ne "unknown" && $period > 0} {
        set design_fclk_mhz [format %.6g [expr {1000.0 / $period}]]
    }
    if {$design_fclk_mhz eq "unknown"} {
        fail "could not read a constrained clock period from the routed \
design; refusing to generate a note that cannot name its own operating point."
    }
    if {$fclk_mhz ne "" && abs($design_fclk_mhz - $fclk_mhz) > 0.001} {
        puts "WARNING: TME_FCLK_MHZ=$fclk_mhz but the opened design is \
constrained at $design_fclk_mhz MHz ($period ns).  The report describes the \
DESIGN; TME_FCLK_MHZ is ignored here."
    }

    set lines {}
    lappend lines "template_match_core standalone — post-route timing"
    lappend lines "generated [clock format [clock seconds]]"
    lappend lines ""
    lappend lines "  clock              : $clk_name"
    lappend lines "  constrained period : $period ns"
    lappend lines "  post-route WNS     : $wns ns"
    lappend lines "  post-route TNS     : $tns ns"
    lappend lines "  post-route WHS     : $whs ns"
    lappend lines "  post-route THS     : $ths ns"
    lappend lines "  verdict            : $verdict"
    # The worst path's ACTUAL delays, read off the design rather than derived.
    # period - WNS is NOT the data-path delay: it is the whole launch-to-capture
    # budget, which also carries setup time, clock uncertainty and skew.  The
    # two differ by a couple of hundred picoseconds here, and conflating them
    # misdescribes where the time goes.
    set path [lindex [get_timing_paths -quiet -delay_type max -max_paths 1] 0]
    set dp ""; set lg ""; set nd ""; set levels ""; set sp ""; set ep ""
    if {[llength $path] == 1} {
        set dp     [get_property -quiet DATAPATH_DELAY $path]
        set lg     [get_property -quiet DATAPATH_LOGIC_DELAY $path]
        set nd     [get_property -quiet DATAPATH_NET_DELAY $path]
        set levels [get_property -quiet LOGIC_LEVELS $path]
        set sp     [get_property -quiet STARTPOINT_PIN $path]
        set ep     [get_property -quiet ENDPOINT_PIN $path]
    }

    if {$period ne "unknown" && $wns ne ""} {
        set budget [expr {$period - $wns}]
        lappend lines ""
        lappend lines [format "  budget consumed (period - WNS): %.3f ns" $budget]
        if {$dp ne ""} {
            lappend lines [format "  worst-path data delay        : %.3f ns" $dp]
            if {$lg ne "" && $nd ne ""} {
                lappend lines [format "    of which logic             : %.3f ns (%.1f%%)" \
                                   $lg [expr {100.0 * $lg / $dp}]]
                lappend lines [format "    of which routing           : %.3f ns (%.1f%%)" \
                                   $nd [expr {100.0 * $nd / $dp}]]
            }
            if {$levels ne ""} {
                lappend lines "  logic levels                 : $levels"
            }
            lappend lines ""
            lappend lines "  The two differ by setup time, clock uncertainty and"
            lappend lines "  skew.  Use 'budget consumed' to re-time to another"
            lappend lines "  period; use 'data delay' to describe where the time"
            lappend lines "  goes.  They are not interchangeable."
        }
        if {$sp ne "" && $ep ne ""} {
            lappend lines ""
            lappend lines "  binding path -- this is what to fix if the clock"
            lappend lines "  is ever raised:"
            lappend lines "    from: $sp"
            lappend lines "    to  : $ep"
        }
        lappend lines ""
        lappend lines [format "  slack at the board's 32.000 ns period: %+.3f ns" \
                           [expr {32.000 - $budget}]]
        lappend lines "  (32 ns is the recorded board period -- CONFIRM it with"
        lappend lines "   Clocks.fclk0_mhz; the bring-up script now prints it.)"
    }
    lappend lines ""
    lappend lines "READ THIS BEFORE QUOTING THE WNS ABOVE"
    lappend lines ""
    lappend lines "The constrained period and the period the board runs at are"
    lappend lines "different numbers, and the WNS is against the CONSTRAINED one."
    lappend lines ""
    if {$design_fclk_mhz == 50} {
        lappend lines "The PS7 here requests 50 MHz on FCLK0 and Vivado constrains"
        lappend lines "20.000 ns, because it computes FCLK0 from a 1600 MHz IO PLL"
        lappend lines "and the divisor pair 8 x 4 (= 32).  On the board, PYNQ applies"
        lappend lines "that same divisor pair to the PL's ACTUAL PLL rate, which on"
        lappend lines "this platform is 1000 MHz -- giving 1000/32 = 31.25 MHz, the"
        lappend lines "32.000 ns period contract §8 records.  Both numbers are"
        lappend lines "correct; they describe different things."
        lappend lines ""
        lappend lines "So the design is over-constrained relative to the hardware by"
        lappend lines "1.6x, which is the safe direction.  Quote 'WNS = <x> ns against"
        lappend lines "a 20 ns constraint', never 'WNS = <x> ns' alone."
        lappend lines ""
        lappend lines "A negative WNS here does NOT necessarily mean the board fails:"
        lappend lines "at 32 ns there is 12 ns of margin the constraint does not know"
        lappend lines "about.  It does mean the design cannot be clocked at 50 MHz,"
        lappend lines "which is the thing worth knowing before anyone tries."
    } else {
        lappend lines "THIS IS A CLOCK PROBE, NOT THE SHIPPING IMAGE.  The opened"
        lappend lines "design is constrained at $period ns, so the PS7 requested"
        lappend lines "$design_fclk_mhz MHz and"
        lappend lines "Vivado constrains the period printed above.  The shipping"
        lappend lines "image requests 50 MHz, is constrained at 20.000 ns, and runs"
        lappend lines "on the board at 31.25 MHz / 32.000 ns (contract §8) because"
        lappend lines "PYNQ applies the PS7's divisor pair to a 1000 MHz PL PLL"
        lappend lines "rather than the 1600 MHz Vivado assumed."
        lappend lines ""
        if {!$met} {
            lappend lines "THIS PROBE FAILED.  The logic does NOT close at $period ns:"
            lappend lines "the verdict above is $verdict, with WNS $wns ns and"
            lappend lines "TNS $tns ns.  There is no frequency claim of any kind in"
            lappend lines "this report -- not an implementation one and not a board"
            lappend lines "one.  Quote it as 'does not close at $period ns in"
            lappend lines "implementation', and quote the WNS only with the period"
            lappend lines "beside it."
            lappend lines ""
            lappend lines "WHAT IT DOES NOT SAY EITHER.  One run at whatever effort"
            lappend lines "this flow used does not establish that the design CANNOT"
            lappend lines "close at $period ns.  A negative slack this small is the"
            lappend lines "kind a directive change, a seed sweep or post-route"
            lappend lines "phys_opt routinely recovers, and none of those was tried"
            lappend lines "unless the build log above says so.  'Did not close' and"
            lappend lines "'cannot close' are different claims; this report supports"
            lappend lines "only the first."
            lappend lines ""
            lappend lines "The binding path below is where to look.  Note it may sit"
            lappend lines "in a module this change did not touch: inheriting margin"
            lappend lines "on someone else's path and then losing it to placement is"
            lappend lines "a normal outcome, not evidence about the edit."
        }
        set obs [observed_board_clock]
        if {[llength $obs] == 0} {
            if {$met} {
                lappend lines "So this result says the LOGIC closes at the probed period."
                lappend lines "It does NOT say the board runs there: reaching it needs the"
                lappend lines "PS7 FCLK config changed and the achieved rate re-measured"
                lappend lines "from Clocks.fclk0_mhz on the board.  Until that measurement"
                lappend lines "exists, quote this as 'closes at <period> in implementation',"
                lappend lines "never as a board frequency."
            } else {
                lappend lines "No board measurement is supplied here, and none is called"
                lappend lines "for: a build that does not meet its own constraint has"
                lappend lines "nothing to carry to a board.  Close the timing first."
            }
        } elseif {!$met} {
            # A supplied board measurement plus a failed route is a
            # CONTRADICTION, not a pair of facts to print side by side.
            array set o $obs
            lappend lines "A BOARD MEASUREMENT WAS SUPPLIED FOR A BUILD THAT DOES NOT"
            lappend lines "CLOSE, AND THE TWO CANNOT BOTH BE ABOUT THIS RESULT:"
            lappend lines ""
            lappend lines "  observed Clocks.fclk0_mhz : $o(mhz)   <- OPERATOR-SUPPLIED"
            lappend lines "  evidence                  : $o(evidence)"
            lappend lines "  bitstream sha256          : $o(sha)"
            lappend lines ""
            lappend lines "The sha256 matches the bitstream beside this report, so the"
            lappend lines "measurement is about THIS image -- an image whose timing"
            lappend lines "was not met.  A part running at a rate its own static"
            lappend lines "timing analysis rejects is running out of specification;"
            lappend lines "it is not a closing build.  Do not quote $o(mhz) MHz as"
            lappend lines "this design's operating frequency on the strength of it."
        } else {
            array set o $obs
            lappend lines "It does not, on its own, say the board runs there."
            lappend lines ""
            lappend lines "A BOARD MEASUREMENT HAS BEEN SUPPLIED FOR THIS BITSTREAM:"
            lappend lines ""
            lappend lines "  observed Clocks.fclk0_mhz : $o(mhz)   <- OPERATOR-SUPPLIED"
            lappend lines "  evidence                  : $o(evidence)"
            lappend lines "  bitstream sha256          : $o(sha)"
            lappend lines ""
            lappend lines "That figure is NOT produced by this script and is NOT"
            lappend lines "verified by it.  The script checked one thing only: that"
            lappend lines "the sha256 above matches the bitstream handed off beside"
            lappend lines "this report, so a measurement cannot be carried over from"
            lappend lines "a different build.  Whether the evidence file supports"
            lappend lines "the number is for the reader to check."
            lappend lines ""
            lappend lines "With that caveat this build is ROUTED at $period ns and"
            lappend lines "OBSERVED at $o(mhz) MHz."
        }
        lappend lines ""
        lappend lines "The 32 ns slack line above is retained only for continuity"
        lappend lines "with the shipping image's report.  It is NOT the operating"
        lappend lines "point this build is about."
    }
    lappend lines ""
    lappend lines "Do NOT invert the data delay to get a maximum frequency."
    if {$met} {
        lappend lines "This build met its constraint with room to spare, at which"
        lappend lines "point the router stops trying -- the routing share below is"
        lappend lines "partly an artefact of a loose constraint, not a hard floor."
        lappend lines "Re-implement at the target period and read the result."
    } else {
        lappend lines "This build did NOT meet its constraint, so the usual caveat"
        lappend lines "-- that the router stopped early on a loose constraint --"
        lappend lines "does not apply: it tried and came up $wns ns short.  The"
        lappend lines "routing share below is therefore closer to a real picture of"
        lappend lines "where the time goes than it would be in a passing run.  It is"
        lappend lines "still not a maximum frequency: re-implement at the period you"
        lappend lines "care about and read that result."
    }
    lappend lines ""
    lappend lines "For comparison, the extractor's standalone image measured"
    lappend lines "WNS +10.144 ns against the same 20 ns constraint (budget"
    lappend lines "consumed 9.856 ns, data delay 9.073 ns, 45,918 endpoints,"
    lappend lines "all constraints met), binding on reset distribution."

    set text [join $lines "\n"]
    set fh [open [file join $out_dir post_route_wns.txt] w]
    puts $fh $text
    close $fh
    puts "\n$text\n"
    puts "full reports in $out_dir"
}

proc ::tme_standalone::verify_observed_binding {build_dir} {
    # Bind an operator-supplied board measurement to the bitstream being handed
    # off.  Called BEFORE the report is written, so a mismatch fails the run
    # instead of producing a note that misattributes a measurement.
    variable project_name
    variable bd_name

    set obs [observed_board_clock]
    if {[llength $obs] == 0} { return }
    array set o $obs

    set impl_dir [file join $build_dir $project_name.runs impl_1]
    set bit [file join $impl_dir ${bd_name}_wrapper.bit]
    if {![file exists $bit]} { fail "bitstream not found at $bit" }
    set actual [file_sha256 $bit]
    if {$actual eq ""} {
        fail "TME_OBSERVED_BITSTREAM_SHA256 was supplied but the bitstream hash could not be computed (certutil unavailable?).  Refusing to print an unverified board measurement."
    }
    if {$actual ne $o(sha)} {
        fail "TME_OBSERVED_BITSTREAM_SHA256 does not match the bitstream being handed off.  supplied: $o(sha)  actual: $actual  -- the supplied board measurement belongs to a different build."
    }
    puts "observed board clock: $o(mhz) MHz (operator-supplied), bitstream hash verified"
}


proc ::tme_standalone::copy_overlay_outputs {build_dir out_dir} {
    variable project_name
    variable bd_name

    set bit [file join $build_dir $project_name.runs impl_1 \
                 ${bd_name}_wrapper.bit]
    set hwh [file join $build_dir $project_name.gen sources_1 bd $bd_name \
                 hw_handoff ${bd_name}.hwh]

    # A missing artifact is FATAL, and the directory is emptied first.
    #
    # These two files are a matched pair: PYNQ pairs them by basename and
    # trusts the .hwh for the address map.  The previous version warned and
    # continued, which meant a build that produced only one of them left the
    # OTHER one behind from a previous build — and a stale .hwh beside a fresh
    # .bit is the worst possible outcome, because it loads cleanly and then
    # reads registers at the wrong offsets.  Fail instead.
    if {![file exists $bit]} { fail "bitstream not found at $bit" }
    if {![file exists $hwh]} { fail "hardware handoff not found at $hwh" }

    foreach {src label} [list $bit bitstream $hwh handoff] {
        # Both land as tme_standalone.* rather than tme_bd_wrapper.* /
        # tme_bd.*, so the basenames match.
        set dst [file join $out_dir $project_name[file extension $src]]
        file copy -force $src $dst
        puts "  $label -> $dst"
    }
    puts "\nCopy BOTH files to the board, keeping the shared basename, and run:"
    puts "  sudo -E python3 tme_standalone_bringup.py \\"
    puts "      --overlay ./$project_name.bit --data-dir ."
}

# -----------------------------------------------------------------------------

proc ::tme_standalone::main {} {
    variable project_name
    variable bd_name
    variable part_name
    variable hls_vlnv

    set repo_root [file normalize [file join [script_dir] .. ..]]
    set ip_repo [env_or TME_IP_REPO \
        [file join $repo_root hls template_match template_match_provisional \
             solution1 impl ip]]
    set build_root [env_or TME_BUILD_ROOT \
        {C:/Users/lychee/tc25/vivado_project/tme_standalone}]
    set do_bitstream [env_or TME_BUILD_BITSTREAM 1]
    set jobs [env_or TME_JOBS 12]
    set report_only [env_or TME_REPORT_ONLY 0]

    # Re-generate the reports and handoff from an EXISTING routed run, without
    # re-implementing.  This exists because the reports are generated by this
    # script: edit the reporting and the checked-in script silently disagrees
    # with the note sitting in overlay_output.  One flag beats a 7-minute
    # rebuild or a stale artifact.
    if {$report_only} {
        set xpr [file join $build_root $project_name.xpr]
        if {![file exists $xpr]} {
            fail "TME_REPORT_ONLY=1 but no project at $xpr — build it first."
        }
        open_project $xpr
        set out_dir [file join $build_root overlay_output]
        file mkdir $out_dir
        foreach stale [glob -nocomplain -directory $out_dir *] {
            file delete -force $stale
        }
        verify_observed_binding $build_root
        report_post_route_timing $out_dir
        copy_overlay_outputs $build_root $out_dir
        return
    }

    if {![file isdirectory $ip_repo]} {
        fail "IP repository not found: $ip_repo\nRun package_provisional.tcl \
              first, or set TME_IP_REPO."
    }

    puts "project    : $project_name"
    puts "part       : $part_name"
    puts "IP repo    : $ip_repo"
    puts "core VLNV  : $hls_vlnv"
    puts "build root : $build_root"
    puts "bitstream  : $do_bitstream"

    file mkdir $build_root
    create_project -force $project_name $build_root -part $part_name
    set_property ip_repo_paths [list $ip_repo] [current_project]
    update_ip_catalog -rebuild

    build_bd

    set bd_file [get_files ${bd_name}.bd]
    make_wrapper -files $bd_file -top
    add_files -norecurse [file join $build_root $project_name.gen sources_1 \
                              bd $bd_name hdl ${bd_name}_wrapper.v]
    set_property top ${bd_name}_wrapper [current_fileset]
    update_compile_order -fileset sources_1

    if {!$do_bitstream} {
        puts "\nBD built and validated. TME_BUILD_BITSTREAM=0, stopping before \
              synthesis."
        return
    }

    launch_runs synth_1 -jobs $jobs
    wait_on_run synth_1
    if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
        fail "synthesis failed — see [get_property DIRECTORY [get_runs synth_1]]"
    }

    launch_runs impl_1 -to_step write_bitstream -jobs $jobs
    wait_on_run impl_1
    if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
        fail "implementation failed — see [get_property DIRECTORY [get_runs impl_1]]"
    }

    # Purge the handoff directory BEFORE writing anything into it, so a
    # previous build's .bit/.hwh/report cannot survive alongside this one's.
    # (Doing this in copy_overlay_outputs instead would delete the reports
    # written moments earlier.)
    set out_dir [file join $build_root overlay_output]
    file mkdir $out_dir
    foreach stale [glob -nocomplain -directory $out_dir *] {
        file delete -force $stale
    }
    verify_observed_binding $build_root
    report_post_route_timing $out_dir
    copy_overlay_outputs $build_root $out_dir
}

::tme_standalone::main
