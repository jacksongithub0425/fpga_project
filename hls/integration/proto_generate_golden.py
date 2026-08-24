"""
Generate golden data for board gate 5 — the multi-candidate STREAM PROTOCOL.

Run from hls/integration/ (hls/.venv python):
    python proto_generate_golden.py

WHAT GATE 4 LEFT UNTESTED

Gate 4 dispatches ONE candidate.  That is enough to prove the extractor's
geometry, its status registers and its pixel content, and it proved all three.
It cannot say anything about the part of contract §5 that only exists when a
batch has more than one member, and that part is where the driver's own
docstring admits the risk:

    meta_out   one 128-bit record per INPUT DESCRIPTOR, TLAST at BATCH end
    patch_out  pixels for VALID candidates only, TLAST on EACH patch's last
               pixel

Two streams, two different TLAST disciplines, one armed once and the other
re-armed per candidate.  With n = 1 those collapse into the same thing: one
record, one patch, one TLAST each, and every off-by-one between them is
invisible.  Specifically, none of these can fail at n = 1 —

  - records arriving in an order other than the descriptor order;
  - the metadata TLAST landing early, so the S2MM completes short and the
    records are parsed out of a buffer whose tail is stale;
  - the patch receive not re-arming between candidates;
  - the receive re-arming but at the wrong length, so a patch is sliced with
    its neighbour's geometry;
  - `static` state in the extractor surviving into the NEXT batch.

FOUR CANDIDATES, FOUR DIFFERENT PATCH SIZES

The batch is deliberately built so consecutive patches differ in size, and so
the sizes go DOWN, UP and DOWN again:

    0  interior          38 x 25 = 950 B
    1  clip-left         26 x 25 = 650 B     smaller
    2  clip-top-right    38 x 19 = 722 B     larger
    3  clip-bottom-right 22 x 16 = 352 B     smaller

A receive that re-arms at a stale length is caught by the size CHANGE, not by
the size itself; a monotonically shrinking batch would let a driver that
carried the previous length forward still "work" for the wrong reason (every
transfer would simply be over-armed).  Going up at candidate 2 removes that
excuse.  The clipping is what produces the variety, so the batch also covers
§4.5's four clip directions — left, top, right and bottom — which is the same
arithmetic the seam suite's `clipped-left` exercises once.

Every descriptor here is VALID.  That is not an oversight: `extract_candidates`
refuses by design to dispatch a descriptor `patch_extract_core` would reject,
because a rejected candidate emits no pixels and would strand the receive armed
for it.  The rejected-mid-batch case therefore cannot reach hardware from this
driver at all, and it is already covered in C simulation by
`pe_tme_generate_golden.py`'s `mid-batch-reject`.  Gate 5 tests the protocol
the driver can actually drive.

THE TEMPLATE BANK IS SHAPED FOR THE REDUCTION, NOT FOR THE EXTRACTOR

Gate 4's phase D says plainly what it could not test: "each kind gets one
trial, so `by_kind[kind]` reduces over a single element and its argmax cannot
be wrong here."  The bank below fixes exactly that:

    alpha[0]  a partial match, score well under 1.0
    alpha[1]  an exact crop of the patch, score exactly 1.0
    beta[0]   a different exact crop, also exactly 1.0

`build_trials` flattens that to the frozen order alpha0, alpha1, beta0, so:

  - `by_kind["alpha"]` must be alpha1 — the SECOND trial of its kind, which a
    reduction that kept the first (or that had degraded to "last wins") gets
    wrong in opposite directions;
  - `best` is a genuine tie between alpha1 and beta0, both exactly 1.0, so
    only the frozen order can settle it and `>=` instead of `>` hands it to
    beta;
  - reversing the bank moves `best` to beta, which is the control that proves
    the tie was settled by order and not by a float coincidence.

Both 1.0 scores are exact crops, so the tie is bit-identical by construction
rather than by luck — the same discipline gate 4 uses for alpha/beta.

ORACLES

None of them are new.  The binary page comes from `binarize_dma_checks.
cpu_golden` (the bit-exact v2.0 HLS reference), the patch geometry and pixels
from `patch_extract_generate_golden.model_validate` (the mirror already proved
bit-exact against the extractor), and every score and location from
`tme_generate_golden.golden` (the exact-integer oracle cross-checked against
cv2).  This file composes them, so a disagreement at the gate is a protocol
defect and cannot be an oracle defect.

WRITES (all five hashed into the manifest itself)

    tb_proto_gray.bin      96 x 64 uint8 page, compact
    tb_proto_bin.bin       96 x 64 binarised page
    tb_proto_patches.bin   the four patches, concatenated in batch order
    tb_proto_templs.bin    the three templates, concatenated in trial order
    tb_proto_cases.txt     manifest — see the format note above write_suite()
"""

import hashlib
import sys
from pathlib import Path

import numpy as np

# Three sibling directories, none of them a package.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "patch_extract"))
sys.path.insert(0, str(_HERE.parent / "template_match"))
sys.path.insert(0, str(_HERE.parents[1] / "sw"))

import patch_extract_generate_golden as PE      # noqa: E402
import tme_generate_golden as TME               # noqa: E402
import binarize_dma_checks as BIN               # noqa: E402

_EXPECTED_BIN_ORACLE = (_HERE.parents[1] / "sw" / "binarize_dma_checks.py")
if Path(BIN.__file__).resolve() != _EXPECTED_BIN_ORACLE.resolve():
    raise RuntimeError(
        f"imported the wrong binarizer oracle: {BIN.__file__}; expected "
        f"{_EXPECTED_BIN_ORACLE}")

# Page and pipeline configuration.  Compact stride: this is the layout the
# unchanged simple-mode S2MM produces, and the seam suite already covers a
# non-compact stride independently.
IMG_W, IMG_H = 96, 64
STRIDE = IMG_W
BUFFER_BYTES = STRIDE * IMG_H
THRESHOLD = 128

# The template envelope every descriptor carries.  §4.5 then gives
#   outward_w = (10*12)//5 = 24   inward_w = (10*7)//5 = 14   patch_h = (8*16)//5 = 25
# so an unclipped patch is 38 x 25 and the clipped ones are the sizes above.
MAX_TW, MAX_TH = 10, 8

# Best-vs-runner-up separation that lets the gate assert an EXACT location.
# Same role and value as MIN_MARGIN in the two suites next door.
MIN_MARGIN = 0.02

# The partial-match template must be clearly under 1.0 or `by_kind["alpha"]`
# would be choosing between two indistinguishable scores and the per-kind
# argmax would pass by coincidence.
ALPHA0_MAX_SCORE = 0.85

GRAY_SEED = 0x9A17
TEMPL_SEED = 0x5C0E


def require(cond, msg: str = "") -> None:
    """`assert` that survives `python -O`, for the same reason as next door:
    this generator is run under -O as part of its acceptance, and a disabled
    assertion must not be able to publish unchecked vectors."""
    if not cond:
        raise AssertionError(msg or "generator self-check failed")


def build_gray() -> np.ndarray:
    """A seeded page whose BINARISATION is rich, which is the actual need.

    Uniform noise rather than a smooth pattern: after the core's integer 3x3
    Gaussian the values land near N(127.5, 27.6), so a threshold of 128 splits
    them roughly evenly and the binary page carries structure at a two-pixel
    correlation length.  A gradient page would binarise to two half-planes,
    every crop of it would be flat or near-flat, and neither the §4.6 template
    rule nor any unique-peak assertion could be built on it.
    """
    rng = np.random.default_rng(GRAY_SEED)
    return rng.integers(0, 256, size=(IMG_H, IMG_W), dtype=np.uint8)


def descriptors():
    """The batch, in dispatch order.  Sizes go down, up, down — see the header.

    Sides alternate on purpose as well: §4.5's left and right formulas mirror
    the patch about the endpoint, and a `side` that silently defaulted to one
    of them is a defect the driver's own docstring records as having happened
    before.
    """
    return [
        PE.cand(48, 32, "left",  MAX_TW, MAX_TH, "interior"),
        PE.cand(12, 32, "left",  MAX_TW, MAX_TH, "clip-left"),
        PE.cand(70, 6,  "right", MAX_TW, MAX_TH, "clip-top-right"),
        PE.cand(88, 60, "right", MAX_TW, MAX_TH, "clip-bottom-right"),
    ]


def unique_crop(patch, rng, tag, tries=400):
    """An exact crop of `patch` whose peak is unique and at its own offset.

    Searched rather than fixed: a binary page repeats locally, and a crop that
    recurs inside the patch leaves the argmax ambiguous — at which point the
    gate could assert a score but not a location, and the absolute-box
    construction it exists to check would go unverified.
    """
    ph, pw = patch.shape
    for _ in range(tries):
        uy = int(rng.integers(0, ph - MAX_TH + 1))
        ux = int(rng.integers(0, pw - MAX_TW + 1))
        templ = patch[uy:uy + MAX_TH, ux:ux + MAX_TW].copy()
        if int(templ.min()) == int(templ.max()):
            continue                            # §4.6: flat template
        score, gx, gy, margin, _ = TME.golden(patch, templ)
        if (gx, gy) == (ux, uy) and score > 0.999 and margin >= MIN_MARGIN:
            return templ, (ux, uy), score, margin
    raise RuntimeError(
        f"{tag}: no crop of this {pw}x{ph} patch had a unique peak in {tries} "
        f"tries. Do not lower MIN_MARGIN to fix it — a lowered margin turns "
        f"every location assertion in gate 5 into a coin flip. Change "
        f"GRAY_SEED so the binary page carries more structure.")


def partial_template(patch, page_bin, rng, tag, tries=400):
    """A template that matches `patch` PARTIALLY, with a unique peak.

    Cut from elsewhere on the page rather than randomised: noise correlates
    with nothing, and a near-zero peak is decided by rounding, which is
    useless for an exact-location assert.  Another region of the same
    binarised page correlates partially and peaks somewhere specific.
    """
    ph, pw = patch.shape
    for _ in range(tries):
        sy = int(rng.integers(0, IMG_H - MAX_TH + 1))
        sx = int(rng.integers(0, IMG_W - MAX_TW + 1))
        templ = page_bin[sy:sy + MAX_TH, sx:sx + MAX_TW].copy()
        if int(templ.min()) == int(templ.max()):
            continue
        score, gx, gy, margin, _ = TME.golden(patch, templ)
        if 0.0 < score <= ALPHA0_MAX_SCORE and margin >= MIN_MARGIN:
            return templ, (gx, gy), score, margin
    raise RuntimeError(
        f"{tag}: no off-patch crop gave a unique sub-unity peak in {tries} "
        f"tries")


def build_case():
    """Compose the three established oracles into one protocol fixture."""
    gray = build_gray()
    page_bin = BIN.cpu_golden(gray, THRESHOLD)

    ink = int((page_bin == 255).sum())
    require(0.2 < ink / page_bin.size < 0.8,
            f"binary page is {100 * ink / page_bin.size:.1f}% ink — too "
            f"lopsided to cut distinguishable templates from")

    cands = []
    for i, c in enumerate(descriptors()):
        valid, reason, x0, y0, x1, y1 = PE.model_validate(c, IMG_W, IMG_H)
        pw, ph = x1 - x0, y1 - y0
        require(valid,
                f"candidate {i} ({c['tag']}) is invalid, reason 0x{reason:x} — "
                f"extract_candidates refuses to dispatch a rejected descriptor "
                f"(a rejected candidate emits no pixels and would strand the "
                f"receive armed for it), so gate 5 cannot use it")
        cands.append(dict(index=i, cand=c, x0=x0, y0=y0, pw=pw, ph=ph,
                          valid=1, reason=reason, packed=PE.pack(c),
                          patch=page_bin[y0:y1, x0:x1].copy(),
                          tag=c["tag"]))

    # The four sizes must actually differ, and must not be monotonic.  This is
    # the property the whole batch is built for, so it is asserted rather than
    # left to the reader to confirm from the numbers.
    sizes = [c["pw"] * c["ph"] for c in cands]
    require(len(set(sizes)) == len(sizes),
            f"patch sizes {sizes} are not all distinct — a receive that "
            f"re-armed at a stale length would survive this batch")
    require(any(b > a for a, b in zip(sizes, sizes[1:])),
            f"patch sizes {sizes} only ever shrink; a driver that carried the "
            f"previous length forward would over-arm every transfer and pass")
    require(len({(c["pw"], c["ph"]) for c in cands}) == len(cands),
            "two candidates share a patch geometry")

    # Templates are cut against candidate 0's patch, which is the one the
    # reduction phase runs on.
    base = cands[0]["patch"]
    rng = np.random.default_rng(TEMPL_SEED)
    a1_t, a1_at, a1_s, a1_m = unique_crop(base, rng, "alpha1")
    b0_t, b0_at, b0_s, b0_m = unique_crop(base, rng, "beta0")
    require(a1_at != b0_at, "alpha1 and beta0 are the same crop")
    a0_t, a0_at, a0_s, a0_m = partial_template(base, page_bin, rng, "alpha0")

    templs = [
        dict(index=0, kind="alpha", base_index=0, pixels=a0_t, tag="alpha0",
             score=a0_s, at=a0_at, margin=a0_m),
        dict(index=1, kind="alpha", base_index=1, pixels=a1_t, tag="alpha1",
             score=a1_s, at=a1_at, margin=a1_m),
        dict(index=2, kind="beta",  base_index=0, pixels=b0_t, tag="beta0",
             score=b0_s, at=b0_at, margin=b0_m),
    ]

    # The reduction the gate will assert, derived here so the gate compares
    # hardware against a file rather than against its own recomputation.
    require(a1_s == b0_s,
            f"alpha1 scores {a1_s!r} and beta0 {b0_s!r} — not bit-identical, "
            f"so `best` is not a tie and the strict-> rule is untested")
    require(a0_s < a1_s,
            f"alpha0 ({a0_s:.6f}) does not score below alpha1 ({a1_s:.6f}); "
            f"the per-kind argmax would pass whichever way it reduced")
    require(templs[1]["index"] > templs[0]["index"],
            "the better alpha trial must come SECOND or a reduction that "
            "keeps the first would still pass")

    return dict(gray=gray, bin=page_bin, cands=cands, templs=templs,
                best_templ=1, by_kind={"alpha": 1, "beta": 2})


def _sha256(blob) -> str:
    return hashlib.sha256(bytes(blob)).hexdigest()


def write_suite(case, out=Path(".")):
    """Manifest format — fixed numeric fields first, strings last.

        PROTO  img_w img_h stride buffer_bytes threshold max_tw max_th
               n_cands n_templs patch_blob_bytes templ_blob_bytes
        CAND   index packed_hex ep_x ep_y side_code valid reason_hex
               x0 y0 pw ph patch_off tag
        TEMPL  index kind base_index tw th templ_off score local_x local_y
               page_x page_y margin tag
        BEST   best_templ_index
        BYKIND kind templ_index
        SHA256 filename hex

    The SHA256 rows cover the four .bin blobs, so the gate can verify its
    vectors from the manifest alone; the manifest's own hash lives in
    sw/GATE5_VECTORS.sha256 alongside them.
    """
    patch_blob, templ_blob = bytearray(), bytearray()
    cand_rows, templ_rows = [], []

    for c in case["cands"]:
        cand_rows.append(
            f"CAND {c['index']} {c['packed']:016x} {c['cand']['ep_x']} "
            f"{c['cand']['ep_y']} {c['cand']['side_code']} {c['valid']} "
            f"{c['reason']:04x} {c['x0']} {c['y0']} {c['pw']} {c['ph']} "
            f"{len(patch_blob)} {c['tag']}")
        patch_blob += c["patch"].tobytes()

    x0, y0 = case["cands"][0]["x0"], case["cands"][0]["y0"]
    for t in case["templs"]:
        th, tw = t["pixels"].shape
        ux, uy = t["at"]
        templ_rows.append(
            f"TEMPL {t['index']} {t['kind']} {t['base_index']} {tw} {th} "
            f"{len(templ_blob)} {t['score']:.6f} {ux} {uy} {x0 + ux} "
            f"{y0 + uy} {min(t['margin'], 999.0):.6f} {t['tag']}")
        templ_blob += t["pixels"].tobytes()

    blobs = {
        "tb_proto_gray.bin": case["gray"].tobytes(),
        "tb_proto_bin.bin": case["bin"].tobytes(),
        "tb_proto_patches.bin": bytes(patch_blob),
        "tb_proto_templs.bin": bytes(templ_blob),
    }
    for name, blob in blobs.items():
        (out / name).write_bytes(blob)

    header = (f"PROTO {IMG_W} {IMG_H} {STRIDE} {BUFFER_BYTES} {THRESHOLD} "
              f"{MAX_TW} {MAX_TH} {len(case['cands'])} {len(case['templs'])} "
              f"{len(patch_blob)} {len(templ_blob)}")
    lines = [header] + cand_rows + templ_rows
    lines.append(f"BEST {case['best_templ']}")
    for kind, idx in case["by_kind"].items():
        lines.append(f"BYKIND {kind} {idx}")
    for name, blob in blobs.items():
        lines.append(f"SHA256 {name} {_sha256(blob)}")

    path = out / "tb_proto_cases.txt"
    # LF, explicitly: this manifest is hashed into sw/GATE5_VECTORS.sha256 and
    # copied to a Linux board, and a record that only verifies on the machine
    # that wrote it verifies nothing about the copy that runs.
    path.write_text("\n".join(lines) + "\n", newline="\n")
    return path, blobs


def main() -> int:
    TME.require_generic_opencv()
    case = build_case()
    out = Path(".")
    path, blobs = write_suite(case, out)

    print(f"page {IMG_W}x{IMG_H} stride {STRIDE}, threshold {THRESHOLD}, "
          f"{int((case['bin'] == 255).mean() * 100)}% ink")
    print(f"{len(case['cands'])} candidates (all valid), "
          f"{len(case['templs'])} templates")
    for c in case["cands"]:
        print(f"  [{c['index']}] {c['tag']:<18s} ep "
              f"({c['cand']['ep_x']},{c['cand']['ep_y']}) "
              f"{'left ' if c['cand']['side_code'] == 0 else 'right'}  patch "
              f"{c['pw']}x{c['ph']} @({c['x0']},{c['y0']}) = "
              f"{c['pw'] * c['ph']} B")
    print(f"  patch sizes in dispatch order: "
          f"{[c['pw'] * c['ph'] for c in case['cands']]}")
    for t in case["templs"]:
        th, tw = t["pixels"].shape
        ux, uy = t["at"]
        print(f"  templ {t['index']} {t['tag']:<8s} {t['kind']:<6s} "
              f"{tw}x{th}  score {t['score']:+.6f} at local ({ux},{uy}) "
              f"margin {t['margin']:.4f}")
    print(f"  reduction: best = templ {case['best_templ']}, "
          f"by_kind = {case['by_kind']}")
    print(f"\nwrote {path.name} + {len(blobs)} blobs "
          f"({sum(len(b) for b in blobs.values())} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
