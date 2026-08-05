# PL Interface Contract — Freeze v1

**Status:** proposed freeze. Sections marked **OPEN** need a decision before
implementation starts; everything else is settled and should be treated as
binding.

**Purpose.** `patch_extract_core`'s boundary arithmetic is verified and
`binarize_core` works, but the two cannot be connected: the image interface,
the coordinate frame, the descriptor validity rules and the result ABI are all
unresolved, and each one reaches into more than one core. This document freezes
them so the remaining work is implementation rather than negotiation.

**Scope.** `binarize_core` → DDR → `patch_extract_core` → `template_match_core`
→ `class_score_core` → `sw/tme_driver.py`.

**How to use it.** Anything below is a contract term. If an implementation
disagrees with this document, the implementation is wrong — or this document
gets amended first, deliberately.

---

## 1. Coordinate frame

**Decision: DDR holds logical, detector-aligned coordinates. The binarizer's
stream-to-DDR writer owns the transformation.**

`binarize_core` emits a raster where the beat at raw `(r, c)` carries the
Gaussian result whose 3×3 window is centred at logical `(r-1, c-1)`, valid only
for `r >= 2, c >= 2`. Raw rows/cols 0 and 1 are pipeline fill and read 0.

The writer stores raw `(r, c)` at logical `(r-1, c-1)` for `r >= 1, c >= 1`,
and discards raw row 0 and raw column 0.

Consequences, which are the point of choosing this option:

- Logical rows `0 .. img_h-2` and columns `0 .. img_w-2` are filled directly.
  Logical row 0 and column 0 inherit the natural zeros from raw row 1 and
  column 1, which is already the correct border value.
- **Logical row `img_h-1` and column `img_w-1` are never produced** and must be
  written as 0 by the writer. 0 means "no ink" under `THRESH_BINARY_INV`, so a
  border can never fabricate a feature. This fill is mandatory, not cosmetic —
  without it those pixels are whatever the buffer held before.
- `patch_extract_core` does no coordinate correction at all. It reads logical
  coordinates from logical storage.
- Text-suppression rectangles keep using logical coordinates, unchanged.
- No `np.roll` over a ~60 MiB buffer on the CPU.

**Rejected alternatives, recorded so they are not revisited:** adding `+1` to
`build_endpoint_patch()` or to the descriptor endpoint. Both also shift
clipping behaviour and the reported boxes, which are detector outputs, not
storage details. Ownership belongs at the storage boundary.

### 1.1 Golden model

An integrated binarize-to-extractor golden **must model the HLS arithmetic, not
OpenCV**. `binarize_core` computes `sum >> 4` — truncation — while
`cv2.GaussianBlur` rounds. Correcting the coordinate shift alone will not make
an OpenCV comparison bit-exact. Either the golden replicates the truncation
(as `binarize_generate_golden.py` already does) or the HLS rounding changes;
do not compare against stock OpenCV and apply a tolerance to paper over it.

---

## 2. Image geometry and memory

**Decision: runtime images up to 9856 × 6400 with an explicit byte stride.**

| Quantity | Value |
|---|---|
| `img_w` | runtime, `3 <= img_w <= 9856` |
| `img_h` | runtime, `3 <= img_h <= 6400` |
| `stride_bytes` | runtime, `>= img_w` |
| `buffer_bytes` | `>= stride_bytes * img_h` |

- Stride is explicit and **must not be assumed equal to `img_w`**. The current
  2D array signature `bin_image[PE_MAX_IMG_H][PE_MAX_IMG_W]` hardcodes a
  2560-byte stride; that is one of the things being removed.
- Stride **should** be rounded up to a 64-byte multiple for AXI burst
  efficiency. 9856 is already 77×128.
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
  `max_th <= 96` imply `patch_w <= 820` and `patch_h <= 307` exactly, and
  `hls/template_match/ab_bram/` retains the A/B synthesis showing why the
  difference matters. At the former 1024 × 320 the matcher needed **352
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

### 5.1 Score-stream framing — **OPEN, but does NOT block the D1/D2 repair**

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
  tuple's `cand_id`. Both are fixed by reordering the loop body — detect the
  boundary, flush and label the completed candidate, *then* merge the new
  tuple — with no change to how boundaries are detected and no dependency on
  this section. See the D1/D2 entries in `class_score_core.cpp`.

Explicit framing is still preferable and still wanted, for two reasons that
survive the correction: it removes the reliance on an ordering guarantee that
lives only in the matcher's FSM and in prose, and it is the only way an
**invalid** candidate — which never reaches the matcher and so produces no
tuples at all — can occupy its ordinal position in the result stream. The
second of those is a genuine blocker for §5.1 item 2, just not for D1/D2.

Three things must be specified:

1. **How score tuples delimit a candidate.** Explicit `TLAST`-per-candidate on
   the score stream, or a transmitted tuple count per candidate, or a
   start-of-candidate sideband. Any of the three removes the dependency on the
   matcher's emission order; `cand_id` inference is correct today but is an
   undocumented cross-core invariant rather than a contract term.
2. **What produces a result for an invalid candidate.** Invalid candidates
   bypass the matcher entirely (§4), so no score tuples exist for them and
   `class_score_core` never sees them. Either the classifier is fed the §6.2
   metadata stream so it can emit a `valid=0` result in the right ordinal
   position, or something downstream merges the two streams. Unspecified today.
3. **Whether every input descriptor yields exactly one software-visible
   result.** Recommended: **yes** — `NUM_CANDS` in, `NUM_CANDS` results out, in
   input order, valid or not. That makes the result buffer a fixed-stride array
   the driver can index by candidate ordinal, and removes any need to
   re-associate by `cand_id`. It also makes a short read an unambiguous error
   rather than a plausible outcome.

(1) is an upgrade, not a prerequisite, for D1/D2. (2) and (3) remain genuinely
open and gate the *integration-signoff* behaviour of `class_score_core`, since
they decide what the core must emit for candidates it never hears about.

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

### 6.3 Classification result record — **OPEN**, 128-bit, byte-aligned

The current 128-bit bit-packed layout is incompatible with the driver's
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
software contract (`run_candidates()` in `sw/tme_driver.py`) returns
`male_score`, `female_score` and `ferrule_score` per candidate, and
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

**Still open:** whether `box_*` is filled by `class_score_core` or by the PS.
Right now the core zeroes those bits unconditionally and the driver unpacks
them into a `box` tuple it hands to callers, so software consumes `(0,0,0,0)`
as though it were a real box. Either the core fills them or the ABI drops the
field — publishing zeros as data is the worst of the three.

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

What has *not* changed: `sw/tme_driver.py` still talks to a single
`self._ctrl` window at the old `0x00`–`0x4C` offsets — the standalone
notebook drives the registers directly, so the per-core driver rewrite is
still owed. `template_match_core` now also presents the coherent one-slave
interface (2026-08-04: its `return` port moved from raw `ap_ctrl_hs` pins
into the `CTRL` bundle, so every scalar, the results and start/done live in
one `s_axi_CTRL` map — `patch_w 0x10`, `patch_h 0x18`, `templ_w 0x20`,
`templ_h 0x28`, `result_score 0x30` + `ap_vld 0x34`, `result_x 0x40/0x44`,
`result_y 0x50/0x54`; regenerated by synthesis, do not transcribe by hand).
It takes no `m_axi` pointer, so the `offset=slave` trap does not arise there.
`binarize_core` and `class_score_core` have not been checked, let alone
fixed, and the same `offset=slave` trap that split the extractor three ways
applies to every one of them that takes an `m_axi` pointer.

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

1. **Two distinct commands, not one START.** `BINARIZE` runs `binarize_core`
   alone; `RUN_CANDIDATES` runs feeder → `patch_extract_core` →
   `template_match_core` → `class_score_core`. Today both drive the same
   `_CTRL_START` bit and both wait on the same `_STATUS_ALL_DONE`, which only
   worked because a wrapper was assumed to know which subset a given START
   meant. With per-core control the driver states it explicitly: it writes
   `ap_start` to the cores that command actually runs, and waits on those
   cores' `ap_done`.
2. **`NUM_CANDS <= 64`,** enforced host-side before dispatch. This is a
   driver buffer bound (`_cand_buf`/`_result_buf` are allocated at
   `_MAX_CANDIDATES × struct`), not a PL limit — `patch_extract_core` takes
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
   a `RUN_CANDIDATES` that trips `PE_FLAGS` bit 0 must still report it after
   the batch completes, which it does only because §4.3 completes normally
   rather than returning early.
4. **Short-stream timeout and reset ownership — the open one.** §5 makes a
   feeder that delivers fewer than `NUM_CANDS` beats block the extractor in
   `cand_in.read()` with `ap_done` low, deliberately. Nothing currently owns
   detecting that or recovering from it. The recovery is a PL reset, which
   with per-core control means: who asserts it, over what scope (one core or
   the whole datapath), and how the streams between cores are drained so the
   next batch does not inherit a half-transferred patch. **Assign this before
   the feeder is built** — it is the one item here that is a design gap rather
   than a transcription.
5. **Address-register ownership.** Under a wrapper, `BIN_ADDR` was one PS
   write mirrored into two cores. Per-core, it is two writes to two registers
   and the driver owns their consistency. Same for `IMG_W`/`IMG_H`
   (binarizer + extractor) and `NUM_CANDS` (extractor + feeder). The DMA
   address registers are different in kind: `CAND_ADDR` and `RESULT_ADDR`
   belong to AXI DMA instances driven through PYNQ's DMA driver, not to any
   HLS core, and `TEMPL_ADDR` belongs to the template streamer. Do not fold
   DMA-owned addresses into a core's map because they sat adjacent in the old
   one.

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
plumbing; its own comment says "must match `axi_lite_regs` in block design".
That register file is what a wrapper would have to implement, and it is kept
here so the option stays costed rather than forgotten:

| Offset | Name | Dir | Owner |
|---|---|---|---|
| `0x00` | `CTRL` — bit0 START, bit1 RESET | W | wrapper (fans out to four `ap_start`) |
| `0x04` | `STATUS` — bit3 ALL_DONE | R | wrapper (AND of four `ap_done`) |
| `0x08` | `GRAY_ADDR` | W | `binarize_core` `m_axi` offset |
| `0x0C` | `BIN_ADDR` | W | **shared**: `binarize_core` write + `patch_extract_core` `bin_image` |
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

- **Four registers are shared** (`BIN_ADDR`, `IMG_W`, `IMG_H`, `NUM_CANDS`).
  The wrapper mirrors one PS write into two or more core registers — which is
  the actual benefit on offer, and under §7.1.1 item 5 becomes the driver's job
  instead.
- **`BIN_ADDR` is 32-bit in the driver, 64-bit at `patch_extract_core`'s
  `bin_image` offset.** The wrapper would zero-extend. Per-core, the driver
  writes `0x10` and `0x14` and owns the high half explicitly — which is
  arguably better, since §2.1's 32-bit offset assumption then has one visible
  place to fail.
- **`CTRL`/`STATUS` are sequencing, not a passthrough**, which is why §7.1.1
  item 1 has to be answered either way. A wrapper does not remove that
  decision; it only moves it into RTL, where it is harder to change.

Revisit this once the pipeline is stable and per-transaction PS overhead is
measured, not before. Until then the driver's `0x00`–`0x4C` offsets are dead
constants awaiting the per-core rewrite, not a map to implement.

---

## 8. Implementation gates

**Board clock: 31.25 MHz — 32.000 ns period.** The bring-up platform clocks the
PL at 31.25 MHz, 6.4× slower than the 5.000 ns period every HLS estimate below
was taken against. Against 32.000 ns the extractor's 4.815 ns estimate has
≈27 ns of headroom and the matcher's 6.978 ns has ≈25 ns. **The extractor's
−1.165 ns and the matcher's −3.328 ns are therefore moot for bring-up.**
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
- **Nothing else in §8 is affected.** Coverage, the binarize-to-extractor
  integration case, and the short-stream timeout ownership in §7.1.1 item 4
  are independent of clock rate.

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
`clk_fpga_0` at **20.000 ns / 50.000 MHz**, so `WNS = +10.144 ns` means the
longest routed path is **9.856 ns** — not that there are 10 ns of margin at the
board's period. Quote it as *"+10.144 ns against a 20 ns constraint"*; the bare
number invites exactly the misreading this section was set up to avoid.

Why the two differ: the BD requests `PCW_FPGA0_PERIPHERAL_FREQMHZ = 50` with no
board preset, and the handoff records `PCW_FCLK0_PERIPHERAL_DIVISOR0 = 8`,
`DIVISOR1 = 4` — a divisor product of 32. Vivado computes 50 MHz from those,
i.e. it assumes a 1600 MHz source. The board reports 31.25 MHz, and
`1600 / 1000 = 50 / 31.25 = 1.6` exactly, so the most likely explanation is
that PYNQ applies the same divisor pair to a **1000 MHz** PL PLL. **That last
step is an inference, not a measurement** — the divisor arithmetic is read from
the handoff, the 31.25 MHz is read from the board, and nothing has yet
confirmed the source rate. Settle it in one line the next time the board is up:

```python
from pynq import Clocks; print(Clocks.fclk0_mhz)
```

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

**Not yet run on silicon.** Nothing below is claimed until it is:

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

C simulation, synthesis and a ten-transaction Verilog co-simulation all pass.

Two gaps remain, and neither is a testbench detail:

- **The binarize-to-extractor integration case** is still absent — see §1.1 for
  what its golden must model.
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
| `binarize_core` | DDR writer owns raw→logical mapping (§1); zero-fill last row and column |
| `patch_extract_core` | `m_axi` pointer + explicit stride + address arithmetic; 16-bit page coords; 11/9-bit patch counters; §4 validation with wide-type overflow checks (§2.1); metadata stream (§6.2); per-patch pixel `TLAST`; `NUM_CANDS`; status registers. **Standalone hardware bring-up passed** — see §8 for scope and for what it does not cover |
| `template_match_core` | result-dimension off-by-one (§4.4) — **done**. `MAX_PATCH` narrowed to the exact 820 × 307 envelope (§3) — **done**. **Golden/TB — done (2026-08-04)**, and it forced an arithmetic rewrite: the old `ap_fixed<48,24>` accumulators wrap at 8.4e6 against window ΣI² up to 1.35e9, the Q16.16 normalisation wraps at 32768, the Newton rsqrt diverges outside x∈(0,3), and the denominator omitted window-mean subtraction — it only ever passed csim because the sole golden was an all-zero patch. The core now computes exact integer sums and normalises once in float: `(N·ΣTI − ΣT·ΣI)/√((N·ΣT²−(ΣT)²)(N·ΣI²−(ΣI)²))`, which is cv2's TM_CCOEFF_NORMED exactly; **the template streams as RAW uint8** (the old mean-subtracted int8+128 encoding wrapped for binary templates) and ΣT/ΣT² are computed in-core. `tme_tb.cpp` is manifest-driven (`-argv "cosim"` selects the RTL subset, same pattern as the extractor) and asserts score AND exact location: unique nonzero peaks (seed-searched margins), the final row/column, both equality axes, negative scores, flat windows, and the 820×307/216×96 maximum-storage case at near-maximum energies — csim 21/21, RTL cosim 5/5. The old generator's §4.5 `int()`-vs-`round()` drift is moot for the TB (the suite is synthetic); §4.5 stays owned by the template pipeline. Post-rewrite: 224 BRAM18K (80%, unchanged), 33 DSP, timing estimate **6.547 ns** (was 6.978) — over the raw 5 ns period, but that target was never required: the standalone image **routes with WNS +3.537 ns against the 20 ns constraint actually implemented** (§8), so timing is closed as a gate. A third TB suite, `-argv "hw"`, carries the cosim cases plus both 820×307 stress cases to silicon — the only test of §3.1's 251,740-byte single DMA transfer, and the only one that fills the 817×304 result map `MAX_RESULT_W/H` are sized for. csim 23/23, RTL cosim 7/7, and `csim -argv hw` 9/9 — that last is a **C simulation of the board vectors, not a hardware result**; nothing has run on silicon yet. **Remaining: consuming per-patch framing and transmitted geometry** — workable for bring-up under §7.1 PS sequencing now that `return` sits in the single `CTRL` bundle, with `sw/tme_standalone_bringup.py` supplying the geometry the core cannot validate for itself |
| `class_score_core` | parked. D1/D2 are repairable now (reorder flush-before-merge, §5.1); D6/D7/D8 and the per-kind-score/match-location gaps wait on §6.3 |
| `sw/tme_driver.py` | buffer sizes per §2.2; stride-aware `suppress_text()` (§2.1); `buffer_bytes` register width; `NUM_CANDS`; result unpack per §6.3; enforce §4.1 and §4.5 before dispatch |
| template pipeline | `max_tw` / `max_th` from post-round template dimensions (§4.5); stream templates as **RAW binarized bytes** — the matcher computes ΣT/ΣT² in-core since 2026-08-04, and the mean-subtracted int8+128 encoding it replaced must not come back (it wraps: binary T−mean spans ±255) |

---

## 10. Open items blocking implementation

1. ~~**§4.4**~~ — **resolved**: option 1 adopted; `+1` applied in both
   `tme_top.cpp` and `correlation_core.cpp`, `MAX_RESULT_W/H` = 817/304,
   §4.1 relaxed to `>=`.
2. ~~**matcher does not fit the part**~~ — **resolved**: `MAX_PATCH` narrowed
   to the exact 820 × 307 envelope, 352 → 224 BRAM18K (125% → 80%). See §3 and
   `hls/template_match/ab_bram/`.
3. **§2.2** — can CMA satisfy two separately contiguous ~60.2 MiB
   allocations? If not, tiling is a platform requirement and §2 changes.
   Probe: `sw/probe_cma_budget.py`, run on the board with `--overlay`.
4. **§5.1** — score-stream framing: how candidates are delimited, and how an
   invalid candidate that bypassed the matcher still produces a result.
   Blocks the invalid-candidate result path and integration signoff, not the
   D1/D2 sequencing repair (see the §5.1 correction).
5. **§6.3** — are `box_*` filled by the PL or dropped from the ABI? PL-filling
   needs patch origin, match location, and winning template dimensions routed
   into the classifier. **Also blocks the result path outright**: the driver
   unpacks 14 bytes while the PL emits 16, so `_result_buf` is under-allocated
   by 128 bytes at a full 64-candidate batch. `class_score_core` stays
   disconnected until this is resolved; `test_result_record_size_is_unresolved`
   in `sw/test_cand_packing.py` is the tripwire.
6. **§7.1 item 4** — who owns the short-stream timeout and the PL reset that
   recovers from it, and over what scope. New in §5's framing decision;
   nothing owns it today.
7. ~~**Matcher timing**~~ — **closed as a gate, 2026-08-04, by measurement.**
   The standalone matcher image implements and routes with **WNS +3.537 ns
   against a 20 ns constraint** (longest routed path 16.463 ns; ≈+15.5 ns at
   the board's 32 ns), all constraints met. See the post-route table in §8.

   The HLS estimate of 6.547 ns is against a 5.000 ns target, i.e. a 200 MHz
   ambition **nothing in this pipeline has ever required** — it was never the
   number that decided whether the design works. What remains true: the
   matcher cannot be clocked at 200 MHz, and the 5 ns synthesis constraint
   stays (§8) because the headroom comes from synthesising tight and clocking
   slow.

   What is *not* closed is how high the clock can go. The measured 16.463 ns
   is not a floor — it is what a build that met its constraint with room to
   spare happened to produce, and >93% of it is routing at 3 logic levels.
   Re-implement at the target period before claiming any maximum frequency,
   and expect `correlation_core`'s partitioned `seg[]` fanout (§8) to be the
   thing to fix, not the loop arithmetic.
8. **Larger patch envelopes now have a second bound** — §3.1. Not blocking
   today (251,740 of 262,143 bytes used), listed so it is not rediscovered
   the hard way.

Everything else in this document is settled and implementable.
