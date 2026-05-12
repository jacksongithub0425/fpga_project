# Vitis HLS 2025.2 project script for template_match_core
#
# Run from the Vitis HLS Tcl console, or from the shell:
#   vitis_hls -f run_hls.tcl

set project_name "template_match"
set part_number  "xc7z020clg400-1"   ;# Arty Z7-020
set script_dir   [file dirname [file normalize [info script]]]
if {![file exists [file join $script_dir "tme_top.cpp"]]} {
    set script_dir [file join [pwd] "FPGA" "hls" "template_match"]
}
if {![file exists [file join $script_dir "tme_top.cpp"]]} {
    set script_dir {C:/Users/g1171/FPGA/hls/template_match}
}
set project_dir  [file join $script_dir $project_name]

cd $script_dir

open_project -reset $project_dir
set_top tme_top

add_files tme_top.cpp
add_files tme_top.h
add_files correlation_core.cpp
add_files norm_rsqrt.cpp

add_files -tb tme_tb.cpp
add_files -tb [glob -nocomplain tb_*.bin tb_golden.txt]

open_solution -reset "solution1"
set_part $part_number
create_clock -period 7ns -name default   ;# ~143 MHz - Phase A fallback
# Critical path is icmp -> select -> add -> store on the inner-loop counter
# in load_patch / load_templ (~6.5 ns at xc7z020-1). Bump to 7.5 ns if
# place-and-route fails closure.

# Run C simulation to validate algorithm against golden reference.
# Generate tb_patch.bin / tb_templ.bin / tb_golden.txt first:
#   python tme_generate_golden.py
csim_design

# Synthesize to RTL and check resource/timing estimates
csynth_design

# C/RTL co-simulation — uncomment when you want waveform verification
# (can take 10–30 min on a full-size page patch)
# cosim_design -rtl verilog -trace_level all

# Package as Vivado-compatible IP for use in the block design
export_design -format ip_catalog \
              -description "Terminal Matching Engine — TM_CCOEFF_NORMED accelerator" \
              -vendor "TermCount" \
              -version "1.0"

close_project
