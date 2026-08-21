# Priority 6 — B0b: the hoisted, vertically-reused window statistics

**Status: implemented, correctness-checked, and cycle-measured. IT DOES NOT
CLOSE 8.000 ns.** Routed 2026-08-20 at the same default flow that closed B1 and
B2: **WNS −0.051470 ns, TNS −0.051470 ns, CONSTRAINTS VIOLATED**. The cycle term
below is measured and stands; the 125 MHz it is converted at does **not** have a
routed build behind it for this core. Not on silicon. See
[Routed timing](#routed-timing).

    B0b = B2 − [rh·th·(tw + rw + 24) + 3·rh] + [S·(pw + 30) + 5]
                                                S = th + 2·(rh − 1)

    aggregate over the 20,680-trial workload   79,767,161,516 cycles
    s/page at 125 MHz                          17.726035892444
    largest Phase-S trial (311×159 / 216×96)   14,950,652 cycles (−1,988,869 vs B2)

The **net** term is measured by paired RTL co-simulation over the same fourteen
invocations, and the net is what is frozen. **The two bracketed halves are not
separately owned** — see [The split is not
identified](#the-split-is-not-identified-and-cannot-be-by-this-method).
**The s/page is still a projection**: it sums a per-trial term over 20,680
modelled trials, and no page has been run on any hardware at any clock.

### What changed on review, 2026-08-20

Five findings, all of them about the evidence rather than about the
implementation. The measured net result is unchanged.

| # | finding | resolution |
|---|---|---|
| 1 | the claimed clean two-term split was **contaminated**: the shadow's comparator reschedules `norm_cols` by +2 cycles a call, and the checker compared only the two columns that could not move | split **withdrawn**; checker now reads all four columns and the module wrapper, and holds them against a recorded inventory |
| 2 | the two corpus components in `tme_cycle_model.py` were **wrong** — 3.088545 and 0.409416, each 0.239655641778 too large, the same offset, so the net stayed exact | corrected to 2.848889358222 and 0.169760466889, now **derived and asserted** rather than narrated |
| 3 | `b0b_post_route_wns.txt` said the design **“closes”** while its own verdict line said `CONSTRAINTS VIOLATED` | generator's failed-timing branch written; the retained report carries a correction header naming the two stale passages |
| 4 | B2's manifest line 12 had been silently **re-baselined** onto the living `tme_top.cpp`, which B0b edits | re-pointed at the immutable `b0b_sources/tme_top.b2.cpp` (`bcccd44c…`); `--write` now refuses to move an already-pinned digest without `--rebaseline` |
| 5 | `run_hls_b0b_mutant.tcl` resolved paths against `[pwd]`, and its pinned-snapshot guard **failed open** on an empty glob | every path anchored to the script's own directory; the guard and the vector list fail closed; sweep re-run, 7/7 |

---

## What B0b does

The shipped core recomputes ΣI and ΣI² for **every (output row *v*, template row
*dy*) pair**: `isq_init` scans `tw` pixels, `isq_slide` scans `rw−1` more, and
that runs `rh·th` times. The window over rows `[v, v+th)` and the window over
rows `[v+1, v+1+th)` share `th−1` rows, so nearly all of it is repeated work.

B0b maintains the per-column accumulators **across** output rows:

| | scans |
|---|---|
| `v == 0` | all `th` rows (the first as a SET, so no separate zeroing pass exists or is modelled) |
| `v > 0` | row `v−1` subtracted, row `v+th−1` added |

One scan costs `tw + (rw − 1) = pw` iterations, so the pass is
`S = th + 2·(rh − 1)` scans and `I = S·pw` iterations — exactly the count
`tme_cycle_model.b0b_count_pass_iterations` already derived. That function is
**unchanged**; the iteration count was never the projected part.

### Counts or sums — a deliberate departure from the priority text

The priority list calls this the *foreground count* pass. It would be one if the
patch were guaranteed binary: for pixels in `{0, 255}`, `ΣI = 255·C` and
`ΣI² = 255²·C`, so a single counter would serve both statistics and cost less
per iteration.

**The contract does not guarantee that.** §4.1 bounds the geometry and says
nothing about pixel *values*, and the vector suites deliberately contain
grayscale patches — `stress-max-result` is grayscale *by necessity* (a 4×4 binary
window recurs within a few hundred of its 248,368 positions, so there would be no
unique peak to assert), and it is one of the two cases that reach the 817×304
result map and `T = 52` on silicon. A count-only core would be an ABI narrowing
that invalidated those vectors.

So the pass carries the general ΣI and ΣI². **The iteration count is identical
either way**, so the frozen figures are unaffected by the choice, and the
count-only specialisation stays available as a later, cheaper-per-iteration
variant if it is ever worth an ABI change.

### Why the subtract and the add are separate scans

Fusing them into one loop over `u` would cost `pw·ph` iterations instead of
`pw·(2·ph − th)` — **fewer**. It is not free. A fused body reads four patch
pixels per iteration: the outgoing and incoming column of the outgoing row *and*
of the incoming row. `patch_buf` is `RAM_2P`, cyclic-partitioned by `PAR_COLS` on
the column dimension, so the two reads at column `u−1` land in the same bank and
the two at `u+tw−1` land in another same bank — two reads per bank, exactly the
port budget, **unless `tw` is a multiple of `PAR_COLS`**, where all four collide
on two banks and the loop cannot hold II=1.

The separate scans read two pixels of one row per iteration and are structurally
the loop B2 already ran at II=1. The fused form is a real candidate for a later
variant; switching to it silently would make the model wrong in the flattering
direction.

---

## Three solutions, because B0b is two changes

Adding a pass and deleting the loops it replaces would have been one indivisible
difference against a single control.

| solution | source | what it is |
|---|---|---|
| `b2ctl` | `b0b_sources/tme_top.b2.cpp` | the shipped core, **byte-identical** to the `tme_top.cpp` that `logs/b2_20260819/MANIFEST.sha256` line 12 records as B2's build input (`bcccd44c…`) |
| `shadow` | `b0b_sources/tme_top.shadow.cpp` | `b2ctl` **plus** the hoisted pass, computed alongside and compared at every result position; nothing removed |
| `b0b` | `b0b_sources/tme_top.b0b.cpp` | the hoisted pass **only**; the repeated loops deleted |

    D1 = shadow − b2ctl     the hoisted pass  PLUS the shadow's comparator
    D2 = b0b    − shadow    −(the deleted loops) MINUS the same comparator
    D  = b0b    − b2ctl     what B0b changes about the shipped core (D ≡ D1 + D2)
                            — the only one of the three that is comparator-free

`correlation_core.b2.cpp`, `tme_top.h` and the b1 vector suite are digest-gated
before the first `add_files` in every one of the three runs, so the only thing
that moves between them is the one file under measurement.

### The split is NOT identified, and cannot be by this method

**This section previously claimed the opposite and the claim is withdrawn.**

`shadow` adds a comparison **inside `norm_cols`**, and it *does* reschedule that
loop:

| | `b2ctl` | `shadow` | `b0b` |
|---|---|---|---|
| `norm_cols` loop latency | 97 ~ 3361 | **99 ~ 3363** | 97 ~ 3361 |
| `norm_cols` module latency | 99 ~ 3363 | **101 ~ 3365** | 99 ~ 3363 |
| achieved II | 4 | 4 | 4 |
| iteration latency | 98 | 98 | 98 |

`sw/tme_b0b_synth.py` compared **only the last two rows**, and those cannot move
for this defect — a pipelined loop's own latency is not a function of its II and
iteration latency. So the gate reported "no loop moved" for a difference it was
structurally unable to see. It now reads all four scheduling columns plus the
module wrapper, and `--assert` holds the result against a **recorded inventory**
(`KNOWN_SCHEDULE_MOVES`) rather than against "none": a new move fails, the
recorded one changing size fails, and the recorded one vanishing fails too,
because that would make this section stale.

`norm_cols` runs **once per output row**, so

    D1 = pass + 2·rh          D2 = −(removal) − 2·rh          D unchanged

`b2ctl` and `b0b` have identical `norm_cols` schedules, so **`D` carries none of
it**. The frozen aggregate, the frozen s/page and the Phase-S figures are all
unaffected. Only the split is, and equally in both directions.

**The alternative fits exactly, which is the whole point.** Moving the
comparator across gives

    D1 − 2·rh = S·(pw + 29) + th + 3
    D2 + 2·rh = −[rh·th·(tw + rw + 24) + rh]

and these reproduce all fourteen transactions just as exactly, because they are
the same fourteen numbers rearranged. `tme_b0b_ab.py` check 3b asserts this
(14/14 on both forms, 14/14 still summing to the same `D`). The co-simulation
constrains the **sum**; preferring one pair over the other is choosing a
functional form, not reading a measurement.

**No comparator-free control was built, because there cannot be one.** A shadow
solution exists to hold *both* copies of the statistics live at once. A copy
nothing reads is dead code and Vitis deletes it — at which point the solution is
`b2ctl` or `b0b` and measures nothing. Some consumer must therefore read both
copies, and the two array reads plus the compare already present are the
cheapest such consumer: moving them into a loop of their own trades +2 inside
`norm_cols` for a whole new loop region, and *selecting* between the copies
instead of comparing them keeps both reads and so keeps the cost. `D1` and `D2`
are unidentifiable **by this method, in principle** rather than by oversight.

**What survives, and it is the part that mattered.** The nuisance is
proportional to `rh`. `W`'s regressor is `rh·th` and `th` varies across the
suite, so nothing `rh`-proportional can move it:

| term | status |
|---|---|
| `tw + rw + 24` per (output row, template row) | **identified** — the removal's own cost |
| `3` per output row | the removal's own share **+ 2 of comparator** |
| `S·(pw + 30) + 5` | the pass **+ 2·rh** |

So the pre-registration is **refuted either way**: it predicted `tw + rw + 21`
and nothing per output row, and 24 is what the data give with or without the
comparator. `tme_cycle_model.PER_ROW_TERMS["window_statistics"]` stands;
`PER_OUTPUT_ROW_STATISTICS` and the pass's `(k, m)` do not, and the FROZEN keys
were renamed `d1_*` / `d2_*` to say so.

Only the non-pipelined containers `slide_v` and `accum_rows` move besides, which
they must — their iteration latency is the sum of what is inside them.

---

## Correctness came before the measurement

The shadow build reports a disagreement through the **existing** result
registers, as an unreachable `result_score = −3.0f` at `(0xFFFF, 0xFFFF)`.
`best_score` starts at `−2.0f` and every written score is clamped into `[−1, +1]`,
so no legal run can produce it. No new port, no driver change: the testbench
compares score bits and exact location, RTL co-simulation compares against the
same goldens, and the board runner's tolerance check would reject it.

**Result: 2,911,495 result positions compared over 100 DUT invocations in five
C-simulation suites, zero disagreements.**

| suite | cases | result |
|---|---|---|
| `b1` | 12 + 2 direct | 12/12 |
| `csim` | 23 | 23/23 |
| `hw` | 9 | 9/9 |
| `b0b` | 23 + 8 corners | 23/23, corners 0 failures |
| `prod` | 15 | 15/15 |

Transcript: `csim_shadow_broad.log`.

"No mismatch" is also what a comparison that never ran would report, so the C
simulation prints the number of positions actually compared per invocation and it
**equals `rh·rw` in all 100**. That is the positive half of the claim.

### The other half: the comparison can fail

`sw/tme_b0b_mutants.py` breaks the hoisted pass on purpose and requires the
shadow to notice. Transcript: `b0b_mutants.txt`; the edits are listed in
`b0b_mutants_list.txt`, and every anchor is required to match **exactly once** in
the snapshot (an anchor matching zero or two times would produce a "mutant" that
is not the defect it claims to be, and its verdict would then be reassuringly
meaningless).

| mutant | required | got |
|---|---|---|
| `none` | must PASS | passed |
| `sub_off_by_one` — subtract row `v` instead of `v−1` | must detect | detected |
| `add_off_by_one` — add row `v+th` instead of `v+th−1` | must detect | detected |
| `no_set` — never SET, so invocations accumulate onto each other | must detect | detected |
| `sub_becomes_add` — add the outgoing row instead of subtracting it | must detect | detected |
| `short_slide` — stop one column short, leaving the last output column stale | must detect | detected |
| `swap_sub_add` — add before subtract | **must PASS** | passed |

`swap_sub_add` is the control that matters. Adding before subtracting is a real
reordering and it is **correct**: it only makes the intermediate a `th+1`-row sum
instead of a `th−1`-row one, and both fit (`97·216·255 = 5,342,760 < 2²³` and
`97·216·255² = 1,362,403,800 < 2³¹`). A gate that flagged it would be flagging a
difference that is not a defect. The shipped order is chosen so the correctness
argument rests on a **sign** rather than on a width bound — which is a reason not
to reorder the two calls, not a claim that reordering is wrong.

**Scope, exactly.** The sweep runs `csim_design -argv b1`: twelve
banking-boundary cases plus the two direct tests, spanning `rh ∈ {1, 6, 7, 10,
16, 24}` and `th ∈ {4, 8, 12, 16}`. It establishes that the shadow **mechanism**
detects these five defect classes **on this suite**. It is not a proof that every
possible defect is caught, and the corner suite (`-argv b0b`, three and a half
minutes a run) is too slow to sweep.

### The corners the manifest suites cannot express

`tme_tb.cpp -argv "b0b"` adds eight direct cases. The generator cannot express
them: it refuses a flat template and it needs a unique non-degenerate peak.

| case | geometry | what it corners | expected |
|---|---|---|---|
| `b0b-zero-40x30` | 40×30 / 4×4 | 26 shifts subtracting from an accumulator that is identically zero | `0x00000000` @ (0,0) |
| `b0b-ones-40x30` | 40×30 / 4×4 | the same at 255 — a flat window, `di == 0` | `0x00000000` @ (0,0) |
| `b0b-ones-rh1-216x96` | 216×96 / 216×96 | `rh == 1`: the initialising pass runs and **no shift ever happens**; also the maximum window, `ΣI = 5,287,680`, `ΣI² = 1,348,358,400` | `0x00000000` @ (0,0) |
| `b0b-ones-216x98` | 216×98 / 216×96 | the maximum window with two shifts over it | `0x00000000` @ (0,0) |
| `b0b-step-rh2` | 40×5 / 4×4 | `rh == 2`: exactly one shift, winner on the far side of it | `0x3F800000` @ (0,1) |
| `b0b-step-first-row` | 40×30 / 4×4 | winner at `v = 0` | `0x3F800000` @ (0,0) |
| `b0b-step-mid-row` | 40×30 / 4×4 | winner at `v = 13` of 27 | `0x3F800000` @ (0,13) |
| `b0b-step-last-row` | 40×30 / 4×4 | winner at `v = rh−1 = 26` | `0x3F800000` @ (0,26) |

**Every expected value is derived, not measured.** The flat cases score `+0.0`
because `di == 0` is the contract value (§4.6) and `best_score` starts at
`−2.0f`, so the first window keeps the location. The step cases put an exact copy
of the template in the patch, where `ΣTI = ΣT²`, `ΣI = ΣT` and `ΣI² = ΣT²`, so
`num = di = dt` and the score is `dt_f / sqrtf(dt_f·dt_f) = 1.0f` exactly —
`sqrt(fl(x·x)) == x` under round-to-nearest, and `dt = 4,161,600` is itself exact
in float32. Every column of a step patch is identical, so the winning row is
reached at every `u` and the row-major first occurrence selects `u = 0`.
`b0b_direct_expect.py` recomputes all eight from the DUT's arithmetic.

They are gated to `-argv b0b` on purpose: a direct DUT call is also a
co-simulation transaction, and `tme_b1_ab.py`/`tme_b2_ab.py` map transaction
indices onto (2 direct + 12 manifest) for `-argv b1`. Adding invocations to every
suite would have silently renumbered B1's and B2's evidence.

---

## The measurement

`b0b_ab.txt`. Five checks, all passing; the negative control
(`b0b_ab_negative_control.txt`) confirms the fits and the declared model are
independent.

| # | check | result |
|---|---|---|
| 1 | `b2ctl` reproduces the published B2 term | **14/14 exact** |
| 2 | `D1 = S·(pw + k) + m` for one `(k, m)` | **14/14**, `k = 30`, `m = 5` |
| 3 | `D2 = −(rh·th·(tw + rw + W) + c·rh)` for one `(W, c)` | **14/14**, `W = 24`, `c = 3` |
| 4 | the fitted constants match `FROZEN["b0b"]` | 4/4 |
| 5 | every `b0b` transaction equals `cycles(…, "B0b")`, zero free parameters | **14/14 exact** |

**Counting the constraints honestly.** Under check 2's shape the residual against
`S·pw` depends on `S` alone, so transactions sharing an `S` restate one equation.
The suite spans `S ∈ {4, 16, 18, 30, 42, 62}`: six independent equations against
two free parameters, so **four surplus constraints** — not twelve. The other
eight sit at an already-constrained `S` with a different `pw` and so test that the
residual is a function of `S` alone. Check 3's regressors are `rh·th` and `rh`,
independent because `th` varies. Check 5 has zero free parameters over thirteen
distinct geometries.

### Both projections were wrong, and only one of them was about B0b

**1. The II was right and the endpoints were still wrong.** csynth puts the
hoisted pass's two loops at **II = 1 with iteration latencies 7 and 14 —
identical to the `isq_init` and `isq_slide` they replace**, which is exactly what
the source set out to preserve. But the frozen endpoints modelled the pass as
`N·I` and nothing else, and the measurement is `S·(pw + 30) + 5`. Over the cosim
suite that is **+11,710 cycles, +25.32%** on the pass. The II was never the risky
input; the per-scan constant that was not modelled at all was.

**2. The window-statistics attribution was wrong, and that is the bigger
finding.** `PREDICTION.txt` — committed before the first build — registered
`D2 = −rh·th·(tw + rw + 21)` with zero free parameters, straight from the model's
four-way split of the fitted per-row term. **It is refuted**: the measurement is
`−(rh·th·(tw + rw + 24) + 3·rh)`, short by **5,190 cycles** over the fourteen
transactions.

This is a finding about the *model*, not about B0b. Only the **sum** of that
four-way split ever had evidence; `check()` proved the four expressions add up and
could not prove any one of them was the right share. Consequences, all now
recorded in `tme_cycle_model.PER_ROW_TERMS`:

* the other three terms sum to `2·tw + 2·rw + 9`, not `+ 12`;
* **which** of the three was over-attributed is **not established**. The
  dictionary carries an explicit `unattributed_correction` of `−3` rather than
  silently shaving it off whichever line looked least defended;
* three cycles per **output row** came out of the `5·rw + 99` term, whose internal
  split had no evidence either;
* **"fully attributed" is withdrawn.**

Note `tw + rw = pw + 1`, so the measured statistics cost is
`rh·th·(pw + 25) + 3·rh` — it depends on the **patch width alone**, which is what
the two deleted loops actually scan (`tw` priming iterations plus `rw − 1` sliding
ones). The measured form is the one the source predicts; the attributed one was
not.

**The shapes are measured; the mechanisms are not.** Fourteen transactions pin
`+30` per scan, `+5` per call, `+3` per (output row, template row) over the old
attribution, and `+3` per output row. *Why* each is there is not established —
no experiment here separates pipeline flush from call overhead from loop-region
control, exactly as none separated B1's `T + 1` or B2's `2·(T − 1)`. Do not
quote a cause. In particular the `3·rh` is an accounting fact about where the
cycles went, not a claim that `reset_acc` dropping two array writes is what
produced it.

### The count pass's II and latency, from synthesis

Priority 6 asked for these specifically. `window_row_scan` is compiled `INLINE
off`, so it has its own report and the three call sites share one instance.

| | achieved II | iteration latency | trip count |
|---|---|---|---|
| `scan_init` | **1** | 7 | 4 … 216 |
| `scan_slide` | **1** | 14 | 1 … 816 |
| `isq_init` (the loop it replaces, in `b2ctl`) | 1 | 7 | 4 … 216 |
| `isq_slide` (the loop it replaces, in `b2ctl`) | 1 | 14 | 1 … 808 |

The hoisted loops are scheduled **identically** to the ones they replace, which
is exactly what keeping the loop shape was for. Module latency for one call is
**33 cycles minimum and 1,063 at the compiled maxima** (`tw = 216`, `rw = 817`,
so `pw = 1,032`) — the `max` column is `LOOP_TRIPCOUNT`-driven, but the shape it
implies, ≈ `pw + 31` per call, corroborates the cosim-measured `pw + 30` per
scan independently of the transaction reports.

So the II was never where the risk was. The endpoints modelled the pass as
`N·I` and stopped; the cost is `S·(pw + 30) + 5`.

### The withdrawn endpoints did not bracket the answer

`17.743730541333` (II=1) and `18.035794052889` (II=3) are **withdrawn**, along
with the `17.597699` base under them. The measurement is **17.726036 — below
both**. The two errors point in opposite directions and the removal wins.

A range check would have reported "inside the bracket" for a wrong
implementation; this file already records that failure once, in the 17.652
attribution that survived review by falling inside `[17.514, 17.806]`. Here the
*correct* implementation lands **outside** it. `tme_cycle_model.check()` now
asserts the measurement is below both endpoints, so a future revision cannot
quietly restore the range and call it confirmed.

### A coincidence worth naming

The Priority 6 brief quotes an expected range of **17.514–17.806 s/page**, and
17.726036 falls inside it. That is the **first** pair of endpoints, withdrawn on
2026-08-19 when B2 was measured and its term replaced the projection underneath
them. The endpoints in force when this work started were **17.743731 /
18.035794**, and the measurement is outside those.

So "the projection was right" is not available in either reading: the first
range contained the answer only because it was built on a B2 term that was
itself wrong by +0.23 s/page, and the corrected range did not contain it at all.
Both of the inputs the endpoints rested on — the pass's cost and the removal's
worth — turned out to be wrong. Landing inside a withdrawn interval is a
coincidence of two errors, not a confirmation.

### B0b is not a uniform improvement

At `rh == 1` it **loses, by exactly `5·th + 2` cycles**: a single output row has
nothing to reuse vertically and the hoisted pass still pays its per-scan
overhead. Derived, asserted in `check()` at three geometries, and visible in the
transaction table as `+22` on the 4×4/4×4 direct case (`th = 4`). Anyone quoting
B0b as a uniform improvement is wrong. Over the real workload it wins by
**2.679 s/page**.

---

## Synthesis

`b0b_synth.txt`. Estimates from the scheduler, not measurements — the `latency
max` column is driven by `LOOP_TRIPCOUNT` pragmas wherever a bound is runtime and
is deliberately not reported.

| | b2ctl | shadow | b0b |
|---|---|---|---|
| timing estimate | 6.547 ns | 6.547 ns | 6.547 ns |
| latency min | 517 | 589 | 458 |
| BRAM18K | 224 | 288 | **224** |
| DSP | 33 | 51 | **48** |
| FF | 24,669 | 32,516 | **26,431** |
| LUT | 46,429 | 53,288 | **47,683** |

B0b is **BRAM-neutral** — the hoisting adds no storage, and the shadow's +64 is
its two extra shadow arrays, which do not ship. It costs **+15 DSP and +1,254
LUT** against `b2ctl`: the scan function's two squaring multipliers are not shared
with the deleted `isq_*` ones. HLS's LUT estimate has been ~2.2× pessimistic
before (87% estimated vs 38.9% post-route on B2), so treat these as a direction,
not a capacity forecast.

---

## Routed timing

**Not available when the rest of this document was written.** B2 closed 8.000 ns
with **+0.011710 ns** — 0.15% of the period — and the binding path moved *into*
`correlation_core`. B0b **inherits** that margin: it does not modify the
`seg → DSP` path. What it can do is perturb placement and routing around a path
with 12 ps to give, and it does add DSPs and LUTs, which is why it was routed
early rather than last.

### Result: it does not close

    clock              : clk_fpga_0
    constrained period : 8.000 ns
    post-route WNS     : -0.051470 ns
    post-route TNS     : -0.051470 ns
    post-route WHS     : +0.050437 ns
    post-route THS     :  0.000000 ns
    verdict            : CONSTRAINTS VIOLATED

    binding path:
      from tme_top_0/inst/grp_correlation_core_fu_1826/seg_965_reg_7002_reg[6]/C
      to   .../grp_correlation_core_Pipeline_mac_loop_fu_8736/
             empty_48_fu_1424_reg[23]_psdsp_1/D
    data delay 7.622 ns — logic 1.592 ns (20.9%), routing 6.030 ns (79.1%),
    5 logic levels

**The binding path is the one B0b does not edit.** It is a `seg` register
feeding the MAC's DSP input inside `correlation_core` — structurally the same
path B2 bound on (`seg_960_reg` there, `seg_965_reg` here). B0b changed
`tme_top.cpp` only. So this is the risk the B2 evidence named in advance:
B0b inherits 12 ps of margin on a path it does not touch, and perturbing
placement around such a path is a plausible way to lose it. It lost it.

| | B1 | B2 | B0b |
|---|---|---|---|
| WNS at 8.000 ns | **+0.134571** | **+0.011710** | **−0.051470** |
| % of period | +1.7% | +0.15% | −0.64% |
| Slice LUTs | 14,792 | 20,694 | 21,176 |
| Slice Registers | 18,483 | 24,409 | 24,887 |
| Block RAM Tile | 115 | 115 | **115** |
| DSPs | 34 | 34 | **34** |

The resource cost is small — **+482 LUT, +478 FF, zero BRAM, zero DSP** against
B2. Note that csynth *estimated* +15 DSP; post-route it is **+0**, which is one
more reason not to read a csynth utilisation figure as a capacity forecast.

**What this does and does not establish.** It is **one implementation run at the
Vivado default flow** — the same flow, with no strategy or directive overrides,
that produced B1's +134.6 ps and B2's +11.7 ps. That sameness is what makes the
three comparable, and it is also the limit of the claim: **no directive sweep,
seed sweep or post-route `phys_opt` has been tried.** 51 ps on a design that
closed two revisions ago is small enough that a stronger effort level is a
reasonable thing to expect to recover it. But that experiment has not been run,
so the honest state is:

* B0b's **cycle term** is measured and stands on its own;
* B0b's **s/page at 125 MHz** is a conversion at a clock this core has no
  routed build for;
* **the Priority 2 gate is not met** — "a frequency is supported only after
  routed timing passes and is measured on the board" — so B0b must not be
  quoted at 125 MHz without this caveat attached.

**The image from this run must not be loaded, and is not committed.** Both
files stay at
`C:/Users/lychee/tc25/vivado_project/tme_b0b_125/overlay_output/`:

    tme_standalone.bit   ad55c1ddcd9d993a5f69cdd771def1243727629a146fe13fc9920a73737c359f
    tme_standalone.hwh   79ad3c6d86e915a6a33a495e438b6b39d5d0d2f31b1cf69e0c883a8e4af78304

B1's and B2's images are committed because they closed and then ran. An image
that violates its timing constraint is not something a later session should be
able to reach for by accident, so this one is recorded by digest only. **The
four routed reports are committed** — `b0b_post_route_wns.txt`,
`b0b_post_route_utilization.rpt`, `post_route_timing_summary.rpt`,
`post_route_worst_paths.rpt` — and they are what every number above is read
from. `tme_cycle_model.py --assert` re-reads the first two and fails if the
verdict line ever stops saying `CONSTRAINTS VIOLATED`.

**`b0b_post_route_wns.txt` carries a correction header, and the prose below it
is partly stale.** The report was generated by a branch that had no
failed-timing case, so its body says *"this result says the LOGIC closes at the
probed period"* and *"this build met its constraint with room to spare"* about a
run whose own verdict line is `CONSTRAINTS VIOLATED`. Every **measured** value
in it is correct — the four slack numbers, the verdict, the budget-consumed and
data-delay lines, the binding path. Two prose passages are not, and the header
names both verbatim. The generator was fixed in
`vivado/tme_standalone/build_tme_standalone.tcl`: the verdict now drives a `met`
flag and every outcome sentence branches on it, including the case where a board
measurement is supplied for a build that did not close (a contradiction the old
prose would have printed as a pair of facts). The report is **not**
regenerated — that would need the routed design reopened, and the numbers in it
are the ones that were measured. `tme_cycle_model.py --assert` now also fails if
the correction header is ever stripped.

### The follow-up this creates

1. **Re-implement at a stronger effort level** and read the result. Until then
   "B0b closes 8 ns" is unsupported and "B0b does not close 8 ns" is supported
   only for the default flow.
2. **If it still does not close, the binding path is the target and B0b is not
   the fix for it.** The path is `seg → DSP` inside `correlation_core`; a
   register stage there, or a different `PAR_COLS`, is a separate change with
   its own measurement.
3. **A board session is blocked** until (1) resolves. B1's and B2's sessions
   gated on `FCLK0 == 125.0000 MHz` against a build that met its constraint;
   running this one would be measuring a design known to violate it.

---

## What is NOT established

* **The clock.** A cosim latency is a zero-stall RTL schedule. Nothing here
  licenses a frequency claim for B0b.
* **Silicon.** B0b has not run on a board. B1's and B2's sessions say nothing
  about it.
* **Any page time.** `17.726036 s/page` sums a per-trial term over 20,680
  **modelled** trials. No page has been run end to end on any hardware, for any
  variant, at any clock.
* **Which of the three remaining per-row terms was over-attributed by 3.** The
  measurement constrains their sum and nothing else.
* **That the shadow comparison catches every possible defect.** It catches the
  five swept, on the b1 suite.
* **The shadow build's own latency as a B0b figure.** `shadow` is `b2ctl` plus a
  pass; it is slower than `b2ctl` by construction and is not a variant anyone
  would ship. Only the differences mean anything.

## Reproducing

    cd hls/template_match
    python tme_generate_production.py --suite b1          # if the vectors are absent
    TME_B0B_SOLUTION=b2ctl  vitis-run.bat --mode hls --tcl run_hls_b0b.tcl
    TME_B0B_SOLUTION=shadow vitis-run.bat --mode hls --tcl run_hls_b0b.tcl
    TME_B0B_SOLUTION=b0b    vitis-run.bat --mode hls --tcl run_hls_b0b.tcl

    cd ../../sw
    python tme_b0b_ab.py --assert
    python tme_b0b_ab.py --negative-control
    python tme_b0b_synth.py --assert
    python tme_b0b_mutants.py --assert
    python tme_cycle_model.py --assert
    python tme_b0b_manifest.py --verify

The broad correctness run adds `TME_B0B_CSIM_BROAD=1 TME_B0B_PROD=1` to the
`shadow` build. `TME_B0B_CSIM_ONLY=1` stops before csynth and produces no
evidence.
