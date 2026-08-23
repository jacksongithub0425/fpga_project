# Source the parameterized sign-off script in library mode and print the
# resolved configuration for every variant.  Builds nothing.
set script "C:/Users/lychee/Desktop/FPGA/three_stage_combined/scripts/run_postextract_signoff.tcl"

puts "@@VARIANT_SELFTEST_BEGIN@@"

# default (no env var) must reproduce the pre-parameterization hardcoded values
if {[info exists ::env(B2PROD_VARIANT)]} { unset ::env(B2PROD_VARIANT) }
namespace eval ::postextract { variable library_only 1 }
source $script
puts "default_variant=$::postextract::variant"
foreach f {matcher_vlnv fclk0_mhz period_ns fclk1_mhz div_product report_prefix} {
    puts [format "  default %-14s = '%s'" $f [::postextract::vcfg $f]]
}
puts "  default expected_clocks = {[::postextract::expected_clocks]}"

# baseline must equal the values this script hardcoded before parameterization
set ok 1
foreach {f want} {matcher_vlnv TermCount:hls:tme_top:0.2 fclk0_mhz 50 period_ns 20.0 \
                  div_product 32 report_prefix postextract} {
    if {[::postextract::vcfg $f] ne $want} {
        puts "BASELINE_REGRESSION: $f = '[::postextract::vcfg $f]', expected '$want'"
        set ok 0
    }
}
if {[::postextract::vcfg fclk1_mhz] ne ""} {
    puts "BASELINE_REGRESSION: fclk1_mhz should be empty"; set ok 0
}
if {[::postextract::expected_clocks] ne {clk_fpga_0}} {
    puts "BASELINE_REGRESSION: expected_clocks should be {clk_fpga_0}"; set ok 0
}
puts [expr {$ok ? "BASELINE_DEFAULTS_RETAINED=PASS" : "BASELINE_DEFAULTS_RETAINED=FAIL"}]

# now walk every variant
foreach v [dict keys $::postextract::variants] {
    set ::postextract::variant $v
    set prod [::postextract::vcfg div_product]
    puts [format "VARIANT %-22s vlnv=%-28s fclk0=%-4s period=%-5s fclk1=%-4s clocks={%s} prefix=%s predicted_board=%.4fMHz" \
        $v [::postextract::vcfg matcher_vlnv] [::postextract::vcfg fclk0_mhz] \
        [::postextract::vcfg period_ns] \
        [expr {[::postextract::vcfg fclk1_mhz] eq "" ? "off" : [::postextract::vcfg fclk1_mhz]}] \
        [::postextract::expected_clocks] [::postextract::vcfg report_prefix] \
        [expr {1000.0 / $prod}]]
}

# a typo must be fatal, not a silent fall-back to baseline
set ::postextract::variant combined_b2_1OO
if {[catch {::postextract::vcfg matcher_vlnv} e]} {
    puts "TYPO_IS_FATAL=PASS ($e)"
} else {
    puts "TYPO_IS_FATAL=FAIL -- returned '$e'"
}
puts "@@VARIANT_SELFTEST_END@@"
