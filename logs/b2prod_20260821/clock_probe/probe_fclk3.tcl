# Probe 3: steer the PS7 to IO PLL = 1000 MHz with an FCLK0 divisor product of
# 10 -- the only combination giving BOTH a 10.000 ns Vivado constraint and a
# live board fclk0 of 100.0 MHz (board = 1000 / div_product, PLL fixed by PYNQ).
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
    set d0   [get_property CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR0 $ps]
    set d1   [get_property CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR1 $ps]
    set pll  [get_property CONFIG.PCW_IO_IO_PLL_FREQMHZ $ps]
    set fb   [get_property CONFIG.PCW_IOPLL_CTRL_FBDIV $ps]
    set act  [get_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ $ps]
    set xtal [get_property CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ $ps]
    if {![string is double -strict $pll] || ![string is integer -strict $d0] ||
        ![string is integer -strict $d1]} {
        puts [format "ROW %-24s UNREADABLE io_pll=<%s> fbdiv=<%s> div=<%s>x<%s> echo=<%s>" \
            $label $pll $fb $d0 $d1 $act]
        return
    }
    set prod [expr {$d0 * $d1}]
    set vmhz [expr {double($pll) / $prod}]
    puts [format "ROW %-24s xtal=%-10s fbdiv=%-3s io_pll=%-9s div=%sx%s=%-3s echo=%-5s VIVADO=%.4fMHz/%.4fns BOARD=%.4fMHz" \
        $label $xtal $fb $pll $d0 $d1 $prod $act $vmhz [expr {1000.0/$vmhz}] [expr {1000.0/$prod}]]
}

proc trial {label body} {
    global part
    create_project -in_memory -part $part -quiet
    create_bd_design [string map {- _} $label]
    set ps [mkps]
    if {[catch {uplevel 1 [list apply [list {ps} $body] $ps]} e]} {
        puts "ROW $label SETFAIL: $e"
    } else {
        show $label $ps
    }
    catch {close_bd_design [current_bd_design]}
    catch {close_project}
}

puts "@@PROBE3_BEGIN@@"

# control: plain request of 100 (expect io_pll 1600, div 16, board 62.5)
trial ctl-req100 {set_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100 $ps}

# control: plain request of 125 (expect io_pll 1000, div 8, board 125.0)
trial ctl-req125 {set_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 125 $ps}

# e: pin the IO PLL to 1000 via FBDIV (33.333333 * 30 = 1000), then ask for 100
trial e-fbdiv30-req100 {set_property -dict [list \
    CONFIG.PCW_IOPLL_CTRL_FBDIV 30 \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100] $ps}

# e2: same, opposite order
trial e2-req100-fbdiv30 {
    set_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100 $ps
    set_property CONFIG.PCW_IOPLL_CTRL_FBDIV 30 $ps}

# b: force the FCLK0 divisors directly
trial b-force-div52 {set_property -dict [list \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100 \
    CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR0 5 \
    CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR1 2] $ps}

# c: request 125 on FCLK1 (forces IO PLL to 1000), then 100 on FCLK0
trial c-fclk1_125-fclk0_100 {set_property -dict [list \
    CONFIG.PCW_EN_CLK1_PORT {1} CONFIG.PCW_FPGA_FCLK1_ENABLE {1} \
    CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ 125 \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100] $ps}

# d: declare the true PYNQ-Z2 PS_CLK of 50 MHz, then ask for 100
trial d-xtal50-req100 {set_property -dict [list \
    CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ 50 \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ 100] $ps}

puts "@@PROBE3_END@@"
