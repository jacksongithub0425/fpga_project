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
because the geometry is transmitted, not re-derived.

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

**Status: adopted architecture, not an implemented system.** Nothing below
except §7.1.2 describes code that exists. Today `sw/tme_driver.py` still talks
to a single `self._ctrl` window at the old `0x00`–`0x4C` offsets, and
`patch_extract_core` is the **only** core that currently presents the coherent
one-slave interface this section requires — `binarize_core`,
`template_match_core` and `class_score_core` have not been checked, let alone
fixed, and the same `offset=slave` trap that split the extractor three ways
applies to every one of them that takes an `m_axi` pointer. Treat §7.1.1 as a
work list, not a description.

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

**Timing.** Re-measured after the two changes the previous revision was
waiting on — the `m_axi` conversion and the §3 narrowing to 11/9-bit patch
counters — both of which have landed. `patch_extract_core` now estimates
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

**Still do not optimise this yet** — but for a different reason than before.
The blocker is no longer "pending changes will move the path", it is that
4.815 ns is an estimate with the uncertainty allowance already spent, and only
place-and-route can say whether that matters. Take this to implementation and
read real slack before touching the loops.

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
| `patch_extract_core` | `m_axi` pointer + explicit stride + address arithmetic; 16-bit page coords; 11/9-bit patch counters; §4 validation with wide-type overflow checks (§2.1); metadata stream (§6.2); per-patch pixel `TLAST`; `NUM_CANDS`; status registers |
| `template_match_core` | result-dimension off-by-one (§4.4) — **done**: `+1` in *both* `tme_top.cpp` and `correlation_core.cpp`, `MAX_RESULT_W/H` = `MAX_PATCH − 4 + 1` = 817/304. `MAX_PATCH` narrowed to the exact 820 × 307 envelope (§3) — **done**, 224 BRAM18K. **Remaining: timing** (6.978 ns vs a 3.650 ns effective budget, −3.328 ns — it misses even the raw 5 ns target, so this is desktop work, not a place-and-route question); **a golden/TB that actually validates it** (`tme_tb.cpp` checks score only against a `0.0 @ (0,0)` golden, which an always-zero DUT passes — needs location assertions, a unique nonzero match, the final row/column, patch==template equality, and the maximum-storage case); the generator's `int()`-vs-`round()` oracle drift (§4.5); and consuming per-patch framing and transmitted geometry |
| `class_score_core` | parked. D1/D2 are repairable now (reorder flush-before-merge, §5.1); D6/D7/D8 and the per-kind-score/match-location gaps wait on §6.3 |
| `sw/tme_driver.py` | buffer sizes per §2.2; stride-aware `suppress_text()` (§2.1); `buffer_bytes` register width; `NUM_CANDS`; result unpack per §6.3; enforce §4.1 and §4.5 before dispatch |
| template pipeline | `max_tw` / `max_th` from post-round template dimensions (§4.5) |

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
7. **Matcher timing** — 6.978 ns against a 3.650 ns effective budget
   (−3.328 ns) and over even the raw 5.000 ns period. Unlike the extractor's
   −1.165 ns, this cannot be deferred to place-and-route: an estimate above
   the target period does not close by routing. Desktop work, and it gates
   integration.

Everything else in this document is settled and implementable.
