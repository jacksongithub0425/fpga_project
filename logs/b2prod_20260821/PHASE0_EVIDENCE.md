# Priority B2-PROD, Phase 0 -- preflight and build parameterization

Date 2026-08-21. Off-board only. Nothing was synthesized, implemented or run on
silicon in this phase.

## Result

Phase 0 is COMPLETE and the task is cleared to proceed to Gate A, with **one
substantive finding that changes how Gate A must be built** (section 4).

## 1. The preserved B2 release candidate

| item | value |
|---|---|
| IP directory | `C:/Users/lychee/Desktop/FPGA/hls/template_match/template_match_b1_b2/b2/impl/ip` |
| VLNV | `TermCountB2:hls:tme_top:0.2` |
| `component.xml` SHA-256 | `ea0d8a051812efef6fce51906ebd89b24a8a9aa185496ff72a18cd8cf9f771f4` |
| export time | 2026-08-19 19:16 |
| git revision | `9301cdeb1e1244c02199b599a52f4e827d93952a`, branch `agent/publish-hls-sw-docs` |
| working tree | **DIRTY** -- 35 entries, all Priority 6 / B0b |

The digest equals the value pinned by the Priority 5 manifest, so this is the
same directory that fed the B2 standalone image. No new hash infrastructure was
added; the digest above was recomputed with `sha256sum` for this record only.

The directory (not the ZIP beside it) is now the fourth entry of `ip_repo_paths`
in `three_stage_combined/scripts/repair_rebuild_export.tcl`.

**HLS was not re-run, and must not be.** `hls/template_match/tme_top.cpp` is
modified against HEAD by in-flight B0b work, so a fresh HLS run from this tree
would not reproduce B2. This is the concrete reason the preserved `impl/ip`
directory is mandatory rather than merely convenient.

All B0b and worktree changes were left untouched.

### Catalogue resolution -- PASS

All four repositories resolve, and both matchers coexist because they differ in
vendor (`TermCount` vs `TermCountB2`):

    all tme_top ipdefs: TermCount:hls:tme_top:0.2 TermCountB2:hls:tme_top:0.2

Full transcript: `clock_probe/catalog.txt`.

## 2. Structural compatibility -- PASS on every point

Baseline `template_match_provisional/solution1/impl/ip` vs B2.

| check | result |
|---|---|
| AXI-Lite register interface and offsets | `xtme_top_hw.h` **byte-identical** |
| Control protocol | `tme_top_CTRL_s_axi.v` **byte-identical** |
| Bus interfaces | identical set, types and modes (6: `s_axi_CTRL`, `ap_clk`, `ap_rst_n`, `interrupt`, `patch_stream`, `templ_stream`) |
| AXI-Stream ports and widths | identical |
| Clock and reset ports | identical |
| Port directions | identical |
| Configured maximum dimensions | identical |

"Identical" for the port table means the full `spirit:model/ports` list --
names, directions and bit ranges -- diffs empty.

Maxima were checked in the generated RTL rather than inferred from the header,
because `tme_top.h` was modified on 2026-08-06, *after* the baseline IP was
packaged on 2026-08-04. The memory-geometry multiset is identical:

    AddressRange 20736  = 216 x 96   MAX_TEMPL_W x MAX_TEMPL_H
    AddressRange 15964  =  52 x 307  T_MAX x MAX_PATCH_H
    AddressRange 52 (x3)             T_MAX
    AddressWidth 15, 14, 6 (x3)

The commit that touched `tme_top.h` (`25caa0b`) does not modify any `MAX_`
constant, which corroborates the RTL evidence.

The baseline packages 48 HDL files and B2 packages 49; **45 names are common,
and 36 of those 45 are byte-identical**. The 9 that differ are exactly B2's
change surface plus the top level: `tme_top.v`, `correlation_core.v`,
`Pipeline_load_seg`, `Pipeline_mac_loop`, `isq_init`, `isq_slide`, `norm_cols`,
`reset_acc`, and the deadlock-monitor header.

Of the remaining files, 3 are baseline-only and 4 are B2-only. Six of those
seven are three **rename pairs** -- `load_patch_VITIS_LOOP_96_1` ->
`..._116_1`, `load_templ_VITIS_LOOP_107_2` -> `..._127_2`, and
`VITIS_LOOP_150_3` -> `..._181_3` -- because HLS names modules and internal
signals after the source line they came from, and the B2 source grew. Each pair
diffs **empty** once `VITIS_LOOP_<n>_` and `_ln<n>` identifiers are normalized,
so all three are pure renames: the patch and template **load path is
structurally unchanged**. The one genuinely new module is B2's
`tme_top_correlation_core_Pipeline_refill_seg`.

**This is structural ABI evidence only.** Result semantics, `TLAST`, ordering
and tie behaviour remain unproven by it and still require cosim and board
testing.

## 3. FCLK0 inventory -- one clock domain, 25 endpoints

Extracted from `tme_bd.bd`. The design is fully synchronous on
`processing_system7_0/FCLK_CLK0`; there is no second clock and no CDC, so
raising FCLK0 raises every core and every interconnect together.

| group | endpoints |
|---|---|
| PS7 | `FCLK_CLK0`, `M_AXI_GP0_ACLK`, `S_AXI_HP0_ACLK`, `S_AXI_HP1_ACLK`, `S_AXI_HP2_ACLK` |
| HLS cores (3) | `binarize_core_0/ap_clk`, `patch_extract_core_0/ap_clk`, `tme_top_0/ap_clk` |
| `axi_dma_binarize` | `m_axi_mm2s_aclk`, `m_axi_s2mm_aclk`, `s_axi_lite_aclk` |
| `axi_dma_patch` | `m_axi_mm2s_aclk`, `s_axi_lite_aclk` |
| `axi_dma_templ` | `m_axi_mm2s_aclk`, `s_axi_lite_aclk` |
| `dma_pe_data` | `m_axi_mm2s_aclk`, `m_axi_s2mm_aclk`, `s_axi_lite_aclk` |
| `dma_pe_meta` | `m_axi_s2mm_aclk`, `s_axi_lite_aclk` |
| SmartConnects (4) | `smartconnect_lite/aclk`, `smartconnect_mem/aclk`, `smartconnect_bin_mem/aclk`, `smartconnect_pe_mem/aclk` |
| reset | `proc_sys_reset_0/slowest_sync_clk` |

All five DMAs are present, 12 DMA clock pins in total.

## 4. FINDING -- requesting 100 MHz gives a 62.5 MHz board

This is the one Phase 0 result that changes the build.

Vivado constrains `clk_fpga_0` at `PCW_IO_IO_PLL_FREQMHZ / (div0 * div1)`.
The board runs it at `1000 MHz / (div0 * div1)`, because PYNQ writes the HWH's
**raw divisors** against the real 1000 MHz IO PLL that its own boot image
programmed. Our bitstream cannot move that PLL. The two agree only when
Vivado's IO PLL model also happens to be 1000 MHz.

That the divisors -- not the requested MHz -- are what PYNQ applies is settled
by the shipping image: it requests 50 and the board measures 31.25 = 1000/32.
Three independent points agree:

| build | request | div product | board fclk0 | source |
|---|---|---|---|---|
| combined + standalone shipping | 50 | 8 x 4 = 32 | **31.25** measured | 2026-08-07 |
| standalone 125 probe / b1 / b2 / b0b | 125 | 4 x 2 = 8 | **125.0** measured | `logs/board_125mhz_gate/01_load_and_clock.txt` |
| PYNQ base overlay, fresh boot | -- | (10) | **100.0** observed | `logs/board_125mhz_gate/00_preexisting_state.txt` |

### Measured PS7 solver behaviour

A preset-less PS7 was swept in Vivado 2025.2. The sweep **reproduces both
shipped HWH files exactly**, which is what makes its 100 MHz answer usable:

    request  IO PLL  div0 x div1   Vivado period   BOARD fclk0
    50       1600    8 x 4 = 32    20.000 ns        31.25 MHz   <- control, matches shipping
    100      1600    4 x 4 = 16    10.000 ns        62.50 MHz   <- TRAP
    125      1000    4 x 2 =  8     8.000 ns       125.00 MHz   <- control, matches b1/b2/b0b
    160      1600    5 x 2 = 10     6.250 ns       100.00 MHz

**A bare request of 100 produces a flawless-looking 10.000 ns constraint and a
board that silently runs at 62.5 MHz.** Every Vivado report would pass Gate C;
only the live `Clocks.fclk0_mhz` check would catch it. Requesting 160 inverts
the problem: the board would be right and the constraint would be 6.25 ns, which
B2 cannot close (it routed 8.000 ns standalone at WNS +0.011710 ns).

### The resolution

The solver keeps its default 1600 MHz IO PLL and only leaves it when no integer
divisor pair reaches the request. Enabling **FCLK1 at 125 MHz** (1600/125 is
not an integer) forces the model to 1000 MHz, after which FCLK0 = 100 lands on
5 x 2 = 10 and both numbers are 100:

    fclk0 100 + fclk1 125  ->  IO PLL 1000, div 5 x 2 = 10
                           ->  Vivado 100.0000 MHz / 10.0000 ns
                           ->  board  100.0000 MHz

Levers that were tried and do **not** work (`clock_probe/probe3_levers.txt`,
`clock_probe/probe4_refine.txt`):

* `CONFIG.PCW_IO_IO_PLL_FREQMHZ 1000` -- rejected by the IP.
* `CONFIG.PCW_IOPLL_CTRL_FBDIV 30` -- silently ignored, it is derived.
* `CONFIG.PCW_FCLK0_PERIPHERAL_DIVISOR0/1` -- silently ignored, also derived.
* `CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ 50` -- solver moves FBDIV to 32 and
  keeps the PLL at 1600.
* FCLK1 requested but not enabled, or enabled with `PCW_EN_CLK1_PORT 0` --
  the latter *clears* `PCW_FPGA_FCLK1_ENABLE` and the PLL falls back to 1600.
* FCLK1 at 200 MHz -- 1600/8 is exact, so the PLL does not move.
  FCLK1 at 250 MHz does work and is an equivalent alternative to 125.

So FCLK1 must be genuinely enabled **and** ported. `FCLK_CLK1` will drive no
logic; it exists only to pin the PLL model. Two consequences Gate A must carry:

1. `clk_fpga_1` joins the timing clock set, so the sign-off's "exactly one
   timing clock" assertion had to become variant-aware. It has zero fanout and
   therefore no timing paths, but this must be confirmed in the Gate A reports
   rather than assumed.
2. The overlay will program the board's fclk1 to 125 MHz (PYNQ default is
   142.857143). Nothing in the PL consumes it. Record and restore it in the
   board session exactly as the 2026-08-17 probe did.

**The prediction for this experiment was recorded before it ran**
(`clock_probe/PREDICTION.txt`) and was correct: the solver does prefer 1600 and
a bare 100 MHz request does yield a 62.5 MHz board.

## 5. Sign-off script parameterization

`three_stage_combined/scripts/run_postextract_signoff.tcl` now carries a variant
table selected by the `B2PROD_VARIANT` environment variable.

| field | baseline | combined_current_100 | combined_b2_100 |
|---|---|---|---|
| `matcher_vlnv` | `TermCount:hls:tme_top:0.2` | `TermCount:hls:tme_top:0.2` | `TermCountB2:hls:tme_top:0.2` |
| `fclk0_mhz` | 50 | 100 | 100 |
| `period_ns` | 20.0 | 10.0 | 10.0 |
| `fclk1_mhz` | off | 125 | 125 |
| `div_product` | 32 | 10 | 10 |
| `report_prefix` | `postextract` | `combined_current_100` | `combined_b2_100` |
| expected clocks | `clk_fpga_0` | `clk_fpga_0 clk_fpga_1` | `clk_fpga_0 clk_fpga_1` |
| predicted board | 31.25 MHz | 100.0000 MHz | 100.0000 MHz |

`div_product` is a stated field rather than a derived one. It is **not**
recoverable from `fclk0_mhz`, because it depends on which IO PLL the solver
picked -- and it is the only field that predicts the live board clock. An
earlier revision of this change derived it as `round(1000/fclk0_mhz)`, which
silently mispredicted the baseline as 50 MHz and would have made the baseline
variant fail its own preflight. Keeping it explicit also keeps the baseline's
31.25 MHz visible in the table instead of implied.

The default is `baseline`, whose values are exactly what the script hardcoded
before, so an invocation naming no variant checks the same design every recorded
report was produced from. A mistyped variant name is fatal rather than a silent
fall-back. Verified in `clock_probe/variant_selftest.txt`:

    BASELINE_DEFAULTS_RETAINED=PASS
    TYPO_IS_FATAL=PASS

New preflight output, which makes the board rate visible at build time instead
of only at board time:

    PREFLIGHT_CLOCK=variant:...;io_pll_mhz:...;div0:...;div1:...;product:...;
                    vivado_mhz:...;predicted_board_mhz:...

No separate XDC clock was invented or deleted; `clk_fpga_0` remains PS7
generated.

## 6. Changed files

| file | change |
|---|---|
| `three_stage_combined/scripts/run_postextract_signoff.tcl` | variant table + `vcfg`/`expected_clocks` helpers; VLNV, FCLK0 MHz, period, clock set and report prefixes parameterized; FCLK1 and divisor-product checks added |
| `three_stage_combined/scripts/repair_rebuild_export.tcl` | B2 `impl/ip` added as the fourth `ip_repo_paths` entry; required-VLNV list follows the variant; `IP_REPO=` echoed |
| `logs/b2prod_20260821/` | new, this document plus the clock-probe artifacts |

Not yet mirrored to `.github-upload`. Nothing committed.

## 7. What Phase 0 did NOT establish

* No synthesis, implementation, timing or utilization result exists yet for any
  100 MHz variant. Every resource figure in the task brief remains a diagnostic
  expectation, not a measurement.
* The BD has not been modified. Gate A still needs a configure step that sets
  FCLK0 = 100 and FCLK1 = 125 on the PS7 and regenerates the BD, wrapper and
  output products. The parameterized sign-off *verifies* that configuration; it
  does not *apply* it.
* Whether `clk_fpga_1` stays fanout-free and endpoint-free through
  implementation is expected but unverified.
* Structural ABI equality is not behavioural equality.

## 8. Known risk carried into Gate B

B2's standalone route closed 8.000 ns at WNS +0.011710 ns -- 0.15% of the
period. The combined image adds the binarizer, the extractor, five DMAs and
four SmartConnects on the same clock. The naive slice prediction in the task
brief is roughly 98.1% occupancy. Gate A exists precisely to separate a 100 MHz
platform problem from a B2 placement problem; do not attribute a Gate A failure
to B2.
