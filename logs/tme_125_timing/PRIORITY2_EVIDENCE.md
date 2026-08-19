# Priority 2 — physical clock probe evidence, 125 MHz

Unchanged RTL (`TermCount:hls:tme_top:0.2`), standalone image (core + 2 DMAs),
xc7z020clg400-1, built with `TME_FCLK_MHZ=125` so the constraint is exactly
**8.000 ns** — no approximation to caveat.

All five items Priority 2 asked for, from the routed checkpoint
`tme_standalone.runs/impl_1/tme_bd_wrapper_routed.dcp`, regenerated
2026-08-17 23:40 by `vivado/tme_standalone/report_congestion_125.tcl`.

## 1–2. WNS and TNS

| | value |
|---|---|
| clock | `clk_fpga_0` |
| constrained period | 8.000 ns |
| **WNS** | **+0.064 ns** |
| **TNS** | **0.000 ns** |
| WHS | +0.015 ns |
| THS | 0.000 ns |
| verdict | all constraints met |

TNS and THS are 0.000 by direct evidence, not by transcription: querying
`get_timing_paths -slack_lesser_than 0` returned *no matching paths* in both
the max and min domains. There are no failing endpoints.

These match the figures the original build reported from the run object
(`STATS.WNS` +0.063836), so two independent extraction paths agree.

## 3. Utilization

| resource | used | available | % |
|---|---|---|---|
| Slice LUTs | 14,903 | 53,200 | 28.01 |
| Slice Registers | 18,467 | 106,400 | 17.36 |
| **Block RAM Tile** | **115** | **140** | **82.14** |
| DSPs | 34 | 220 | 15.45 |
| F7 Muxes | 918 | 26,600 | 3.45 |
| F8 Muxes | 312 | 13,300 | 2.35 |
| Bonded IOB | 0 | 125 | 0.00 |

**BRAM at 82.1% is the binding resource**, not logic. Any B-series change that
adds buffering competes for 25 remaining tiles.

## 4. Congestion — the item that was missing until now

    Placer Final Level Congestion Reporting
      * No congestion windows are found above level 5

    Initial Estimated Router Congestion Reporting
      * No initial estimated congestion windows are found above level 5

**The design is not congested.** Neither the placer's final view nor the
router's initial estimate found a single window above the level-5 reporting
threshold, in any direction.

This matters for the B-series: 67.9% of the critical path is routing delay,
which could have meant the router was fighting congestion. It was not. The
routing delay is distance and fanout on an uncongested die, so the fix is to
shorten the path, not to relieve pressure — and there is placement headroom for
a larger core, with BRAM rather than routing as the limit.

## 5. Critical path

    from : tme_bd_i/tme_top_0/inst/templ_buf_U/ram_reg_0_0/CLKBWRCLK
    to   : tme_bd_i/tme_top_0/inst/grp_tme_top_Pipeline_VITIS_LOOP_150_3_fu_1446
           /t_row_81_fu_1448_reg[0]/D

| | |
|---|---|
| logic levels | **0** |
| data delay | 7.642 ns |
| — of which logic | 2.454 ns (33%) |
| — of which routing | 5.188 ns (67%) |
| clock skew | −0.064 ns |
| clock uncertainty | 0.125 ns |
| routes | 1 |
| **high fanout** | **216** |
| logical path | `RAMB36E1/CLKBWRCLK -(216)- FDRE/D` |

**Logic levels 0 with fanout 216.** This is one BRAM output driving 216
flip-flops through a single route — clock-to-out plus distribution, with no
combinational logic in between. 216 is `MAX_TEMPL_W`: the fully partitioned
`t_row[]` staging array at `tme_top.cpp:178`.

That names the fix precisely. There is no logic to retime and no congestion to
relieve; the only levers are reducing the fanout (feed the MAC one pixel per
cycle from BRAM instead of broadcasting a row) or registering the BRAM output
to split the distribution across two cycles. The first addresses cycles *and*
slack, the second only slack.

For context, the second-worst path is a 6-level LUT/MUXF chain at +1.182 ns —
**18x more slack**. The template-staging fanout is the sole binding structure.

## Scope

This is the unchanged core at 8.000 ns. It says nothing about a modified core:
with +0.064 ns of margin, any B-series change must be re-timed. Board
measurement of this bitstream is separate evidence — see
`logs/board_125mhz_gate/`.

## Provenance

Generated read-only from the routed checkpoint. `overlay_output/` was neither
opened nor modified: `build_tme_standalone.tcl`'s `TME_REPORT_ONLY=1` path
glob-deletes that directory before regenerating, and its `.bit`/`.hwh` are
hash-bound evidence (`logs/board_125mhz_gate/PROBE125_ARTIFACTS.sha256`, board
copies corroborated 2026-08-18). Both hashes were verified unchanged after each
Vivado invocation.

Full reports (216 KB timing summary, 108 KB worst paths) stay in the build root
at `tme_standalone_125/timing_evidence/`; the three compact ones are copied
here.
