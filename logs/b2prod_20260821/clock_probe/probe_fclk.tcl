# Preset-less PS7 FCLK0 divisor probe.  Replicates build_tme_standalone.tcl's
# PS7 configuration exactly, sweeps the requested FCLK0 frequency, and reports
# what Vivado's own solver chooses.  Reports only; builds nothing.

set part xc7z020clg400-1
set sweep {50 100 125 160}

puts "@@FCLK_PROBE_BEGIN@@"
puts "vivado_version=[version -short]"
puts "part=$part"

foreach mhz $sweep {
    create_project -in_memory -part $part
    create_bd_design "probe_$mhz"
    set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 \
                processing_system7_0]
    apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
        -config {make_external "FIXED_IO, DDR" apply_board_preset "0" \
                 Master "Disable" Slave "Disable"} $ps
    set_property -dict [list \
        CONFIG.PCW_FPGA_FCLK0_ENABLE {1} \
        CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ $mhz \
        CONFIG.PCW_USE_S_AXI_HP0 {1} \
    ] $ps

    set d0   [get_property CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR0 $ps]
    set d1   [get_property CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR1 $ps]
    set pll  [get_property CONFIG.PCW_IO_IO_PLL_FREQMHZ $ps]
    set src  [get_property CONFIG.PCW_FPGA0_PERIPHERAL_CLKSRC $ps]
    set act  [get_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ $ps]
    set xtal [get_property CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ $ps]
    set arm  [get_property CONFIG.PCW_ARM_PLL_FREQMHZ $ps]
    set ddr  [get_property CONFIG.PCW_DDR_PLL_FREQMHZ $ps]

    set prod [expr {$d0 * $d1}]
    set vivado_mhz [expr {double($pll) / $prod}]
    set board_mhz  [expr {1000.0 / $prod}]

    puts [format "FCLK_ROW req=%s clksrc=%s xtal=%s io_pll=%s arm_pll=%s ddr_pll=%s div0=%s div1=%s prod=%s echoed_freqmhz=%s vivado_mhz=%.6f vivado_ns=%.6f board_mhz_at_1000=%.6f" \
        $mhz $src $xtal $pll $arm $ddr $d0 $d1 $prod $act $vivado_mhz \
        [expr {1000.0 / $vivado_mhz}] $board_mhz]

    close_bd_design [current_bd_design]
    close_project
}
puts "@@FCLK_PROBE_END@@"
