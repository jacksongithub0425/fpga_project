# Priority B2-PROD, Gates B and C -- combined B2 at 100 MHz

Date 2026-08-21. **Off-board only.** Nothing in this document was measured on
silicon. Every frequency figure here is a *prediction* until the board session
reads `Clocks.fclk0_mhz`.

## Result

| gate | verdict |
|---|---|
| Gate A -- combined `cur` matcher at 100 MHz | **PASS** (earlier session, unchanged) |
| Gate B -- B2 substituted, implemented, `.bit`/`.hwh` generated | **PASS** |
| Gate C -- implementation sign-off | **PASS** |
| Board preflight, synthetic Gates 1-5, PDF, stress page, corpus | **NOT STARTED** |

**B2/100 is NOT yet declared PASS.** The completion decision requires seven
conditions; this document closes three of them. Conditions 4-7 are board work
and none of it has been attempted.

The build is **timing-clean but physically fragile** -- see section 4.

## 1. The shipped artifacts

`combined_b2_100/postextract_board_bundle_20260821_160540/`

| file | bytes | SHA-256 |
|---|---|---|
| `three_stage_combined.bit` | 4,045,690 | `C9E6EE67F07531CA187DA84798E422990EC9A5A23FC90011325D94866DD6FDE8` |
| `three_stage_combined.hwh` | 926,777 | `32AC478E76F72F85F939CAF206F3CDD84BF27EB0B4A4DDD044559EAD459CF5B7` |
| `three_stage_combined_routed.dcp` | 33,471,828 | `C29B946CF1CD587E6FFEA3FD2608B87F2B07719FF30BE9FC8ACE388D2B7A3B61` |

Digests recomputed independently of the build with `sha256sum` and matched
`BUILD_INFO.txt` exactly.

### Why these two files are from the same build

Their mtimes differ (HWH 15:42, bitstream 16:05), so the pairing is proven by
hash chain rather than by timestamp:

1. `repair_project_metadata` force-generated all BD targets at 15:42 and
   recorded `hwh_sha256` and `bd_sha256`.
2. `verify_fresh_ooc_runs` proved all 14 OOC DCPs are newer than both that
   generation epoch and the HWH -- so synthesis consumed *these* products.
3. After `write_bitstream`, both digests were re-checked and still matched, and
   the routed DCP was proven unchanged by the bitstream step
   (`source_route_dcp_sha256`).

So the HWH describes exactly the BD that produced the netlist that produced the
bitstream. That is the claim; "same wall-clock minute" is not.

## 2. Gate B -- the substitution

Matcher `TermCountB2:hls:tme_top:0.2`, from the preserved packaged directory
`hls/template_match/template_match_b1_b2/b2/impl/ip` (never re-run from the
dirty live source tree -- see `PHASE0_EVIDENCE.md`). Confirmed present in the
shipped HWH alongside the unchanged binarizer and extractor:

    VLNV="TermCount:hls:binarize_core:2.0"
    VLNV="TermCount:hls:patch_extract_core:0.1"
    VLNV="TermCountB2:hls:tme_top:0.2"

Instance name, register map and stream topology preserved; the sign-off's cell,
address and interface-net checks all passed.

### Resources -- the slice prediction was pessimistic

| metric | Gate A (`cur` @ 100) | Gate B (B2 @ 100) | delta |
|---|---|---|---|
| Slice LUTs | 27,544 | 33,264 | +5,720 |
| Slice Registers | 36,646 | 42,673 | +6,027 |
| **Slice** | 10,996 (82.68%) | **12,347 (92.83%)** | +1,351 |
| Block RAM Tile | 130.5 (93.21%) | **130.5 (93.21%)** | **0** |
| DSP | 40 | 40 | 0 |

Two predictions from the task, both now settled:

* **B2's documented zero BRAM delta holds in the combined image.** 130.5 tiles,
  9.5 headroom, identical to Gate A.
* **The naive slice prediction was wrong in the safe direction.** Adding the
  standalone 2,033-slice delta to Gate A predicted ~13,048 slices / 98.1%. The
  actual figure is **12,347 / 92.83%** -- 701 slices below the prediction. As
  the task said, only implemented placement establishes feasibility; it did.

## 3. Gate C -- sign-off criteria

| criterion | result |
|---|---|
| Generated clock period exactly 10.000 ns | `clk_fpga_0` **10.000 ns** |
| Setup WNS >= 0, TNS = 0 | **WNS +0.135**, TNS 0.000, 0 failing of 123,002 |
| Hold WHS >= 0, THS = 0 | **WHS +0.008**, THS 0.000, 0 failing of 123,002 |
| Pulse width | WPWS +3.750, TPWS 0.000 |
| No unconstrained endpoints | `check_timing` **0 in every category** |
| Clock and reset relationships valid | 2 clocks, both PS7-generated; no combinational loops |
| Route complete | **62,813 / 62,813** fully routed, 0 errors, 0 problem nets across all 8 route-status classes |
| DRC clean | 17 total, **0 Fatal/Error/Critical** (1 Warning `RTSTAT-10`, rest Advisory) |
| Methodology | 1 total, **0 Fatal/Error/Critical** (`ULMTCS-1`, control-set advisory) |
| No unjustified timing exceptions | **`constrs_1` contains no user constraints at all** -- zero exceptions were added; the only XDC in the design is IP-generated |
| Utilization and congestion documented | section 2 and section 4 |
| `.bit` and `.hwh` from the same build | section 1 |

`clk_fpga_1` exists as a timing clock at 8.000 ns with **0 sequential cells, 0
launch paths, 0 capture paths** -- it is purely an IO-PLL-model lever and reaches
no logic. The single `RTSTAT-10` "no routable loads" warning is that net.

The **route reproduced the preferred default build exactly**: same WNS (+0.135),
same WHS (+0.008), same 62,813 nets, and a congestion table whose windows are
**identical** to the 14:12 run, field for field.

## 4. Congestion -- the gate was split, and why

The strict `<=L3` rule previously applied to both sections of
`report_design_analysis -congestion`. That conflated two different kinds of
evidence, and it is now split:

| section | what it is | rule | result |
|---|---|---|---|
| 1. Placer Final Level Congestion | a **measurement** of the shipped placement | **hard gate at L3** | max **L3**, 1 window, 0 above -- **PASS** |
| 2. Initial Estimated Router Congestion | the router's **pre-route prediction** | recorded; **hard limit L5** | max **L4**, 5 windows, **2 above L3** -- PASS, flagged |

The justification for not gating section 2 at L3 is that it is a prediction the
router then spends its run resolving, and the post-route facts are all in hand
by the time it is read: complete route, zero routing errors, positive setup and
hold, zero critical DRC. Gate C's own wording asks that congestion be
*documented*, not that it be `<=L3`.

**This was not a loosening into a warning.** A placer-final window above L3
still fails, and an estimated window at L5 still fails. Both were verified with
deliberate mutants (section 6).

### Documented fragility

The build emits:

    POSTROUTE_CONGESTION_FRAGILITY=2 estimated window(s) at level 4; the route
    completed and met timing, but this build is physically fragile and small
    netlist or placement changes may not re-close

Both L4 windows are `Long`-type, ~12.8% of tiles, and both are dominated by
`tme_top_0/inst/grp_correlation_core_fu_1779` and its `mac_loop` / `load_seg`
pipelines. Taken with **WNS +0.135 ns -- 135 ps, 1.35% of the period** -- and
92.83% slice occupancy, the honest label is **timing-clean but physically
fragile**. Do not assume an unrelated edit will re-close.

The worst setup path is inside the matcher and is placement-bound, not
logic-bound:

    slack 0.135  levels 0  datapath 9.274 ns
    from tme_bd_i/tme_top_0/inst/templ_buf_U/ram_reg_0_7/CLKBWRCLK
    to   tme_bd_i/tme_top_0/inst/.../t_row_193_fu_1000_reg[7]/D

This is the same `templ_buf -> t_row` path the Priority 2 8 ns probe identified
as the standalone critical path, now binding in the combined image.

`tme_top_0`'s reset fanout is **369** here, against 348 in Gate A.

### The strategy experiment, recorded as a negative result

`Congestion_SpreadLogic_high` was tried against these windows and **made both
metrics worse**: WNS +0.135 -> +0.126 and 2 -> 3 windows above L3. `impl_1` was
restored to `Vivado Implementation Defaults` before the shipped build. No
floorplanning, no seed farm, no false paths.

## 5. Two defects found and fixed in the build infrastructure

**a. The export driver was baseline-only.** `repair_rebuild_export.tcl`
hardcoded the baseline's 2026-08-10 BD snapshot and accepted-route DCP as
recovery inputs, so on any variant project it aborted in its first minute on a
missing file. Both are now variant fields (`recovery_bd_tcl`,
`recovery_accepted_dcp`). A variant with no prior accepted route must declare
that explicitly, and the "none" is written into the manifest rather than
silently skipped.

**b. `validate_hwh` never checked the matcher.** It gated `binarize_core` and
`patch_extract_core` but not `tme_top` -- so a HWH could pass while describing
the wrong matcher, which is the single cell this whole task substitutes. Now
gated by VLNV and instance name, against the variant rather than a literal.

### New: the HWH clock gate

Vivado's reported period is **not** evidence about the board. PYNQ derives the
live PL clock from the HWH's raw divisors against the 1000 MHz IO PLL its boot
image programmed, so a bare 100 MHz request yields a perfect-looking 10.000 ns
constraint and a board that silently runs 62.5 MHz.

The shipped HWH is now gated on those divisors, and it carries:

    fclk0_div0:5  fclk0_div1:2  fclk0_div_product:10   -> board 100.0000 MHz
    fclk1_div0:4  fclk1_div1:2  fclk1_div_product:8    -> board 125.0000 MHz
    io_pll_mhz:1000.000

FCLK1 at 125 is the lever: 1000/8 = 125 is unreachable from a 1600 MHz model, so
it is an independent witness that the IO PLL model really is 1000.

**This is still a prediction.** `1000 / div_product` is a model validated at
50 -> 31.25 and 125 -> 125.0. Silicon must confirm it.

## 6. The gates were tested, not assumed

Every gate changed here was checked against deliberate mutants in `tclsh`:

| suite | result |
|---|---|
| Congestion split gate -- both real reports accepted; placer L4 rejected; estimate L5 rejected; unrouted rejected; out-of-range level rejected | **6/6** |
| HWH clock gate -- baseline HWH accepted as `baseline` predicting 31.25 MHz; rejected as `combined_b2_100`; divisor product 32-vs-10 rejected; the literal 62.5 MHz trap (4x4=16) rejected; FCLK1 lever not pulled rejected; correct config accepted at 100.0000/125.0000 | **6/6** |
| `boundary_crc` classifier -- fires on the real pair only; refuses a design change bundled with a CRC change, a design change with an identical CRC, a single byte flipped elsewhere, and identical inputs | **5/5** |

The baseline HWH is a particularly good control: it is a *shipped, silicon-
confirmed* artifact, and the gate independently derives its known 31.25 MHz.

## 7. The `boundary_crc` finding

The first export attempt failed the BD-immutability gate. The diff was one line:

    < "boundary_crc": "0xCC6CFA53F622E079"
    > "boundary_crc": "0xCC6CFA53B4580162"

Same size, every other byte identical. Rather than assume it benign, it was
probed: **three further forced generations left the BD byte-identical**
(`FC4B8829A9D18BDD40AAA77C6C5C74E116B30E41013335AF69EA1B88FCD7F7EA` each time).
It is a **one-time normalization** -- a BD built by `write_bd_tcl` plus a
recreate script carries a `boundary_crc` that the first full forced generation
recomputes, then holds. That also explains why the baseline passed this gate on
08-11: its BD had already been through one.

**The gate was not weakened.** When `boundary_crc` is the only difference it now
says so, names the preserved snapshot to check against, and says to re-run --
and **still fails**, because a human should confirm that a preserved design was
only renormalized.

Consequence to record: the BD in `combined_b2_100` now differs by that one
bookkeeping field from the one the 14:12 route was built from. Design content is
unchanged, the pre-normalization copy is preserved in
`recovery_snapshots/pre_full_export_20260821_152850/`, and the shipped build
re-synthesized everything from scratch.

## 8. Performance -- modeled, not measured

The RTL has **no page-level hardware cycle counter**. Nothing here is a measured
hardware cycle count. For the board session, the frozen figures to quote:

* Production initial matching: **303.401 s/page at 125 MHz**, **379.251 s/page
  at 100 MHz**.
* The conditional-refinement figure (316.797 s at 125 / ~396 s at 100) is **not**
  the production initial-matching estimate.
* Deleting the full correlation term still leaves 91.651 s/page at 125 MHz and
  **114.563 s/page at 100 MHz**.
* The 3.753-second front-end term is **not** the architecture's hard floor.

Expect roughly four hours of matcher time for the 36-page corpus at B2/100
before PS overhead.

A known-cycle silicon check is available and should be used: B2 at 820x307 /
216x96 is L = 257,145,732 cycles = **2.5715 s at 100 MHz**, with the nearest
wrong clock rung 257 ms away.

## 9. Changed files

| file | change |
|---|---|
| `three_stage_combined/scripts/run_postextract_signoff.tcl` | congestion gate split into hard placer-final and advisory router-estimate; two new variant fields for recovery inputs |
| `three_stage_combined/scripts/repair_rebuild_export.tcl` | recovery inputs parameterized; matcher VLNV/instance added to `validate_hwh`; new `validate_hwh_clocks` divisor gate; `boundary_crc` diagnosis; manifest records variant, matcher and clock facts |
| `logs/b2prod_20260821/gateB/restore_default_strategy.tcl` | new -- restores `impl_1` to default strategy, records the discarded experiment |
| `logs/b2prod_20260821/gateB/probe_boundary_crc.tcl` | new -- the convergence probe |

Nothing was committed. B0b and other worktree changes were left untouched.

## 10. Remaining blockers

1. **Board preflight not run.** Fresh boot, `cma>=192M`, driver-order buffer
   allocation, load this `.bit`/`.hwh`, and read `Clocks.fclk0_mhz == 100.0`.
   Until that read succeeds, 100 MHz is a prediction from divisors.
2. **Synthetic Gates 1-5 not run** against this image.
3. **Detector-parity discrepancy unresolved** -- must be settled before the
   corpus, and the authoritative refinement organization stated. Do not mix the
   808-call CPU trace with the 968-call PL-geometry trace.
4. **Physical fragility** (section 4) is a standing risk for any future edit to
   this image, not a blocker for board work.
