"""
Generate testbench golden data for tme_tb.cpp.

Run from the template_match/ directory (hls/.venv python):
    python tme_generate_golden.py

Self-contained: every case is procedural (seeded numpy), no PDF render, no
sw/ imports.  Two oracles per case:

  1. EXACT integer window sums (int64 integral images + FFT cross-correlation
     rounded back to integers, spot-verified against direct dot products),
     normalised in float64:
         score = (N*STI - ST*SI) / sqrt((N*STT - ST^2) * (N*SII - SI^2))
     This is algebraically cv2's TM_CCOEFF_NORMED scaled by N/N and is the
     same arithmetic tme_top.cpp performs (integer sums, one float
     sqrt/divide), so it is the golden that gets WRITTEN.
  2. cv2.matchTemplate(TM_CCOEFF_NORMED) as the independent reference —
     asserted against oracle 1 per case (score and argmax), never shipped.

The suite exists to discharge the verification gap named in contract §9 /
run_hls.tcl item 2: location assertions, a unique nonzero match, the final
result row/column (the §4.4 +1 fix), patch==template equality, and the
maximum-storage 820x307/216x96 case with near-maximum window energies.
Peak uniqueness is enforced by construction: each randomised case is
seed-searched until the best score clears the runner-up by MIN_MARGIN, so
the TB may assert the DUT's (x, y) EXACTLY, not approximately.

Writes (manifest header: "n_cases patch_blob_bytes templ_blob_bytes";
row: "index pw ph tw th patch_off templ_off score x y margin category tag"):

    tb_tme_cases_csim.txt  / tb_tme_patches_csim.bin  / tb_tme_templs_csim.bin
    tb_tme_cases_cosim.txt / tb_tme_patches_cosim.bin / tb_tme_templs_cosim.bin
    tb_tme_cases_hw.txt    / tb_tme_patches_hw.bin    / tb_tme_templs_hw.bin

The cosim manifest is the small subset run_hls.tcl selects with -argv
"cosim"; anything only in the csim manifest never reaches RTL.

The hw manifest is what sw/tme_standalone_bringup.py sends to the board: the
cosim cases plus the two 820 x 307 stress cases, which stress different things.

`stress-max-envelope` is the point of the suite's existence — its patch is
251,740 bytes, the single AXI DMA transfer contract §3.1 bounds at 262,143
(2^18 - 1, from the DMA's 18-bit c_sg_length_width).  The small cases are all
under 15 KB, so a run limited to them verifies arithmetic and says nothing
about that bound.  Putting it in *cosim* instead would not help: at ~190M
cycles it is hours of xsim, and an RTL simulation containing no DMA cannot
speak to a DMA length bound anyway.  It is a block-design property, so silicon
is the only place it can be tested.

`stress-max-result` covers the other axis.  The envelope case maximises
storage but its result map is only 605 x 212; MAX_RESULT_W/H are 817/304,
sized for the smallest legal 4 x 4 template.  Until it was added nothing
wrote the top 212 accumulator entries or ran norm_cols past u = 604.

Three cases exist purely to make the *score* worth transporting: before them
every score in cosim and hw was exactly 0.0 or 1.0 — 0x00000000 and
0x3F800000 — which between them exercise no sign bit and one mantissa bit,
on a value that crosses AXI4-Lite as raw IEEE-754 for software to
reinterpret.  `equality-negative` (-0.73) and `equality-different` (0.0096)
fix that.
"""

from pathlib import Path

import numpy as np
import cv2

# Contract §4.1 / tme_top.h limits — keep in sync.
MAX_PATCH_W, MAX_PATCH_H = 820, 307
MAX_TEMPL_W, MAX_TEMPL_H = 216, 96
MIN_TEMPL_DIM = 4
MAX_CASES = 64          # TB manifest bound

MIN_MARGIN = 0.02       # best-vs-runner-up separation for exact-loc asserts
SCORE_XCHECK = 2e-3     # oracle-1 vs cv2 agreement bound
N_SPOT = 12             # direct dot-product spot checks on the FFT STI

# Contract §3.1: one patch is one AXI DMA transfer, and the bring-up platform's
# DMA reports buffer_max_size = 262,143 (2^18 - 1, from an 18-bit
# c_sg_length_width).  This is a BLOCK-DESIGN parameter, invisible from any
# source file here — it is repeated in sw/tme_standalone_bringup.py, which
# checks it against the DMA's own reported value at run time rather than
# trusting this constant.
DMA_MAX_BYTES = 262143


def bin_noise(rng, h, w, density):
    """Binary image, THRESH_BINARY_INV world: ink = 255, background = 0."""
    return ((rng.random((h, w)) < density) * 255).astype(np.uint8)


def window_sums(a, th, tw):
    """Exact int64 sliding-window sums of `a` over th x tw windows."""
    h, w = a.shape
    ii = np.zeros((h + 1, w + 1), np.int64)
    ii[1:, 1:] = a.astype(np.int64).cumsum(0).cumsum(1)
    rh, rw = h - th + 1, w - tw + 1
    return (ii[th:th + rh, tw:tw + rw] - ii[0:rh, tw:tw + rw]
            - ii[th:th + rh, 0:rw] + ii[0:rh, 0:rw])


def score_map(patch, templ):
    """Oracle 1: exact integer sums, float64 normalisation.  Returns the
    (rh, rw) float64 map plus the peak window ΣI² (for stress asserts)."""
    ph, pw = patch.shape
    th, tw = templ.shape
    rh, rw = ph - th + 1, pw - tw + 1
    P = patch.astype(np.int64)
    T = templ.astype(np.int64)
    n = tw * th

    si = window_sums(patch, th, tw)
    sii = window_sums(P * P, th, tw)
    st = int(T.sum())
    stt = int((T * T).sum())
    dt = n * stt - st * st
    assert dt > 0, "flat template — meaningless case, fix the builder"

    # Cross-correlation ΣTI via zero-padded FFT, rounded back to the exact
    # integer it represents.  Worst-case FFT noise here is ~1, far under the
    # 0.5 rounding radius; the spot checks below make that an assertion
    # rather than a hope.
    s0, s1 = ph + th, pw + tw
    f = np.fft.rfft2(P, s=(s0, s1)) * np.conj(np.fft.rfft2(T, s=(s0, s1)))
    sti = np.rint(np.fft.irfft2(f, s=(s0, s1))[:rh, :rw]).astype(np.int64)

    spot = np.random.default_rng(0xC0FFEE)
    for _ in range(N_SPOT):
        v = int(spot.integers(0, rh))
        u = int(spot.integers(0, rw))
        direct = int((P[v:v + th, u:u + tw] * T).sum())
        assert direct == sti[v, u], f"FFT STI off at ({u},{v}): {sti[v, u]} != {direct}"

    num = n * sti - st * si
    di = n * sii - si * si
    assert (di >= 0).all()
    denom = np.sqrt(np.float64(dt) * di.astype(np.float64))
    with np.errstate(divide="ignore", invalid="ignore"):
        smap = np.where(di == 0, 0.0, num / np.where(denom == 0, 1.0, denom))
    return smap, int(sii.max())


def golden(patch, templ):
    """Peak of oracle 1, first-occurrence row-major (the tie-break both
    cv2.minMaxLoc and the DUT's strict > comparison implement), plus the
    margin over the runner-up.  Cross-checks oracle 2 (cv2)."""
    smap, sii_max = score_map(patch, templ)
    rh, rw = smap.shape
    flat_idx = int(np.argmax(smap))
    gy, gx = divmod(flat_idx, rw)
    score = float(smap[gy, gx])
    if smap.size > 1:
        rest = smap.copy()
        rest.flat[flat_idx] = -np.inf
        margin = score - float(rest.max())
    else:
        margin = 999.0

    ref = cv2.matchTemplate(patch, templ, cv2.TM_CCOEFF_NORMED)
    assert ref.shape == (rh, rw), (
        f"cv2 map {ref.shape} vs (pw-tw+1, ph-th+1)=({rh},{rw}) — §4.4 drift")
    assert abs(float(ref[gy, gx]) - score) <= SCORE_XCHECK, (
        f"oracle disagreement at ({gx},{gy}): {score} vs cv2 {ref[gy, gx]}")
    if smap.size > 1 and margin >= 0.01:
        ry, rx = divmod(int(np.argmax(ref)), rw)
        assert (rx, ry) == (gx, gy), (
            f"cv2 argmax ({rx},{ry}) != exact argmax ({gx},{gy}) "
            f"despite margin {margin:.4f}")
    return score, gx, gy, margin, sii_max


def check_envelope(patch, templ):
    ph, pw = patch.shape
    th, tw = templ.shape
    assert MIN_TEMPL_DIM <= tw <= MAX_TEMPL_W, f"tw {tw}"
    assert MIN_TEMPL_DIM <= th <= MAX_TEMPL_H, f"th {th}"
    assert tw <= pw <= MAX_PATCH_W, f"pw {pw}"      # equality legal (§4.4 opt 1)
    assert th <= ph <= MAX_PATCH_H, f"ph {ph}"


def solve(tag, category, builder, base_seed, min_margin=MIN_MARGIN,
          max_tries=60):
    """Deterministic seed search: first seed whose case meets every
    constraint wins.  builder(rng) -> (patch, templ, expect_loc or None)."""
    for k in range(max_tries):
        rng = np.random.default_rng(base_seed * 1000 + k)
        patch, templ, expect = builder(rng)
        check_envelope(patch, templ)
        score, gx, gy, margin, sii_max = golden(patch, templ)
        if expect is not None and (gx, gy) != expect:
            continue
        if margin < min_margin:
            continue
        return dict(tag=tag, category=category, patch=patch, templ=templ,
                    score=score, x=gx, y=gy, margin=margin, sii_max=sii_max)
    raise RuntimeError(f"{tag}: no seed met constraints in {max_tries} tries")


# ---- Case builders -------------------------------------------------------

def planted(rng, pw, ph, tw, th, ux, uy, density=0.3):
    """Noise patch with the template cut from (ux, uy) — an exact match
    there, so the expected peak location is known by construction."""
    patch = bin_noise(rng, ph, pw, density)
    templ = patch[uy:uy + th, ux:ux + tw].copy()
    return patch, templ, (ux, uy)


def build_csim():
    cases = []

    # Blank patch: every window flat, every score exactly 0, peak (0,0) by
    # first-occurrence.  Continuity with the old sole golden — but now it is
    # one case among many instead of the entire suite.
    def blank(rng):
        patch = np.zeros((120, 200), np.uint8)
        templ = bin_noise(rng, 24, 40, 0.3)
        return patch, templ, (0, 0)
    c = solve("blank-patch", "degenerate", blank, 101, min_margin=0.0)
    assert c["score"] == 0.0
    cases.append(c)

    # Flat and non-flat windows in one search: the di==0 short-circuit must
    # not outscore or displace the real peak.
    def half_blank(rng):
        patch = bin_noise(rng, 160, 240, 0.3)
        patch[:, :120] = 0
        return patch, patch[70:102, 150:198].copy(), (150, 70)
    cases.append(solve("half-blank-peak", "peak", half_blank, 102))

    cases.append(solve("peak-interior", "peak",
                       lambda r: planted(r, 300, 200, 48, 32, 137, 61), 103))

    # The §4.4 cases: peaks at the final column and/or row of the result
    # map.  A DUT still computing pw-tw instead of pw-tw+1 cannot reach
    # these locations and fails the exact-loc assert.
    cases.append(solve("peak-final-corner", "peak",
                       lambda r: planted(r, 260, 140, 36, 28, 224, 112), 104))
    cases.append(solve("peak-final-col", "peak",
                       lambda r: planted(r, 220, 150, 32, 24, 188, 63), 105))
    cases.append(solve("peak-final-row", "peak",
                       lambda r: planted(r, 220, 150, 32, 24, 95, 126), 106))

    # patch == template: exactly one search position (§4.4 option 1 makes
    # equality legal; the map is 1x1, not empty).
    def eq_identical(rng):
        patch = bin_noise(rng, 48, 64, 0.35)
        return patch, patch.copy(), (0, 0)
    c = solve("equality-identical", "equality", eq_identical, 107)
    assert c["score"] > 0.999
    cases.append(c)

    # Same geometry, template unrelated to the patch: still one position,
    # whose (near-zero, possibly negative) score must be reported as-is.
    # Its 0.009578 is also the only non-round mantissa in the small cases, so
    # build_cosim lifts it.
    def eq_different(rng):
        patch = bin_noise(rng, 48, 64, 0.35)
        templ = bin_noise(rng, 48, 64, 0.35)
        return patch, templ, (0, 0)
    cases.append(solve("equality-different", "equality", eq_different, 108,
                       min_margin=0.0))

    # A winning score that is NEGATIVE.  Every other case in every suite
    # reports a best score of exactly 0.0, exactly 1.0, or a small positive —
    # so the sign bit of `result_score` was never exercised anywhere, and on
    # hardware it rides an AXI4-Lite register as raw IEEE-754 bits that
    # software reinterprets.  `anti-match` does not cover this: it puts -1.0
    # in the result *map* but its reported best is +0.12 elsewhere.
    #
    # A 1x1 result map is what forces the issue — with one position there is
    # nowhere better to go, so the anti-correlated score is the answer.  The
    # quarter-randomisation keeps it off exactly -1.0 (0xBF800000), which
    # would be as round a bit pattern as the 1.0 it replaces.
    def eq_negative(rng):
        patch = bin_noise(rng, 48, 64, 0.5)
        templ = (255 - patch).astype(np.uint8)
        keep = rng.random((48, 64)) < 0.25
        templ = np.where(keep, bin_noise(rng, 48, 64, 0.5), templ)
        return patch, templ.astype(np.uint8), (0, 0)
    c = solve("equality-negative", "equality", eq_negative, 116,
              min_margin=0.0)
    assert c["score"] < -0.2, (
        f"equality-negative scored {c['score']:.4f} — not negative enough to "
        f"be a sign-bit test")
    assert abs(c["score"] + 1.0) > 0.01, "score landed on exactly -1.0"
    cases.append(c)

    # Equality in one dimension only: single-column / single-row maps.
    cases.append(solve("equality-width", "equality",
                       lambda r: planted(r, 120, 90, 120, 40, 0, 33), 109))
    cases.append(solve("equality-height", "equality",
                       lambda r: planted(r, 200, 80, 56, 80, 77, 0), 110))

    # Smallest legal template.  A 4x4 binary pattern recurs by chance, so
    # this leans on the seed search for uniqueness.
    cases.append(solve("min-templ-4x4", "edge",
                       lambda r: planted(r, 40, 30, 4, 4, 17, 9), 111))

    # Inverted slice: exact anti-correlation (-1.0) at the cut location.
    # The best score is a modest noise match elsewhere — exercises negative
    # scores and proves best-tracking is not fooled by |score|.
    def anti(rng):
        patch = bin_noise(rng, 150, 200, 0.3)
        templ = (255 - patch[40:72, 60:108]).astype(np.uint8)
        return patch, templ, None
    c = solve("anti-match", "edge", anti, 112)
    assert c["score"] < 0.9, "anti-match found a perfect match?"
    cases.append(c)

    # General uint8 robustness — the pipeline only ever sends 0/255, but the
    # arithmetic must not depend on that.
    def gray(rng):
        patch = rng.integers(0, 256, (160, 240), dtype=np.uint8)
        templ = patch[90:122, 150:198].copy()
        return patch, templ, (150, 90)
    cases.append(solve("grayscale-random", "robustness", gray, 113))

    # Maximum storage, near-maximum energies: full 820x307 patch, full
    # 216x96 template cut from the bottom-right corner — final row AND
    # column at the full envelope — with the template region densified so
    # the window ΣI² approaches its 1.348e9 ceiling.  This is the case that
    # wraps the previous ap_fixed accumulators.
    def stress_max(rng):
        patch = bin_noise(rng, MAX_PATCH_H, MAX_PATCH_W, 0.6)
        y0, x0 = MAX_PATCH_H - MAX_TEMPL_H, MAX_PATCH_W - MAX_TEMPL_W
        patch[y0:, x0:] = bin_noise(rng, MAX_TEMPL_H, MAX_TEMPL_W, 0.92)
        return patch, patch[y0:, x0:].copy(), (x0, y0)
    c = solve("stress-max-envelope", "stress", stress_max, 114)
    assert c["sii_max"] > 1.1e9, (
        f"stress window ΣI² {c['sii_max']:.3e} too low to stress sumsq_t")
    assert (c["x"], c["y"]) == (MAX_PATCH_W - MAX_TEMPL_W,
                                MAX_PATCH_H - MAX_TEMPL_H)
    cases.append(c)

    # Same envelope, peak near the origin (band v=0 side of the search).
    cases.append(solve("stress-max-origin", "stress",
                       lambda r: planted(r, MAX_PATCH_W, MAX_PATCH_H,
                                         MAX_TEMPL_W, MAX_TEMPL_H, 3, 2,
                                         density=0.55), 115))

    # MAXIMUM RESULT MAP — a different stress axis from the two above.
    #
    # stress-max-envelope maximises *storage* (the 216x96 template fills
    # templ_buf) but its result map is only 605 x 212.  MAX_RESULT_W/H are
    # 817/304, sized for the smallest legal 4x4 template, and nothing reached
    # them: the top 212 entries of sti_col/sii_col/si_col were never written,
    # and isq_slide/norm_cols never ran past u = 604.  This case is the one
    # that fills those arrays to their declared bounds and puts the peak at
    # (816, 303) — the final column AND row of the largest possible map.
    #
    # Grayscale, not binary, and deliberately so: a 4x4 binary window recurs
    # by chance within a few hundred positions, and there are 248,368 of them
    # here, so a binary build could not have a unique peak to assert.  With
    # 8-bit pixels a 4x4 window is effectively unique.
    def stress_max_result(rng):
        patch = rng.integers(0, 256, (MAX_PATCH_H, MAX_PATCH_W), dtype=np.uint8)
        ux, uy = MAX_PATCH_W - MIN_TEMPL_DIM, MAX_PATCH_H - MIN_TEMPL_DIM
        templ = patch[uy:uy + MIN_TEMPL_DIM, ux:ux + MIN_TEMPL_DIM].copy()
        return patch, templ, (ux, uy)
    c = solve("stress-max-result", "stress", stress_max_result, 117)
    rw = MAX_PATCH_W - MIN_TEMPL_DIM + 1
    rh = MAX_PATCH_H - MIN_TEMPL_DIM + 1
    assert (rw, rh) == (817, 304), f"result map {rw}x{rh}, expected 817x304"
    assert (c["x"], c["y"]) == (rw - 1, rh - 1), (
        f"peak at ({c['x']},{c['y']}), expected the final cell "
        f"({rw - 1},{rh - 1})")
    cases.append(c)

    # Randomised geometry sweep.
    rng_geo = np.random.default_rng(20260804)
    for i in range(6):
        tw = int(rng_geo.integers(8, MAX_TEMPL_W + 1))
        th = int(rng_geo.integers(8, MAX_TEMPL_H + 1))
        pw = int(rng_geo.integers(tw, min(tw + 300, MAX_PATCH_W) + 1))
        ph = int(rng_geo.integers(th, min(th + 150, MAX_PATCH_H) + 1))
        ux = int(rng_geo.integers(0, pw - tw + 1))
        uy = int(rng_geo.integers(0, ph - th + 1))
        dens = float(rng_geo.uniform(0.15, 0.6))
        cases.append(solve(
            f"random-{i:02d}-{pw}x{ph}-t{tw}x{th}", "random",
            lambda r, pw=pw, ph=ph, tw=tw, th=th, ux=ux, uy=uy, dens=dens:
                planted(r, pw, ph, tw, th, ux, uy, dens),
            200 + i))

    return cases


def pick(cases, tag):
    """Lift an already-solved case out of another suite, by tag.

    Taking the object rather than re-running the builder makes byte-identity
    structural: every suite carrying a given tag carries the same pixels, and
    an edit to one builder cannot silently desync them.
    """
    for c in cases:
        if c["tag"] == tag:
            return c
    raise RuntimeError(
        f"no case tagged '{tag}' to lift — if it was renamed, rename it here "
        f"too rather than dropping it from the suite that needs it")


def build_cosim(csim_cases):
    """Small enough to finish in RTL, broad enough to matter: equality,
    final-corner (§4.4), interior peak, all-flat, minimum template — plus two
    cases carrying scores that are neither 0.0 nor 1.0.

    That last part is not cosmetic.  Before they were added, every score in
    this suite (and in `hw`, which is built from it) was exactly 0.0
    (0x00000000) or exactly 1.0 (0x3F800000) — two bit patterns that between
    them exercise one mantissa bit and no sign bit.  `result_score` crosses
    AXI4-Lite as raw IEEE-754 bits that software reinterprets, so a suite made
    of round numbers is a weak test of that path.  Both additions are 64x48
    equality cases: one search position each, so they cost nothing in xsim.
    """
    cases = []

    def eq_identical(rng):
        patch = bin_noise(rng, 48, 64, 0.35)
        return patch, patch.copy(), (0, 0)
    c = solve("cosim-eq-identical", "equality", eq_identical, 301)
    assert c["score"] > 0.999
    cases.append(c)

    cases.append(solve("cosim-final-corner", "peak",
                       lambda r: planted(r, 80, 56, 20, 14, 60, 42), 302))
    cases.append(solve("cosim-interior", "peak",
                       lambda r: planted(r, 64, 48, 16, 12, 23, 17), 303))

    def blank(rng):
        patch = np.zeros((48, 64), np.uint8)
        templ = bin_noise(rng, 12, 16, 0.3)
        return patch, templ, (0, 0)
    c = solve("cosim-blank", "degenerate", blank, 304, min_margin=0.0)
    assert c["score"] == 0.0
    cases.append(c)

    cases.append(solve("cosim-min-4x4", "edge",
                       lambda r: planted(r, 40, 30, 4, 4, 17, 9), 305))

    # The two non-round scores, lifted from csim so all three suites run the
    # same pixels: a small positive (~0.0096) and a negative (~-0.5).
    cases.append(pick(csim_cases, "equality-different"))
    cases.append(pick(csim_cases, "equality-negative"))

    return cases


def build_hw(cosim_cases, csim_cases):
    """The silicon suite: every cosim case, plus the two 820x307 stress cases.

    Both stress cases are lifted from the already-solved csim list (see
    `pick`), and they stress different things:

      stress-max-envelope  216x96 template — maximum STORAGE, and the
                           251,740-byte single DMA transfer that is the only
                           reason this suite exists (contract §3.1).
      stress-max-result    4x4 template — maximum RESULT MAP, 817x304, which
                           is what MAX_RESULT_W/H are actually sized for.  The
                           envelope case only reaches 605x212, so without this
                           the top 212 accumulator entries are never touched
                           on hardware.

    `stress-max-result` is deliberately NOT in the cosim suite: at 820x307 it
    is far too slow for xsim, and like the envelope case it tests something
    (array bounds under a real DMA-fed patch) that RTL simulation of a design
    with no DMA cannot speak to.
    """
    stress = pick(csim_cases, "stress-max-envelope")
    cases = list(cosim_cases) + [stress, pick(csim_cases, "stress-max-result")]

    # §3.1 is the reason this suite exists, so assert both halves of it: the
    # big case must actually be the full envelope, and every case must fit one
    # transfer.  A patch over the bound would be truncated by the DMA and the
    # matcher would correlate against whatever the tail of its BRAM held.
    ph, pw = stress["patch"].shape
    assert (pw, ph) == (MAX_PATCH_W, MAX_PATCH_H), (
        f"stress case is {pw}x{ph}, not the {MAX_PATCH_W}x{MAX_PATCH_H} "
        f"envelope — it no longer exercises §3.1")
    assert pw * ph == 251740, f"envelope moved: {pw}x{ph} = {pw * ph} B"
    for c in cases:
        h, w = c["patch"].shape
        assert w * h <= DMA_MAX_BYTES, (
            f"{c['tag']}: patch {w}x{h} = {w * h} B exceeds the §3.1 "
            f"single-transfer bound of {DMA_MAX_BYTES} B")
        th, tw = c["templ"].shape
        assert tw * th <= DMA_MAX_BYTES, f"{c['tag']}: template too large"

    print(f"\nhw suite §3.1 headroom: {DMA_MAX_BYTES - pw * ph:,} B "
          f"({pw * ph} of {DMA_MAX_BYTES} used by stress-max-envelope)")
    return cases


# ---- Emission ------------------------------------------------------------

def write_suite(cases, name, out=Path(".")):
    assert len(cases) <= MAX_CASES
    tags = [c["tag"] for c in cases]
    assert len(set(tags)) == len(tags), "duplicate tag"

    patches = bytearray()
    templs = bytearray()
    rows = []
    for i, c in enumerate(cases):
        ph, pw = c["patch"].shape
        th, tw = c["templ"].shape
        rows.append(
            f"{i} {pw} {ph} {tw} {th} {len(patches)} {len(templs)} "
            f"{c['score']:.6f} {c['x']} {c['y']} {min(c['margin'], 999.0):.6f} "
            f"{c['category']} {c['tag']}")
        patches += c["patch"].tobytes()
        templs += c["templ"].tobytes()

    header = f"{len(cases)} {len(patches)} {len(templs)}"
    (out / f"tb_tme_cases_{name}.txt").write_text(
        "\n".join([header] + rows) + "\n")
    (out / f"tb_tme_patches_{name}.bin").write_bytes(bytes(patches))
    (out / f"tb_tme_templs_{name}.bin").write_bytes(bytes(templs))

    print(f"\n{name}: {len(cases)} cases, patches {len(patches)} B, "
          f"templates {len(templs)} B")
    for r in rows:
        print("  " + r)


def main():
    csim = build_csim()
    cosim = build_cosim(csim)
    write_suite(csim, "csim")
    write_suite(cosim, "cosim")
    write_suite(build_hw(cosim, csim), "hw")
    print("\nOK — csim/cosim via run_hls.tcl; the hw suite goes to the board "
          "with sw/tme_standalone_bringup.py (validate it first with "
          "run_hls.tcl's csim_design -argv \"hw\")")


if __name__ == "__main__":
    main()
