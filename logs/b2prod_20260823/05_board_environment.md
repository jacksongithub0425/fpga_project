# Board environment — measured 2026-08-23, same boot as gates 01–04

Two findings from the live board that change Stage 2 planning. Both were
measured, not inferred. Nothing was installed and nothing was changed.

## 1. PyMuPDF is absent, and not practically installable

```
arch          : armv7l          (32-bit ARM)
python        : 3.10.4  /usr/local/share/pynq-venv/bin/python3
fitz/pymupdf  : MISSING
cv2           : 4.5.4
numpy         : 1.21.5
cores         : 2
RAM           : 491 MiB total, 325 MiB available, 511 MiB swap
disk          : 21 G free
```

`pip download pymupdf` reaches PyPI but finds **no armv7l wheel** and falls
back to `pymupdf-1.28.2.tar.gz` (**87.9 MB**), which would have to build MuPDF
from source on two cores and 491 MiB. That was aborted; nothing was installed
and `/tmp/pmtest` was removed.

`detect_page()` needs `fitz` for three *inputs*, not just for drawing:
`render_page`, `extract_words` (text suppression and labels) and
`extract_horizontal_segments_vector` (the vector segments candidates are built
from). Geometry emission is already solved — the board writes JSON and the
drawing happens off-board — but rendering, words and segments are not.

### What apt offers, and why it is not a free win

```
python3-fitz   Candidate: 1.19.2+ds1-1ubuntu1
mupdf-tools    Candidate: 1.19.0+ds1-2
libmupdf-dev   Candidate: 1.19.0+ds1-2
```

A binding exists — but against **MuPDF 1.19**, where every renderer parity
result in `logs/b2prod_20260822/` was produced against **PyMuPDF 1.28.0 /
MuPDF 1.29**. A different rasteriser is a different grey page, and the grey
page is the input to the truncating blur, Otsu, the binary, the patches and
every score. Installing it would silently invalidate:

* the native-grayscale rejection (0/36 byte-identical),
* the striped-conversion proof (36/36 byte-identical in both modes),
* `rendered_shape()`'s pixmap agreement on all 36 pages,
* the 36/36 rung-P prediction, which was computed on 1.28.0 renders.

None of that is a reason it cannot be used — it is a reason the oracle would
have to be re-based on the same version first, and the corpus evidence
regenerated.

## 2. OpenCV 4.5.4 (board) vs 5.0.0 (dev): `matchTemplate` differs by 1 ULP

Same inputs on both sides, verified by hashing them:

```
INPUT rgb sha256   : fe2756261addaf47d1de64076bbfbc5ca4b167c6485341f1cd6fa840ac9e25e2   (both)
INPUT patch sha256 : b4db57e057ed62f72be7d0e7becde556dfbb1155453e61ccc73287bfc3e05f0e   (both)

                      board cv2 4.5.4                     dev cv2 5.0.0
BGR2GRAY sha256       6eaaabf1d9f7d5a3...                 6eaaabf1d9f7d5a3...      IDENTICAL
RGB2GRAY == 2-step    True                                True                     IDENTICAL
OTSU threshold        127                                 127                      IDENTICAL
matchTemplate max     1.000000000                         0.999999881              DIFFERS
result sha256         14e614af4e17af84...                 8aa2d4c2406f2e71...      DIFFERS
```

The difference is **~1.19e-7**, one float32 ULP near 1.0 — a changed
summation/SIMD path, not a semantic change.

**What it can and cannot reach.** `pl-all` runs initial matching on the
fabric, so `cv2.matchTemplate` on the board is used **only by host
refinement** (`best_template_match_local(prefer_local_alignment=True)`) — 26
calls / 208 correlations on the Stage 3 page, 117 refine calls corpus-wide.

* **Scores**: 1.2e-7 against a 0.005 tolerance. Cannot matter.
* **Locations**: must be EXACT, and a 1-ULP difference can flip an argmax
  where two positions are genuinely tied. The tie rule is strict `>`, first in
  row-major order, so a pair that is exactly equal on one version and 1 ULP
  apart on the other can pick a different winner.

So the residual risk is: a refined box moving on a near-tie, between the
off-board `cpu-production` oracle (5.0.0) and board-local refinement (4.5.4).
That is exactly the kind of single-page difference that would otherwise be
filed against silicon.

**The good news** is that the two functions the binariser depends on —
`BGR2GRAY` and Otsu — are bit-identical across the two versions, so the
renderer and threshold work is unaffected by the version gap.

## Bearing on the sequence

Steps 1 and 2 are complete and passed (`03_script_identity.txt`,
`04_counted_clock.txt`). Step 3's **instrumentation** is unaffected by either
finding — VmHWM / MemAvailable / CmaFree / VmRSS at checkpoints is the right
design regardless. What is blocked is running the whole-pipeline memory gate
**on a real page on the board**, because the board cannot render one.
