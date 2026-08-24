#!/usr/bin/env python3
"""Explicit PL/CPU backends for the terminal detector — the Stage 2 seam.

WHY THIS FILE EXISTS
--------------------
Stages 2-4 of B2/100 (the first real PDF, the stress page, the 36-page
corpus) need a `detect_page()` that can run its three heavy stages on the
fabric.  Nothing did: the detector has no backend seam at all, and the only
two scripts that import `tme_driver` and also open PDFs use it for GEOMETRY
inside analytical cycle-model tools.  This module is that seam.

    cpu           binarize CPU     patch CPU (per base)   match CPU
    pl-binarize   binarize PL      patch CPU (per base)   match CPU
    pl-extract    binarize PL      patch PL (side bank)   match CPU
    pl-all        binarize PL      patch PL (side bank)   match PL

    cpu-sidebank  binarize CPU     patch CPU (side bank)  match CPU
                  ^ DIAGNOSTIC, not one of the four.  It is the PL's
                    organisation executed entirely on the host, and it exists
                    so the parity ladder below can be walked with no board.

NO SILENT FALLBACK.  Selecting a `pl-*` backend and not getting the PL is a
failed run, never a quiet hand-back to the CPU.  Every constructor here
raises rather than degrading, and `Backend.describe()` states exactly which
stage ran where so a transcript can never be ambiguous about what produced a
number.

THE PARITY LADDER — read this before comparing anything to the frozen oracle
---------------------------------------------------------------------------
The frozen 36-page CPU oracle was established on `cpu`.  `pl-all` differs
from it in THREE independent ways, and lumping them together is how a real
hardware fault hides behind an expected arithmetic difference.  Each rung
changes exactly one thing:

    cpu           -> pl-binarize   isolates THE BINARISER      (A)
    pl-binarize   -> pl-extract    isolates THE ORGANISATION   (B)
    pl-extract    -> pl-all        isolates THE MATCHER SILICON (C)

Only rung C is required to be identical.  A and B are EXPECTED to differ, for
reasons that are arithmetic and already documented elsewhere in this project:

(A) THE BINARISER IS NOT `to_binary_inv`.  `binarize_core` computes an
    integer 3x3 Gaussian with a TRUNCATING `sum >> 4`; `cv2.GaussianBlur`
    rounds.  `binarize_dma_checks.cpu_golden` says so in as many words, and
    gate 3 asserts on a real page that the truncating and rounding oracles
    genuinely disagree.  The core also zeroes the 1-pixel border that OpenCV
    fills by reflection, and it takes a FIXED threshold register where
    `to_binary_inv` runs Otsu over the blurred page.  `PlBinarizer` therefore
    reproduces the core's own arithmetic on the host to pick the threshold,
    and `cpu` and `pl-binarize` are NOT expected to agree pixel-for-pixel.

(B) THE PATCH ORGANISATION DIFFERS.  `best_template_match_local` builds one
    patch PER TEMPLATE BASE, sized at that base's largest scale.  The PL
    extractor emits one patch PER CANDIDATE, sized to the largest template of
    that SIDE — `tme_full_search_baseline.py`'s `cpu_per_base` and
    `pl_side_bank`, the two policies it refuses to add together.  The PL
    patch is a strict superset of every CPU patch in both dimensions, so the
    correlation searches a larger domain and the TM_CCOEFF_NORMED
    denominators are taken over different pixels.  Winners can and do move.
    This is a change of workload, not a fault.

(C) THE MATCHER IS THE SAME ARITHMETIC.  Given the same patch and the same
    template, `tme_top` and `cv2.matchTemplate` agree — that is the claim
    nine hardware vectors have carried since 2026-08-07, and it is the only
    rung a hardware fault can hide in.  Compare `pl-extract` against `pl-all`
    with exact locations and exact row-major tie behaviour, score error
    <= 0.005 (the board PASS criterion; never "N/N exact score").

REFINEMENT CANNOT RUN ON THIS RTL, AND THAT IS NOT A FALLBACK
-------------------------------------------------------------
`refine_misaligned_terminal_boxes` matches with `prefer_local_alignment=True`,
which takes the argmax of `result - w*norm_dist` over the WHOLE correlation
map.  `tme_top` returns a scalar argmax of the RAW map and no map, so the
adjusted argmax is not recoverable from what the hardware reports — the
already-measured "65.6% of refinement needs a non-scalar argmax".  Refinement
therefore runs on the host under every backend.  It is declared, counted, and
printed by `describe()`; it is a stated capability boundary of the current
RTL, not a silent degradation, and `--require-pl-refine` exists only so a
future RTL can be made to fail loudly here instead of being missed.

TIE BREAKS AND ORDER
--------------------
Every reduction in this file uses STRICTLY GREATER (`>`), so the first
candidate reaching a maximum wins — per scale, per base, per kind.  That is
the CPU baseline's loop nesting, `tme_driver.build_trials`'s frozen order,
and `match_candidate`'s documented rule, and the three must stay in
agreement: `test_pl_backends.py` asserts it against a constructed tie.

    python pl_backends.py --selftest      # no board, no PDF
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

import terminal_counter_endpoint_first as det

# `cv2` is imported the same way the detector does it: at module level.  A
# backend module that could not correlate would be useless anyway, and a
# lazy import here would only move the failure to the middle of a page.
import cv2


BACKENDS = ("cpu", "pl-binarize", "pl-extract", "pl-all")
DIAGNOSTIC_BACKENDS = ("cpu-sidebank", "cpu-production")
ALL_BACKENDS = BACKENDS + DIAGNOSTIC_BACKENDS

#: Which patch organisation each backend searches.  `cpu_per_base` and
#: `pl_side_bank` are `tme_full_search_baseline.py`'s two policies; the names
#: are deliberately identical so a reader can move between the files.
ORGANISATION = {
    "cpu":          "cpu_per_base",
    "pl-binarize":  "cpu_per_base",
    "cpu-sidebank": "pl_side_bank",
    "cpu-production": "pl_side_bank",
    "pl-extract":   "pl_side_bank",
    "pl-all":       "pl_side_bank",
}

#: Where each stage runs.  Printed verbatim by `describe()`.
STAGE_MAP = {
    "cpu":          ("cpu", "cpu", "cpu"),
    "pl-binarize":  ("pl",  "cpu", "cpu"),
    "pl-extract":   ("pl",  "pl",  "cpu"),
    "pl-all":       ("pl",  "pl",  "pl"),
    "cpu-sidebank": ("cpu", "cpu", "cpu"),
    "cpu-production": ("cpu(core-equivalent)", "cpu", "cpu"),
}

ANCHOR_DISTANCE_WEIGHT = 0.12          # det.best_template_match_local's default

#: The board PASS criterion, re-exported so a comparison written against
#: this module cannot quietly pick its own tolerance. |score - gold| <=
#: this AND exact (x, y); never "N/N exact score".
SCORE_TOL_ASSERT = 0.005

#: The extractor's descriptor limit (`tme_driver._MAX_CANDIDATES`). Imported
#: lazily below rather than hardcoded twice; this default is only the value
#: used when tme_driver is not importable (the CPU-only backends never
#: dispatch a batch, so it is never load-bearing there).
try:
    from tme_driver import _MAX_CANDIDATES as MAX_BATCH
except Exception:                                            # noqa: BLE001
    MAX_BATCH = 64


class BackendError(RuntimeError):
    """A backend could not do what its name promises.  Never caught to fall back."""


# ---------------------------------------------------------------------------
# The shared classification tail.
# ---------------------------------------------------------------------------

def decide_kind(hits: Dict[str, dict], score_thresh: float,
                ferrule_score_thresh: float, score_margin: float) -> dict:
    """The tail of `classify_endpoint`, lifted so every backend shares it.

    Lifted rather than duplicated on purpose: this is the arithmetic that
    turns per-kind scores into a class, and a backend that reimplemented it
    would be able to disagree with the CPU for a reason that has nothing to
    do with the stage it replaced.  The only thing a backend supplies is
    `hits`; the decision is identical for all of them.

    `hits[kind]` needs "score" and "box"; anything else is carried through.
    """
    if not hits:
        return {"kind": "unknown", "score": -1.0, "box": None,
                "male_score": -1.0, "female_score": -1.0,
                "ferrule_score": -1.0}

    ranked = sorted(hits.items(), key=lambda item: item[1]["score"],
                    reverse=True)
    best_kind, best_hit = ranked[0]
    second_score = ranked[1][1]["score"] if len(ranked) > 1 else -1.0

    needed = ferrule_score_thresh if best_kind == "ferrule" else score_thresh
    if best_hit["score"] < needed or (best_hit["score"] - second_score) < score_margin:
        final_kind = "unknown"
    else:
        final_kind = best_kind

    return {
        "kind": final_kind,
        "score": best_hit["score"],
        "box": best_hit["box"],
        "male_score": hits.get("male", {}).get("score", -1.0),
        "female_score": hits.get("female", {}).get("score", -1.0),
        "ferrule_score": hits.get("ferrule", {}).get("score", -1.0),
    }


# NOTE on `sorted`: Python's sort is stable, so equal scores keep dict
# insertion order — male, female, ferrule as `build_side_templates` builds
# them.  `classify_endpoint` does exactly this and the frozen oracle carries
# the consequence, so it is reproduced rather than "fixed".


def anchor_adjust(raw_score: float, abs_x: int, abs_y: int,
                  base_templ: np.ndarray, side: str, scale: float,
                  tw: int, th: int, endpoint: Tuple[float, float],
                  weight: float = ANCHOR_DISTANCE_WEIGHT) -> Tuple[float, float]:
    """`(adjusted_score, anchor_dist)` — `best_template_match_local`'s penalty.

    `abs_x/abs_y` are the match's ABSOLUTE page coordinates (patch origin
    already added).  The anchor offset comes from the BASE template scaled by
    `scale`, not from the resized template's own dimensions: that is what the
    CPU does, and on a half-integer product the two differ by a pixel.
    """
    base_ax, base_ay = det.side_template_anchor(base_templ, side)
    anchor_x = abs_x + base_ax * scale
    anchor_y = abs_y + base_ay * scale
    anchor_dist = float(math.hypot(anchor_x - endpoint[0],
                                   anchor_y - endpoint[1]))
    norm_dist = anchor_dist / max(8.0, 0.5 * (tw + th))
    return float(raw_score) - weight * norm_dist, anchor_dist


# ---------------------------------------------------------------------------
# Binarisers.
# ---------------------------------------------------------------------------

class CpuBinarizer:
    """`to_binary_inv` — OpenCV blur + Otsu.  The frozen oracle's binariser."""

    where = "cpu"

    def __call__(self, gray: np.ndarray) -> np.ndarray:
        return det.to_binary_inv(gray)

    def threshold_used(self) -> Optional[int]:
        return None


class CoreBinarizer:
    """`binarize_core`'s arithmetic on the HOST.  The production oracle's stage 1.

    `CpuBinarizer` is `to_binary_inv` -- OpenCV's ROUNDING blur, a reflected
    border, and Otsu over that.  The core truncates (`sum >> 4`), zeroes the
    1-pixel border, and takes a fixed threshold register.  Those are rung A of
    the parity ladder, and rung A is EXPECTED to differ, which is exactly why
    `cpu` cannot be the oracle a board run is required to reproduce: 29 of 36
    pages differ from `pl-all` under the ladder criterion, and most of that is
    rung A plus rung B rather than anything the fabric did wrong.

    This class is what closes rung A on the host.  It picks the threshold the
    way `PlBinarizer` does -- Otsu over the core's OWN blurred image -- and
    then applies the core's own thresholding, so `cpu-production` and
    `pl-binarize` differ in NOTHING but which chip ran the convolution.

    It is not a re-implementation of `PlBinarizer`; both call the same two
    functions, so a change to the core's arithmetic moves them together.
    """

    where = "cpu(core-equivalent)"

    def __init__(self):
        self._threshold: Optional[int] = None

    def __call__(self, gray: np.ndarray) -> np.ndarray:
        self._threshold = otsu_on_truncating_blur(gray)
        return cpu_binary_like_core(gray, self._threshold)

    def threshold_used(self) -> Optional[int]:
        return self._threshold


class PlBinarizer:
    """`binarize_core` on the fabric, with the host picking its threshold.

    The core takes a FIXED threshold register and applies its own truncating
    integer Gaussian, so "run Otsu the way the CPU does and hand over the
    number" is wrong twice over: OpenCV's Otsu runs on a ROUNDED blur, and
    the core's border is zero where OpenCV reflects.  The threshold is
    therefore chosen on the core's OWN blurred image, computed here by
    `binarize_dma_checks.cpu_golden`'s arithmetic, so the only difference
    left between this and the core is the fabric itself — which gate 3 has
    already shown to be bit-exact over a full 63,078,400-byte page.
    """

    where = "pl"

    def __init__(self, pl):
        self._pl = pl
        self._threshold: Optional[int] = None

    def __call__(self, gray: np.ndarray) -> np.ndarray:
        self._threshold = otsu_on_truncating_blur(gray)
        out = self._pl.binarize_page(gray, self._threshold)
        if out is None:
            raise BackendError(
                "binarize_page() returned None; a pl-* backend does not fall "
                "back to the CPU binariser")
        return out

    def threshold_used(self) -> Optional[int]:
        return self._threshold


def truncating_blur(gray: np.ndarray) -> np.ndarray:
    """`binarize_core`'s integer 3x3 Gaussian with the truncating `>> 4`.

    The interior only — shape (h-2, w-2).  Identical expression to
    `binarize_dma_checks.cpu_golden`, kept here rather than imported so this
    module does not drag in that file's board-side dependencies;
    `test_pl_backends.py` asserts the two agree wherever both can run.

    **Do not call this on a whole production page.**  It returns int32 and
    builds several same-sized temporaries on the way, so a 9792x6336 page
    costs about 248 MB for the result alone.  `blur_stripes` is the streaming
    form and is what everything on the board path uses; this stays as the
    single expression both of them are defined by, and as the reference the
    stripes are checked against.
    """
    g = np.ascontiguousarray(gray, dtype=np.uint8).astype(np.int32)
    return (
        g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:]
        + 2 * g[1:-1, :-2] + 4 * g[1:-1, 1:-1] + 2 * g[1:-1, 2:]
        + g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]
    ) >> 4


# How much int32 blur to hold at once, in bytes.  Nothing here scales with
# the page: at 8 MB a 9792-wide page is blurred 204 interior rows at a time,
# and the whole threshold decision costs a 256-bin histogram.
_STRIPE_BUDGET_BYTES = 8 << 20

# OpenCV's `getThreshVal_Otsu_8u` guards its ratios with FLT_EPSILON, not
# DBL_EPSILON, even though every accumulator in it is a double.
_FLT_EPSILON = 1.1920928955078125e-07


def stripe_rows(width: int, budget_bytes: int = _STRIPE_BUDGET_BYTES) -> int:
    """Interior rows per stripe, from the width and a byte budget."""
    return max(1, int(budget_bytes) // max(1, 4 * int(width)))


def blur_stripes(gray: np.ndarray, rows: Optional[int] = None):
    """Yield `(y0, stripe)` over the truncating blur, a band at a time.

    `y0` indexes the INTERIOR, so the stripe covers interior rows
    `y0 .. y0 + len(stripe)`, which are source rows `y0 .. y0 + len + 2` and
    output rows `y0 + 1 ...`.  Each band is taken as a VIEW of `gray` and
    overlaps its neighbour by the two rows the 3x3 kernel needs, so the
    boundary rows are computed from real neighbours rather than from a
    zero-padded edge -- the stripes concatenate to exactly `truncating_blur`,
    which `test_pl_backends.py` asserts against several stripe heights.

    This exists because the whole-page form is not memory-feasible on the
    board.  At zoom 4 the first production page is 9792x6336: BGR 186 MB,
    grey 62 MB and a full int32 blur 248 MB come to 496 MB before NumPy's
    temporaries, against roughly 290 MiB of userspace once `cma=192M` is
    reserved out of 512 MiB.  Streaming replaces the 248 MB with 8.
    """
    h, w = gray.shape[:2]
    if h < 3 or w < 3:
        return
    step = int(rows) if rows else stripe_rows(w)
    step = max(1, step)
    for y0 in range(0, h - 2, step):
        y1 = min(y0 + step, h - 2)
        yield y0, truncating_blur(gray[y0:y1 + 2])


def blur_histogram(gray: np.ndarray,
                   rows: Optional[int] = None) -> np.ndarray:
    """The 256-bin histogram of the truncating blur, without building it.

    Exact, not sampled: every interior pixel is counted once.  The bins are
    safe to fix at 256 because the kernel weights sum to 16, so the largest
    possible sum is 255*16 = 4080 and `>> 4` brings it back to 255 -- the
    blur cannot leave the uint8 range whatever the input.
    """
    hist = np.zeros(256, dtype=np.int64)
    for _y0, stripe in blur_stripes(gray, rows):
        hist += np.bincount(stripe.reshape(-1), minlength=256)
    return hist


def otsu_from_histogram(hist) -> int:
    """`cv2.threshold(..., THRESH_OTSU)`'s own arithmetic, on a histogram.

    A transcription of OpenCV's `getThreshVal_Otsu_8u`, because Otsu is a
    function of the histogram ALONE and the histogram is the one thing that
    can be accumulated without materialising the image.  The operation order
    is preserved statement for statement -- the running `mu1 *= q1` before
    the update, the FLT_EPSILON guards on both tails, and the STRICT `>` that
    makes the FIRST maximiser win a tie rather than the last.  Reproducing
    the tie rule matters: on a page with a flat between-class variance the
    two conventions differ by a whole grey level, and that is a different
    binary image on every pixel near the threshold.

    `test_pl_backends.py` checks this against `cv2.threshold` on the real
    pages and over randomised histograms; it is not trusted on inspection.
    """
    h = [int(v) for v in hist]
    total = sum(h)
    if total <= 0:
        raise BackendError("cannot pick a threshold from an empty histogram")
    scale = 1.0 / total

    mu = 0.0
    for i in range(256):
        mu += i * float(h[i])
    mu *= scale

    mu1 = 0.0
    q1 = 0.0
    max_sigma = 0.0
    max_val = 0
    for i in range(256):
        p_i = h[i] * scale
        mu1 *= q1
        q1 += p_i
        q2 = 1.0 - q1
        if min(q1, q2) < _FLT_EPSILON or max(q1, q2) > 1.0 - _FLT_EPSILON:
            continue
        mu1 = (mu1 + i * p_i) / q1
        mu2 = (mu - q1 * mu1) / q2
        sigma = q1 * q2 * (mu1 - mu2) * (mu1 - mu2)
        if sigma > max_sigma:
            max_sigma = sigma
            max_val = i
    return int(max_val)


def otsu_on_truncating_blur(gray: np.ndarray,
                            rows: Optional[int] = None) -> int:
    """Otsu over the image the CORE will actually threshold.

    The interior of the truncating blur.  Not the raw grey (a different
    histogram), not OpenCV's rounded blur (off by the rounding), and not a 4x
    downsample (a different histogram again —
    `binarize_dma_checks.otsu_threshold_downsampled` is a different strategy
    and is not what this backend uses).

    Streamed through `blur_histogram` rather than handed to `cv2.threshold`
    as one array.  The threshold is identical -- Otsu reads nothing but the
    histogram -- and the peak cost stops scaling with the page.
    """
    h, w = gray.shape[:2]
    if h < 3 or w < 3:
        raise BackendError(
            f"image {w}x{h} is too small to blur; "
            f"binarize_core needs at least 3x3")
    return otsu_from_histogram(blur_histogram(gray, rows))


def cpu_binary_like_core(gray: np.ndarray, threshold: int) -> np.ndarray:
    """The core's whole output on the host: truncating blur, zeroed border.

    The off-board stand-in for `pl.binarize_page`, and the thing
    `test_pl_backends.py` compares the PL against.  `<=` matches the core and
    `THRESH_BINARY_INV` alike.
    """
    h, w = gray.shape[:2]
    out = np.zeros((h, w), dtype=np.uint8)
    if h < 3 or w < 3:
        return out
    thr = int(threshold)
    # Striped for the same reason as the histogram: the whole-page int32 blur
    # does not fit alongside the page on the board.  The output is the uint8
    # image and nothing else is alive at once.
    for y0, stripe in blur_stripes(gray):
        out[1 + y0:1 + y0 + stripe.shape[0], 1:-1] = np.where(
            stripe <= thr, np.uint8(255), np.uint8(0))
    return out


# ---------------------------------------------------------------------------
# Matchers.  Each returns `hits` for `decide_kind`.
# ---------------------------------------------------------------------------

class CpuPerBaseMatcher:
    """The frozen oracle: `classify_endpoint` unchanged, one patch per base.

    Delegates rather than reimplements. This backend must stay byte-identical
    to the detector run with no backend at all, and the only way to guarantee
    that is to call the same function.
    """

    organisation = "cpu_per_base"
    where = "cpu"

    def __init__(self):
        self.trials_run = 0
        self.calls = 0

    def begin_page(self, page_bin, side_templates, scales, candidates):
        self._page_bin = page_bin
        self._side_templates = side_templates
        self._scales = scales

    def hits_for(self, cand: dict) -> Dict[str, dict]:
        self.calls += 1
        side = cand["side"]
        endpoint = cand["endpoint"]
        hits: Dict[str, dict] = {}
        for kind, templ_list in self._side_templates[side].items():
            best_hit = None
            for templ in templ_list:
                hit = det.best_template_match_local(
                    self._page_bin, templ, endpoint, side, scales=self._scales)
                self.trials_run += len(self._scales)
                if hit is None:
                    continue
                if best_hit is None or hit["score"] > best_hit["score"]:
                    best_hit = hit
            if best_hit is not None:
                hits[kind] = best_hit
        return hits

    def end_page(self):
        pass


class _SideBankMixin:
    """Shared geometry for both side-bank matchers.

    The patch box is `tme_driver.predict_patch_box`, which replicates
    `patch_extract_core`'s own decomposition and clamp order and is proven
    against all 66 rows of the extractor's golden manifest.  It is NOT
    re-derived here: re-derivation is exactly the "106 px patch matched with
    a 152 px assumption" failure the driver's docstring warns about.
    """

    organisation = "pl_side_bank"

    def _patch_box(self, cand, img_w, img_h) -> Tuple[int, int, int, int]:
        from tme_driver import compute_cand_envelope, predict_patch_box
        side = cand["side"]
        ep_x = int(round(cand["endpoint"][0]))
        ep_y = int(round(cand["endpoint"][1]))
        max_tw, max_th = compute_cand_envelope(self._side_templates, side,
                                               self._scales)
        return predict_patch_box(ep_x, ep_y, 0 if side == "left" else 1,
                                 max_tw, max_th, img_w, img_h)

    def _trials(self, side: str) -> List[dict]:
        from tme_driver import build_trials
        if side not in self._trial_cache:
            self._trial_cache[side] = build_trials(self._side_templates[side],
                                                   self._scales)
        return self._trial_cache[side]

    def _base_of(self, side: str, trial: dict) -> np.ndarray:
        return self._side_templates[side][trial["kind"]][trial["base_index"]]


class CpuSideBankMatcher(_SideBankMixin):
    """The PL's ORGANISATION, executed with `cv2.matchTemplate`.

    The middle rung of the parity ladder. Against `cpu` it isolates the
    organisation change; against `pl-all` it isolates the silicon. Without
    it, a `pl-all` run that disagrees with the frozen oracle gives no way to
    say which of the two caused it — and a matcher fault would be indexed as
    "expected geometry difference" and never looked at again.
    """

    where = "cpu"

    def __init__(self):
        self.trials_run = 0
        self.calls = 0
        self._trial_cache: Dict[str, List[dict]] = {}

    def begin_page(self, page_bin, side_templates, scales, candidates):
        self._page_bin = page_bin
        self._side_templates = side_templates
        self._scales = scales

    def hits_for(self, cand: dict) -> Dict[str, dict]:
        self.calls += 1
        side = cand["side"]
        endpoint = cand["endpoint"]
        img_h, img_w = self._page_bin.shape[:2]
        px0, py0, pw, ph = self._patch_box(cand, img_w, img_h)
        patch = self._page_bin[py0:py0 + ph, px0:px0 + pw]
        return self._reduce(patch, px0, py0, side, endpoint)

    def _reduce(self, patch, px0, py0, side, endpoint) -> Dict[str, dict]:
        by_kind: Dict[str, dict] = {}
        ph, pw = patch.shape[:2]
        for trial in self._trials(side):
            if not trial["legal"]:
                continue
            t = trial["pixels"]
            th_, tw_ = t.shape
            # The CPU baseline's rule, equality included: it skips `tw >= pw`
            # even though the PL accepts equality (§4.4).  Parity wins.
            if tw_ >= pw or th_ >= ph:
                continue
            result = cv2.matchTemplate(patch, t, cv2.TM_CCOEFF_NORMED)
            if result.size == 0:
                continue
            self.trials_run += 1
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            abs_x, abs_y = px0 + int(max_loc[0]), py0 + int(max_loc[1])
            score, anchor_dist = anchor_adjust(
                max_val, abs_x, abs_y, self._base_of(side, trial), side,
                trial["scale"], tw_, th_, endpoint)
            hit = {"score": score, "raw_score": float(max_val),
                   "anchor_dist": anchor_dist,
                   "box": (abs_x, abs_y, int(tw_), int(th_)),
                   "kind": trial["kind"], "templ_id": trial["templ_id"],
                   "base_index": trial["base_index"], "scale": trial["scale"]}
            k = trial["kind"]
            # Strictly greater: first trial in the frozen order wins ties.
            if k not in by_kind or hit["score"] > by_kind[k]["score"]:
                by_kind[k] = hit
        return by_kind

    def end_page(self):
        pass


class PlSideBankMatcher(_SideBankMixin):
    """`match_candidate` on the fabric, one side-bank patch per candidate.

    `cv2.minMaxLoc` is never called here. The patch comes from the extractor
    (`pl-all`, `pl-extract` both dispatch the same batch), and the §6.2
    metadata record — not a re-derivation — supplies the patch origin the
    boxes are built from.
    """

    where = "pl"

    def __init__(self, pl, use_pl_matcher: bool = True,
                 cross_check: bool = False):
        self._pl = pl
        self._use_pl_matcher = use_pl_matcher
        self._cpu_fallback_matcher = None if use_pl_matcher else CpuSideBankMatcher()
        # Rung C IN ONE RUN.  Separate `pl-extract` and `pl-all` runs compare
        # two different extractions: each re-renders the page, re-binarises
        # it, re-dispatches the batch, and nothing proves the two matchers
        # were handed the same pixels.  With this on, the CPU reduction runs
        # against the SAME `patch` array the fabric just matched, taken from
        # the same metadata record, inside the same candidate -- so a
        # difference can only be the matcher.
        self._cross_check = ({"trials": 0, "candidates": 0, "mismatches": [],
                              "max_score_delta": 0.0}
                             if (cross_check and use_pl_matcher) else None)
        self.trials_run = 0
        self.calls = 0
        self._trial_cache: Dict[str, List[dict]] = {}
        self._records: Dict[int, dict] = {}
        self._batches = 0
        self._cpu_cmp = None

    def begin_page(self, page_bin, side_templates, scales, candidates):
        """Dispatch the WHOLE candidate batch to the extractor, once.

        One batch, not one call per candidate: the extractor emits its
        metadata records with TLAST at batch end, so the batch is the unit
        the hardware framing is built around.  Records are held by input
        index because `extract_candidates` returns in input order and the
        detector consults candidates in that same order.
        """
        self._page_bin = page_bin
        self._side_templates = side_templates
        self._scales = scales
        self._records = {}
        self._batches = 0
        if not candidates:
            return
        cands = list(candidates)
        # CHUNKED at the descriptor limit.  `_validate_batch_size` REJECTS a
        # batch over 64 rather than truncating it — deliberately, so a page
        # with 65 endpoints fails loudly instead of returning 64 plausible
        # results — which makes chunking the caller's job, and this is the
        # caller.  Real pages run ~20-44 candidates, so this is usually one
        # chunk; a page that needs two must not be the run that discovers it.
        for start in range(0, len(cands), MAX_BATCH):
            chunk = cands[start:start + MAX_BATCH]
            self._batches += 1
            recs = self._pl.extract_candidates(chunk, side_templates, scales)
            if len(recs) != len(chunk):
                raise BackendError(
                    f"the extractor returned {len(recs)} records for "
                    f"{len(chunk)} candidates; nothing downstream compares "
                    f"result count to input count, so this is raised here")
            for i, rec in enumerate(recs):
                # §6.2: one record per input descriptor, IN INPUT ORDER.  The
                # records are keyed by the candidate object below, so a
                # reordered batch would silently attach candidate i's patch
                # to candidate j -- every box would be built from the wrong
                # origin and nothing downstream would notice, because each
                # record is individually well formed.  The core's own
                # ordinal is the one field that can say so.
                got = int(rec["cand_id"])
                if got != i:
                    raise BackendError(
                        f"metadata record {i} of this batch carries "
                        f"cand_id={got}; §6.2 emits one record per input "
                        f"descriptor in input order, so the batch came back "
                        f"reordered and every patch origin after this point "
                        f"belongs to a different candidate")
                self._records[id(chunk[i])] = rec

    def hits_for(self, cand: dict) -> Dict[str, dict]:
        self.calls += 1
        rec = self._records.get(id(cand))
        if rec is None:
            raise BackendError(
                "no extractor record for this candidate — begin_page() must "
                "be given the same candidate objects the detector will pass "
                "to hits_for()")
        patch = rec.get("patch")
        if patch is None:
            raise BackendError(
                f"the extractor marked this candidate invalid and returned no "
                f"patch (record {rec}); the host pre-checks should have made "
                f"that unreachable")

        side = cand["side"]
        endpoint = cand["endpoint"]
        # `x0`/`y0` are the record's own field names (§6.2). The RECORD is
        # authoritative for the geometry a match runs on — re-deriving it is
        # how a clipped candidate ends up matched with the wrong assumption.
        px0, py0 = int(rec["x0"]), int(rec["y0"])

        if not self._use_pl_matcher:
            # `pl-extract`: the PL's patch, the host's correlation.  The
            # patch is the hardware's, so this still exercises the extractor.
            m = self._cpu_fallback_matcher
            m._page_bin = self._page_bin
            m._side_templates = self._side_templates
            m._scales = self._scales
            hits = m._reduce(patch, px0, py0, side, endpoint)
            self.trials_run += m.trials_run
            m.trials_run = 0
            return hits

        trials = self._trials(side)
        score_fn = self._make_score_fn(side, endpoint, px0, py0)
        out = self._pl.match_candidate(patch, px0, py0, trials,
                                       score_fn=score_fn)
        # `trials`, not `len(by_kind)`.  `by_kind` holds at most one winner
        # per class, so counting it reported 3 for a candidate that ran 52
        # matcher invocations -- and that number is what every wall-time and
        # cycle-model comparison downstream is divided by.  The driver
        # reports what it actually dispatched.
        if "trials" not in out:
            raise BackendError(
                "match_candidate() returned no 'trials' count; this backend "
                "reports matcher INVOCATIONS and will not infer them from "
                "the number of class winners")
        self.trials_run += int(out["trials"])
        if self._cross_check is not None:
            self._run_cross_check(patch, px0, py0, side, endpoint, out)
        hits: Dict[str, dict] = {}
        for kind, hit in out["by_kind"].items():
            hits[kind] = {
                "score": hit["score"], "raw_score": hit["raw_score"],
                "box": hit["box"], "kind": kind,
                "templ_id": hit["templ_id"], "base_index": hit["base_index"],
                "scale": hit["scale"],
            }
        return hits

    def _run_cross_check(self, patch, px0, py0, side, endpoint, out) -> None:
        """The CPU's reduction over the fabric's own patch.  Rung C, inline.

        Compared under the board PASS criterion and nothing looser: exact
        `(x, y, w, h)`, exact winning class set, and |score delta| <= 0.005.
        Differences are RECORDED rather than raised, because one page's
        report is worth more than the first mismatch -- `rung_c_report()`
        carries them out, and the caller decides.
        """
        if self._cpu_cmp is None:
            self._cpu_cmp = CpuSideBankMatcher()
        m = self._cpu_cmp
        m._page_bin = self._page_bin
        m._side_templates = self._side_templates
        m._scales = self._scales
        m.trials_run = 0
        cpu_hits = m._reduce(patch, px0, py0, side, endpoint)
        cc = self._cross_check
        cc["candidates"] += 1
        cc["trials"] += m.trials_run
        pl_hits = out["by_kind"]
        for kind in sorted(set(cpu_hits) | set(pl_hits)):
            a, b = cpu_hits.get(kind), pl_hits.get(kind)
            if a is None or b is None:
                cc["mismatches"].append(
                    {"kind": kind, "why": "one side found no winner",
                     "cpu": a and a["box"], "pl": b and b["box"]})
                continue
            box_b = (int(b["box"][0]), int(b["box"][1]),
                     int(b["box"][2]), int(b["box"][3]))
            delta = abs(float(a["score"]) - float(b["score"]))
            cc["max_score_delta"] = max(cc["max_score_delta"], delta)
            if tuple(a["box"]) != box_b or delta > SCORE_TOL_ASSERT:
                cc["mismatches"].append(
                    {"kind": kind, "why": "box" if tuple(a["box"]) != box_b
                     else "score", "cpu": tuple(a["box"]), "pl": box_b,
                     "score_delta": delta})

    def rung_c_report(self) -> Optional[dict]:
        """The inline rung-C result, or None if it was not enabled."""
        return None if self._cross_check is None else dict(self._cross_check)

    def _make_score_fn(self, side, endpoint, px0, py0):
        """The anchor penalty, evaluated on the SAME numbers the CPU uses.

        `match_candidate` hands back the raw register score and the match
        location; the penalty is a pure function of those plus the trial, so
        it is applied identically on both sides of rung C and cannot itself
        be the cause of a `pl-extract`/`pl-all` disagreement.
        """
        def score_fn(raw, mx, my, trial):
            t = trial["pixels"]
            th_, tw_ = t.shape
            score, _ = anchor_adjust(raw, px0 + mx, py0 + my,
                                     self._base_of(side, trial), side,
                                     trial["scale"], tw_, th_, endpoint)
            return score
        return score_fn

    def retained_bytes(self) -> Dict[str, int]:
        """What this page is still holding, counted by BACKING allocation.

        Not `sum(patch.nbytes)`.  `extract_candidates` appends
        `np.array(self._patch_rx_buf)` per candidate — a full-bound copy of
        the receive buffer, `_MAX_PATCH_BYTES` = 251,740 B — and
        `rec["patch"]` is a reshaped SLICE of it, so the whole copy stays
        alive for as long as the record does, which is the whole page.  On
        the corpus maximum of 82 candidates that is ~19.7 MiB retained where
        the naive sum reads a few MB, and it is the one page-level
        allocation that scales with candidate count rather than page size.

        Both numbers are reported so the gap is visible rather than
        inferred.
        """
        from mem_sampler import distinct_backing_bytes
        patches = [r.get("patch") for r in self._records.values()]
        view = sum(int(p.nbytes) for p in patches if p is not None)
        return {"records": len(self._records),
                "patch_view_bytes": int(view),
                "patch_backing_bytes": distinct_backing_bytes(patches),
                "batches": int(self._batches)}

    def end_page(self):
        self._records = {}
        self._batches = 0


# ---------------------------------------------------------------------------
# The facade the detector talks to.
# ---------------------------------------------------------------------------

class Backend:
    """One named configuration of binariser + matcher, and nothing implicit."""

    def __init__(self, name: str, binarizer, matcher, pl=None,
                 require_pl_refine: bool = False):
        if name not in ALL_BACKENDS:
            raise ValueError(f"unknown backend {name!r}; known: "
                             f"{', '.join(ALL_BACKENDS)}")
        self.name = name
        self.organisation = ORGANISATION[name]
        self._binarizer = binarizer
        self._matcher = matcher
        self._pl = pl
        self._require_pl_refine = require_pl_refine
        # Only the side-bank backends dispatch the extractor, and only they
        # need the suppression to land in DDR. `pl-binarize` keeps the CPU's
        # per-base organisation and never reads that buffer again, so it takes
        # the copy like any CPU backend.
        self._extractor_reads_ddr = ORGANISATION[name] == "pl_side_bank"
        self.refine_calls = 0
        self.pages = 0

    # -- stages ------------------------------------------------------------
    def binarize_inv(self, gray: np.ndarray) -> np.ndarray:
        return self._binarizer(gray)

    def suppress_text(self, page_bin: np.ndarray, words, expand: int):
        """Zero the text boxes — IN PLACE when the extractor will read them.

        `build_text_suppressed_binary` returns a COPY. For a CPU backend that
        is exactly right. For `pl-extract`/`pl-all` it is a bug with no error
        attached: `patch_extract_core` reads the shared DDR binary buffer by
        physical address, so a suppressed copy held by the host leaves the
        fabric extracting patches from a page that still has all its text on
        it — the spurious-match mode the suppression exists to prevent, and
        invisible in every log. It surfaces only as a rung-C difference, which
        is how `test_pl_backends.py` found it.

        So the PL path suppresses THROUGH THE DRIVER, into the buffer the
        extractor reads, and hands back a view of that same memory. `expand`
        is passed rather than left at the driver's old hardcoded 3: at the
        default zoom the detector uses 4, and a one-pixel-thinner box around
        every word moves scores everywhere.
        """
        if self._pl is None or not self._extractor_reads_ddr:
            return det.build_text_suppressed_binary(page_bin, words,
                                                    expand=expand)
        self._pl.suppress_text(words, expand=expand)
        return self._pl.binary_view()

    def begin_page(self, page_bin, side_templates, scales, candidates):
        self.pages += 1
        self._matcher.begin_page(page_bin, side_templates, scales, candidates)

    def classify(self, cand, score_thresh, ferrule_score_thresh,
                 score_margin) -> dict:
        return decide_kind(self._matcher.hits_for(cand), score_thresh,
                           ferrule_score_thresh, score_margin)

    def end_page(self):
        """Release whatever the page held.  Called even when a page found
        nothing, so a PL backend never carries one page's extractor records
        into the next page's `hits_for` lookups."""
        self._matcher.end_page()

    def refine_hit(self, page_bin, det_rec, side_templates, scales):
        """Host-side refinement.  See the module docstring — not a fallback.

        Returns the best `prefer_local_alignment=True` hit, or None.
        """
        if self._require_pl_refine:
            raise BackendError(
                "--require-pl-refine was given, but this RTL cannot refine: "
                "prefer_local_alignment takes the argmax of the "
                "anchor-adjusted correlation MAP and tme_top returns only a "
                "scalar argmax of the RAW map. Drop the flag to run "
                "refinement on the host, or supply an RTL that reports a map")
        self.refine_calls += 1
        best = None
        for templ in side_templates[det_rec["side"]][det_rec["kind"]]:
            hit = det.best_template_match_local(
                page_bin, templ, det_rec["endpoint"], det_rec["side"],
                scales=scales, prefer_local_alignment=True)
            if hit is None:
                continue
            if best is None or hit["score"] > best["score"]:
                best = hit
        return best

    # -- the live objects a runner has to gate --------------------------
    @property
    def pl(self):
        """The `PLPipeline`, or None for a CPU backend.

        Public because a production runner has to hand it to
        `safe_teardown.teardown()`, which is the only teardown that reprograms
        the PL instead of exiting and releasing retained CMA pages.
        """
        return self._pl

    @property
    def overlay(self):
        """The loaded `Overlay`, or None.  For the identity/clock gate."""
        return None if self._pl is None else self._pl.overlay

    def rung_c_report(self) -> Optional[dict]:
        """The inline rung-C comparison, when the backend was built with it."""
        fn = getattr(self._matcher, "rung_c_report", None)
        return fn() if fn is not None else None

    def retained_bytes(self) -> dict:
        """Per-page retention, for the memory sampler.

        Zeros rather than None for a matcher that holds nothing per
        candidate: "this backend retains nothing" and "nobody asked" are
        different answers, and the sampler records the first one.
        """
        fn = getattr(self._matcher, "retained_bytes", None)
        if fn is None:
            return {"records": 0, "patch_view_bytes": 0,
                    "patch_backing_bytes": 0, "batches": 0}
        return fn()

    def sampler_arrays(self) -> dict:
        """The backend's own page-sized buffers, by name, for the sampler.

        The detector's `gray` / `page_bin` / `clean_bin` do not describe
        what a `pl-*` process holds.  The driver keeps a full-page CMA GREY
        buffer that the MM2S reads and that nothing on the host side
        references, plus the CMA BINARY buffer that `page_bin` and
        `clean_bin` are views of.  Naming both here puts the grey one into
        the explicit accounting — it was absent from the byte totals
        entirely — and makes the binary one's aliasing visible as a group
        rather than as a size that has to be recognised.

        Empty for a CPU backend: "there is no such buffer" and "it is not
        allocated yet" are different answers, and the second is a `None`
        value under a present key.

        Live objects are handed back deliberately.  The sampler reads
        scalars off them and keeps no reference (`describe_arrays`), which
        is the same contract the detector's own arrays are passed under.
        """
        if self._pl is None:
            return {}
        fn = getattr(self._pl, "image_buffers", None)
        return dict(fn()) if fn is not None else {}

    # -- reporting ---------------------------------------------------------
    def describe(self) -> str:
        b, p, m = STAGE_MAP[self.name]
        thr = self._binarizer.threshold_used()
        parts = [
            f"backend={self.name}",
            f"organisation={self.organisation}",
            f"binarize={b}", f"patch={p}", f"match={m}",
            "refine=cpu (RTL reports no correlation map)",
            f"pages={self.pages}",
            f"classify_calls={self._matcher.calls}",
            f"trials={self._matcher.trials_run}",
            f"refine_calls={self.refine_calls}",
        ]
        if thr is not None:
            parts.insert(6, f"binarize_threshold={thr}")
        cc = self.rung_c_report()
        if cc is not None:
            parts.append(f"rung_c_inline=cands:{cc['candidates']},"
                         f"trials:{cc['trials']},"
                         f"mismatches:{len(cc['mismatches'])},"
                         f"max_score_delta:{cc['max_score_delta']:.6f}")
        return ";".join(parts)

    def close(self) -> bool:
        """True only if nothing was retained.  Mirrors `PLPipeline.close`."""
        if self._pl is None:
            return True
        return bool(self._pl.close())


def make_backend(name: str, overlay: Optional[str] = None, pl=None,
                 require_pl_refine: bool = False,
                 timeout_s: float = 120.0,
                 rung_c_inline: bool = False) -> Backend:
    """Build a backend by name.  Raises rather than degrading.

    `pl` is injectable so the off-board suite can drive every PL path against
    a fake pipeline; when it is None and the name needs hardware, a real
    `PLPipeline` is constructed and any failure propagates.
    """
    if name not in ALL_BACKENDS:
        raise ValueError(f"unknown backend {name!r}; known: "
                         f"{', '.join(ALL_BACKENDS)}")

    if name == "cpu":
        return Backend(name, CpuBinarizer(), CpuPerBaseMatcher(),
                       require_pl_refine=require_pl_refine)
    if name == "cpu-sidebank":
        return Backend(name, CpuBinarizer(), CpuSideBankMatcher(),
                       require_pl_refine=require_pl_refine)
    if name == "cpu-production":
        # The whole of production semantics on the host: the core's own
        # binariser arithmetic, the PL's patch organisation, the same trial
        # order and tie rule, and host refinement.  This -- not `cpu` -- is
        # what a B2/100 board run is required to reproduce 36/36.
        return Backend(name, CoreBinarizer(), CpuSideBankMatcher(),
                       require_pl_refine=require_pl_refine)

    if pl is None:
        if not overlay:
            raise BackendError(
                f"backend {name!r} needs the fabric but no overlay path was "
                f"given; pass --overlay (there is no CPU fallback)")
        try:
            import tme_driver
            pl = tme_driver.PLPipeline(overlay, timeout_s=timeout_s)
        except Exception as exc:                             # noqa: BLE001
            raise BackendError(
                f"backend {name!r} could not open the overlay {overlay!r}: "
                f"{type(exc).__name__}: {exc}. A pl-* run FAILS here rather "
                f"than continuing on the CPU") from exc

    binarizer = PlBinarizer(pl)
    if name == "pl-binarize":
        matcher = CpuPerBaseMatcher()
    elif name == "pl-extract":
        if rung_c_inline:
            raise BackendError(
                "rung_c_inline compares the PL matcher against the CPU on "
                "one extracted patch; pl-extract has no PL matcher to "
                "compare. Use pl-all")
        matcher = PlSideBankMatcher(pl, use_pl_matcher=False)
    else:                                                    # pl-all
        matcher = PlSideBankMatcher(pl, use_pl_matcher=True,
                                    cross_check=rung_c_inline)
    return Backend(name, binarizer, matcher, pl=pl,
                   require_pl_refine=require_pl_refine)


# ---------------------------------------------------------------------------

def _selftest() -> int:
    """Board-free checks of the parts that do not need a PDF or a fabric."""
    import binarize_dma_checks as bdc

    fails = []

    def check(ok, label, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
              + (f": {detail}" if detail else ""))
        if not ok:
            fails.append(label)

    rng = np.random.default_rng(20260822)
    gray = rng.integers(0, 256, size=(64, 96), dtype=np.uint8)

    thr = otsu_on_truncating_blur(gray)
    mine = cpu_binary_like_core(gray, thr)
    theirs = bdc.cpu_golden(gray, thr)
    check(np.array_equal(mine, theirs),
          "cpu_binary_like_core == binarize_dma_checks.cpu_golden",
          f"threshold {thr}")

    ocv = det.to_binary_inv(gray)
    check(not np.array_equal(ocv, mine),
          "to_binary_inv and the core's arithmetic really do DIFFER "
          "(if these ever agree, rung A stops being a real rung)",
          f"{int(np.count_nonzero(ocv != mine))} pixels differ")

    check(set(BACKENDS) == set(STAGE_MAP) - set(DIAGNOSTIC_BACKENDS),
          "STAGE_MAP covers exactly the four backends plus the diagnostic")
    check(all(ORGANISATION[b] in ("cpu_per_base", "pl_side_bank")
              for b in ALL_BACKENDS),
          "every backend names one of the two documented policies")

    empty = decide_kind({}, 0.3, 0.3, 0.0)
    check(empty["kind"] == "unknown" and empty["box"] is None,
          "decide_kind({}) is the same 'unknown' classify_endpoint returns")

    hits = {"male": {"score": 0.5, "box": (1, 2, 3, 4)},
            "female": {"score": 0.5, "box": (5, 6, 7, 8)}}
    tie = decide_kind(hits, 0.3, 0.3, 0.0)
    check(tie["box"] == (1, 2, 3, 4),
          "a tie between kinds keeps dict order (male first), as the frozen "
          "oracle's stable sort does")

    print(f"\n{'OK' if not fails else 'FAILED'}: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())
    print(__doc__)
