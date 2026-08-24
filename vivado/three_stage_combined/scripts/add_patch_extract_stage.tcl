# Incrementally add the extractor stage to the routed matcher+binarizer project.
#
# Run with Vivado 2025.2:
#   vivado.bat -mode batch -notrace -source scripts/add_patch_extract_stage.tcl
#
# This script deliberately edits the existing tme_bd design.  It does not
# rebuild the project from vivado/tme_chain/build_tme_chain.tcl because that
# script uses a different two-HP-port topology and different instance names.

namespace eval ::add_patch_extract {
    variable project_dir [file normalize [file join [file dirname [info script]] ..]]
    variable project_file [file join $project_dir three_stage_combined.xpr]
    variable bd_file [file join $project_dir three_stage_combined.srcs sources_1 bd tme_bd tme_bd.bd]
    variable snapshots_dir [file join $project_dir snapshots]

    variable pe_vlnv TermCount:hls:patch_extract_core:0.1
    variable dma_vlnv xilinx.com:ip:axi_dma:7.1
    variable sc_vlnv xilinx.com:ip:smartconnect:1.0
}

proc ::add_patch_extract::fail {message} {
    puts stderr "\nPATCH_EXTRACT_INTEGRATION_ERROR: $message\n"
    return -code error $message
}

proc ::add_patch_extract::one_cell {name} {
    set obj [get_bd_cells -quiet $name]
    if {[llength $obj] != 1} {
        fail "expected exactly one existing BD cell '$name', found [llength $obj]"
    }
    return $obj
}

proc ::add_patch_extract::require_absent {names} {
    set present {}
    foreach name $names {
        if {[llength [get_bd_cells -quiet $name]] != 0} {
            lappend present $name
        }
    }
    if {[llength $present] != 0} {
        fail "refusing a partial/double integration; these target cells already exist: $present"
    }
}

proc ::add_patch_extract::connect_clock {source targets} {
    foreach target $targets {
        set pin [get_bd_pins -quiet $target]
        if {[llength $pin] != 1} {
            fail "clock target pin '$target' does not exist"
        }
        connect_bd_net $source $pin
    }
}

proc ::add_patch_extract::connect_reset {source targets} {
    foreach target $targets {
        set pin [get_bd_pins -quiet $target]
        if {[llength $pin] != 1} {
            fail "reset target pin '$target' does not exist"
        }
        connect_bd_net $source $pin
    }
}

proc ::add_patch_extract::main {} {
    variable project_dir
    variable project_file
    variable bd_file
    variable snapshots_dir
    variable pe_vlnv
    variable dma_vlnv
    variable sc_vlnv

    if {[version -short] ne "2025.2"} {
        fail "this checkpoint was built with Vivado 2025.2; running [version -short]"
    }
    if {![file isfile $project_file]} {
        fail "active project not found: $project_file"
    }
    if {![file isfile $bd_file]} {
        fail "active block design not found: $bd_file"
    }

    open_project $project_file
    update_ip_catalog
    open_bd_design $bd_file

    # The signoff checkpoint supplied by the previous milestone is this exact
    # matcher+binarizer topology.  Check it before changing anything so the
    # Tcl cannot accidentally target the stale matcher-only sibling project.
    foreach cell {
        processing_system7_0 proc_sys_reset_0 tme_top_0
        axi_dma_patch axi_dma_templ smartconnect_lite smartconnect_mem
        binarize_core_0 axi_dma_binarize smartconnect_bin_mem
    } {
        one_cell $cell
    }
    require_absent {
        patch_extract_core_0 dma_pe_data dma_pe_meta smartconnect_pe_mem
    }

    if {[llength [get_ipdefs -all -quiet $pe_vlnv]] != 1} {
        fail "extractor IP '$pe_vlnv' is not uniquely visible in the project catalogue"
    }

    file mkdir $snapshots_dir
    set stamp [clock format [clock seconds] -format %Y%m%d_%H%M%S]
    set backup_tcl [file join $snapshots_dir tme_bd_pre_patch_extract_${stamp}.tcl]
    set backup_bd [file join $snapshots_dir tme_bd_pre_patch_extract_${stamp}.bd]
    write_bd_tcl -force $backup_tcl
    file copy -force $bd_file $backup_bd
    puts "preserved pre-extractor BD snapshots:"
    puts "  $backup_tcl"
    puts "  $backup_bd"

    set ps [one_cell processing_system7_0]
    set sc_lite [one_cell smartconnect_lite]

    # HP0 remains matcher-only and HP1 remains binarizer-only.  The extractor
    # gets a new HP2 path so this incremental checkpoint keeps the three stages
    # physically and diagnostically separable.
    set_property -dict [list \
        CONFIG.PCW_USE_S_AXI_HP2 {1} \
        CONFIG.PCW_S_AXI_HP2_DATA_WIDTH {64} \
    ] $ps
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {8}] $sc_lite

    create_bd_cell -type ip -vlnv $pe_vlnv patch_extract_core_0

    # Candidate MM2S + patch S2MM.  These values reproduce the extractor image
    # that passed standalone hardware bring-up, including its 18-bit transfer
    # length contract and 64-bit candidate stream.
    set dma_data [create_bd_cell -type ip -vlnv $dma_vlnv dma_pe_data]
    set_property -dict [list \
        CONFIG.c_addr_width {32} \
        CONFIG.c_include_sg {0} \
        CONFIG.c_include_mm2s {1} \
        CONFIG.c_include_s2mm {1} \
        CONFIG.c_include_mm2s_dre {0} \
        CONFIG.c_include_s2mm_dre {0} \
        CONFIG.c_include_mm2s_sf {1} \
        CONFIG.c_include_s2mm_sf {1} \
        CONFIG.c_sg_length_width {18} \
        CONFIG.c_m_axi_mm2s_data_width {64} \
        CONFIG.c_m_axi_s2mm_data_width {32} \
        CONFIG.c_m_axis_mm2s_tdata_width {64} \
        CONFIG.c_s_axis_s2mm_tdata_width {8} \
        CONFIG.c_mm2s_burst_size {16} \
        CONFIG.c_s2mm_burst_size {16} \
        CONFIG.c_micro_dma {0} \
        CONFIG.c_single_interface {0} \
    ] $dma_data

    # Metadata is one 128-bit record per descriptor.  Both the AXIS and AXI-MM
    # sides stay 128-bit; the HP2 SmartConnect performs the 128-to-64 downsize.
    set dma_meta [create_bd_cell -type ip -vlnv $dma_vlnv dma_pe_meta]
    set_property -dict [list \
        CONFIG.c_addr_width {32} \
        CONFIG.c_include_sg {0} \
        CONFIG.c_include_mm2s {0} \
        CONFIG.c_include_s2mm {1} \
        CONFIG.c_include_s2mm_dre {0} \
        CONFIG.c_include_s2mm_sf {1} \
        CONFIG.c_sg_length_width {18} \
        CONFIG.c_m_axi_s2mm_data_width {128} \
        CONFIG.c_s_axis_s2mm_tdata_width {128} \
        CONFIG.c_s2mm_burst_size {16} \
        CONFIG.c_micro_dma {0} \
        CONFIG.c_single_interface {0} \
    ] $dma_meta

    set sc_pe [create_bd_cell -type ip -vlnv $sc_vlnv smartconnect_pe_mem]
    set_property -dict [list CONFIG.NUM_SI {4} CONFIG.NUM_MI {1}] $sc_pe

    # AXI4-Lite: retain the five signed-off windows and append the extractor.
    connect_bd_intf_net [get_bd_intf_pins smartconnect_lite/M05_AXI] \
                        [get_bd_intf_pins patch_extract_core_0/s_axi_CTRL]
    connect_bd_intf_net [get_bd_intf_pins smartconnect_lite/M06_AXI] \
                        [get_bd_intf_pins dma_pe_data/S_AXI_LITE]
    connect_bd_intf_net [get_bd_intf_pins smartconnect_lite/M07_AXI] \
                        [get_bd_intf_pins dma_pe_meta/S_AXI_LITE]

    # HP2 owns every extractor memory master: candidate read, patch write,
    # metadata write, and the core's direct binary-image read.
    set hp2_masters {
        S00 dma_pe_data/M_AXI_MM2S
        S01 dma_pe_data/M_AXI_S2MM
        S02 dma_pe_meta/M_AXI_S2MM
        S03 patch_extract_core_0/m_axi_BIN_IMG
    }
    foreach {port master} $hp2_masters {
        connect_bd_intf_net [get_bd_intf_pins $master] \
                            [get_bd_intf_pins smartconnect_pe_mem/${port}_AXI]
    }
    connect_bd_intf_net [get_bd_intf_pins smartconnect_pe_mem/M00_AXI] \
                        [get_bd_intf_pins processing_system7_0/S_AXI_HP2]

    # Extractor streams reproduce the standalone topology.  Patches return to
    # DDR because the matcher may replay one patch for multiple templates.
    connect_bd_intf_net [get_bd_intf_pins dma_pe_data/M_AXIS_MM2S] \
                        [get_bd_intf_pins patch_extract_core_0/cand_in]
    connect_bd_intf_net [get_bd_intf_pins patch_extract_core_0/patch_out] \
                        [get_bd_intf_pins dma_pe_data/S_AXIS_S2MM]
    connect_bd_intf_net [get_bd_intf_pins patch_extract_core_0/meta_out] \
                        [get_bd_intf_pins dma_pe_meta/S_AXIS_S2MM]

    set clk [get_bd_pins processing_system7_0/FCLK_CLK0]
    connect_clock $clk {
        processing_system7_0/S_AXI_HP2_ACLK
        patch_extract_core_0/ap_clk
        dma_pe_data/s_axi_lite_aclk
        dma_pe_data/m_axi_mm2s_aclk
        dma_pe_data/m_axi_s2mm_aclk
        dma_pe_meta/s_axi_lite_aclk
        dma_pe_meta/m_axi_s2mm_aclk
        smartconnect_pe_mem/aclk
    }

    set peripheral_rstn [get_bd_pins proc_sys_reset_0/peripheral_aresetn]
    connect_reset $peripheral_rstn {
        patch_extract_core_0/ap_rst_n
        dma_pe_data/axi_resetn
        dma_pe_meta/axi_resetn
    }
    # The staged baseline has every existing SmartConnect on peripheral_aresetn;
    # keep the new fabric in that same signed-off reset domain.
    connect_reset $peripheral_rstn {smartconnect_pe_mem/aresetn}

    # Preserve every signed-off address.  Only the next free core and DMA
    # windows are consumed.
    set lite_offsets {
        patch_extract_core_0/s_axi_CTRL 0x40020000
        dma_pe_data/S_AXI_LITE          0x41E30000
        dma_pe_meta/S_AXI_LITE          0x41E40000
    }
    foreach {slave offset} $lite_offsets {
        assign_bd_address -offset $offset -range 0x00010000 \
            -target_address_space [get_bd_addr_spaces processing_system7_0/Data] \
            [get_bd_addr_segs $slave/Reg] -force
    }

    foreach space {
        dma_pe_data/Data_MM2S
        dma_pe_data/Data_S2MM
        dma_pe_meta/Data_S2MM
        patch_extract_core_0/Data_m_axi_BIN_IMG
    } {
        assign_bd_address -offset 0x00000000 -range 0x20000000 \
            -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs processing_system7_0/S_AXI_HP2/HP2_DDR_LOWOCM] \
            -force
    }

    regenerate_bd_layout
    validate_bd_design
    save_bd_design

    set bd_obj [get_files -all $bd_file]
    if {[llength $bd_obj] != 1} {
        fail "expected one registered tme_bd file after save, found [llength $bd_obj]"
    }
    generate_target all $bd_obj
    export_ip_user_files -of_objects $bd_obj -no_script -sync -force -quiet
    update_compile_order -fileset sources_1

    # Invalidate only generated run products.  The prior routed DCP under the
    # timestamped postbin_route_reports_* directory is outside these runs and
    # remains intact.
    if {[llength [get_runs -quiet impl_1]] == 1} {
        reset_run impl_1
    }
    if {[llength [get_runs -quiet synth_1]] == 1} {
        reset_run synth_1
    }

    puts "\n@@PATCHEXTRACT_BD_INTEGRATION_PASS@@"
    puts "PROJECT=$project_file"
    puts "BD=$bd_file"
    puts "CORE=patch_extract_core_0 VLNV=$pe_vlnv CTRL=0x40020000"
    puts "DMA_DATA=dma_pe_data CTRL=0x41E30000 SG_LENGTH_WIDTH=18"
    puts "DMA_META=dma_pe_meta CTRL=0x41E40000 WIDTH=128 SG_LENGTH_WIDTH=18"
    puts "MEMORY_PATH=smartconnect_pe_mem -> processing_system7_0/S_AXI_HP2"
    puts "BITSTREAM_GENERATED=0"
    close_project
}

set rc [catch {::add_patch_extract::main} message options]
if {$rc} {
    puts stderr $message
    if {[dict exists $options -errorinfo]} {
        puts stderr [dict get $options -errorinfo]
    }
    catch {close_project}
    exit 1
}
exit 0
