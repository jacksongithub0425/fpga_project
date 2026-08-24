# Vitis HLS 2025.2 project script for binarize_core
#
# Run from the Vitis HLS Tcl console, or from the shell:
#   vitis_hls -f run_hls.tcl
#
# The C testbench is self-contained and exact. The Python generator remains
# available only for optional board/notebook vectors.

set project_name "binarize"
set part_number  "xc7z020clg400-1"

open_project -reset $project_name
set_top binarize_core

add_files binarize_core.cpp
add_files binarize_core.h

add_files -tb binarize_tb.cpp

open_solution -reset "solution1"
set_part $part_number
create_clock -period 5ns -name default   ;# 200 MHz

csim_design
csynth_design
cosim_design -rtl verilog

export_design -format ip_catalog \
              -description "Binarize Core v2.0 - exact logical-layout 3x3 Gaussian + THRESH_BINARY_INV" \
              -vendor "TermCount" \
              -version "2.0"

close_project
