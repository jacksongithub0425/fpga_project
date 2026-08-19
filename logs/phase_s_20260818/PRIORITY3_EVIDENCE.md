# Priority 3 — Phase S prototype: evidence

Captured 2026-08-18. Tool: `.github-upload/sw/tme_phase_s.py`, run with
`hls/.venv/Scripts/python.exe`. **No RTL was modified and none needs to be** —
Phase S turns out to be a pure PS/driver change (see "No RTL change" below).

Every figure here regenerates from the tool; nothing is transcribed by hand.

    python tme_phase_s.py --selftest
    python tme_phase_s.py --truncation-report
    python tme_phase_s.py "../../sample/*" --control
    python tme_phase_s.py "../../sample/*" --phase-s --fix-truncation --truth

---

## What was built

The extractor is untouched and `MAX_PATCH` stays 820x307. For every trial the PS
crops a sub-rectangle out of the patch it already holds, sized so the correlation
produces exactly a **96x64 result search area**, and correlates only that.

ROI placement rule (pure integer arithmetic, PS-reproducible):

    anchor_x = tw - 1 (left) or 0 (right)        anchor_y = th // 2
    ideal    = (int(round(endpoint)) - patch_origin) - anchor
    roi_x0   = clamp(ideal_x - roi_w//2, 0, rw_full - roi_w)
    roi_y0   = clamp(ideal_y - roi_h//2, 0, rh_full - roi_h)

The rule uses the **resized** template's integer anchor, not the detector's
`base_anchor * scale`. That is deliberate and safe: the rule only decides WHERE
to look. All scoring and penalty arithmetic downstream is untouched and still
runs on the returned global location, so the sub-pixel difference between the two
anchors cannot affect a score — only, at the margin, a crop bound.

## Control — the harness is trustworthy

`--control` instruments everything and changes nothing:

| | |
|---|---|
| pages byte-identical to `baseline_cpu_20260811` | **36 / 36** |
| initial trials | **20,680** |
| refinement trials | **808** |
| full-patch search, modelled | **333.413 s/page** |
| Phase S / +B1 / +B2 | **36.476 / 26.240 / 20.175** |
| refinement at Phase S geometry | **1.492 s/page** |

Every one of those reproduces the Priority 0 frozen figures independently, from a
tool that computes them from the corpus rather than reading them from
`model.FROZEN`.

## Verification items

| item | result |
|---|---|
| crop contents | the cropped score map **is** the sub-rectangle of the full map. TM_CCOEFF_NORMED at (x,y) depends only on the (th x tw) window there, so this is structural, not empirical. Proven on synthetic data in `--selftest` and re-proven on all 20,680 real trials. |
| border behaviour | **no padding is ever introduced**, so there is no border case to get wrong. Clamping at all four edges, a result map smaller than the ROI in one or both axes, and a degenerate 1x1 map are all covered; 20,000 random geometries all stay in bounds. |
| local -> global conversion | exercised end to end: cropped values are written back at the ROI offset and the detector's own unmodified coordinate code consumes them to produce page output. |
| raw score and winning location | recorded per trial for both the oracle and Phase S (`--dump`). |
| global tie order | `cv2.minMaxLoc` scans row-major; masking the exterior leaves the first in-ROI tie winning, and drops an earlier out-of-ROI tie. Tested explicitly. |
| final page output | measured against the baseline **and** against labelled truth, below. |

**One caveat on "exact".** On CPU the crop is numerically equal to the full map to
**1.222e-06**, not bit-exact (only 3.23% of trials matched bit-for-bit). That is
OpenCV switching between spatial and DFT correlation with the array size — not a
crop-placement error. The HLS core is exact integer and has no such term.

## The gate: the oracle is NOT reproduced

All 20,680 trials ran. They do not reproduce the full-search oracle:

| | initial trials |
|---|---|
| oracle argmax falls inside the 96x64 ROI | **9,865 / 20,680 = 47.70%** |
| Phase S returns the same location | 45.10% |
| Phase S returns the same score | 18.61% |

Page parity against `baseline_cpu_20260811`: **4/36 byte-identical**, 32 pages
differ, **6 pages change counts**, net `male -1, female +1, ferrule -3`.

By the Priority 1 precedent this is a parity **FAIL**, and it is reported as one.

## But the baseline is not ground truth

The workbook `sw/expected_result.xlsx` labels 28 of the corpus files. Scored
against the labels, counts summed per input file:

| file | truth m/f/fe | baseline | Phase S 96x64 | \|err\| |
|---|---|---|---|---|
| example_01 | 36/36/0 | 36/32/0 u4 | **36/36/0 u0** | 4 -> 0 BETTER |
| example_02 | 20/17/0 | 20/18/0 u1 | **20/17/0 u1** | 1 -> 0 BETTER |
| example_06 | 29/3/37 | 28/3/37 u8 | 28/3/34 u11 | 1 -> 4 WORSE |
| example_21 | 6/6/0 | 6/7/0 u0 | **6/6/0 u0** | 1 -> 0 BETTER |
| example_29 | 14/19/0 | 15/19/0 u9 | **14/19/0 u10** | 1 -> 0 BETTER |

    files scored               28   (7 unlabelled, skipped)
    improved / unchanged / regressed        4 / 23 / 1
    TOTAL absolute count error   baseline 26 -> Phase S 22   (-4)

**Phase S is 9.14x faster and measurably more accurate on this corpus.** Three of
the four improvements are exact hits on the labelled count. The single regression
loses 3 ferrules on `example_06`.

The right reading: the ROI removes far-from-endpoint spurious maxima that were
beating the true match and then being penalised into `unknown`. That is a policy
change with an accuracy benefit — not a defect — but it IS a change, so it is the
user's call to accept, not the tool's.

## Refinement collapses 808 -> 0

Under Phase S **no refinement call fires at all** (control: 808).

**This is an EMPIRICAL result on this corpus, not a proof.** 808 calls fired
under the control and 0 fired under Phase S; that is a count, and it is the whole
of the evidence.

The proposed mechanism is that refinement triggers on `old_dy > max(20, h*0.70)`
while the ROI gives the box only +/-32 rows around the endpoint. But that does
NOT close: the trigger is `max(20, ...)`, so for any template with `th < 29` the
threshold is 20 px and a box 21-32 rows off would still trip it. The ROI narrows
the opportunity; it does not eliminate it. Clamping at a patch border widens it
further, since a clamped ROI is no longer centred on the endpoint at all.

So this is the same shape as the red flag recorded for the re-centred scale
ladder ("the misalignment check going quiet, not alignment improving") and it
deserves the same scepticism. What can be said is narrower than "cannot happen":
on these 36 pages it did not happen once.

**Cross-priority consequence.** Priority 8 exists because 65.6% of the 808
refinement calls need a non-scalar argmax that the core cannot provide. Under
this ROI policy that workload is empty, so Priority 8 becomes **contingent rather
than blocking** — it is still needed for any input that does produce a misaligned
box, but it is no longer on the critical path. On this corpus it also removes the
1.492 s/page refinement term from the Phase S budget — a corpus-conditional
saving, not a structural one.

## example_06: why the one regression happens

Investigated 2026-08-18. **Neither hypothesis was right for this page: it is not
ROI clamping and not an off-endpoint terminal.** Across `example_06`, **0 of
2,208 trials** were clamped or shrunk — the ROI never touched a patch border
here. That is a per-page census, not a corpus-wide one; see "Consequence for the
plan" below for what it does and does not license.

The three lost ferrules all fail the same way (comparing crop vs no-crop at equal
envelope, so the truncation fix is held constant):

| endpoint | ferrule score | male score | outcome |
|---|---|---|---|
| (5194.5, 2260.8) | 0.5646 -> **0.5646** | 0.4395 -> **0.5844** | margin 0.0197 < 0.03 -> unknown |
| (5194.5, 2700.7) | 0.5628 -> **0.5628** | 0.5025 -> **0.5847** | margin 0.0219 < 0.03 -> unknown |
| (5194.5, 4102.7) | 0.5628 -> **0.5628** | 0.4794 -> **0.5846** | margin 0.0218 < 0.03 -> unknown |

**The ferrule score never moves.** What changes is that the *male* score rises to
meet it, and the `SCORE_MARGIN = 0.03` tie-break then rejects both.

**This is COUPLED search-and-calibration behaviour — not "the classifier rather
than the search".** Both halves move, in sequence. Restricting the raw argmax
changes *which male peak is selected*; the post-argmax distance penalty is then
evaluated at that new location, which changes the score distribution the
thresholds were calibrated against. Measured, per endpoint:

| endpoint | policy | winning male | raw | dist px | penalty | selected |
|---|---|---|---|---|---|---|
| (5194.5, 2260.8) | control | 59x36 | 0.5114 | 28.48 | 0.0720 | 0.4395 |
| | Phase S | **52x31** | 0.5878 | **1.12** | **0.0032** | 0.5844 |
| (5194.5, 2700.7) | control | 52x31 | 0.5878 | 29.48 | 0.0853 | 0.5025 |
| | Phase S | 52x31 | 0.5878 | **1.00** | **0.0029** | 0.5847 |
| (5194.5, 4102.7) | control | 59x36 | 0.6979 | 86.43 | 0.2183 | 0.4794 |
| | Phase S | **52x31** | 0.5878 | **1.03** | **0.0030** | 0.5846 |

The search half is visible in the first column: two of the three change *scale*
(0.80 -> 0.70), so the raw argmax genuinely moves rather than merely being
re-scored. The third row is the clearest case — its raw score **falls** 0.6979 ->
0.5878, and it still wins, because the penalty collapses 0.2183 -> 0.0030.

**The penalty cap is NOT universally near 0.10** — it is template-dependent,
since `norm = max(8, 0.5*(tw+th))`. Inside a 96x64 ROI the largest offset is
`hypot(48, 32) = 57.7 px`, giving

    52x31  male     norm 41.5   ROI penalty range  0 .. 0.1668
    109x26 ferrule  norm 67.5   ROI penalty range  0 .. 0.1026

so the *smaller* competing template has the *larger* cap, 0.167. Quoting 0.10 as
the cap silently generalises the ferrule's number to the male that actually won.
What is true is much more specific: these three male penalties fall to
**0.0029-0.0032**, essentially zero, because the ROI puts the peak ~1 px from
the endpoint.

The same coupling produces the gains — two endpoints on this page go
`unknown -> male` (control margins 0.0095 and 0.0182, both under 0.03). It
backfires here only because a 52x31 male template at scale 0.70 fits *inside* a
109x26 ferrule, so once it may sit on the endpoint it scores 0.5878 against the
ferrule's 0.5646.

**Margin sensitivity** (endpoint-level re-decision from the scores each policy
produced; nothing re-correlated). Truth for this file is male 29 / female 3 /
ferrule 37:

| margin | control m/f/fe/unk | Phase S m/f/fe/unk |
|---|---|---|
| 0.00 | 27 / 4 / 43 / 6 | 32 / 3 / 39 / 6 |
| 0.01 | 26 / 4 / 40 / 10 | 32 / 3 / 33 / 12 |
| 0.03 (shipped) | 26 / 3 / 38 / 13 | 28 / 3 / 28 / 21 |
| 0.05 | 26 / 3 / 23 / 28 | 28 / 3 / 10 / 39 |

Both policies are extremely sensitive to this constant, and Phase S is more
sensitive, because its scores are bunched closer together. The thresholds
(0.33 / 0.24) and the margin (0.03) were tuned against the full-search score
distribution and Phase S shifts that distribution.

**Consequence for the plan.** On this page the ROI rule needs no variant. That
is as far as the evidence reaches: **0 of 2,208 unclipped trials rules out border
clamping FOR example_06 ONLY.** It does not survey the corpus, and it therefore
does NOT globally rule out a per-kind or asymmetric ROI policy — a page whose
endpoints sit nearer a patch edge, or a kind whose templates are wider than the
ROI, could still clamp. Deciding that needs a corpus-wide clamp census, which was
not run.

What Phase S disturbs is search and calibration together. Retuning
`SCORE_MARGIN` is a detector change, outside Priority 3's scope, and is NOT done
here — but no Phase-S accuracy claim should be quoted without noting that it was
measured at a margin tuned for the old distribution.

## ROI size sweep — bigger does not buy parity

All with the truncation fix on. "Phase S" is this run's own ROI geometry.

| ROI | argmax in ROI | identical | count-changed pages | Phase S s/page | +B2 |
|---|---|---|---|---|---|
| **96x64** | 47.70% | 4 | 6 | **36.476** | **20.175** |
| 128x96 | 57.22% | 5 | 6 | 70.559 | 37.452 |
| 192x160 | 75.08% | 5 | 6 | 157.841 | 79.442 |
| 256x192 | 94.18% | 12 | 1 | 225.639 | 110.554 |

Two conclusions. **Cost explodes far faster than parity recovers** — 128x96
already doubles the time and still changes the same six pages. And at 256x192 the
crop is nearly a no-op: the result converges on the truncation-fix-only run
(1 page, `male -2 / unknown +2`), confirming the sweep is internally consistent.

**96x64 is the only affordable operating point.** Parity cannot be bought back by
growing the ROI.

## The int() truncation fix

`--truncation-report`: 4 of 7 distinct templates change patch geometry.

    left  ferrule 109x28   max_tw 163 -> 164   patch 619x134 -> 622x134
    left  male     74x45   max_th  67 ->  68   patch 421x214 -> 421x217
    right ferrule 109x26   max_tw 163 -> 164   patch 619x124 -> 622x124
    right male     74x45   max_th  67 ->  68   patch 421x214 -> 421x217

The envelope only ever grows, so the fix cannot make a template stop fitting.

Measured **alone** (no crop) it is not parity-neutral either: 14/36 identical,
1 page changes counts (`male 28->26, unknown +2`), and the full-patch cost rises
333.413 -> 335.384 s/page.

Measured **with the crop** it is completely inert at page level — identical 4/36,
the same six pages, the same deltas. That is structural: the ROI is anchored to
the endpoint in global coordinates, so it selects the same pixels no matter where
the patch origin sits. **Phase S decouples the search window from the envelope**,
which is the same decoupling the scale-policy work found was worth a lot
independently.

## No RTL change is required

Phase S needs nothing from the RTL. `pw`/`ph` are already runtime arguments; the
compiled 820x307 `MAX_PATCH` is only a bound. Feeding the existing, unchanged
core a 311x159 patch with a 216x96 template *is* the maximum Phase-S trial.

So the remaining half of the gate — **measure real Phase-S board cycles** — is a
vector-and-run exercise on the already-validated 125 MHz standalone image, not an
implementation task.

## Board measurement — DONE 2026-08-19

Full record: `logs/phase_s_board_20260818/PHASE_S_BOARD_SESSION.md`.

**7/7 cases PASS at a verified 125.0000 MHz**, re-invocation PASS, DMA halt
verified, remote `__EXIT__ 0`, board restored and verified. The predeclared
comparison:

    phase-s-max   model 0.187814 s   measured 0.189 s   delta +1.19 ms
                  tolerance +/-5 ms declared before the run   -> PASS

All seven residuals are positive, 1.19-2.68 ms, with relative error falling from
41.5% on the 4x4 case to 0.63% on `phase-s-max` — the signature of a fixed
DMA/control/polling cost, matching the 125 MHz gate's pattern.

**This is wall time, not a cycle count.** The RTL has no cycle counter, so
nothing on the board counts cycles; 0.189 s is *consistent with* 23,476,737
cycles at 125 MHz plus ~1.2 ms of fixed overhead, and does not independently
confirm the cycle figure. Measurements are printed to 3 dp, so each carries a
+/-0.5 ms quantisation floor.

The **geometry** is therefore silicon-anchored. The **36.476 s/page page figure
is still a projection** — it aggregates 20,680 modelled trials and no page has
been run end to end.

**The vectors are ready.** `tme_generate_golden.py` gained `--only <suite>`, and
`build_phase_s()` emits a suite under its own name so the hash-bound files are
never rewritten. Verified: after generating, all 14 `csim`/`cosim`/`hw`/`prod`
files still match their pre-run hashes, and the new suite regenerates
byte-identically. Record: `tb_tme_phase_s.sha256`.

    python tme_generate_golden.py --only phase_s          # in hls/template_match/
    python tme_standalone_bringup.py --suite phase_s      # on the board

| # | case | patch | template | cycles | s @125 MHz |
|---|---|---|---|---|---|
| 0 | phase-s-min-templ | 99x67 | 4x4 | 529,857 | 0.004239 |
| 1 | phase-s-origin | 147x94 | 52x31 | 4,675,602 | 0.037405 |
| 2 | phase-s-workload-mode | 147x94 | 52x31 | 4,675,602 | 0.037405 |
| 3 | phase-s-workload-wide | 259x105 | 164x42 | 9,039,507 | 0.072316 |
| 4 | phase-s-final-cell | 215x157 | 120x94 | 17,775,923 | 0.142207 |
| 5 | phase-s-workload-max | 215x157 | 120x94 | 17,775,923 | 0.142207 |
| 6 | **phase-s-max** | 311x159 | 216x96 | **23,476,737** | **0.187814** |
| | seven unique cases | | | 77,949,151 | 0.623593 |
| +0 | re-invocation of case 0 | 99x67 | 4x4 | 529,857 | 0.004239 |
| | **actually executed** | | | **78,479,008** | **0.627832** |

**The order is load-bearing and asserted.** `tme_standalone_bringup.py` re-runs
`cases[0]` after the whole suite, to catch stale `static` BRAM residue. So the
suite must ASCEND — smallest first, largest last — for that check to test the
shrink direction it exists for.

An earlier draft of this suite had `phase-s-max` at index 0. That was wrong twice
over: the re-invocation re-ran the *largest* case, testing the grow direction the
sequence had already covered, and it silently added 23,476,737 cycles to the run
a second time, so the executed total was **101,425,888**, not the 77,949,151 the
seven unique cases sum to. `build_phase_s()` now asserts `cases[0]` is the
smallest, `cases[-1]` is the largest, and `cases[-1].tag == "phase-s-max"`.

`phase-s-max` is exactly `PHASE_S_GEOMETRY`, and its 23,476,737 cycles is exactly
`FROZEN["phase_s"]["max_cycles"]` — so the board run measures the frozen figure
directly rather than something adjacent to it. The whole suite is 0.62 s at
125 MHz, so it needs no special timeout handling.

Geometries come from the 20,680-trial trace, not invention: `workload-mode` is
the most common trial (3.6%), `workload-max` and `workload-wide` are the largest
and widest that actually occur, and `max` is the compiled bound, which sits above
every real trial.

**Two stress axes vanish under Phase S**, which is a finding rather than a gap in
the suite. The result map is constant at 96x64, so `MAX_RESULT_W/H` (817/304) are
never approached and `stress-max-result` has no Phase-S analogue. And the largest
patch is 49,449 B against the §3.1 bound of 262,143 — **18.9%** — so the
single-DMA-transfer limit stops being tight. §3.1 remains covered by `--suite hw`,
which is untouched.

### Preflights (required before silicon, all passed 2026-08-18)

1. **`tme_tb.cpp` accepts `phase_s`.** The suite whitelist at the `main()` argv
   loop rejected it, so the suite could not be C-simulated at all. Added, and
   **C-sim run: 7/7 cases PASS** plus the §4.6 direct tests at 0 failures. Run in
   a scratch HLS project (different project name) so `-reset` could not touch
   `hls/template_match/template_match`.
2. **`--expect-fclk-mhz` is fail-closed.** The bring-up only *warned* on a wrong
   clock and, worse, continued silently when the clock could not be read at all —
   producing a clean-looking run whose every elapsed time is uninterpretable.
   Both now abort. The stale "contract §8 records 31.25 MHz / constrained at
   20 ns" guidance was written for the 50 MHz-request shipping image and is no
   longer printed for other builds.
3. **Suite reordered ascending** and `tb_tme_phase_s.sha256` regenerated. See the
   order note above.

Remaining: run it on the board and compare measured wall time against the model.

### What the board measurement is, and is not

The comparison is **`phase-s-max` wall time vs 0.187814 s, tolerance +/-5 ms,
declared before the run**.

That wall time is **DMA + core + polling**, measured PS-side. It is **not a
direct measurement of 23,476,737 cycles** — the RTL carries no cycle counter, so
nothing on the board counts cycles. The model is being tested against an
end-to-end time that includes fixed overhead the model does not describe. This is
the same construction the 125 MHz gate used, where that fixed overhead was
visible as a residual growing as compute shrank (1.48% on the 4x4 case against
0.055% on the envelope case).

## Evidence tiers, unchanged

| tier | content |
|---|---|
| measured | control 36/36; 20,680 trials; crop identity to 1.222e-06; labelled accuracy 26 -> 22; refinement 808 -> 0 |
| silicon-anchored | the `cur` cycle formula the s/page figures use |
| silicon-anchored | Phase-S GEOMETRY: 7/7 correct at 125.0000 MHz, `phase-s-max` wall time within +1.19 ms of model |
| core-only projection | Phase S 36.476 s/page — aggregates 20,680 modelled trials; no page run end to end |
| unproved | end-to-end page latency; Phase S accuracy policy (Detector-v2 work) |
