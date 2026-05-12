# Vitis HLS 2025.2 project script for class_score_core
#   vitis_hls -f run_hls.tcl

set project_name "class_score"
set part_number  "xc7z020clg400-1"

open_project -reset $project_name
set_top class_score_core

add_files class_score_core.cpp
add_files class_score_core.h

open_solution -reset "solution1"
set_part $part_number
create_clock -period 5ns -name default

csynth_design

export_design -format ip_catalog \
              -description "Class Score Core — template score comparator and classifier" \
              -vendor "TermCount" \
              -version "1.0"

close_project
