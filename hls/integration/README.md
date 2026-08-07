# extractor → matcher seam

The first test of anything downstream of `patch_extract_core`'s outputs.

Both cores are verified on their own — the extractor at csim/cosim, the
matcher at csim/cosim plus a routed implementation. Neither testbench contains
the other, so neither can fail the way this one can: **the thing under test is
the PS loop between them**, and its failures are software failures.

```
../.venv/Scripts/python.exe pe_tme_generate_golden.py
vitis-run.bat --mode hls --tcl run_csim.tcl
```

## What it pins

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

## Oracles — neither is new

That is the point. Patch geometry and pixels come from
`patch_extract_generate_golden.model_validate` / `build_endpoint_patch`, the
mirror already proved bit-exact against the extractor. Scores and locations
come from `tme_generate_golden.golden`, the exact-integer oracle already proved
against cv2. This directory only composes them, so a disagreement here is a
seam defect and cannot be an oracle defect.

The page uses a non-zero stride (544 for a 512-wide image) because contract §2
says the row stride is runtime and must never be assumed equal to the width —
a seam test at `stride == img_w` would not notice a core that assumed it. Pad
bytes are the sentinel `0xA5`, which the page pattern cannot produce.

## What this is NOT

- **Not synthesised, not cosimulated, and that is deliberate.** There is no
  top function here; the project instantiates two cores that are each
  synthesised in their own directory. Cosim drives *one* top through an RTL
  wrapper and so cannot run a loop that reads a metadata record and then
  decides what to do next — and the sequencing is the test.
- **Not silicon.** The hardware half of this seam is a two-core block design
  on the board. It is not built, and **nothing in this project has run on
  silicon**. See contract §7.1 and §9.
- **Not a throughput result.** One matcher invocation per (candidate,
  template) pair is the §6.4 architecture, not a measured rate.
