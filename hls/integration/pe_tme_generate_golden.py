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
"""

import sys
from pathlib import Path

import numpy as np

# Both generators live in sibling directories and neither is a package.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "patch_extract"))
sys.path.insert(0, str(_HERE.parent / "template_match"))

import patch_extract_generate_golden as PE      # noqa: E402
import tme_generate_golden as TME               # noqa: E402

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
