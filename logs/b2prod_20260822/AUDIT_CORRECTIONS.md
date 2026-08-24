# B2/100 audit corrections — 2026-08-22

The 2026-08-22 audit left conditions **6** (first PDF and stress page) and
**7** (36/36 corpus parity) open, and named a set of corrections that had to
land before the next board session. This records what changed, what it was
measured against, and what is still not done.

**Nothing here is a board result.** Every number below came from off-board
execution against `test_pl_backends.FakePL` or from pure software. No file was
committed.

---

## 1. Gate 7 — the two remaining fail-open paths

Both were reproduced first, then fixed, then mutation-controlled.

### 1.1 An unexpected R5 result escaped `run_all`'s abort handler

`run_all` wrapped R1–R3 in a try/except that closed the pipeline and
reprogrammed the PL. **R4 and R5 ran outside it.** R5 makes three `require`s
before it reaches its own `close()`/reset, and each of them raises on an
unexpected result — a core that completed unfed, a driver that let a start
through, a guard that refused with wording R5 does not recognise.

Reproduced (`gate7_repro_before_fix.py`, the guard reworded and nothing else):

```
resets = 1     holds = 0     outstanding = True     closed = False
```

A wedged core left wedged, CMA pages retained by a refusing `close()` that was
never called, and the only reference to them a frame that was about to die.

R4 has the same shape of hole for a page that **times out** mid-transfer:
`_discard` prints a note about a retained buffer and lets the abort carry on.

**Fixed** by factoring the recovery into `abort_recovery()` — close, reprogram,
or hand over to the fail-stop hold — and routing R4 and R5 through it.
Same file, after:

```
resets = 2     holds = 0     outstanding = True     closed = True
```

`outstanding` stays True and that is correct: the driver's latch is what makes
`close()` retain. What changed is that the close *happened* and the PL was
*reprogrammed* afterwards.

### 1.2 `fail_stop()` printed before it held

`fail_stop()` announced itself with `print()` before calling `hold_fn`. On the
teardown path stdout is a notebook's and can already be gone. Measured against
a stdout that raises:

```
outcome = BrokenPipeError: stdout is gone      hold_calls = 0
```

The hold was skipped **because the log went away** — the exact failure mode
`safe_teardown.say()` exists to prevent. Two more sites had it: the failure
message inside `final_reprogram()` (whose `False` is what sends `main()` into
the hold) and `main()`'s own message one statement before the hold. All three
use `say()` now; `final_reprogram()` returns `False` instead of raising.

### 1.3 What the tests now require

`test_board_gate_recovery.py`: **18 tests, 12 injected defects** (was 13 and
10). Two new defects — `r5_wrong_error` (the busy-core guard refuses with
unrecognised wording) and `r4_timeout` (R4 aborts with the pipeline poisoned)
— exist because without them the two new guards would be untested code.

The invariant is asserted directly, over the clean run **and all twelve
defects**: every pipeline must end in one of exactly three states —

1. a `close()` that proved the DMAs halted and freed the pages, or
2. a **successful** reprogram after the last time it refused to free (attempts
   do not count; `reset_fails` calls `reset_fn` and gets `False`, which leaves
   the fabric exactly as unsafe as before), or
3. inside a fail-stop hold that carries **the pipeline and its retained
   buffers** and never returns.

### 1.4 Mutation control

Each fix reverted **on its own** in a scratch copy, imported under the real
module name, suite re-run (`gate7_mutation_control.txt`):

| reverted | caught by |
|---|---|
| R5's abort guard | `test_an_unexpected_r5_result_still_closes_and_reprograms` (`_close_verdict is None`), and the invariant on `wedge_completes` |
| R4's abort guard | the invariant, on `r4_timeout` |
| `fail_stop`'s `say()` | `test_a_broken_stdout_cannot_skip_the_fail_stop_hold` |
| `final_reprogram`'s `say()` | `test_a_broken_stdout_cannot_skip_the_final_reprograms_verdict` |

`MUTATION CONTROL PASSED: every reverted fix is caught.`

**Gate 7 has NOT been re-run on silicon.** It needs board access, and until it
is re-run the 2026-08-22 gate-7 result must not be quoted as closing the
fail-open finding.

---

## 2. The threshold path was not memory-feasible on the board

`otsu_on_truncating_blur()` built the whole int32 blur and handed it to
`cv2.threshold`. On the first production page (9792×6336 at zoom 4):

| | bytes |
|---|---|
| BGR | 186,126,336 |
| grey | 62,042,112 |
| int32 blur | 248,039,440 |
| uint8 copy for `cv2.threshold` | 62,009,860 |
| **total, before NumPy temporaries** | **558,217,748** |

against roughly **290 MiB** of userspace once `cma=192M` is reserved out of
512 MiB. (The audit's 496,336,896 is the first three terms; the `astype`
copy adds a fourth.)

**Replaced with a streaming implementation**, and the number it produces is
required to be identical:

* `blur_stripes()` yields the blur a band at a time, bands overlapping by the
  two rows the 3×3 kernel needs, so a stripe boundary is computed from real
  neighbours rather than from a zero-padded edge.
* `blur_histogram()` accumulates the exact 256-bin histogram. 256 bins is safe
  because the kernel weights sum to 16, so the largest possible sum is
  255·16 = 4080 and `>> 4` returns it to 255.
* `otsu_from_histogram()` transcribes OpenCV's `getThreshVal_Otsu_8u`
  statement for statement — the running `mu1 *= q1`, the FLT_EPSILON guards on
  both tails, and the **strict `>`** that makes the first maximiser win a tie.
* `cpu_binary_like_core()` thresholds in the same stripes.

The stripe budget bounds **the returned blur stripe** at the production
width: 8.0 MB, 214 interior rows, and it does not grow with page height. It is
not a bound on the process, and must not be quoted as one — the page's own
grey and binary images dwarf it.

### Exactness (`otsu_exactness.txt`)

| population | result |
|---|---|
| 400 randomised images, each also checked at stripe heights 1, 2, 3, 7 and 10,000 | stripes reassemble to `truncating_blur` exactly; histograms identical; thresholds identical to `cv2` |
| 306 histogram cases, including flat, single-bin, two-bin, tied maxima, one-outlier and empty-tail | identical to `cv2` |
| the production page, 9792×6336 | threshold **162**, `cv2` **162** |
| all **36 corpus pages** (`otsu_corpus.txt`) | **0 disagreements** with `cv2` |

### What the exactness evidence does and does not prove

Five wrong transcriptions were run against the same populations
(`otsu_mutation_control.txt`). Two are **semantic** and are caught:

* the `>=` tie rule (last maximiser wins) — caught by 303 cases;
* dropping the degenerate-tail guards — caught by 1 case.

Three are **numeric** and no case separates them, on 706 synthetic histograms
or on any of the 36 real pages: `DBL_EPSILON` for `FLT_EPSILON`, float32
accumulators, and recomputing `mu1` rather than running it. A broader search
was abandoned deliberately: probing the epsilon constant needs histograms with
~10⁹ counts, and feeding one to `cv2` means materialising a 1 GB image per
case. **Those three are transcribed from OpenCV's source and are unfalsified,
not proven.** The shipped code uses Python floats (`double`), `FLT_EPSILON`,
and the running `mu1`, which is what the source does.

### BGR is no longer held across the PL work

`detect_page(keep_bgr=False)` drops the 186 MB BGR array as soon as grey is
taken. `annotate_page` only ever wanted `bgr.shape`, so `rendered_shape()`
derives it the way PyMuPDF sizes the pixmap — checked against the real pixmap
on **all 36 corpus pages**, and, whenever the real array is present, checked
against it at runtime and raised on. `--debug-images` defaults to **off for
`pl-*` backends**, because the debug PNG is the only thing that still needs
the array.

The annotated output is **byte-identical** with and without the array:
both PDFs rendered at 2x compare equal over 46,531,584 bytes of samples,
and the page counts match (male=20, female=18, unknown=1).

### The renderer was the larger half of the same problem

Streaming the blur left `render_page()` itself as the peak. On a 9792×6336
page it held four arrays at once:

| | bytes |
|---|---|
| the Pixmap's own buffer | 186,126,336 |
| the `pix.samples` **copy** | 186,126,336 |
| BGR | 186,126,336 |
| grey | 62,042,112 |
| **total** | **620,421,120** |

Dropping BGR after `render_page` *returns* does nothing about that: the peak
is inside it. Two changes, neither of which moves a byte of output.

**`pix.samples_mv` instead of `pix.samples`** — 186 MB of pure duplication.
Verified equal to `pix.samples` on every corpus page (`gray_parity.txt`).
PyMuPDF here is 1.28.0; `HAVE_SAMPLES_MV` records which path was taken, since
an older PyMuPDF on the board would silently pay the copy.

**`keep_bgr=False` never builds BGR.** The grey page is filled a band at a
time straight from the pixmap. Striping the **conversion** is exact by
construction — `RGB2BGR` is a channel swap and `BGR2GRAY` a weighted sum,
both strictly per-pixel — and `render_parity.txt` shows all six checks exact
on all **36 pages**: BGR, grey, `keep_bgr=False` grey, the Otsu threshold, and
the core-equivalent binary.

Peak inside `render_page(keep_bgr=False)` is the pixmap plus the grey page,
about **248 MB**, and the pixmap goes when it returns — 62 MB retained.
Again: a bound on that function, not on the process.

Silicon memory closure therefore requires the maximum page to complete the
whole pipeline, not merely `render_page()`: render, streamed binarization with
`page_bin` and `clean_bin` simultaneously live, PL extraction/matching,
geometry emission and safe teardown, followed by a small-page re-invocation.
RSS, `MemAvailable` and CMA availability must be recorded across those phases.

### Two cheaper routes were measured and REJECTED

Both were in the plan; neither is byte-identical, and the plan's step 3 turns
out not to preserve the arithmetic it was expected to.

* **Native grayscale** (`colorspace=fitz.csGRAY`) would have cost 124 MB —
  the cheapest of all. MuPDF rasterises *into* grey rather than converting
  afterwards, and its weights are not OpenCV's. Over all 36 corpus pages:
  **0/36 byte-identical**, up to **23 grey levels** apart, ~0.006% of pixels
  on most pages. The Otsu threshold happened not to move on any of the 36 —
  that is luck, not a guarantee, and the criterion is byte equality.
* **Striping the RENDER** with `clip=` tiles exactly in *geometry* — every
  band's irect came back exactly as asked — but not in *pixels*. MuPDF
  antialiases content against the clip edge, so the last ~15 rows of each band
  differ by up to 24 levels. Overlap margins of 16 and 64 rows still differed;
  256 was enough on the three pages tried, but there is no bound on how far a
  clipped object's coverage reaches, and "big enough so far" is not a
  correctness argument. A pixel-scissor render (`DeviceWrapper`) would be
  exact by construction, but it is an uninitialised internal in PyMuPDF 1.28.

Both are asserted as *still failing* in `test_pl_backends.py`, so neither can
quietly come back without the corpus comparison being re-run.

### Geometry out, drawing elsewhere

`--geometry-json PATH` writes the boxes, classes and page shape; `--no-annotate`
skips drawing into the PDF and saving it (the counts are still reported); and
`--from-geometry PATH` redraws the annotated PDF off-board from the source plus
the record, with no render, no detection and no backend. The redraw is
**byte-identical** to annotating directly — 46,531,584 bytes of rendered
samples compare equal — and a record whose shape does not match the PDF at
that zoom is refused rather than drawn in the wrong place.

`tme_backend_parity.py` passes `keep_bgr=False` too: it never draws anything,
so it was building and discarding both 186 MB arrays on every page.

---

## 3. Both integration runners were unsafe on timeout

`terminal_counter_endpoint_first.py` had no `try`/`finally` at all: a page that
raised skipped `backend.close()` entirely. `tme_backend_parity.py` turned
`close() == False` into a `SystemExit` — and exiting is precisely the release,
because the retained pages go back while the fabric may still target them.
Neither armed the signal blocks, so a SIGTERM or a closed notebook killed the
process mid-DMA before any `finally` could run.

Both now:

* call `safe_teardown.arm_teardown_protection()` **before the first transfer**
  (Ctrl-C stops working from there on; that is the documented trade);
* run `safe_teardown.teardown(backend.pl, overlay, status)` in a `finally`,
  which reprograms the PL from inside the process and fail-stops rather than
  returning if that fails;
* re-raise the original failure only **after** the teardown.

Asserted by `test_the_pdf_runner_tears_down_even_when_a_page_raises`: a page
that raises `TimeoutError` must still reach `teardown()` exactly once, must
have armed the signals, and must **not** have called `close()` directly.

`tme_backend_parity.py` also now builds each backend **once for the whole
run** rather than once per PDF: one overlay load, one teardown owner, and
call counters that cover the run being reported.

---

## 4. Build identity and live clock, before the first page

`inspect_overlay.gate_identity_and_clock(overlay, variant)` is split out of the
preflight so both runners gate on the same code rather than a drifting copy.
Both halves are needed:

* the VLNV alone cannot tell a B2 bitstream at 62.5 MHz from one at 100, and
  every performance figure is scaled by that;
* the clock alone cannot tell B2 from the baseline matcher — and for the
  100 MHz variants `fclk0 == 100.0` is **also PYNQ's power-on default**, which
  is why `_check_clock` gates `fclk1 == 125.0` too.

Both runners take `--variant`; a B2/100 run must pass
`--variant combined_b2_100`. A gate failure processes **no page** and still
tears down (`test_the_pdf_runner_refuses_a_board_running_the_wrong_build`).

---

## 5. Counts and ordering

**`trials_run` counted the wrong thing.** `PlSideBankMatcher` incremented once
per entry in `by_kind`, which holds at most one winner per class. Measured on
the Stage 2 page: it reported **132** where the matcher actually ran **1,200**
invocations — **9.1× low**, and that number is the divisor of every
wall-time-per-trial and cycle-model comparison. `match_candidate()` now returns
`"trials"` (the length of the selected list, after the legality and fit
filters) and the backend uses it. The two side-bank backends dispatch the same
trial list, so `cpu-sidebank` and `pl-all` must now report the same count —
which is what makes this checkable with no board.

**`cand_id` ordering is asserted.** §6.2 emits one record per input descriptor
in input order, and records are keyed by candidate object. A reordered batch
attaches candidate *i*'s patch to candidate *j*: every box after that is built
from the wrong origin, and each record is individually well formed, so nothing
else can notice. The core's own ordinal is the only field that can say so, and
`begin_page()` now checks it (`test_a_reordered_metadata_batch_is_refused`).

**The corpus is a pinned shape.** `--require-corpus` refuses to report unless
the run covered exactly **35 unique PDFs and 36 pages** — verified against the
sample directory, where exactly one PDF (`doc_001`) has two pages.
Globs are de-duplicated by resolved path first: on Windows `glob` matches
case-insensitively, so `"*.PDF" "*.pdf"` expanded to every file twice.

**`--assert-rung-c` no longer passes vacuously.** It exited 0 for
`--backends cpu cpu-sidebank`, having compared no silicon at all. A rung that
did not run cannot have passed.

---

## 6. The production-semantic oracle, and rung C in one run

### `cpu` cannot be the oracle

The audit's finding is confirmed by an independent run
(`corpus_cpu_oracle_gap.txt`): a direct `cpu` → `pl-all` comparison over the
whole corpus finds **7 of 36** pages identical under the ladder criterion —
28 pages at 0/1, 6 at 1/1, and the two-page PDF at 1/2. "35/36" was aggregate
per-class count equality, which two pages can satisfy with different boxes;
accepting it would waive detection-level parity on **29** pages. The reason is
arithmetic, not a fault: `cpu` binarises with `to_binary_inv` (rung A) and
searches per template base (rung B).

### `cpu-production`

A new diagnostic backend that closes both rungs on the host: the core's own
truncating blur and threshold choice, the PL's side-bank patch organisation,
the same trial order and tie rule, host refinement. The only thing left
between it and `pl-all` is which chip ran them, so the ladder gains a rung:

| rung | change | expectation |
|---|---|---|
| **P** `cpu-production` → `pl-all` | the fabric, production semantics held | **MUST be identical — the 36/36 requirement** |

### Off-board result on the full corpus (`corpus_parity_offboard.txt`)

`--fake-pl --backends cpu-production pl-all --require-corpus --assert-rung-c`,
exit **0**:

```
35 unique PDFs, 36 pages
rung P: 36/36 pages identical - exact (x,y) and |dscore| <= 0.005

cpu-production  pages=36  classify_calls=738  trials=20680  refine_calls=117
pl-all          pages=36  classify_calls=738  trials=20680  refine_calls=117
```

Every counter agrees. On the Stage 2 page alone both report threshold 162, 44
classification calls, 1,200 matcher invocations and 6 refinement calls, where
`cpu` → `pl-all` differs by 3 boxes and 6 scores (max |Δscore| 0.064609).

**This is a prediction, not a result.** The fake fabric is arithmetically exact
but is not silicon; what it establishes is that 36/36 on rung P is
*arithmetically reachable*, which 36/36 against the frozen `cpu` oracle is not.

### Rung C inside one run

Separate `pl-extract` and `pl-all` runs each re-render, re-binarise and
re-dispatch: nothing proves the two matchers received the same pixels.
`--rung-c-inline` runs the CPU reduction against the **same `patch` array** the
fabric just matched, from the same metadata record, inside the same candidate,
under the board PASS criterion. On the Stage 2 page: 44 candidates, 1,200 CPU
trials, 0 mismatches, max |Δscore| 0.000000. A one-pixel box drift injected
into the fake matcher is caught
(`test_the_inline_rung_c_catches_a_matcher_that_lies`).

### Call counts

`refine_calls=117` and `trials=20680` are **this run's** counts on the
PL-side-bank geometry. Neither the 808-call CPU trace nor the 968-call
PL-geometry trace applies; recount on silicon.

---

## 7. Off-board suite status

| suite | result |
|---|---|
| `test_board_gate_clock.py` | 12/12 |
| `test_board_gate_extract.py` | 14/14 |
| `test_board_gate_full_dma.py` | 13/13 |
| `test_board_gate_protocol.py` | 6/6 |
| `test_board_gate_recovery.py` | **18/18**, 12 injected defects |
| `test_board_preflight.py` | 47 checks, every injected defect detected |
| `test_cand_packing.py` | 9/9 |
| `test_driver_close.py` | 12/12 |
| `test_driver_state.py` | 11/11 |
| `test_gate_signals.py` | 6/6 |
| `test_pl_backends.py` | **39/39** (was 24) |
| `test_safe_teardown.py` | 23/23 |
| `test_binarize_dma_checks.py` | 8/8 — needs `PYTHONPATH=<repo root>`, pre-existing |

Three contract assertions were updated for the new `match_candidate` return
key (`board_gate_extract.py`, `board_gate_protocol.py`, `test_driver_state.py`)
— each now asserts `"trials": 0` rather than ignoring it.

---

## 7b. Stage 3 organizations, corrected

The initial workload is common to both organizations: 68 candidates, two
`[64, 4]` batches, 2,040 matcher trials, largest patch 622×300, and exactly
148,323,642,023 modeled B2 cycles = 1,483.2364 s at 100 MHz.

The refinement counts are not interchangeable:

| organization | refinement workload | valid interpretation |
|---|---:|---|
| live `cpu-production` / `pl-all` | 26 host calls / 208 correlations | production expectation; real Cortex-A9 time unmeasured |
| frozen `trace_20260818b` | 176 refinement records | retained CPU-trigger organization only |

Repricing exactly the 176 frozen records at `pl_side_bank`/B2 gives
13,283,542,750 cycles. Adding that diagnostic to the common initial total
gives 161,607,184,773 cycles = 1,616.0718 s. It does **not** price the live
production path's 208 correlations. The former quotient in
`stage3_refine.txt` divided quantities from different organizations and has
been deleted. Its regenerated transcript now prints both organizations
separately, and `stage3_cycles.txt` labels 161,607,184,773 as the frozen-trace
mixed diagnostic rather than the plan's refined total.

Refinement does not run on this RTL: `prefer_local_alignment` needs the argmax
of the anchor-adjusted *map* and `tme_top` returns only a scalar argmax of the
*raw* map. Stage 3 must compare measured **DMA/core** wall time against
**1,483.236 s**, measure the 26/208 ARM portion separately, and build the
per-page deadline from separately named PL, ARM, PS-overhead and margin terms.

## 8. Still open

* **Gate 7 on silicon.** Needs board access; not run.
* **Stage 2** (`doc_002`), **Stage 3** (`doc_003` page 1:
  68 candidates, 64+4 chunking, modelled 1483.236 s at 100 MHz, then
  immediately a small case), and **Stage 4** (the 36-page corpus at rung P).
* Conditions **6** and **7** of the completion decision remain **open**. The
  work here is an off-board prerequisite; it qualifies nothing.

## 9. Changed files

`sw/board_gate_recovery.py`, `sw/test_board_gate_recovery.py`,
`sw/pl_backends.py`, `sw/test_pl_backends.py`, `sw/tme_driver.py`,
`sw/terminal_counter_endpoint_first.py`, `sw/tme_backend_parity.py`,
`sw/inspect_overlay.py`, `sw/board_gate_extract.py`,
`sw/board_gate_protocol.py`, `sw/test_driver_state.py`,
`sw/BOARD_RUNBOOK.md`, and this directory.

Renderer and geometry work touched `sw/terminal_counter_endpoint_first.py`,
`sw/tme_backend_parity.py` and `sw/test_pl_backends.py` (now **45** tests).
