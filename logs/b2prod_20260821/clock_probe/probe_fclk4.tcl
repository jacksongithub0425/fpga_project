# Probe 4: refine the working recipe.  Enabling FCLK1 at 125 MHz forces the IO
# PLL to 1000, which lets FCLK0=100 land on divisor product 10 (board 100.0 MHz)
# while Vivado still constrains 10.000 ns.  Question: can FCLK1 be enabled
# WITHOUT creating a second BD clock port (and hence a second timing clock)?
set part xc7z020clg400-1

proc mkps {} {
    set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 \
                processing_system7_0]
    apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
        -config {make_external "FIXED_IO, DDR" apply_board_preset "0" \
                 Master "Disable" Slave "Disable"} $ps
    set_property -dict [list CONFIG.PCW_FPGA_FCLK0_ENABLE {1} \
        CONFIG.PCW_USE_S_AXI_HP0 {1}] $ps
    return $ps
}

proc show {label ps} {
    set d0  [get_property CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR0 $ps]
    set d1  [get_property CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR1 $ps]
    set pll [get_property CONFIG.PCW_IO_IO_PLL_FREQMHZ $ps]
    set fb  [get_property CONFIG.PCW_IOPLL_CTRL_FBDIV $ps]
    set e0  [get_property CONFIG.PCW_FPGA_FCLK1_ENABLE $ps]
    set p1  [get_property CONFIG.PCW_EN_CLK1_PORT $ps]
    set prod [expr {$d0 * $d1}]
    set vmhz [expr {double($pll) / $prod}]
    # which FCLK pins actually exist on the cell?
    set pins {}
    foreach pin [get_bd_pins -quiet ${ps}/*] {
        set n [file tail $pin]
        if {[string match "FCLK_CLK*" $n]} { lappend pins $n }
    }
    puts [format "ROW %-26s fbdiv=%-3s io_pll=%-9s div=%sx%s=%-3s VIVADO=%.4fMHz/%.4fns BOARD=%.4fMHz fclk1_en=%s clk1_port=%s pins={%s}" \
        $label $fb $pll $d0 $d1 $prod $vmhz [expr {1000.0/$vmhz}] [expr {1000.0/$prod}] $e0 $p1 [join $pins ,]]
}

proc trial {label body} {
    global part
    create_project -in_memory -part $part -quiet
    create_bd_design [string map {- _} $label]
    set ps [mkps]
    if {[catch {uplevel 1 [list apply [list {ps} $body] $ps]} e]} {
        puts "ROW $label SETFAIL: $e"
    } else { show $label $ps }
    catch {close_bd_design [current_bd_design]}
    catch {close_project}
}

puts "@@PROBE4_BEGIN@@"

# baseline shipping config, for the pin list
trial base-req50 {set_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 50 $ps}

# the working recipe from probe 3, with the port explicitly ON
trial c-port1 {set_property -dict [list CONFIG.PCW_EN_CLK1_PORT {1} \
    CONFIG.PCW_FPGA_FCLK1_ENABLE {1} CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ 125 \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100] $ps}

# same, but suppress the BD port
trial c-port0 {set_property -dict [list CONFIG.PCW_EN_CLK1_PORT {0} \
    CONFIG.PCW_FPGA_FCLK1_ENABLE {1} CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ 125 \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100] $ps}

# does FCLK1 even need to be *enabled*, or is requesting its frequency enough?
trial c-freqonly {set_property -dict [list CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ 125 \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100] $ps}

# other FCLK1 frequencies that might also force the 1000 MHz PLL
foreach f {250 200 142.857143 50} {
    trial [format "c-fclk1_%s" $f] [subst -nocommands {set_property -dict [list \
        CONFIG.PCW_EN_CLK1_PORT {1} CONFIG.PCW_FPGA_FCLK1_ENABLE {1} \
        CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ $f \
        CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100] \$ps}]
}

puts "@@PROBE4_END@@"
