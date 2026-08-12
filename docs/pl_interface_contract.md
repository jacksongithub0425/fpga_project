# PL Interface Contract — Freeze v1

**Status:** proposed freeze. Sections marked **OPEN** need a decision before
implementation starts; everything else is settled and should be treated as
binding.

**Purpose.** Freeze the boundaries between the PL stages. The coordinate frame,
image interface, descriptor validity rules and result ABI are settled here. A
narrow three-stage C/golden harness now executes those decisions across
`binarize_core`, `patch_extract_core` and `template_match_core`: its generator
self-checks and Vitis HLS 2025.2 CSim pass. The combined block design, DMA
sequencing and silicon path remain implementation work.

**Scope.** `binarize_core` → DDR → `patch_extract_core` → `template_match_core`
→ `sw/tme_driver.py`. (`class_score_core` was removed from the MVP on
2026-08-11 — classification, the per-candidate reduction and box construction
are PS-side; see §5.1, §6.3, §6.4 and §10 items 4–5. Its sections are retained
as the design record for the contingency that re-instates a PL classifier.)

**How to use it.** Anything below is a contract term. If an implementation
disagrees with this document, the implementation is wrong — or this document
gets amended first, deliberately.

---

## 1. Coordinate frame

**Decision: DDR holds logical, detector-aligned coordinates. `binarize_core`'s
output scheduler owns the transformation and border fill; a simple-mode S2MM
stores that already-logical compact stream unchanged.**

Internally, the Gaussian/threshold result computed at raw `(r, c)` belongs at
logical `(r-1, c-1)`, valid only for `r >= 2, c >= 2`. Raw rows/columns 0 and 1
are pipeline fill and read 0.

The core's output scheduler consumes raw row 0 without output, discards raw
column 0, and emits raw `(r, c)` for `r >= 1, c >= 1` at logical
`(r-1, c-1)`. It appends one zero suffix to every mapped row and one all-zero
final row. The result is exactly `img_w * img_h` AXI-stream beats in compact
logical row-major order, with `TLAST` only on the final beat.

Consequences, which are the point of choosing this option:

- Logical rows `0 .. img_h-2` and columns `0 .. img_w-2` are filled directly.
  Logical row 0 and column 0 inherit the natural zeros from raw row 1 and
  column 1, which is already the correct border value.
- **Logical row `img_h-1` and column `img_w-1` have no raw 3×3 result** and are
  emitted as 0 by `binarize_core`. 0 means "no ink" under
  `THRESH_BINARY_INV`, so a border can never fabricate a feature. This fill is
  mandatory, not cosmetic.
- `patch_extract_core` does no coordinate correction at all. It reads logical
  coordinates from logical storage.
- Text-suppression rectangles keep using logical coordinates, unchanged.
- No `np.roll` over a ~60 MiB buffer on the CPU.

**Rejected alternatives, recorded so they are not revisited:** adding `+1` to
`build_endpoint_patch()` or to the descriptor endpoint. Both also shift
clipping behaviour and the reported boxes, which are detector outputs, not
storage details. Ownership belongs in the binarizer output scheduler at the
stream/storage boundary.

### 1.1 Golden model

An integrated binarize-to-extractor golden **must model the HLS arithmetic, not
OpenCV**. `binarize_core` computes `sum >> 4` — truncation — while
`cv2.GaussianBlur` rounds. Correcting the coordinate shift alone will not make
an OpenCV comparison bit-exact. Either the golden replicates the truncation
(as `binarize_generate_golden.py` already does) or the HLS rounding changes;
do not compare against stock OpenCV and apply a tolerance to paper over it.

**C/golden case passed under Vitis HLS 2025.2 CSim on 2026-08-09.**
`hls/integration/` runs a separate 24×20 case at threshold 140 through the live
C sources for `binarize_core` → `patch_extract_core` →
`template_match_core`. It checked all 480 compact logical raster bytes and
sidebands, the zero final row/column, the complete extractor metadata record
and 168-byte patch, then a raw 4×4 template match at score `+1.000000`, local
`(4,1)`, rebased to page `(7,5)`. The truncating Gaussian witness and legacy
raw-layout mutation also passed their controls. The generator passed in both
normal and `python -O` modes under NumPy 2.5.1 / OpenCV 5.0.0. Recorded result:

```
THREE-STAGE C/GOLDEN PASSED (0 errors): 480 gray beats -> 480 logical bytes -> 168 patch beats -> matcher local (4,1), page (7,5); 1 injected-layout control
SEAM TEST PASSED (0 errors): 4 descriptors, 3 matcher runs, 2 injected-bug controls
INTEGRATION C/GOLDEN PASSED: three-stage errors=0, seam errors=0
```

Vitis also reported `CSim done with 0 errors` (15 s CSim, 19 s total
`vitis-run`). This is C simulation with PS/DDR staging modelled between function
calls; the timings are host diagnostics, not throughput. It is not a combined
HLS top or RTL cosimulation, a direct stream between hardware cores, a combined
block design, a DMA execution or a silicon result. The pre-existing extractor
→ matcher seam remains a separate phase and retains its rejection, clipping,
non-compact stride and injected-bug coverage.

---

## 2. Image geometry and memory

**Decision: runtime images up to 9856 × 6400 with an explicit byte stride.**

| Quantity | Value |
|---|---|
| `img_w` | runtime, `3 <= img_w <= 9856` |
| `img_h` | runtime, `3 <= img_h <= 6400` |
| `stride_bytes` | runtime, `>= img_w` |
| `buffer_bytes` | `>= stride_bytes * img_h` |

- The extractor's input ABI is stride-general and **must not assume
  `stride_bytes == img_w`**. The old 2D array signature
  `bin_image[PE_MAX_IMG_H][PE_MAX_IMG_W]` hardcoded a 2560-byte stride; that is
  one of the things removed.
- The current direct binarizer → simple-mode S2MM path is specifically compact:
  `binarize_core` emits exactly `img_w * img_h` logical beats, S2MM writes them
  consecutively, and the extractor is programmed with
  `stride_bytes = img_w`, `buffer_bytes = img_w * img_h` for that buffer.
- A non-compact buffer remains legal extractor input, but it requires a
  producer that actually creates the row padding (for example a row-aware
  writer or a PS repack). The current simple S2MM is not such a producer.
  Rounding stride up to a 64-byte multiple is an efficiency recommendation only
  for those padded producers; it does not reinterpret a compact DMA stream.
- The lower bound is 3, not 2: a 3×3 kernel needs three rows and columns to
  produce any valid output at all.

### 2.1 Overflow rules for the memory validation

`stride_bytes * img_h` at the maxima is 9856 × 6400 = 63,078,400 — comfortably
inside 32 bits, but the *product must not be computed in a narrow type on
either side*. Rules:

- Compute `stride_bytes * img_h` in a **wider type than either operand**, then
  validate the result against all three of: `buffer_bytes`, the 32-bit linear
  offset range, and the platform AXI address range. A product that silently
  wraps passes every downstream check.
- `buffer_bytes` needs a **defined AXI-Lite register width**. It is currently
  undeclared; 32 bits is sufficient and should be stated rather than assumed.
- The validation must reject on overflow explicitly (reason bit 8), not rely
  on the wrapped value happening to fail a later comparison.

`suppress_text()` in the driver must become **stride-aware**. It currently does
`np.frombuffer(..., count=h*w).reshape(h, w)`, which assumes a compact buffer
and silently produces a skewed view the moment `stride_bytes > img_w` — every
row shifted progressively left, so text suppression zeroes the wrong pixels
with no error. Use a strided view (`as_strided` or a `reshape(h, stride)[:, :w]`
slice) built from `stride_bytes`.

### 2.2 Memory budget — **OPEN**

9856 × 6400 = 63,078,400 B ≈ **60.2 MiB** per full-size image buffer. The
requirement is **two separately contiguous ~60.2 MiB allocations, totalling
~120.3 MiB** — grayscale and binary are independent buffers and do not need to
be contiguous with each other. The driver today allocates `2560 * 3600` ≈
8.8 MiB each, so this is a 6.8× increase per buffer.

**Confirm the board's CMA can satisfy two allocations of that size before
implementing.**
If it cannot, tiling or streaming becomes a platform requirement and changes
the extractor's design again — which is why this is a gate, not a detail.

The alternative — keeping 2560 × 3600 — is not free either: it requires
deliberately lowering the render zoom and then regenerating and revalidating
every template, because template pixel dimensions are tied to render scale.
Neither option is a small change. Pick one explicitly.

---

## 3. Bit-width policy

Different quantities, different widths. Do not collapse them to one.

| Quantity | Width | Bound by |
|---|---|---|
| Page coordinate (`ep_x`, `ep_y`, `x0`, `y0`) | 16 bits | image, `<= 9856` |
| Patch width + column counter | 11 bits | matcher, `<= 820` |
| Patch height + row counter | 9 bits | matcher, `<= 307` |
| `stride_bytes`, linear DDR offset | 32 bits | `stride * img_h` fits |
| AXI physical address | platform AXI address width | |

The patch counters **shrink**, they do not grow — a patch is bounded by what
the matcher can hold, not by the page. Widening every counter to 14 bits would
give back the loop-counter timing already won and buy nothing.

This only holds once §4 validation is in place. Validating `max_tw <= 216` and
`max_th <= 96` makes `patch_w <= 820` and `patch_h <= 307` automatic, at which
point 11 and 9 bits are unreachable-by-construction safe. **Both have landed:**
`patch_extract_core` implements the §4 validation and its patch counters are
11/9-bit, so the previously noted 12-bit wrap is gone.

Two cautions on the numbers in the table above:

- **820 × 307 is the exact reachable envelope, and it is adopted.** It is not
  a round number and must not be rounded up: `max_tw <= 216` and
  `max_th <= 96` imply `patch_w <= 820` and `patch_h <= 307` exactly. The A/B
  synthesis showing why the difference matters **was not retained in the
  repository** — an earlier revision of this line cited
  `hls/template_match/ab_bram/`, which does not exist in the tree; the
  numbers below are the record, and re-running the A/B is the only way to
  re-derive them. At the former 1024 × 320 the matcher needed **352
  BRAM18K against 280 available — 125%, not implementable at all**; at
  820 × 307 it needs **224 (80%)**. `patch_buf` is 16 cyclic banks, and
  820 × 307 drops each below the 16 384-word depth step, halving it from 16 to
  8 BRAM18K per bank. That is a cliff, not a slope: an intermediate
  round-up saves nothing, so "leave some headroom" here costs the whole part.
  Three constants must move together —
  `tme_top.h` `MAX_PATCH_W/H`, `patch_extract_core.h` `PE_MAX_PATCH_W/H`, and
  `patch_extract_generate_golden.py` `MAX_PATCH_W/H`.
- The counter widths are unaffected (820 < 2048, 307 < 512), and the
  extractor's reported metrics did not move: 4.815 ns, 2 BRAM18K, 6 DSP,
  3386 FF, 4318 LUT. **That is not the same as unchanged RTL** — the
  comparison constants behind reason bits 5/6 went from 1024/320 to 820/307,
  so the extractor's behaviour on invalid descriptors genuinely differs. The
  metrics being identical says the change was free in area and timing, not
  that nothing changed. Re-verification (csim + cosim) is what covers the
  behaviour, and it is why the 217/97 cases were added to the cosim manifest.
- Narrowing the counters did not improve timing (§8). The widths are right on
  correctness grounds; do not cite them as a timing result.

### 3.1 The DMA transfer length is a second bound on the patch envelope

**`820 × 307 = 251,740` bytes must fit in one AXI DMA transfer, and on the
bring-up platform the ceiling is 262,143 bytes.** PYNQ reports
`buffer_max_size = 262143` for both DMA instances in the standalone extractor
image — `2^18 − 1`, set by the DMA's 18-bit buffer-length register
(`c_sg_length_width`). §5 gives every valid patch its own pixel `TLAST`, so one
patch is exactly one S2MM transfer, and the payload is `patch_w × patch_h`
bytes with no header. The current envelope therefore clears the bound with
**10,403 bytes (≈10.2 KiB) to spare** — a 4% margin, not a comfortable one.

This is recorded because it is invisible from the source. Nothing in
`patch_extract_core.h`, `tme_top.h` or the golden generators mentions it; it
lives in the block design's DMA configuration. **Anyone proposing a larger
`PE_MAX_PATCH_W × PE_MAX_PATCH_H` must check this bound alongside the three
constants §3 requires moving together.**

Two notes on how it interacts with what is already here:

- **It is a block-design parameter, not a law.** `c_sg_length_width` is
  configurable up to 26 bits (67,108,863 bytes). Raising it is a BD edit plus
  a re-implementation — cheap, but neither free nor invisible, and it must be
  a deliberate decision rather than something discovered after a patch
  silently truncates.
- **It very nearly coincides with the BRAM cliff §3 already describes.**
  `patch_buf` is 16 cyclic banks that stay at 8 BRAM18K each only while every
  bank is under the 16,384-word depth step — `16 × 16,384 = 262,144` bytes for
  the whole buffer, one byte above the DMA's 262,143. So the first envelope
  large enough to need a second DMA transfer is also, to within a byte, the
  first one that doubles the matcher's BRAM back toward the 125% that did not
  fit the part. The two bounds are effectively the same wall. That is a
  convenient accident, not a designed relationship — it does not survive a
  change to the bank count, so check both.

The metadata stream is nowhere near either bound: 16 bytes × `NUM_CANDS <= 64`
(§7.1.1 item 2) is 1,024 bytes per batch.

**How this bound is actually tested — the `hw` suite.** Until 2026-08-04 nothing
exercised it. The matcher's cosim manifest tops out at a 4,480-byte patch, so
every pre-silicon run verified arithmetic and left the transfer bound entirely
untested; and it cannot be tested in simulation even in principle, because an
RTL co-simulation contains no DMA and this is a DMA parameter.

`tme_generate_golden.py` therefore writes a third suite,
`tb_tme_cases_hw.txt` — the cosim cases plus `stress-max-envelope`, whose
820 × 307 patch is a single **251,740-byte** transfer, 10,403 bytes under the
ceiling. (It also carries `stress-max-result`, a second 820 × 307 patch under a
4 × 4 template — same transfer size, but it is there for §4.4's result-map
bound rather than this one; see §8.) `sw/tme_standalone_bringup.py` sends it to the board and prints the
headroom it actually observed. Three details make it evidence rather than
decoration:

- The stress case is **lifted from the already-solved csim case object**, not
  re-solved, so the board and csim run byte-identical pixels by construction.
- The bring-up script reads the DMA's **own** `buffer_max_size` rather than
  trusting the 262,143 above, and says so when the two disagree. The bound is
  a block-design parameter; a BD edit can move it without touching this repo.
- `build_tme_standalone.tcl` sets `c_sg_length_width = 18` **deliberately, to
  match the extractor image**. The point is to reproduce the platform's
  constraint, not to engineer around it.

Note what the suite still does not cover: it proves 251,740 bytes *fits*, not
that 262,144 bytes *fails*. The failing side is unreachable from software —
`validate_geometry()` rejects it before `ap_start`, which is the correct
behaviour and also means the truncation path can only be reached by
deliberately breaking the validator.

---

## 4. Descriptor validity

**Decision: reject, never crop.** Emit one metadata record per input
descriptor. Invalid descriptors produce metadata only — no pixel payload, no
DDR read, and nothing reaches the matcher.

### 4.1 Validation rules

Global image configuration:

```
3 <= img_w <= 9856
3 <= img_h <= 6400
stride_bytes  >= img_w
buffer_bytes  >= stride_bytes * img_h
```

Per descriptor:

```
side in {left, right}          # side > 1 is invalid, not "right"
ep_x < img_w
ep_y < img_h
4 <= max_tw <= 216
4 <= max_th <= 96
patch_w <= 820                 # post-clip
patch_h <= 307                 # post-clip
patch_w >= max_tw              # equality legal — §4.4 option 1 is ADOPTED
patch_h >= max_th
```

The minimum-size bump in `patch_extract_core` (`x1 <= x0+1 -> x1 = x0+2`) is
**correct and stays** — it is deterministic extractor robustness and it keeps
reads in range. The bug was never the bump; it was handing the resulting 2×2
patch to a matcher that cannot consume it. The `valid` bit is the barrier.

Two scope notes on the patch-versus-template checks, so their value is not
overstated:

- They are **not** what rejects the existing 2×2 regression case. That case has
  `max_tw = max_th = 0`, so the `[4,216]` / `[4,96]` bounds reject it first.
  The patch-versus-template checks earn their place on small images and on
  future clipped geometry, where a legal template meets a patch that clipping
  has shrunk below it.
- `patch_w <= 820` / `patch_h <= 307` are implied by the `max_tw` / `max_th`
  bounds. Kept as independent checks anyway — cheap, and a clipping or bump
  change that breaks the implication is exactly what they catch.

  **Bits 5/6 have never been reachable without bits 2/3**, before or after the
  §3 narrowing, and an earlier revision of this section said otherwise. Under
  the old 1024 × 320 the smallest overrunning template was `max_tw = 270`
  (→ 1026) and `max_th = 101` (→ 323); both already violated the 216/96 cap.
  What the narrowing changed is only *which* pair first co-fires: 270/101
  became **217/97**. Since 820 × 307 is the exact envelope, 216/96 now lands
  precisely on the bound and stays valid.

  The generator asserts this with **exact** reason words, not bit containment
  — an unexpected extra bit means the geometry drifted into another check, and
  a containment test would pass:

  | case | geometry | reason |
  |---|---|---|
  | `hi-boundary-820`, `cosim-max-legal` | 820 × 307 | `0x000`, valid |
  | `tw-217-first-illegal`, `cosim-tw-217`, `hi-overflow-823` | 823 × 307 | `0x024` (bits 2+5) |
  | `th-97-first-illegal`, `cosim-th-97`, `hi-overflow-h310` | 820 × 310 | `0x048` (bits 3+6) |

  The `cosim-` rows exist because **`run_hls.tcl` passes `-argv "cosim"`**,
  which selects the small cosim manifest — anything only in the csim manifest
  never reaches RTL. Bits 5/6 were csim-only until those rows were added.

### 4.4 Matcher result dimensions are off by one — **RESOLVED: option 1 adopted**

`tme_top.cpp` computes:

```c
int rw = pw - tw;   // result map width
int rh = ph - th;   // result map height
```

Full valid-correlation dimensions are `pw - tw + 1` and `ph - th + 1`. As
written, the matcher **omits the final possible row and column** of every
search, and `pw == tw` yields zero search positions rather than the one
position that exists.

`rw` is computed **twice, in two files**, and both are wrong the same way:

```c
tme_top.cpp:70          int rw = pw - tw;   // result map width
correlation_core.cpp:31 int rw = pw - tw;
```

`correlation_core` uses its own `rw` for the `u0 >= rw` tile break and the
`u < rw` writeback guard, so fixing only `tme_top` would leave the correlator
writing one column short of what the normalizer then reads — a fresh defect
rather than a partial fix. **Both must change together**, along with `rh` in
`tme_top`.

Pick one, and note the choice propagates into §4.1:

1. **Fix the matcher**: use `+ 1` in *both* `tme_top.cpp:70` and
   `correlation_core.cpp:31`, update `MAX_RESULT_W` / `MAX_RESULT_H`, and allow
   patch/template equality. §4.1 then relaxes to `patch_w >= max_tw`.
   New maxima, taking the minimum legal template from §4.1 (`max_tw`,
   `max_th >= 4`):

   ```c
   MAX_RESULT_W = MAX_PATCH_W - 4 + 1   // 1021 when MAX_PATCH_W was 1024
   MAX_RESULT_H = MAX_PATCH_H - 4 + 1   //  317 when MAX_PATCH_H was  320
   ```

   These grow `num_acc[]` and `isq_col[]` by one element each and widen the
   `isq_slide` tripcount, so the matcher needs its own re-synthesis and
   re-verification after the change — it is not a comment-level edit.
   (The *formula* is the contract term, not the numbers. §3 has since adopted
   the exact envelope 820 × 307, so the current values are **817 / 304**.)
2. **Require strict inequality**: keep the matcher as-is and enforce
   `patch_w > max_tw`, `patch_h > max_th`.

**Option 2 does not fix the defect** — it only guarantees at least one search
position exists. The last row and column of the correlation surface stay
unreachable either way, so a match at the extreme edge of a patch is still
missed. Option 1 is the real fix; option 2 is containment.

**Decision: option 1 is adopted and implemented.** Both `tme_top.cpp` and
`correlation_core.cpp` now compute `pw - tw + 1` / `ph - th + 1`, and
`MAX_RESULT_W`/`MAX_RESULT_H` are **817/304** (they were 1021/317 until §3
adopted the exact 820 × 307 patch envelope; both are `MAX_PATCH − 4 + 1` and
follow automatically). §4.1 above encodes the relaxed
`>=` accordingly. The matcher's golden (`cv2.matchTemplate`) already searched
the full surface, so no golden regeneration is required — the fix strictly
widens the DUT's search toward what the golden always modelled.

### 4.5 `max_tw` / `max_th` must come from the post-round template dimensions

Patch sizing truncates (`int(base * scale)`) while the actual template
dimensions are produced with `int(round(base * scale))`. At half-integer
products the descriptor **underestimates the real template by one pixel**,
which then feeds every bound in §4.1 and §4.4 with a value one short.

Define `max_tw` / `max_th` from the exact post-round dimensions of the
template that is actually transmitted. The descriptor must describe the real
template, not a re-derivation of it.

### 4.2 Reason bitmask

```
bit 0   ep_x >= img_w
bit 1   ep_y >= img_h
bit 2   max_tw outside [4, 216]
bit 3   max_th outside [4, 96]
bit 4   side not in {0, 1}
bit 5   patch_w > 820
bit 6   patch_h > 307
bit 7   patch smaller than template after clipping
bit 8   global image configuration invalid
```

Bits accumulate; a descriptor may set several. These positions are numbered
within `reason` itself; on the wire it sits at `status[15:1]` (§6.2), so
reason bit *n* is `status` bit *n+1*.

### 4.3 Globally invalid image configuration

If the image configuration itself fails validation, the core must:

1. consume **all** `NUM_CANDS` descriptors,
2. emit invalid metadata (reason bit 8) for each,
3. perform **no** DDR pixel reads,
4. latch a batch-level error flag and a rejected count in the status register,
5. complete normally — assert `ap_done`.

Returning early is specifically forbidden: it strands the feeder with
descriptors still queued and backpressures the batch. "Fails cleanly" here
means "drains the stream and reports", not "stops".

This is also the current silent-failure path worth naming: with `img_w = 0`
today, every candidate clips to a 4-beat read of `bin_image[0][0..1]` and the
core reports normal completion. That must become a reported rejection.

### 4.6 Flat templates are illegal input — `dt == 0` is an ABI violation

**Decision: a template with no pixel variation must be rejected by host
software before the first DMA transfer. The matcher's per-window zero for
`dt == 0` is a defensive fallback, not a contract term, and nothing may depend
on the value it produces.**

Notation — all exact integers over one template-sized window of `N = tw · th`
8-bit pixels, as computed in `tme_top.cpp`:

```
dt  = N·ΣT²  − (ΣT)²      template variance term, = N²·Var(T)
di  = N·ΣI²  − (ΣI)²      window   variance term, = N²·Var(I)
num = N·ΣTI  − ΣT·ΣI      covariance term
```

#### The three domains

| domain | meaning | contract |
|---|---|---|
| `dt > 0 && di > 0` | ordinary case | the mathematical TM_CCOEFF_NORMED expression `num / √(dt·di)` |
| `di == 0` | **legal** — a flat search window inside a legal patch | score is **+0.0**; this *is* a contract term |
| `dt == 0` | **illegal** — a flat template | rejected by the ABI before dispatch; the DUT's `0.0f` is a defensive fallback only |

A flat *window* is ordinary input — `blank-patch` and `half-blank-peak` in
`tme_generate_golden.py` are built from them, and OpenCV agrees, since a zero
denominator falls through its clamp ladder to `num = 0`. A flat *template* is
not input at all: it carries no information any matcher can use, the two
implementations disagree about what to return for it, and OpenCV does not even
agree with itself — see the next section.

#### OpenCV's epsilon test is *not* exactly `dt == 0`

OpenCV's `common_matchTemplate` (`modules/imgproc/src/templmatch.cpp`) takes an
early return *before* any correlation is computed:

```cpp
templNorm = templSdv[0]*templSdv[0] + ... ;        // summed population variance
if( templNorm < DBL_EPSILON && method == CV_TM_CCOEFF_NORMED )
{
    result = Scalar::all(1);                       // ONES, not zeros
    return;
}
```

`templSdv` comes from `meanStdDev`, whose divisor is `N` and not `N − 1`, so
the quantity `templNorm` *represents* is the population variance, `dt / N²`.
It is not *computed* that way. On the CPU uint8 path `meanStdDev` accumulates
`ΣT` and `ΣT²` in **integer** block accumulators — those are exact — and only
then scales in `double`, forming `ΣT²/N − (ΣT/N)²`: a difference of two nearly
equal doubles. For a flat template the exact answer is 0 while the computed
answer is whatever that cancellation leaves behind, neither necessarily 0 nor
necessarily below `DBL_EPSILON`. `matchTemplate` consumes that result and then
takes the epsilon branch above.

The equivalence therefore holds in one direction only:

```
template is flat   ⇔   dt == 0                        exact, over integers
dt >  0            ⇒   templNorm  ≫  DBL_EPSILON      proved below, CPU uint8 path
dt == 0            ⇏   templNorm  <  DBL_EPSILON      FAILS
```

**A measured counterexample.** Against the OpenCV installed in `hls/.venv`
(5.0.0), a 7 × 7 template filled with 2 has exact `dt = 0`, and:

```
cv2.meanStdDev  →  templNorm = 4.440892098500627e-16
DBL_EPSILON     =              2.220446049250313e-16
```

It misses the early return, so OpenCV goes on to correlate a zero-variance
template and returns something other than ones. **What it returns depends on
the patch, and on the dispatch**, so a number is only meaningful with both
attached. Against a 10 × 10 patch cut from a 16 × 16 ramp (`p[r][c] = 16r + c`):

| flat 7 × 7 template | generic path | IPP path |
|---|---|---|
| all-2 | peak 5.494174e−08, min 2.747087e−08 | identical |
| all-127 | peak 5.494174e−08, **min 0.0** | **entire map exactly 0.0** |

Two things fall out of that table. Exactly `0.0` is on the menu — under IPP the
whole result map for an all-127 template *is* zero, and even on the generic path
zero appears in it. And identical inputs give different results depending on
which implementation OpenCV dispatched to, which is why the oracle pins the path
rather than trusting whichever one the build happens to use. None of these
figures generalises to another patch; that is the point.

This is not one unlucky size either: at 7 × 7, **157 of the 256 possible flat
fills** miss the branch; at 40 × 30, 24 of 256; at 4 × 4, 8 × 8 and 216 × 96,
none do (generic path, IPP disabled — see below). Whether a mathematically flat
template reaches the branch depends on the rounding of one subtraction inside
`meanStdDev` — not on anything the caller can inspect, and not on `N` in any
pattern worth memorising.

Two consequences, both load-bearing:

- **There is no single answer OpenCV gives for a flat template.** It fills the
  result with ones when the early return fires (the mechanism is visible in the
  source above; the historical behaviour is discussed in OpenCV issue #5688),
  and otherwise correlates normally and returns a value that depends on the
  patch **and** on which implementation it dispatched to — which, as the table
  above shows, includes an entire result map of exactly 0.0. So the DUT's `0.0`
  can coincide with OpenCV's, and that coincidence means nothing. Any claim that
  "cv2 emits exactly 0" for a flat template is false; so is any claim that it
  reliably emits 1.0; and so is any claim of agreement built on a matching zero.
- **This makes host-side rejection more valuable, not less.** `min(templ) ==
  max(templ)` decides on the integers themselves: it rejects every flat
  template and nothing else, with no dependence on floating-point luck.
  Deferring to OpenCV's epsilon test would inherit a branch a flat template may
  or may not take.

#### The direction that does hold: no legal non-flat template is mistaken for flat

The `⇒` above is the one the pipeline depends on — a template the host accepts
must never be one OpenCV takes the early return on, or the two would be scoring
different sets of inputs. That direction has eleven orders of margin:

- `dt = ½ ΣᵢΣⱼ (Tᵢ − Tⱼ)²`. Take `v` to be a most-common pixel value and
  `k = #{i : Tᵢ ≠ v}`; a non-flat template has `1 ≤ k ≤ N − 1`. Every one of the
  `k·(N − k)` cross pairs contributes at least 1, because the pixels are
  integers and differ. So `dt ≥ k·(N − k) ≥ N − 1`, and the bound is attained
  (`k = 1`, `δ = 1`). The minimum **nonzero** `dt` at fixed `N` is exactly
  `N − 1`; brute force over small `N` agrees.
- The smallest legal template is 4 × 4 (§4.1), so the **global** legal minimum
  nonzero `dt` is **15**, at `N = 16`.
- The minimum nonzero *population* variance is `(N − 1)/N²`, which falls as `N`
  grows, so its floor sits at the largest legal template, `N = 20,736`:
  **4.822298 × 10⁻⁵**.

That floor is 2.17 × 10¹¹ times `DBL_EPSILON` (2.220446 × 10⁻¹⁶). What makes
this a *proof* and not five lucky measurements is that the roundoff can be
bounded — **on OpenCV's generic C path, `uint8` input, `binary64` scaling**:

- `ΣT ≤ 20,736 · 255 = 5,287,680` and `ΣT² ≤ 20,736 · 255² = 1,348,358,400`.
  Both are accumulated in integers and both are far under `2⁵³`, so both are
  **exact** — there is no accumulation error to bound, only scaling error.
- `templNorm` is `fl(ΣT²·s) − fl(fl(ΣT·s)²)` with `s = fl(1/N)`, and then
  `common_matchTemplate` takes the `sqrt` (`templSdv`) and squares it back.
  Every term is bounded by `max(T)² = 65,025`. Counting conservatively — two
  roundings into `ΣT²·s`, three into `(ΣT·s)²`, one on the difference, and the
  `sqrt`/square round trip — gives about `11u · 65,025 ≈ 7.94 × 10⁻¹¹` with
  `u = 2⁻⁵³ ≈ 1.11 × 10⁻¹⁶`. Call it **under 10⁻¹⁰**. (Omitting the `sqrt` and
  its squaring would give `8u ≈ 5.8 × 10⁻¹¹` — the same conclusion, but it is
  not the whole path.)
- `10⁻¹⁰` against a legal floor of `4.822298 × 10⁻⁵` leaves five orders of
  margin, so a legal non-flat template cannot be pushed under `DBL_EPSILON`
  (eleven orders further down) by any amount of rounding on this path.

**The bound only describes code that runs.** The OpenCV installed here (5.0.0)
reports `ippIP AVX2`, and an IPP dispatch need not execute the generic source
the derivation follows. This is not hypothetical: the all-127 row of the table
above differs between the two paths on identical inputs. So
`tme_generate_golden.py` calls `cv2.ipp.setUseIPP(False)` and **verifies
`useIPP()` came back false** before the cross-check oracle or the epsilon
self-test runs anything — the asserted numbers are then measurements of the path
the proof is about. The same self-test re-runs with IPP re-enabled and prints
those figures as **measurement only**; they are not covered by any bound here.
If the disable ever fails, the self-test says so and downgrades its own language
rather than keeping the word "proved".

(For the record, on this build the *non-flat* measurements are identical under
both dispatches — it is the flat, out-of-contract side where they diverge. That
is luck, not a property to rely on: the pinning is what makes the assertion mean
something.)

On the generic path the measured error at 4 × 4, 7 × 7, 40 × 30, 212 × 87 and
216 × 96 peaks at 1.9 × 10⁻¹², about 40 × inside the bound. **No legal non-flat
uint8 template comes near OpenCV's epsilon test.**

The rest of the pinning still matters: this is an argument about `uint8` inputs
summed exactly and scaled once in `binary64`. A `float32` intermediate, a fused
reduction with a different association, or a non-CPU backend (OpenCL/CUDA) would
need it re-derived. The asymmetry is the point: exact integers cannot be fooled
in either direction, double-precision cancellation only in the flat one — where
nothing is scored anyway.

#### Width bounds

The extremes are two-valued at the pixel limits `B = 0`, `V = 255`, split as
evenly as `N` allows, so `max(dt) = ⌊N²/4⌋ · (V − B)²`. At `N = 20,736`:

| quantity | maximum | width |
|---|---|---|
| `dt`, `di` | 6,989,889,945,600 | **43 bits** unsigned |
| `dt · di` | 48,858,561,451,599,970,959,360,000 | **86 bits** unsigned, 87 signed |
| `num` | ±6,989,889,945,600 | 43 bits + sign |

`43 + 43 = 86`: the operand-width bound and the exact product bound **agree**,
so nothing is lost by reasoning in widths rather than exact extremes. Note that
`dt · di` is a bound on the *mathematics*, not on a signal — `tme_top`
evaluates `dt_f * (float)di` in float, so there is no 86-bit datapath to look
for. `|num| ≤ √(dt·di) ≤ max(dt)` by Cauchy–Schwarz.

**The construction intermediates are wider than the results.** `dt` is 43 bits
and `num` is 43 bits plus sign, but no term either is built from is that narrow:

```
N·ΣT²  ≤ 20,736 · 1,348,358,400 = 27,959,559,782,400    45 bits
(ΣT)²  =         5,287,680²     = 27,959,559,782,400    45 bits
N·ΣTI, ΣT·ΣI     same bound     = 27,959,559,782,400    45 bits
```

The first two are *equal* at the extreme (an all-255 template) and cancel to
zero. That is what makes the intermediates worth naming — but it does **not**
mean the types must be wide enough to hold them.

#### What the widths actually require

Fixed-width two's-complement subtraction is modular:

```
(A mod 2^W  −  B mod 2^W)  mod 2^W   =   (A − B) mod 2^W
```

So truncating both 45-bit operands to a common width `W` and subtracting loses
nothing, **provided the result is representable in `W` bits**. The result width
binds, not the operand width; the wrap cancels. Since
`0 ≤ dt ≤ 6,989,889,945,600 < 2^43` and `|num| ≤ 6,989,889,945,600 < 2^43`:

the **joint minimum is `(wide_t, num_t) = (44 unsigned, 44 signed)`**. That is
one statement about a pair, not two independent budgets, because `num` is
written `(num_t)(wide_t)(...)`: the inner cast truncates *before* the
subtraction, so the wrap cancels only if the result is reduced modulo the same
power of two the operands were.

| `(wide_t, num_t)` | `dt` | `num` | why |
|---|---|---|---|
| **(48, 48)** — implemented | ✓ | ✓ | no operand ever wraps; nothing to argue about |
| **(44, 44)** — joint minimum | ✓ | ✓ | operands reduced mod 2⁴⁴, result reduced mod 2⁴⁴ — the wrap cancels |
| (43, 44) | ✓ | ✗ | operands reduced mod 2⁴³, then zero-extended into 44-bit signed arithmetic that never reduces them back |
| (44, 45) | ✓ | ✗ | operands reduced mod 2⁴⁴, result kept in 45 bits — a wrapped operand stays wrapped |

**Widening one of them is not automatically safe.** `(44, 45)` is the
counter-intuitive row: it gives `num` *more* bits than the minimum and still
gets the wrong answer, because the truncation happens before the wider signed
subtraction. So the rule is **not** "`wide_t ≥ 44` and `num_t ≥ 44`":

> Use **equal modular widths** — `wide_t` and `num_t` the same `W ≥ 44` —
> **or** preserve the 45-bit operands outright (`≥ 45` unsigned / `≥ 46`
> signed, which `48/48` does). Anything in between needs the argument redone.

A probe compiled against Vitis 2025.2's own `ap_int.h` puts the equal-width
boundary exactly at 44: 43 wrong, 44 / 45 / 46 correct. 43 fails for the plain
reason — `|num|` reaches 6,989,889,945,600 and a 43-bit signed value stops at
`2^42 − 1`. `tme_tb.cpp::bound_case` runs both failing configurations as
executable witnesses, so the table above is tested rather than asserted:

- **(43, 44)** at the positive maximum: `num` = −1,806,203,076,608 instead of
  +6,989,889,945,600, while `dt` stays correct — which is exactly why checking
  `dt` alone would pass this change through.
- **(44, 45)** on an *ordinary legal input* — a 216 × 96 template with 14,000 of
  its 20,736 pixels at 255, matched against itself: `num` =
  −11,460,068,444,416 instead of +6,132,117,600,000. Nothing extreme is
  required to trip it; it is not a corner case.

**48 / 48 stays.** Not because 44 is unsafe — the pair is provably and
measurably sufficient — but because 48 lands in the "preserve the operands
outright" case, where the modular argument is not load-bearing at all: nobody
reading `dt` or `num` has to reconstruct it to believe the result. Narrowing
would cost a resynthesis, a fresh cosim and a rebuilt bitstream to save nothing
anyone has asked for. Treat the widths as a preservation policy, not as a
correctness cliff — and treat any edit to *either* type as an edit to both.

#### Agreement with OpenCV, stated precisely

For `dt > 0` and `di > 0`, the core evaluates the mathematical
TM_CCOEFF_NORMED expression using exact integer sufficient statistics followed
by float normalisation. OpenCV uses a different numerical path (float64
integral images, plus an explicit near-boundary clamp that snaps any `|num|`
within 12.5 % of the denominator to ±1), so **agreement is tolerance-based
rather than bit-exact**. This is not a claim that the core is universally more
accurate, nor that the tolerance covers only OpenCV's error. Mismatches are
adjudicated against the independent high-precision oracle in
`tme_generate_golden.py`, which is neither implementation.

#### Who enforces this

`tme_top` has no validation path, no reason bitmask and no status register
(§7.1), so rejection lives entirely in host software:

- `sw/tme_standalone_bringup.py::validate_template_content()` — runs **after**
  the final resize / binarisation / cropping, on the same `bytes` object that is
  then handed to the DMA. It accepts **`bytes` only**, deliberately including
  not a read-only `memoryview`: that flag says *this view* cannot write, not
  that the memory is immutable (a read-only view over a `bytearray` aliases a
  buffer that can still change under it), and a multi-dimensional view would
  make `len()` count rows instead of bytes and raise out of `min()`/`max()`
  before any rule was applied. It requires
  `len(templ) == templ_w · templ_h`, rejects an empty buffer, and rejects
  `min(templ) == max(templ)`. The whole manifest / template bank is validated
  **before the first DMA transfer or `ap_start`**, so one flat entry rejects the
  batch instead of being discovered five cases in. The same check is repeated
  inside `run_case()` for direct callers.
- `hls/template_match/tme_generate_golden.py::score_map()` raises `ValueError`
  — not `assert` — on `dt == 0`, so the generator's rejection survives
  `python -O`. Both the generator and the bring-up validator are run under
  `python` and `python -O` as part of accepting a change here.

What tests this, and where:

| check | where |
|---|---|
| flat 4×4 templates (all 0 / 127 / 255) rejected — **on exact integer `dt`, before cv2 is consulted at all** | `tme_standalone_bringup.py --selftest`, `tme_generate_golden.py::selftest_flat_template_rejected` |
| the surviving OpenCV direction, **asserted**: minimum-nonflat templates at 4×4, 7×7, 40×30, 212×87, 216×96 land above `DBL_EPSILON` and within the 1e-10 roundoff bound of the exact population variance | `tme_generate_golden.py::selftest_opencv_epsilon` |
| the flat side of the asymmetry — how many of the 256 fills miss the epsilon branch at each size, and what a flat template scores against the named ramp patch under **both** dispatches — **measured and printed, never asserted**, because §4.6 depends on none of it; printing it makes a cv2 version bump visible instead of silently invalidating the numbers quoted above | `tme_generate_golden.py::selftest_opencv_epsilon` |
| IPP is disabled **and the disable verified** before either the cross-check oracle or the asserted checks run; the self-test downgrades its own wording if it cannot be | `tme_generate_golden.py::use_generic_opencv` |
| padded / short / empty template buffer rejected | `tme_standalone_bringup.py --selftest` |
| non-`bytes` buffer rejected — `bytearray`, read-only `memoryview` over a `bytearray`, 2-D `memoryview` | `tme_standalone_bringup.py --selftest` |
| minimally non-flat template (`dt = 15`) **accepted** | both self-tests — the guard against turning `min == max` into a threshold |
| mixed bank, one flat entry → the abort **condition** is non-empty. The ordering — that nothing is dispatched before the bank is judged — is `main()`'s statement order and is **not** instrumented by any self-test; it would take a mocked driver or a board run | `tme_standalone_bringup.py --selftest` |
| DUT on a flat template: raw `0x00000000` at (0,0), both streams drained | `tme_tb.cpp::run_direct_tests` |
| DUT on the `dt = 15` template: raw `0x3F800000` at (0,0) | `tme_tb.cpp::run_direct_tests` |
| `max(dt) = 6,989,889,945,600`, the ±`num` extremes, the all-255 cancellation, and **both** width witnesses — (43u, 44s) and (44u, 45s) — in the DUT's own types | `tme_tb.cpp::bound_case` |

The two DUT tests run in **every** suite, before the manifest loop, so they are
covered by RTL cosim as well as C simulation. They cannot be manifest cases:
the generator refuses to write a golden for a flat template.

`patch_extract_core::prevalidate()` is **not** affected by that `python -O`
concern: it is C++ and already uses explicit checks with error counting. The
word "asserts" elsewhere in §4 is descriptive, not a reference to Python
`assert`. A repo-wide audit for validation written as `assert` is still worth
doing on its own merits.

#### RTL

**No RTL change is required to close this section.** The per-window `dt == 0`
fallback in `norm_cols` stays: it costs nothing and it keeps an out-of-contract
input from producing a NaN. An early return after the template load would also
be structurally safe — the core carries no `DATAFLOW` pragma and both stream
loads are sequential, so returning after both completes every stream read — but
it buys nothing and would cost a resynthesis, a fresh cosim and a rebuilt
bitstream. **The current `.bit`/`.hwh` stay valid**, and that was checked
rather than assumed — the re-synthesis run on 2026-08-05 was compared against
the RTL `package_provisional.tcl` built on 2026-08-04 from the same sources,
part and 5 ns clock:

| artifact | result |
|---|---|
| `tme_top_CTRL_s_axi.v` | **byte-identical** |
| generated `xtme_top_hw.h` | **byte-identical** — `0x30` / `0x40` / `0x50` unmoved |
| resources | 224 BRAM18K, 33 DSP, 18,247 FF, 34,573 LUT — unchanged |
| timing estimate | 6.547 ns against the 5 ns target — unchanged |
| other `syn/verilog` files | differ **only** in HLS's `_ln<source-line>` identifiers and `VITIS_LOOP_<line>_n` module names, shifted by exactly the number of comment lines added (+16 before `tme_top.cpp:41`, +22 after `:122`); normalising those away leaves an identical multiset of statements. The reordering observed is across separate `always @(posedge)` blocks, across continuous `assign`s, and among nonblocking assignments **whose destinations are distinct** — none of those carry ordering semantics |

A second full `run_hls.tcl` on the main project the same evening (23:20–23:38,
after the §4.6 comment corrections landed in `tme_top.cpp` / `tme_top.h`)
reproduced this independently: **224 BRAM18K, 33 DSP, 18,247 FF, 34,573 LUT and
6.547 ns**, with csim 23/23, `-argv "hw"` 9/9 and RTL cosim 7/7. Two rounds of
comment-only edits, same numbers twice.

That last row is the thing to expect from any comment-only edit to
`tme_top.cpp`: the netlist is unchanged but the generated names are not, so
"the RTL files are byte-identical" is the wrong acceptance test. Compare the
CTRL slave, the register map and the resource/timing numbers, and normalise
`_ln<N>` before diffing anything else.

One caveat for the next person who repeats this comparison: nonblocking-assignment
order **is** significant when two assignments in the same `always` block target
the *same* register — the last one wins. That case does not occur in this diff,
which is why the reordering is benign here. It has to be checked, not assumed.

Closing this section did **not** close §6.3 (the 14-versus-16-byte result
record) or §7.1 item 4 (timeout / reset ownership). **§6.3 has since been
closed separately — 2026-08-11, by deleting the record from the MVP ABI
rather than resizing it (§10 item 5). §7.1 item 4 (= §10 item 6) is the one
that remains open.**

---

## 5. Framing

```
NUM_CANDS              AXI-Lite register; authoritative batch count
candidate TLAST        derived from NUM_CANDS by the feeder
patch metadata TLAST   asserted on the last metadata record of the batch
pixel TLAST            asserted at the end of each VALID patch
invalid candidate      metadata record only; no pixel beats
```

`NUM_CANDS` is the authority and `TLAST` is derived from it, not the reverse.
The core it replaced looped on a blocking `cand_in.read()` until `TLAST`, so a
descriptor stream that never asserted it hung the core with `ap_done` never
rising and no recovery short of a PL reset. With a count register the core
knows when to stop regardless, and `TLAST` becomes a cross-check reported as
an error rather than a deadlock.

**Implemented, and one consequence is load-bearing.** `patch_extract_core`
reads **exactly `NUM_CANDS` descriptors, always**, and compares each beat's
`TLAST` against the position it must occupy:

```
mismatch |= (cand.last != (i == NUM_CANDS - 1))
```

This catches an early marker and a missing final marker without ever changing
how many beats are consumed. An early `TLAST` specifically must **not** be
treated as end-of-stream: `TLAST` is a wire bit, not a beat count, so a feeder
can deliver all `NUM_CANDS` descriptors and still misplace it. Stopping early
would leave real descriptors queued in the DMA for the *next* invocation to
read as its own batch — silent cross-batch corruption, reported only as a flag
on the run that caused it.

The trade this accepts: a feeder delivering **fewer** than `NUM_CANDS` beats
now blocks in `cand_in.read()` with `ap_done` low, rather than completing with
filler. That is deliberate — a short stream is a feeder fault by construction,
since §5 has the feeder derive `TLAST` from the same count register, and a
visible timeout beats a batch that completes with plausible-looking data. It
does create an obligation this document has not yet assigned: **something must
own the short-stream timeout and the PL reset that recovers from it.** Assign
it when the feeder is specified (see §8).

Pixel `TLAST` moves from once-per-batch to once-per-valid-patch. This is what
lets the matcher frame patches without the PS having to recompute the clamped
geometry independently — the duplicated-clamping-ladder hazard disappears
because the geometry is transmitted, not re-derived. It also makes each patch
exactly one DMA transfer on the PS side, which is where §3.1's 262,143-byte
transfer bound attaches.

### 5.1 Score-stream framing — **CLOSED for the MVP 2026-08-11: no PL consumer**

> **Decision (2026-08-11).** `class_score_core` is removed from the MVP, so
> the matcher→classifier stream this section frames does not exist in the
> MVP: the PS sequences one matcher invocation per (candidate, template,
> scale) trial and reduces the scores itself, holding a running per-kind
> argmax under strictly-greater comparison over a frozen trial order (§6.4
> option 1, extended to the whole reduction). Everything below is retained
> unchanged as the design record this decision was made against, and becomes
> binding again only under §6.4's standing condition (a matcher that iterates
> templates internally). Re-open only if a benchmark of the completed PS
> classification identifies it as a meaningful bottleneck.

The above frames extractor→matcher. It says nothing about matcher→classifier.
That gap is real and worth closing, but an earlier version of this section
overstated it: it claimed D1/D2 in `class_score_core` could not be repaired
until the framing was specified, on the grounds that inferring boundaries from
`cand_id` transitions is a guess. **That diagnosis was wrong on both halves.**

- **Inference from `cand_id` is sufficient here.** The matcher emits all tuples
  for a candidate before the next candidate's, in ascending `cand_id` order,
  and the batch ends with `TLAST`. An ordered-`cand_id` change therefore
  identifies an interior boundary unambiguously, and batch `TLAST` identifies
  the final one. There is no case where "next candidate" and "same candidate,
  batch ended" are confusable, because the second is not a boundary at all.
- **D1/D2 are ordinary sequencing bugs.** The defect is that the incoming
  tuple is merged into `best_score[]` *before* the previous candidate is
  flushed, and that the flushed record is then labelled with the triggering
  tuple's `cand_id`. Both are fixed by reordering the loop body, with no
  change to how boundaries are detected and no dependency on this section.
  See the D1/D2 entries in `class_score_core.cpp`.

  **But the reordering is not uniform, and an earlier revision of this bullet
  said it was.** "Flush, then merge" is right for a *candidate change* and
  wrong for *batch `TLAST`*, because the two boundaries stand in opposite
  relations to the tuple that triggers them:

  | boundary | the triggering tuple belongs to | correct order |
  |---|---|---|
  | `cand_id` changed | the **next** candidate | flush previous (labelled with the previous `cand_id`), reset `best_score[]`, **then** merge |
  | batch `TLAST` | the candidate **being flushed** | merge, **then** flush |

  Applying "flush then merge" uniformly drops the batch's final tuple from the
  reduction — invisible unless that tuple is the winner, which is why the
  repair needs a regression **whose last tuple is the winning one**. The two
  can also coincide: a `TLAST` arriving on a tuple with a new `cand_id` must
  flush the previous candidate, reset, merge, and flush again — **two result
  records out of one input beat**. A loop written around a single
  `last_for_cand` boolean, as the current one is, cannot express that.

Explicit framing is still preferable and still wanted, for two reasons that
survive the correction: it removes the reliance on an ordering guarantee that
lives only in the matcher's FSM and in prose, and it is the only way an
**invalid** candidate — which never reaches the matcher and so produces no
tuples at all — can occupy its ordinal position in the result stream. The
second of those is a genuine blocker for §5.1 item 2, just not for D1/D2.

Three things had to be specified. **All three are answered as of 2026-08-07**
and the record they imply is §6.4. When this section was only *specified*,
every answer imposed an obligation on `class_score_core`; with the 2026-08-11
decision those obligations lapse for the MVP (there is no PL consumer to
carry them) and survive only as §6.4's contingency work list. §10 item 4
records the closure.

1. **How score tuples delimit a candidate.** *Settled: ascending `cand_id`,
   with `TLAST` on the last tuple of the batch — and it is now a **software**
   guarantee, not a matcher-FSM one.* Under §6.4 the tuples are emitted by the
   PS, which walks candidates in ordinal order and emits every trial for a
   candidate before moving on, so the ordering invariant lives in a loop
   anyone can read instead of in an emission order nobody specified. That is
   the whole of the upgrade this item asked for; a per-candidate `TLAST` or a
   transmitted tuple count would add nothing on top of a producer that already
   counts.
2. **What produces a result for an invalid candidate.** *Settled: the PS
   emits a placeholder tuple for it.* Invalid candidates still get a §6.2
   metadata record, and the PS is already reading that record to decide
   whether to run a match at all (§7.1, and the seam test in
   `hls/integration/`). When it decides not to, it emits one tuple with
   `templ_id = 0xFFFF` — "no template was run" — so the candidate occupies its
   ordinal position without any core having to hear about it. Neither feeding
   the metadata stream into `class_score_core` nor a downstream merge is
   needed.
3. **Whether every input descriptor yields exactly one software-visible
   result.** *Settled: **yes**.* `NUM_CANDS` in, `NUM_CANDS` results out, in
   input order, valid or not. The result buffer is a fixed-stride array the
   driver indexes by candidate ordinal, no re-association by `cand_id`, and a
   short read is an unambiguous error rather than a plausible outcome. (1) and
   (2) are what make this cheap to guarantee: the producer is the PS, and it
   knows `NUM_CANDS` before it starts.

None of this was ever a prerequisite for D1/D2, and it still is not.

---

## 6. Stream and record ABIs

### 6.1 Candidate descriptor — unchanged, 64-bit

```
[15:0]   ep_x
[31:16]  ep_y
[33:32]  side
[47:34]  max_tw
[63:48]  max_th
```

Already correct and already matched by `pack_candidate()` in the driver. Note
the wire fields are wider than the legal ranges in §4.1 — `max_tw` is 14 bits
but legal only to 216. That gap is exactly why §4 validation exists; do not
narrow the wire fields to "fix" it.

### 6.2 Patch metadata record — new, 128-bit, byte-aligned

One per input descriptor, in input order.

```
[15:0]    cand_id
[31:16]   status        = valid | (reason << 1)
[47:32]   x0            logical page coordinate
[63:48]   y0
[79:64]   patch_w
[95:80]   patch_h
[127:96]  reserved, zero
```

Software layout `"<HHHHHHI"` — `cand_id`, `status`, `x0`, `y0`, `patch_w`,
`patch_h`, `reserved` — 16 bytes.

`status` is deliberately described as **one 16-bit word**, not as a bit field
at 16 plus a bitmask from 17. Every field here is 16-bit aligned; software
unpacks `status` whole and masks it. Splitting `valid` out as a named bit
position in the ABI is how §6.3 got bit-packed in the first place.

### 6.3 Classification result record — **REMOVED FROM THE MVP ABI (2026-08-11)**

> **Decision (2026-08-11).** This record is deleted from the MVP rather than
> repaired. It never acquired a producer: the current overlay
> (`three_stage_combined`, 2026-08-11) carries no `class_score_core`, no
> `axi_lite_regs`, and no result DMA, so the 14-vs-16-byte defect below is
> resolved by removing the path, not by widening the driver's unpack to 16
> bytes. What replaces it, all PS-side in `sw/tme_driver.py`:
>
> - `match_template()` reads `tme_top_0`'s `result_score` / `result_x` /
>   `result_y` AXI4-Lite registers per trial (§7.1), latching each
>   Clear-on-Read `ap_vld` once;
> - the PS holds the per-kind scores and the running argmax (strict `>`,
>   frozen trial order — §6.4 option 1), so nothing fabricates `-1.0`
>   sentinels;
> - the PS retains the winning trial's template and constructs the box
>   itself, in absolute logical-page coordinates (§1):
>
>   ```
>   box_x = patch_x0 + match_x     # patch origin from the §6.2 metadata record
>   box_y = patch_y0 + match_y
>   box_w = winning template width      # post-§4.5 rounding — the streamed one
>   box_h = winning template height
>   ```
>
>   which is exactly the rebasing `hls/integration/pe_tme_tb.cpp` executed in
>   C simulation.
>
> `test_result_record_size_is_unresolved` in `sw/test_cand_packing.py` — the
> tripwire that held this section open — is retired with the record it
> guarded. The layout analysis below is retained as the design record for a
> future PL classifier (§6.4's standing condition).

The (now removed) 128-bit bit-packed layout was incompatible with the driver's
14-byte `"<fBBHHHH"` unpack, whose own comment claims 16 bytes. Depending on
how the DMA and the driver's transfer length interact, the 14-vs-16-byte
stride mismatch may surface as a DMA transfer error, a stalled/short transfer,
or truncated records that desynchronise after record 0 — the same class of bug
already fixed once on the candidate path. A buffer overwrite is one possible
outcome, not the guaranteed one; do not tune the fix to that single symptom.

Proposed replacement, 16 bytes, all fields naturally aligned, driver format
`"<BBHfHHHH"`:

```
[7:0]     kind         0=unknown, 1=male, 2=female, 3=ferrule_tentative
[15:8]    flags        bit0 = valid
[31:16]   cand_id
[63:32]   score        float32
[79:64]   box_x
[95:80]   box_y
[111:96]  box_w
[127:112] box_h
```

**Kind encoding: the driver's is authoritative** (`0=unknown, 1=male,
2=female, 3=ferrule`). It is already documented identically in
`class_score_core.h` and `sw/tme_driver.py`; only the `.cpp` disagrees, and it
is the unverified file. Note the **input** score-tuple `kind` field
legitimately uses a different encoding (`0=male, 1=female, 2=ferrule`) — the
two must be named apart in code, not merged.

**This record is incomplete as proposed — one score is not enough.** The
software contract (`run_candidates()` in `sw/tme_driver.py` — the method the
2026-08-11 rewrite replaced with `extract_candidates()` / `match_candidate()`)
returns `male_score`, `female_score` and `ferrule_score` per candidate, and
`postprocess_ps` consumes them; the driver currently fabricates `-1.0` for all
three, which is a placeholder, not a design. `class_score_core` already
accumulates `best_score[kind]` internally, so the data exists at the point of
emission. The record needs either the three per-kind scores alongside the
winning one, or an explicit, recorded decision that software downgrades to
winning-score-only — silently shipping `-1.0` sentinels as real fields is
neither.

Likewise the **match location**: `tme_top` produces `result_x`/`result_y` (the
best-match offset within the patch), but nothing currently routes it past the
matcher. **The PS does not possess the winning match location unless the PL
returns it** — it cannot re-derive the location without re-running the match
on the CPU, which defeats the accelerator. Any option in which the PS fills
`box_*` therefore still requires the match location (and winning
template/scale identity) in the result record or a side channel. That
constraint reshapes the "PS-filling is cheaper" argument below: PS-filling is
cheaper only for the *patch origin*, which the PS knows; the location and the
winning template identity must come from the PL either way.

**The question this section stayed open on** (answered 2026-08-11: the PS
fills the box — see the decision banner above): whether `box_*` is filled by
`class_score_core` or by the PS. The parked core zeroes those bits
unconditionally and the old driver unpacked them into a `box` tuple it handed
to callers, so software consumed `(0,0,0,0)` as though it were a real box.
Either the core fills them or the ABI drops the field — publishing zeros as
data was the worst of the three.

Whichever way it goes, the box is defined as **absolute logical-page
coordinates** `(x, y, w, h)` in the frame of §1, not patch-relative.

PL-filling is not free: producing an absolute box requires three things the
core does not currently receive — the **patch origin** (`x0`, `y0` from §6.2),
the **match location** within the patch, and the **winning template's
dimensions and scale**. Routing all three into `class_score_core` is a larger
change than it looks. But note what the PS actually holds: the patch origin
(via §6.2 metadata) — and nothing else. The match location and winning
template identity exist only in the PL, so "PS fills the box" still means the
PL transmits those two. The real choice is *where the arithmetic happens*, not
*who has the data*. Decide on that basis, not on where it looks tidier.

A zero/invalid box must also be defined. Recommend `(0, 0, 0, 0)` reserved to
mean "no box", valid only when `flags.valid == 0` — so a zero-area box can
never be confused with a real detection at the page origin.

**One half of the "who has the data" question above is now answered, and it
went the other way from the guess.** See §6.4: under PS sequencing the PS
*does* hold the winning template identity, because it chose the template for
every trial it launched. The match location still has to come from the PL, but
it already does — `tme_top` returns `result_x`/`result_y` in its AXI4-Lite
registers, and `hls/integration/pe_tme_tb.cpp` executes the rebasing
end-to-end. So the sentence above that says *"the match location and winning
template identity exist only in the PL"* is **half wrong**: the location does,
the identity does not. **This narrows the gap rather than closing it, and the
difference matters**: what the PS holds is every *trial's* identity, not the
identity of the trial `class_score_core` selected. The PL reduces to
`best_score[kind]` internally and returns a `kind`, so naming the winning
`(templ_id, match_x, match_y)` needs one of the three options in §6.4 — a
PS-side argmax whose tie-break matches the core's strict `>`, a widened result
record, or moving the reduction out of the PL. ~~Until one is chosen the PS
cannot fill `box_*` either.~~ (**Chosen 2026-08-11: the PS-side argmax, with
the reduction moved out of the PL entirely — see this section's banner.**)
PL-filling has lost its structural argument without PS-filling having gained
one.

### 6.4 Matcher score tuple — **OPTION 1 ADOPTED (2026-08-11); not materialised in the MVP**

*Status history: specified 2026-08-07 with the producer side running in
`hls/integration/` and the consumer parked. On 2026-08-11 option 1 below was
adopted and extended: `class_score_core` is removed from the MVP, so the PS
does not merely retain the argmax alongside a tuple stream — the PS-side
argmax IS the reduction, and no tuple stream is materialised on any wire.
The record layout below is retained as the ABI of record for the standing
condition at the end of this section (a matcher that iterates templates
internally), which is when a physical stream would come back.*

One per **trial**, where a trial is one (candidate, template) pair actually
run through the matcher, plus one placeholder per candidate that was never run
(§5.1 item 2).

```
[15:0]    cand_id       ordinal of the descriptor within the batch
[31:16]   templ_id      index into the PS template table; 0xFFFF = no trial
[47:32]   kind          one 16-bit word: 0=male, 1=female, 2=ferrule
[63:48]   reserved, zero
[95:64]   score         IEEE-754 float32
[111:96]  match_x       best-match offset WITHIN the patch (tme_top result_x)
[127:112] match_y       (tme_top result_y)
```

Software layout `"<HHHHfHH"` — 16 bytes, every field naturally aligned.

**Read `kind` as a whole 16-bit word and compare it**, exactly as §6.2 says to
read `status`. It is not a 2-bit field at bit 32 with 14 bits of headroom to
be reclaimed later; §6.3 is what bit-packing this ABI looks like afterwards.
Note also that this `kind` uses the matcher-side encoding (`0=male, 1=female,
2=ferrule`), which is **not** the §6.3 result encoding (`0=unknown, 1=male,
2=female, 3=ferrule_tentative`). They are off by one and must be named apart
in code — the existing `class_score_core.h` comment already documents both,
which is precisely why they get conflated.

#### The producer is the PS, and that is the decision

`template_match_core` does **not** grow a score-stream port. The tuples are
assembled in software from what a PS-sequenced match already has in hand:

| field | where the PS gets it |
|---|---|
| `cand_id` | the §6.2 metadata record it just read |
| `templ_id` | **it chose the template** — it wrote `templ_w`/`templ_h` and streamed the pixels |
| `kind` | its own template table, same source as `templ_id` |
| `score` | `tme_top`'s `result_score` register |
| `match_x/y` | `tme_top`'s `result_x`/`result_y` registers |

Nothing in that column needs new hardware, and the middle two rows are the
whole reason this ABI does not need a template-identity field on the wire from
the PL: **identity is implied by the invocation.** The PS starts one match per
(candidate, template) pair, so the answer it reads back is unambiguously that
pair's. This is a property of the sequencing, not a lucky coincidence — and it
is the property that stops holding the moment anyone gives the matcher more
than one template per `ap_start`.

**Per-trial identity is not the same as knowing which trial won, and that
distinction was elided in the first draft of this section.** `class_score_core`
reduces the tuples to `best_score[kind]` *inside the PL* and emits one result
per candidate; the PS is told the winning `kind`, not the winning `templ_id`
or its `match_x`/`match_y`. So the PS knows every trial it launched and still
cannot name the one the classifier selected. Since the §6.3 box needs exactly
that trial's location and template dimensions, this has to be pinned. Three
ways, and one of them has to be chosen before the classifier is connected:

1. **PS retains the per-kind argmax** (recommended). While emitting tuples the
   PS also keeps, per `(cand_id, kind)`, the `(templ_id, match_x, match_y)` of
   the best-scoring trial — a running max it can compute for free over data it
   is already producing. The PL's returned `tentative_kind` then indexes
   straight into it. No PL change, no new wire field.
   **The tie-break must match the core exactly**: `class_score_core` uses
   `if (score > best_score[kind])`, strictly greater, so the **first** trial
   reaching the maximum wins. A PS using `>=`, or iterating its bank in a
   different order, will silently disagree with the PL on ties — and ties are
   not exotic here, since a template that matches perfectly at two scales
   scores 1.0 at both.
2. **PL returns the winning identity.** Widen the §6.3 result record to carry
   `templ_id` and the winning `match_x`/`match_y`; `[127:112]` is reserved and
   `class_score_core` already tracks the maximum it would need to remember.
   Costs a PL change and re-opens §6.3's layout, which at the time of
   writing was blocked on the 14-vs-16-byte defect (resolved 2026-08-11 by
   removing the record; re-opening the layout now means specifying a new one
   from scratch).
3. **Drop the reduction from the PL.** If the PS is doing the argmax anyway
   under (1), `class_score_core` is performing a reduction over data the PS
   holds in full. Worth stating plainly as an option rather than discovering
   it later; it is not being recommended here, because that call belongs with
   whoever owns the throughput budget.
   **This is what the 2026-08-11 decision did.** Removing `class_score_core`
   from the MVP *is* option 3, executed with option 1's tie-break discipline
   — hence the heading's "option 1, extended to the whole reduction". The
   "not recommended" above is the state of the argument on 2026-08-07, kept
   so the reversal is visible rather than silent.

~~Until one is chosen, **the PS cannot fill `box_*`**~~ — **superseded
2026-08-11: option 1 was chosen (see this section's heading and status
note), so the PS does fill `box_*`, and §6.3 no longer records a blocker.**
The sentence is kept struck through rather than deleted because it names the
actual cause — the PS holding every trial's identity but not the winner's —
which is precisely what the adopted PS-side argmax resolves.

So the standing condition on this section: **if the matcher is ever changed to
iterate templates internally for throughput, §6.4 must move into the PL and
grow a real `templ_id` on the wire.** At that point the framing answers in
§5.1 revert to being cross-core invariants too. Nothing here is a claim that
PS sequencing is the right long-term architecture; it is the architecture
being built, and this is its ABI.

#### `templ_id = 0xFFFF` — the placeholder

A candidate the §4 validation rejected never reaches the matcher and produces
no trial. The PS emits exactly one tuple for it, with `templ_id = 0xFFFF`,
`kind = 0xFFFF`, `score = -1.0f`, and `match_x = match_y = 0`.

**`templ_id == 0xFFFF` is the only test. The score is ignored filler and is
NOT a sentinel.** An earlier revision of this section called `-1.0f` "outside
TM_CCOEFF_NORMED's `[-1, +1]` range"; that is simply wrong — the range is
closed, `tme_top` *clamps* to it (`if (score < -1.0f) score = -1.0f`), and a
perfectly anti-correlated window returns exactly `-1.0f` as a real result.
Worse, `class_score_core` initialises `best_score[]` to `-1.0f`, so a filler
`-1.0f` is indistinguishable from "this kind was never scored" at the one
place it would matter. Any value is equally arbitrary; `-1.0f` is chosen only
because it cannot *raise* a `best_score[]` entry if it is ever wrongly merged.
Do not build a second decoding on it.

**Consumers must branch on `templ_id` BEFORE decoding or indexing on `kind`.**
This is not style. `class_score_core.cpp` currently does
`best_score[(int)kind]` against `float best_score[3]` with `kind` taken as
`ap_uint<2>` — so `kind == 3` is already an out-of-bounds write today, latent
only because nothing emits a 3. A placeholder whose `kind` is `0xFFFF`
truncates to exactly 3 on that path. So **widening `score_stream_t` to the
§6.4 layout is not sufficient to connect the classifier**: the widening alone
preserves the out-of-bounds path and makes it reachable. The un-parking work
is (a) branch on `templ_id == 0xFFFF` and emit the `valid=0` result without
touching `best_score[]`, (b) range-check `kind` even after that, and (c) the
D1/D2 reordering in §5.1 above.

Placeholders must be tested **interior, final, and consecutive** — a rejected
descriptor between two valid ones, a rejected descriptor as the last in the
batch (which is where `TLAST` lands on a tuple that must not be merged), and
two rejections in a row (which is where a "reset on boundary" that assumes a
preceding merge emits a stale record). The seam suite in `hls/integration/`
covers the interior case on the producer side; the consumer side has no test
because the consumer is parked.

This is what makes §5.1 item 3 hold: every descriptor gets at least one tuple,
so a batch of `NUM_CANDS` descriptors yields `NUM_CANDS` results out of
`class_score_core` whatever the validation did.

#### Transport: one buffered transfer per batch, and its ceiling

Batch-only `TLAST` (§5.1 item 1) is a statement about the *transport*, not
just the record: the tuples must cross as **one buffered MM2S transfer** for
the whole batch. Per-trial transfers would be functionally wrong, not merely
slower — PYNQ's `sendchannel.transfer()` asserts `TLAST` on the last beat of
whatever it is given, so a transfer per tuple asserts `TLAST` on *every* tuple
and `class_score_core` flushes a result per trial instead of per candidate.

That gives the trial count a hard ceiling from §3.1's DMA bound: at 16 bytes
per tuple, `floor(262143 / 16) = ` **16,383 tuples per batch**.

The *expected* count is well under it but is **not a constant**, and freezing
one would be the wrong move. Trials per candidate is
`sum(len(bank) for bank in side_templates[side].values()) × len(MATCH_SCALES)`
— data-dependent on the template bank on disk. With `len(MATCH_SCALES) == 8`
and one base template per kind, that is 24 per candidate, so
`_MAX_CANDIDATES == 64` gives 1,536 trials (24,576 B). More base templates per
kind scale it linearly.

So the driver must **compute the trial count from the actual bank and validate
it before allocating or transferring**, against the 16,383 ceiling and against
its own buffer — the same discipline `DMA_MAX_BYTES` already gets in
`sw/tme_standalone_bringup.py`, where the DMA's own reported `buffer_max_size`
wins over the compiled-in constant.

**`class_score_core`'s `#pragma HLS LOOP_TRIPCOUNT max=720` is stale and must
be corrected when the core is un-parked.** 720 is under the 1,536 that the
software constants already imply at their minimum, and its derivation is
recorded nowhere. A `LOOP_TRIPCOUNT` is a scheduling hint and does not bound
behaviour, so this is not a functional bug — it means the core's reported
latency is understated by more than 2x, which is exactly the kind of number
that later gets quoted as a throughput result.

#### What is verified, and what is not

`hls/integration/pe_tme_tb.cpp` runs the producer side of this in C
simulation: it reads each §6.2 record, decides whether to run a trial, drives
the matcher with the record's geometry, and rebases `result_x`/`result_y` onto
the page. Every field in the table above is therefore exercised except the
tuple's own packing — which, under the 2026-08-11 decision, never crosses a
wire in the MVP: the PS consumes its own values in place. **Nothing here has
run on silicon.** If a PL classifier is ever re-instated, connecting it means
widening its 48-bit `score_stream_t` to this 128-bit layout as part of
un-parking it, not before, so the change lands with a testbench that can see
it — together with the rest of the contingency work list in §10 item 4.

---

## 7. Status and error reporting

Minimum AXI-Lite status surface, readable after `ap_done`:

- batch error flag (global configuration invalid)
- rejected-descriptor count
- processed-descriptor count
- `TLAST`-vs-`NUM_CANDS` mismatch flag

Without this, the §4.3 path is indistinguishable from a clean run — which is
precisely the current failure mode.

### 7.1 Control architecture — per-core AXI4-Lite, sequenced from software

**Decision, for bring-up and until something forces otherwise: each core
exposes exactly one AXI4-Lite slave, all of them are exposed to the PS through
the AXI interconnect, and the PS sequences them. There is no register-mirroring
wrapper.** Each core's generated `x<core>_hw.h` is therefore the authoritative
map for that core, and `sw/tme_driver.py` is rewritten around one window per
core instead of one window overall.

**Status: proven on hardware for one core, still a work list for the rest.**
This is no longer an adopted-but-unbuilt architecture. A standalone PL image
(`patch_extract_standalone.bit`) carrying `patch_extract_core` plus two AXI
DMAs and nothing else loads under PYNQ, and the core's `register_map`
reproduces the §7.1.2 table field for field — `bin_image_1`/`bin_image_2`,
`img_w`, `img_h`, `stride_bytes`, `buffer_bytes`, `num_cands`, and the three
`sts_*` registers with their Clear-on-Read `ap_vld` companions. PS sequencing
works exactly as this section assumed it would: write the scalars, arm the
metadata S2MM, set `ap_start`, push descriptors on the candidate MM2S, poll
`ap_idle`. **§8 records what that run does and does not establish** — the
architecture is validated, the core's full behaviour is not.

**The driver rewrite has since landed (2026-08-11).** `sw/tme_driver.py`
no longer has a `self._ctrl` window or any `_REG_*` constant: it resolves
`binarize_core_0`, `patch_extract_core_0` and `tme_top_0` by name in the
`three_stage_combined` overlay and addresses each through its own
`register_map`, never a transcribed offset. (Until that date it talked to a
single window at the old `0x00`–`0x4C` offsets, which matched no per-core
map and no hardware; §7.1.3 keeps that map as a costed, unadopted option.)
`template_match_core` now also presents the coherent one-slave
interface (2026-08-04: its `return` port moved from raw `ap_ctrl_hs` pins
into the `CTRL` bundle, so every scalar, the results and start/done live in
one `s_axi_CTRL` map — `patch_w 0x10`, `patch_h 0x18`, `templ_w 0x20`,
`templ_h 0x28`, `result_score 0x30` + `ap_vld 0x34`, `result_x 0x40/0x44`,
`result_y 0x50/0x54`; regenerated by synthesis, do not transcribe by hand).
It takes no `m_axi` pointer, so the `offset=slave` trap does not arise there.
`binarize_core` likewise has AXI-stream pixel input/output plus one `CTRL`
AXI-Lite bundle for `img_w`, `img_h`, `threshold` and `return`; it has no
`m_axi` pointer or image-address register, so that trap does not apply to it.
`class_score_core` was never checked and now does not need to be — it is out
of the MVP (2026-08-11) and absent from the overlay. The trap itself still
applies to any core that actually takes an `m_axi` pointer.

So §7.1.1 remains a work list for items 1, 2, 4 and 5. Item 3 is the one that
moved: the Clear-on-Read companions are now confirmed present on real
silicon rather than inferred from the generated header. Note that confirms
their *existence*, not the driver discipline — no run has yet read a `sts_*`
register twice and watched the second read come back empty, which is the
hazard item 3 actually names.

Rationale: a wrapper that fans a single START out to four cores and mirrors
shared registers into each of them is itself a piece of RTL that masters four
AXI-Lite interfaces, and it has to be debugged before any core can be. Software
sequencing needs no new RTL, fails in a place with a stack trace, and lets a
single core be brought up alone (§8's standalone extractor test depends on
exactly that). The unified map is preserved below as a deferred option, because
it becomes attractive again once the pipeline is stable and per-transaction PS
overhead starts to matter — but it is an optimisation, not a prerequisite.

#### 7.1.1 What software must define

Five things, none of which the per-core decision answers on its own:

1. **Distinct commands, not one START.** `BINARIZE` runs `binarize_core`
   alone; `EXTRACT` runs the candidate feeder → `patch_extract_core`;
   `MATCH` runs one `tme_top` invocation per (candidate, template, scale)
   trial under PS sequencing (2026-08-11: `class_score_core` is out of the
   MVP, so there is no fourth stage — the reduction is the PS's own loop).
   The old driver drove a single `_CTRL_START` bit and waited on one
   `_STATUS_ALL_DONE`, which only worked because a wrapper was assumed to
   know which subset a given START meant. With per-core control the driver
   states it explicitly: it writes `ap_start` to the cores that command
   actually runs, and waits on those cores' `ap_done`.
2. **`NUM_CANDS <= 64`,** enforced host-side before dispatch. This is a
   driver buffer bound (`_cand_buf`/`_meta_buf` are allocated at
   `_MAX_CANDIDATES × struct`; there is no `_result_buf` since §10 item 5
   closed), not a PL limit — `patch_extract_core` takes
   `num_cands` as a 16-bit register and has no per-candidate storage. Reject
   above it; do not truncate. The feeder must derive `TLAST` from the *same*
   value the extractor gets, or §5's cross-check compares a number against
   itself.
3. **Sticky completion and error state.** `ap_done` on the AXI-Lite control
   register is Clear-on-Read, and the extractor's `sts_*` registers have
   Clear-on-Read `ap_vld` companions. A polling loop that reads them twice
   loses the result the second time. Software reads each once per run and
   latches it; anything that must survive to a later read is the driver's
   variable, not the core's register. Errors must be sticky *per command* —
   an `EXTRACT` that trips `sts_flags` bit 0 must still report it after
   the batch completes, which it does only because §4.3 completes normally
   rather than returning early. (Named `RUN_CANDIDATES` / `PE_FLAGS` before
   2026-08-11, after the unified wrapper this section already rejected.)
4. **Short-stream timeout and reset ownership — the open one.** §5 makes a
   feeder that delivers fewer than `NUM_CANDS` beats block the extractor in
   `cand_in.read()` with `ap_done` low, deliberately. Nothing currently owns
   detecting that or recovering from it. The recovery is a PL reset, which
   with per-core control means: who asserts it, over what scope (one core or
   the whole datapath), and how the streams between cores are drained so the
   next batch does not inherit a half-transferred patch. **Assign this before
   the feeder is built** — it is the one item here that is a design gap rather
   than a transcription.
5. **Address-register ownership.** The grayscale source physical address is
   programmed into the grayscale MM2S DMA. The binary destination physical
   address is programmed into the binary S2MM DMA, and that same physical
   address is written to `patch_extract_core`'s `CTRL.bin_image` pointer before
   extraction. The driver owns consistency between those two writes; this is a
   DMA destination plus an HLS pointer, not one address mirrored into two HLS
   cores. `IMG_W`/`IMG_H` are still shared configuration (binarizer +
   extractor), as is `NUM_CANDS` (extractor + feeder). `CAND_ADDR` belongs to
   an AXI DMA instance driven through PYNQ's DMA driver (`dma_pe_data` MM2S),
   and `TEMPL_ADDR` likewise (`axi_dma_templ`). Do not fold DMA-owned
   addresses into a core's map because they sat adjacent in the old one.
   **There is no `RESULT_ADDR`**: the overlay has no result DMA, and results
   come back in `tme_top_0`'s AXI4-Lite scalar registers (§6.3, §10 item 5).
   Adding one back would be a change to §5.1/§6.3, not a wiring convenience.

#### 7.1.2 Per-core surface — settled and implemented

`patch_extract_core`
previously synthesised *three* control surfaces: an `s_axi_CTRL` bundle for the
scalars, a second `s_axi_control` bundle that Vitis HLS invented on its own for
the `offset=slave` DDR base, and raw `ap_start`/`ap_done`/`ap_idle`/`ap_ready`
top-level pins. `offset=slave` chooses *that there is* an offset register, not
which bundle holds it; without an explicit `s_axilite` line for the pointer it
defaults to a bundle named `control`. The addresses then collide in a way that
reads as a driver bug rather than an interface bug — `control 0x10` was the DDR
base while `CTRL 0x10` was `img_w`.

The rule, for every core in §9, is therefore: **every `s_axilite` port,
including the `m_axi` pointer and `return`, names the same bundle.** In
`patch_extract_core` that is `CTRL`, and the generated
`xpatch_extract_core_hw.h` is now a single map.

Note what does *not* catch this: C simulation calls the function directly and
co-simulation drives the generated wrapper, so both pass unchanged with the
interface split three ways. The evidence lives only in the synthesis interface
report and the generated driver header. **Reviewing the generated header is a
required step whenever an interface pragma changes.**

The current `patch_extract_core` `CTRL` map, for reference — this is what the
driver's extractor window is rewritten against:

| Offset | Field |
|---|---|
| `0x00` | `ap_start` / `ap_done` / `ap_idle` / `ap_ready` / `auto_restart` |
| `0x04`–`0x0C` | interrupt enable / status |
| `0x10` | `bin_image` DDR base, bits 31:0 |
| `0x14` | `bin_image` DDR base, bits 63:32 |
| `0x1C` | `img_w` |
| `0x24` | `img_h` |
| `0x2C` | `stride_bytes` |
| `0x34` | `buffer_bytes` |
| `0x3C` | `num_cands` |
| `0x44` / `0x48` | `sts_flags` / its `ap_vld` (COR) |
| `0x54` / `0x58` | `sts_rejected` / its `ap_vld` (COR) |
| `0x64` / `0x68` | `sts_processed` / its `ap_vld` (COR) |

Regenerated by synthesis into
`patch_extract/solution1/.autopilot/db/driver/src/xpatch_extract_core_hw.h`.
**Do not transcribe it into the driver by hand and do not assume it is
stable** — adding or reordering a port moves every offset after it.

#### 7.1.3 Deferred alternative — the unified `0x00`–`0x4C` wrapper

Recorded, not adopted. `sw/tme_driver.py` was written against one `self._ctrl`
window whose offsets (`_REG_CTRL = 0x00` … `_REG_PE_PROCESSED = 0x4C`) match no
per-core HLS map and never could, because they span four cores plus the DMA
plumbing; its own comment said "must match `axi_lite_regs` in block design"
(that file was rewritten on 2026-08-11 and the offsets are gone from it).
That register file is what a wrapper would have to implement, and it is kept
here so the option stays costed rather than forgotten:

| Offset | Name | Dir | Owner |
|---|---|---|---|
| `0x00` | `CTRL` — bit0 START, bit1 RESET | W | wrapper (fans out to four `ap_start`) |
| `0x04` | `STATUS` — bit3 ALL_DONE | R | wrapper (AND of four `ap_done`) |
| `0x08` | `GRAY_ADDR` | W | grayscale-input MM2S DMA source |
| `0x0C` | `BIN_ADDR` | W | binary-output S2MM DMA destination + `patch_extract_core` `bin_image` (same physical buffer) |
| `0x10` | `IMG_W` | W | **shared**: `binarize_core` + `patch_extract_core` |
| `0x14` | `IMG_H` | W | **shared**: `binarize_core` + `patch_extract_core` |
| `0x18` | `THRESHOLD` | W | `binarize_core` |
| `0x20` | `CAND_ADDR` | W | candidate feeder MM2S |
| `0x24` | `RESULT_ADDR` | W | result S2MM |
| `0x28` | `NUM_CANDS` | W | **shared**: `patch_extract_core` + candidate feeder (§5: the feeder derives TLAST from *this* register — one source, or the cross-check is meaningless) |
| `0x2C` | `SCORE_THRESH` (Q8.8) | W | `class_score_core` |
| `0x30` | `FERRULE_THRESH` (Q8.8) | W | `class_score_core` |
| `0x34` | `SCORE_MARGIN` (Q8.8) | W | `class_score_core` |
| `0x38` | `TEMPL_ADDR` | W | template streamer |
| `0x3C` | `STRIDE_BYTES` | W | `patch_extract_core` |
| `0x40` | `BUFFER_BYTES` | W | `patch_extract_core` (32-bit, §2.1) |
| `0x44` | `PE_FLAGS` | R | `patch_extract_core` `sts_flags` |
| `0x48` | `PE_REJECTED` | R | `patch_extract_core` `sts_rejected` |
| `0x4C` | `PE_PROCESSED` | R | `patch_extract_core` `sts_processed` |

What building it would cost, beyond the RTL itself:

- **Three configuration values fan out across participants** (`IMG_W`,
  `IMG_H`, `NUM_CANDS`). A wrapper could mirror one PS write to the relevant
  HLS cores/feeder; under §7.1.1 item 5 that fan-out is the driver's job.
  `BIN_ADDR` is different: it coordinates a binary S2MM destination with the
  extractor's `bin_image` pointer, so the wrapper would have to program two
  different kinds of interface, not mirror a value between HLS cores.
- **The deferred `BIN_ADDR` field is 32-bit while `patch_extract_core`'s
  `bin_image` offset is 64-bit.** The wrapper would zero-extend. Per-core, the
  driver programs the DMA destination and writes extractor offsets `0x10` and
  `0x14`, owning the high half explicitly — which is arguably better, since
  §2.1's 32-bit offset assumption then has one visible place to fail.
- **`CTRL`/`STATUS` are sequencing, not a passthrough**, which is why §7.1.1
  item 1 has to be answered either way. A wrapper does not remove that
  decision; it only moves it into RTL, where it is harder to change.

Revisit this once the pipeline is stable and per-transaction PS overhead is
measured, not before. The `0x00`–`0x4C` offsets are no longer in the driver
at all — the 2026-08-11 rewrite deleted them (§7.1) — so this table is a
specification of what a wrapper would have to implement, not a description
of anything that exists.

---

## 8. Implementation gates

**Board clock: 31.25 MHz — 32.000 ns period.** The bring-up platform clocks the
PL at 31.25 MHz, 6.4× slower than the 5.000 ns period every HLS estimate below
was taken against. Against 32.000 ns the extractor's 4.815 ns estimate has
≈27 ns of headroom and the matcher's 6.547 ns has ≈25 ns. **The extractor's
−1.165 ns and the matcher's −2.897 ns are therefore moot for bring-up.**
Neither gates getting the pipeline running end to end.

Four consequences, three of which are constraints rather than relief:

- **Do not re-target HLS to 32 ns.** Those figures describe RTL scheduled
  under `create_clock -period 5ns`. Re-synthesising at 32 ns produces
  *different* RTL: HLS chains far more operations per cycle and the critical
  path grows to fill whatever period it is given, so the estimate would come
  back marginal again while latency in cycles drops. The headroom comes from
  synthesising tight and clocking slow. Keep the 5 ns constraint in
  `package_provisional.tcl` and the matcher's `run_hls.tcl`.
- **Deferred, not discharged.** 31.25 MHz is a bring-up choice, and every one
  of these numbers returns the moment the clock is raised for throughput — at
  which point the matcher's correlation is the thing that decides how high the
  clock can go. The timing work moves from a *does-it-work* gate to a
  *how-fast* gate. Move it; do not delete it.
- **A generated bitstream is not timing closure** — and the real slack is now
  measured, so this bullet is no longer an instruction. See
  **"Post-route slack — measured"** below. Two things came back that this
  section had assumed otherwise about: the implementation is constrained at
  **20 ns, not 32 ns**, and the binding path is **reset distribution**, not
  any of the paths estimated below.
- **Nothing else in §8 is affected.** The passed three-stage C/golden CSim
  (§1.1), the still-unbuilt combined BD/DMA/silicon path, and the short-stream
  timeout ownership in §7.1.1 item 4 are independent of clock rate.

### Silicon — `template_match_core` standalone, 9/9 (2026-08-07)

**The first hardware result in this project.** Everything previously labelled
`hw` in this document was a C simulation of the board vectors; the rule that
said to write "csim -argv hw: 9/9" and never "hw 9/9" is retired for this core,
because there is now a real one to distinguish it from.

Run on a Zynq-7020 (`xc7z020clg400-1`) under PYNQ, from
`/home/xilinx/jupyter_notebooks/tme_test`, against the `tme_standalone` overlay
carrying `TermCount:hls:tme_top:0.2`, `axi_dma_patch` and `axi_dma_templ` and
nothing else. All six transferred files verified by `sha256sum -c` on the board
before the run.

| | result |
|---|---|
| cases | **9/9 passed**, score and **exact** (x, y) per case |
| §3.1 single transfer | `stress-max-envelope` moved **251,740 B in one transfer**, 10,403 B under the platform's 262,143 B ceiling |
| DMA bound, read at run time | **262,143 B** — matches the §3.1 constant rather than being assumed equal to it |
| result-map extremes | `stress-max-result` peaked at **(816, 303)**, the final cell of the 817 × 304 map `MAX_RESULT_W/H` are sized for |
| re-invocation | `cosim-eq-identical` re-run **after** the 251,740 B case: +1.0000 @ (0,0), PASS |
| PL clock | `Clocks.fclk0_mhz` = **31.2500 MHz (32.000 ns)** — see the clock note below |
| preflight | all four geometry registers round-trip 16 bits, are independent, and the core stays idle across the writes |
| teardown | no unverified-halt warning, so `close()` read back `DMACR.Reset == 0 && DMASR.Halted == 1` on both channels |

Read the last row precisely: a clean teardown is evidenced by the **absence**
of the warning block `close()` prints on failure, plus a zero exit status.
That is real evidence — the warning is unconditional on that path and forces
exit 1 — but it is weaker than a printed confirmation, and `echo $?` is worth
capturing in future transcripts.

**Two scope limits, because this image is deliberately small.** It contains one
core and its two DMAs, so a pass says the matcher's arithmetic and its §3.1
transfer bound work on hardware when the PS tells it the truth about geometry.
It says nothing about how the matcher *learns* that geometry — the extractor
seam is C-simulated only (`hls/integration/`, §9) and has no hardware. And it
says **nothing about timing**: the driver prints that reminder itself. Post-route
WNS comes from the implementation report, below.

#### First measured runtimes — the derived bracket was low

| case | derived (this document, before the run) | **measured** |
|---|---|---|
| `stress-max-envelope` | 372–411M cycles, 11.9–13.2 s | **13.362 s** (≈ 418M cycles at 31.25 MHz) |
| `stress-max-result` | 18–19M cycles, ≈ 0.6 s | **0.676 s** (≈ 21M cycles) |

The bracket was derived from the synthesis report's fixed sub-loop latencies
and explicitly excluded PS overhead. Measured, both cases land **above** its top
— by 1.6% on the envelope case and 11% on the smaller one. The direction and
the size are what a small fixed per-case overhead looks like (DMA setup plus the
1 ms poll granularity is ~0.1 s, which is 1% of 13 s and 15% of 0.7 s), so the
derivation was close for the case it was built for and should not be reused as
a general model. **Quote the measured figures from here on**; the bracket stays
only as the record of what was claimed before a board existed.

The 120 s default timeout clears the measured worst case by 9×, which is the
margin it was chosen for.

---

### Post-route slack — measured (2026-08-04)

Read off two standalone implementations under Vivado 2025.2, both routed.
**These are the first real place-and-route numbers in this document;
everything else called "timing" above is an HLS estimate.**

| | extractor | **matcher** |
|---|---|---|
| project | `tc25/.../patch_extract_standalone` | `tc25/.../tme_standalone` |
| WNS | **+10.144 ns** | **+3.537 ns** |
| TNS | 0.000, 0 failing / 45,918 | 0.000, 0 failing / 46,820 |
| WHS / THS | +0.020 / 0.000 ns | +0.023 / 0.000 ns |
| budget consumed (period − WNS) | 9.856 ns | 16.463 ns |
| worst-path data delay | 9.073 ns | **16.332 ns** |
| slack at the board's 32 ns | ≈ +22.14 ns | ≈ **+15.54 ns** |
| verdict | all constraints met | all constraints met, fully routed |

The last two rows are **not** the same quantity and an earlier revision of this
section conflated them. `period − WNS` is the whole launch-to-capture budget —
data path *plus* setup time, clock uncertainty and skew — and it is the figure
that stays fixed when you re-time the design to a different period, which is
why the 32 ns row is derived from it. The data-path delay is what the report
attributes to the path itself. They differ by ~0.13–0.78 ns here. Use the
first to re-time, the second to describe where the time goes.

**The matcher meets timing at 50 MHz.** That is worth stating plainly, because
every prior statement about matcher timing in this document and in
`package_provisional.tcl` derives from the HLS estimate of 6.547 ns against a
5.000 ns target — a figure that describes a 200 MHz ambition nothing has ever
required. At the constraint actually implemented the design closes with
+3.537 ns to spare, and at the board's period it has ≈15.5 ns. **The HLS
"timing failure" has never been a bring-up gate and is not one now**; §8's
existing judgement was right, and is now measured rather than argued.

The matcher's full-image utilisation is also well under the part: 14,665 LUT
(27.6%), 18,076 FF (17.0%), 115 BRAM tiles (82.1%), 34 DSP (15.5%) — for the
core *plus* both DMAs, both SmartConnects and the PS. Note the LUT figure
against HLS's 34.6k (64%) estimate for the core alone: **HLS over-estimated
LUTs by roughly 2.4×**. BRAM is the resource that is genuinely tight, exactly
as §3 says.

Two findings matter more than the numbers themselves.

**1. The constrained period is 20 ns, not 32 ns.** The report's only clock is
`clk_fpga_0` at **20.000 ns / 50.000 MHz**, so `WNS = +10.144 ns` means
**9.856 ns** of that 20 ns launch-to-capture budget was consumed — not that
there are 10 ns of margin at the board's period. Quote it as *"+10.144 ns
against a 20 ns constraint"*; the bare number invites exactly the misreading
this section was set up to avoid. And "9.856 ns" is the *budget*, setup,
uncertainty and skew included; the worst path's own data delay is **9.073 ns**
(previous paragraph). Neither figure is "the longest routed path" — that phrase
was used here for both and means neither.

Why the two differ: the BD requests `PCW_FPGA0_PERIPHERAL_FREQMHZ = 50` with no
board preset, and the handoff records `PCW_FCLK0_PERIPHERAL_DIVISOR0 = 8`,
`DIVISOR1 = 4` — a divisor product of 32. Vivado computes 50 MHz from those,
i.e. it assumes a 1600 MHz source. The board reports 31.25 MHz, and
`1600 / 1000 = 50 / 31.25 = 1.6` exactly, so the most likely explanation is
that PYNQ applies the same divisor pair to a **1000 MHz** PL PLL.

**The check this paragraph asked for has now run** (2026-08-07, matcher
bring-up): the driver prints `PL clock (measured): 31.2500 MHz (32.000 ns)`
before touching the core. So the 31.25 MHz is no longer "read off the board"
loosely — it is `Clocks.fclk0_mhz` on the running system, and it matches the
divisor arithmetic exactly.

**What that does and does not settle.** It confirms the resulting PL clock and
therefore the 1.6× over-constraint, which is all any timing statement here
depends on. It does **not** directly measure the 1000 MHz source: PYNQ derives
`fclk0_mhz` from the PLL configuration registers rather than counting edges, so
`1000 / 32 = 31.25` remains the *explanation* for a number now confirmed by a
second, independent path. That is a materially stronger position than before
and still not a counter measurement. Nobody needs to close the remaining gap
unless the source rate itself starts carrying weight.

Either way the design is **over-constrained by 1.6×**, which is the safe
direction: at the board's 32 ns the true slack on that path is ≈ **+22.14 ns**.

**2. Both designs are routing-bound, and neither binds where the estimates
said.**

| | worst path | data delay | routing share | logic levels |
|---|---|---|---|---|
| extractor | `proc_sys_reset_0/…PR_OUT_DFF[0]` → `patch_extract_core_0/CTRL_s_axi_U/FSM_onehot_rstate_reg[1]/R` | 9.073 ns | **93.6%** | 1 (LUT1) |
| matcher | `correlation_core/…Pipeline_load_seg/trunc_ln57_reg…` → `…/seg_231_fu_1516_reg[4]` | 16.332 ns | **93.3%** | 3 (2×LUT6, MUXF7) |

The extractor's binding path is **reset distribution** — a high-fanout net, not
any of the 4.815 ns counter recurrences tabulated below, none of which appear
near the top. The matcher's is the **fully-partitioned `seg[SEG_W]` register
file** inside `correlation_core`'s `load_seg` stage (`tme_top.h` sets
`SEG_W = PAR_COLS + MAX_TEMPL_W = 232`), which is a placement-and-fanout
problem created by `ARRAY_PARTITION complete`, not an arithmetic one.

That is the answer to the question this section posed — "if it is not
[enormous], something other than the paths estimated below is binding". Both
answers are: yes, and it is wiring.

**Do not read 16.332 ns as a floor.** Both paths are >93% routing at ≤3 logic
levels, and both met a constraint with room to spare — at which point Vivado
stops optimising. A tighter constraint would produce different placement and
different numbers. So these figures bound what the *current builds* achieve;
they do **not** establish a maximum clock. Anyone raising the clock should
re-implement at the target period and read the result, not invert a delay from
a build that was never asked to go faster. What the figures do establish is where to look first when that
happens — reset replication for the extractor, `seg[]` fanout for the matcher
— and that in neither case is it the loop arithmetic below.

**Timing (HLS estimates).** Re-measured after the two changes the previous
revision was waiting on — the `m_axi` conversion and the §3 narrowing to
11/9-bit patch counters — both of which have landed. `patch_extract_core` now estimates
**4.815 ns** against a 3.650 ns effective budget (5.000 ns target − 1.350 ns
uncertainty), i.e. **effective estimated slack of −1.165 ns**. The
corresponding 207.68 MHz is simply the reciprocal of the estimate, not a
place-and-route result, and carries no implementation guarantee. The estimate
is under the 5.000 ns period, so the design may still close once routed — but
the whole uncertainty allowance is consumed and then some, and HLS estimates
exclude routing.

**Do not read the 4.723 → 4.815 ns move as a result of the narrowing.** The
`m_axi` conversion and the 11/9-bit narrowing landed in the same revision, so
the delta is not attributable to either one and no A/B exists that would make
it so. Measuring that would take a deliberate one-variable experiment, and
nothing currently depends on the answer.

What the current report does support, without an A/B, is structural: the
counter recurrence spends 1.588 ns in *each* of its store and load legs
against 1.639 ns in the width-sensitive middle operation. Width is therefore
at most a third of that path even in principle, so counter width is not the
available lever regardless of which revision moved what. §3 remains right on
its own terms — the widths are correct and the registers smaller — but it
should not be cited as a timing remedy.

Three residual paths, from the synthesis log:

| Module | Path | Estimate |
|---|---|---|
| `..._Pipeline_full_cols` | 11-bit counter recurrence, store → load → **add** → store | 4.815 ns |
| `..._Pipeline_last_row` | 11-bit counter recurrence, store → load → **icmp** → store | 4.815 ns |
| `patch_extract_core` | 48-bit `footprint` multiply (§2.1 overflow check) | 3.950 ns |

The first two are the same construct twice, differing only in which middle
operation the report attributes. **Note the third is new information**: the
`stride_bytes * img_h` product is a single 3.950 ns operation, already over
the 3.650 ns effective budget by itself, so it is an independent gate rather
than slack under the counter paths. It runs once per batch, which makes it a
cheap thing to break across a cycle if it turns out to matter.

An earlier revision of this section stated the report showed no separate
`last_row` comparison path. That is no longer what the report says — the
current one attributes an `icmp` there. Both descriptions were accurate for
the build in front of them; the path list above supersedes.

**Still do not optimise this yet** — and now for a third reason. It was
"pending changes will move the path"; then it was "4.815 ns is an estimate
with the uncertainty allowance already spent, and only place-and-route can say
whether that matters". It is now simply that at 31.25 MHz nothing here is
close to binding. Read the real post-route slack off the standalone
implementation, record it above, and leave the loops alone until the clock is
raised.

**Hardware bring-up — the extractor runs standalone.** A PL image containing
only `patch_extract_core`, one MM2S/S2MM DMA carrying the candidate and pixel
streams, and a second S2MM-only DMA carrying metadata, loads under PYNQ and
the core behaves as this document specifies. Established on real silicon:

- **§7.1.2's map is the real map.** `register_map` matches the generated
  header field for field, Clear-on-Read companions included.
- **§4.3, the globally-invalid path.** `img_w = 0` with one descriptor returns
  `status = 0x0200` (reason bit 8, `valid` clear), `sts_flags = 0x1`,
  `rejected = 1`, `processed = 1`, and completes normally. That is a reported
  rejection replacing the silent 4-beat read of `bin_image[0][0..1]` this
  section named as the old failure mode.
- **§6.2's record ABI.** `"<HHHHHHI"` unpacks correctly at a 16-byte stride
  across a four-record batch.
- **§4.1/§4.2 per-descriptor rejection.** `max_tw = 0` returns
  `status = 0x0008` — reason bit 2 — while the three valid descriptors in the
  same batch return `0x0001` with correct geometry.
- **Non-compact stride (§2.1).** A 64 × 64 image at `stride_bytes = 72`
  extracts pixel-exact patches against the golden formula. First evidence the
  stride arithmetic is right in hardware and not only in csim.
- **Per-patch pixel `TLAST` (§5).** Each valid patch delivers exactly
  `patch_w × patch_h` beats and then TLASTs, with the sentinel fill beyond it
  untouched; the invalid descriptor contributes no pixel beats, observable
  because the three armed receives line up with candidates 0, 2 and 3 and
  compare clean. That is the hardware form of the manifest's
  `count = valid ? patch_w * patch_h : 0` assertion.
- **§7's status surface over a mixed batch.** `processed = 4`,
  `rejected = 1`, `flags = 0x0`.

What it did **not** establish, and must not be read as covering:

- **The `TLAST`/`NUM_CANDS` cross-check is half-tested.** Its clear path is
  exercised (`flags = 0x0` on correctly framed batches), its set path is not:
  PYNQ's `sendchannel.transfer()` asserts `TLAST` on the last beat of whatever
  buffer it is handed, so producing a genuine mismatch needs a feeder that can
  misplace the marker deliberately.
- **Everything ran at 64 × 64.** The high-coordinate cases and the full
  820 × 307 envelope — which is where §3, §3.1 and the 11/9-bit counters are
  actually load-bearing — exist only in the testbench.
- **Nothing downstream was present.** This is the extractor plus DMAs, not the
  pipeline, and it says nothing about §1's coordinate frame, which needs the
  binarizer.
- **One check in the globally-invalid run is inconclusive**, not passing: it
  reads the S2MM `idle` bit on a channel that was never armed, where a
  never-started channel and a channel that received data are not
  distinguishable. The no-pixel-reads claim rests on the multi-descriptor run
  instead.

**Hardware bring-up — the matcher, image built and ready to run (2026-08-04).**
A second standalone PL image carries `template_match_core` and nothing else:

```
PS M_AXI_GP0 -> smartconnect -> axi_dma_patch S_AXI_LITE   (0x41E0_0000)
                            |-> axi_dma_templ S_AXI_LITE   (0x41E1_0000)
                            \-> tme_top_0     s_axi_CTRL   (0x4000_0000)

axi_dma_patch M_AXIS_MM2S (8-bit) -> tme_top_0 patch_stream
axi_dma_templ M_AXIS_MM2S (8-bit) -> tme_top_0 templ_stream
both M_AXI_MM2S -> smartconnect -> PS S_AXI_HP0
```

Built by `vivado/tme_standalone/build_tme_standalone.tcl` (in the repo — unlike
the extractor's BD, which exists only as a project under `tc25/`), driven by
`sw/tme_standalone_bringup.py`. Both DMAs are **MM2S-only**: both matcher
streams are PL inputs and the results return over AXI4-Lite, so there is no
S2MM anywhere. Adding one would be a change to §5.1/§6.3, not a wiring
convenience.

Established so far, all off-board:

- **The image builds, fits and closes timing** — see the post-route table
  above; fully routed, 0 routing errors, 82% BRAM.
- **The single-slave surface is real.** The exported IP has no raw
  `ap_start`/`ap_done` pins; `s_axi_CTRL` is the only control interface, which
  is what makes PS sequencing possible without wrapper RTL. §7.1.2's rule held
  for this core without needing the three-way repair the extractor did.
- **The vectors are validated.** `csim -argv "hw"` passes 9/9, so a board
  failure is a hardware finding rather than a bad golden. Three of those nine
  were added on 2026-08-04 after an audit showed the suite could not have
  caught several classes of failure:

  | case | what it covers that nothing else did |
  |---|---|
  | `equality-negative` | a winning score that is **negative** (−0.732374). Every other score in every suite is exactly 0.0 or 1.0 — bit patterns `0x00000000` and `0x3F800000` — so the sign bit of `result_score` was never exercised on a register software reinterprets as raw IEEE-754. `anti-match` does not cover it: it puts −1.0 in the result *map* but reports +0.12 as its best. |
  | `equality-different` | a non-round mantissa (0.009578), for the same float-transport reason. |
  | `stress-max-result` | the **maximum result map, 817 × 304**, with the peak at its final cell (816, 303). `stress-max-envelope` maximises *storage* but its map is only 605 × 212, so the top 212 entries of `sti_col`/`sii_col`/`si_col` — arrays declared at `MAX_RESULT_W` = 817 — had never been written, and `isq_slide`/`norm_cols` had never run past `u = 604`. Grayscale rather than binary by necessity: a 4 × 4 binary window recurs within a few hundred of the 248,368 positions, so there would be no unique peak to assert. |

**~~Not yet run on silicon.~~ — SUPERSEDED 2026-08-07: it ran, 9/9.** Every
bullet below was established by the silicon result recorded earlier in this
same section ("Silicon — `template_match_core` standalone, 9/9"). The list is
kept because it is the pre-registered statement of what the run had to prove,
which is what makes that result a test rather than a demonstration; read it
as the checklist that was discharged, not as outstanding work:

- the arithmetic on real hardware against exact-location golden;
- §3.1's 251,740-byte single transfer (see §3.1 — this is the whole reason the
  `hw` suite exists);
- PS sequencing through one window: write geometry, arm both MM2S, `ap_start`,
  poll `ap_done`, read `result_score`/`result_x`/`result_y` with their
  Clear-on-Read `ap_vld` companions;
- that the `static` patch/template BRAMs do not leak between invocations —
  the script re-runs a 64 × 48 case *after* the 820 × 307 one specifically to
  test the shrink direction, which the suite order otherwise would not.

**What it will not establish, by construction: framing.** `tme_top` ignores
`TLAST` and reads exactly `patch_w × patch_h` beats, and the bring-up script
writes the geometry out of band, so the DMA length and the core's expectation
agree because software made them agree. That is precisely the agreement the
extractor→matcher seam does not get for free, and it is why item 3 of
`package_provisional.tcl`'s banner stays OPEN. A green run here says the
matcher works when told the truth about its input; it says nothing about how
it comes to know it.

**Software-side geometry validation is mandatory and now exists.**
`validate_geometry()` in `sw/tme_standalone_bringup.py` enforces §4.1's bounds,
§4.4's `patch >= templ` (equality legal), and §3.1's transfer bound *before*
`ap_start`. This is not defensive politeness: **`tme_top` has no validation
path, no reason bitmask and no status register** — unlike `patch_extract_core`,
it takes the four scalars at face value and indexes its BRAMs with them. An
oversized `patch_w` overruns `patch_buf[307][820]` into the neighbouring row
and returns a confident wrong answer; `patch_w < templ_w` returns the core's
`best_score` initialiser of **-2.0** at (0,0). The validator has a
`--selftest` mode that needs neither PYNQ nor a bitstream.

**Coverage.** Largely discharged. The testbench now exercises high in-range
coordinates (a procedural 9800 × 6400 page at stride 9856, which is what makes
the widened `bx`/`by` high bits verified rather than assumed), both a
non-compact stride and a compact one over the same golden, four globally
invalid image configurations including a 32-bit footprint wrap, oversized
descriptors, re-invocation, and an empty batch.

The two `TLAST`/`NUM_CANDS` mismatch cases are deliberately kept **one defect
each**, because a case carrying both is passed by a core implementing either
one: the early case delivers all `NUM_CANDS` descriptors with a spurious
marker on an interior ordinal *and the correct marker on the last*, so the
only anomaly is the extra one; the late case omits the final marker and adds
nothing else. The early case then runs a second, correctly framed batch on the
same streams **without draining** — so if the core ever truncates again, the
failure appears as the second batch receiving the first batch's patches, which
is the actual hazard, not merely as a flag on the run that caused it.

For `patch_extract_core` itself, C simulation, synthesis and a ten-transaction
Verilog co-simulation all pass.

**The binarizer boundary now has one passed narrow C/golden chain case.** The
separate phase in `hls/integration/` uses a 24×20 page, exact truncating
binarizer oracle, compact DDR staging, one valid extractor descriptor and one
matcher invocation. Generator self-checks passed in normal and `python -O`
modes, and Vitis HLS 2025.2 CSim verified the 480-byte logical raster, 14×12
patch at `(3,4)`, score `+1.000000` at local `(4,1)` / page `(7,5)`, plus the
truncation and legacy-layout controls. The older extractor → matcher seam also
remains unchanged and passed in the same run, including its rejection,
clipping, two-cursor, non-compact-stride and injected-bug controls. This closes
the C/golden execution gate only; it does not claim a combined top, RTL
cosimulation, direct stream, block design, DMA execution or silicon result.

One system gap remains, and it is not a testbench detail:

- **A short candidate stream cannot be tested at all**, by design. §5 makes the
  core read exactly `NUM_CANDS` descriptors, so a feeder that delivers fewer
  blocks in `cand_in.read()` with `ap_done` low. That is the intended failure
  mode (the alternative strands descriptors for the next invocation — see the
  FRAMING banner in `patch_extract_core.cpp`), but it means the recovery path
  is a **feeder-side timeout and PL reset**, which is a system-level obligation
  this document does not yet place on anyone. Assign it when the feeder is
  specified.

**`tme_legal` — discharged.** It is wired up as a manifest self-check
(`prevalidate()` asserts `tme_legal == valid` per row), the metadata stream now
exposes `valid`/`reason` and `check_meta()` compares both against the manifest,
and the stronger obligation is met too: the manifest pins
`count = valid ? patch_w * patch_h : 0`, so every invalid case asserts that
**no pixel payload reached the matcher**, not merely that the metadata says
invalid. The batch-level total-beat and leftover-beat checks catch any
surplus. `run_invalid_config()` makes the same assertion for the §4.3 path.

---

## 9. Work implied, by core

| Core | Work |
|---|---|
| `binarize_core` | Output scheduler owns raw→logical mapping and emits the mandatory zero final row/column (§1); exactly `img_w * img_h` compact logical beats feed an unchanged simple-mode S2MM. The 24×20 three-stage C/golden case passed byte-for-byte under Vitis HLS 2025.2 CSim; there is no combined BD/DMA/silicon claim |
| `patch_extract_core` | `m_axi` pointer + explicit stride + address arithmetic; 16-bit page coords; 11/9-bit patch counters; §4 validation with wide-type overflow checks (§2.1); metadata stream (§6.2); per-patch pixel `TLAST`; `NUM_CANDS`; status registers. **Standalone hardware bring-up passed** — see §8 for scope and for what it does not cover |
| `template_match_core` | result-dimension off-by-one (§4.4) — **done**. `MAX_PATCH` narrowed to the exact 820 × 307 envelope (§3) — **done**. **Golden/TB — done (2026-08-04)**, and it forced an arithmetic rewrite: the old `ap_fixed<48,24>` accumulators wrap at 8.4e6 against window ΣI² up to 1.35e9, the Q16.16 normalisation wraps at 32768, the Newton rsqrt diverges outside x∈(0,3), and the denominator omitted window-mean subtraction — it only ever passed csim because the sole golden was an all-zero patch. The core now computes exact integer sums and normalises once in float: `(N·ΣTI − ΣT·ΣI)/√((N·ΣT²−(ΣT)²)(N·ΣI²−(ΣI)²))`, the mathematical TM_CCOEFF_NORMED expression — agreement with cv2 is tolerance-based rather than bit-exact, and only on the `dt>0 && di>0` domain (§4.6); **the template streams as RAW uint8** (the old mean-subtracted int8+128 encoding wrapped for binary templates) and ΣT/ΣT² are computed in-core. `tme_tb.cpp` is manifest-driven (`-argv "cosim"` selects the RTL subset, same pattern as the extractor) and asserts score AND exact location: unique nonzero peaks (seed-searched margins), the final row/column, both equality axes, negative scores, flat windows, and the 820×307/216×96 maximum-storage case at near-maximum energies (21 csim / 5 cosim cases at that point; current counts below). The old generator's §4.5 `int()`-vs-`round()` drift is moot for the TB (the suite is synthetic); §4.5 stays owned by the template pipeline. Post-rewrite: 224 BRAM18K (80%, unchanged), 33 DSP, timing estimate **6.547 ns** (was 6.978) — over the raw 5 ns period, but that target was never required: the standalone image **routes with WNS +3.537 ns against the 20 ns constraint actually implemented** (§8), so timing is closed as a gate. A third TB suite, `-argv "hw"`, carries the cosim cases plus both 820×307 stress cases to silicon — the only test of §3.1's 251,740-byte single DMA transfer, and the only one that fills the 817×304 result map `MAX_RESULT_W/H` are sized for. csim 23/23, RTL cosim 7/7, and `csim -argv hw` 9/9 in simulation — and, **2026-08-07, the same nine vectors pass on SILICON, 9/9** (§8): the 251,740 B §3.1 transfer moved in one go, the 817×304 map's final cell hit at (816,303), a clean re-invocation after the largest case, and 13.362 s measured for the envelope case. This core's standalone bring-up is done; the extractor seam it feeds is C-simulated only. **§4.6 closed 2026-08-05** (flat templates are illegal input, rejected host-side before the first DMA by an exact `min == max` test — OpenCV's `templNorm < DBL_EPSILON` branch is *not* exact in the flat direction, which is an argument for rejecting here rather than deferring to cv2) — no RTL change, plus two direct DUT tests, the dt / ±num width extremes and both width-coupling witnesses, all running in every suite ahead of the manifest loop. **Remaining: consuming per-patch framing and transmitted geometry** — workable for bring-up under §7.1 PS sequencing now that `return` sits in the single `CTRL` bundle, with `sw/tme_standalone_bringup.py` supplying the geometry the core cannot validate for itself |
| extractor → matcher seam | **C simulation done (2026-08-07)** — `hls/integration/`, the first execution of anything downstream of the extractor's outputs. Neither core's own testbench can fail this way, because the thing under test is the PS loop between them: `meta_out` carries one record per *descriptor* and `patch_out` carries pixels for *valid candidates only*, so the PS keeps two cursors, and a loop that advances them together is correct on every batch with nothing rejected and permanently misaligned on the first one without. Pins: record-vs-pixel cursor discipline across a mid-batch rejection, matcher geometry taken from the metadata record rather than re-derived (a clipped candidate whose patch is 106 px where the §4.5 formula says 152), page-vs-patch coordinate rebasing, and `TLAST` landing exactly on beat `patch_w*patch_h` — which `tme_top` ignores by construction, so a framing disagreement is silent in the matcher and corrupts the *next* patch. Both PS bugs are also performed deliberately as negative controls and required to produce a wrong answer, so the suite is known to be able to fail. Result reads `SEAM TEST PASSED (0 errors): 4 descriptors, 3 matcher runs, 2 injected-bug controls` — quote it that way, not as a count of printed PASS lines. **Not synthesised and not cosimulated, on purpose** (no top function of its own; cosim drives one core through an RTL wrapper and cannot run a loop that decides what to do next). The hardware half is a two-core block design and is **not built** |
| binarizer → extractor → matcher C/golden | **Passed under Vitis HLS 2025.2 CSim (2026-08-09).** A separate `hls/integration/` phase preserves the extractor → matcher seam above and adds one deterministic 24×20 page at threshold 140. It verified exact truncating Gaussian arithmetic, 480 compact logical output beats and sidebands, zero final borders, the §6.2 metadata record, a 168-byte 14×12 patch at `(3,4)`, and raw-template matcher score `+1.000000` at local `(4,1)` / page `(7,5)` with margin 0.622036. Truncation and legacy raw-layout controls passed; the generator also passed normal and `python -O` self-checks. Results: `THREE-STAGE C/GOLDEN PASSED (0 errors): 480 gray beats -> 480 logical bytes -> 168 patch beats -> matcher local (4,1), page (7,5); 1 injected-layout control` and `INTEGRATION C/GOLDEN PASSED: three-stage errors=0, seam errors=0`; Vitis reported `CSim done with 0 errors`. The harness models PS/DDR staging between C core calls: this is not a combined top or cosim, direct core stream, combined BD, DMA run or silicon result |
| `class_score_core` | **removed from the MVP (2026-08-11)** — classification, the per-candidate reduction and box construction are PS-side (§5.1, §6.3, §6.4; §10 items 4–5). Not in the `three_stage_combined` overlay. Reconsider only if a benchmark of the completed PS classification identifies it as a meaningful bottleneck; the un-parking work list (D1/D2 reordering, `templ_id` branch before `kind`, `score_stream_t` widening to §6.4, `LOOP_TRIPCOUNT` correction) is preserved in §6.4 and §10 item 4 |
| `sw/tme_driver.py` | rewrite against the `three_stage_combined` overlay (§7.1: one `register_map` window per core — `binarize_core_0`, `patch_extract_core_0`, `tme_top_0` — plus the five DMAs `axi_dma_binarize`, `dma_pe_data`, `dma_pe_meta`, `axi_dma_patch`, `axi_dma_templ`); explicit backends (CPU / PL-binarize / PL-extract / PL-all) with **no silent FPGA→CPU fallback**; `match_template()` reads `tme_top_0`'s scalar result registers and the PS owns argmax (strict `>`, frozen trial order) and box construction (§6.3); stride-aware `suppress_text()` (§2.1); `buffer_bytes` register width; `NUM_CANDS`; enforce §4.1, §4.5 and §4.6 before dispatch |
| template pipeline | `max_tw` / `max_th` from post-round template dimensions (§4.5); stream templates as **RAW binarized bytes** — the matcher computes ΣT/ΣT² in-core since 2026-08-04, and the mean-subtracted int8+128 encoding it replaced must not come back (it wraps: binary T−mean spans ±255); reject flat templates (`min == max`) after the final resize/binarise/crop and before the first DMA (§4.6) |

---

## 10. Open items blocking implementation

1. ~~**§4.4**~~ — **resolved**: option 1 adopted; `+1` applied in both
   `tme_top.cpp` and `correlation_core.cpp`, `MAX_RESULT_W/H` = 817/304,
   §4.1 relaxed to `>=`.
2. ~~**matcher does not fit the part**~~ — **resolved**: `MAX_PATCH` narrowed
   to the exact 820 × 307 envelope, 352 → 224 BRAM18K (125% → 80%). See §3,
   which also records that the supporting A/B synthesis run was not retained
   in the repository.
3. **§2.2** — can CMA satisfy two separately contiguous ~60.2 MiB
   allocations? If not, tiling is a platform requirement and §2 changes.
   Probe: `sw/probe_cma_budget.py`, run on the board with `--overlay`.
4. ~~**§5.1**~~ — **CLOSED for the MVP, 2026-08-11, by architecture
   decision: `class_score_core` is removed from the MVP.** The PS owns the
   whole per-candidate reduction: it sequences one matcher invocation per
   (candidate, template, scale) trial, reads `tme_top_0`'s scalar results,
   and keeps a running per-kind argmax using **strictly-greater comparison
   (`if score > best_score`, never `>=`) over a frozen trial order**, so the
   first trial reaching the maximum wins ties deterministically — the same
   tie-break the CPU baseline's `best_template_match_local`/
   `classify_endpoint` already implement, which is what makes CPU/PL parity
   checkable. The §6.4 answers stand as design record; with no PL consumer,
   the tuples are never materialised on a wire, and every obligation this
   item listed (boundary-ordering regressions, the `templ_id == 0xFFFF`
   branch, placeholder coverage, the 16,383-tuple ceiling check, the
   `LOOP_TRIPCOUNT` correction) lapses for the MVP and is preserved in §6.4
   as the contingency work list. Re-open only if a benchmark of the completed
   PS classification identifies it as a meaningful bottleneck.

   The standing condition survives the closure: **if the matcher is ever
   changed to iterate templates internally, §6.4 moves into the PL and grows
   a real `templ_id` on the wire** — and this item's work list comes back
   with it.
5. ~~**§6.3**~~ — **CLOSED 2026-08-11: `box_*` is filled by the PS, and the
   §6.3 record is deleted from the MVP ABI rather than repaired.** The record
   had no producer — the `three_stage_combined` overlay carries no
   `class_score_core`, no `axi_lite_regs`, and no result DMA — so the
   14-vs-16-byte defect is resolved by removing the path, **not** by widening
   the driver's 14-byte unpack to 16. `match_template()` reads `tme_top_0`'s
   `result_score`/`result_x`/`result_y` registers per trial, the PS retains
   the winning trial's template, and constructs
   `box = (patch_x0 + match_x, patch_y0 + match_y, templ_w, templ_h)` in
   absolute logical-page coordinates (§1) — the rebasing
   `hls/integration/pe_tme_tb.cpp` already executed in C simulation. Per-kind
   scores are PS state under the same argmax, so the `-1.0` sentinel
   fabrication goes away with the record.
   `test_result_record_size_is_unresolved` in `sw/test_cand_packing.py`, the
   tripwire that held this item open, is retired with the record it guarded.
6. **§7.1 item 4** — who owns the short-stream timeout and the PL reset that
   recovers from it, and over what scope. New in §5's framing decision;
   nothing owns it today.
7. ~~**Matcher timing**~~ — **closed as a gate, 2026-08-04, by measurement.**
   The standalone matcher image implements and routes with **WNS +3.537 ns
   against a 20 ns constraint** (16.463 ns of that budget consumed, of which
   16.332 ns is the worst path's data delay; ≈+15.5 ns at the board's 32 ns),
   all constraints met. See the post-route table in §8.

   The HLS estimate of 6.547 ns is against a 5.000 ns target, i.e. a 200 MHz
   ambition **nothing in this pipeline has ever required** — it was never the
   number that decided whether the design works. What remains true: the
   matcher cannot be clocked at 200 MHz, and the 5 ns synthesis constraint
   stays (§8) because the headroom comes from synthesising tight and clocking
   slow.

   What is *not* closed is how high the clock can go. The measured 16.332 ns
   data delay is not a floor — it is what a build that met its constraint with
   room to spare happened to produce, and >93% of it is routing at 3 logic
   levels.
   Re-implement at the target period before claiming any maximum frequency,
   and expect `correlation_core`'s partitioned `seg[]` fanout (§8) to be the
   thing to fix, not the loop arithmetic.
8. **Larger patch envelopes now have a second bound** — §3.1. Not blocking
   today (251,740 of 262,143 bytes used), listed so it is not rediscovered
   the hard way.

Everything else in this document is settled and implementable.
