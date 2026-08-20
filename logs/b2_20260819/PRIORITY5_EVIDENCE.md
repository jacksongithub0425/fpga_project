# Priority 5 — B2, horizontal overlap reuse: evidence

Captured 2026-08-19; corrected the same day (six claims — see the change note
at the end). Every figure here regenerates from the tools (`tme_b2_ab.py
--assert`, `tme_b2_mutants.py --assert`, `tme_cycle_model.py --assert`,
`tme_b2_manifest.py --verify`), and nothing is transcribed by hand.

    cd hls/template_match
    <venv>/python.exe tme_generate_production.py --suite b1     # if absent
    TME_SOLUTION=b1 vitis-run.bat --mode hls --tcl run_hls_b1.tcl   # the control
    TME_SOLUTION=b2 vitis-run.bat --mode hls --tcl run_hls_b1.tcl
    vitis-run.bat --mode hls --tcl csim_prod_b2.tcl    # broad geometry, T <= 52
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

| | aggregate cycles | s/page @ 125 MHz | basis |
|---|---|---|---|
| Phase S, per-trial ROI | — | 36.476 | projection, projected term |
| + B1, measured | 118,504,314,487 | 26.334292108222 | projection, measured term, board-corroborated |
| + B2, as **projected** before any RTL existed | 90,789,445,687 | 20.175432374889 | projection, **withdrawn** term |
| + B2, as **measured** by RTL co-simulation | **91,823,241,527** | **20.405164783778** | projection, **measured** term |

The projected tile term was `T*(tw + 41) + (tw - 1)`. The RTL is

    tile = T * (tw + 44) + tw - 2

exact on 14/14 transactions. The projection was optimistic by exactly

    1,033,795,840 cycles  =  0.229732408889 s/page

against B1's miss of 425,680,640 cycles = 0.094595697778.

> **The withdrawn figure is frozen as an integer, and it was wrong until now.**
> It used to sit in `FROZEN["b2"]` as the rounded `20.175432`, with the miss
> computed as *measured minus that* — `0.22973278377777717`, which is
> **1,687 cycles** away from the truth. Six decimal places on a page average is
> a 2.25-million-cycle tolerance, so a rounded projection cannot pin its own
> miss. Both endpoints and the difference are now cycle counts, the s/page
> figures are derived from them, and `tme_cycle_model.py --assert` executes the
> pinned `tme_cycle_model.py.pre_b2` implementation to recompute
> **90,789,445,687** before checking the integer identity
> `91,823,241,527 − 90,789,445,687 = 1,033,795,840` and then the floats. B1's
> block got the same integer treatment; its withdrawn projection was already
> cycle-exact (118,078,633,847), so only the last digit of the float moved.
>
> The withdrawn aggregate is **recomputable, not transcribed**: load
> `logs/b2_20260819/tme_cycle_model.py.pre_b2` and call `page_cycles` on the
> workload this repo discovers. That snapshot reproduces the live model's `cur`
> (164,143,337,975) and `B1` (118,504,314,487) aggregates **exactly**, which is
> what says the two differ in the B2 term and in nothing else.

### What predates the RTL in repository ancestry, and what only looks prior

This section previously claimed the prediction was "registered before the
build". **The retained timestamps contradict that**, so here is the actual
chronology, from the file mtimes in this directory:

| artifact | written | relative to the answer |
|---|---|---|
| `b1_sources/correlation_core.b2.cpp` | 19:04:37 | the RTL source |
| `template_match_b1_b2/.../result.transaction.rpt` | **19:10:13** | **the answer exists** |
| `run_b2.log` (cosim finished) | 19:10:16 | — |
| `.../impl/ip/` (packaged) | 19:16:08 | +6 min |
| `tme_cycle_model.py.pre_b2` (snapshot) | 19:19:15 | **+9 min** |
| `PREDICTION.txt` | 19:19:23 | **+9 min** |
| `b2_mutants.txt` (adequacy gate) | 19:27:40 | **+17 min** |

So:

* **The pre-RTL projection appears in retained commit ancestry before the B2
  source/build commit.** `T*(tw+41) + (tw-1)` and the `20.175432` page figure
  are in `e762cbf`; the B2 source appears later in that history. An unsigned,
  unpushed commit is not an external timestamp, so the commit date is not proof
  of when a third party could first observe either state. The defensible claim
  is repository ordering, and that is what the projection comparison rests on.
* **`--predict` and its snapshot are a RECONSTRUCTION.** Both postdate the
  measurement. What makes the snapshot usable is not its mtime but that its
  content is checkable against `e762cbf` and that `--assert` refuses to run if
  it ever starts carrying the measured term.
* **`control-naive` was never predicted by anyone.** It is computed by
  `tme_b2_ab.py` after the fact. It is a useful *baseline* — "B1's measured
  term plus perfect reuse" — and calling it a candidate prediction was wrong.
* **The mutant adequacy gate is a POST-BUILD test**, not a precondition the
  build had to pass. It is weaker in exactly one way — the suite could not have
  been changed in response to what it found, because the measurement already
  existed — and in no other.

### The miss, decomposed — and B1's overhead DID recur

| candidate | tile term | provenance |
|---|---|---|
| pre-RTL projection | `T*(tw+41) + (tw-1)` | retained in ancestor commit `e762cbf`; no external timestamp claim |
| control-naive | `T*(tw+42) + tw` | computed after the fact, a baseline |
| **measured** | **`T*(tw+44) + tw-2`** | **matches neither** |

Against the **pre-RTL projection** — the like-for-like comparison, since that
is the one B1 also made — the miss is, exactly on 14/14 transactions:

    3T - 1  =  (T + 1)  +  2 * (T - 1)     per (output row, template row)

**Read both terms.**

* The `(T + 1)` is **B1's own correction, and it recurs.** At `T = 1` the
  second term is zero and the *entire* miss is B1's `T + 1` — confirmed on all
  five single-tile transactions (`flat-templ-4x4`, `min-nonflat`,
  `b1-w001/w015/w016-tw216`), where the miss is exactly `rh*th*2`.
* The `2 * (T - 1)` is the **additional** miss, and it is additional *only*
  against `control-naive`, a baseline that already contains B1's term.

> **The earlier wording — "B1's overhead did not recur, in size or in shape" —
> was false**, and it was false in the direction that made B2 look like an
> independent surprise rather than a compounding one. The correct statement is
> that B1's `T+1` recurred *and* a further `2*(T-1)` was added on top. Quoting
> `2*(T-1)` without naming its baseline inverts the conclusion.
>
> This is now machine-checked. `tme_b2_ab.py --assert` gained **check 4**: the
> miss against the pre-RTL projection must equal `rh*th*(3T-1)`, that must
> equal `rh*th*((T+1) + 2*(T-1))`, and at `T = 1` the whole miss must be
> `rh*th*(T+1)`. It reports **14/14 and 5/5**. A revision that quietly restates
> the old claim has to make this fail first.

**What this does and does not license for B0b.** A naive projection has now
been optimistic twice, in two different shapes (`T+1`, then `3T-1`). That is a
reason to measure B0b's term rather than project it. It is **not** a licence to
apply either correction to B0b in advance.

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

### Four checks, failing for different reasons

1. **The control is intact** — every `b1` transaction still equals
   `T*(2*tw+41)+1`. 14/14.
2. **The declared model** — every measured `b2` latency equals
   `tme_cycle_model.cycles(..., "B2")`, zero free parameters. 14/14.
3. **The fitted shape** — the shortfall fits one `rh*th*(a*T + b)` with
   `(a, b) = (+2, −2)`, fitted from the B2 data and not read from the model.
   14/14.
4. **The decomposition** — the miss against the pre-RTL projection is
   `rh*th*(3T-1) = rh*th*((T+1) + 2*(T-1))`, and at `T = 1` the whole miss is
   `rh*th*(T+1)`. **14/14 and 5/5.** This is the check that keeps "B1's
   overhead recurred and was compounded" from decaying back into "B1's overhead
   did not recur".

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

### Broad geometry: the tile-count range the b1 suite does not reach

**The b1 co-simulation suite tops out at `T = 6` tiles.** B2 is an *indexing*
change whose entire behaviour is "what tile `t` inherits from tile `t-1`", so
tile count is the axis along which it is most likely to be wrong — and six
tiles is not a sample of an axis the RTL is compiled to run to **52**. Until
this ran, "B2 is functionally correct" was a claim about roughly one ninth of
the tile-count range.

`hls/template_match/csim_prod_b2.tcl` — a new isolated reset project
(`template_match_b2_prod`), the same b2 snapshot verified by digest, the same
four production vectors `csim_prod_b1.tcl` checks:

    15/15 cases passed  +  9 direct and bound tests
    TESTBENCH PASSED
    CSim done with 0 errors.

| | b1 cosim suite | **production suite** |
|---|---|---|
| tile counts `T` | 1, 2, 3, 5, 6 | 1, 12, 19, 20, 22, **29, 52** |
| widest result map `rw` | 96 | **817** (`prod-max-result`, 820×307 / 4×16) |
| largest template | 216×96 | 216×96, and 164×94 at `T = 29` |

`T = 52` is `ceil(MAX_RESULT_W / PAR_COLS)` — the compiled maximum, and the
`LOOP_TRIPCOUNT max` the tile loop is annotated with. `prod-lane15-full`
exercises the lane-15 pair at `T = 29`; `prod-max-result` puts its peak at
`(816, 291)`, the last valid column of the widest legal map, which is precisely
where a tile-break or lane-mask error surfaces.

**What this is and is not.** C simulation exercises the **source**, not the
RTL. That is the right trade here for the same reason `csim_prod_b1.tcl` gives:
these maps do not fit in xsim, and a wrong overlap, a wrong refill base or a
dropped `seg[seg_len-1]` is an indexing defect that shows up identically in C.
A *timing*-dependent defect would not, and this suite says nothing about one.
The co-simulation above remains the only RTL-level functional evidence, and it
remains capped at `T = 6`.

`template_match_b2_prod` is its own reset project, and the measured `b2`
project's transaction report and `csynth.rpt` are byte-unchanged across it (the
manifest re-verifies both). It is not hermetic: `tme_top.cpp/.h` and `tme_tb.cpp`
were compiled live without pre-build digest gates and are manifest-bound only
after the run.

### The suite was proved adequate — after the build, not before it

Overlap reuse replaces a re-read with **carried state**, which is the thing a
vector suite is least likely to probe by accident. Priority 5's brief names the
transitions to test — first/last tile, partial tile, new row, new invocation,
stale register data — and the honest question is whether the *existing* suite
can fail on them. `sw/tme_b2_mutants.py` answers it by transcribing
`correlation_core` into Python with defect knobs and running each mutant through
the DUT's float32 reduction against every case's golden:

| defect | what it breaks | detected |
|---|---|---|
| `shift_by=15` / `shift_by=17` | off-by-one slide | **all 256** uniform fills, 7 cases |
| `refill_n=15` | drops `seg[seg_len-1]` — the lane-15 hazard | **all 256**, 6 cases |
| `refill_off=+1` | leaves `seg[tw-1]` holding the old tile | **all 256**, 8 cases |
| `refill_u0=-16` | refills from the previous tile's column base | **all 256**, 8 cases |
| `no-full-tile0` | reuse carried across rows **and invocations** | **all 256**, 8 cases |

**Read "all 256" exactly, because the earlier wording overstated it.** The
sweep sets *every* element of the 231-register file to the **same byte** and
walks that byte over `0..255`. It is a sweep over **256 uniform fills**, not
over the `256**231` arbitrary register states, and it never claimed to be one.
What makes the uniform sweep worth running is narrow and stated: the only
mutants whose result can depend on the fill at all are those that read above
`seg_len-1`, where nothing has been written; for every other mutant the two
endpoint evaluations agree and the fill is provably irrelevant. **A defect
sensitive to some non-uniform pattern that no uniform fill reproduces is
outside what this file tests.**

When this ran: `b2_mutants.txt` is stamped 19:27:40, seventeen minutes after
the co-simulation report it is supposed to have gated. It is a **post-build
adequacy test of the pinned suite**. That weakens it in exactly one way — the
suite could not have been changed in response to what it found — and in no
other: the mutants, the goldens and the counts are all recomputable, and none
of them reads a `b2` report.

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

**independently of `t`.** The damage is therefore **confined to** the output
columns `u < tw - 1`, however many tiles the map has — verified on all 12
cases, where the largest differing column is always `< tw - 1`.

**Note the direction of that result.** It is a *containment* bound: no column
at or above `tw - 1` can differ. It does **not** say every column below
`tw - 1` does differ — a lane can inherit a stale value that happens to leave
the accumulated `sti` unchanged, and nothing here rules that out.

So exactly one implication is derived, and it is the one that matters for test
design: a case detects a cross-invocation defect **only if its argmax lies in
the first `tw - 1` columns**. That is what proves the four blind cases *must*
be blind. The converse — argmax below the bound ⇒ detects — is **not** derived;
it is reported below because it matches the observation on 12/12, not because
the geometry requires it, and `--selftest` labels the column `conjecture` for
that reason:

| case | tw−1 | max damaged u | argmax u | conjecture | observed |
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

The four blind cases are blind **by construction, and that half IS derived**:
the width sweep deliberately plants its peak at the *last* valid column,
`u = rw - 1` — where a tile-count or lane-mask error shows up — and the
containment bound then guarantees the mutant cannot reach it. No amount of
extra width cases would help. This matters beyond B2 — the same geometry will govern
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
structure B2 added. So **B0b inherits 0.012 ns of margin on a path inside the
block it edits**, not B1's 0.135 ns on a path somewhere else.

> **Say that precisely — the earlier wording overclaimed.** It read "a path it
> will itself be touching", which asserts more than is known. B0b deletes the
> window statistics and hoists a count pass; **it does not modify the
> `seg` → DSP path**, and nothing here predicts that it will. What it can do is
> perturb placement and routing around a path with 12 ps to give. That is a
> real risk and a good reason to route B0b **early** rather than at the end —
> but it is not the claim that B0b attacks the critical path.

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

## The packaged IP is preserved, and it is the image's actual input

B1 learned this one the expensive way: `package_b1.tcl` re-ran *after* the
Vivado build and overwrote the `impl/ip` directory the bitstream had been built
from, so B1's manifest can only pin a **re-export** whose bytes post-date its
own image.

**B2's do not.** The chronology in `vivado_b2_125.log` and the file mtimes:

| | time | |
|---|---|---|
| `package_b2.tcl` finished | 19:16:09 | IP exported |
| Vivado loaded the IP repo | — | `Loaded user IP repository '…/template_match_b1_b2/b2/impl/ip'` |
| first synthesis run launched | 19:17:10 | |
| `impl/ip/` last written | 19:16:08 | **not touched since** |

So these are the exact bytes `tme_standalone.bit` was built from:

| artifact | sha256 | size |
|---|---|---|
| `component.xml` | `ea0d8a051812efef6fce51906ebd89b24a8a9aa185496ff72a18cd8cf9f771f4` | 139,790 |
| `TermCountB2_hls_tme_top_0_2.zip` | `5d602b3e839a4c3cc4799c2da496c6ff560ce0bcc837a930f0854c7872a66eab` | 652,564 |

Both are now **in the B2 manifest and committed to git**, with `.gitignore`
negations and `-text` attributes so a fresh clone reproduces the bytes rather
than the platform's line-ending conventions. The manifest binding is what
detects an overwrite; the **git copy** is what survives one. Pinning them is
also what will let the board session say *which core ran* rather than inferring
it from a log line.

Three further inputs were pinned at the same time, having been load-bearing and
unrecorded:

* **`tme_top.cpp` and `tme_top.h`** — compiled alongside the snapshot in every
  one of these projects. The snapshot was digest-verified and its two build
  partners were not, so an edit to either would have changed *both* halves of a
  "paired" measurement with no manifest noticing.
* **`b1_sources/correlation_core.b1.cpp`** — the control's source. B2's claim
  is a difference against `b1`; the control's source is evidence for B2 exactly
  as much as its report is.
* **`sw/tme_b1_manifest.py`** — the *imported implementation*. Every digest in
  this manifest is produced by its `digest`, written by its `write` and copied
  by its `mirror`; this file supplies only the entry list. Leaving it unpinned
  meant the rule that produced every number here was itself unrecorded, so a
  change to the EOL or binary-suffix policy would have silently reinterpreted
  the whole manifest.

The off-board correction grew the set from 33 to 42 artifacts. The
fail-closed board protocol added the runner, both vector payloads, checksum
manifest, plan and three scripts, and the 2026-08-20 session then added its
transcripts: `tme_b2_manifest.py --verify` binds **64 artifacts**.

**B2 has since run on silicon** (`logs/b2_board_20260819/`, commit `196f985`):
phase_s 7/7 and hw 9/9 at a gated 125.0000 MHz, exercising tile counts
{1, 3, 4, 6, 38, 52} against the T = 6 this document's co-simulation reached.
Nothing in the sections above was measured on hardware, and none of it is
restated as though it were; the board result is recorded in
`B2_BOARD_SESSION.md` and frozen in `FROZEN["board_b2"]`.

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
  pairing. The **production** suite was run separately, against the same
  snapshot, precisely so the pairing stayed intact.
* **No RTL-level evidence above `T = 6`.** The broad-geometry pass to `T = 52`
  is C simulation. It exercises the source, not what Vitis compiled.
* **The mechanism behind the `2*(T-1)` is not localized.** Only its shape is.
* **The mutant sweep is over 256 uniform fills**, not over arbitrary register
  states, and its containment result bounds where damage *may* appear rather
  than proving where it *does*.

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

There are now **two** warnings attached to that tier rather than one, and the
second is worse than the first reading of it suggested. B1's projected term was
optimistic by `T+1` per (output row, template row). B2's was optimistic by
`3T-1 = (T+1) + 2*(T-1)` — **B1's miss recurred in full, and a further
`2*(T-1)` accumulated on top of it.** Two naive projections, two misses, both
in the direction that flattered the change, and the second larger than the
first in both absolute and per-tile terms.

So: measure B0b's term, do not project it. And do **not** apply either
correction to B0b in advance — "optimistic by about B1's amount" was already
wrong for B2, and there is no reason to think "optimistic by about B2's amount"
will be right for B0b.

B0b has no `FROZEN` block of its own — only the count-pass terms in
`FROZEN["b0b_count_pass"]` and the two endpoints in
`FROZEN["s_per_page_at_125mhz"]`, all of them derived or projected. It must not
acquire a measured tile term, a routed WNS or a board figure by inheritance
from B2's; those are what the two steps B2 has been through are for.

---

## Change note, 2026-08-19: six claims corrected

Every one of these was found by re-reading the evidence against the artifacts,
and each is a claim that was stronger than what the artifacts support.

| # | claim as written | as corrected |
|---|---|---|
| 1 | "B1's overhead did not recur, in size or in shape" | **False.** miss vs the pre-RTL projection is `3T-1 = (T+1) + 2*(T-1)`; B1's `T+1` recurs, and at `T=1` it is the whole miss. `2*(T-1)` is the *additional* miss against a baseline that already carries B1's term. Now enforced by `tme_b2_ab.py` check 4 (14/14, 5/5). |
| 2 | withdrawn projection `20.175432`, miss `0.22973278377777717` | Rounded, and the miss derived from it was **1,687 cycles wrong**. Frozen as integers: `90,789,445,687` → `1,033,795,840` cycles → `0.229732408889` s/page, asserted as an integer identity. |
| 3 | "registered BEFORE the build" | Timestamps say otherwise. The **projection** is retained in commit ancestry before the B2 source/build commit, but an unsigned, unpushed commit is not an external timestamp. `--predict`, its snapshot and the mutant gate all **postdate** the 19:10:13 cosim report. Relabelled *reconstructed pre-RTL baselines* and *post-build adequacy test*. |
| 4 | packaged IP unpinned | `component.xml` and the IP ZIP are the **actual input** to the bitstream (exported 19:16:08, read by a synthesis run launched 19:17:10). Both now in git and in the manifest, along with `tme_top.cpp/.h`, the B1 control source and the imported `tme_b1_manifest.py`. The off-board set grew 33 → **42** artifacts; the later prepared board protocol brings the current manifest to **55** without claiming a board result. |
| 5 | functional evidence capped at `T = 6` | Isolated production C-sim added (`csim_prod_b2.tcl`): **15/15 + 9 direct**, tile counts to `T = 52`, the compiled maximum. The correlation snapshot/vectors were pre-build digest-gated; live shared sources were manifest-bound afterwards. |
| 6a | "B0b starts on a path it will itself be touching" | B0b **inherits** the 12 ps margin; it does not modify the `seg` → DSP path. It may disturb placement around it — still a reason to route early, not a claim about what B0b edits. |
| 6b | "all 256 possible stale register fills" | **256 uniform fills**, not `256**231` states. And the self-healing result is a *containment* bound: it proves the blind cases must be blind; it does not prove every column below `tw-1` changes. |
