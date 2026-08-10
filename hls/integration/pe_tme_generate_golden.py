"""
Generate golden data for pe_tme_tb.cpp — the extractor -> matcher seam.

Run from hls/integration/ (hls/.venv python):
    python pe_tme_generate_golden.py

This is the FIRST test of anything downstream of `patch_extract_core`'s
outputs.  Both cores are separately verified; what has never been executed is
the join, and the join is not a wire — it is PS software, so the failures
available here are software failures that neither core's own testbench can
produce.  Contract §7.1 names the gap; this closes the C-simulation half of
it.  (The other half is silicon, and nothing has run on silicon yet.)

WHAT THE SEAM ACTUALLY IS

`patch_extract_core` emits two streams that do NOT run at the same rate:

  meta_out   one 128-bit record per INPUT DESCRIPTOR, valid or not
  patch_out  pixels for VALID candidates ONLY, TLAST on each patch's last
             pixel

So record i does not correspond to patch i.  A PS loop that advances both
cursors together desynchronises permanently at the first rejected descriptor,
and every match after it is computed against the wrong patch while every
register read still looks healthy — right shapes, plausible scores, no error
bit anywhere.  `mid-batch-reject` below exists to make that failure a test
failure.

Three more things the PS must get right, one case each:

  clipped-left      the matcher's patch_w/patch_h must come from the METADATA
                    RECORD, not from re-deriving the §4.5 formula on the
                    descriptor.  Near a page edge the extractor clips, and
                    this candidate's patch is 106 px wide where the unclipped
                    formula says 152.  Feed the formula's number and the
                    matcher reads 46 pixels per row too many, consuming the
                    next patch's data.
  offset-template   the score is not always 1.0 across the seam, and the
                    result the PS reports is in PAGE coordinates: the matcher
                    returns (u, v) inside the patch and the PS must add the
                    record's (x0, y0).  Getting this wrong is invisible on any
                    case whose patch starts at the origin, so no case here
                    does.
  framing           TLAST from the extractor must land exactly on beat
                    patch_w*patch_h.  `tme_top` ignores TLAST entirely and
                    reads the count it was told, so a disagreement is silent
                    in the matcher and shows up as corruption in the NEXT
                    patch.  The TB checks the position of every TLAST.

ORACLES

Neither is new; that is the point.  Patch geometry and pixels come from
`patch_extract_generate_golden.model_validate` / `build_endpoint_patch`, the
mirror already proved bit-exact against the extractor.  Scores and locations
come from `tme_generate_golden.golden`, the exact-integer oracle already
proved against cv2.  This file only composes them, so a disagreement here is
a seam defect and cannot be an oracle defect.

Writes:
    tb_pe_tme_image.bin    img_w x img_h row-major uint8 page (compact; the
                           TB lays it into a strided buffer itself)
    tb_pe_tme_templs.bin   template pixel blob, concatenated, raw uint8
    tb_pe_tme_cases.txt    manifest; header then one row per DESCRIPTOR

Manifest header:
    n_cands img_w img_h stride_bytes buffer_bytes n_templ templ_blob_bytes
Row (fixed numeric fields first, strings last, so a C++ reader can scan):
    index packed_hex last valid reason_hex x0 y0 patch_w patch_h
    templ_id templ_off templ_w templ_h score page_x page_y local_x local_y
    margin category tag
Invalid rows carry templ_id = -1 and zeros for every matcher field; they
still carry x0/y0/patch_w/patch_h, because the record reports geometry even
when valid=0 and the TB checks that too.

The same run also writes a separate, deliberately small three-stage case:

    tb_bpe_tme_gray.bin     24x20 grayscale input to binarize_core
    tb_bpe_tme_bin.bin      exact compact logical binarizer golden
    tb_bpe_tme_patch.bin    exact 14x12 extractor patch golden
    tb_bpe_tme_templs.bin   one literal raw 4x4 matcher template
    tb_bpe_tme_cases.txt    one descriptor, using the row schema above

Its header preserves the seven PE->TME fields above, then appends:

    threshold gray_blob_bytes bin_blob_bytes patch_blob_bytes

This is a separate manifest because the PE->TME suite has one global 512x384
pre-binarized page and load-bearing rejected-descriptor ordering.  Folding a
24x20 grayscale case into that manifest would either lie about the global page
or destroy the cursor-skew coverage that suite exists to provide.
"""

import hashlib
import sys
from pathlib import Path

import numpy as np

# Both generators live in sibling directories and neither is a package.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "patch_extract"))
sys.path.insert(0, str(_HERE.parent / "template_match"))
sys.path.insert(0, str(_HERE.parents[1] / "sw"))

import patch_extract_generate_golden as PE      # noqa: E402
import tme_generate_golden as TME               # noqa: E402
import binarize_dma_checks as BIN                # noqa: E402

_EXPECTED_BIN_ORACLE = (_HERE.parents[1] / "sw" / "binarize_dma_checks.py")
if Path(BIN.__file__).resolve() != _EXPECTED_BIN_ORACLE.resolve():
    raise RuntimeError(
        f"imported the wrong binarizer oracle: {BIN.__file__}; expected "
        f"{_EXPECTED_BIN_ORACLE}")

# A page big enough to hold an unclipped 152x96 patch with room around it,
# small enough that csim stays seconds.  Stride deliberately EXCEEDS img_w:
# contract §2 says the row stride is runtime and must never be assumed equal
# to the width, and a seam test that used stride == img_w would not notice a
# core that assumed it.
IMG_W, IMG_H = 512, 384
STRIDE = 544
BUFFER_BYTES = STRIDE * IMG_H

# §4.5 geometry for these descriptors, so the numbers below are readable:
#   tw_2fifths = (2*40)//5 = 16
#   outward_w  = 2*40 + 16 = 96      inward_w = 40 + 16 = 56
#   patch_h    = 3*30 + 30//5 = 96
# left  : x0 = ep_x - 96, x1 = ep_x + 56   -> pw = 152 unclipped
# y     : y0 = ep_y - 48, y1 = y0 + 96     -> ph = 96
MAX_TW, MAX_TH = 40, 30

# The best-vs-runner-up separation that lets the TB assert an EXACT location
# instead of a neighbourhood.  Same role as MIN_MARGIN in tme_generate_golden.
MIN_MARGIN = 0.02

# Separate binarize -> extractor -> matcher case.  Compact stride is
# deliberate: it is the layout the unchanged simple-mode S2MM produces.  The
# PE -> TME suite above already covers a non-compact stride independently.
BPE_IMG_W, BPE_IMG_H = 24, 20
BPE_STRIDE = BPE_IMG_W
BPE_BUFFER_BYTES = BPE_STRIDE * BPE_IMG_H
BPE_THRESHOLD = 140
BPE_TW = BPE_TH = 4

# Fixed hashes make oracle drift fatal even under ``python -O``.  In
# particular, generating the literal template by cropping the DUT output
# would let the retired raw-layout shift follow itself through the matcher and
# turn this case into a tautology.
BPE_HASHES = {
    "gray":  "ede44a0efa7757102f18bd583983d4b262a6e5f7141bd5a13302d8be555573b3",
    "bin":   "118ac39fb5cfed72a7e024cddee9045ae43157710600e0772f91fe50bd829a57",
    "patch": "3a97be1698cea0fc1805683ccba6f2e62b4ed1742196023b561e7b9e5ba47c0d",
    "templ": "f00dc88ec2407e1e83e7a0c43949506f46f526b586c2bcc7c2990c35b5d24f8f",
}
BPE_CASES_SHA256 = (
    "ed90565eec56a4aa816d7ba12f64a146cabb95b58548254f304d65ba111a740b")


def descriptors():
    """The batch, in the order the PS will feed it.

    Order is load-bearing: `mid-batch-reject` sits BETWEEN two valid
    candidates so the cursor bug it targets has somewhere to go wrong, and
    `clipped-left` comes after it so a single test failure distinguishes
    "skipped the reject" from "used the wrong geometry".
    """
    return [
        # 1. Plain valid candidate, patch fully inside the page, template cut
        #    from within it.  The baseline: if this fails nothing else means
        #    anything.
        PE.cand(200, 150, "left", MAX_TW, MAX_TH, "seam-baseline"),
        # 2. INVALID: ep_x past the right edge (reason bit 0).  Emits a
        #    metadata record and ZERO pixel beats.
        PE.cand(IMG_W + 4, 150, "left", MAX_TW, MAX_TH, "mid-batch-reject",
                category=PE.EXTRACTOR_EDGE),
        # 3. Valid but CLIPPED at the left edge: x0 clamps to 0 and patch_w
        #    comes out 106, not the formula's 152.
        PE.cand(50, 150, "left", MAX_TW, MAX_TH, "clipped-left"),
        # 4. Valid, and its template is NOT the crop under the peak — the
        #    score crosses the seam as something other than 0.0 or 1.0.
        PE.cand(300, 250, "right", MAX_TW, MAX_TH, "offset-template"),
    ]


def pick_template(image, x0, y0, pw, ph, tag, rng):
    """A template that has a UNIQUE peak inside this patch.

    Cut from the patch itself, so the correct answer is a known offset and the
    peak is exactly 1.0.  The offset is searched rather than fixed: this page
    is a deterministic pattern, and a crop that happens to repeat inside the
    search window would leave the argmax ambiguous — at which point the TB
    could not assert a location, only a score, and the coordinate rebasing
    this file exists to test would go unchecked.  Rejecting on `margin`
    instead of hoping is the same discipline `solve()` uses next door.
    """
    patch = image[y0:y0 + ph, x0:x0 + pw]
    for attempt in range(64):
        uy = int(rng.integers(0, ph - MAX_TH + 1))
        ux = int(rng.integers(0, pw - MAX_TW + 1))
        templ = patch[uy:uy + MAX_TH, ux:ux + MAX_TW].copy()
        score, gx, gy, margin, _ = TME.golden(patch, templ)
        if (gx, gy) == (ux, uy) and margin >= MIN_MARGIN and score > 0.999:
            return templ, score, gx, gy, margin
    raise RuntimeError(
        f"{tag}: no template offset in {pw}x{ph} gave a unique peak after 64 "
        f"tries (best margin under {MIN_MARGIN}). The page pattern must have "
        f"become periodic inside a patch — fix pattern_block, do not lower "
        f"the margin, because a lowered margin silently converts every "
        f"location assertion in this suite into a coin flip.")


def offset_template(image, x0, y0, pw, ph, tag):
    """A template cut from ELSEWHERE on the page: a real, non-unity score.

    Deliberately not random noise.  A noise template scores near zero against
    everything, and near-zero peaks are exactly where the argmax is decided by
    rounding — useless for an exact-location assert.  Another region of the
    same pattern correlates partially and peaks somewhere specific.
    """
    patch = image[y0:y0 + ph, x0:x0 + pw]
    for dy, dx in ((0, 0), (60, 40), (120, 80), (180, 120), (240, 160)):
        sy, sx = (y0 + ph + dy) % (IMG_H - MAX_TH), (x0 + pw + dx) % (IMG_W - MAX_TW)
        templ = image[sy:sy + MAX_TH, sx:sx + MAX_TW].copy()
        score, gx, gy, margin, _ = TME.golden(patch, templ)
        if margin >= MIN_MARGIN and score < 0.999:
            return templ, score, gx, gy, margin
    raise RuntimeError(f"{tag}: no off-patch template gave a unique sub-unity peak")


def _sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def build_bpe_case():
    """Compose the established binarizer, extractor and matcher oracles.

    Every check is an explicit raise rather than an assert: this function is
    run under ``python -O`` as an acceptance gate, so a disabled assertion
    must not be able to publish unchecked vectors.
    """
    yy, xx = np.mgrid[0:BPE_IMG_H, 0:BPE_IMG_W]
    gray = ((53 * yy + 29 * xx + 7 * yy * xx + 172) & 0xFF).astype(np.uint8)
    if int(gray[3, 11]) != 0x71:
        raise RuntimeError(
            f"BPE gray formula drifted at (3,11): got 0x{gray[3, 11]:02x}, "
            "expected the pre-witness value 0x71")
    gray[3, 11] = 0x79

    # Use the board suite's exact v2.0 HLS oracle rather than adding another
    # copy of the Gaussian/truncation/layout arithmetic here.
    bin_image = BIN.cpu_golden(gray, BPE_THRESHOLD)

    c = PE.cand(12, 10, "left", BPE_TW, BPE_TH,
                "logical-layout-shift", category="three-stage")
    valid, reason, x0, y0, x1, y1 = PE.model_validate(
        c, BPE_IMG_W, BPE_IMG_H)
    pw, ph = x1 - x0, y1 - y0
    packed = PE.pack(c)

    # Literal, not a crop of observed output.  It happens to equal the exact
    # CPU-golden crop at local (4,1); keeping the bytes literal is what makes a
    # DUT storage shift move the peak instead of moving the template with it.
    templ = np.array([
        [255, 255,   0,   0],
        [255, 255, 255,   0],
        [  0,   0,   0, 255],
        [255,   0,   0, 255],
    ], dtype=np.uint8)
    patch = np.ascontiguousarray(bin_image[y0:y1, x0:x1])
    score, ux, uy, margin, _ = TME.golden(patch, templ)

    # ---- frozen input/geometry/output ---------------------------------
    if not valid or reason != 0:
        raise RuntimeError(
            f"BPE descriptor must be valid with reason 0, got "
            f"valid={valid} reason=0x{reason:x}")
    if packed != 0x00040010000A000C:
        raise RuntimeError(
            f"BPE packed descriptor drifted: 0x{packed:016x}")
    if (x0, y0, x1, y1, pw, ph) != (3, 4, 17, 16, 14, 12):
        raise RuntimeError(
            "BPE geometry drifted: "
            f"box=({x0},{y0})..({x1},{y1}) patch={pw}x{ph}")
    if gray.shape != (20, 24) or gray.size != 480:
        raise RuntimeError(f"BPE gray shape drifted: {gray.shape}")
    if bin_image.shape != (20, 24) or bin_image.size != 480:
        raise RuntimeError(f"BPE binary shape drifted: {bin_image.shape}")
    if patch.shape != (12, 14) or patch.size != 168:
        raise RuntimeError(f"BPE patch shape drifted: {patch.shape}")
    if templ.shape != (4, 4) or templ.size != 16:
        raise RuntimeError(f"BPE template shape drifted: {templ.shape}")
    if not np.array_equal(templ, patch[1:5, 4:8]):
        raise RuntimeError(
            "literal BPE template no longer equals the exact golden crop at "
            "local (4,1)")
    if (int(np.count_nonzero(bin_image)), int(np.count_nonzero(patch)),
            int(np.count_nonzero(templ))) != (279, 115, 8):
        raise RuntimeError(
            "BPE nonzero counts drifted: "
            f"page={np.count_nonzero(bin_image)} "
            f"patch={np.count_nonzero(patch)} "
            f"template={np.count_nonzero(templ)}")
    if (np.count_nonzero(bin_image[0, :]) != 0
            or np.count_nonzero(bin_image[-1, :]) != 0
            or np.count_nonzero(bin_image[:, 0]) != 0
            or np.count_nonzero(bin_image[:, -1]) != 0):
        raise RuntimeError("BPE logical border is not all zero")

    # HLS's exact truncation decision.  The modified input is the top-left
    # kernel tap for logical (4,12): sum 2248 truncates to 140 but rounds to
    # 141, so threshold 140 must produce 255 and a rounding implementation
    # must disagree at this byte.
    win = gray[3:6, 11:14].astype(np.int32)
    weighted_sum = int(
        win[0, 0] + 2 * win[0, 1] + win[0, 2]
        + 2 * win[1, 0] + 4 * win[1, 1] + 2 * win[1, 2]
        + win[2, 0] + 2 * win[2, 1] + win[2, 2])
    trunc = weighted_sum >> 4
    rounded = (weighted_sum + 8) >> 4
    if (weighted_sum, trunc, rounded, int(bin_image[4, 12])) != (
            2248, 140, 141, 255):
        raise RuntimeError(
            "BPE truncation witness drifted: "
            f"sum={weighted_sum} trunc={trunc} rounded={rounded} "
            f"pixel={int(bin_image[4, 12])}")

    # The exact-integer matcher oracle must retain a unique, well-separated
    # perfect peak in the intended coordinate frame.
    expected_margin = 0.6220355269907727
    if abs(score - 1.0) > 1e-12 or (ux, uy) != (4, 1):
        raise RuntimeError(
            f"BPE matcher golden drifted: score={score} local=({ux},{uy})")
    if abs(margin - expected_margin) > 1e-12:
        raise RuntimeError(
            f"BPE matcher margin drifted: {margin} != {expected_margin}")
    if (x0 + ux, y0 + uy) != (7, 5):
        raise RuntimeError(
            f"BPE page rebase drifted: ({x0 + ux},{y0 + uy})")

    # Inject the retired raw stream layout in the golden model.  A suite that
    # still gave the right answer under this control would not test the
    # boundary it claims to test.
    legacy = np.zeros_like(bin_image)
    legacy[2:, 2:] = bin_image[1:-1, 1:-1]
    legacy_patch = np.ascontiguousarray(legacy[y0:y1, x0:x1])
    n_legacy_diff = int(np.count_nonzero(legacy_patch != patch))
    legacy_score, legacy_x, legacy_y, _, _ = TME.golden(
        legacy_patch, templ)
    if n_legacy_diff != 53:
        raise RuntimeError(
            f"BPE legacy-layout control drifted to {n_legacy_diff} patch "
            "mismatches; expected 53")
    if (abs(legacy_score - 1.0) > 1e-12
            or (legacy_x, legacy_y) != (5, 2)):
        raise RuntimeError(
            "BPE legacy-layout control no longer moves the peak exactly one "
            f"pixel: score={legacy_score} local=({legacy_x},{legacy_y})")
    if (legacy_x, legacy_y) == (ux, uy):
        raise RuntimeError(
            "BPE legacy-layout control still gives the golden location; the "
            "case cannot detect the raw/logical boundary regression")

    blobs = {
        "gray": gray.tobytes(),
        "bin": bin_image.tobytes(),
        "patch": patch.tobytes(),
        "templ": templ.tobytes(),
    }
    for name, expected in BPE_HASHES.items():
        observed = _sha256(blobs[name])
        if observed != expected:
            raise RuntimeError(
                f"BPE {name} hash drifted: {observed} != {expected}")

    row = dict(index=0, packed=packed, last=1, valid=1, reason=reason,
               x0=x0, y0=y0, pw=pw, ph=ph, templ_id=0, templ_off=0,
               tw=BPE_TW, th=BPE_TH, score=score,
               page_x=x0 + ux, page_y=y0 + uy, ux=ux, uy=uy,
               margin=margin, category="three-stage",
               tag="logical-layout-shift")
    return row, blobs, weighted_sum, n_legacy_diff


def write_bpe_case(out):
    row, blobs, weighted_sum, n_legacy_diff = build_bpe_case()
    (out / "tb_bpe_tme_gray.bin").write_bytes(blobs["gray"])
    (out / "tb_bpe_tme_bin.bin").write_bytes(blobs["bin"])
    (out / "tb_bpe_tme_patch.bin").write_bytes(blobs["patch"])
    (out / "tb_bpe_tme_templs.bin").write_bytes(blobs["templ"])

    header = (
        f"1 {BPE_IMG_W} {BPE_IMG_H} {BPE_STRIDE} {BPE_BUFFER_BYTES} "
        f"1 {len(blobs['templ'])} {BPE_THRESHOLD} {len(blobs['gray'])} "
        f"{len(blobs['bin'])} {len(blobs['patch'])}")
    line = (
        f"{row['index']} {row['packed']:016x} {row['last']} {row['valid']} "
        f"{row['reason']:04x} {row['x0']} {row['y0']} {row['pw']} "
        f"{row['ph']} {row['templ_id']} {row['templ_off']} {row['tw']} "
        f"{row['th']} {row['score']:.6f} {row['page_x']} {row['page_y']} "
        f"{row['ux']} {row['uy']} {row['margin']:.6f} {row['category']} "
        f"{row['tag']}")
    manifest = (header + "\n" + line + "\n").encode("ascii")
    manifest_hash = _sha256(manifest)
    if manifest_hash != BPE_CASES_SHA256:
        raise RuntimeError(
            f"BPE manifest hash drifted: {manifest_hash} != "
            f"{BPE_CASES_SHA256}")
    (out / "tb_bpe_tme_cases.txt").write_bytes(manifest)

    print(
        f"BPE case: page {BPE_IMG_W}x{BPE_IMG_H} threshold {BPE_THRESHOLD}, "
        f"patch {row['pw']}x{row['ph']} @({row['x0']},{row['y0']}), "
        f"score {row['score']:+.6f}, local ({row['ux']},{row['uy']}) -> "
        f"page ({row['page_x']},{row['page_y']}), margin "
        f"{row['margin']:.4f}")
    print(
        f"  truncation control PASS: sum {weighted_sum} >> 4 = 140; "
        f"legacy-layout control PASS: {n_legacy_diff}/168 bytes differ, "
        "peak moves to (5,2)")


def main():
    # The cv2 cross-check inside TME.golden must run on the generic path for
    # the same reason it must there: an IPP build dispatches to a different
    # function.  Fatal, not a warning — see require_generic_opencv().
    TME.require_generic_opencv()

    image = PE.build_image(IMG_W, IMG_H)
    rng = np.random.default_rng(0x5EA3)

    rows, templ_blob = [], bytearray()
    n_valid = 0
    for i, c in enumerate(descriptors()):
        valid, reason, x0, y0, x1, y1 = PE.model_validate(c, IMG_W, IMG_H)
        pw, ph = x1 - x0, y1 - y0
        last = 1 if i == len(descriptors()) - 1 else 0
        packed = PE.pack(c)

        if not valid:
            rows.append(dict(index=i, packed=packed, last=last, valid=0,
                             reason=reason, x0=x0, y0=y0, pw=pw, ph=ph,
                             templ_id=-1, templ_off=0, tw=0, th=0,
                             score=0.0, page_x=0, page_y=0, ux=0, uy=0,
                             margin=0.0, category=c["category"],
                             tag=c["tag"]))
            continue

        if c["tag"] == "offset-template":
            templ, score, ux, uy, margin = offset_template(
                image, x0, y0, pw, ph, c["tag"])
        else:
            templ, score, ux, uy, margin = pick_template(
                image, x0, y0, pw, ph, c["tag"], rng)

        # §4.6: the template must be non-flat or no golden exists for it.
        # TME.golden already raises on a flat template; assert the reason it
        # cannot have here, so a future page change that flattens a crop
        # fails in this file rather than downstream.
        if int(templ.min()) == int(templ.max()):
            raise ValueError(f"{c['tag']}: template is flat (§4.6)")

        rows.append(dict(index=i, packed=packed, last=last, valid=1,
                         reason=reason, x0=x0, y0=y0, pw=pw, ph=ph,
                         templ_id=n_valid, templ_off=len(templ_blob),
                         tw=MAX_TW, th=MAX_TH, score=score,
                         page_x=x0 + ux, page_y=y0 + uy, ux=ux, uy=uy,
                         margin=min(margin, 999.0), category=c["category"],
                         tag=c["tag"]))
        templ_blob += templ.tobytes()
        n_valid += 1

    check_suite(rows)

    out = Path(".")
    (out / "tb_pe_tme_image.bin").write_bytes(image.tobytes())
    (out / "tb_pe_tme_templs.bin").write_bytes(bytes(templ_blob))
    header = (f"{len(rows)} {IMG_W} {IMG_H} {STRIDE} {BUFFER_BYTES} "
              f"{n_valid} {len(templ_blob)}")
    lines = [header] + [
        f"{r['index']} {r['packed']:016x} {r['last']} {r['valid']} "
        f"{r['reason']:04x} {r['x0']} {r['y0']} {r['pw']} {r['ph']} "
        f"{r['templ_id']} {r['templ_off']} {r['tw']} {r['th']} "
        f"{r['score']:.6f} {r['page_x']} {r['page_y']} {r['ux']} {r['uy']} "
        f"{r['margin']:.6f} {r['category']} {r['tag']}"
        for r in rows]
    (out / "tb_pe_tme_cases.txt").write_text("\n".join(lines) + "\n")

    # Separate global geometry and a separate summary: the existing seam's
    # 4-descriptor/3-run evidence remains byte-for-byte and semantically
    # unchanged.
    write_bpe_case(out)

    print(f"page {IMG_W}x{IMG_H} stride {STRIDE}, {len(rows)} descriptors "
          f"({n_valid} valid), templates {len(templ_blob)} B")
    for r in rows:
        if r["valid"]:
            print(f"  [{r['index']}] {r['tag']:<16s} patch {r['pw']}x{r['ph']} "
                  f"@({r['x0']},{r['y0']})  score {r['score']:+.6f}  "
                  f"local ({r['ux']},{r['uy']}) -> page "
                  f"({r['page_x']},{r['page_y']})  margin {r['margin']:.4f}")
        else:
            print(f"  [{r['index']}] {r['tag']:<16s} REJECTED reason "
                  f"0x{r['reason']:x}, no pixel beats")


def check_suite(rows):
    """Refuse to write a suite that cannot detect what it exists to detect.

    Explicit raises, not asserts: this file is run under `python -O` as part
    of its acceptance, and an assert would make that run write a manifest it
    had not checked.
    """
    valid = [r for r in rows if r["valid"]]
    if len(valid) < 2:
        raise ValueError("need >= 2 valid candidates or the cursor cannot "
                         "desynchronise and mid-batch-reject proves nothing")

    idx_invalid = [r["index"] for r in rows if not r["valid"]]
    if not idx_invalid:
        raise ValueError("no rejected descriptor: the record/patch cursor "
                         "skew this suite targets cannot occur")
    if not (min(r["index"] for r in valid) < min(idx_invalid)
            < max(r["index"] for r in valid)):
        raise ValueError("the rejected descriptor must sit BETWEEN two valid "
                         "ones; at the end of the batch a cursor bug has "
                         "nothing left to corrupt and the suite passes wrong")

    # The clipped case must actually clip, or it is a duplicate baseline.
    clipped = [r for r in valid if r["tag"] == "clipped-left"]
    plain = [r for r in valid if r["tag"] == "seam-baseline"]
    if clipped and plain and clipped[0]["pw"] >= plain[0]["pw"]:
        raise ValueError(
            f"clipped-left patch_w {clipped[0]['pw']} is not smaller than "
            f"the unclipped {plain[0]['pw']} — it is no longer clipping, so "
            f"'geometry comes from the record' is untested")

    # At least one score must be neither 0.0 nor 1.0, for the same reason the
    # matcher's own suite needs one: those two encode no sign bit and one
    # mantissa bit between them.
    if not any(0.0 < abs(r["score"]) < 0.999 for r in valid):
        raise ValueError("every score is 0.0 or 1.0; add an offset-template "
                         "case or the float path is not exercised")

    # A patch that starts at the origin would make page == local coordinates,
    # so the rebasing would pass without being performed.
    if not any(r["x0"] > 0 and r["y0"] > 0 for r in valid):
        raise ValueError("no valid patch has a non-zero origin; page-vs-local "
                         "coordinate rebasing would be untested")

    for r in valid:
        if r["margin"] < MIN_MARGIN:
            raise ValueError(
                f"{r['tag']}: margin {r['margin']:.6f} < {MIN_MARGIN}; the TB "
                f"asserts an EXACT location and this peak is not unique")
        if not (r["tw"] <= r["pw"] and r["th"] <= r["ph"]):
            raise ValueError(f"{r['tag']}: template does not fit its patch")


if __name__ == "__main__":
    main()
