# Why does `generate_target -force all` change the preserved BD's boundary_crc
# in the VARIANT project when the same call left the baseline BD byte-stable on
# 2026-08-11?
#
# The question that decides the fix: does the CRC CONVERGE after one forced
# generation (a one-time normalization -- the variant BD was produced by
# write_bd_tcl + a recreate script, not by a full forced generation), or does it
# churn on every call (nondeterministic bookkeeping)?
#
# Convergent  -> the BD is now normalized; re-running the export is legitimate
#                and the immutability gate keeps its full strength.
# Nondeterministic -> the gate is comparing a field that is not a function of
#                design content, and must compare content EXCLUDING that field.
#
# This probe changes no design content: it only calls generate_target, which the
# export driver calls anyway.
set project_file "C:/Users/lychee/Desktop/FPGA/combined_b2_100/three_stage_combined.xpr"
set bd_file "C:/Users/lychee/Desktop/FPGA/combined_b2_100/three_stage_combined.srcs/sources_1/bd/tme_bd/tme_bd.bd"

proc digest {path} {
    set native [file nativename [file normalize $path]]
    set command "(Get-FileHash -Algorithm SHA256 -LiteralPath '$native').Hash"
    set hash [exec powershell.exe -NoProfile -NonInteractive -Command $command]
    return [string toupper [string trim $hash]]
}

proc crc {path} {
    set handle [open $path r]
    set text [read $handle]
    close $handle
    if {[regexp {"boundary_crc"\s*:\s*"([^"]+)"} $text matched value]} {
        return $value
    }
    return NONE
}

proc snap {label} {
    global bd_file
    puts "CRC_PROBE=$label;sha256:[digest $bd_file];boundary_crc:[crc $bd_file];bytes:[file size $bd_file]"
}

open_project $project_file
set bd_obj [lindex [get_files -all $bd_file] 0]

snap before_any_generation
for {set pass 1} {$pass <= 3} {incr pass} {
    generate_target -force all $bd_obj
    snap "after_forced_generation_pass_$pass"
}
close_project
puts "@@CRC_PROBE_DONE@@"
exit 0
