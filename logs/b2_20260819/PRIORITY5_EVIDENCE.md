# Priority 5 — B2, horizontal overlap reuse: evidence

Captured 2026-08-19. Every figure here regenerates from the tools
(`tme_b2_ab.py --assert`, `tme_b2_mutants.py --assert`,
`tme_cycle_model.py --assert`, `tme_b2_manifest.py --verify`), and nothing is
transcribed by hand.

    cd hls/template_match
    <venv>/python.exe tme_generate_production.py --suite b1     # if absent
    TME_SOLUTION=b1 vitis-run.bat --mode hls --tcl run_hls_b1.tcl   # the control
    TME_SOLUTION=b2 vitis-run.bat --mode hls --tcl run_hls_b1.tcl
    vitis-run.bat --mode hls --tcl package_b2.tcl
    TME_IP_REPO=.../template_match_b1_b2/b2/impl/ip \
    TME_HLS_VLNV=TermCountB2:hls:tme_top:0.2 TME_FCLK_MHZ=125 \
    TME_BUILD_ROOT=C:/Users/lychee/tc25/vivado_project/tme_b2_125 \
      vivado -mode batch -source vivado/tme_standalone/build_tme_standalone.tcl
    cd ../../.github-upload/sw
    <venv>/python.exe tme_b2_mutants.py --assert
    <venv>/python.exe tme_b2_ab.py --assert
    <venv>/python.exe tme_cycle_model.py --assert
    <venv>/python.exe tme_b2_manifest.py --verify

---

## What changed

`correlation_core.cpp`, phase 1 of `tile_loop`. B1 loaded `seg_len = tw + 15`
pixels for **every** tile. Consecutive tiles start `PAR_COLS = 16` apart, so
their segments overlap in `seg_len - 16 = tw - 1` pixels; B2 re-reads only the
16 that are new.

```c
    int overlap = seg_len - PAR_COLS;      // = tw - 1, the reused pixels
    int refill0 = overlap;

    if (t == 0) {
        load_seg: for (int i = 0; i < SEG_W; i++) {   // unchanged from B1
            if (i >= seg_len) break;
            seg[i] = (u0 + i < pw) ? patch_line[u0 + i] : 0;
        }
    } else {
        shift_seg: for (int i = 0; i < SEG_W - PAR_COLS; i++) {
#pragma HLS UNROLL
            if (i < overlap) seg[i] = seg[i + PAR_COLS];
        }
        refill_seg: for (int k = 0; k < PAR_COLS; k++) {   // COMPILE-TIME bound
#pragma HLS PIPELINE II=1
            int j = refill0 + k;
            seg[j] = (u0 + j < pw) ? patch_line[u0 + j] : 0;
        }
    }
```

Nothing else moved — not the tile count, the lane masking, the MAC schedule or
the writeback. That is what makes the paired co-simulation below attributable.

Vitis unrolled `shift_seg` completely (factor 215) and pipelined `refill_seg`
at II = 1, depth 5.

**Why a shift and not a rotating read index.** Leaving the pixels where they
are and rotating the read would turn each of the 16 lanes' `seg[p + x]` into a
runtime rotation over 231 registers. The shift puts the variable part on the
*write* side, where `seg` already has a decoder because `load_seg` indexes it by
a loop counter, and leaves the MAC's read addressing bit-for-bit what B1 had.

**Zero-padding survives the shift.** Tile `t-1` wrote
`seg[j] = (u0 - 16 + j < pw) ? patch_line[…] : 0`; read at `j = i + 16` that is
exactly `(u0 + i < pw) ? patch_line[u0 + i] : 0`, the value B1's load would have
produced at `seg[i]`. The out-of-patch zeros propagate into the final,
partially-masked tile instead of being recomputed there.

---

## The headline

**Read the units before the numbers.** What is measured is a CYCLE TERM. The
s/page figures are **workload projections** built from that term over 20,680
modelled trials — no page has been run on any hardware at any clock.

| | s/page @ 125 MHz | basis |
|---|---|---|
| Phase S, per-trial ROI | 36.476 | projection, projected term |
| + B1, measured | 26.334292108222 | projection, measured term, board-corroborated |
| + B2, as **projected** before any RTL existed | 20.175432 | projection, **withdrawn** term |
| + B2, as **measured** by RTL co-simulation | **20.405164783778** | projection, **measured** term |

The projected tile term was `T*(tw + 41) + (tw - 1)`. The RTL is

    tile = T * (tw + 44) + tw - 2

exact on 14/14 transactions. The projection was optimistic by **0.229733
s/page** — more than twice B1's miss of 0.094596.

### The prediction was registered before the build, and it was wrong in a new way

`tme_b2_ab.py --predict` computes two candidates from the **retained
pre-measurement copy of the model** and the **`b1` transaction report** — it
reads no `b2` report at all, so it cannot see the answer, and its numbers are
recomputable at any time rather than resting on a timestamp:

| candidate | tile term | what it would have meant |
|---|---|---|
| pre-RTL projection | `T*(tw+41) + (tw-1)` | B1's overhead did not recur |
| control-naive | `T*(tw+42) + tw` | B1's `T+1` recurred exactly once |
| **measured** | **`T*(tw+44) + tw-2`** | **neither** |

The shortfall against the naive reuse arithmetic
`rh*th*(T-1)*(tw-1)` is

    2 * (T - 1)     cycles per (output row, template row)

against B1's `T + 1`. **B1's overhead did not recur, in size or in shape.**
That outcome is only worth stating because both alternatives were written down
first.

> A safeguard was added after a near miss. The first version of `--predict`
> read the **live** model, which was true right up until the model was corrected
> with the measured term — at which moment the "prediction" silently became the
> answer and every printed difference collapsed to zero. It now reads
> `logs/b2_20260819/tme_cycle_model.py.pre_b2`, and `--assert` fails if that
> snapshot ever starts carrying the measured term.

### The shape is measured; the mechanism is not

Two cycles per tile beyond the pixels, minus two per call, is what fourteen
transactions pin. Whether that is the shift's own state, the `t == 0` branch or
the extra loop region is **not** established — no experiment here separates
them, exactly as with B1's `T + 1`. Do not quote a cause, and do not carry a
correction forward to B0b.

### B2 is never a regression, and B1 was

The saving over B1 is

    (T - 1) * (tw - 3)     per (output row, template row)

zero at `T = 1` (a single-tile invocation has nothing to reuse — confirmed as an
exact tie on all five such transactions) and positive for every legal geometry
with more than one tile, since contract §4.1 puts `tw >= 4`. At the compiled
maximum template width, where B1 **lost**:

| | `phase-s-max` (311×159, 216×96) |
|---|---|
| `cur` | 23,476,737 |
| B1 | 23,482,881 (+6,144 — a loss) |
| **B2** | **16,939,521 (−6,543,360 vs B1)** |

---

## The paired co-simulation

`sw/tme_b2_ab.py`, control `b1`, 14 transactions each. **The control is `b1`,
not `cur`**: B2 is B1 plus the reuse, so pairing against the unmodified core
would fold two changes into one difference and leave neither attributable.

| # | case | patch | templ | rw | T | measured b1 | measured b2 | ×
|---|---|---|---|---|---|---|---|---|
| 0 | direct flat-templ-4x4 | 12×10 | 4×4 | 9 | 1 | 4,568 | 4,568 | 1.000 |
| 1 | direct min-nonflat | 4×4 | 4×4 | 1 | 1 | 536 | 536 | 1.000 |
| 2 | b1-w001-tw216 | 216×13 | 216×8 | 1 | 1 | 59,040 | 59,040 | 1.000 |
| 3 | b1-w015-tw216 | 230×13 | 216×8 | 15 | 1 | 61,658 | 61,658 | 1.000 |
| 4 | b1-w016-tw216 | 231×13 | 216×8 | 16 | 1 | 61,845 | 61,845 | 1.000 |
| 5 | b1-w017-tw216 | 232×13 | 216×8 | 17 | 2 | 84,736 | 74,512 | 1.137 |
| 6 | b1-w031-tw100 | 130×21 | 100×12 | 31 | 2 | 114,374 | 102,734 | 1.113 |
| 7 | b1-w032-tw100 | 131×21 | 100×12 | 32 | 2 | 114,805 | 103,165 | 1.113 |
| 8 | b1-w033-tw100 | 132×21 | 100×12 | 33 | 3 | 144,156 | 120,876 | 1.193 |
| 9 | b1-w095-tw020 | 114×27 | 20×12 | 95 | 6 | 178,366 | 162,046 | 1.101 |
| 10 | b1-w096-tw020 | 115×27 | 20×12 | 96 | 6 | 179,049 | 162,729 | 1.100 |
| 11 | b1-tie-samerow | 47×21 | 16×12 | 32 | 2 | 42,481 | 40,921 | 1.038 |
| 12 | b1-tie-rowmajor | 47×21 | 16×12 | 32 | 2 | 42,481 | 40,921 | 1.038 |
| 13 | b1-lane15 | 88×39 | 24×16 | 65 | 5 | 300,096 | 267,840 | 1.120 |

Suite total 1,388,191 → 1,263,391 cycles (1.099× **on this suite**). The suite
is not the workload; the page figure comes from `tme_cycle_model` over the
20,680-trial trace.

**The control reproduced its own published B1 term on 14/14** in the same
comparison — that is what makes a `b2` residual attributable to the change and
not to the harness.

### Three checks, failing for different reasons

1. **The control is intact** — every `b1` transaction still equals
   `T*(2*tw+41)+1`. 14/14.
2. **The declared model** — every measured `b2` latency equals
   `tme_cycle_model.cycles(..., "B2")`, zero free parameters. 14/14.
3. **The fitted shape** — the shortfall fits one `rh*th*(a*T + b)` with
   `(a, b) = (+2, −2)`, fitted from the B2 data and not read from the model.
   14/14.

`--negative-control` perturbs the declared model by +7 and confirms check 2
fails on 0/14 while check 3 still passes 14/14: **the two are independent**.
This is the mistake `tme_b1_ab.py` records having made once, where the declared
model was overwritten from the fit and the residual became zero by construction.

**Counting the constraints honestly.** Under a fit whose residual depends only
on `T`, transactions sharing a `T` restate one equation. `T` spans
{1, 2, 3, 5, 6}: five independent equations, two free parameters, **three**
surplus constraints — not thirteen. Eight of the remaining observations sit at
an already-constrained `T` but a different geometry, so they test *geometry
invariance*; exactly one is a true repeat (12 and 13 share 47×21 / 16×12).
Check 2 does not share this weakness: zero free parameters over thirteen
distinct geometries.

---

## Functional verification

**C simulation: 12/12 vectors plus 9 direct and bound tests — `TESTBENCH
PASSED`.**
**C/RTL co-simulation: `*** C/RTL co-simulation finished: PASS ***`.**

Both against the **pinned** b1 suite, whose three files were SHA-256 verified by
`run_hls_b1.tcl` before the build, and against the b2 snapshot, verified the
same way (`c8c7b088…caec5d8ce`).

### The suite was proved adequate BEFORE the build, not assumed

Overlap reuse replaces a re-read with **carried state**, which is the thing a
vector suite is least likely to probe by accident. Priority 5's brief names the
transitions to test — first/last tile, partial tile, new row, new invocation,
stale register data — and the honest question is whether the *existing* suite
can fail on them. `sw/tme_b2_mutants.py` answers it by transcribing
`correlation_core` into Python with defect knobs and running each mutant through
the DUT's float32 reduction against every case's golden:

| defect | what it breaks | detected |
|---|---|---|
| `shift_by=15` / `shift_by=17` | off-by-one slide | **all 256** stale fills, 7 cases |
| `refill_n=15` | drops `seg[seg_len-1]` — the lane-15 hazard | **all 256**, 6 cases |
| `refill_off=+1` | leaves `seg[tw-1]` holding the old tile | **all 256**, 8 cases |
| `refill_u0=-16` | refills from the previous tile's column base | **all 256**, 8 cases |
| `no-full-tile0` | reuse carried across rows **and invocations** | **all 256**, 8 cases |

A cell counts the stale register fills for which a case *fails* the testbench
check. **`all 256` is an unconditional detection** — no value the register file
could be holding lets that defect through, the same standard `build_lane15` is
held to.

And the honest implementation reproduces the direct cross-correlation, the
golden `(x, y)` and score, and is independent of the stale fill, on 12/12 cases.

#### Why `no-full-tile0` is caught by 8 of 12 and not by all 12

This is geometry, not luck, and `--selftest` derives it rather than reporting a
count. Under the mutant, tile 0 shifts instead of loading, so `seg[i]` holds
inherited data for `i < overlap = tw - 1`; the refill always writes
`seg[tw-1 .. tw+14]` from the right columns. Each subsequent tile shifts again,
so the damaged prefix shrinks by `PAR_COLS` per tile, and at tile `t` it is
`i < tw - 1 - 16t`. Lane `p` of tile `t` writes output column `u = 16t + p` and
is damaged only when `p < tw - 1 - 16t`, i.e. when

    u = 16t + p  <  tw - 1

**independently of `t`.** The damaged set is exactly the output columns
`u < tw - 1`, however many tiles the map has — verified on all 12 cases, where
the largest differing column is always `< tw - 1`.

So a case detects a cross-invocation defect **only if its argmax lies in the
first `tw - 1` columns**, and that prediction matches the observed detection on
12/12:

| case | tw−1 | max damaged u | argmax u | predicted | observed |
|---|---|---|---|---|---|
| b1-w001-tw216 | 215 | 0 | 0 | detects | detects |
| b1-w015-tw216 | 215 | 14 | 14 | detects | detects |
| b1-w016-tw216 | 215 | 15 | 15 | detects | detects |
| b1-w017-tw216 | 215 | 16 | 16 | detects | detects |
| b1-w031-tw100 | 99 | 30 | 30 | detects | detects |
| b1-w032-tw100 | 99 | 31 | 31 | detects | detects |
| b1-w033-tw100 | 99 | 32 | 32 | detects | detects |
| b1-w095-tw020 | 19 | 18 | 94 | blind | blind |
| b1-w096-tw020 | 19 | 18 | 95 | blind | blind |
| b1-tie-samerow | 15 | 14 | 5 | detects | detects |
| b1-tie-rowmajor | 15 | 14 | 31 | blind | blind |
| b1-lane15 | 23 | 20 | 31 | blind | blind |

The four blind cases are blind **by construction**: the width sweep
deliberately plants its peak at the *last* valid column, `u = rw - 1`, because
that is where a tile-count or lane-mask error shows up. No amount of extra
width cases would help. This matters beyond B2 — the same geometry will govern
any later variant that carries state between calls, **B0b included**.

**Two variations turned out to be INERT, and saying so is part of the result:**

* **`unguarded-shift`** — copying the full `SEG_W - PAR_COLS` span rather than
  stopping at the overlap changes no result anywhere. Everything it writes above
  the overlap is either refilled immediately or sits at an index `>= seg_len`
  that no lane reads. The guard in the shipped source is there so the C++ does
  not *read* uninitialised storage, **not** because the values matter.
* **`pad=clamp`** — substituting the last real pixel for the out-of-patch zeros
  changes no result either, and for a sharper reason, re-proved by
  `--selftest`: `seg[i]` holds patch column `u0 + i`, and lane `p` contributes
  to output column `u0 + p`, written back only when `u0 + p < rw`. For any
  **unmasked** lane, `u0 + p + x <= (rw-1) + (tw-1) = pw - 1`, so every index it
  reads is inside the patch row. **The pad VALUE is unreachable.**

  **The guard itself is not optional.** The largest index it has to stop is
  `pw + 14`, which at `pw = 820` is **834**, past the end of
  `patch_line[MAX_PATCH_W = 820]`. Removing the test is an out-of-bounds read
  even though the value it produces could not matter. Value: inert. Test:
  required.

### What this does not establish

`tme_b2_mutants.py` is a Python transcription of the C++, not the C++ and
certainly not the RTL. It shows that the **suite** discriminates the behaviours
it models; it cannot show that Vitis compiled the source into one of them. That
is what csim and cosim are for, and the mutant gate is a precondition for
believing them, not a substitute. A mutant it cannot express is a mutant it says
nothing about.

---

## Routed timing — it closes, and it almost does not

`TermCountB2:hls:tme_top:0.2` implemented into the standalone image at
**8.000 ns**, the same constraint B1 was probed at, same part, same block
design, same script. `b2_post_route_wns.txt` is the authority; this is what it
says:

| | B1 (Priority 4) | **B2 (Priority 5)** |
|---|---|---|
| constrained period | 8.000 ns | 8.000 ns |
| post-route WNS | +0.134571 ns | **+0.011710 ns** |
| post-route TNS | 0.000000 | 0.000000 |
| post-route WHS | +0.010239 ns | +0.012492 ns |
| verdict | all constraints met | all constraints met |
| worst-path data delay | 7.589 ns | 7.435 ns |
| logic levels | 0 | 5 |
| binding path | `templ_buf → t_row` | **inside `correlation_core`** |

**0.011710 ns is 0.15% of the period**, against B1's 1.7%. Quote this as *"this
run closed 8.000 ns"*, not as *"B2 closes 8.000 ns"* — a re-implementation with
a different seed is under no obligation to reproduce 12 ps of slack.

**The binding path moved into `correlation_core`, and that is the part with
consequences.** B1 bound on `templ_buf_U/ram_reg → t_row_reg`, outside the
correlation core entirely. B2 binds on

    from: …/grp_correlation_core_fu_1779/seg_960_reg_7052_reg[1]/C
    to:   …/grp_correlation_core_Pipeline_mac_loop_fu_8736/…_psdsp_6/D

— its own segment shift register feeding the MAC's DSP input, i.e. exactly the
structure B2 added. So **B0b starts from 0.012 ns of slack on a path it will
itself be touching**, not from B1's 0.135 ns on a path somewhere else. That is
a materially worse starting position than the B1 → B2 step had, and it is a
reason to route B0b early rather than at the end.

`tme_cycle_model.py --assert` re-reads all of this out of the report rather than
trusting the literals: period, WNS, the "all constraints met" verdict, whether
the binding path is inside `correlation_core`, and both utilisation rows — six
values, and each one fails independently if the freeze drifts.

### Resource cost

| | B1 | **B2** | Δ | of device |
|---|---|---|---|---|
| Slice LUTs | 14,792 | **20,694** | +5,902 (+39.9%) | 38.90% |
| Slice Registers | 18,483 | **24,409** | +5,926 (+32.1%) | 22.94% |
| Block RAM Tile | 115 | **115** | 0 | 82.14% |
| DSPs | 34 | **34** | 0 | 15.45% |

The shift register is pure fabric: **BRAM and DSP counts are unchanged.** The
LUT/FF cost is the 231-element register file gaining a shift input and a
per-register enable. The device still has room — the binding constraint is
timing, not area, and BRAM at 82% remains the tightest resource, exactly as
before.

> The HLS *estimate* had said 46,429 LUTs (87%), against 34,635 (65%) for B1.
> Post-route both are ~2.2× lower. Vitis's utilisation estimate is not a
> capacity forecast at this design's size, and was not treated as one here.

---

## What Priority 5 did NOT do

* **No board session.** B1 has one; B2 has a routed result and nothing more.
  Nothing has run this core on silicon. Do not quote B2's s/page at 125 MHz as
  though the clock were established for **this** core — the conversion rate is
  borrowed from B1's board-observed 125.0000 MHz, which was measured on
  different RTL. With 12 ps of routed margin, that borrowing is a weaker
  assumption for B2 than it was for B1, not a formality.
* **No page has been run**, for any variant, at any clock. 20.405 s/page is a
  projection summing a measured term over 20,680 modelled trials.
* **No new vector suite.** Deliberate: the pinned b1 suite is what makes the
  measurement a *pair*, and its adequacy for the new defect classes was
  established rather than assumed (above). Adding cases would have broken the
  pairing.
* **The mechanism behind the `2*(T-1)` is not localized.** Only its shape is.

## Knock-on: B0b's endpoints moved

B0b is *defined* as B2 minus the window statistics plus the hoisted count pass,
so it inherits B2's tile term whole. Correcting B2 moved both endpoints:

| | withdrawn | now |
|---|---|---|
| B0b base (deletion only) | 17.367966 | 17.597699 |
| B0b @ II = 1 | 17.513998132444 | 17.743730541333 |
| B0b @ II = 3 | 17.806061644000 | 18.035794052889 |

**Nothing about the count pass changed** — `FROZEN["b0b_count_pass"]` is
untouched and its two terms (0.146031755778 and 0.438095267333 s/page) are the
same numbers they always were. What changed is the base underneath them.

There are now **two** warnings attached to that tier rather than one: B1's
projected term was optimistic by `T+1` per (output row, template row) and B2's
by `2*(T-1)`, which is a *different shape*. So "the projection is optimistic by
about B1's amount" is not a correction anyone may apply to B0b in advance.
