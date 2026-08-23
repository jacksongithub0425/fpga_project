# Confirm all four IP repositories resolve, with the B2 core alongside the
# baseline.  Reads only; builds nothing.
set root "C:/Users/lychee/Desktop/FPGA"
set repos [list \
    "$root/hls/template_match/template_match_provisional/solution1/impl/ip" \
    "$root/hls/patch_extract/patch_extract_provisional/solution1/impl/ip" \
    "$root/binarizer-logical-layout/hls/binarize/binarize/solution1/impl/ip" \
    "$root/hls/template_match/template_match_b1_b2/b2/impl/ip"]

puts "@@CATALOG_PROBE_BEGIN@@"
create_project -in_memory -part xc7z020clg400-1
foreach r $repos {
    puts [format "repo %-6s %s" [expr {[file isfile "$r/component.xml"] ? "OK" : "MISSING"}] $r]
}
set_property ip_repo_paths $repos [current_project]
update_ip_catalog
foreach vlnv {
    TermCount:hls:tme_top:0.2
    TermCountB2:hls:tme_top:0.2
    TermCount:hls:binarize_core:2.0
    TermCount:hls:patch_extract_core:0.1
} {
    set d [get_ipdefs -all -quiet $vlnv]
    puts [format "vlnv %-34s -> %s" $vlnv [expr {[llength $d] == 1 ? "RESOLVED" : "NOT RESOLVED ([llength $d])"}]]
}
puts "all tme_top ipdefs: [get_ipdefs -all -quiet *:hls:tme_top:*]"
close_project
puts "@@CATALOG_PROBE_END@@"
