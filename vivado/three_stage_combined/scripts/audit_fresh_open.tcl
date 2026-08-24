# Read-only smoke test for opening the repaired combined project and block design
# in a brand-new Vivado 2025.2 process.  This deliberately performs no save,
# generation, reset, launch, or write operation.

proc audit_one {label objects} {
    if {[llength $objects] != 1} {
        error "$label: expected exactly one object, got [llength $objects]: $objects"
    }
    return [lindex $objects 0]
}

proc audit_same_intf_net {left right} {
    set lp [audit_one "interface pin $left" [get_bd_intf_pins -quiet $left]]
    set rp [audit_one "interface pin $right" [get_bd_intf_pins -quiet $right]]
    set ln [audit_one "interface net $left" [get_bd_intf_nets -quiet -of_objects $lp]]
    set rn [audit_one "interface net $right" [get_bd_intf_nets -quiet -of_objects $rp]]
    if {$ln ne $rn} { error "$left and $right are not connected" }
}

proc run_fresh_open_audit {} {
    if {[version -short] ne "2025.2"} { error "Vivado 2025.2 is required" }
    if {[llength [get_projects -quiet]] != 0} { error "start a brand-new Vivado process" }

    set root [file normalize [file join [file dirname [info script]] ..]]
    set xpr [file join $root three_stage_combined.xpr]
    set bd_path [file join $root three_stage_combined.srcs sources_1 bd tme_bd tme_bd.bd]
    open_project -read_only $xpr
    if {[string tolower [get_property PART [current_project]]] ne "xc7z020clg400-1"} {
        error "wrong project part: [get_property PART [current_project]]"
    }
    set bd [audit_one "registered tme_bd" [get_files -all -quiet $bd_path]]
    open_bd_design $bd
    current_bd_instance /

    foreach {name vlnv} {
        binarize_core_0      TermCount:hls:binarize_core:2.0
        patch_extract_core_0 TermCount:hls:patch_extract_core:0.1
        dma_pe_data          xilinx.com:ip:axi_dma:7.1
        dma_pe_meta          xilinx.com:ip:axi_dma:7.1
        smartconnect_pe_mem  xilinx.com:ip:smartconnect:1.0
    } {
        set cell [audit_one "BD cell $name" [get_bd_cells -quiet $name]]
        if {[get_property VLNV $cell] ne $vlnv} {
            error "$name has wrong VLNV: [get_property VLNV $cell]"
        }
    }
    set ps [audit_one "processing system" [get_bd_cells -quiet processing_system7_0]]
    if {[get_property CONFIG.PCW_USE_S_AXI_HP2 $ps] != 1 ||
        [get_property CONFIG.PCW_S_AXI_HP2_DATA_WIDTH $ps] != 64} {
        error "HP2 is not enabled at 64 bits"
    }
    if {[get_property CONFIG.NUM_MI [get_bd_cells smartconnect_lite]] != 8} {
        error "control SmartConnect is not 8-MI"
    }
    if {[get_property CONFIG.c_sg_length_width [get_bd_cells axi_dma_binarize]] != 26} {
        error "binarizer DMA length width is not 26"
    }
    foreach {left right} {
        smartconnect_lite/M05_AXI patch_extract_core_0/s_axi_CTRL
        smartconnect_lite/M06_AXI dma_pe_data/S_AXI_LITE
        smartconnect_lite/M07_AXI dma_pe_meta/S_AXI_LITE
        smartconnect_pe_mem/M00_AXI processing_system7_0/S_AXI_HP2
    } {
        audit_same_intf_net $left $right
    }

    close_bd_design [current_bd_design]
    close_project
    puts "@@POSTEXTRACT_FRESH_READONLY_OPEN_PASS@@"
}

set rc [catch {run_fresh_open_audit} message options]
if {$rc} {
    puts stderr $message
    if {[dict exists $options -errorinfo]} { puts stderr [dict get $options -errorinfo] }
    catch {close_bd_design [current_bd_design]}
    catch {close_project}
    exit 1
}
exit 0
