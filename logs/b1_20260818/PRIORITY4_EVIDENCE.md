# Priority 4 — B1, runtime segment width: evidence

Captured 2026-08-18/19. Every OFF-BOARD figure here regenerates from the tools
(`tme_b1_ab.py --assert`, `tme_cycle_model.py --assert`,
`tme_b1_manifest.py --verify`), and nothing is transcribed by hand.

**The board numbers do not regenerate.** A session is a one-time event: the
wall times in the silicon section below exist only in the retained transcripts,
and re-running would produce a *new* measurement, not reproduce this one. They
are bound by digest instead — that is what `MANIFEST.sha256` is for.

    cd hls/template_match
    <venv>/python.exe tme_generate_production.py --suite b1
    TME_SOLUTION=cur vitis-run.bat --mode hls --tcl run_hls_b1.tcl
    TME_SOLUTION=b1  vitis-run.bat --mode hls --tcl run_hls_b1.tcl
    TME_SOLUTION=b1b vitis-run.bat --mode hls --tcl run_hls_b1.tcl
    cd ../../.github-upload/sw
    <venv>/python.exe tme_b1_ab.py --assert
    <venv>/python.exe tme_cycle_model.py --assert
    <venv>/python.exe tme_b1_manifest.py --verify

Those three builds are now **safe to re-run in any order, any number of
times**. Each variant owns its own project (`template_match_b1_<variant>`)
opened with `-reset`, so a run can neither inherit sources from a previous run
nor destroy another variant's reports. The earlier single-project form could do
both: `open_project` without `-reset` reopens what is on disk and `add_files`
accumulates into `hls.app`, and the retained `hls.app` still named the
working-tree `correlation_core.cpp` — so re-running the pinned-snapshot script
on top of it would have compiled two cores. See "Provenance of the retained
reports" below for what that meant for the reports this document quotes.

---

## What changed

`correlation_core.cpp`, one loop bound:

```c
static const int SEG_W = PAR_COLS + MAX_TEMPL_W - 1;   // 231, was 232

    int seg_len = tw + PAR_COLS - 1;                   // 19 at tw=4, 231 at tw=216
    load_seg: for (int i = 0; i < SEG_W; i++) {
        if (i >= seg_len) break;
        ...
    }
```

Nothing else moved — not the tile count, the lane masking, the MAC schedule or
the writeback. That is what makes the paired co-simulation below attributable.

`SEG_W` was one larger than any tile can reach: the MAC reads `seg[p + x]` for
`p < 16` and `x < tw`, so the top index is `tw + 14` and `tw + 15` entries are
exactly sufficient. The file's own header comment already said
`seg[PAR_COLS + MAX_TEMPL_W - 1]`; the constant did not.

---

## The headline, and the correction

**The projection was wrong, and the paired measurement is what caught it.**

**Read the units before the numbers.** What is measured is a CYCLE TERM. The
s/page figures below are **workload projections** built from that term over
20,680 modelled trials — no page has been run on any hardware at any clock, and
"26.334 s/page" must never be written as a measured page time.

| | s/page @ 125 MHz | basis |
|---|---|---|
| Phase S, per-trial ROI (no B1) | 36.476 | projection, projected term |
| + B1, as **projected** before any RTL existed | 26.239696410444 | projection, **withdrawn** term |
| + B1, as **measured** by RTL co-simulation | **26.334292108222** | projection, **measured** term |

The projected tile term was `T*(tw + (16+tw-1) + 25) = T*(2*tw + 40)`. The RTL
is

    tile = T * (2*tw + 41) + 1

exact on 14/14 transactions. The projection was optimistic by `T + 1` cycles
per (output row, template row) — in the direction that flatters the change —
worth exactly **0.094595697778 s/page**. Note that miss is against the *exact*
withdrawn projection, not against the 26.240 that was itself a rounded freeze.

The binding freeze is now the **exact integer aggregate**:

    FROZEN["b1"]["aggregate_cycles"] = 118,504,314,487     asserted with ==

not a rounded s/page against `TOL = 5e-4`, which at 36 pages and 125 MHz spans
±2,250,000 cycles — more than the entire overhead this revision records. A
one-cycle drift anywhere in the 20,680 trials now fails the assertion.

B1 still takes the initial-trial matcher projection from 36.476 to 26.334, a
1.385x. What moved is the third decimal, not the conclusion.

---

## Where the two extra cycles are

The residual is not noise. It is exactly `rh*th*(T + 1)` on every transaction —
**one term proportional to the tile count, one constant per call.** Two
constants — but **count the constraints honestly**. Under the fitted form the
residual per (output row, template row) depends *only* on `T`, so transactions
sharing a `T` restate the same equation. `T` spans `{1,2,3,5,6}`: **five**
independent equations, two free parameters, **three surplus constraints**.

The other nine observations are **not** nine repeats. Eight of them sit at an
already-constrained `T` but at a *different geometry*, so what they test is
**geometry invariance** — that the residual really is a function of `T` alone
and not of `pw`, `ph`, `tw` or `th`. That is a genuine test of the form, just a
different one from the three surplus constraints. Exactly **one** observation is
a true repeat: transactions 11 and 12 share `47×21 / 16×12`, and in a
deterministic simulation that confirms determinism, nothing more.

The **declared-model** check is the stronger one and does not share that
weakness: it predicts from `(pw, ph, tw, th)` with **zero** free parameters, so
all **thirteen distinct geometries** are constraints — thirteen, not fourteen,
because transactions 11 and 12 share `47×21 / 16×12`.

**That shape is measured. The mechanism is not.** The natural reading is a
per-tile loop-exit test on `i >= seg_len` plus a per-call bound setup, and it
is only a reading — the `b1b` experiment below removed the exit test entirely
and changed nothing, which is evidence *against* that attribution being the
whole story. The right name for it is **dynamic-bound overhead**; nothing here
establishes where the cycles are spent, and nothing here licenses assuming B2
or B0b pay the same, or pay only this.

`tme_b1_ab.py` runs three checks that can fail independently:

1. the control reproduces the model's published `cur` term (`k = 25`);
2. every B1 latency matches the **declared** model `cycles(..., "B1")`;
3. every shortfall fits `rh*th*(a*T + b)` with `(a, b)` **fitted from the data,
   not read from the model**, coming out `(1, 1)`.

Checks 2 and 3 were briefly the same check — an earlier revision overwrote the
declared model with the fitted value before asserting, making the residual zero
by construction. They are separated now, and a negative control confirms it:
perturbing the declared model by +7 cycles fails check 2 on 14/14 while check 3
still passes on 14/14.

### What `b1b` rules out — and what it does not

A second variant (`b1b`) hoisted a clamped bound out of the tile loop so the
body carries no per-iteration predicate:

```c
    int seg_n = (seg_len < SEG_W) ? seg_len : SEG_W;
    load_seg: for (int i = 0; i < seg_n; i++) { ... }
```

Its transaction report is **byte-identical to `b1`'s**, at 44 more LUTs. So the
**source-level form of the test costs nothing**: writing it as `for (i < SEG_W)
{ if (i >= seg_len) break; … }` or as `for (i < seg_n)` produces the same
schedule to the cycle.

**That is the whole of the result, and it is narrower than it first looks.**
`b1b` still has a **runtime** loop bound — `seg_n` is computed at run time, it
is merely spelled as the induction test instead of a predicate in the body. So
the experiment cannot separate "runtime-bounded control costs `T + 1`" from any
other cause the two variants share; **both** have runtime-bounded control.
Deciding that needs a compile-time-bounded control, and none was run. An earlier
revision of this document concluded "the `T + 1` is what HLS charges for a
runtime-bounded loop" — that went further than the evidence.

The mechanism therefore stays **unlocalized**. The shipped form is the `break`
one, on grounds that do not depend on the mechanism at all: identical cycles,
fewer LUTs, a write that cannot leave the array however `templ_w` is
programmed, and the idiom `mac_loop`/`isq_init`/`isq_slide` already use.

Recorded rather than deleted, but recorded for what it is: someone seeing
`T + 1` in a B2 or B0b measurement need not re-test the *predicate form*. They
still have to measure their own overhead.

### B1 is a net LOSS at the compiled maximum template width

Net saving per (output row, template row) is `T*(217 − tw) − (T + 1)`, so:

| tw | 216 | 215 | 214 | 164 | 100 | 52 | 24 | 4 |
|---|---|---|---|---|---|---|---|---|
| T=1 | **−1** | 0 | +1 | +51 | +115 | +163 | +191 | +211 |
| T=6 | **−1** | +5 | +11 | +311 | +695 | +983 | +1151 | +1271 |

Read the `tw = 215` column carefully: B1 **ties only at `T = 1`** and is a
small net win for every larger tile count (`T − 1` cycles per output row ×
template row). `tw = 216` is the only width that loses outright, and it loses at
every `T`. "Break-even at 215" without naming `T` is wrong.

`phase-s-max` (311×159 / 216×96) is exactly the losing corner: 23,476,737 →
**23,482,881** cycles, 6,144 cycles *slower* = **49.152 µs at 125 MHz**. It is
the compiled bound, not a real trial — every template in the 20,680-trial
workload is narrower, which is where the 36.476 → 26.334 comes from.

**That 49 µs cannot validate B1 on the board.** The bring-up script prints
seconds to milliseconds, so a 0.049 ms difference is two orders of magnitude
under the timer resolution. A board session must carry **workload-width** cases,
where the saving is 20–56%, to measure anything at all.

---

## Verification

### The suite

`tme_generate_production.py --suite b1` — 12 cases, and it refuses to write a
suite that cannot detect a B1 defect (`check_b1_suite`).

| axis | coverage |
|---|---|
| result widths | 1, 15, 16, 17, 31, 32, 33, 95, 96 — every value asked for |
| masked lanes in the final tile | 15 (rw=17, 33), 1 (rw=15, 31, 95), 0 (rw=16, 32, 96) |
| template widths | 216 (`seg_len`=231, the bound), 100, 24, 20, 16 |
| peak placement | last valid column `u = rw − 1` in all nine width cases |
| argmax lane | 0, 5, 14, 15 — including lane 15, the only mutation-sensitive one |
| tie order | two exact ties, byte-identical windows |
| seg_len − 1 off-by-one | `build_lane15` pair, all 256 stale values |

Regeneration is byte-identical from a different directory and under `python -O`
(`tb_tme_b1.sha256`). The production vectors were not touched:
`sha256sum -c tb_tme_prod.sha256` → 4/4 OK.

### Tie order

Two cases, because a same-row tie cannot distinguish row-major from lane order:

* `b1-tie-samerow` — winner (5,4), decoy (21,4): different **tiles**, same row.
* `b1-tie-rowmajor` — winner (31,2), decoy (3,7): the winner is the **larger
  column in the earlier row**. A comparator ordering by column, or a lane-major
  reduction, returns (3,7).

Both windows are byte-identical copies, so the three window sums are identical
integers, the float32 values are identical, and the tie is exact rather than a
rounding artifact. Both score exactly 1.000000 with margin 0.000000, and the
DUT returns the first row-major occurrence in both.

### Last-lane masking, and why the lane-15 pair is the real test

Masked lanes never reach `acc[]`, so they cannot *create* a wrong answer — the
width sweep tests the tile break and the `u < rw` guard, which is worth doing
but is not the hazard B1 introduces.

The hazard is `seg_len − 1`. Only `seg[tw + 14]` goes unwritten, only lane 15
reading the template's last column reads it, and **an exact-crop plant on lane
15 is blind at stale value S = 255** (the corrupted read `255·T[y,tw−1]` equals
the true `P·T` at an exact match). `build_lane15` plants a *pair* with equal
window ink count so the `k·S` term cancels; the generator reports

    b1-lane15: smallest peak score change over all 256 stale values = 0.000000

i.e. at that stale value a score-tolerance assert sees **nothing**, and only the
displaced argmax catches it. That is why the case asserts a location, not a
score.

### The production run was re-done, because the first one proved nothing

The originally retained `csim_prod.log` came from the *old shared project*: it
opened `template_match_b1` and compiled the **working-tree**
`correlation_core.cpp`, not the pinned snapshot the co-simulation measured. A
pass under those conditions is a statement about whatever happened to be in the
tree, not about the RTL under test — and `add_files` accumulates, so re-running
it after the A/B script moved to pinned sources would have put two cores in one
project.

`csim_prod_b1.tcl` now builds a hermetic `template_match_b1_prod` with
`-reset`, compiles `b1_sources/correlation_core.b1.cpp` by digest, and — new in
this pass — **verifies all four `tb_tme_prod.sha256` inputs before running**.
Verifying the source and not the stimulus was half a verification: the suite's
whole claim is about specific pixels, and the blobs are gitignored, so the
digest record is the only thing that says they are the right ones. The retained
log now shows all five digests checked, `open_project -reset
template_match_b1_prod`, and zero occurrences of `add_files
correlation_core.cpp`.

### Results

| stage | result |
|---|---|
| **board, B1 bitstream, gated 125.0000 MHz** | **7/7 PASS** — score within the runner's ±0.005 tolerance and the exact (x, y) on every case; six workload-width savings match the model inside the ±1 ms floor |
| C simulation, unmodified RTL | **12/12 PASS** — establishes the suite as a parity oracle |
| C simulation, B1 RTL | **12/12 PASS**, identical scores and locations |
| C simulation, B1 RTL, **production suite** | **15/15 PASS** from the hermetic `template_match_b1_prod`, compiling the pinned `b1` snapshot and no working-tree source. Source digest **and all four `tb_tme_prod.sha256` inputs** verified before the run. Covers `prod-lane15-small` (24×16 in 200×60), `prod-lane15-full` (164×94 in 622×300), the 817-wide max result map, exact and near ties, negative score, flat region |
| RTL co-simulation, `cur` | **PASS**, 14 transactions |
| RTL co-simulation, `b1` | **PASS**, 14 transactions |
| control: `cur` vs the published model | **14/14 exact**, fitted per-tile k = 25.0 |
| `b1` vs the measured model | **14/14 exact** |
| structure: `naive − measured == rh·th·(T+1)` | **14/14** |

Suite total 2,407,047 → 1,388,191 cycles (1.734× on this suite — the suite is
not the workload; the page figure comes from the 20,680-trial trace).

### Synthesis

| | BRAM | DSP | FF | LUT | est. Fmax |
|---|---|---|---|---|---|
| `cur` | 224 (80%) | 33 (15%) | 18,247 | 34,573 (64%) | 152.74 MHz |
| `b1` | 224 (80%) | 33 (15%) | 18,282 | 34,635 (65%) | 152.74 MHz |
| `b1b` | 224 (80%) | 33 (15%) | 18,285 | 34,679 (65%) | 152.74 MHz |

`load_seg` stays II=1 with iteration latency 4; its worst-case trip count is
232 → 231. BRAM is untouched, which matters: 82.1% was the binding resource in
the routed 125 MHz build.

---

## Routed timing — B1 closes 8.000 ns

Built 2026-08-18 from `TermCountB1:hls:tme_top:0.2` into
`C:/Users/lychee/tc25/vivado_project/tme_b1_125`, standalone image (core + 2
DMAs), xc7z020clg400-1, `TME_FCLK_MHZ=125` so the constraint is exactly
8.000 ns. Priority 2's build root is untouched.

| | unmodified (Priority 2) | **B1** |
|---|---|---|
| constrained period | 8.000 ns | 8.000 ns |
| post-route WNS | +0.064 ns | **+0.135 ns** |
| TNS | 0.000 | **0.000** |
| WHS / THS | +0.015 / 0.000 | **+0.010 / 0.000** |
| verdict | all constraints met | **all constraints met** |
| Slice LUTs | 14,903 (28.01%) | 14,792 (27.80%) |
| Slice Registers | 18,467 (17.36%) | 18,483 (17.37%) |
| Block RAM | 115 (82.14%) | 115 (82.14%) |
| DSP | 34 (15.45%) | 34 (15.45%) |

**Do not read the extra 0.071 ns as a B1 improvement.** The four worst paths are
still

    templ_buf_U/ram_reg_0_3/CLKBWRCLK -> ...VITIS_LOOP_181_3.../t_row_*_reg[3]/D

at **logic levels 0** — the fully-partitioned `t_row` staging array in
`tme_top.cpp`, which B1 does not touch. Same structure as Priority 2, so the
slack difference is placement and routing variation on an unchanged path. What
this build establishes is the verdict, not the margin: the shortened segment
load **still meets the 8.000 ns constraint**, and BRAM is unchanged at the
82.1% that was already the binding resource.

Stated that narrowly on purpose. "Does not cost timing" would be a claim about
the design's achievable frequency, which a single implementation run at one
period cannot support: the router stops once the constraint is met, both builds
met it, and neither was pushed to failure. What is established is a verdict at
one period, not a margin and not a maximum.

Post-route LUTs went slightly *down* (14,792 vs 14,903) even though the HLS
estimate went slightly *up* (34,635 vs 34,573). The routed number is the one
that means anything; the estimate is scheduling-stage bookkeeping.

The bitstream and `.hwh` are at
`C:/Users/lychee/tc25/vivado_project/tme_b1_125/overlay_output/tme_standalone.{bit,hwh}`
(`2cd4a2b0…` / `ffa94282…`), outside this repo with the rest of the Vivado
project. **They have since been run on the board** — see "Silicon" below; an
earlier revision of this paragraph said they had not, and was left standing
after the session. The hashes above are the ones the board session re-took
inside the transcript that configured the PL.

### A packaging trap worth not re-discovering

`export_design -version "0.2b1"` **produced a `component.xml` carrying version
1.0**, and reported success. What is **established** is the substitution: the
requested string is not what was written, and the exit code did not say so.

*Why* is **not** established. "A VLNV version must be numeric" is the obvious
explanation and it was never tested — no other malformed version was tried, and
no documentation was consulted. The lesson does not depend on the cause: this
flow can write a VLNV field you did not ask for and still exit 0.

1.0 is the version this project reserves for a release, so the quiet failure
mode was publishing a B1 build under the release identity. Two fixes:

* the B1 IP is `TermCountB1:hls:tme_top:0.2` — the **vendor** carries the
  distinction, since the version field cannot;
* `package_b1.tcl` now reads the VLNV back out of the generated
  `component.xml` and fails on any mismatch;
* `build_tme_standalone.tcl` takes `TME_HLS_VLNV` (default unchanged, so an
  invocation that names nothing still builds the core every recorded WNS was
  measured on) and prints the VLNV in its banner.

---

## Silicon — 7/7 at a gated 125 MHz, and the saving is measurable

Board session 2026-08-19T05:13–05:16Z, full record in
`logs/b1_board_20260818/B1_BOARD_SESSION.md`. Overlay
`TermCountB1:hls:tme_top:0.2`.

**It is a controlled comparison.** The runner and all three vector files are
byte-identical to the Priority 3 session that measured the *unmodified* core at
the same clock; only the `.bit`/`.hwh` changed. Reusing the hash-bound
`phase_s` suite also means no gate evidence was regenerated.

| case | patch / templ | cur (P3) | B1 | measured Δ | predicted Δ | residual |
|---|---|---|---|---|---|---|
| phase-s-min-templ | 99×67 / 4×4 | 0.006 | 0.004 | +2.0 ms | +2.603 ms | −0.60 |
| phase-s-origin | 147×94 / 52×31 | 0.040 | 0.024 | +16.0 ms | +15.602 ms | +0.40 |
| phase-s-workload-mode | 147×94 / 52×31 | 0.040 | 0.024 | +16.0 ms | +15.602 ms | +0.40 |
| phase-s-workload-wide | 259×105 / 164×42 | 0.075 | 0.068 | +7.0 ms | +6.688 ms | +0.31 |
| phase-s-final-cell | 215×157 / 120×94 | 0.144 | 0.117 | +27.0 ms | +27.674 ms | −0.67 |
| phase-s-workload-max | 215×157 / 120×94 | 0.144 | 0.117 | +27.0 ms | +27.674 ms | −0.67 |
| **phase-s-max** | 311×159 / 216×96 | 0.189 | 0.190 | **−1.0 ms** | **−0.049 ms** | −0.95 |
| total | | 0.638 | 0.544 | +94.0 ms | +95.793 ms | −1.79 |

Each wall time prints to milliseconds, so each *difference* carries ±1 ms. The
six informative residuals span −0.67 to +0.40 ms.

**`phase-s-max` was predeclared as uninformative and is.** Its predicted change
is −49 µs, twenty times under the print resolution; its measured −1.0 ms sits
within ±1 ms of the prediction *and* within ±1 ms of zero, so it discriminates
nothing. It appears because it is in the suite. This is precisely why the
session had to carry workload widths.

All gates met: `FCLK0 gate: PASS — 125.0000 MHz` (fail-closed), 7/7 PASS,
re-invocation PASS in the *shrink* direction, `__EXIT__ 0` (which requires the
DMA-halt check), `RESTORE_VERIFIED`, shipping artifacts untouched.

**What a PASS is, precisely.** `tme_standalone_bringup.py` requires
`abs(score - gold) <= SCORE_TOL` with `SCORE_TOL = 0.005`, *and* `(x, y)`
equal. The location is exact; **the score is not tested for equality**. All
seven cases print `+1.0000` against a gold `+1.0000`, but the transcript prints
four decimals and the gate allows 0.005, so "exact score" is not something this
session establishes and is not claimed here.
The bitstream hash is re-taken **inside the transcript that configures the PL**,
so the chain from bytes to result is unbroken for this run.

**What silicon does not add.** Agreement here is at ±1 ms = ±125,000 cycles,
three orders of magnitude coarser than the co-simulation. The board says the
term is right at workload widths; the cosim is what makes it exact. And nothing
here is a page time.

---

## Evidence hierarchy — where B1 now sits

B1 moves **two tiers**. The RTL exists, its cycle term is measured by paired
co-simulation, and it has since run on silicon: 7/7 at a gated 125.0000 MHz
with the saving resolved at six workload widths. (An earlier revision of this
section ended "It is *not* measured in the board sense" and then listed the
board result three bullets later. The board result is the current state; that
sentence was stale.)

What B1 is still **not** is a measured *page*. The board resolves ±1 ms;
the page figure sums a cycle term over 20,680 modelled trials.

Closed since this section was first written — listed so the change is visible,
not because they are open:

* **Routed timing.** All constraints met at 8.000 ns.
* **125 MHz.** No longer a constrained period: `Clocks.fclk0_mhz` read back
  125.0000 through a fail-closed gate.
* **Silicon.** 7/7 (±0.005 on score, exact on location), saving resolved at six
  workload widths.

Still open, and not claimed here:

* **The page.** **26.334292108222 s/page is a projection over the
  20,680-trial trace**, matcher-only:
  it excludes refinement, DMA, extraction and PS work, and no page has been run
  end to end at any clock.
* **B2 and B0b are still projections written in the same optimistic style** as
  B1's was, and their tile terms have not been measured. B1 is the standing
  warning, but be precise about what it warns of: a projected tile term was
  optimistic by `T + 1` cycles per (output row, template row) **here**, at
  **this** geometry sweep, for reasons that were never localized. That is a
  reason to measure, not a rate to carry over. Nothing here licenses assuming
  B2 or B0b pay `T + 1`, pay only `T + 1`, or pay anything at all — and the
  `b1b` result shows only that the *source-level form* of a bound is free, not
  where the cycles come from. Each must be measured on its own.

---

## Files

| file | role |
|---|---|
| `hls/template_match/correlation_core.cpp` | the change |
| `hls/template_match/tme_generate_production.py` | `--suite b1`, `build_b1_*`, `check_b1_suite` |
| `hls/template_match/tme_tb.cpp` | whitelists `b1` and `prod` |
| `hls/template_match/run_hls_b1.tcl` | one hermetic project per variant; never opens the frozen `template_match/solution1` |
| `hls/template_match/csim_prod_b1.tcl` | production-geometry lane-15 in C |
| `hls/template_match/package_b1.tcl` | exports vendor `TermCountB1`, version **`0.2`** — see the packaging trap above; `0.2b1` is not a legal VLNV version and was silently rewritten to `1.0` |
| `hls/template_match/b1_sources/` | the three pinned, hash-verified source snapshots + README |
| `hls/template_match/template_match_b1_{cur,b1,b1b}/` | the three hermetic HLS projects: cosim transaction reports and `csynth.rpt` |
| `.github-upload/sw/tme_b1_ab.py` | the adjudicator |
| `.github-upload/sw/tme_cycle_model.py` | measured `B1` variant, exact `FROZEN["b1"]` aggregate |
| `.github-upload/sw/tme_scale_policy.py` | recomputes every cycle figure from the model; reads no captured `cycles_*` column |
| `.github-upload/sw/tme_b1_manifest.py` | writes and verifies the manifest below |
| `.github-upload/sw/tme_standalone_bringup.py` | the board runner (current copy, with the stale-banner notice) |
| `.github-upload/hls/template_match/tb_tme_{cases,patches,templs}_phase_s.*` | the vector payloads the board consumed |
| `trace_20260818b/B1_COLUMN_STALE.md` | marks `cycles_S_B1` as computed with the withdrawn term |
| `logs/b1_20260818/correlation_core.cpp.pre_b1` | the pre-B1 source (also git `eb1c8ac`) |
| `logs/b1_20260818/correlation_core.cpp.b1_break` | the source the *original* `b1` cosim compiled — comment-only different from the `b1` snapshot (see below) |
| `logs/b1_20260818/tme_cycle_model.py.pre_b1` | the model carrying the withdrawn projection |
| `logs/b1_20260818/overlay_output/tme_standalone.{bit,hwh}` | the B1 image that ran: `2cd4a2b0…` / `ffa94282…`, copied in from the out-of-repo Vivado build root |
| `logs/b1_20260818/overlay_output/post_route_{timing_summary,worst_paths}.rpt` | the full routed reports the WNS/worst-path claims are extracted from |
| `vivado/tme_standalone/build_tme_standalone.tcl` | turns the packaged IP into that image; takes `TME_HLS_VLNV` |
| `logs/b1_20260818/MANIFEST.sha256` | binding digest over every Priority 4 artifact (`tme_b1_manifest.py --verify` prints the count) |
| `logs/b1_board_20260818/` | the board session: 5 transcripts, `B1_BOARD_SESSION.md`, and `tme_standalone_bringup.py.as_run` |

---

## Provenance of the retained reports

Two gaps were found on 2026-08-18 while making the A/B build re-runnable. Both
are recorded rather than papered over.

**1. The reports quoted above were re-produced, and they reproduce exactly.**
The transaction reports and `csynth.rpt` files this document originally quoted
were built by an earlier version of `run_hls_b1.tcl` that added the
*working-tree* `correlation_core.cpp`, not the pinned snapshot — the retained
`hls.app` still names it. The pinned-snapshot script had therefore **never
produced them**, and could not have: the snapshot lives in `b1_sources/`, one
level below `tme_top.h`, so it needs an explicit include path that the script
did not carry. Both are fixed, and all three variants were rebuilt from the
pinned sources into hermetic per-variant projects (`logs/b1_rerun_20260818/`).

| rebuilt artifact | against the original |
|---|---|
| `cur` `result.transaction.rpt` | **byte-identical** |
| `b1` `result.transaction.rpt` | **byte-identical** |
| `b1b` `result.transaction.rpt` | **byte-identical** — and still byte-identical to `b1`'s, so the "hoisting the bound changes nothing" result survives the rebuild |
| `csynth.rpt` timing + resource sections | **identical** |
| `csynth.rpt` remainder | differs in build date, project name, and operator names derived from source line numbers (`add_ln74` → `add_ln104`) |

The line-number shift is expected and is the reason the transaction reports
could be byte-identical in the first place: `correlation_core.cpp.b1_break` —
the file the original `b1` cosim compiled — differs from the `b1` snapshot
**only in comments**, and those comments moved the code down 30 lines.
`tme_b1_ab.py --assert` passes against the rebuilt reports, so the paired
measurement this document rests on is now reproducible from the tree.

**2. The packaged IP on disk post-dates the bitstream.** `package_b1.tcl` ran
at 22:06 and overwrote `template_match_b1/b1/impl/ip/`, which the 21:35 Vivado
build had already read. The IP the board image was actually built from was not
retained. What ties the image to a core is `vivado_b1_125.log`, which names the
IP repository path and resolves `TermCountB1:hls:tme_top:0.2`, plus the
bitstream hash re-taken inside the configure transcript. The IP in the manifest
is labelled a **re-export** for exactly this reason.
