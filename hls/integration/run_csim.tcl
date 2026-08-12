# Vitis HLS 2025.2 — binarizer -> extractor -> matcher plus the preserved
# extractor -> matcher seam, C simulation ONLY.
#   vitis-run.bat --mode hls --tcl run_csim.tcl
#
# Generate the vectors first:
#   ../.venv/Scripts/python.exe pe_tme_generate_golden.py
#
# NO csynth_design AND NO cosim_design HERE, on purpose.
#
# There is nothing to synthesise.  This project has no top function of its
# own — it invokes three cores that are each synthesised and verified in
# their own directories, and the thing under test is the storage/PS boundary
# between them.  `set_top` below names the extractor
# only because open_solution requires a top; it is not what is being checked,
# and running csynth here would produce a fourth copy of an RTL nobody uses.
#
# Cosim would be worse than useless: co-simulation drives ONE top function
# through an RTL wrapper, so it cannot run a PS loop that reads a metadata
# record and then decides what to do next.  The sequencing is the test.  The
# corresponding hardware joins require a combined block design on the board,
# not an xsim wrapper around one top -- see docs/pl_interface_contract.md §7.1.

set project_name "pe_tme_seam"
set part_number  "xc7z020clg400-1"

open_project -reset $project_name
set_top patch_extract_core

# All three cores' synthesisable sources, unmodified and read from where they
# live.  Copies would rot: the whole value of this test is that it runs the
# same text their standalone projects synthesise.
set core_cflags "-I../binarize -I../patch_extract -I../template_match"
add_files ../binarize/binarize_core.cpp           -cflags $core_cflags
add_files ../patch_extract/patch_extract_core.cpp -cflags $core_cflags
add_files ../template_match/tme_top.cpp           -cflags $core_cflags
add_files ../template_match/correlation_core.cpp  -cflags $core_cflags

add_files -tb pe_tme_tb.cpp -cflags $core_cflags
add_files -tb [glob -nocomplain \
    tb_pe_tme_*.bin tb_pe_tme_*.txt \
    tb_bpe_tme_*.bin tb_bpe_tme_*.txt]

open_solution -reset "solution1"
set_part $part_number
create_clock -period 5ns -name default

csim_design

close_project
