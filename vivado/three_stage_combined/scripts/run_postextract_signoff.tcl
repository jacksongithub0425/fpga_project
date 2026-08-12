# Synthesize and route the staged matcher+binarizer+extractor design, stopping
# at route_design.  A passing run emits @@POSTEXTRACT_ROUTE_SIGNOFF_PASS@@ and
# writes a routed DCP plus the full report set.  It never requests a bitstream.

namespace eval ::postextract {
    variable project_dir [file normalize [file join [file dirname [info script]] ..]]
    variable project_file [file join $project_dir three_stage_combined.xpr]
    variable bd_file [file join $project_dir three_stage_combined.srcs sources_1 bd tme_bd tme_bd.bd]
    variable jobs 8
}

proc ::postextract::fail {message} {
    puts stderr "\nPOSTEXTRACT_SIGNOFF_ERROR: $message\n"
    return -code error $message
}

proc ::postextract::one {label objects} {
    if {[llength $objects] != 1} {
        fail "$label: expected exactly one object, got [llength $objects]: $objects"
    }
    return [lindex $objects 0]
}

proc ::postextract::expect_text {label got expected} {
    if {$got ne $expected} {
        fail "$label: expected '$expected', got '$got'"
    }
}

proc ::postextract::expect_int {label got expected} {
    if {[expr {wide($got)}] != [expr {wide($expected)}]} {
        fail "$label: expected $expected, got $got"
    }
}

proc ::postextract::cell {name expected_vlnv} {
    set obj [one "BD cell $name" [get_bd_cells -quiet $name]]
    expect_text "$name VLNV" [get_property VLNV $obj] $expected_vlnv
    return $obj
}

proc ::postextract::addr {path expected_offset expected_range} {
    set seg [get_bd_addr_segs -quiet $path]
    if {[llength $seg] != 1} {
        fail "address segment $path: expected one object, got [llength $seg]: $seg"
    }
    expect_int "$path offset" [get_property OFFSET $seg] $expected_offset
    expect_int "$path range" [get_property RANGE $seg] $expected_range
}

proc ::postextract::same_intf_net {a b} {
    set a_pin [one "interface pin $a" [get_bd_intf_pins -quiet $a]]
    set b_pin [one "interface pin $b" [get_bd_intf_pins -quiet $b]]
    set a_net [one "interface net on $a" [get_bd_intf_nets -quiet -of_objects $a_pin]]
    set b_net [one "interface net on $b" [get_bd_intf_nets -quiet -of_objects $b_pin]]
    if {$a_net ne $b_net} {
        fail "interfaces are not on the same net: $a ($a_net), $b ($b_net)"
    }
}

proc ::postextract::same_pin_net {a b} {
    set a_pin [one "pin $a" [get_bd_pins -quiet $a]]
    set b_pin [one "pin $b" [get_bd_pins -quiet $b]]
    set a_net [one "net on $a" [get_bd_nets -quiet -of_objects $a_pin]]
    set b_net [one "net on $b" [get_bd_nets -quiet -of_objects $b_pin]]
    if {$a_net ne $b_net} {
        fail "pins are not on the same net: $a ($a_net), $b ($b_net)"
    }
}

proc ::postextract::read_file {path} {
    set f [open $path r]
    try {
        return [read $f]
    } finally {
        close $f
    }
}

proc ::postextract::route_count {text label} {
    foreach line [split $text "\n"] {
        if {[string first $label $line] >= 0 &&
            [regexp {:[[:space:]]*([0-9]+)[[:space:]]*:[[:space:]]*$} $line -> count]} {
            return $count
        }
    }
    fail "could not parse '$label' from report_route_status"
}

proc ::postextract::timing_summary {text} {
    set saw_header 0
    foreach line [split $text "\n"] {
        if {[string first "WNS(ns)" $line] >= 0 &&
            [string first "WHS(ns)" $line] >= 0 &&
            [string first "WPWS(ns)" $line] >= 0} {
            set saw_header 1
            continue
        }
        if {$saw_header && [regexp {^[[:space:]]*([-+]?[0-9.]+)[[:space:]]+([-+]?[0-9.]+)[[:space:]]+([0-9]+)[[:space:]]+([0-9]+)[[:space:]]+([-+]?[0-9.]+)[[:space:]]+([-+]?[0-9.]+)[[:space:]]+([0-9]+)[[:space:]]+([0-9]+)[[:space:]]+([-+]?[0-9.]+)[[:space:]]+([-+]?[0-9.]+)[[:space:]]+([0-9]+)[[:space:]]+([0-9]+)[[:space:]]*$} \
                $line -> wns tns tns_fail tns_total whs ths ths_fail ths_total wpws tpws tpws_fail tpws_total]} {
            return [list $wns $tns $tns_fail $tns_total \
                         $whs $ths $ths_fail $ths_total \
                         $wpws $tpws $tpws_fail $tpws_total]
        }
    }
    fail "could not parse the Design Timing Summary numeric row"
}

proc ::postextract::bad_violations {objects} {
    set bad {}
    foreach v $objects {
        set severity [string toupper [get_property SEVERITY $v]]
        if {$severity eq "FATAL" || $severity eq "ERROR" ||
            $severity eq "CRITICAL WARNING"} {
            lappend bad $v
        }
    }
    return $bad
}

proc ::postextract::bram_counts {} {
    set ramb18 [llength [get_cells -hierarchical -quiet -filter {REF_NAME == RAMB18E1}]]
    set ramb36 [llength [get_cells -hierarchical -quiet -filter {REF_NAME == RAMB36E1}]]
    set fifo18 [llength [get_cells -hierarchical -quiet -filter {REF_NAME == FIFO18E1}]]
    set fifo36 [llength [get_cells -hierarchical -quiet -filter {REF_NAME == FIFO36E1}]]
    set bram18eq [expr {$ramb18 + $fifo18 + 2 * ($ramb36 + $fifo36)}]
    return [list $ramb18 $ramb36 $fifo18 $fifo36 $bram18eq]
}

proc ::postextract::check_timing_categories {path} {
    set text [read_file $path]
    set expected {
        no_clock constant_clock pulse_width_clock
        unconstrained_internal_endpoints no_input_delay no_output_delay
        multiple_clock generated_clocks loops partial_input_delay
        partial_output_delay latch_loops
    }
    set seen [dict create]
    set findings {}
    foreach line [split $text "\n"] {
        if {[regexp {^[[:space:]]*[0-9]+\.[[:space:]]+checking[[:space:]]+([^[:space:]]+)[[:space:]]+\(([0-9]+)\)} \
                $line -> category count]} {
            dict set seen $category 1
            if {$count != 0} {
                lappend findings "$category=$count"
            }
        }
    }
    set missing {}
    foreach category $expected {
        if {![dict exists $seen $category]} {
            lappend missing $category
        }
    }
    if {[llength $missing] != 0} {
        fail "check_timing omitted required categories: $missing"
    }
    if {[llength $findings] != 0} {
        fail "nonzero check_timing findings: $findings"
    }
}

proc ::postextract::check_timing_metrics {path} {
    lassign [timing_summary [read_file $path]] \
        wns tns tns_fail tns_total \
        whs ths ths_fail ths_total \
        wpws tpws tpws_fail tpws_total
    if {double($wns) < 0.0 || abs(double($tns)) > 1.0e-9 || $tns_fail != 0} {
        fail "setup timing failed: WNS=$wns TNS=$tns failing_endpoints=$tns_fail"
    }
    if {double($whs) < 0.0 || abs(double($ths)) > 1.0e-9 || $ths_fail != 0} {
        fail "hold timing failed: WHS=$whs THS=$ths failing_endpoints=$ths_fail"
    }
    if {double($wpws) < 0.0 || abs(double($tpws)) > 1.0e-9 || $tpws_fail != 0} {
        fail "pulse-width timing failed: WPWS=$wpws TPWS=$tpws failing_endpoints=$tpws_fail"
    }
    if {$tns_total <= 0 || $ths_total <= 0 || $tpws_total <= 0} {
        fail "timing summary has an empty check universe: setup=$tns_total hold=$ths_total pulse=$tpws_total"
    }
    return [list $wns $tns $whs $ths $wpws $tpws]
}

proc ::postextract::preflight_bd {{persist_generated_state 1}} {
    variable project_dir
    variable bd_file

    open_bd_design $bd_file
    current_bd_instance /

    set ps [cell processing_system7_0 xilinx.com:ip:processing_system7:5.5]
    cell proc_sys_reset_0      xilinx.com:ip:proc_sys_reset:5.0
    cell smartconnect_lite     xilinx.com:ip:smartconnect:1.0
    cell smartconnect_mem      xilinx.com:ip:smartconnect:1.0
    cell smartconnect_bin_mem  xilinx.com:ip:smartconnect:1.0
    cell smartconnect_pe_mem   xilinx.com:ip:smartconnect:1.0
    cell axi_dma_patch         xilinx.com:ip:axi_dma:7.1
    cell axi_dma_templ         xilinx.com:ip:axi_dma:7.1
    set bin_dma [cell axi_dma_binarize xilinx.com:ip:axi_dma:7.1]
    set pe_data [cell dma_pe_data xilinx.com:ip:axi_dma:7.1]
    set pe_meta [cell dma_pe_meta xilinx.com:ip:axi_dma:7.1]
    cell tme_top_0             TermCount:hls:tme_top:0.2
    cell binarize_core_0       TermCount:hls:binarize_core:2.0
    cell patch_extract_core_0  TermCount:hls:patch_extract_core:0.1

    expect_int "total AXI DMA count" \
        [llength [get_bd_cells -quiet -hierarchical -filter {VLNV =~ "xilinx.com:ip:axi_dma:*"}]] 5
    expect_int "total HLS core count" \
        [llength [get_bd_cells -quiet -hierarchical -filter {VLNV =~ "*:hls:*"}]] 3

    foreach hp {0 1 2} {
        expect_int "HP${hp} enabled" [get_property CONFIG.PCW_USE_S_AXI_HP${hp} $ps] 1
        expect_int "HP${hp} width" [get_property CONFIG.PCW_S_AXI_HP${hp}_DATA_WIDTH $ps] 64
    }
    expect_int "HP3 disabled" [get_property CONFIG.PCW_USE_S_AXI_HP3 $ps] 0
    expect_int "FCLK0 MHz" [get_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ $ps] 50
    expect_int "control SmartConnect MI count" \
        [get_property CONFIG.NUM_MI [get_bd_cells smartconnect_lite]] 8
    expect_int "extractor SmartConnect SI count" \
        [get_property CONFIG.NUM_SI [get_bd_cells smartconnect_pe_mem]] 4
    expect_int "extractor SmartConnect MI count" \
        [get_property CONFIG.NUM_MI [get_bd_cells smartconnect_pe_mem]] 1
    expect_int "HP2 ID width" [get_property CONFIG.PCW_S_AXI_HP2_ID_WIDTH $ps] 6
    expect_int "binarizer DMA length width" [get_property CONFIG.c_sg_length_width $bin_dma] 26

    foreach {label obj prop expected} [list \
        pe_data_mm2s $pe_data CONFIG.c_include_mm2s 1 \
        pe_data_s2mm $pe_data CONFIG.c_include_s2mm 1 \
        pe_data_addr $pe_data CONFIG.c_addr_width 32 \
        pe_data_sg $pe_data CONFIG.c_include_sg 0 \
        pe_data_mm2s_dre $pe_data CONFIG.c_include_mm2s_dre 0 \
        pe_data_s2mm_dre $pe_data CONFIG.c_include_s2mm_dre 0 \
        pe_data_mm2s_sf $pe_data CONFIG.c_include_mm2s_sf 1 \
        pe_data_s2mm_sf $pe_data CONFIG.c_include_s2mm_sf 1 \
        pe_data_len $pe_data CONFIG.c_sg_length_width 18 \
        pe_data_mm2s_mem $pe_data CONFIG.c_m_axi_mm2s_data_width 64 \
        pe_data_s2mm_mem $pe_data CONFIG.c_m_axi_s2mm_data_width 32 \
        pe_data_mm_axis $pe_data CONFIG.c_m_axis_mm2s_tdata_width 64 \
        pe_data_s_axis $pe_data CONFIG.c_s_axis_s2mm_tdata_width 8 \
        pe_data_mm2s_burst $pe_data CONFIG.c_mm2s_burst_size 16 \
        pe_data_s2mm_burst $pe_data CONFIG.c_s2mm_burst_size 16 \
        pe_data_micro $pe_data CONFIG.c_micro_dma 0 \
        pe_data_single $pe_data CONFIG.c_single_interface 0 \
        pe_meta_mm2s $pe_meta CONFIG.c_include_mm2s 0 \
        pe_meta_s2mm $pe_meta CONFIG.c_include_s2mm 1 \
        pe_meta_addr $pe_meta CONFIG.c_addr_width 32 \
        pe_meta_sg $pe_meta CONFIG.c_include_sg 0 \
        pe_meta_s2mm_dre $pe_meta CONFIG.c_include_s2mm_dre 0 \
        pe_meta_s2mm_sf $pe_meta CONFIG.c_include_s2mm_sf 1 \
        pe_meta_len $pe_meta CONFIG.c_sg_length_width 18 \
        pe_meta_mem_width $pe_meta CONFIG.c_m_axi_s2mm_data_width 128 \
        pe_meta_axis_width $pe_meta CONFIG.c_s_axis_s2mm_tdata_width 128 \
        pe_meta_s2mm_burst $pe_meta CONFIG.c_s2mm_burst_size 16 \
        pe_meta_micro $pe_meta CONFIG.c_micro_dma 0 \
        pe_meta_single $pe_meta CONFIG.c_single_interface 0] {
        expect_int $label [get_property $prop $obj] $expected
    }

    foreach {a b} {
        dma_pe_data/M_AXIS_MM2S patch_extract_core_0/cand_in
        patch_extract_core_0/patch_out dma_pe_data/S_AXIS_S2MM
        patch_extract_core_0/meta_out dma_pe_meta/S_AXIS_S2MM
        dma_pe_data/M_AXI_MM2S smartconnect_pe_mem/S00_AXI
        dma_pe_data/M_AXI_S2MM smartconnect_pe_mem/S01_AXI
        dma_pe_meta/M_AXI_S2MM smartconnect_pe_mem/S02_AXI
        patch_extract_core_0/m_axi_BIN_IMG smartconnect_pe_mem/S03_AXI
        smartconnect_pe_mem/M00_AXI processing_system7_0/S_AXI_HP2
        smartconnect_lite/M05_AXI patch_extract_core_0/s_axi_CTRL
        smartconnect_lite/M06_AXI dma_pe_data/S_AXI_LITE
        smartconnect_lite/M07_AXI dma_pe_meta/S_AXI_LITE
    } {
        same_intf_net $a $b
    }

    foreach sink {
        processing_system7_0/S_AXI_HP2_ACLK
        patch_extract_core_0/ap_clk
        dma_pe_data/s_axi_lite_aclk
        dma_pe_data/m_axi_mm2s_aclk
        dma_pe_data/m_axi_s2mm_aclk
        dma_pe_meta/s_axi_lite_aclk
        dma_pe_meta/m_axi_s2mm_aclk
        smartconnect_pe_mem/aclk
    } {
        same_pin_net processing_system7_0/FCLK_CLK0 $sink
    }
    foreach sink {
        patch_extract_core_0/ap_rst_n
        dma_pe_data/axi_resetn
        dma_pe_meta/axi_resetn
        smartconnect_pe_mem/aresetn
    } {
        same_pin_net proc_sys_reset_0/peripheral_aresetn $sink
    }

    foreach {path offset range} {
        processing_system7_0/Data/SEG_tme_top_0_Reg                  0x40000000 0x00010000
        processing_system7_0/Data/SEG_binarize_core_0_Reg            0x40010000 0x00010000
        processing_system7_0/Data/SEG_patch_extract_core_0_Reg       0x40020000 0x00010000
        processing_system7_0/Data/SEG_axi_dma_patch_Reg              0x41E00000 0x00010000
        processing_system7_0/Data/SEG_axi_dma_templ_Reg              0x41E10000 0x00010000
        processing_system7_0/Data/SEG_axi_dma_binarize_Reg           0x41E20000 0x00010000
        processing_system7_0/Data/SEG_dma_pe_data_Reg                0x41E30000 0x00010000
        processing_system7_0/Data/SEG_dma_pe_meta_Reg                0x41E40000 0x00010000
        dma_pe_data/Data_MM2S/SEG_processing_system7_0_HP2_DDR_LOWOCM 0x00000000 0x20000000
        dma_pe_data/Data_S2MM/SEG_processing_system7_0_HP2_DDR_LOWOCM 0x00000000 0x20000000
        dma_pe_meta/Data_S2MM/SEG_processing_system7_0_HP2_DDR_LOWOCM 0x00000000 0x20000000
        patch_extract_core_0/Data_m_axi_BIN_IMG/SEG_processing_system7_0_HP2_DDR_LOWOCM 0x00000000 0x20000000
    } {
        addr $path $offset $range
    }

    validate_bd_design
    set snapshot ""
    if {$persist_generated_state} {
        save_bd_design
        set snapshot [file join $project_dir snapshots \
            "tme_bd_post_patch_extract_pre_synth_[clock format [clock seconds] -format %Y%m%d_%H%M%S].tcl"]
        write_bd_tcl -force $snapshot

        set bd_obj [one "registered tme_bd" [get_files -all $bd_file]]
        generate_target all $bd_obj
        export_ip_user_files -of_objects $bd_obj -no_script -sync -force -quiet
        update_compile_order -fileset sources_1

    }
    close_bd_design [current_bd_design]

    puts "POSTEXTRACT_SAVED_SIGNATURE=BD:tme_bd;TME:0.2;BIN:2.0;PE:0.1;DMA:5;CTRL_MI:8;HP0:64;HP1:64;HP2:64;BIN_DMA_LEN:26;PE_DMA_LEN:18"
    if {$persist_generated_state} {
        puts "POSTEXTRACT_BD_SNAPSHOT=$snapshot"
        puts "@@POSTEXTRACT_SAVED_STATE_PASS@@"
    } else {
        puts "@@POSTEXTRACT_PRESERVED_BD_PREFLIGHT_PASS@@"
    }
}

proc ::postextract::run_synthesis {} {
    variable project_dir
    variable bd_file
    variable jobs

    set synth_run [one "synth_1 run" [get_runs -quiet synth_1]]
    set impl_run [one "impl_1 run" [get_runs -quiet impl_1]]
    foreach run [list $synth_run $impl_run] {
        set status [get_property STATUS $run]
        if {[string match "*Running*" $status] || [string match "*Queued*" $status]} {
            fail "run [get_property NAME $run] is already active: $status"
        }
    }

    reset_run $impl_run
    reset_run $synth_run
    set launch_epoch [clock seconds]
    launch_runs $synth_run -jobs $jobs
    wait_on_run $synth_run

    set progress [get_property PROGRESS $synth_run]
    set status [get_property STATUS $synth_run]
    puts "SYNTH_PROGRESS=$progress"
    puts "SYNTH_STATUS=$status"
    if {$progress ne "100%" || ![string match "*Complete*" $status]} {
        fail "synthesis failed or stopped early: $status ($progress)"
    }
    if {[lsearch -exact [list_property $synth_run] NEEDS_REFRESH] >= 0 &&
        [get_property NEEDS_REFRESH $synth_run]} {
        fail "synth_1 is marked NEEDS_REFRESH after completion"
    }

    set synth_dir [file normalize [get_property DIRECTORY $synth_run]]
    set expected_dir [file normalize [file join $project_dir three_stage_combined.runs synth_1]]
    if {![string equal -nocase $synth_dir $expected_dir]} {
        fail "synth_1 points outside the active project: $synth_dir"
    }
    set synth_dcp [file join $synth_dir tme_bd_wrapper.dcp]
    if {![file exists $synth_dcp] || [file size $synth_dcp] == 0} {
        fail "fresh synthesis checkpoint is missing: $synth_dcp"
    }
    if {[file mtime $synth_dcp] < [file mtime $bd_file] ||
        [file mtime $synth_dcp] < $launch_epoch} {
        fail "synthesis checkpoint is stale relative to this launch or saved BD"
    }

    open_run $synth_run -name synth_1
    foreach name {
        tme_bd_i/patch_extract_core_0
        tme_bd_i/dma_pe_data
        tme_bd_i/dma_pe_meta
        tme_bd_i/smartconnect_pe_mem
    } {
        one "synthesized hierarchy cell $name" [get_cells -quiet $name]
    }
    set blackboxes [get_cells -hierarchical -quiet -filter {IS_BLACKBOX == 1}]
    if {[llength $blackboxes] != 0} {
        fail "synthesized design contains black-box cells: $blackboxes"
    }

    set report_dir [file join $project_dir \
        "postextract_synth_reports_[clock format [clock seconds] -format %Y%m%d_%H%M%S]"]
    file mkdir $report_dir
    set timing_rpt [file join $report_dir postextract_synth_timing_summary.rpt]
    set constraints_rpt [file join $report_dir postextract_synth_constraint_checks.rpt]
    set drc_rpt [file join $report_dir postextract_synth_drc.rpt]
    set util_rpt [file join $report_dir postextract_synth_utilization.rpt]
    set util_hier_rpt [file join $report_dir postextract_synth_utilization_hier.rpt]
    set clocks_rpt [file join $report_dir postextract_synth_clocks.rpt]
    set copy_dcp [file join $report_dir postextract_synth.dcp]

    report_timing_summary -delay_type min_max -check_timing_verbose \
        -report_unconstrained -max_paths 10 -nworst 1 -input_pins -file $timing_rpt
    check_timing -verbose -file $constraints_rpt
    report_clocks -file $clocks_rpt
    report_drc -no_waivers -file $drc_rpt
    report_utilization -file $util_rpt
    report_utilization -hierarchical -hierarchical_depth 4 \
        -hierarchical_min_primitive_count 0 -file $util_hier_rpt

    set bad_drc [bad_violations [get_drc_violations -quiet]]
    if {[llength $bad_drc] != 0} {
        fail "post-synthesis DRC has Fatal/Error/Critical Warning violations: $bad_drc"
    }
    check_timing_categories $constraints_rpt
    set timing [check_timing_metrics $timing_rpt]
    lassign [bram_counts] ramb18 ramb36 fifo18 fifo36 bram18eq
    if {$bram18eq <= 0 || $bram18eq > 280} {
        fail "post-synthesis BRAM demand is invalid or over capacity: $bram18eq/280 BRAM18-equivalents"
    }

    write_checkpoint -force $copy_dcp
    foreach f [list $timing_rpt $constraints_rpt $drc_rpt $util_rpt \
                    $util_hier_rpt $clocks_rpt $copy_dcp] {
        if {![file exists $f] || [file size $f] == 0} {
            fail "expected non-empty synthesis artifact is missing: $f"
        }
    }

    lassign $timing wns tns whs ths wpws tpws
    puts "POSTSYNTH_TIMING=WNS:$wns;TNS:$tns;WHS:$whs;THS:$ths;WPWS:$wpws;TPWS:$tpws"
    puts "POSTSYNTH_BRAM=RAMB36E1:$ramb36;FIFO36E1:$fifo36;RAMB18E1:$ramb18;FIFO18E1:$fifo18;BRAM18_EQ:$bram18eq/280;TILES:[format %.1f [expr {$bram18eq / 2.0}]]/140.0"
    puts "POSTSYNTH_REPORT_DIR=$report_dir"
    puts "POSTSYNTH_DCP=$copy_dcp"
    puts "@@POSTEXTRACT_SYNTH_SIGNOFF_PASS@@"
    close_design
}

proc ::postextract::check_congestion {path} {
    set text [read_file $path]
    if {![regexp -line -nocase {^[[:space:]]*\|[[:space:]]*Design[[:space:]]+State[[:space:]]*:[[:space:]]*Routed[[:space:]]*$} $text]} {
        fail "congestion report does not describe a routed design: $path"
    }

    array set header_count {placer 0 router 0}
    array set level_col {placer -1 router -1}
    array set direction_col {placer -1 router -1}
    array set rows {placer 0 router 0}
    set section ""
    set max_level 0
    foreach raw [split $text "\n"] {
        set line [string trim $raw]
        if {[regexp -nocase {^[[:space:]]*1\.[[:space:]]+Placer Final Level Congestion Reporting} $line]} {
            set section placer
            continue
        }
        if {[regexp -nocase {^[[:space:]]*2\.[[:space:]]+Initial Estimated Router Congestion Reporting} $line]} {
            set section router
            continue
        }
        if {$section eq "" || ![string match {|*|} $line]} { continue }
        set fields {}
        foreach field [lrange [split $line {|}] 1 end-1] {
            lappend fields [string trim $field " \t\r\n\""]
        }
        set found_direction -1
        set found_level -1
        for {set i 0} {$i < [llength $fields]} {incr i} {
            set field [lindex $fields $i]
            if {[string equal -nocase $field Direction]} { set found_direction $i }
            if {[regexp -nocase {^(Congestion[[:space:]]+)?Level$} $field]} { set found_level $i }
        }
        if {$found_direction >= 0 && $found_level >= 0} {
            incr header_count($section)
            set direction_col($section) $found_direction
            set level_col($section) $found_level
            continue
        }
        if {$direction_col($section) < 0 || $level_col($section) < 0 ||
            [llength $fields] <= $direction_col($section) ||
            [llength $fields] <= $level_col($section)} { continue }
        set direction [lindex $fields $direction_col($section)]
        if {![regexp -nocase {^(North|South|East|West)([[:space:]].*)?$} $direction]} { continue }
        set level [lindex $fields $level_col($section)]
        if {![string is integer -strict $level] || $level < 3 || $level > 8} {
            fail "recognized $section congestion row has invalid level '$level': $raw"
        }
        incr rows($section)
        if {$level > $max_level} { set max_level $level }
    }

    array set no_window [list \
        placer [regexp -all -line -nocase {^[[:space:]]*\*[[:space:]]+No congestion windows are found above level 3[[:space:]]*$} $text] \
        router [regexp -all -line -nocase {^[[:space:]]*\*[[:space:]]+No initial estimated congestion windows are found above level 3[[:space:]]*$} $text]]
    foreach section {placer router} {
        if {$header_count($section) != 1} {
            fail "$section congestion evidence has $header_count($section) recognized headers; expected one"
        }
        if {$rows($section) == 0 && $no_window($section) != 1} {
            fail "$section congestion table has no valid rows and no unique no-window marker"
        }
        if {$rows($section) != 0 && $no_window($section) != 0} {
            fail "$section congestion table has both rows and a no-window marker"
        }
    }
    set reported_rows [expr {$rows(placer) + $rows(router)}]
    if {$reported_rows != 0} {
        fail "congestion hold: found $reported_rows window(s) above level 3; maximum reported level is $max_level"
    }
    return LE3
}

proc ::postextract::run_route {{emit_route_only_bitstream_token 1}} {
    variable project_dir
    variable bd_file
    variable jobs

    set synth_run [one "synth_1 run" [get_runs -quiet synth_1]]
    set impl_run [one "impl_1 run" [get_runs -quiet impl_1]]
    set synth_dcp [file join [get_property DIRECTORY $synth_run] tme_bd_wrapper.dcp]

    set launch_epoch [clock seconds]
    launch_runs $impl_run -to_step route_design -jobs $jobs
    puts "IMPLEMENTATION_TARGET=route_design"
    puts "BITSTREAM_REQUESTED=0"
    wait_on_run $impl_run

    set progress [get_property PROGRESS $impl_run]
    set status [get_property STATUS $impl_run]
    puts "IMPL_PROGRESS=$progress"
    puts "IMPL_STATUS=$status"
    if {$progress ne "100%" || ![string match "*Complete*" $status]} {
        fail "implementation failed or stopped before route completion: $status ($progress)"
    }
    if {[string match -nocase "*write_bitstream*" $status]} {
        fail "implementation advanced through write_bitstream: $status"
    }

    set impl_dir [file normalize [get_property DIRECTORY $impl_run]]
    set expected_dir [file normalize [file join $project_dir three_stage_combined.runs impl_1]]
    if {![string equal -nocase $impl_dir $expected_dir]} {
        fail "impl_1 points outside the active project: $impl_dir"
    }
    set routed_dcp [file join $impl_dir tme_bd_wrapper_routed.dcp]
    if {![file exists $routed_dcp] || [file size $routed_dcp] == 0} {
        fail "routed implementation checkpoint is missing: $routed_dcp"
    }
    if {[file mtime $routed_dcp] < [file mtime $synth_dcp] ||
        [file mtime $routed_dcp] < [file mtime $bd_file] ||
        [file mtime $routed_dcp] < $launch_epoch} {
        fail "routed checkpoint is stale relative to this route, synthesis, or saved BD"
    }
    set forbidden [concat \
        [glob -nocomplain -directory $impl_dir *.bit] \
        [glob -nocomplain -directory $impl_dir *.bin]]
    if {[llength $forbidden] != 0} {
        fail "route-only run unexpectedly contains bitstream products: $forbidden"
    }

    open_run $impl_run -name impl_1
    foreach name {
        tme_bd_i/patch_extract_core_0
        tme_bd_i/dma_pe_data
        tme_bd_i/dma_pe_meta
        tme_bd_i/smartconnect_pe_mem
    } {
        one "routed hierarchy cell $name" [get_cells -quiet $name]
    }
    set blackboxes [get_cells -hierarchical -quiet -filter {IS_BLACKBOX == 1}]
    if {[llength $blackboxes] != 0} {
        fail "routed design contains black-box cells: $blackboxes"
    }

    set report_dir [file join $project_dir \
        "postextract_route_reports_[clock format [clock seconds] -format %Y%m%d_%H%M%S]"]
    file mkdir $report_dir
    set route_rpt [file join $report_dir postextract_route_status.rpt]
    set timing_rpt [file join $report_dir postextract_route_timing_summary.rpt]
    set pulse_rpt [file join $report_dir postextract_route_pulse_width.rpt]
    set constraints_rpt [file join $report_dir postextract_route_constraint_checks.rpt]
    set clocks_rpt [file join $report_dir postextract_route_clocks.rpt]
    set drc_rpt [file join $report_dir postextract_route_drc.rpt]
    set methodology_rpt [file join $report_dir postextract_route_methodology.rpt]
    set util_rpt [file join $report_dir postextract_route_utilization.rpt]
    set util_hier_rpt [file join $report_dir postextract_route_utilization_hier.rpt]
    set congestion_rpt [file join $report_dir postextract_route_congestion.rpt]
    set copy_dcp [file join $report_dir postextract_routed.dcp]

    report_route_status -ignore_cache -file $route_rpt
    report_timing_summary -delay_type min_max -check_timing_verbose \
        -report_unconstrained -max_paths 10 -nworst 1 -input_pins -file $timing_rpt
    report_pulse_width -all_violators -file $pulse_rpt
    check_timing -verbose -file $constraints_rpt
    report_clocks -file $clocks_rpt
    report_drc -no_waivers -file $drc_rpt
    report_methodology -no_waivers -file $methodology_rpt
    report_utilization -file $util_rpt
    report_utilization -hierarchical -hierarchical_depth 4 \
        -hierarchical_min_primitive_count 0 -file $util_hier_rpt
    report_design_analysis -congestion -min_congestion_level 3 -file $congestion_rpt

    foreach f [list $route_rpt $timing_rpt $pulse_rpt $constraints_rpt \
                    $clocks_rpt $drc_rpt $methodology_rpt $util_rpt \
                    $util_hier_rpt $congestion_rpt] {
        if {![file exists $f] || [file size $f] == 0} {
            fail "expected non-empty post-route report is missing: $f"
        }
    }

    set route_text [read_file $route_rpt]
    set routable [route_count $route_text "# of routable nets"]
    set fully_routed [route_count $route_text "# of fully routed nets"]
    set route_errors [route_count $route_text "# of nets with routing errors"]
    set route_bool_full [report_route_status -ignore_cache -boolean_check ROUTED_FULLY]
    set route_bool_partial [report_route_status -ignore_cache -boolean_check PARTIALLY_ROUTED]
    set route_bool_errors [report_route_status -ignore_cache -boolean_check ERRORS_IN_ROUTES]
    if {$routable <= 0 || $fully_routed != $routable || $route_errors != 0 ||
        !$route_bool_full || $route_bool_partial || $route_bool_errors} {
        fail "routing incomplete: routable=$routable fully=$fully_routed errors=$route_errors booleans=$route_bool_full/$route_bool_partial/$route_bool_errors"
    }
    set bad_route_nets {}
    foreach route_type {UNPLACED UNROUTED PARTIAL GAPS CONFLICTS ANTENNAS NODRIVER MULTI_DRIVER} {
        set found [report_route_status -return_nets -route_type $route_type]
        if {[llength $found] != 0} { lappend bad_route_nets "$route_type=[llength $found]" }
    }
    if {[llength $bad_route_nets] != 0} {
        fail "route-status problem nets remain: $bad_route_nets"
    }

    check_timing_categories $constraints_rpt
    set timing [check_timing_metrics $timing_rpt]
    lassign $timing wns tns whs ths wpws tpws

    set setup_path [get_timing_paths -quiet -delay_type max -sort_by slack \
        -max_paths 1 -nworst 1 -no_report_unconstrained]
    set hold_path [get_timing_paths -quiet -delay_type min -sort_by slack \
        -max_paths 1 -nworst 1 -no_report_unconstrained]
    one "worst setup path" $setup_path
    one "worst hold path" $hold_path
    set setup_slack [get_property SLACK $setup_path]
    set hold_slack [get_property SLACK $hold_path]
    if {double($setup_slack) < 0.0 || double($hold_slack) < 0.0} {
        fail "worst timing path failed: setup=$setup_slack hold=$hold_slack"
    }

    set clk [one "clk_fpga_0 timing clock" [get_clocks -quiet clk_fpga_0]]
    if {[llength [get_clocks -quiet]] != 1} {
        fail "expected exactly one timing clock; got [get_clocks -quiet]"
    }
    set clk_period [get_property PERIOD $clk]
    if {abs(double($clk_period) - 20.0) > 0.001} {
        fail "clk_fpga_0 period changed: expected 20.000 ns, got $clk_period"
    }

    set drc_objects [get_drc_violations -quiet]
    set bad_drc [bad_violations $drc_objects]
    if {[llength $bad_drc] != 0} {
        fail "post-route DRC has Fatal/Error/Critical Warning violations: $bad_drc"
    }
    set methodology_objects [get_methodology_violations -quiet]
    set bad_methodology [bad_violations $methodology_objects]
    if {[llength $bad_methodology] != 0} {
        fail "methodology has Fatal/Error/Critical Warning violations: $bad_methodology"
    }
    set congestion_label [check_congestion $congestion_rpt]

    lassign [bram_counts] ramb18 ramb36 fifo18 fifo36 bram18eq
    if {$bram18eq <= 0 || $bram18eq > 280} {
        fail "post-route BRAM demand is invalid or over capacity: $bram18eq/280 BRAM18-equivalents"
    }
    set bram_headroom [expr {280 - $bram18eq}]

    write_checkpoint -force $copy_dcp
    if {![file exists $copy_dcp] || [file size $copy_dcp] == 0} {
        fail "accepted routed checkpoint copy was not written: $copy_dcp"
    }

    puts "POSTROUTE_ROUTE=routable:$routable;fully_routed:$fully_routed;errors:$route_errors;boolean_full:$route_bool_full;boolean_partial:$route_bool_partial;boolean_errors:$route_bool_errors"
    puts "POSTROUTE_TIMING=WNS:$wns;TNS:$tns;WHS:$whs;THS:$ths;WPWS:$wpws;TPWS:$tpws;SETUP_PATH_SLACK:$setup_slack;HOLD_PATH_SLACK:$hold_slack"
    puts "POSTROUTE_CONSTRAINTS=no_clock:0;unconstrained_internal_endpoints:0;all_categories:0"
    puts "POSTROUTE_CLOCK=clk_fpga_0;PERIOD_NS:[format %.3f $clk_period]"
    puts "POSTROUTE_DRC=total:[llength $drc_objects];fatal_error_or_critical:0;waivers_disabled:1"
    puts "POSTROUTE_METHODOLOGY=total:[llength $methodology_objects];fatal_error_or_critical:0;waivers_disabled:1"
    puts "POSTROUTE_REPORTED_CONGESTION_MAX_LEVEL=$congestion_label;GATE=PASS_NO_WINDOWS_ABOVE_LEVEL_3"
    puts "POSTROUTE_BRAM=RAMB36E1:$ramb36;FIFO36E1:$fifo36;RAMB18E1:$ramb18;FIFO18E1:$fifo18;BRAM18_EQ:$bram18eq/280;BRAM18_EQ_HEADROOM:$bram_headroom"
    puts "POSTROUTE_BRAM_TILES=[format %.1f [expr {$bram18eq / 2.0}]]/140.0;TILE_HEADROOM=[format %.1f [expr {$bram_headroom / 2.0}]]"
    puts "REPORT_DIR=$report_dir"
    puts "ROUTED_CHECKPOINT_COPY=$copy_dcp"
    if {$emit_route_only_bitstream_token} {
        puts "BITSTREAM_GENERATED=0"
    } else {
        puts "ROUTE_PHASE_BITSTREAM_GENERATED=0"
    }
    return $report_dir
}

proc ::postextract::main {} {
    variable project_dir
    variable project_file

    if {[version -short] ne "2025.2"} {
        fail "use Vivado 2025.2; current version is [version -short]"
    }
    if {![file isfile $project_file]} {
        fail "active project not found: $project_file"
    }
    open_project $project_file
    expect_text "project name" [get_property NAME [current_project]] three_stage_combined
    expect_text "target part" [string tolower [get_property PART [current_project]]] xc7z020clg400-1
    if {![string equal -nocase [file normalize [get_property DIRECTORY [current_project]]] $project_dir]} {
        fail "opened the wrong project directory: [get_property DIRECTORY [current_project]]"
    }

    puts "@@POSTEXTRACT_SIGNOFF_BEGIN@@"
    preflight_bd
    run_synthesis
    run_route
    close_project
    puts "@@POSTEXTRACT_ROUTE_SIGNOFF_PASS@@"
}

if {[info exists ::postextract::library_only] && $::postextract::library_only} {
    return
}

set rc [catch {::postextract::main} message options]
if {$rc} {
    puts stderr $message
    if {[dict exists $options -errorinfo]} {
        puts stderr [dict get $options -errorinfo]
    }
    catch {close_project}
    exit 1
}
exit 0
