# integration C simulation

This directory runs two separate checks in one C-simulation project. They are
separate on purpose: the newer three-stage case adds the binarizer boundary
without weakening or relabelling the older extractor → matcher seam.

| phase | what it establishes |
|---|---|
| extractor → matcher seam | the PS join between `patch_extract_core`'s metadata/pixel streams and `template_match_core`, including rejection, clipping and two-cursor discipline |
| binarizer → extractor → matcher C/golden | one compact logical raster passes byte-exactly through all three C models and produces the pinned metadata, patch and matcher result |

The cores are also verified separately. The extractor has csim/cosim evidence;
the matcher has csim/cosim and standalone silicon evidence. Neither standalone
testbench contains the PS-controlled joins below.

```
../.venv/Scripts/python.exe pe_tme_generate_golden.py
vitis-run.bat --mode hls --tcl run_csim.tcl
```

Recorded 2026-08-09 with Vitis HLS 2025.2: the generator passed both normal
Python and `python -O` self-check runs under NumPy 2.5.1 / OpenCV 5.0.0. Vitis
then reported `CSim done with 0 errors`; CSim elapsed 15 s and the complete
`vitis-run` elapsed 19 s. Those are host run times, not datapath throughput.

## Extractor → matcher seam — preserved

`patch_extract_core` emits two streams that do not run at the same rate:

| stream | one entry per |
|---|---|
| `meta_out` | **input descriptor** — valid or not |
| `patch_out` | **valid candidate** — rejected ones emit zero pixel beats |

So record *i* is not patch *i*, and the PS keeps two cursors. A loop that
advances them together is correct on every batch in which nothing is rejected
— which is every batch anyone writes by hand — and permanently misaligned on
the first one that isn't. Nothing downstream notices: the geometry is
well-formed, the score is a plausible number, no status bit is set, and the
answer belongs to a different candidate.

The four manifest cases, one hazard each:

| case | pins |
|---|---|
| `seam-baseline` | the join at all: patch 152×96 at (104,102), template cut from it, score 1.0 at a known offset |
| `mid-batch-reject` | the cursor. A rejected descriptor **between two valid ones**, so a per-record cursor has somewhere to go wrong |
| `clipped-left` | geometry comes from the **record**. Clipped at the page edge, its patch is 106 px wide where the §4.5 formula says 152 |
| `offset-template` | a score that is neither 0.0 nor 1.0 (0.9609) crossing the seam, and page-vs-patch coordinate rebasing at a non-zero origin |

Plus, on every valid case: `TLAST` must land exactly on beat
`patch_w * patch_h`. `tme_top` ignores `TLAST` entirely and reads the count it
was told, so a framing disagreement is **silent in the matcher** and shows up
as corruption in the *next* patch.

## The negative controls

Every assertion above says the right inputs give the right answer. None of
them says a wrong input would have been *caught* — and a suite whose cases all
pass under the bug it was written for is decoration. So both PS bugs are
performed deliberately on `clipped-left` and required to produce a wrong
answer:

```
[PASS] negative control (geometry): re-deriving §4.5 instead of reading the
       record gives score 0.612614 at page (76,164), not the golden 1.000000
       at (40,129) — the bug is detectable
[PASS] negative control (cursor): advancing the pixel cursor once per RECORD
       gives score 0.238917 at page (60,103), not the golden 1.000000 at
       (40,129) — the bug is detectable
```

If either ever prints `[FAIL]`, the corresponding case above is measuring
nothing and this directory should not be believed about it.

## Reading the result

```
SEAM TEST PASSED (0 errors): 4 descriptors, 3 matcher runs, 2 injected-bug controls
```

Quote it that way. The suite prints seven `[PASS]` lines, but seven is a count
of *printed lines* — it is not a case count and "7/7" would imply a denominator
that does not exist. Three descriptors reach the matcher because the fourth is
rejected, which is the whole point of the batch.

## Compact binarizer → extractor → matcher case

The second phase is one deliberately small, exact case:

| quantity | value |
|---|---|
| grayscale page | 24×20, 480 bytes |
| threshold | 140 |
| binary DDR layout | compact logical row-major, `stride_bytes = 24`, `buffer_bytes = 480` |
| descriptor | left endpoint `(12,10)`, maximum template 4×4, packed `0x00040010000a000c` |
| metadata | valid, patch 14×12 at logical origin `(3,4)` |
| template | raw, non-flat 4×4 bytes |
| matcher result | score `+1.000000`, local `(4,1)`, rebased page coordinate `(7,5)` |

The grayscale formula is deterministic,
`(53*r + 29*c + 7*r*c + 172) & 255`, with one pinned pixel override. It includes
an arithmetic witness whose Gaussian sum is 2248: the HLS rule `2248 >> 4`
produces 140 and therefore binary 255 at threshold 140, while rounded division
would produce 141 and binary 0. The legacy raw-layout mutation is also required
to fail: it changes 53 of the 168 patch bytes and moves the peak from local
`(4,1)` to `(5,2)`. The correct peak clears its runner-up by 0.622036.

The phase requires all 480 binarizer output bytes and sidebands to agree, the
final logical row and column to be zero, the extractor's complete §6.2 record
and 168 patch bytes to agree, and both matcher score and exact location to
agree. Matcher geometry comes from the emitted metadata record, never from
re-deriving the descriptor formula.

The generator keeps the original `tb_pe_tme_*` seam vectors intact and writes
the compact phase to a separate namespace:
`tb_bpe_tme_gray.bin`, `tb_bpe_tme_bin.bin`, `tb_bpe_tme_patch.bin`,
`tb_bpe_tme_templs.bin` and `tb_bpe_tme_cases.txt`. The BPE manifest extends
the header with the threshold and the three byte counts; its descriptor row is
the same §6.2/21-field ABI already consumed by the preserved seam.

The recorded three-stage summary is separate from the seam summary:

```
THREE-STAGE C/GOLDEN PASSED (0 errors): 480 gray beats -> 480 logical bytes -> 168 patch beats -> matcher local (4,1), page (7,5); 1 injected-layout control
```

After the preserved seam also passed, the run closed with:

```
INTEGRATION C/GOLDEN PASSED: three-stage errors=0, seam errors=0
```

This one-valid-descriptor case does not replace the seam's rejected descriptor,
clipped geometry, non-compact stride or injected-bug controls.

## Oracles

That is the point. Patch geometry and pixels come from
`patch_extract_generate_golden.model_validate` / `build_endpoint_patch`, the
mirror already proved bit-exact against the extractor. Scores and locations
come from `tme_generate_golden.golden`, the exact-integer oracle already proved
against cv2. This directory only composes them, so a disagreement here is a
seam defect and cannot be an oracle defect.

The three-stage phase adds the binarizer's exact integer model: the 3×3
weighted sum is divided with truncating `>> 4`, thresholding is
`blurred <= threshold`, and the output model includes the logical-coordinate
shift and mandatory zero final row/column. Stock `cv2.GaussianBlur` is not its
golden because OpenCV rounds at the division boundary.

The original seam page uses a non-zero stride (544 for a 512-wide image)
because the extractor's contract permits runtime stride and must never assume
it equals the width. Pad bytes are the sentinel `0xA5`, which the page pattern
cannot produce. The three-stage page is intentionally different:
`binarize_core` emits exactly `img_w * img_h` compact logical beats and the
current simple-mode S2MM stores them unchanged, so its faithful DDR boundary is
`stride_bytes == img_w`. Padding that phase in the testbench would invent a
row-repacking writer that the block design does not contain.

## What this is NOT

- **Not synthesised or cosimulated as a combined core, and that is
  deliberate.** There is no combined top function here. C simulation links
  the live sources from all three core directories and models the PS/DDR
  staging between calls. Cosim drives one top through one RTL wrapper and
  cannot execute that software decision loop.
- **Not a direct hardware stream.** The binarizer result is materialised at the
  compact DDR boundary before the extractor reads it; extractor pixels are
  inspected and re-staged before the matcher is invoked from metadata geometry.
  No claim is made for a direct binarizer→extractor or extractor→matcher wire.
- **Not a combined block design, DMA run or silicon result.** No three-core BD
  has been built or routed and no combined transfer has run on the board. The
  matcher's standalone 9/9 silicon result (contract §8) belongs to a different,
  one-core image and does not establish either join.
- **Not a throughput result.** One matcher invocation per (candidate,
  template) pair is the §6.4 architecture, not a measured rate.
