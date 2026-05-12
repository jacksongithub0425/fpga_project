# Vitis HLS 2025.2 project script for patch_extract_core
#   vitis_hls -f run_hls.tcl

set project_name "patch_extract"
set part_number  "xc7z020clg400-1"

open_project -reset $project_name
set_top patch_extract_core

add_files patch_extract_core.cpp
add_files patch_extract_core.h

add_files -tb patch_extract_tb.cpp

open_solution -reset "solution1"
set_part $part_number
create_clock -period 5ns -name default

# Validate algorithm against synthetic image with known per-pixel pattern
csim_design

csynth_design

export_design -format ip_catalog \
              -description "Patch Extract Core - endpoint-aligned image crop" \
              -vendor "TermCount" \
              -version "1.0"

close_project
