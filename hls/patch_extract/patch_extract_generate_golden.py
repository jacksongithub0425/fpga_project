"""
Generate testbench golden data for patch_extract_tb.cpp.

Run this once from the patch_extract/ directory:
    python patch_extract_generate_golden.py [--out-dir DIR]

Boundaries come from the real build_endpoint_patch() in
terminal_counter_endpoint_first.py, which is bit-exact with
patch_extract_core.cpp — so the testbench can demand zero mismatch.
Validity and reason bits come from a Python mirror of the contract §4
rules (docs/pl_interface_contract.md); keep model_validate() in sync with
the validation block in patch_extract_core.cpp.

Writes:
    tb_patch_extract_image.bin          - compact img_w x img_h row-major uint8
                                          (the TB places it into a strided
                                          buffer itself; padding = sentinel)
    tb_patch_extract_cases_csim.txt     - full directed + random + edge suite
    tb_patch_extract_golden_csim.bin    - expected pixels, VALID candidates
                                          only, candidate order
    tb_patch_extract_cases_cosim.txt    - small subset for C/RTL co-simulation
    tb_patch_extract_golden_cosim.bin   - expected pixels for that subset
    tb_patch_extract_cases_hicoord.txt  - high-coordinate suite on a 9800x6400
                                          page with stride 9856 (csim only);
                                          image is procedural, never written
                                          to disk — probe triples in the
                                          manifest let the TB verify its own
                                          C++ fill against this file's model
    tb_patch_extract_golden_hicoord.bin - expected pixels for that suite

Manifest row format (whitespace separated, strings last so a C++ reader can
scan the fixed fields first):

    index packed_hex last ep_x ep_y side_code max_tw max_th
    x0 y0 x1 y1 offset count valid reason_hex tme_legal category tag

Golden blob contains pixels for VALID rows only (invalid rows have count=0);
offsets are cumulative over valid rows.  Categories:

  conformance    (valid=1) - legal full-pipeline inputs per contract §4.1.
  extractor_edge (valid=0) - degenerate/out-of-frame/oversized descriptors.
                 The extractor must REJECT these (§4: reject, never crop) —
                 metadata only, no pixel payload, no DDR read.
  robustness     (valid=0) - max_th above the 96 ceiling (reason bit 3).
                 These also fire the post-clip height check (bit 6), because
                 MAX_PATCH_H is now the exact envelope 307 and any th above
                 96 exceeds it.

tme_legal is retained for continuity and must equal `valid` for every row —
the generator asserts it.  Under the adopted §4.4 option 1, patch/template
EQUALITY is legal, so conformance checks use >=, not >.
"""

import argparse
import random
import sys
from pathlib import Path

import numpy as np

# Add sw/ to path so we can import the existing script
SW_DIR = Path(__file__).resolve().parents[2] / "sw"
sys.path.insert(0, str(SW_DIR))

import terminal_counter_endpoint_first as tcef
from terminal_counter_endpoint_first import build_endpoint_patch

# Guard against importing one of the dated backups sitting next to it in sw/
EXPECTED_MODULE = SW_DIR / "terminal_counter_endpoint_first.py"
assert Path(tcef.__file__).resolve() == EXPECTED_MODULE.resolve(), (
    f"imported the wrong reference module: {tcef.__file__}"
)

# ---- Configuration ------------------------------------------------------
# Deliberately not a power of two and not any stride the TB uses: a core that
# assumes a compiled-in stride instead of the runtime register reads the
# wrong pixels.
IMG_W, IMG_H = 1009, 503

# High-coordinate suite: exercises bx/by bits above the old 12-bit counter
# width (>4095) and the patch-width bound (bit 5), which needs a page wider
# than 1024.  img_w < stride so the whole suite is also a non-compact-stride
# test.  The image is procedural (see pattern()); the TB re-derives it in C++
# and validates its fill against the probe triples in the manifest.
HI_IMG_W, HI_IMG_H = 9800, 6400
HI_STRIDE = 9856          # 77 x 128 — 64-byte multiple per contract §2
HI_NPROBES = 32

# The testbench prefills its strided DDR buffer with this before placing the
# image, so a read of padding or out-of-region bytes yields a value that
# cannot occur in valid pixels.
SENTINEL = 0xA5
SENTINEL_REPLACEMENT = 0xA4

# Contract §4.1 limits — must match patch_extract_core.h
MIN_IMG_DIM = 3
MAX_IMG_W, MAX_IMG_H = 9856, 6400
MIN_TEMPL_DIM = 4
MAX_TEMPL_W = 216
MAX_TEMPL_H = 96
# Exact reachable envelope implied by MAX_TEMPL_W/H above (contract §3):
#   820 = 2*216 + floor(432/5) + 216 + floor(432/5)
#   307 = 3*96  + floor(96/5)
# Must track PE_MAX_PATCH_W/H in patch_extract_core.h and MAX_PATCH_W/H in
# tme_top.h.  Formerly 1024/320, which the matcher could not fit on the part.
MAX_PATCH_W = 820
MAX_PATCH_H = 307

# Reason bits (§4.2) — must match PE_R_* in patch_extract_core.h
R_EPX, R_EPY, R_TW, R_TH, R_SIDE, R_PW, R_PH, R_SMALL, R_GLOBAL = range(9)

SIDE_CODE = {"left": 0, "right": 1}

CONFORMANCE = "conformance"
EXTRACTOR_EDGE = "extractor_edge"
ROBUSTNESS = "robustness"
TME_LEGAL = {CONFORMANCE: 1, EXTRACTOR_EDGE: 0, ROBUSTNESS: 0}

# Fixed expectations that catch accidental oracle drift on either side.
ANCHORS = [
    ((600, 251, "left", 216, 96), (82, 98, 902, 405)),
    ((400, 251, "right", 216, 96), (98, 98, 918, 405)),
    ((600, 251, "left", 45, 96), (492, 98, 663, 405)),
    ((600, 251, "left", 216, 158), (82, 0, 902, 503)),
]


def pattern_block(y0, y1, w):
    """Rows [y0, y1) of the coordinate-sensitive pattern, as uint8.

    An addressing or stride error lands on a visibly wrong value instead of a
    plausible one.  The (x % 3) term is load-bearing, not decoration: without
    it the pattern is 29x + 17y + (x^y), and since 29x+17y == x+y (mod 2) and
    (x^y) == x+y (mod 2), every pixel comes out even — bit 0 constant across
    the whole image.  Do not simplify.

    The C++ twin lives in patch_extract_tb.cpp (pattern_px); keep in sync.
    Sentinel replacement makes 0xA5 unreachable by construction.
    """
    yy, xx = np.mgrid[y0:y1, 0:w]
    img = ((29 * xx + 17 * yy + (xx ^ yy) + (xx % 3)) & 0xFF).astype(np.uint8)
    img[img == SENTINEL] = SENTINEL_REPLACEMENT
    return img


def build_image(w, h):
    """Full pattern image, built in row blocks to cap intermediate memory
    (the int32 mgrid scratch for 9800x6400 would otherwise be ~1 GB)."""
    image = np.empty((h, w), dtype=np.uint8)
    step = 512
    for y0 in range(0, h, step):
        y1 = min(y0 + step, h)
        image[y0:y1] = pattern_block(y0, y1, w)
    assert not (image == SENTINEL).any(), "sentinel must not occur in valid pixels"
    for bit in range(8):
        assert np.unique((image >> bit) & 1).size == 2, f"bit {bit} is constant"
    return image


def cand(ep_x, ep_y, side, max_tw, max_th, tag, category=CONFORMANCE):
    """side may be 'left'/'right' or a raw 2-bit code (for invalid-side
    cases, which have no name)."""
    side_code = SIDE_CODE[side] if isinstance(side, str) else int(side)
    return dict(ep_x=ep_x, ep_y=ep_y, side_code=side_code, max_tw=max_tw,
                max_th=max_th, tag=tag, category=category)


def model_validate(c, img_w, img_h):
    """Python mirror of the §4 per-descriptor validation in
    patch_extract_core.cpp.  Returns (valid, reason, x0, y0, x1, y1).

    Geometry is computed for every descriptor (the metadata record reports
    it even when valid=0).  side_code==0 uses the left formula, anything
    else the right one, exactly as the core's `if (side == 0)` does."""
    reason = 0
    if c["ep_x"] >= img_w:
        reason |= 1 << R_EPX
    if c["ep_y"] >= img_h:
        reason |= 1 << R_EPY
    if not (MIN_TEMPL_DIM <= c["max_tw"] <= MAX_TEMPL_W):
        reason |= 1 << R_TW
    if not (MIN_TEMPL_DIM <= c["max_th"] <= MAX_TEMPL_H):
        reason |= 1 << R_TH
    if c["side_code"] > 1:
        reason |= 1 << R_SIDE

    side_str = "left" if c["side_code"] == 0 else "right"
    x0, y0, x1, y1 = build_endpoint_patch(
        c["ep_x"], c["ep_y"], side_str, img_w, img_h, c["max_tw"], c["max_th"])
    pw, ph = x1 - x0, y1 - y0

    if pw > MAX_PATCH_W:
        reason |= 1 << R_PW
    if ph > MAX_PATCH_H:
        reason |= 1 << R_PH
    # §4.4 option 1 adopted: equality is legal, reject only strictly-smaller.
    if pw < c["max_tw"] or ph < c["max_th"]:
        reason |= 1 << R_SMALL

    return reason == 0, reason, x0, y0, x1, y1


def build_candidates():
    """The full C-simulation suite, in all three categories."""
    cands = []

    # -- conformance: legal full-pipeline inputs --------------------------
    cands += [
        cand(600, 251, "left", 216, 96, "anchor-max-left"),
        cand(400, 251, "right", 216, 96, "anchor-max-right"),
    ]

    # max_tw=45 is the first width where the old float path disagreed with
    # the exact rational (int(45*1.4)=62 vs 45*7//5=63).  Guards a revert.
    cands += [
        cand(600, 251, "left", 45, 96, "rational-guard-left"),
        cand(600, 251, "right", 45, 96, "rational-guard-right"),
        cand(600, 251, "left", 45, 45, "rational-guard-square"),
    ]

    # Corners of the legal template envelope.  4 is the driver's floor
    # (terminal_counter_endpoint_first.py:557) and is otherwise unreachable:
    # random draws over [4,216]x[4,96] essentially never land on it.
    # The 216x96 corner is already covered by the anchors above.
    cands += [
        cand(600, 251, "left", MIN_TEMPL_DIM, MIN_TEMPL_DIM, "envelope-min"),
        cand(600, 251, "left", MIN_TEMPL_DIM, MAX_TEMPL_H, "envelope-min-w-max-h"),
        cand(600, 251, "left", MAX_TEMPL_W, MIN_TEMPL_DIM, "envelope-max-w-min-h"),
    ]

    # Corners and edge midpoints — every clamp path, still pipeline-legal.
    for side in ("left", "right"):
        for ex, ey, name in [
            (0, 0, "corner-tl"),
            (IMG_W - 1, 0, "corner-tr"),
            (0, IMG_H - 1, "corner-bl"),
            (IMG_W - 1, IMG_H - 1, "corner-br"),
            (0, 251, "edge-left"),
            (IMG_W - 1, 251, "edge-right"),
            (500, 0, "edge-top"),
            (500, IMG_H - 1, "edge-bottom"),
        ]:
            cands.append(cand(ex, ey, side, 96, 48, f"{name}-{side}"))

    # In-frame pseudo-random cases.  Sampling the endpoint over the full
    # 16-bit range would put every draw outside a 1009x503 image, collapsing
    # them all to the same bottom-right clamp-and-reject.
    rng = random.Random(20260727)
    for i in range(18):
        cands.append(cand(
            rng.randint(0, IMG_W - 1), rng.randint(0, IMG_H - 1),
            rng.choice(("left", "right")),
            rng.randint(MIN_TEMPL_DIM, MAX_TEMPL_W),
            rng.randint(MIN_TEMPL_DIM, MAX_TEMPL_H),
            f"random-{i:02d}",
        ))

    # -- extractor_edge: must be REJECTED with the right reason bits ------
    cands += [
        # Template range floor (bits 2+3): the old "clip to 2x2" inputs.
        cand(600, 251, "left", 0, 0, "min-template-left", EXTRACTOR_EDGE),
        cand(600, 251, "right", 0, 0, "min-template-right", EXTRACTOR_EDGE),
        cand(600, 251, "left", 1, 1, "tiny-template", EXTRACTOR_EDGE),
        # Template range ceiling boundaries (bit 2 / bit 3).  217 and 97 are
        # the first illegal values; 216 and 96 (legal) are covered by the
        # anchors and envelope cases above.
        cand(600, 251, "left", 217, 96, "tw-217-first-illegal", EXTRACTOR_EDGE),
        cand(600, 251, "left", 216, 97, "th-97-first-illegal", EXTRACTOR_EDGE),
        # Wire-maximum fields: max_tw is 14-bit, max_th 16-bit.  The clip to
        # the full 503-row image still exceeds 307, so bit 6 fires alongside
        # bits 2/3 — the "independent post-clip check" earning its keep.
        cand(600, 251, "left", 0x3FFF, 0xFFFF, "wire-max-templ", EXTRACTOR_EDGE),
        # Invalid side codes (bit 4): side is 2 bits on the wire and >1 must
        # be rejected, not treated as "right".
        cand(600, 251, 2, 96, 48, "side-code-2", EXTRACTOR_EDGE),
        cand(600, 251, 3, 96, 48, "side-code-3", EXTRACTOR_EDGE),
        # Endpoint exactly at the first out-of-frame coordinate (bits 0/1).
        cand(IMG_W, 251, "left", 96, 48, "epx-eq-imgw", EXTRACTOR_EDGE),
        cand(600, IMG_H, "left", 96, 48, "epy-eq-imgh", EXTRACTOR_EDGE),
    ]
    for side in ("left", "right"):
        cands += [
            cand(IMG_W + 500, 251, side, 216, 96, f"oof-right-{side}", EXTRACTOR_EDGE),
            cand(600, IMG_H + 500, side, 216, 96, f"oof-bottom-{side}", EXTRACTOR_EDGE),
            cand(65535, 65535, side, 216, 96, f"oof-max-{side}", EXTRACTOR_EDGE),
        ]

    # -- robustness: max_th above the 96 ceiling (reason bit 3) -----------
    # The patch_h figures below are pre-clipping.  The centred (600,251)
    # variants keep most of them; the (0,0) corner variants clip to roughly
    # half.  All are invalid via bit 3; the taller clips also set bit 6.
    # (The pre-clip heights below are all above the 307 bound now that
    # MAX_PATCH_H is the exact envelope, so every one of these sets bit 6 as
    # well as bit 3.  Names describe the pre-clip patch height, not a bound.)
    for th, name in [(100, "rows-320"), (101, "rows-323"),
                     (158, "rows-full-image"), (216, "rows-691-clipped")]:
        cands += [
            cand(600, 251, "left", 216, th, f"robust-{name}", ROBUSTNESS),
            cand(0, 0, "right", 216, th, f"robust-{name}-corner", ROBUSTNESS),
        ]

    return cands


def build_cosim_candidates():
    """Small enough to finish in RTL, broad enough to matter.  Six candidates
    in one batch exercises per-patch TLAST across patch seams.

    Invalid descriptors are wedged BETWEEN valid patches, never trailing:
    their whole value is the metadata-only path — no pixel beats, no DDR read
    — so the pixel stream must close the preceding patch with TLAST, skip the
    reject entirely, and reopen cleanly for the next valid one.  A trailing
    reject would leave that resumption seam untested.

    Three rejects, each a different reason word, and two of them adjacent so
    back-to-back skipping is covered too:

        [1] cosim-tw-217   reason 0x024  bits 2+5, geometry 823x307
        [3] cosim-invalid-mid reason 0x00c  bits 2+3 (degenerate 0x0 template)
        [4] cosim-th-97    reason 0x048  bits 3+6, geometry 820x310

    The 217/97 pair is here rather than only in the csim manifest because the
    csim manifest never reaches RTL — run_hls.tcl passes -argv "cosim", which
    selects THIS list.  The post-clip size bits (5 and 6) were csim-only until
    these two rows existed.  Both are unclipped at (600,251) on the 1009x503
    image (823 <= 1009, 310 <= 503), so they reach the bound honestly rather
    than being trimmed to it, and neither emits a pixel beat — the cosim
    golden stays at 262,748 bytes."""
    return [
        cand(500, 251, "left", 20, 20, "cosim-small-interior"),
        cand(600, 251, "left", 217, 96, "cosim-tw-217", EXTRACTOR_EDGE),
        cand(0, 0, "right", 40, 40, "cosim-clipped-corner"),
        cand(600, 251, "left", 0, 0, "cosim-invalid-mid", EXTRACTOR_EDGE),
        cand(600, 251, "left", 216, 97, "cosim-th-97", EXTRACTOR_EDGE),
        cand(600, 251, "left", 216, 96, "cosim-max-legal"),
    ]


def build_hicoord_candidates():
    """High-coordinate suite on the 9800x6400 page.  Also carries the
    patch-size boundary pairs.

    Patch width is 3*tw + 2*floor(2*tw/5), so max_tw=216 -> exactly 820, the
    largest patch the matcher can hold, and max_tw=217 -> 823, the first that
    overruns it.  The pair lives here rather than in the 1009-wide main suite
    because an 820-wide patch only stays unclipped on a page wide enough not
    to clamp it first — clipping would shrink it below the bound and the case
    would prove nothing.

    What changed when MAX_PATCH went from 1024x320 to the exact envelope
    820x307 is the CO-FIRE THRESHOLD, not the ability of bit 5 to stand
    alone.  It never could: the smallest max_tw overrunning 1024 was 270, and
    270 already violated the 216 template cap, so bit 2 came with it.  The
    same held for height (101 -> 323, already over the 96 cap).  Narrowing the
    bound moved that first co-firing pair from 270/101 down to 217/97.

    What the narrowing DID change here is that the old 270 -> 1026 /
    269 -> 1021 pair no longer straddles anything: at 820 both overrun, so
    269 stopped being a negative case.  It is replaced by 216 -> 820, which
    is a genuine negative because 216 is legal and lands exactly on the
    bound.  The assertions in main() pin all of it with exact reason words."""
    cands = [
        cand(9000, 6000, "left", 216, 96, "hi-interior-left"),
        cand(9000, 5000, "right", 216, 96, "hi-interior-right"),
        cand(HI_IMG_W - 1, HI_IMG_H - 1, "left", 96, 48, "hi-corner-br"),
        cand(HI_IMG_W - 1, 3200, "right", 216, 96, "hi-edge-right"),
        # Width boundary pair, unclipped at x=5000 on a 9800-wide page.
        cand(5000, 3200, "left", 216, 96, "hi-boundary-820"),
        cand(5000, 3200, "left", 217, 96, "hi-overflow-823", EXTRACTOR_EDGE),
        # Height boundary pair at the same place, so the row bound is also
        # exercised on an unclipped patch rather than only on the 503-row
        # main image where clipping reaches it first.
        cand(5000, 3200, "left", 216, 97, "hi-overflow-h310", EXTRACTOR_EDGE),
    ]
    rng = random.Random(20260729)
    for i in range(4):
        cands.append(cand(
            rng.randint(4200, HI_IMG_W - 1), rng.randint(4200, HI_IMG_H - 1),
            rng.choice(("left", "right")),
            rng.randint(MIN_TEMPL_DIM, MAX_TEMPL_W),
            rng.randint(MIN_TEMPL_DIM, MAX_TEMPL_H),
            f"hi-random-{i:02d}",
        ))
    return cands


def pack(c):
    """Pack into the 64-bit AXI-Stream layout from patch_extract_core.h."""
    ep_x, ep_y = c["ep_x"], c["ep_y"]
    side = c["side_code"]
    max_tw, max_th = c["max_tw"], c["max_th"]

    assert 0 <= ep_x <= 0xFFFF, f"ep_x {ep_x} exceeds 16 bits"
    assert 0 <= ep_y <= 0xFFFF, f"ep_y {ep_y} exceeds 16 bits"
    assert 0 <= side <= 0x3, f"side {side} exceeds 2 bits"
    assert 0 <= max_tw <= 0x3FFF, f"max_tw {max_tw} exceeds 14 bits"
    assert 0 <= max_th <= 0xFFFF, f"max_th {max_th} exceeds 16 bits"

    packed = ep_x | (ep_y << 16) | (side << 32) | (max_tw << 34) | (max_th << 48)

    # Round-trip the fields exactly as the core decodes them.
    assert packed & 0xFFFF == ep_x
    assert (packed >> 16) & 0xFFFF == ep_y
    assert (packed >> 32) & 0x3 == side
    assert (packed >> 34) & 0x3FFF == max_tw
    assert (packed >> 48) & 0xFFFF == max_th
    assert packed < (1 << 64)
    return packed


def build_suite(cands, image, name, img_w, img_h):
    """Resolve validity + boundaries, slice VALID patches, self-check."""
    rows = []
    blob = bytearray()

    for idx, c in enumerate(cands):
        valid, reason, x0, y0, x1, y1 = model_validate(c, img_w, img_h)

        # Geometry must be inside the image for every candidate, valid or not
        # — the clamps guarantee it, and the metadata reports it either way.
        assert 0 <= x0 < x1 <= img_w, f"{c['tag']}: x range {x0}..{x1}"
        assert 0 <= y0 < y1 <= img_h, f"{c['tag']}: y range {y0}..{y1}"

        pw, ph = x1 - x0, y1 - y0
        count = 0
        if valid:
            patch = image[y0:y1, x0:x1]
            assert patch.shape == (ph, pw), f"{c['tag']}: slice {patch.shape}"
            pixels = patch.tobytes()
            count = len(pixels)
            assert count == pw * ph, f"{c['tag']}: byte count"

            # A constant patch would be a weak diagnostic.
            assert np.unique(patch).size > 1, f"{c['tag']}: patch is constant"
            assert SENTINEL not in patch, f"{c['tag']}: patch contains the sentinel"

            # Valid implies the full §4.1 envelope, restated directly.
            # Equality is legal under the adopted §4.4 option 1, hence >=.
            assert MIN_TEMPL_DIM <= c["max_tw"] <= MAX_TEMPL_W, f"{c['tag']}: max_tw"
            assert MIN_TEMPL_DIM <= c["max_th"] <= MAX_TEMPL_H, f"{c['tag']}: max_th"
            assert pw >= c["max_tw"], f"{c['tag']}: patch {pw} < templ {c['max_tw']}"
            assert ph >= c["max_th"], f"{c['tag']}: patch {ph} < templ {c['max_th']}"
            assert pw <= MAX_PATCH_W, f"{c['tag']}: width {pw} > {MAX_PATCH_W}"
            assert ph <= MAX_PATCH_H, f"{c['tag']}: height {ph} > {MAX_PATCH_H}"

        # Category and §4 verdict must agree — a conformance case the model
        # rejects (or an edge case it accepts) is a suite bug, not a DUT bug.
        assert (TME_LEGAL[c["category"]] == 1) == valid, (
            f"{c['tag']}: category {c['category']} but valid={valid} "
            f"(reason 0x{reason:03x})"
        )

        rows.append(dict(
            index=idx, packed=pack(c), last=1 if idx == len(cands) - 1 else 0,
            x0=x0, y0=y0, x1=x1, y1=y1,
            offset=len(blob), count=count, valid=int(valid), reason=reason,
            **c,
        ))
        if valid:
            blob += pixels

    by_cat = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    breakdown = ", ".join(f"{v} {k}" for k, v in sorted(by_cat.items()))
    n_valid = sum(r["valid"] for r in rows)
    print(f"{name}: {len(rows)} candidates ({breakdown}), {n_valid} valid, "
          f"{len(blob)} golden bytes ({len(blob)/1024:.1f} KiB)")
    return rows, bytes(blob)


def format_rows(rows):
    lines = []
    for r in rows:
        lines.append(
            f"{r['index']} {r['packed']:016x} {r['last']} "
            f"{r['ep_x']} {r['ep_y']} {r['side_code']} "
            f"{r['max_tw']} {r['max_th']} "
            f"{r['x0']} {r['y0']} {r['x1']} {r['y1']} "
            f"{r['offset']} {r['count']} "
            f"{r['valid']} {r['reason']:03x} "
            f"{TME_LEGAL[r['category']]} {r['category']} {r['tag']}"
        )
    return lines


def write_suite(rows, blob, cases_path, golden_path, img_w, img_h):
    lines = [f"{img_w} {img_h} {len(rows)} {len(blob)}"] + format_rows(rows)
    Path(cases_path).write_text("\n".join(lines) + "\n")
    Path(golden_path).write_bytes(blob)
    print(f"Written: {cases_path.name} ({len(rows)} candidates)")
    print(f"Written: {golden_path.name} ({len(blob)} bytes)")


def write_hicoord_suite(rows, blob, image, cases_path, golden_path):
    """Header carries the stride and the probe triples, because the TB
    rebuilds this image procedurally rather than loading a 60 MB file."""
    rng = random.Random(20260730)
    probes = []
    for _ in range(HI_NPROBES):
        x = rng.randint(0, HI_IMG_W - 1)
        y = rng.randint(0, HI_IMG_H - 1)
        probes.append((x, y, int(image[y, x])))

    lines = [f"{HI_IMG_W} {HI_IMG_H} {HI_STRIDE} {len(rows)} {len(blob)} "
             f"{len(probes)}"]
    lines += [f"{x} {y} {v}" for x, y, v in probes]
    lines += format_rows(rows)
    Path(cases_path).write_text("\n".join(lines) + "\n")
    Path(golden_path).write_bytes(blob)
    print(f"Written: {cases_path.name} ({len(rows)} candidates, "
          f"{len(probes)} probes)")
    print(f"Written: {golden_path.name} ({len(blob)} bytes)")


def check_anchors():
    for (ex, ey, side, tw, th), expect in ANCHORS:
        got = tuple(build_endpoint_patch(ex, ey, side, IMG_W, IMG_H, tw, th))
        assert got == expect, (
            f"anchor drift: ep=({ex},{ey}) {side} {tw}x{th} -> {got}, expected {expect}"
        )
        print(f"Anchor OK: ep=({ex},{ey}) {side} {tw}x{th} -> {expect} "
              f"[{expect[2]-expect[0]}x{expect[3]-expect[1]}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--out-dir", type=Path, default=Path("."),
                    help="directory to write the .bin/.txt artifacts into")
    args = ap.parse_args()
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    image = build_image(IMG_W, IMG_H)
    print(f"Image: {IMG_W}x{IMG_H} px, {image.nbytes} bytes, "
          f"{len(np.unique(image))} distinct values")

    check_anchors()

    csim_rows, csim_blob = build_suite(
        build_candidates(), image, "csim", IMG_W, IMG_H)
    cosim_rows, cosim_blob = build_suite(
        build_cosim_candidates(), image, "cosim", IMG_W, IMG_H)

    # Template-ceiling cases: EXACT reason words and EXACT geometry.
    #
    # These are `==`, not `& bit`.  A containment test passes when an
    # unexpected extra bit appears, and an extra bit here would mean the
    # geometry drifted into some other check — precisely the drift these rows
    # exist to catch.  0x024 = bits 2|5, 0x048 = bits 3|6.
    EXPECT_TW217 = (1 << R_TW) | (1 << R_PW)      # 0x024
    EXPECT_TH97  = (1 << R_TH) | (1 << R_PH)      # 0x048

    def check_ceiling(row, want_reason, want_w, want_h, where):
        got_w = row["x1"] - row["x0"]
        got_h = row["y1"] - row["y0"]
        assert row["reason"] == want_reason, (
            f"{where} {row['tag']}: reason 0x{row['reason']:03x}, expected "
            f"exactly 0x{want_reason:03x} (geometry {got_w}x{got_h} vs bound "
            f"{MAX_PATCH_W}x{MAX_PATCH_H})")
        assert (got_w, got_h) == (want_w, want_h), (
            f"{where} {row['tag']}: geometry {got_w}x{got_h}, expected "
            f"{want_w}x{want_h} — if this clipped, the case no longer reaches "
            f"the bound it is testing")
        assert not row["valid"], f"{where} {row['tag']} must be invalid"

    csim_by_tag = {r["tag"]: r for r in csim_rows}
    check_ceiling(csim_by_tag["tw-217-first-illegal"],
                  EXPECT_TW217, 823, 307, "csim")
    check_ceiling(csim_by_tag["th-97-first-illegal"],
                  EXPECT_TH97, 820, 310, "csim")

    # The same two rows in the COSIM manifest, which is the only one that
    # reaches RTL (run_hls.tcl passes -argv "cosim").  Without these the
    # post-clip size bits are csim-only.
    cosim_by_tag = {r["tag"]: r for r in cosim_rows}
    check_ceiling(cosim_by_tag["cosim-tw-217"], EXPECT_TW217, 823, 307, "cosim")
    check_ceiling(cosim_by_tag["cosim-th-97"],  EXPECT_TH97,  820, 310, "cosim")

    # Rejects must contribute no pixels, so adding them must not move the
    # cosim golden.  Asserted rather than eyeballed: a reject that leaked even
    # one beat would desynchronise every later patch in the batch.
    n_cosim_reject = sum(1 for r in cosim_rows if not r["valid"])
    assert n_cosim_reject == 3, (
        f"expected 3 cosim rejects, found {n_cosim_reject}")
    assert all(r["count"] == 0 for r in cosim_rows if not r["valid"]), (
        "an invalid cosim row claims pixel payload")
    assert len(cosim_blob) == 262748, (
        f"cosim golden moved to {len(cosim_blob)} bytes — metadata-only "
        f"rejects must not change the pixel total")

    # And the largest legal template must stay valid — cosim-max-legal is
    # exactly 820x307 and unclipped, so it proves the bound admits what it
    # must rather than merely rejecting.
    maxlegal = cosim_by_tag["cosim-max-legal"]
    assert (maxlegal["x1"] - maxlegal["x0"] == MAX_PATCH_W
            and maxlegal["y1"] - maxlegal["y0"] == MAX_PATCH_H), (
        f"cosim-max-legal must be exactly {MAX_PATCH_W}x{MAX_PATCH_H}, got "
        f"{maxlegal['x1'] - maxlegal['x0']}x{maxlegal['y1'] - maxlegal['y0']}")
    assert maxlegal["valid"] and maxlegal["reason"] == 0, (
        "cosim-max-legal must remain VALID at the exact bound")

    image_path = out / "tb_patch_extract_image.bin"
    image_path.write_bytes(image.tobytes())
    print(f"Written: {image_path.name} ({image.nbytes} bytes)")

    write_suite(csim_rows, csim_blob,
                out / "tb_patch_extract_cases_csim.txt",
                out / "tb_patch_extract_golden_csim.bin",
                IMG_W, IMG_H)
    write_suite(cosim_rows, cosim_blob,
                out / "tb_patch_extract_cases_cosim.txt",
                out / "tb_patch_extract_golden_cosim.bin",
                IMG_W, IMG_H)

    # ---- High-coordinate suite ------------------------------------------
    hi_image = build_image(HI_IMG_W, HI_IMG_H)
    print(f"Hi-coord image: {HI_IMG_W}x{HI_IMG_H} px (procedural, not "
          f"written to disk)")
    hi_rows, hi_blob = build_suite(
        build_hicoord_candidates(), hi_image, "hicoord", HI_IMG_W, HI_IMG_H)

    # The boundary cases must land on the documented reason bits exactly.
    # These are the assertions that make 820x307 a tested bound rather than a
    # constant somebody edited.
    by_tag = {r["tag"]: r for r in hi_rows}

    fit = by_tag["hi-boundary-820"]
    assert fit["x1"] - fit["x0"] == MAX_PATCH_W, (
        f"max_tw=216 must produce an unclipped {MAX_PATCH_W}-wide patch, got "
        f"{fit['x1'] - fit['x0']}")
    assert fit["y1"] - fit["y0"] == MAX_PATCH_H, (
        f"max_th=96 must produce an unclipped {MAX_PATCH_H}-row patch, got "
        f"{fit['y1'] - fit['y0']}")
    assert fit["valid"] and fit["reason"] == 0, (
        "the largest legal template must produce a VALID patch exactly at the "
        f"bound — got valid={fit['valid']} reason=0x{fit['reason']:03x}")

    # Same exact-equality treatment as the csim/cosim ceiling rows, on a page
    # wide enough that nothing clips.
    check_ceiling(by_tag["hi-overflow-823"],  EXPECT_TW217, 823, 307, "hicoord")
    check_ceiling(by_tag["hi-overflow-h310"], EXPECT_TH97,  820, 310, "hicoord")

    write_hicoord_suite(hi_rows, hi_blob, hi_image,
                        out / "tb_patch_extract_cases_hicoord.txt",
                        out / "tb_patch_extract_golden_hicoord.bin")

    n_legal = sum(r["valid"] for r in csim_rows)
    print(f"\ncsim: {n_legal}/{len(csim_rows)} candidates valid")
    print(f"cosim golden beats: {len(cosim_blob)}")


if __name__ == "__main__":
    main()
