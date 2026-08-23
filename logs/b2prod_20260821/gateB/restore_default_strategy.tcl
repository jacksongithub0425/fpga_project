# Restore combined_b2_100/impl_1 to the DEFAULT implementation strategy.
#
# The Congestion_SpreadLogic_high experiment (2026-08-21 14:21) was run to see
# whether spreading logic would clear the level-4 windows in the router's
# pre-route congestion ESTIMATE.  It made both metrics worse:
#
#   default strategy         WNS +0.135  WHS +0.008  2 estimated windows > L3
#   Congestion_SpreadLogic_high  WNS +0.126  WHS +0.021  3 estimated windows > L3
#
# The default-strategy route is therefore the build we ship, and impl_1 has to
# be put back before the bitstream is generated from it.
set project_file "C:/Users/lychee/Desktop/FPGA/combined_b2_100/three_stage_combined.xpr"
open_project $project_file
set impl [get_runs impl_1]
set synth [get_runs synth_1]
set directive_steps {opt_design place_design phys_opt_design route_design post_route_phys_opt_design}

puts "IMPL_STRATEGY_BEFORE=[get_property STRATEGY $impl]"
puts "IMPL_DESCRIPTION_BEFORE=[get_property DESCRIPTION $impl]"
foreach step $directive_steps {
    set prop "STEPS.${step}.ARGS.DIRECTIVE"
    if {[llength [list_property -quiet $impl $prop]] > 0} {
        puts "STEP_DIRECTIVE_BEFORE=$step;VALUE:[get_property $prop $impl]"
    }
}

set_property strategy "Vivado Implementation Defaults" $impl

puts "IMPL_STRATEGY_AFTER=[get_property STRATEGY $impl]"
puts "IMPL_DESCRIPTION_AFTER=[get_property DESCRIPTION $impl]"
foreach step $directive_steps {
    set prop "STEPS.${step}.ARGS.DIRECTIVE"
    if {[llength [list_property -quiet $impl $prop]] > 0} {
        puts "STEP_DIRECTIVE_AFTER=$step;VALUE:[get_property $prop $impl]"
    }
}

# Synthesis must stay non-incremental so the netlist is reproducible from
# sources alone rather than from whatever checkpoint happened to be lying around.
set_property AUTO_INCREMENTAL_CHECKPOINT 0 $synth
set_property INCREMENTAL_CHECKPOINT {} $synth
set_property STEPS.SYNTH_DESIGN.ARGS.INCREMENTAL_MODE off $synth
puts "SYNTH_INCREMENTAL_MODE=[get_property STEPS.SYNTH_DESIGN.ARGS.INCREMENTAL_MODE $synth]"
puts "SYNTH_AUTO_INCREMENTAL_CHECKPOINT=[get_property AUTO_INCREMENTAL_CHECKPOINT $synth]"
puts "SYNTH_INCREMENTAL_CHECKPOINT=[get_property INCREMENTAL_CHECKPOINT $synth]"

if {[get_property STRATEGY $impl] ne "Vivado Implementation Defaults"} {
    puts stderr "RESTORE_FAILED: impl_1 strategy is [get_property STRATEGY $impl]"
    close_project
    exit 1
}
foreach step $directive_steps {
    set prop "STEPS.${step}.ARGS.DIRECTIVE"
    if {[llength [list_property -quiet $impl $prop]] > 0
        && [get_property $prop $impl] ne "Default"} {
        puts stderr "RESTORE_FAILED: $step directive is [get_property $prop $impl], expected Default"
        close_project
        exit 1
    }
}
close_project
puts "@@STRATEGY_RESTORED@@"
exit 0
