# Repair generated project metadata from the preserved tme_bd, rebuild every
# OOC/top-level run, enforce the strict route gates, and only then generate a
# matching PYNQ .bit/.hwh pair.  Run in a fresh Vivado 2025.2 batch process.

namespace eval ::postextract { variable library_only 1 }
set helper_script [file normalize [file join [file dirname [info script]] run_postextract_signoff.tcl]]
source $helper_script

namespace eval ::postextract_export {
    variable project_dir $::postextract::project_dir
    variable project_file $::postextract::project_file
    variable bd_file $::postextract::bd_file
    variable jobs 8
    variable expected_ooc_stems {
        tme_bd_axi_dma_binarize_0
        tme_bd_axi_dma_patch_0
        tme_bd_axi_dma_templ_0
        tme_bd_binarize_core_0_0
        tme_bd_dma_pe_data_0
        tme_bd_dma_pe_meta_0
        tme_bd_patch_extract_core_0_0
        tme_bd_proc_sys_reset_0_0
        tme_bd_processing_system7_0_0
        tme_bd_smartconnect_bin_mem_0
        tme_bd_smartconnect_lite_0
        tme_bd_smartconnect_mem_0
        tme_bd_smartconnect_pe_mem_0
        tme_bd_tme_top_0_0
    }
}

proc ::postextract_export::fail {message} {
    ::postextract::fail $message
}

proc ::postextract_export::sha256 {path} {
    if {![file isfile $path] || [file size $path] == 0} {
        fail "cannot hash a missing or empty file: $path"
    }
    set native [file nativename [file normalize $path]]
    set quoted [string map [list "'" "''"] $native]
    set command "(Get-FileHash -Algorithm SHA256 -LiteralPath '$quoted').Hash"
    if {[catch {exec powershell.exe -NoProfile -NonInteractive -Command $command} hash]} {
        fail "SHA-256 failed for $path: $hash"
    }
    set hash [string toupper [string trim $hash]]
    if {![regexp {^[0-9A-F]{64}$} $hash]} {
        fail "SHA-256 returned an invalid digest for $path: $hash"
    }
    return $hash
}

proc ::postextract_export::copy_required {source destination label} {
    if {![file isfile $source] || [file size $source] == 0} {
        fail "$label is missing or empty: $source"
    }
    set source_hash [sha256 $source]
    file copy -force $source $destination
    if {![file isfile $destination] || [file size $destination] != [file size $source]} {
        fail "$label copy failed: $source -> $destination"
    }
    set destination_hash [sha256 $destination]
    if {$destination_hash ne $source_hash} {
        fail "$label copy hash mismatch: $source -> $destination"
    }
    return $source_hash
}

proc ::postextract_export::snapshot_recovery_inputs {} {
    variable project_dir
    variable project_file
    variable bd_file

    set stamp [clock format [clock seconds] -format %Y%m%d_%H%M%S]
    set recovery_dir [file join $project_dir recovery_snapshots "pre_full_export_$stamp"]
    if {[file exists $recovery_dir]} {
        fail "refusing to reuse recovery directory: $recovery_dir"
    }
    file mkdir $recovery_dir
    set records [list \
        project_sha256 [copy_required $project_file \
            [file join $recovery_dir three_stage_combined.xpr] "project snapshot"] \
        bd_sha256 [copy_required $bd_file \
            [file join $recovery_dir tme_bd.bd] "BD snapshot"]]

    set known_good_tcl [file join $project_dir snapshots \
        tme_bd_post_patch_extract_pre_synth_20260810_224708.tcl]
    lappend records known_good_bd_tcl_sha256 \
        [copy_required $known_good_tcl [file join $recovery_dir [file tail $known_good_tcl]] \
            "known-good BD Tcl snapshot"]

    set accepted_dcp [file join $project_dir postextract_route_reports_20260810_230031 \
        postextract_routed.dcp]
    lappend records accepted_route_dcp_sha256 \
        [copy_required $accepted_dcp [file join $recovery_dir postextract_routed_accepted.dcp] \
            "accepted routed checkpoint"]

    set old_hwh [file join $project_dir three_stage_combined.gen sources_1 bd tme_bd \
        hw_handoff tme_bd.hwh]
    if {[file isfile $old_hwh] && [file size $old_hwh] > 0} {
        lappend records previous_hwh_sha256 \
            [copy_required $old_hwh [file join $recovery_dir tme_bd_pre_repair.hwh] \
                "previous HWH"]
    }
    set digest_file [file join $recovery_dir SHA256.txt]
    set handle [open $digest_file w]
    try {
        foreach {key value} $records { puts $handle "$key=$value" }
    } finally {
        close $handle
    }
    puts "RECOVERY_SNAPSHOT_DIR=$recovery_dir"
    puts "RECOVERY_SHA256_MANIFEST=$digest_file"
    return $recovery_dir
}

proc ::postextract_export::repair_project_metadata {} {
    variable project_dir
    variable bd_file
    variable expected_ooc_stems

    set repos [list \
        [file normalize [file join $project_dir .. hls template_match \
            template_match_provisional solution1 impl ip]] \
        [file normalize [file join $project_dir .. hls patch_extract \
            patch_extract_provisional solution1 impl ip]] \
        [file normalize [file join $project_dir .. binarizer-logical-layout hls \
            binarize binarize solution1 impl ip]]]
    foreach repo $repos {
        if {![file isfile [file join $repo component.xml]]} {
            fail "required IP repository is missing component.xml: $repo"
        }
    }
    set_property ip_repo_paths $repos [current_project]
    update_ip_catalog

    foreach vlnv {
        TermCount:hls:tme_top:0.2
        TermCount:hls:binarize_core:2.0
        TermCount:hls:patch_extract_core:0.1
    } {
        ::postextract::one "catalog IP $vlnv" [get_ipdefs -all -quiet $vlnv]
    }

    # Validate every topology/configuration invariant without serializing the
    # trusted BD.  Generated products and run metadata are repaired separately.
    set bd_hash_before [sha256 $bd_file]
    ::postextract::preflight_bd 0
    if {[sha256 $bd_file] ne $bd_hash_before} {
        fail "preserved BD changed during read/validate preflight: $bd_file"
    }

    set bd_obj [::postextract::one "registered tme_bd" [get_files -all -quiet $bd_file]]
    set target_epoch [clock seconds]
    generate_target -force all $bd_obj
    export_ip_user_files -of_objects $bd_obj -no_script -sync -force -quiet
    update_compile_order -fileset sources_1
    if {[catch {create_ip_run -force $bd_obj} why]} {
        fail "could not recreate BD OOC run registrations: $why"
    }

    if {[sha256 $bd_file] ne $bd_hash_before} {
        fail "preserved BD changed while repairing generated metadata: $bd_file"
    }

    set source_ip_dir [file join $project_dir three_stage_combined.srcs sources_1 bd tme_bd ip]
    foreach stem $expected_ooc_stems {
        set xci [file normalize [file join $source_ip_dir $stem ${stem}.xci]]
        if {![file isfile $xci] || [file size $xci] == 0} {
            fail "regenerated XCI is missing: $xci"
        }
        ::postextract::one "registered XCI $stem" [get_files -all -quiet $xci]
        ::postextract::one "OOC run ${stem}_synth_1" [get_runs -quiet ${stem}_synth_1]
    }

    set forbidden_registered {}
    foreach f [get_files -all -quiet] {
        set name [string map {\\ /} [get_property NAME $f]]
        if {[regexp -nocase {/ip/(tme_bd_(dma_pe_data_0|dma_pe_meta_0|patch_extract_core_0_0|smartconnect_pe_mem_0)_1)/} $name]} {
            lappend forbidden_registered $name
        }
    }
    if {[llength $forbidden_registered] != 0} {
        fail "stale duplicate XCI paths remain registered: $forbidden_registered"
    }

    set hwh [file join $project_dir three_stage_combined.gen sources_1 bd tme_bd \
        hw_handoff tme_bd.hwh]
    if {![file isfile $hwh] || [file size $hwh] == 0 ||
        [file mtime $hwh] < $target_epoch || [file mtime $hwh] < [file mtime $bd_file]} {
        fail "forced target generation did not produce a fresh HWH: $hwh"
    }
    validate_hwh $hwh
    puts "@@POSTEXTRACT_PROJECT_METADATA_REPAIR_PASS@@"
    return [list bd_sha256 $bd_hash_before hwh_sha256 [sha256 $hwh] \
        target_generation_epoch $target_epoch]
}

proc ::postextract_export::reset_all_runs {} {
    variable expected_ooc_stems

    set ordered_runs [list impl_1 synth_1]
    foreach stem $expected_ooc_stems { lappend ordered_runs ${stem}_synth_1 }
    foreach run_name $ordered_runs {
        set run [::postextract::one "run $run_name" [get_runs -quiet $run_name]]
        set status [get_property STATUS $run]
        if {[string match "*Running*" $status] || [string match "*Queued*" $status]} {
            fail "run $run_name is active: $status"
        }
    }
    foreach run_name $ordered_runs {
        reset_run [get_runs $run_name]
    }
    puts "RESET_RUN_COUNT=[llength $ordered_runs]"
}

proc ::postextract_export::verify_fresh_ooc_runs {target_generation_epoch} {
    variable project_dir
    variable bd_file
    variable expected_ooc_stems

    set hwh [file join $project_dir three_stage_combined.gen sources_1 bd tme_bd \
        hw_handoff tme_bd.hwh]
    set present_runs 0
    set cache_materialized 0
    foreach stem $expected_ooc_stems {
        set run_name ${stem}_synth_1
        set runs [get_runs -quiet $run_name]
        if {[llength $runs] > 1} {
            fail "OOC run $run_name is ambiguous: $runs"
        }
        if {[llength $runs] == 1} {
            incr present_runs
            set run [lindex $runs 0]
            set status [get_property STATUS $run]
            set progress [get_property PROGRESS $run]
            if {$progress ne "100%" || ![string match "*Complete*" $status]} {
                fail "retained OOC run $run_name did not complete: $status ($progress)"
            }
        } else {
            # Vivado 2025.2 removes a transient create_ip_run object after a
            # config_ip_cache hit and materializes its DCP directly in .gen.
            incr cache_materialized
        }

        set dcp [file join $project_dir three_stage_combined.gen sources_1 bd \
            tme_bd ip $stem ${stem}.dcp]
        if {![file isfile $dcp] || [file size $dcp] == 0 ||
            [file mtime $dcp] < $target_generation_epoch ||
            [file mtime $dcp] < [file mtime $bd_file] ||
            [file mtime $dcp] < [file mtime $hwh]} {
            fail "generated OOC DCP is missing, empty, or stale for $run_name: $dcp"
        }
    }
    puts "OOC_REGISTRY=retained_runs:$present_runs;cache_materialized:$cache_materialized"
    puts "@@POSTEXTRACT_ALL_OOC_FRESH_PASS@@"
}

proc ::postextract_export::validate_hwh {hwh} {
    set text [::postextract::read_file $hwh]
    foreach fragment {
        {VLNV="TermCount:hls:binarize_core:2.0"}
        {VLNV="TermCount:hls:patch_extract_core:0.1"}
        {INSTANCE="axi_dma_binarize"}
        {INSTANCE="dma_pe_data"}
        {INSTANCE="dma_pe_meta"}
        {INSTANCE="smartconnect_pe_mem"}
        {<PARAMETER NAME="C_SG_LENGTH_WIDTH" VALUE="26"/>}
        {S_AXI_HP2}
    } {
        if {[string first $fragment $text] < 0} {
            fail "generated HWH is missing required evidence: $fragment"
        }
    }
}

proc ::postextract_export::write_manifest {path entries} {
    set handle [open $path w]
    try {
        puts $handle "build=three_stage_combined"
        puts $handle "vivado=[version -short]"
        puts $handle "part=[get_property PART [current_project]]"
        puts $handle "generated_utc=[clock format [clock seconds] -gmt 1 -format {%Y-%m-%dT%H:%M:%SZ}]"
        puts $handle "binarize_core=TermCount:hls:binarize_core:2.0"
        puts $handle "binarizer_dma_sg_length_width=26"
        puts $handle "patch_extract_core=TermCount:hls:patch_extract_core:0.1"
        puts $handle "memory_ports=HP0:matcher,HP1:binarizer,HP2:patch_extract"
        foreach {key value} $entries { puts $handle "$key=$value" }
    } finally {
        close $handle
    }
}

proc ::postextract_export::generate_and_export_bitstream {route_report_dir repair_info} {
    variable project_dir
    variable jobs

    catch {close_design}
    set impl_run [::postextract::one "impl_1 run" [get_runs -quiet impl_1]]
    set route_dcp [file join [get_property DIRECTORY $impl_run] tme_bd_wrapper_routed.dcp]
    if {![file isfile $route_dcp] || [file size $route_dcp] == 0} {
        fail "route gate passed without a routed implementation DCP: $route_dcp"
    }
    set source_route_hash [sha256 $route_dcp]

    set launch_epoch [clock seconds]
    puts "IMPLEMENTATION_TARGET=write_bitstream"
    puts "BITSTREAM_REQUESTED=1"
    launch_runs $impl_run -to_step write_bitstream -jobs $jobs
    wait_on_run $impl_run
    set status [get_property STATUS $impl_run]
    set progress [get_property PROGRESS $impl_run]
    puts "BIT_IMPL_STATUS=$status"
    puts "BIT_IMPL_PROGRESS=$progress"
    if {$progress ne "100%" || ![string match -nocase "*write_bitstream*Complete*" $status]} {
        fail "write_bitstream did not complete: $status ($progress)"
    }
    if {[lsearch -exact [list_property $impl_run] NEEDS_REFRESH] >= 0 &&
        [get_property NEEDS_REFRESH $impl_run]} {
        fail "impl_1 is marked NEEDS_REFRESH after write_bitstream"
    }
    if {[sha256 $route_dcp] ne $source_route_hash} {
        fail "write_bitstream changed the route-gated implementation DCP: $route_dcp"
    }

    set impl_dir [file normalize [get_property DIRECTORY $impl_run]]
    set source_bit [file join $impl_dir tme_bd_wrapper.bit]
    set source_hwh [file join $project_dir three_stage_combined.gen sources_1 bd \
        tme_bd hw_handoff tme_bd.hwh]
    foreach {label path} [list bitstream $source_bit HWH $source_hwh] {
        if {![file isfile $path] || [file size $path] == 0 || [file mtime $path] < $launch_epoch} {
            if {$label eq "HWH" && [file isfile $path] && [file size $path] > 0 &&
                [file mtime $path] >= [file mtime $::postextract::bd_file]} {
                continue
            }
            fail "$label is missing, empty, or stale: $path"
        }
    }
    validate_hwh $source_hwh
    if {[sha256 $::postextract::bd_file] ne [dict get $repair_info bd_sha256]} {
        fail "preserved BD changed after metadata repair"
    }
    if {[sha256 $source_hwh] ne [dict get $repair_info hwh_sha256]} {
        fail "HWH changed after the forced target-generation gate"
    }

    set stamp [clock format [clock seconds] -format %Y%m%d_%H%M%S]
    set bundle [file join $project_dir "postextract_board_bundle_$stamp"]
    file mkdir $bundle
    set export_bit [file join $bundle three_stage_combined.bit]
    set export_hwh [file join $bundle three_stage_combined.hwh]
    set bit_hash [copy_required $source_bit $export_bit "bitstream export"]
    set hwh_hash [copy_required $source_hwh $export_hwh "HWH export"]
    set accepted_route [file join $route_report_dir postextract_routed.dcp]
    set accepted_route_hash [copy_required $accepted_route \
        [file join $bundle three_stage_combined_routed.dcp] "routed DCP export"]

    set manifest [file join $bundle BUILD_INFO.txt]
    write_manifest $manifest [list \
        route_report_dir $route_report_dir \
        bit_file [file tail $export_bit] \
        bit_bytes [file size $export_bit] \
        bit_sha256 $bit_hash \
        hwh_file [file tail $export_hwh] \
        hwh_bytes [file size $export_hwh] \
        hwh_sha256 $hwh_hash \
        bd_sha256 [dict get $repair_info bd_sha256] \
        project_sha256 [sha256 $::postextract::project_file] \
        source_route_dcp_sha256 $source_route_hash \
        routed_dcp_file three_stage_combined_routed.dcp \
        routed_dcp_sha256 $accepted_route_hash]

    puts "BOARD_BUNDLE=$bundle"
    puts "BITSTREAM=$export_bit"
    puts "HWH=$export_hwh"
    puts "BUILD_INFO=$manifest"
    puts "BITSTREAM_GENERATED=1"
    return $bundle
}

proc ::postextract_export::main {} {
    variable project_dir
    variable project_file

    if {[version -short] ne "2025.2"} {
        fail "use Vivado 2025.2; current version is [version -short]"
    }
    if {[llength [get_projects -quiet]] != 0} {
        fail "start this repair/export driver in a fresh Vivado process"
    }
    snapshot_recovery_inputs
    open_project $project_file
    ::postextract::expect_text "project name" [get_property NAME [current_project]] three_stage_combined
    ::postextract::expect_text "target part" \
        [string tolower [get_property PART [current_project]]] xc7z020clg400-1
    if {![string equal -nocase [file normalize [get_property DIRECTORY [current_project]]] $project_dir]} {
        fail "opened the wrong project directory: [get_property DIRECTORY [current_project]]"
    }

    puts "@@POSTEXTRACT_FULL_REBUILD_BEGIN@@"
    set repair_info [repair_project_metadata]
    reset_all_runs
    ::postextract::run_synthesis
    verify_fresh_ooc_runs [dict get $repair_info target_generation_epoch]
    set route_report_dir [::postextract::run_route 0]
    if {![file isdirectory $route_report_dir]} {
        fail "this route run did not return its report directory: $route_report_dir"
    }
    puts "@@POSTEXTRACT_ROUTE_SIGNOFF_PASS@@"
    set bundle [generate_and_export_bitstream $route_report_dir $repair_info]
    close_project
    puts "FINAL_BOARD_BUNDLE=$bundle"
    puts "@@POSTEXTRACT_FULL_EXPORT_PASS@@"
}

set rc [catch {::postextract_export::main} message options]
if {$rc} {
    puts stderr $message
    if {[dict exists $options -errorinfo]} { puts stderr [dict get $options -errorinfo] }
    catch {close_design}
    catch {close_project}
    exit 1
}
exit 0
