"""
Generate the PRODUCTION-GEOMETRY vector package — one suite, two consumers.

Run from the template_match/ directory (hls/.venv python):
    python tme_generate_production.py

WHY THIS EXISTS

Every vector the matcher has ever been verified against was chosen to make an
oracle tractable, not to look like the workload.  The largest non-stress case
in `tb_tme_cases_csim.txt` is a 300x200 patch under a 48x32 template; the two
stress cases are 820x307 envelope probes.  The real detector runs 622x300
patches under 164x94 templates and six other fixed shapes, and NONE of those
shapes has ever been executed by anything.

That gap is about to matter twice over:

  - 0.2 (the silicon-proven float build) needs a CHARACTERISATION baseline at
    production shape — the seconds-per-trial number that Phase 0's performance
    target is set against, and the score/location answers that any faster
    implementation has to reproduce.
  - 0.3 (the binary popcount redesign) needs an ACCEPTANCE oracle at the same
    shapes, in the terms 0.3 actually computes: exact integer counts, not a
    float score with a tolerance.

Using one package for both is the point.  If 0.3's acceptance vectors were
generated separately, "0.3 matches 0.2" would be a comparison between two
oracles rather than between two implementations, and every disagreement would
first have to be triaged as a possible vector defect.  Here the pixels are
literally the same bytes.

WHAT IS BINARY ABOUT IT

Contract 0.3 (Phase 4) narrows the input domain to strictly {0, 255}: the
detector's patches come out of `THRESH_BINARY_INV` and its templates are
preprocessed the same way, so the general uint8 domain 0.2 accepts was never
exercised by the application.  Every patch and template written here is
binary, which is what lets one file carry both oracles:

    I = 255*i, T = 255*t  with i, t in {0, 1}
      SI  = 255*B     SII = 255^2*B     ST = 255*A    STT = 255^2*A
      STI = 255^2*C
      num = 255^2*(N*C - A*B) = 255^2*q
      dt  = 255^2*A*(N-A)     di = 255^2*B*(N-B) = 255^2*d
      score = q / sqrt(A*(N-A) * d)

so the float score 0.2 reports and the integer (q, B) pair 0.3 reports are two
readings of the same window.  `check_oracles()` asserts that identity per case
rather than assuming it: the counts come from integral images and an integer
cross-correlation, the score comes from `tme_generate_golden.score_map`'s
255-scaled float64 path, and the two are computed from the pixels
independently.

WIDTHS, AND WHY 28 BITS IS NOT A GUESS

Phase 5 pins q to signed 28 bits.  Taken naively that looks wrong -- N*C and
A*B each reach N^2 = 20736^2 = 4.30e8, which needs 30 bits, and their
difference could be assumed to need the same.  It does not, because C is not
free: the Frechet bounds max(0, A+B-N) <= C <= min(A, B) hold for any two
subsets of an N-set.  Maximising N*C - A*B under them gives

    q_max = max_A A*(N-A)     = N^2/4
    q_min = -max_A A*(N-A)    = -N^2/4

so |q| <= N^2/4 = 1.0750e8 < 2^27 = 1.3422e8, and signed 28 bits holds it with
24.9% of headroom.  `check_widths()` re-derives that bound for every N in the
suite and asserts every observed q against it, so a future geometry change
that broke the argument would fail here rather than in silicon.  The same
argument gives d <= N^2/4 (27 unsigned bits) and, for the WITHIN-TEMPLATE
comparison the argmax actually performs (A and therefore A*(N-A) are constant
across one map, so they cancel),

    q1^2 * d2  vs  q2^2 * d1   <=  (N^2/4)^2 * (N^2/4) = 1.24e24 < 2^81

which is the 81-bit cross-product Phase 5 names.  Note what that means: the
81-bit width is only valid because the comparison never crosses templates.  A
future reduction that compared two DIFFERENT templates' windows in the PL
would have to carry A*(N-A) through and would need 107 bits.

THE LANE-15 CASE, AND WHY IT IS PROVED RATHER THAN HOPED

Phase 3 shrinks `correlation_core`'s segment load from the compile-time
SEG_W = PAR_COLS + MAX_TEMPL_W to a runtime seg_len = PAR_COLS + tw - 1.  The
off-by-one that change invites is seg_len - 1, and it is very nearly invisible:
`seg[p + x]` reaches index tw + 14 for exactly ONE (p, x) pair -- lane 15
reading the template's last column -- so a suite whose peaks all land on other
lanes passes the mutant unchanged.

The mutant is also simpler than it looks.  With seg_len = tw + 14 the element
at index tw + 14 is never written by ANY tile, so it is not "the previous
tile's value": it is whatever the register file held before the core ever ran,
one unknown constant S for the whole execution.  That makes the mutant exactly
modellable:

    STI_mut(u,v) = STI(u,v) + sum_y T[y, tw-1] * (S - P[v+y, u+tw-1])
                   for output columns u = 15 (mod 16), unchanged elsewhere

`lane15_survives_mutation()` therefore does not guess S.  It sweeps ALL 256
values and requires that every one of them moves the reported argmax.  A case
is only accepted if the mutation is detectable no matter what the registers
came up holding -- including S = 0 (post-reset RTL) and S = 255.

WRITES

    tb_tme_cases_prod.txt     same 13-column format as the csim/cosim/hw
    tb_tme_patches_prod.bin   manifests, so tme_tb.cpp and
    tb_tme_templs_prod.bin    sw/tme_standalone_bringup.py read it unchanged
                              -- this is the 0.2 characterisation path

    tb_tme_counts_prod.txt    the 0.3 acceptance sidecar: N, A, dA and the
                              exact (x, y, B, C, q, d) at the argmax, the
                              min-q window, the observed q/B extremes, the
                              flat-window census, and the planted probes

    tb_tme_prod.sha256        sha256sum -c record over all four

The blobs are ~1.5 MB and are deliberately NOT committed.  They are fully
determined by the pinned seeds and geometries below, so `sha256sum -c
tb_tme_prod.sha256` after a regeneration is stronger evidence than a committed
copy would be: it proves the generator still produces the vectors that were
verified, rather than proving a file was not edited.

THE GRAYSCALE SUITES ARE NOT TOUCHED.  tb_tme_cases_{csim,cosim,hw}.txt keep
their pixels, their hashes and their meaning as the historical record of what
0.2 was proved against on silicon (9/9, 2026-08-07).  Nothing here regenerates
them.
"""

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np

import tme_generate_golden as TME

# ---------------------------------------------------------------------------
# Pinned constants.  Every number below is part of the vector package: change
# one and the sha256 record stops matching, which is the intended alarm.
# ---------------------------------------------------------------------------

# tme_top.h -- keep in sync (checked against TME's copies in check_config()).
MAX_PATCH_W, MAX_PATCH_H = 820, 307
MAX_TEMPL_W, MAX_TEMPL_H = 216, 96
MAX_RESULT_W = MAX_PATCH_W - 4 + 1      # 817
MAX_RESULT_H = MAX_PATCH_H - 4 + 1      # 304
PAR_COLS = 16                           # correlation_core lane count

# Contract 3.1 single-transfer bound (18-bit c_sg_length_width).
DMA_MAX_BYTES = 262143

# Best-vs-runner-up separation that lets a consumer assert an EXACT location.
# Same role and value as MIN_MARGIN next door.
MIN_MARGIN = 0.02

# 4.6 / tme_tb.cpp MAX_SCORE_ERR: the tolerance 0.2 is verified to.  Recorded
# here because two things below are stated relative to it -- which near-tie
# pairs 0.2 cannot separate, and how large a lane-15 mutation has to be before
# a tolerance-based score assert would notice it on its own.
SCORE_TOL = 0.005

# Agreement bound between the two oracles.  Both are exact-integer quantities
# divided in float64, so they should agree to a few ULP; 1e-12 is four orders
# of magnitude of slack over what has ever been observed and still fails hard
# on a real algebra defect.
ORACLE_XCHECK = 1e-12

# Production geometries.  patch = ((12*tw)//5 + (7*tw)//5) x ((16*th)//5), the
# 4.5 envelope patch_extract_core builds for a template of tw x th -- so the
# template dimensions here are not chosen, they are back-solved from the patch
# shapes the detector actually produces.  check_config() re-derives every one.
#
#   622x300  left bank         tw=164 th=94
#   622x224  right bank        tw=164 th=70
#   421x214  base              tw=111 th=67
#   399x224  base              tw=105 th=70
#   456x300  base              tw=120 th=94
#   619x134  left ferrule      tw=163 th=42
#   619x124  right ferrule     tw=163 th=39
GEOMETRIES = {
    "left-bank":     (164, 94),
    "right-bank":    (164, 70),
    "base-421":      (111, 67),
    "base-399":      (105, 70),
    "base-456":      (120, 94),
    "ferrule-left":  (163, 42),
    "ferrule-right": (163, 39),
}

# Score targets, one per production shape.  Deterministic by CONSTRUCTION, not
# by seed search: the planted window's (B, C) are solved for the target, so the
# score is whatever the integers say and the only thing left to search is that
# the planted window really is the map's peak.
SCORE_TARGETS = {
    "left-bank":     0.70,
    "right-bank":    0.50,
    "base-421":      0.33,
    "base-399":      0.30,
    "base-456":      0.24,
    "ferrule-left":  0.20,
    "ferrule-right": 1.00,      # exact crop: the clean-detection reference
}

# Seeds.  Pinned individually so a change to one case cannot perturb another
# through a shared stream.
SEEDS = {
    "left-bank":     0x9E37_0001,
    "right-bank":    0x9E37_0002,
    "base-421":      0x9E37_0003,
    "base-399":      0x9E37_0004,
    "base-456":      0x9E37_0005,
    "ferrule-left":  0x9E37_0006,
    "ferrule-right": 0x9E37_0007,
    "negative-1x1":  0x9E37_0010,
    "tie-exact":     0x9E37_0020,
    "tie-near":      0x9E37_0021,
    "tie-float32":   0x9E37_0022,
    "flat-region":   0x9E37_0030,
    "lane15-small":  0x9E37_0040,
    "lane15-full":   0x9E37_0041,
    "max-result":    0x9E37_0050,
    "b1-lane15":     0x9E37_0120,
}

INK = 255
CAT_PROD = "production"
CAT_SIGN = "sign"
CAT_TIE = "tie"
CAT_LANE = "lane15"
CAT_BOUNDS = "bounds"
CAT_B1 = "b1-width"


def require(cond, msg: str = "") -> None:
    """`assert` that survives `python -O`.

    Same rule and the same reason as `tme_generate_golden.require`: every check
    in this file gates either an oracle or a written artifact, and this
    generator is expected to be runnable under -O as part of its own
    acceptance.  An assert would let that run publish vectors it had not
    checked.
    """
    if not cond:
        raise AssertionError(msg or "generator self-check failed")


# ---------------------------------------------------------------------------
# The binary count oracle
# ---------------------------------------------------------------------------

def bits(img: np.ndarray) -> np.ndarray:
    """{0, 255} uint8 -> {0, 1} int64, refusing anything else.

    The refusal is the contract check, not a convenience: Phase 4 makes a
    non-binary patch an input error the PS must reject before any DMA, and a
    vector generator that quietly accepted 128 would be writing goldens for a
    domain 0.3 does not implement.
    """
    u = np.unique(img)
    require(bool(np.isin(u, (0, INK)).all()),
            f"non-binary pixel values {u.tolist()[:8]} — contract 0.3 4.x "
            f"restricts patch and template pixels to {{0, {INK}}}")
    return (img.astype(np.int64) // INK)


def _window_sums_i64(a: np.ndarray, th: int, tw: int) -> np.ndarray:
    """Exact sliding-window sums over th x tw windows (integral image)."""
    h, w = a.shape
    ii = np.zeros((h + 1, w + 1), np.int64)
    ii[1:, 1:] = a.cumsum(0).cumsum(1)
    rh, rw = h - th + 1, w - tw + 1
    return (ii[th:th + rh, tw:tw + rw] - ii[0:rh, tw:tw + rw]
            - ii[th:th + rh, 0:rw] + ii[0:rh, 0:rw])


def _and_popcount(pb: np.ndarray, tb: np.ndarray) -> np.ndarray:
    """C(u,v) = |window(u,v) AND template|, exactly, as int64.

    FFT cross-correlation rounded back to the integer it represents, then
    VERIFIED two ways rather than trusted:

      - a full direct recomputation over a contiguous block of the map (see
        `verify_block` in count_maps), which is the check that would catch a
        systematic transform error;
      - scattered direct dot products, which is the check that would catch a
        localised one.

    Worst-case FFT round-off here is far under the 0.5 rounding radius (the
    magnitudes are counts, not 255-scaled products, so this is a much easier
    transform than the one in tme_generate_golden.score_map), but "far under"
    is not a proof and the checks are cheap.
    """
    ph, pw = pb.shape
    th, tw = tb.shape
    rh, rw = ph - th + 1, pw - tw + 1
    s0, s1 = ph + th, pw + tw
    f = np.fft.rfft2(pb, s=(s0, s1)) * np.conj(np.fft.rfft2(tb, s=(s0, s1)))
    return np.rint(np.fft.irfft2(f, s=(s0, s1))[:rh, :rw]).astype(np.int64)


def count_maps(patch: np.ndarray, templ: np.ndarray, *, block: int = 24,
               n_spot: int = 24, spot_seed: int = 0xB1A5) -> dict:
    """The 0.3 oracle: N, A, dA and the (B, C, q, d) maps, all exact int64.

    Independent of the float path in every step that could hide a defect --
    different summation (integral images vs FFT-of-255-scaled-products),
    different scale (counts vs 255^2-scaled sums), different normalisation
    (none vs a float64 divide).  `check_oracles()` then requires the two to
    agree, which is only evidence because they were computed separately.
    """
    pb, tb = bits(patch), bits(templ)
    ph, pw = pb.shape
    th, tw = tb.shape
    require(th <= ph and tw <= pw, f"template {tw}x{th} does not fit {pw}x{ph}")
    rh, rw = ph - th + 1, pw - tw + 1

    n = tw * th
    a = int(tb.sum())
    da = a * (n - a)
    if da <= 0:
        # 4.6, restated in count terms: A == 0 or A == N is a flat template,
        # which has no golden in any oracle.  A raise, not an assert, for the
        # same reason score_map's is.
        raise ValueError(
            f"flat template ({tw}x{th}, A={a} of N={n}): A*(N-A) = {da}. "
            f"Contract 4.6 makes this illegal input; no golden exists for it.")

    b = _window_sums_i64(pb, th, tw)
    c = _and_popcount(pb, tb)
    q = n * c - a * b
    d = b * (n - b)

    # Full direct recomputation of a contiguous block of C.  Anchored on the
    # map's argmax so the block always covers the window every downstream
    # assertion depends on.
    peak = int(np.argmax(np.where(d > 0, q, np.iinfo(np.int64).min)))
    py, px = divmod(peak, rw)
    y0 = max(0, min(py - block // 2, rh - min(block, rh)))
    x0 = max(0, min(px - block // 2, rw - min(block, rw)))
    y1, x1 = min(rh, y0 + block), min(rw, x0 + block)
    for v in range(y0, y1):
        rows = pb[v:v + th]
        for u in range(x0, x1):
            direct = int((rows[:, u:u + tw] * tb).sum())
            require(direct == int(c[v, u]),
                    f"C oracle disagrees at ({u},{v}): FFT {int(c[v, u])} vs "
                    f"direct {direct}")
    verified_block = (x0, y0, x1 - x0, y1 - y0)

    spot = np.random.default_rng(spot_seed)
    for _ in range(n_spot):
        v = int(spot.integers(0, rh))
        u = int(spot.integers(0, rw))
        direct = int((pb[v:v + th, u:u + tw] * tb).sum())
        require(direct == int(c[v, u]),
                f"C oracle disagrees at spot ({u},{v}): FFT {int(c[v, u])} vs "
                f"direct {direct}")

    # Frechet bounds.  These are what the 28-bit width argument rests on, so
    # assert them on the real data instead of citing them.
    lo = np.maximum(0, a + b - n)
    hi = np.minimum(a, b)
    require(bool(((c >= lo) & (c <= hi)).all()),
            "C violates the Frechet bounds max(0,A+B-N) <= C <= min(A,B) — "
            "the 28-bit q width argument does not hold for this map")

    return dict(n=n, a=a, da=da, b=b, c=c, q=q, d=d, rw=rw, rh=rh,
                verified_block=verified_block)


def exact_score(q: int, d: int, da: int) -> float:
    """float64 reading of one window's exact counts.  Flat window -> +0.0."""
    if d == 0:
        return 0.0
    return float(q) / float(np.sqrt(float(da) * float(d)))


def cmp_exact(qa: int, da_: int, qb: int, db: int) -> int:
    """+1 / 0 / -1 for window a vs window b under the Phase 5 exact rule.

    Sign classes first (positive > zero > negative, and a flat window IS zero
    by 4.4), then a squared cross-product WITHIN a class.  A*(N-A) is common to
    both windows -- one template per map -- so it cancels and never appears.

    The negative branch reverses, and that is the whole reason this is written
    out rather than left to `q*q*db > qb*qb*da`: for two negative scores the
    LARGER squared magnitude is the SMALLER (more negative) score.  Getting
    that backwards is invisible on every suite whose best score is positive,
    which until now has been every suite but one.
    """
    ca = 0 if (da_ == 0 or qa == 0) else (1 if qa > 0 else -1)
    cb = 0 if (db == 0 or qb == 0) else (1 if qb > 0 else -1)
    if ca != cb:
        return 1 if ca > cb else -1
    if ca == 0:
        return 0
    la = qa * qa * db
    lb = qb * qb * da_
    if ca > 0:
        return (la > lb) - (la < lb)
    return (la < lb) - (la > lb)


def exact_argmax(q: np.ndarray, d: np.ndarray, da: int) -> dict:
    """Row-major first-occurrence argmax under `cmp_exact`, done exactly.

    Two stages, and the narrowing stage is sound rather than convenient.  The
    float64 score of a window carries ~1e-16 relative error, so any pair
    separated by more than 1e-9 is ordered correctly by float alone; every pair
    NOT separated by 1e-9 is kept as a candidate and settled with Python
    integers, where no rounding exists.  So the float is used only to discard
    windows that cannot possibly win, never to choose among those that can.
    """
    rh, rw = q.shape
    cls = np.zeros(q.shape, np.int8)
    live = d > 0
    cls[live & (q > 0)] = 1
    cls[live & (q < 0)] = -1
    top = int(cls.max())

    flat_idx = np.flatnonzero((cls == top).ravel())
    if top == 0:
        # Every window is flat or exactly uncorrelated: all score +0.0, so the
        # answer is the first in row-major order and there is nothing to
        # compare.  cv2's minMaxLoc and the DUT agree here by construction.
        first = int(flat_idx[0])
        gy, gx = divmod(first, rw)
        return dict(x=gx, y=gy, n_exact_ties=int(flat_idx.size),
                    runner_up_gap=0.0, n_candidates=int(flat_idx.size))

    qf = q.ravel()[flat_idx].astype(np.float64)
    df = d.ravel()[flat_idx].astype(np.float64)
    sf = qf / np.sqrt(float(da) * df)
    best_f = float(sf.max())
    keep = flat_idx[sf >= best_f - 1e-9]
    require(keep.size <= 5000,
            f"{keep.size} windows are within 1e-9 of the peak — this map is "
            f"degenerate and no exact-location assertion should be built on "
            f"it; give the case a distinguishing structure instead")

    qr, dr = q.ravel(), d.ravel()
    best = int(keep[0])
    ties = 1
    for idx in keep[1:]:
        idx = int(idx)
        cmp_ = cmp_exact(int(qr[idx]), int(dr[idx]), int(qr[best]), int(dr[best]))
        if cmp_ > 0:
            best, ties = idx, 1
        elif cmp_ == 0:
            ties += 1                       # row-major first already held

    # Gap to the best STRICTLY worse window, in float64 score units.  Reported,
    # never used for a decision -- it is what tells a consumer whether the 0.2
    # float core can be expected to reproduce this location.
    gap = float("inf")
    best_s = exact_score(int(qr[best]), int(dr[best]), da)
    for idx in flat_idx:
        idx = int(idx)
        if cmp_exact(int(qr[idx]), int(dr[idx]), int(qr[best]), int(dr[best])) < 0:
            gap = min(gap, best_s - exact_score(int(qr[idx]), int(dr[idx]), da))
    if not np.isfinite(gap):
        # No window is strictly worse — a 1x1 map, or every window exactly
        # tied.  999.0 rather than 0.0, matching tme_generate_golden.golden's
        # convention for the same situation: the location is unambiguous
        # because there is nothing to be ambiguous with, and a 0 here would
        # read as "maximally ambiguous" to anything that ranks by margin.
        gap = 999.0

    gy, gx = divmod(best, rw)
    return dict(x=gx, y=gy, n_exact_ties=ties, runner_up_gap=gap,
                n_candidates=int(keep.size))


def dut_float32_argmax(patch: np.ndarray, templ: np.ndarray) -> dict:
    """What tme_top.cpp 0.2 would report, modelled in float32.

    Not a third oracle -- a MODEL OF THE DUT, and the difference matters.  The
    goldens here are exact integers, but 0.2 reduces in single precision:

        score = (float)num / hls::sqrtf(dt_f * (float)di)   then clamp [-1,1]
        if (score > best_score) { best = ...; }              strict >, first wins

    Everything up to `num`, `dt`, `di` is exact integer arithmetic in the DUT
    too, so the only question this answers is whether the float32 divide,
    square root and clamp can reorder the top of a map -- which is precisely
    the question the near-tie cases exist to ask, and the one that decides
    whether 0.2 may be held to a case's exact location at all.

    Run for every case, not just the ties: a production-shape map has ~95,000
    windows and the largest `num` values run to 41 bits, so "float32 obviously
    resolves it" is an assumption worth checking once rather than repeating.
    """
    p = patch.astype(np.int64)
    t = templ.astype(np.int64)
    ph, pw = p.shape
    th, tw = t.shape
    rh, rw = ph - th + 1, pw - tw + 1
    n = tw * th

    si = _window_sums_i64(p, th, tw)
    sii = _window_sums_i64(p * p, th, tw)
    st = int(t.sum())
    stt = int((t * t).sum())
    dt = n * stt - st * st

    s0, s1 = ph + th, pw + tw
    f = np.fft.rfft2(p, s=(s0, s1)) * np.conj(np.fft.rfft2(t, s=(s0, s1)))
    sti = np.rint(np.fft.irfft2(f, s=(s0, s1))[:rh, :rw]).astype(np.int64)

    num = n * sti - st * si
    di = n * sii - si * si

    dt_f = np.float32(dt)
    with np.errstate(divide="ignore", invalid="ignore"):
        prod = (dt_f * di.astype(np.float32)).astype(np.float32)
        score = (num.astype(np.float32)
                 / np.sqrt(np.where(prod == 0, np.float32(1.0),
                                    prod)).astype(np.float32))
    score = np.where(di == 0, np.float32(0.0), score).astype(np.float32)
    score = np.clip(score, np.float32(-1.0), np.float32(1.0)).astype(np.float32)

    # `best_score` starts at -2.0f and the comparison is strict, so this is a
    # row-major first-occurrence argmax -- exactly what np.argmax does.
    flat = int(np.argmax(score))
    gy, gx = divmod(flat, rw)
    return dict(x=gx, y=gy, score=float(score[gy, gx]))


def check_oracles(patch: np.ndarray, templ: np.ndarray, cm: dict) -> dict:
    """Require the count oracle and the 255-scaled float oracle to agree.

    This is the load-bearing check of the whole file.  0.3's acceptance
    argument is "the same pixels give the same answer", and it is only an
    argument if the two readings were produced by code that does not share a
    path.  Compared here: the full score map (so a defect at any window fails,
    not just at the peak) and the argmax under both rules.
    """
    smap, _ = TME.score_map(patch, templ)
    q, d, da = cm["q"], cm["d"], cm["da"]
    with np.errstate(divide="ignore", invalid="ignore"):
        cmap = np.where(d == 0, 0.0,
                        q.astype(np.float64)
                        / np.sqrt(float(da) * np.where(d == 0, 1.0,
                                                       d.astype(np.float64))))
    require(smap.shape == cmap.shape,
            f"map shapes disagree: float {smap.shape} vs counts {cmap.shape}")
    err = float(np.abs(smap - cmap).max())
    require(err <= ORACLE_XCHECK,
            f"count oracle and float oracle disagree by {err:.3e} (bound "
            f"{ORACLE_XCHECK:.0e}) — the binary identity "
            f"score = q/sqrt(A(N-A)*d) does not hold for this case, so the "
            f"0.2 and 0.3 goldens are not two readings of one window")

    ex = exact_argmax(q, d, da)
    fy, fx = divmod(int(np.argmax(smap)), smap.shape[1])
    return dict(exact=ex, float_x=fx, float_y=fy, map_err=err)


# ---------------------------------------------------------------------------
# Width bounds (Phase 5's declared integer widths, checked on real data)
# ---------------------------------------------------------------------------

def check_widths(cm: dict, tag: str) -> dict:
    """Assert the Frechet-derived width bounds and report what was observed."""
    n, a, da = cm["n"], cm["a"], cm["da"]
    q, d, b, c = cm["q"], cm["d"], cm["b"], cm["c"]
    q_bound = n * n // 4                      # max |q| over all legal (A,B,C)

    q_max, q_min = int(q.max()), int(q.min())
    require(max(abs(q_max), abs(q_min)) <= q_bound,
            f"{tag}: |q| reached {max(abs(q_max), abs(q_min))} against the "
            f"Frechet bound N^2/4 = {q_bound} — the signed-28-bit q width "
            f"argument is broken for this geometry")
    require(q_bound < (1 << 27),
            f"{tag}: N^2/4 = {q_bound} does not fit signed 28 bits")
    require(int(d.max()) <= q_bound and da <= q_bound,
            f"{tag}: d or A(N-A) exceeded N^2/4")
    require(n * int(c.max()) < (1 << 29) and a * int(b.max()) < (1 << 29),
            f"{tag}: N*C or A*B does not fit unsigned 29 bits (signed 30)")

    # The within-template cross-product Phase 5 forms as unsigned 81 bits.
    xprod = max(abs(q_max), abs(q_min)) ** 2 * int(d.max())
    require(xprod < (1 << 81),
            f"{tag}: |q|^2 * d reached {xprod} — over the 81-bit cross-product")
    return dict(q_bound=q_bound, q_max=q_max, q_min=q_min,
                b_min=int(b.min()), b_max=int(b.max()),
                d_max=int(d.max()), xprod_bits=int(xprod).bit_length())


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def patch_shape(tw: int, th: int) -> tuple:
    """The 4.5 envelope patch for a tw x th template — the production shape."""
    return (12 * tw) // 5 + (7 * tw) // 5, (16 * th) // 5


def solve_counts(n: int, a: int, target: float, b: int | None = None) -> tuple:
    """(B, C) whose exact score is as close to `target` as the integers allow.

    Deliberately not a search over pixels.  Fixing the planted window's ink
    count B and its overlap C fixes the score exactly -- score = (N*C - A*B) /
    sqrt(A(N-A) * B(N-B)) -- so the target is hit by construction and the only
    thing left to verify is that the planted window is the map's peak.  A seed
    search over noise would have to hit the target by luck and would move
    whenever any unrelated constant changed.
    """
    if b is None:
        b = a                               # same density: score = (N*C-A^2)/(A(N-A))
    da = a * (n - a)
    d = b * (n - b)
    require(d > 0, f"planted window would be flat (B={b}, N={n})")
    lo, hi = max(0, a + b - n), min(a, b)
    # C from the target, then clamped into the Frechet interval and rounded to
    # whichever neighbour actually lands closer.
    c_real = (target * float(np.sqrt(float(da) * float(d))) + a * b) / n
    best = None
    for c in {int(np.floor(c_real)), int(np.ceil(c_real))}:
        c = max(lo, min(hi, c))
        s = exact_score(n * c - a * b, d, da)
        if best is None or abs(s - target) < abs(best[2] - target):
            best = (b, c, s)
    return best


def plant_window(rng, pb: np.ndarray, tb: np.ndarray, ux: int, uy: int,
                 b: int, c: int) -> None:
    """Overwrite pb[uy:uy+th, ux:ux+tw] with a window of ink B, overlap C.

    C ink pixels are drawn from the template's ink set and B - C from its
    complement, so |W| = B and |W AND T| = C hold by construction rather than
    by measurement.
    """
    th, tw = tb.shape
    ink = np.flatnonzero(tb.ravel() == 1)
    bg = np.flatnonzero(tb.ravel() == 0)
    require(c <= ink.size and (b - c) <= bg.size,
            f"cannot plant B={b} C={c} into a template with {ink.size} ink "
            f"and {bg.size} background pixels")
    win = np.zeros(tw * th, np.int64)
    win[rng.choice(ink, size=c, replace=False)] = 1
    if b - c > 0:
        win[rng.choice(bg, size=b - c, replace=False)] = 1
    pb[uy:uy + th, ux:ux + tw] = win.reshape(th, tw)


def to_u8(pb: np.ndarray) -> np.ndarray:
    return (pb.astype(np.uint8) * INK)


def build_targeted(tag_key: str, target: float, density: float = 0.5,
                   loc=None, max_tries: int = 24) -> dict:
    """A production-shape case whose peak score is `target`, by construction.

    The background is binary noise at `density`; the planted window sits where
    `loc` says.  What IS searched is only whether the plant wins the map --
    with N in the thousands a random window's score fluctuates by O(1/sqrt(N))
    (~0.008 at N = 15,416), so a 0.20 plant clears the field comfortably and
    the search almost never iterates.  It is kept because "almost never" is not
    "never", and a silent second-place plant would put the golden location on a
    noise peak.
    """
    tw, th = GEOMETRIES[tag_key]
    pw, ph = patch_shape(tw, th)
    n = tw * th
    seed = SEEDS[tag_key]

    for k in range(max_tries):
        rng = np.random.default_rng(seed * 1000 + k)
        pb = (rng.random((ph, pw)) < density).astype(np.int64)
        tb = (rng.random((th, tw)) < density).astype(np.int64)
        a = int(tb.sum())
        if a == 0 or a == n:
            continue

        ux, uy = loc if loc else ((pw - tw) // 3, (ph - th) // 3)
        if target >= 0.999:
            pb[uy:uy + th, ux:ux + tw] = tb          # exact crop
            b, c = a, a
        else:
            b, c, _ = solve_counts(n, a, target)
            plant_window(rng, pb, tb, ux, uy, b, c)

        patch, templ = to_u8(pb), to_u8(tb)
        cm = count_maps(patch, templ)
        ex = exact_argmax(cm["q"], cm["d"], cm["da"])
        if (ex["x"], ex["y"]) != (ux, uy):
            continue
        if ex["runner_up_gap"] < MIN_MARGIN:
            continue
        return _finish(f"prod-{tag_key}", CAT_PROD, patch, templ, cm, ex,
                       target=target,
                       probes=[("planted", ux, uy)])
    raise RuntimeError(
        f"{tag_key}: the planted target-{target} window did not win its own "
        f"map in {max_tries} seeds. Do not lower MIN_MARGIN to fix this — "
        f"raise the plant's score or lower the background density, because a "
        f"lowered margin turns every location assertion in this suite into a "
        f"coin flip.")


def _finish(tag: str, category: str, patch: np.ndarray, templ: np.ndarray,
            cm: dict, ex: dict, target=None, probes=(), note: str = "") -> dict:
    """Assemble one case record, cross-checking both oracles and the widths."""
    xc = check_oracles(patch, templ, cm)
    require((xc["exact"]["x"], xc["exact"]["y"]) == (ex["x"], ex["y"]),
            f"{tag}: exact_argmax is not reproducible")
    w = check_widths(cm, tag)

    q0 = int(cm["q"][ex["y"], ex["x"]])
    d0 = int(cm["d"][ex["y"], ex["x"]])
    score = exact_score(q0, d0, cm["da"])

    # The one disagreement worth naming loudly.  0.2 reduces in float and 0.3
    # in exact integers, so they can only be required to agree on a location
    # when the float map's own argmax agrees with the exact one.  Where it does
    # not, that is a real property of the case and belongs in the manifest, not
    # in a retry loop.
    float_agrees = (xc["float_x"], xc["float_y"]) == (ex["x"], ex["y"])
    dut = dut_float32_argmax(patch, templ)
    dut32_agrees = (dut["x"], dut["y"]) == (ex["x"], ex["y"])
    require(abs(dut["score"] - score) <= SCORE_TOL,
            f"{tag}: the float32 DUT model scores {dut['score']:+.6f} where "
            f"the exact counts give {score:+.6f} — a gap of "
            f"{abs(dut['score'] - score):.2e}, over SCORE_TOL={SCORE_TOL}. "
            f"0.2 cannot be held to this case's score.")

    ph, pw = patch.shape
    th, tw = templ.shape
    require(pw * ph <= DMA_MAX_BYTES,
            f"{tag}: patch {pw}x{ph} = {pw * ph} B exceeds the 3.1 "
            f"single-transfer bound of {DMA_MAX_BYTES} B")
    require(tw <= MAX_TEMPL_W and th <= MAX_TEMPL_H
            and pw <= MAX_PATCH_W and ph <= MAX_PATCH_H,
            f"{tag}: {pw}x{ph} / {tw}x{th} is outside the 4.1 envelope")

    qmin_idx = int(np.argmin(np.where(cm["d"] > 0, cm["q"],
                                      np.iinfo(np.int64).max)))
    my, mx = divmod(qmin_idx, cm["rw"])

    probe_rows = []
    for label, px, py in probes:
        probe_rows.append((label, px, py, int(cm["b"][py, px]),
                           int(cm["c"][py, px]), int(cm["q"][py, px]),
                           int(cm["d"][py, px])))

    return dict(
        tag=tag, category=category, patch=patch, templ=templ,
        score=score, x=ex["x"], y=ex["y"],
        margin=min(ex["runner_up_gap"], 999.0),
        target=target, note=note,
        n=cm["n"], a=cm["a"], da=cm["da"], rw=cm["rw"], rh=cm["rh"],
        b=int(cm["b"][ex["y"], ex["x"]]), c=int(cm["c"][ex["y"], ex["x"]]),
        q=q0, d=d0,
        n_exact_ties=ex["n_exact_ties"], float_agrees=float_agrees,
        dut32_agrees=dut32_agrees, dut32_xy=(dut["x"], dut["y"]),
        dut32_score=dut["score"],
        float_xy=(xc["float_x"], xc["float_y"]), map_err=xc["map_err"],
        n_flat=int((cm["d"] == 0).sum()),
        qmin_at=(mx, my), qmin=int(cm["q"][my, mx]),
        qmin_b=int(cm["b"][my, mx]), qmin_c=int(cm["c"][my, mx]),
        qmin_d=int(cm["d"][my, mx]),
        widths=w, probes=probe_rows,
        verified_block=cm["verified_block"],
    )


def build_negative_1x1() -> dict:
    """A reported best score that is NEGATIVE, at the maximum template size.

    Why a 1x1 result map is the only way to get one: `q < 0` windows are
    everywhere in every case here, but the argmax reports the BEST window, and
    on any map with room to move some window is positive.  Making patch ==
    template leaves exactly one search position, so the anti-correlated score
    IS the answer -- which is what exercises Phase 5's `positive > zero >
    negative` class ordering and, on 0.2, the sign bit of `result_score` as it
    crosses AXI4-Lite.

    216x96 rather than a small shape: this is also the only case in the suite
    that runs the maximum template through the core, and it costs one window.
    """
    tw, th = MAX_TEMPL_W, MAX_TEMPL_H
    n = tw * th
    rng = np.random.default_rng(SEEDS["negative-1x1"])
    tb = (rng.random((th, tw)) < 0.5).astype(np.int64)
    a = int(tb.sum())
    # Anti-correlate, then let a quarter of the pixels back to noise so the
    # score is not the maximally round -1.0 (0xBF800000).
    pb = 1 - tb
    keep = rng.random((th, tw)) < 0.25
    pb = np.where(keep, (rng.random((th, tw)) < 0.5).astype(np.int64), pb)

    patch, templ = to_u8(pb), to_u8(tb)
    cm = count_maps(patch, templ)
    require(cm["rw"] == 1 and cm["rh"] == 1,
            f"negative-1x1 result map is {cm['rw']}x{cm['rh']}, not 1x1")
    ex = exact_argmax(cm["q"], cm["d"], cm["da"])
    q0 = int(cm["q"][0, 0])
    require(q0 < 0, f"negative-1x1 has q = {q0} >= 0 — not a sign test")
    s = exact_score(q0, int(cm["d"][0, 0]), cm["da"])
    require(s < -0.2, f"negative-1x1 scored {s:+.4f} — too shallow to be a "
                      f"sign-bit test")
    require(a not in (0, n))
    return _finish("prod-negative-1x1", CAT_SIGN, patch, templ, cm, ex,
                   probes=[("only-window", 0, 0)],
                   note="1x1 result map; best score is negative")


def build_tie_exact() -> dict:
    """Two windows with IDENTICAL exact counts — a real tie, settled by order.

    Both are exact crops of the template, so q, B, C and d match bit for bit
    and no float coincidence is involved.  The first in row-major order must
    win under both the 0.2 float reduction (strict >) and the 0.3 exact
    comparison, and `n_exact_ties == 2` in the sidecar is what says the tie was
    actually present rather than accidentally broken by the noise around it.
    """
    tw, th = GEOMETRIES["base-399"]
    pw, ph = patch_shape(tw, th)
    rng = np.random.default_rng(SEEDS["tie-exact"])
    pb = (rng.random((ph, pw)) < 0.5).astype(np.int64)
    tb = (rng.random((th, tw)) < 0.5).astype(np.int64)

    first, second = (20, 15), (200, 100)
    for (ux, uy) in (first, second):
        pb[uy:uy + th, ux:ux + tw] = tb

    patch, templ = to_u8(pb), to_u8(tb)
    cm = count_maps(patch, templ)
    ex = exact_argmax(cm["q"], cm["d"], cm["da"])
    require(ex["n_exact_ties"] == 2,
            f"tie-exact found {ex['n_exact_ties']} tied windows, expected 2 — "
            f"the two plants overlap or the noise produced a third")
    require((ex["x"], ex["y"]) == first,
            f"tie-exact went to {(ex['x'], ex['y'])}, not the row-major first "
            f"{first} — the first-occurrence rule is not being applied")
    return _finish("prod-tie-exact", CAT_TIE, patch, templ, cm, ex,
                   probes=[("tie-first", *first), ("tie-second", *second)],
                   note="two bit-identical peaks; row-major first must win")


def _closest_pair(n: int, a: int, around: float, span: int = 3):
    """(B1,C1),(B2,C2) with the smallest NONZERO exact score gap near `around`.

    Enumerated over the integers rather than searched over pixels: the pair
    that is hardest to separate is a property of the count lattice, and finding
    it by perturbing noise would find whatever the noise happened to offer.
    """
    da = a * (n - a)
    b0, c0, _ = solve_counts(n, a, around)
    cands = []
    for b in range(max(1, b0 - span), min(n - 1, b0 + span) + 1):
        d = b * (n - b)
        lo, hi = max(0, a + b - n), min(a, b)
        c_mid = int(round((around * float(np.sqrt(float(da) * float(d)))
                           + a * b) / n))
        for c in range(max(lo, c_mid - span), min(hi, c_mid + span) + 1):
            cands.append((b, c, n * c - a * b, d))
    best = None
    for i in range(len(cands)):
        for j in range(len(cands)):
            if i == j:
                continue
            b1, c1, q1, d1 = cands[i]
            b2, c2, q2, d2 = cands[j]
            if cmp_exact(q1, d1, q2, d2) <= 0:
                continue                    # want strictly first > second
            gap = exact_score(q1, d1, da) - exact_score(q2, d2, da)
            if best is None or gap < best[0]:
                best = (gap, (b1, c1), (b2, c2))
    require(best is not None, "no separable pair in the enumerated lattice")
    return best


def build_tie_near(tag: str, seed_key: str, around: float,
                   winner_first: bool) -> dict:
    """Two windows separated by the SMALLEST gap the count lattice allows.

    Two of these are built, with the winner planted first and second, because
    they test different failures.  Winner-first passes a reduction that has
    silently degraded to "keep the first plausible peak"; winner-second is the
    one that catches it.

    The gap is reported, not asserted against MIN_MARGIN -- being under it is
    the entire point.  `float_agrees` in the sidecar records whether the float
    map's own argmax still lands on the exact winner, which is the only honest
    basis for deciding whether 0.2 can be held to this case's location.
    """
    tw, th = GEOMETRIES["base-399"]
    pw, ph = patch_shape(tw, th)
    n = tw * th
    rng = np.random.default_rng(SEEDS[seed_key])
    tb = (rng.random((th, tw)) < 0.5).astype(np.int64)
    a = int(tb.sum())
    gap, hi_bc, lo_bc = _closest_pair(n, a, around)

    for k in range(24):
        rng = np.random.default_rng(SEEDS[seed_key] * 1000 + k)
        pb = (rng.random((ph, pw)) < 0.5).astype(np.int64)
        first, second = (20, 15), (200, 100)
        hi_at, lo_at = (first, second) if winner_first else (second, first)
        plant_window(rng, pb, tb, hi_at[0], hi_at[1], *hi_bc)
        plant_window(rng, pb, tb, lo_at[0], lo_at[1], *lo_bc)

        patch, templ = to_u8(pb), to_u8(tb)
        cm = count_maps(patch, templ)
        ex = exact_argmax(cm["q"], cm["d"], cm["da"])
        if (ex["x"], ex["y"]) != hi_at:
            continue
        if ex["n_exact_ties"] != 1:
            continue
        return _finish(tag, CAT_TIE, patch, templ, cm, ex,
                       probes=[("near-hi", *hi_at), ("near-lo", *lo_at)],
                       note=f"exact gap {gap:.3e} "
                            f"({'under' if gap < SCORE_TOL else 'over'} "
                            f"SCORE_TOL={SCORE_TOL}); winner planted "
                            f"{'first' if winner_first else 'second'}")
    raise RuntimeError(f"{tag}: the near-tie plants never won their own map")


def build_flat_region() -> dict:
    """B = 0 and B = N windows in a production-shape map: the +0.0 path.

    Contract 4.4 makes a flat window score exactly +0.0, and 0.3 reaches the
    same answer through `d = B*(N-B) == 0`.  Both extremes are present here --
    an all-background block gives B = 0, an all-ink block gives B = N -- so the
    two ways of reaching d = 0 are covered, not just the easy one.  The peak is
    planted in the noisy third so the flat windows are numerous and harmless
    rather than decorative.
    """
    tw, th = GEOMETRIES["base-421"]
    pw, ph = patch_shape(tw, th)
    n = tw * th
    rng = np.random.default_rng(SEEDS["flat-region"])
    pb = (rng.random((ph, pw)) < 0.5).astype(np.int64)
    tb = (rng.random((th, tw)) < 0.5).astype(np.int64)
    a = int(tb.sum())

    pb[0:th + 20, 0:tw + 20] = 0                      # B = 0 windows
    pb[ph - th - 20:ph, pw - tw - 20:pw] = 1          # B = N windows

    ux, uy = (pw - tw) // 2, (ph - th) // 2
    b, c, _ = solve_counts(n, a, 0.45)
    plant_window(rng, pb, tb, ux, uy, b, c)

    patch, templ = to_u8(pb), to_u8(tb)
    cm = count_maps(patch, templ)
    require(int((cm["b"] == 0).sum()) > 0, "no B == 0 window survived")
    require(int((cm["b"] == n).sum()) > 0, "no B == N window survived")
    ex = exact_argmax(cm["q"], cm["d"], cm["da"])
    require((ex["x"], ex["y"]) == (ux, uy),
            f"flat-region peak moved to {(ex['x'], ex['y'])}")
    b0 = np.argwhere(cm["b"] == 0)[0]
    bn = np.argwhere(cm["b"] == n)[0]
    return _finish("prod-flat-region", CAT_BOUNDS, patch, templ, cm, ex,
                   probes=[("planted", ux, uy),
                           ("flat-B0", int(b0[1]), int(b0[0])),
                           ("flat-BN", int(bn[1]), int(bn[0]))],
                   note="carries B=0 and B=N flat windows (score +0.0)")


def build_max_result() -> dict:
    """MAX_RESULT_W exactly, and the tightest possible final partial tile.

    820x307 under a 4x16 template gives rw = 817 = MAX_RESULT_W, so `acc[]` is
    written at its top index and `norm_cols` runs to u = 816.  817 mod 16 = 1,
    so the last tile has ONE valid lane out of sixteen -- the narrowest partial
    tile the design can produce, and the case a writeback guard that used `u <=
    rw` or dropped the guard entirely would fail on.

    The template is 4 wide (the minimum, which is what makes rw maximal) but 16
    tall, because a 4x4 template carries 16 bits against 248,368 windows and
    its exact matches would number in the handfuls -- no unique peak, no
    location assertion.  64 bits makes the planted crop unique, and the
    uniqueness is verified rather than assumed.
    """
    pw, ph = MAX_PATCH_W, MAX_PATCH_H
    tw, th = 4, 16
    rng = np.random.default_rng(SEEDS["max-result"])
    pb = (rng.random((ph, pw)) < 0.5).astype(np.int64)
    tb = (rng.random((th, tw)) < 0.5).astype(np.int64)

    ux, uy = pw - tw, ph - th        # the final column AND the final row
    pb[uy:uy + th, ux:ux + tw] = tb

    patch, templ = to_u8(pb), to_u8(tb)
    cm = count_maps(patch, templ)
    require(cm["rw"] == MAX_RESULT_W,
            f"max-result rw = {cm['rw']}, not MAX_RESULT_W = {MAX_RESULT_W}")
    require(cm["rh"] == ph - th + 1)
    require(cm["rw"] % PAR_COLS == 1,
            f"rw {cm['rw']} mod {PAR_COLS} = {cm['rw'] % PAR_COLS}; this case "
            f"exists for the 1-lane final tile")
    ex = exact_argmax(cm["q"], cm["d"], cm["da"])
    require((ex["x"], ex["y"]) == (ux, uy),
            f"max-result peak at {(ex['x'], ex['y'])}, not the final corner "
            f"{(ux, uy)} — the planted crop is not unique")
    require(ex["n_exact_ties"] == 1,
            f"max-result has {ex['n_exact_ties']} tied peaks; the location "
            f"assertion needs exactly one")
    return _finish("prod-max-result", CAT_BOUNDS, patch, templ, cm, ex,
                   probes=[("final-corner", ux, uy)],
                   note=f"rw={cm['rw']} (MAX_RESULT_W), final tile has "
                        f"{cm['rw'] % PAR_COLS} valid lane")


# ---------------------------------------------------------------------------
# The lane-15 mutation case
# ---------------------------------------------------------------------------

def lane15_mutant_setup(patch: np.ndarray, templ: np.ndarray) -> dict:
    """Everything in the mutant score map that does NOT depend on S.

    Split out because the sweep below evaluates 256 stale values and the
    transform, the window sums and the normalisation denominator are identical
    for all of them; only the numerator's correction term moves.  At the
    622x300 / 164x94 production shape that is the difference between one FFT
    and 256 of them.
    """
    p = patch.astype(np.int64)
    t = templ.astype(np.int64)
    ph, pw = p.shape
    th, tw = t.shape
    rh, rw = ph - th + 1, pw - tw + 1

    si = _window_sums_i64(p, th, tw)
    sii = _window_sums_i64(p * p, th, tw)
    st = int(t.sum())
    stt = int((t * t).sum())
    n = tw * th
    dt = n * stt - st * st

    s0, s1 = ph + th, pw + tw
    f = np.fft.rfft2(p, s=(s0, s1)) * np.conj(np.fft.rfft2(t, s=(s0, s1)))
    sti = np.rint(np.fft.irfft2(f, s=(s0, s1))[:rh, :rw]).astype(np.int64)

    di = n * sii - si * si
    denom = np.sqrt(np.float64(dt) * di.astype(np.float64))

    # The affected output columns and the two halves of the correction term.
    # STI_mut = STI + S*sum(T[:,tw-1]) - sum_y T[y,tw-1]*P[v+y, u+tw-1]
    cols = np.arange(PAR_COLS - 1, rw, PAR_COLS)
    tlast = t[:, tw - 1]
    if cols.size:
        win = np.lib.stride_tricks.sliding_window_view(
            p[:, cols + tw - 1], th, axis=0)[:rh]
        patch_term = np.einsum("vjy,y->vj", win, tlast)
    else:
        patch_term = np.zeros((rh, 0), np.int64)

    return dict(sti=sti, si=si, di=di, denom=denom, n=n, st=st, rw=rw,
                cols=cols, tlast_sum=int(tlast.sum()), patch_term=patch_term)


def lane15_mutant_map(setup: dict, s: int) -> np.ndarray:
    """The score map for one stale-register value S."""
    sti = setup["sti"].copy()
    if setup["cols"].size:
        sti[:, setup["cols"]] += (np.int64(s) * setup["tlast_sum"]
                                  - setup["patch_term"])
    num = setup["n"] * sti - setup["st"] * setup["si"]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(setup["di"] == 0, 0.0,
                        num / np.where(setup["denom"] == 0, 1.0,
                                       setup["denom"]))


def lane15_mutant_maps(patch: np.ndarray, templ: np.ndarray, s: int):
    """The score map `correlation_core` would produce with seg_len = tw + 14.

    Derived, not simulated.  With the shortened load the element at index
    tw + 14 is never written by any tile, so it holds one unknown constant S
    for the whole run, and it is read by exactly one (lane, column) pair --
    p = 15, x = tw - 1.  So the mutant differs from the truth only in output
    columns u = 15 (mod 16), and only by the template's last column:

        STI_mut(u,v) = STI(u,v) + sum_y T[y,tw-1] * (S - P[v+y,u+tw-1])

    SI and SII are untouched: those come from tme_top's own incremental window
    loops, not from correlation_core, so the mutation moves the numerator
    alone.

    Kept as a one-shot wrapper over the split form so the derivation reads in
    one place; the sweep uses `lane15_mutant_setup` / `lane15_mutant_map`.
    """
    return lane15_mutant_map(lane15_mutant_setup(patch, templ), s)


def _sim_correlation(patch: np.ndarray, templ: np.ndarray, seg_delta: int,
                     stale: int) -> np.ndarray:
    """Literal transcription of correlation_core, with a settable seg_len.

    Deliberately slow and deliberately stupid: nested loops in the same order
    as the C++, one segment register file, the same `idx < pw` guard, the same
    `u < rw` writeback guard.  It exists only to check `lane15_mutant_maps`,
    which is a closed-form DERIVATION -- and a derivation that is wrong would
    otherwise be indistinguishable from a mutation that is undetectable.

    `seg_delta = 0` is Phase 3's proposed seg_len = PAR_COLS + tw - 1;
    `seg_delta = -1` is the off-by-one.  Elements the load never reaches keep
    `stale`, which is exactly what the register file would hold.
    """
    p = patch.astype(np.int64)
    t = templ.astype(np.int64)
    ph, pw = p.shape
    th, tw = t.shape
    rh, rw = ph - th + 1, pw - tw + 1
    seg_w = PAR_COLS + tw - 1
    seg_len = seg_w + seg_delta

    sti = np.zeros((rh, rw), np.int64)
    for v in range(rh):
        for y in range(th):
            line = p[v + y]
            trow = t[y]
            seg = np.full(seg_w, stale, np.int64)
            for u0 in range(0, rw, PAR_COLS):
                for i in range(seg_len):
                    idx = u0 + i
                    seg[i] = line[idx] if idx < pw else 0
                for lane in range(PAR_COLS):
                    u = u0 + lane
                    if u < rw:
                        sti[v, u] += int((seg[lane:lane + tw] * trow).sum())
    return sti


def selftest_lane15_model() -> None:
    """The derivation must reproduce the transcription, both ways.

    Two claims are checked on a case small enough to brute-force:

      1. seg_len = PAR_COLS + tw - 1 is SUFFICIENT.  The tiled simulation with
         that length reproduces the FFT cross-correlation exactly, for a stale
         fill of 0 and of 255 -- i.e. no lane ever reads an unwritten element,
         which is the correctness half of Phase 3's change.
      2. seg_len - 1 is EXACTLY the closed form `lane15_mutant_maps` uses.
         The simulation with the short length and stale value S must match the
         derived numerator for every S tried.

    Without (1) the suite could be pinning goldens for a change that is simply
    wrong; without (2) the 256-value sweep would be sweeping the wrong model.
    """
    rng = np.random.default_rng(0x1A5E)
    pw, ph, tw, th = 70, 26, 11, 5
    pb = (rng.random((ph, pw)) < 0.5).astype(np.int64)
    tb = (rng.random((th, tw)) < 0.5).astype(np.int64)
    tb[:, tw - 1] = np.array([1, 0, 1, 1, 0])          # mixed last column
    patch, templ = to_u8(pb), to_u8(tb)

    p = patch.astype(np.int64)
    t = templ.astype(np.int64)
    rh, rw = ph - th + 1, pw - tw + 1
    s0, s1 = ph + th, pw + tw
    f = np.fft.rfft2(p, s=(s0, s1)) * np.conj(np.fft.rfft2(t, s=(s0, s1)))
    truth = np.rint(np.fft.irfft2(f, s=(s0, s1))[:rh, :rw]).astype(np.int64)

    for stale in (0, 255):
        got = _sim_correlation(patch, templ, 0, stale)
        require(np.array_equal(got, truth),
                f"seg_len = PAR_COLS + tw - 1 does NOT reproduce the true "
                f"correlation with stale={stale}: {int((got != truth).sum())} "
                f"of {truth.size} outputs differ. Phase 3's proposed length is "
                f"too short, and every golden below would be pinning a bug.")

    cols = np.arange(PAR_COLS - 1, rw, PAR_COLS)
    tlast = t[:, tw - 1]
    win = np.lib.stride_tricks.sliding_window_view(
        p[:, cols + tw - 1], th, axis=0)
    for stale in (0, 97, 255):
        got = _sim_correlation(patch, templ, -1, stale)
        derived = truth.copy()
        derived[:, cols] += (np.int64(stale) * int(tlast.sum())
                             - np.einsum("vjy,y->vj", win[:rh], tlast))
        require(np.array_equal(got, derived),
                f"the seg_len-1 closed form disagrees with the transcription "
                f"at stale={stale} — lane15_mutant_maps is modelling the "
                f"wrong mutation")
        require(not np.array_equal(got, truth),
                f"seg_len-1 with stale={stale} produced the CORRECT map on "
                f"this fixture; the self-test fixture is not sensitive")


def lane15_survives_mutation(patch, templ, gx, gy, gscore) -> dict:
    """Require the seg_len - 1 mutation to be detectable for EVERY stale S.

    Returns the worst case over S: the smallest location displacement and the
    smallest score change any stale register value produces.  A case is only
    usable if the argmax moves for all 256 -- a mutation that merely perturbs
    the score would slip past a tolerance-based assert, and 0.2's score assert
    is tolerance-based by contract (4.6).
    """
    ph, pw = patch.shape
    th, tw = templ.shape
    rw = pw - tw + 1
    require(gx % PAR_COLS == PAR_COLS - 1,
            f"peak column {gx} is not a lane-{PAR_COLS - 1} column "
            f"({gx % PAR_COLS}); this case cannot detect the mutation at all")

    setup = lane15_mutant_setup(patch, templ)
    min_dscore = float("inf")
    max_dscore = 0.0
    unmoved = []
    winners = set()
    for s in range(256):
        smap = lane15_mutant_map(setup, s)
        my, mx = divmod(int(np.argmax(smap)), rw)
        if (mx, my) == (gx, gy):
            unmoved.append(s)
        winners.add((mx, my))
        dsc = abs(float(smap[gy, gx]) - gscore)
        min_dscore = min(min_dscore, dsc)
        max_dscore = max(max_dscore, dsc)
    require(not unmoved,
            f"stale-register values {unmoved[:8]} (of {len(unmoved)}) leave "
            f"the argmax at ({gx},{gy}) — the seg_len-1 mutation would pass "
            f"this case. An exact-match plant always leaves S=255 blind; the "
            f"pair construction in build_lane15 is what removes the blind "
            f"spot, so this means the pair did not take.")
    return dict(all_moved=True, min_peak_dscore=min_dscore,
                max_peak_dscore=max_dscore,
                n_distinct_mutant_winners=len(winners))


# Template ink fraction for the lane-15 cases.  Not cosmetic -- see
# build_lane15's derivation: the achievable corruption differential is
# 1/(alpha*(1-alpha)*tw) score units, so alpha near 0.5 (the natural choice)
# makes it SMALLEST.  0.15 buys the headroom that lets the planted gap sit
# above MIN_MARGIN and still be flipped.
LANE15_ALPHA = 0.15


def build_lane15(tag: str, seed_key: str, tw: int, th: int, pw: int, ph: int,
                 max_tries: int = 40) -> dict:
    """Two planted lane-15 windows the seg_len-1 mutation must reorder.

    THE OBVIOUS CONSTRUCTION DOES NOT WORK, and the reason is worth writing
    down because it is the whole design.  Plant an exact crop on a lane-15
    column and the mutation is INVISIBLE at stale value S = 255: the corrupted
    read contributes `255 * T[y,tw-1]` where the truth contributes
    `P[v+y,u+tw-1] * T[y,tw-1]`, and at an exact match those are the same
    number.  A suite built that way reports "mutation detected" for 255 of the
    256 possible register states and silently passes the one a reset actually
    produces near.

    Worse, no single window can be made safe.  The corruption at a window is

        dSTI(u,v) = 255*k*S - 255^2 * m(u,v)

    with k = ink pixels in the template's last column and m = how many of those
    the window's own last column matches.  S is free over [0,255], so there is
    always a value making |k*S - 255*m| small, and a score-tolerance assert
    (0.2 verifies scores to SCORE_TOL by contract 4.6) sees nothing.

    What IS S-independent is the DIFFERENCE between two windows: k is a
    property of the template, so the k*S term is identical for every window and
    cancels.  Two lane-15 windows with the same ink count B (hence the same
    denominator) therefore separate by

        dC = (C1 - C2) - (m1 - m2)                 exactly, for every S

    So the case plants a pair: W1 wins truthfully by `gap` counts, and its last
    column matches the template's (m1 = k = th) while the runner-up's does not
    (m2 = 0).  Under the mutation W1 loses exactly th counts relative to W2, so
    choosing gap < th makes W2 overtake for ALL 256 stale values -- the argmax
    moves, which is the assertion that survives a score tolerance.

    The margin arithmetic then fixes alpha.  With B = A = alpha*N the score
    moves 1/(alpha*(1-alpha)*tw) per count, so the largest flippable gap is
    th * that; it must exceed MIN_MARGIN or no gap satisfies both conditions.
    """
    n = tw * th
    rw, rh = pw - tw + 1, ph - th + 1
    a = int(round(LANE15_ALPHA * n))
    da = a * (n - a)
    per_count = n / float(da)               # score units per unit of C
    gap_counts = th // 2
    require(per_count * th > MIN_MARGIN,
            f"{tag}: the largest flippable gap is {per_count * th:.4f} score "
            f"units, under MIN_MARGIN={MIN_MARGIN} — at tw={tw} this geometry "
            f"cannot carry a lane-15 pair. Lower LANE15_ALPHA.")
    require(per_count * gap_counts >= MIN_MARGIN,
            f"{tag}: planted gap {per_count * gap_counts:.4f} is under "
            f"MIN_MARGIN={MIN_MARGIN}")

    # Two lane-15 columns, both past the first tile so the case is about a
    # steady-state tile rather than the first one.  The extremes of the usable
    # range, so the two plants cannot overlap at any production width.
    lanes = [u + PAR_COLS - 1
             for u in range(PAR_COLS, rw - PAR_COLS, PAR_COLS)
             if u + PAR_COLS - 1 + tw <= pw]
    require(len(lanes) >= 2,
            f"{tag}: only {len(lanes)} usable lane-{PAR_COLS - 1} columns")
    u1, u2 = lanes[0], lanes[-1]
    v1, v2 = rh // 4, 3 * rh // 4
    require(abs(u1 - u2) >= tw or abs(v1 - v2) >= th,
            f"{tag}: the two plants overlap ({u1},{v1}) vs ({u2},{v2}) at "
            f"{tw}x{th}")

    for attempt in range(max_tries):
        rng = np.random.default_rng(SEEDS[seed_key] * 1000 + attempt)
        pb = (rng.random((ph, pw)) < 0.5).astype(np.int64)

        # Template: last column ALL ink, so k = th is maximal and m is simply
        # "how many rows of the window's last column are ink".
        tb = np.zeros((th, tw), np.int64)
        rest = rng.permutation((tw - 1) * th)[:a - th]
        flat = np.zeros((tw - 1) * th, np.int64)
        flat[rest] = 1
        tb[:, :tw - 1] = flat.reshape(th, tw - 1)
        tb[:, tw - 1] = 1
        require(int(tb.sum()) == a, "template ink count drifted")

        ink_rest = np.flatnonzero(tb[:, :tw - 1].ravel() == 1)
        bg_rest = np.flatnonzero(tb[:, :tw - 1].ravel() == 0)

        # C1 chosen for a mid-range score; C2 sits `gap_counts` below it.
        c1 = int(round((0.55 * da + a * a) / n))
        c2 = c1 - gap_counts
        # C <= min(A, B) and B == A here, so `a` is the Frechet ceiling; the
        # last column contributes exactly th to C1, hence the th floor.
        ok = (th <= c1 <= a and c2 >= 0
              and (c1 - th) <= ink_rest.size and c2 <= ink_rest.size
              and (a - th) - (c1 - th) <= bg_rest.size
              and a - c2 <= bg_rest.size)
        if not ok:
            continue

        def place(ux, uy, c_rest, last_col_ink):
            win = np.zeros((th, tw), np.int64)
            win[:, tw - 1] = 1 if last_col_ink else 0
            body = np.zeros((tw - 1) * th, np.int64)
            body[rng.choice(ink_rest, size=c_rest, replace=False)] = 1
            n_bg = a - c_rest - (th if last_col_ink else 0)
            if n_bg > 0:
                body[rng.choice(bg_rest, size=n_bg, replace=False)] = 1
            win[:, :tw - 1] = body.reshape(th, tw - 1)
            pb[uy:uy + th, ux:ux + tw] = win

        place(u1, v1, c1 - th, True)        # m1 = th, C1 = (c1-th) + th
        place(u2, v2, c2, False)            # m2 = 0,  C2 = c2

        patch, templ = to_u8(pb), to_u8(tb)
        cm = count_maps(patch, templ)
        if int(cm["c"][v1, u1]) != c1 or int(cm["c"][v2, u2]) != c2:
            continue
        if int(cm["b"][v1, u1]) != a or int(cm["b"][v2, u2]) != a:
            continue
        ex = exact_argmax(cm["q"], cm["d"], cm["da"])
        if (ex["x"], ex["y"]) != (u1, v1):
            continue
        if not (MIN_MARGIN <= ex["runner_up_gap"] < per_count * th):
            continue
        score = exact_score(int(cm["q"][v1, u1]), int(cm["d"][v1, u1]),
                            cm["da"])
        try:
            mut = lane15_survives_mutation(patch, templ, u1, v1, score)
        except AssertionError:
            continue
        case = _finish(tag, CAT_LANE, patch, templ, cm, ex,
                       probes=[("lane15-winner", u1, v1),
                               ("lane15-runner-up", u2, v2)],
                       note=f"winner on lane {u1 % PAR_COLS} of tile "
                            f"{u1 // PAR_COLS}, gap {gap_counts} counts; "
                            f"seg_len-1 moves the argmax for all 256 stale "
                            f"values")
        case["lane15"] = mut
        return case
    raise RuntimeError(
        f"{tag}: no seed produced a lane-15 pair that all 256 stale-register "
        f"values reorder. This is the one case in the suite that may not be "
        f"weakened — without it the seg_len-1 off-by-one is untested.")


# ---------------------------------------------------------------------------
# Priority 4 (B1): the banking-boundary width suite
# ---------------------------------------------------------------------------
# B1 replaces correlation_core's compile-time segment load
#
#     SEG_W = PAR_COLS + MAX_TEMPL_W          232 pixels, every tile, always
#
# with the runtime-required
#
#     seg_len = PAR_COLS + tw - 1             19 pixels at tw=4, 231 at tw=216
#
# Two failure modes follow from that change, and they need DIFFERENT cases:
#
#   1. THE LENGTH IS WRONG BY ONE (seg_len - 1).  Only seg[tw + 14] goes
#      unwritten, and only lane 15 reading the template's last column ever
#      reads it, so a suite whose peaks avoid lane 15 passes the mutant.  Worse,
#      an exact-crop plant ON lane 15 is still blind at stale value S = 255.
#      build_lane15 is the construction that closes this, and it is reused
#      here unchanged at a cosim-affordable geometry -- see its docstring for
#      why a PAIR of windows is what makes the detection S-independent.
#
#   2. THE TILE COUNT OR THE LANE MASK IS WRONG.  tile_loop breaks on
#      u0 >= rw and writeback guards on u < rw; how many lanes of the FINAL
#      tile are masked is decided by rw mod PAR_COLS.  That is the axis
#      B1_WIDTHS sweeps: 15 masked lanes at rw = 17 and 33, one at 15/31/95,
#      none at 16/32/96, and the degenerate single-position map at rw = 1.
#
# Every width case plants its peak at the LAST valid column, u = rw - 1, which
# is where a tile-count or mask error shows up as a moved argmax rather than a
# score that merely drifts.
B1_WIDTHS = [
    # rw    tw   th  rh  peak_u  peak_v
    (   1, 216,   8,  6,      0,      3),   # 1-wide map, 15 lanes masked
    (  15, 216,   8,  6,     14,      2),   # one lane masked -- and it is 15
    (  16, 216,   8,  6,     15,      4),   # exactly one full tile
    (  17, 216,   8,  6,     16,      1),   # second tile carries ONE column
    (  31, 100,  12, 10,     30,      5),
    (  32, 100,  12, 10,     31,      3),
    (  33, 100,  12, 10,     32,      8),
    (  95,  20,  12, 16,     94,      7),
    (  96,  20,  12, 16,     95,     12),
]

# tw is varied deliberately across that table, because tw is what B1 actually
# shortens.  216 is MAX_TEMPL_W, where seg_len = 231 and B1 saves ONE cycle per
# tile -- the boundary where the new bound must still be long enough; 20 is
# where it saves 56%.  A suite at a single tw would test the saving at one
# point and the sufficiency at none.
B1_SEEDS = {
    "b1-w001": 0x9E37_0100, "b1-w015": 0x9E37_0101, "b1-w016": 0x9E37_0102,
    "b1-w017": 0x9E37_0103, "b1-w031": 0x9E37_0104, "b1-w032": 0x9E37_0105,
    "b1-w033": 0x9E37_0106, "b1-w095": 0x9E37_0107, "b1-w096": 0x9E37_0108,
    "b1-tie-samerow": 0x9E37_0110, "b1-tie-rowmajor": 0x9E37_0111,
    "b1-lane15": 0x9E37_0120,
}


def _b1_tie_pair(rng, pw, ph, tw, th, first, second, density=0.35):
    """Two BYTE-IDENTICAL windows, so the tie is exact in every arithmetic.

    A near-tie would only test the float path's rounding.  Copying the same
    pixels to both positions makes the three window sums identical INTEGERS,
    hence identical float32 values, hence a tie the DUT's strict > must break
    by first row-major occurrence -- the cv2.minMaxLoc rule.  Both windows are
    exact crops of the template, so both score exactly 1.0 and no third window
    can outrank them.
    """
    ax, ay = first
    bx, by = second
    require(abs(ax - bx) >= tw or abs(ay - by) >= th,
            f"tie plants overlap: ({ax},{ay}) and ({bx},{by}) at {tw}x{th}")
    patch = TME.bin_noise(rng, ph, pw, density)
    win = patch[ay:ay + th, ax:ax + tw].copy()
    patch[by:by + th, bx:bx + tw] = win
    return patch, win.copy(), first


def build_b1_widths() -> list:
    """One planted case per banking-boundary result width."""
    cases = []
    for rw, tw, th, rh, ux, uy in B1_WIDTHS:
        pw, ph = tw + rw - 1, th + rh - 1
        key = f"b1-w{rw:03d}"
        tag = f"{key}-tw{tw:03d}"
        require(ux == rw - 1,
                f"{tag}: peak at u={ux} is not the last column of a {rw}-wide "
                f"map -- the tile break and the writeback mask are what this "
                f"case exists to test")
        require(0 <= uy < rh, f"{tag}: peak row {uy} outside a {rh}-tall map")
        cases.append(TME.solve(
            tag, CAT_B1,
            lambda r, pw=pw, ph=ph, tw=tw, th=th, ux=ux, uy=uy:
                TME.planted(r, pw, ph, tw, th, ux, uy),
            B1_SEEDS[key]))
    return cases


def build_b1_ties() -> list:
    """Row-major tie-break, in the two orders that can disagree."""
    tw, th, rw, rh = 16, 12, 32, 10
    pw, ph = tw + rw - 1, th + rh - 1
    out = []

    # Same output row, different TILES: the winner is in tile 0 and the decoy
    # in tile 1, so norm_cols must keep the column it saw first.
    out.append(TME.solve(
        "b1-tie-samerow", CAT_B1,
        lambda r: _b1_tie_pair(r, pw, ph, tw, th, (5, 4), (21, 4)),
        B1_SEEDS["b1-tie-samerow"], min_margin=0.0))

    # The winner is the LARGER column in the EARLIER row.  A comparator that
    # ordered by column, or a lane-major reduction, would return (3,7) instead
    # -- which a same-row tie cannot distinguish.
    out.append(TME.solve(
        "b1-tie-rowmajor", CAT_B1,
        lambda r: _b1_tie_pair(r, pw, ph, tw, th, (31, 2), (3, 7)),
        B1_SEEDS["b1-tie-rowmajor"], min_margin=0.0))

    for c in out:
        require(c["margin"] == 0.0,
                f"{c['tag']}: margin {c['margin']!r} is not an EXACT tie, so "
                f"the first-occurrence rule is not what this case tests")
        require(c["score"] == 1.0,
                f"{c['tag']}: exact crops must score exactly 1.0, got "
                f"{c['score']!r}")
    return out


def build_b1_suite() -> list:
    cases = build_b1_widths() + build_b1_ties()
    # The seg_len-1 detector, at a geometry cosim can afford.  The production
    # suite runs this construction at 200x60 and at the full 622x300 left-bank
    # shape; 88x39 is the same construction with rw = 65, the smallest width
    # that still offers two non-overlapping lane-15 columns (build_lane15 needs
    # range(PAR_COLS, rw - PAR_COLS, PAR_COLS) to have at least two entries).
    cases.append(build_lane15("b1-lane15", "b1-lane15", 24, 16, 88, 39))
    return cases


def check_b1_suite(cases: list) -> None:
    """Refuse to write a B1 suite that cannot detect a B1 defect."""
    tags = [c["tag"] for c in cases]
    require(len(set(tags)) == len(tags), "duplicate tag")
    require(len(cases) <= TME.MAX_CASES, f"{len(cases)} cases over the bound")

    widths, tws = set(), set()
    for c in cases:
        ph, pw = c["patch"].shape
        th, tw = c["templ"].shape
        widths.add(pw - tw + 1)
        tws.add(tw)
        require(pw <= MAX_PATCH_W and ph <= MAX_PATCH_H
                and tw <= MAX_TEMPL_W and th <= MAX_TEMPL_H,
                f"{c['tag']}: outside the 4.1 envelope")
    for want in (1, 15, 16, 17, 31, 32, 33, 95, 96):
        require(want in widths,
                f"result width {want} is missing -- the banking-boundary "
                f"sweep is what this suite exists for")

    require(MAX_TEMPL_W in tws,
            f"no case at tw = {MAX_TEMPL_W}: seg_len there is "
            f"{PAR_COLS + MAX_TEMPL_W - 1}, the longest the shortened load "
            f"ever has to be, and the case where B1 saves almost nothing")
    require(min(tws) <= 24,
            "no small template: B1 saves (232 - (tw+15)) cycles per tile, so "
            "a suite of wide templates would measure the change at its weakest")

    ties = [c for c in cases if c["margin"] == 0.0]
    require(len(ties) >= 2,
            "fewer than two exact ties; the row-major first-occurrence rule "
            "is what a lane-major reduction would break")

    # The width cases must between them land an argmax on the last lane, because
    # rw mod PAR_COLS is what decides whether the final tile is full.
    on_last_lane = [c["tag"] for c in cases
                    if "lane15" not in c and c["x"] % PAR_COLS == PAR_COLS - 1]
    require(on_last_lane,
            f"no width case puts its argmax on lane {PAR_COLS - 1}; the full "
            f"final tile (rw a multiple of {PAR_COLS}) is then never the case "
            f"that reports the answer")

    lane = [c for c in cases if "lane15" in c]
    require(lane,
            "no lane-15 pair -- without it the seg_len-1 off-by-one, the one "
            "defect this change actually invites, is untested")
    for c in lane:
        require(c["lane15"]["all_moved"],
                f"{c['tag']}: some stale-register value leaves the argmax "
                f"where it was; the pair construction did not take")
        require(c["lane15"]["n_distinct_mutant_winners"] >= 1,
                f"{c['tag']}: lane-15 record malformed")


# ---------------------------------------------------------------------------
# Suite assembly and emission
# ---------------------------------------------------------------------------

def check_config() -> None:
    """Re-derive the production geometries and confirm the shared constants."""
    require((TME.MAX_PATCH_W, TME.MAX_PATCH_H) == (MAX_PATCH_W, MAX_PATCH_H)
            and (TME.MAX_TEMPL_W, TME.MAX_TEMPL_H) == (MAX_TEMPL_W,
                                                       MAX_TEMPL_H),
            "envelope constants disagree with tme_generate_golden")
    require(TME.DMA_MAX_BYTES == DMA_MAX_BYTES, "DMA bound disagrees")
    expect = {
        "left-bank": (622, 300), "right-bank": (622, 224),
        "base-421": (421, 214), "base-399": (399, 224),
        "base-456": (456, 300), "ferrule-left": (619, 134),
        "ferrule-right": (619, 124),
    }
    for key, (tw, th) in GEOMETRIES.items():
        got = patch_shape(tw, th)
        require(got == expect[key],
                f"{key}: template {tw}x{th} builds a {got[0]}x{got[1]} patch, "
                f"not the {expect[key][0]}x{expect[key][1]} the detector "
                f"produces — the back-solved template size is wrong")
        require(105 <= tw <= 164, f"{key}: tw {tw} outside the observed "
                                  f"105..164 production range")
        require(th <= MAX_TEMPL_H, f"{key}: th {th} over MAX_TEMPL_H")


def build_suite() -> list:
    cases = []
    for key in GEOMETRIES:
        cases.append(build_targeted(key, SCORE_TARGETS[key]))
    cases.append(build_negative_1x1())
    cases.append(build_tie_exact())
    cases.append(build_tie_near("prod-tie-near-first", "tie-near", 0.40, True))
    cases.append(build_tie_near("prod-tie-near-second", "tie-float32", 0.40,
                                False))
    cases.append(build_flat_region())
    cases.append(build_lane15("prod-lane15-small", "lane15-small",
                              24, 16, 200, 60))
    tw, th = GEOMETRIES["left-bank"]
    pw, ph = patch_shape(tw, th)
    cases.append(build_lane15("prod-lane15-full", "lane15-full",
                              tw, th, pw, ph))
    cases.append(build_max_result())
    return cases


def check_suite(cases: list) -> None:
    """Refuse to write a suite that cannot detect what it exists to detect."""
    tags = [c["tag"] for c in cases]
    require(len(set(tags)) == len(tags), "duplicate tag")
    require(len(cases) <= TME.MAX_CASES,
            f"{len(cases)} cases exceeds the TB manifest bound "
            f"{TME.MAX_CASES}")

    shapes = {(c["patch"].shape[1], c["patch"].shape[0]) for c in cases}
    for want in ((622, 300), (622, 224), (421, 214), (399, 224), (456, 300),
                 (619, 134), (619, 124)):
        require(want in shapes,
                f"production shape {want[0]}x{want[1]} is missing — this suite "
                f"exists to run the shapes the detector actually produces")

    scores = sorted(c["score"] for c in cases)
    require(any(s < -0.2 for s in scores),
            "no case reports a negative best score; the sign class and the "
            "IEEE-754 sign bit are both untested")
    for target in (0.20, 0.24, 0.30, 0.33, 0.50, 0.70):
        require(any(abs(s - target) < 0.01 for s in scores),
                f"no case lands near the {target} score target")

    require(any(c["n_exact_ties"] > 1 for c in cases),
            "no exact tie; the row-major first-occurrence rule is untested")
    require(any(0 < c["margin"] < SCORE_TOL for c in cases),
            f"no near tie under SCORE_TOL={SCORE_TOL}; 0.2 and 0.3 are never "
            f"asked to agree on a separation only the integers can see")
    require(any("lane15" in c for c in cases), "no lane-15 case")
    require(any(c["n_flat"] > 0 for c in cases),
            "no flat window anywhere; the d == 0 -> +0.0 path is untested")

    disagree = [c["tag"] for c in cases if not c["float_agrees"]]
    if disagree:
        print(f"\n  NOTE: the float64 map's argmax differs from the exact "
              f"argmax on {disagree}.")

    # The one that decides what 0.2 may be asserted against.  Reported rather
    # than raised: a case 0.2 cannot reproduce is still a valid 0.3 vector, and
    # burying it in a retry loop would hide the most interesting thing the
    # suite has to say.
    dut_bad = [c["tag"] for c in cases if not c["dut32_agrees"]]
    if dut_bad:
        print(f"\n  NOTE: the float32 DUT model does NOT reproduce the exact "
              f"argmax on {dut_bad}. Assert those locations against 0.3 only; "
              f"for 0.2 they are score-only cases.")
    else:
        print(f"\n  every case: the float32 DUT model reproduces the exact "
              f"integer argmax, smallest separation "
              f"{min(c['margin'] for c in cases if c['margin'] < 900):.3e}")


def write_counts(cases: list, out: Path) -> Path:
    """The 0.3 acceptance sidecar: exact integers, no score, no tolerance."""
    lines = [
        "# tme production count oracle — contract 0.3 acceptance",
        "# score = q / sqrt(A*(N-A) * d);  q = N*C - A*B;  d = B*(N-B)",
        "# CASE index tag pw ph tw th N A dA rw rh x y B C q d "
        "n_exact_ties n_flat q_min q_max b_min b_max float_agrees "
        "dut32_agrees",
        "# PROBE case_index label x y B C q d",
        f"CASES {len(cases)}",
    ]
    for i, c in enumerate(cases):
        ph, pw = c["patch"].shape
        th, tw = c["templ"].shape
        w = c["widths"]
        lines.append(
            f"CASE {i} {c['tag']} {pw} {ph} {tw} {th} {c['n']} {c['a']} "
            f"{c['da']} {c['rw']} {c['rh']} {c['x']} {c['y']} {c['b']} "
            f"{c['c']} {c['q']} {c['d']} {c['n_exact_ties']} {c['n_flat']} "
            f"{w['q_min']} {w['q_max']} {w['b_min']} {w['b_max']} "
            f"{int(c['float_agrees'])} {int(c['dut32_agrees'])}")
    n_probes = sum(len(c["probes"]) for c in cases)
    lines.append(f"PROBES {n_probes}")
    for i, c in enumerate(cases):
        for label, px, py, b, cc, q, d in c["probes"]:
            lines.append(f"PROBE {i} {label} {px} {py} {b} {cc} {q} {d}")
    path = out / "tb_tme_counts_prod.txt"
    path.write_text("\n".join(lines) + "\n", newline="\n")
    return path


def normalise_newlines(path: Path) -> None:
    """Force LF, so the pinned hashes mean the same thing on every platform.

    `Path.write_text` follows the platform, which is how the csim/cosim/hw
    manifests came to be CRLF on this build machine.  That is harmless for
    those suites -- nothing hashes them -- but this package's acceptance test
    IS its hash record, and a suite that only verifies on the machine that
    wrote it verifies nothing about a regeneration anywhere else.  The .bin
    blobs are unaffected either way; only the two text artifacts need this.
    """
    raw = path.read_bytes()
    fixed = raw.replace(b"\r\n", b"\n")
    if fixed != raw:
        path.write_bytes(fixed)


def write_hashes(paths, out: Path, name: str = "prod") -> Path:
    """A `sha256sum -c`-compatible record over every file the suite wrote."""
    lines = []
    for p in paths:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{h}  {p.name}")
    path = out / f"tb_tme_{name}.sha256"
    path.write_text("\n".join(lines) + "\n", newline="\n")
    return path


def report(cases: list) -> None:
    print("\n--- production suite ---")
    print(f"  {'tag':<24s} {'patch':>9s} {'templ':>8s} {'score':>9s} "
          f"{'(x,y)':>13s} {'margin':>10s} {'q':>12s} {'N':>7s}")
    for c in cases:
        ph, pw = c["patch"].shape
        th, tw = c["templ"].shape
        loc = f"({c['x']},{c['y']})"
        print(f"  {c['tag']:<24s} {pw:>4d}x{ph:<4d} {tw:>3d}x{th:<4d} "
              f"{c['score']:+9.6f} {loc:>13s} {c['margin']:10.6f} "
              f"{c['q']:>12d} {c['n']:>7d}")

    print("\n  widths observed across the suite (Phase 5 declares "
          "q signed 28, N*C/A*B signed 30, |q|^2*d unsigned 81):")
    q_obs = max(max(abs(c["widths"]["q_max"]), abs(c["widths"]["q_min"]))
                for c in cases)
    q_bound = max(c["widths"]["q_bound"] for c in cases)
    xb = max(c["widths"]["xprod_bits"] for c in cases)
    print(f"    max |q| observed {q_obs:,} ({int(q_obs).bit_length()} bits "
          f"+ sign); Frechet bound N^2/4 = {q_bound:,} "
          f"({q_bound.bit_length()} bits + sign, fits 28 with "
          f"{100 * (1 - q_bound / (1 << 27)):.1f}% spare)")
    print(f"    max |q|^2 * d = {xb} bits (bound 81)")

    lane = [c for c in cases if "lane15" in c]
    for c in lane:
        print(f"\n  {c['tag']}: peak column {c['x']} = lane "
              f"{c['x'] % PAR_COLS} of tile {c['x'] // PAR_COLS}; all 256 "
              f"stale-register values move the argmax, and the smallest peak "
              f"score change any of them causes is "
              f"{c['lane15']['min_peak_dscore']:.6f} "
              f"({c['lane15']['min_peak_dscore'] / SCORE_TOL:.1f}x SCORE_TOL)")

    near = [c for c in cases if c["category"] == CAT_TIE]
    for c in near:
        print(f"  {c['tag']}: margin {c['margin']:.3e}, "
              f"{c['n_exact_ties']} exact tie(s), float argmax "
              f"{'agrees' if c['float_agrees'] else 'DIFFERS'}")


def write_b1(out: Path) -> int:
    """Emit the Priority 4 suite and leave the production vectors alone.

    Separate from main()'s production path on purpose: `tb_tme_prod.sha256` is
    the acceptance test for that package, so a run that regenerates the B1
    vectors must not be able to rewrite a single production byte.  Nothing
    below reads or writes a `*_prod.*` file.
    """
    cases = build_b1_suite()
    check_b1_suite(cases)
    TME.write_suite(cases, "b1", out=out)
    normalise_newlines(out / "tb_tme_cases_b1.txt")
    written = [out / "tb_tme_cases_b1.txt",
               out / "tb_tme_patches_b1.bin",
               out / "tb_tme_templs_b1.bin"]
    rec = write_hashes(written, out, name="b1")

    print()
    print("--- B1 suite (Priority 4) ---")
    print(f"  {'tag':<20s} {'patch':>9s} {'templ':>8s} {'rw':>4s} {'rh':>4s} "
          f"{'T':>3s} {'seg_len':>8s} {'score':>9s} {'(x,y)':>11s} "
          f"{'lane':>5s} {'margin':>9s}")
    for c in cases:
        ph, pw = c["patch"].shape
        th, tw = c["templ"].shape
        rw, rh = pw - tw + 1, ph - th + 1
        print(f"  {c['tag']:<20s} {pw:4d}x{ph:<4d} {tw:3d}x{th:<4d} "
              f"{rw:4d} {rh:4d} {-(-rw // PAR_COLS):3d} "
              f"{PAR_COLS + tw - 1:8d} {c['score']:+9.6f} "
              f"({c['x']:3d},{c['y']:3d}) {c['x'] % PAR_COLS:5d} "
              f"{c['margin']:9.6f}")
    for c in cases:
        if "lane15" in c:
            m = c["lane15"]
            print()
            print(f"  {c['tag']}: the seg_len-1 mutation moves the argmax for "
                  f"all 256 stale values.")
            print(f"    smallest peak score change {m['min_peak_dscore']:.6f} "
                  f"-- at that stale value a score-tolerance assert would see")
            print(f"    NOTHING; only the {m['n_distinct_mutant_winners']} "
                  f"distinct displaced argmaxes catch it.")
    print()
    print(f"wrote {len(written)} vector files + {rec.name}")
    for line in rec.read_text().splitlines():
        print("  " + line)
    print()
    print('Run with run_hls_b1.tcl (csim_design -argv "b1").  No *_prod.* '
          'file was read or written.')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate the production and B1 vector packages.")
    ap.add_argument("--suite", choices=("prod", "b1"), default="prod",
                    help="prod (default) writes the pinned production package; "
                         "b1 writes only the Priority 4 banking-boundary suite "
                         "and touches no production file.")
    args = ap.parse_args(argv)

    # The cv2 cross-check inside TME.score_map's neighbourhood runs on the
    # generic path for the same reason it must next door: an IPP build
    # dispatches to different arithmetic, and a quoted number from a dispatch
    # nobody named is not reproducible.  Fatal, not a warning.
    TME.require_generic_opencv()
    check_config()
    selftest_lane15_model()

    if args.suite == "b1":
        return write_b1(Path("."))

    cases = build_suite()
    check_suite(cases)

    out = Path(".")
    # Emitted by the shared writer, not a copy of it: the row format has to be
    # byte-identical to the csim/cosim/hw manifests or tme_tb.cpp and
    # tme_standalone_bringup.py cannot read this suite, and a second
    # implementation of the format is a second thing to keep in sync.
    TME.write_suite(cases, "prod", out=out)
    normalise_newlines(out / "tb_tme_cases_prod.txt")
    counts = write_counts(cases, out)
    written = [out / "tb_tme_cases_prod.txt",
               out / "tb_tme_patches_prod.bin",
               out / "tb_tme_templs_prod.bin",
               counts]
    rec = write_hashes(written, out)

    report(cases)
    print(f"\nwrote {len(written)} vector files + {rec.name}")
    for line in rec.read_text().splitlines():
        print("  " + line)
    print("\nThe blobs are NOT committed. Regenerate with this file and "
          "verify with `sha256sum -c tb_tme_prod.sha256`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
