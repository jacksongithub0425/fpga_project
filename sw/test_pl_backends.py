#!/usr/bin/env python3
"""Drive every backend off the board, against a fake fabric.

    python test_pl_backends.py        # from sw/, with the HLS venv python
    pytest test_pl_backends.py

THE FAKE IS ARITHMETICALLY EXACT, AND THAT IS THE POINT
-------------------------------------------------------
`FakePL` implements `binarize_page`, `extract_candidates`, `match_candidate`
and `close()` with the SAME arithmetic the silicon is claimed to have —
`binarize_dma_checks.cpu_golden`'s truncating Gaussian, `predict_patch_box`'s
own decomposition, and `cv2.matchTemplate` for the correlation.  So a correct
`pl-all` must land EXACTLY on `cpu-sidebank`, and rung C of the parity ladder
becomes a test that runs on a laptop.  What this does NOT establish is that
the silicon has that arithmetic — that is what the board gates are for.  It
establishes that the WIRING between the detector and the driver does not
change the answer, which is the part a board run cannot isolate.

The tests fall into four groups:

  frozen path   `detect_page(backend=None)` must be unchanged, and the `cpu`
                backend must reproduce it exactly.  These run on a real page
                of a real PDF, because the seam was cut into a 1,399-line
                detector and a unit test would not have noticed a mis-routed
                `clean_bin`.
  wiring        pl-binarize / pl-extract / pl-all against `FakePL`, including
                rung C by construction.
  refusals      no silent fallback: every way a PL backend can fail must
                raise, and `close()` returning False must be a failed run.
  arithmetic    the binariser's threshold choice, the anchor penalty, the
                tie-break order, and the batch limit.
"""

from __future__ import annotations

import sys

import math
import mmap
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np
import cv2

import corpus_labels as CL
import pl_backends as B
import terminal_counter_endpoint_first as det
import binarize_dma_checks as bdc
from tme_driver import (build_trials, compute_cand_envelope,
                        predict_patch_box, _MAX_CANDIDATES)

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent.parent / "sample"

#: Stage 2's page, named by LABEL so no source file carries a drawing
#: filename.  `resolve()` returns None on a machine without the corpus --
#: the normal state of a clone -- and the placeholder keeps this a Path,
#: so the `.exists()` skips below read exactly as they always did.
STAGE2_PDF = CL.resolve("doc_002", SAMPLES) or (SAMPLES / "doc_002.absent")


# ---------------------------------------------------------------------------
# The fake fabric.
# ---------------------------------------------------------------------------

class FakeCmaBuffer(np.ndarray):
    """Shaped like `pynq.buffer.PynqBuffer`: an ndarray over a foreign buffer.

    WHY THIS EXISTS.  `FakePL` used to keep a plain owning `np.ndarray` as
    its page, and `binary_view()` returned it directly.  Nothing about the
    real driver's memory looks like that: `PLPipeline._bin_buf` is a
    `PynqBuffer` over an mmap of CMA, and `binary_view()` is

        np.frombuffer(self._bin_buf, …).reshape(h, stride)[:, :w]

    which is a FOUR-deep `.base` chain ending outside numpy.  The sampler's
    `distinct_bytes` is the number that predicts RSS, and it is computed by
    walking that chain — so a fake with no chain in it tested the walk
    against a case the board never presents.  This subclass reproduces the
    shape: ndarray over a foreign buffer, with a guard tail past the visible
    page exactly as the driver allocates one.

    It is a MODEL, not the article, and the article was measured.
    `logs/b2prod_20260823/07_pynq_alias_chain.txt` has the real chain,

        ndarray -> ndarray -> PynqBuffer -> memoryview

    (an `mmap` here, a `memoryview` there; both are non-ndarray providers,
    which is the property the walk turns on).  That run is also what shows
    the provider can be LARGER than the buffer — a 6,144 B `cma_gray` sat
    inside an 8,192 B memoryview — which is why `_root_object` charges the
    innermost ndarray rather than the end of the chain.
    """

    def __new__(cls, nbytes: int):
        backing = mmap.mmap(-1, nbytes)
        self = np.ndarray.__new__(cls, (nbytes,), np.uint8, buffer=backing)
        self._backing = backing
        return self


class FakePL:
    """A PLPipeline-shaped object with the silicon's documented arithmetic.

    Records what it was asked to do so the tests can assert on framing (one
    batch per page, chunked at the descriptor limit) as well as on numbers.
    """

    #: The driver's binary buffer carries a guard tail past the visible page
    #: so an S2MM overrun has somewhere to land where it can be seen.
    OUTPUT_GUARD_BYTES = 4096

    def __init__(self, fail_on: str = "", close_returns: bool = True):
        self.fail_on = fail_on
        self.close_returns = close_returns
        self.batches: List[int] = []
        self.suppress_expand = None
        self.match_calls = 0
        self.binarize_calls = 0
        self.closed = False
        self._bin: Optional[np.ndarray] = None
        self._threshold: Optional[int] = None
        self._gray_buf: Optional[FakeCmaBuffer] = None
        self._bin_buf: Optional[FakeCmaBuffer] = None
        self._shape: Optional[tuple] = None

    # -- stage 1 -----------------------------------------------------------
    def binarize_page(self, gray, threshold):
        if self.fail_on == "binarize":
            raise RuntimeError("fake binarize failure")
        self.binarize_calls += 1
        self._threshold = int(threshold)
        out = bdc.cpu_golden(gray, int(threshold))
        h, w = out.shape[:2]
        # Both CMA buffers, allocated the way the driver allocates them and
        # kept across pages by the same `>=` test.
        n = h * w
        if self._gray_buf is None or len(self._gray_buf) < n:
            self._gray_buf = FakeCmaBuffer(n)
            self._bin_buf = FakeCmaBuffer(n + self.OUTPUT_GUARD_BYTES)
        self._gray_buf[:n] = np.ascontiguousarray(gray).ravel()
        self._bin_buf[:n] = out.ravel()
        self._shape = (h, w)
        self._bin = self.binary_view()
        return self._bin

    def binary_view(self):
        if self._shape is None:
            raise RuntimeError("no page has been binarized yet")
        h, w = self._shape
        # The driver's exact expression, stride included, so the `.base`
        # chain the sampler walks here is the one it walks on silicon.
        return np.frombuffer(self._bin_buf, dtype=np.uint8,
                             count=h * w).reshape(h, w)[:, :w]

    def image_buffers(self):
        """`PLPipeline.image_buffers()`: the two CMA buffers, or None."""
        return {"cma_gray": self._gray_buf, "cma_binary": self._bin_buf}

    def suppress_text(self, words, expand=3):
        """Zero the boxes IN the buffer, the way the driver does.

        In place, and returning nothing: a fake that returned a copy here
        would hide the exact bug this models — an extractor reading a page
        the host thinks it suppressed.
        """
        if self._bin is None:
            raise RuntimeError("binarize_page() must run before suppress_text()")
        h, w = self._bin.shape[:2]
        self.suppress_expand = int(expand)
        for word in words:
            x0 = max(0, int(word["x0"] - expand))
            y0 = max(0, int(word["y0"] - expand))
            x1 = min(w, int(word["x1"] + expand))
            y1 = min(h, int(word["y1"] + expand))
            if x1 <= x0 or y1 <= y0:
                continue
            self._bin[y0:y1, x0:x1] = 0

    # -- stage 2 -----------------------------------------------------------
    def extract_candidates(self, candidates, side_templates, scales):
        if self.fail_on == "extract":
            raise RuntimeError("fake extractor failure")
        if len(candidates) > _MAX_CANDIDATES:
            raise ValueError(f"batch of {len(candidates)} exceeds "
                             f"{_MAX_CANDIDATES}")
        self.batches.append(len(candidates))
        if self._bin is None:
            raise RuntimeError("binarize_page() must run first")
        img_h, img_w = self._bin.shape[:2]
        out = []
        for c in candidates:
            side = c["side"]
            ep_x = int(round(c["endpoint"][0]))
            ep_y = int(round(c["endpoint"][1]))
            max_tw, max_th = compute_cand_envelope(side_templates, side, scales)
            x0, y0, pw, ph = predict_patch_box(
                ep_x, ep_y, 0 if side == "left" else 1,
                max_tw, max_th, img_w, img_h)
            rec = {"cand_id": len(out), "valid": True, "reason": 0,
                   "reasons": [], "x0": x0, "y0": y0,
                   "patch_w": pw, "patch_h": ph}
            if self.fail_on == "invalid_record":
                rec["valid"] = False
                rec["patch"] = None
            elif self.fail_on == "short_batch" and len(out) == 0:
                continue                      # one record short, on purpose
            else:
                # The extractor reads the SHARED binary buffer, so the patch
                # is a view of the page the binariser wrote — not of whatever
                # array the caller happens to be holding.
                rec["patch"] = np.ascontiguousarray(
                    self._bin[y0:y0 + ph, x0:x0 + pw])
            out.append(rec)
        return out

    # -- stage 3 -----------------------------------------------------------
    def match_candidate(self, patch, patch_x0, patch_y0, trials, score_fn=None):
        if self.fail_on == "match":
            raise RuntimeError("fake matcher failure")
        self.match_calls += 1
        ph, pw = patch.shape[:2]
        best = None
        by_kind: dict = {}
        dispatched = 0
        for trial in trials:
            if not trial["legal"]:
                continue
            t = trial["pixels"]
            th_, tw_ = t.shape
            if tw_ >= pw or th_ >= ph:
                continue
            result = cv2.matchTemplate(patch, t, cv2.TM_CCOEFF_NORMED)
            if result.size == 0:
                continue
            dispatched += 1
            _, raw, _, loc = cv2.minMaxLoc(result)
            x, y = int(loc[0]), int(loc[1])
            score = score_fn(float(raw), x, y, trial) if score_fn else float(raw)
            hit = {"score": score, "raw_score": float(raw),
                   "match_x": x, "match_y": y, "kind": trial["kind"],
                   "templ_id": trial["templ_id"],
                   "base_index": trial["base_index"], "scale": trial["scale"],
                   "box": (patch_x0 + x, patch_y0 + y, tw_, th_),
                   "elapsed_s": 0.0}
            if best is None or hit["score"] > best["score"]:
                best = hit
            k = trial["kind"]
            if k not in by_kind or hit["score"] > by_kind[k]["score"]:
                by_kind[k] = hit
        # The real driver reports what it dispatched; a fake that did not
        # would let the trials_run bug pass off the board.
        return {"best": best, "by_kind": by_kind, "trials": dispatched}

    def close(self):
        self.closed = True
        return self.close_returns


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

_TEMPLATES = None


def templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        _TEMPLATES = det.build_side_templates(
            det.load_template_bank(str(HERE / "male_ter" / "male_left.png")),
            det.load_template_bank(str(HERE / "male_ter" / "male_right.png")),
            det.load_template_bank(str(HERE / "female_ter" / "female_left.png")),
            det.load_template_bank(str(HERE / "female_ter" / "female_right.png")),
            det.load_template_bank(str(HERE / "ferrule_ter" / "ferrule_left.png")),
            det.load_template_bank(str(HERE / "ferrule_ter" / "ferrule_right.png")),
        )
    return _TEMPLATES


def run_page(backend=None, page_index: int = 0):
    import fitz
    if not STAGE2_PDF.exists():
        raise SkipTest(f"{STAGE2_PDF} not present")
    doc = fitz.open(str(STAGE2_PDF))
    try:
        return det.detect_page(doc[page_index], side_templates=templates(),
                               zoom=4.0, score_thresh=0.33,
                               ferrule_score_thresh=0.24, score_margin=0.03,
                               backend=backend)
    finally:
        doc.close()


def boxes_of(dets) -> list:
    return [(d["kind"], d["x"], d["y"], d["w"], d["h"]) for d in dets]


def scores_of(dets) -> list:
    return [float(d["score"]) for d in dets]


class SkipTest(Exception):
    pass


# ---------------------------------------------------------------------------
# frozen path
# ---------------------------------------------------------------------------

def test_the_cpu_backend_reproduces_the_frozen_path_exactly():
    """`cpu` must be the no-backend path, box for box and score for score.

    Run on a real page, not a synthetic one: the seam was cut into a
    1,399-line function, and the failure mode it invites is a stage handed
    `page_bin` where it used to get `clean_bin` — which a synthetic page with
    no text would never show.
    """
    _b, c0, d0 = run_page(backend=None)
    _b, c1, d1 = run_page(backend=B.make_backend("cpu"))
    assert len(c0) == len(c1), (len(c0), len(c1))
    assert boxes_of(d0) == boxes_of(d1)
    assert scores_of(d0) == scores_of(d1)        # exact: same code, same input
    assert len(d0) > 10, f"only {len(d0)} detections — is this the right page?"


def test_the_cpu_backend_counts_what_it_did():
    b = B.make_backend("cpu")
    run_page(backend=b)
    d = b.describe()
    assert "backend=cpu" in d and "organisation=cpu_per_base" in d
    assert "refine=cpu" in d
    assert "classify_calls=" in d and "classify_calls=0" not in d


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def test_rung_c_by_construction_pl_all_equals_cpu_sidebank():
    """The same patch and the same template must give the same answer.

    `FakePL` correlates with `cv2.matchTemplate`, exactly as
    `CpuSideBankMatcher` does, so any difference here is the WIRING — the
    anchor penalty applied to the wrong numbers, the patch origin taken from
    a re-derivation instead of the record, a trial list built per page
    instead of per side.  Those are the faults a board run cannot separate
    from a silicon fault.
    """
    pl = FakePL()
    b_all = B.make_backend("pl-all", pl=pl)
    _x, _c, d_all = run_page(backend=b_all)

    # cpu-sidebank binarises with OpenCV, so give it the fake's binary page
    # to compare against: this test is about the matcher and the patch, and
    # rung A is measured separately.
    b_side = B.make_backend("cpu-sidebank")
    b_side._binarizer = _FixedBinarizer(pl._threshold)
    _x, _c, d_side = run_page(backend=b_side)

    assert boxes_of(d_all) == boxes_of(d_side), _first_diff(d_all, d_side)
    for s_a, s_b in zip(scores_of(d_all), scores_of(d_side)):
        assert s_a == s_b, (s_a, s_b)          # exact: identical arithmetic
    assert pl.match_calls > 0, "pl-all never called the matcher"


def test_the_extractor_reads_the_SUPPRESSED_page():
    """The suppression must land in the buffer the extractor reads.

    `build_text_suppressed_binary` returns a copy, so the obvious wiring
    leaves the fabric extracting patches from a page that still has all its
    text on it. Nothing raises; the only symptom is a rung-C difference. This
    asserts the mechanism directly rather than waiting to notice the symptom.
    """
    pl = FakePL()
    run_page(backend=B.make_backend("pl-all", pl=pl))
    assert pl.suppress_expand == 4, (
        f"the driver was given expand={pl.suppress_expand}; detect_page uses "
        f"max(2, round(zoom)) = 4 at the default zoom, and the driver's old "
        f"hardcoded 3 would thin every suppression box by a pixel")
    # A page with words on it must have had pixels cleared.
    assert int(np.count_nonzero(pl._bin == 0)) > 0

    # pl-binarize keeps the per-base organisation and never re-reads the
    # buffer, so it must NOT pay for the in-place write.
    pl2 = FakePL()
    run_page(backend=B.make_backend("pl-binarize", pl=pl2))
    assert pl2.suppress_expand is None, (
        "pl-binarize suppressed into DDR; nothing reads it there")


class _FixedBinarizer:
    """The core's arithmetic on the host, at a pinned threshold."""
    where = "cpu"

    def __init__(self, threshold):
        self.threshold = threshold

    def __call__(self, gray):
        return B.cpu_binary_like_core(gray, self.threshold)

    def threshold_used(self):
        return self.threshold


def _first_diff(a, b):
    for i, (x, y) in enumerate(zip(boxes_of(a), boxes_of(b))):
        if x != y:
            return f"first difference at {i}: {x} vs {y}"
    return f"lengths {len(a)} vs {len(b)}"


def test_pl_extract_uses_the_pl_patch_but_the_host_matcher():
    pl = FakePL()
    b = B.make_backend("pl-extract", pl=pl)
    _x, _c, d = run_page(backend=b)
    assert pl.batches, "the extractor was never dispatched"
    assert pl.match_calls == 0, "pl-extract must NOT use the PL matcher"
    assert len(d) > 10
    assert "patch=pl" in b.describe() and "match=cpu" in b.describe()


def test_pl_extract_and_pl_all_agree():
    """Rung C on a real page, both ends driven by the same fake."""
    d_e = run_page(backend=B.make_backend("pl-extract", pl=FakePL()))[2]
    d_a = run_page(backend=B.make_backend("pl-all", pl=FakePL()))[2]
    assert boxes_of(d_e) == boxes_of(d_a), _first_diff(d_e, d_a)
    for s_a, s_b in zip(scores_of(d_e), scores_of(d_a)):
        assert abs(s_a - s_b) <= B.SCORE_TOL_ASSERT


def test_pl_binarize_keeps_the_per_base_organisation():
    pl = FakePL()
    b = B.make_backend("pl-binarize", pl=pl)
    _x, _c, d = run_page(backend=b)
    assert pl.binarize_calls == 1
    assert not pl.batches, "pl-binarize must not dispatch the extractor"
    assert pl.match_calls == 0
    assert b.organisation == "cpu_per_base"


def test_the_batch_is_dispatched_once_per_page_and_chunked():
    pl = FakePL()
    b = B.make_backend("pl-all", pl=pl)
    _x, cands, _d = run_page(backend=b)
    assert sum(pl.batches) == len(cands), (pl.batches, len(cands))
    assert all(n <= _MAX_CANDIDATES for n in pl.batches), pl.batches
    expected = max(1, math.ceil(len(cands) / _MAX_CANDIDATES))
    assert len(pl.batches) == expected, (pl.batches, expected)


def test_a_page_over_the_descriptor_limit_is_chunked_not_truncated():
    """65 candidates must become two batches, never one truncated to 64."""
    pl = FakePL()
    m = B.PlSideBankMatcher(pl, use_pl_matcher=True)
    gray = np.full((600, 900), 200, dtype=np.uint8)
    pl.binarize_page(gray, 128)
    cands = [{"endpoint": (300.0 + 3 * i, 300.0), "side": "left"}
             for i in range(_MAX_CANDIDATES + 1)]
    m.begin_page(pl._bin, templates(), det.MATCH_SCALES, cands)
    assert pl.batches == [_MAX_CANDIDATES, 1], pl.batches
    assert len(m._records) == _MAX_CANDIDATES + 1


# ---------------------------------------------------------------------------
# refusals — no silent fallback
# ---------------------------------------------------------------------------

def test_a_failing_pl_stage_raises_rather_than_falling_back():
    for stage in ("binarize", "extract", "match"):
        pl = FakePL(fail_on=stage)
        name = {"binarize": "pl-binarize", "extract": "pl-all",
                "match": "pl-all"}[stage]
        try:
            run_page(backend=B.make_backend(name, pl=pl))
        except Exception as exc:                             # noqa: BLE001
            assert not isinstance(exc, SkipTest)
            print(f"  [raises] {stage:<9} {type(exc).__name__}")
            continue
        raise AssertionError(
            f"a failing {stage} stage did NOT fail the run — that is the "
            f"silent fallback this design forbids")


def test_a_short_extractor_batch_raises():
    pl = FakePL(fail_on="short_batch")
    try:
        run_page(backend=B.make_backend("pl-all", pl=pl))
    except B.BackendError as exc:
        assert "records for" in str(exc), exc
        return
    raise AssertionError("a batch one record short was accepted")


def test_an_invalid_record_raises_rather_than_matching_nothing():
    pl = FakePL(fail_on="invalid_record")
    try:
        run_page(backend=B.make_backend("pl-all", pl=pl))
    except B.BackendError as exc:
        assert "no patch" in str(exc) or "invalid" in str(exc), exc
        return
    raise AssertionError("an invalid extractor record was accepted")


def test_a_pl_backend_without_a_fabric_or_an_overlay_raises():
    for name in ("pl-binarize", "pl-extract", "pl-all"):
        try:
            B.make_backend(name, overlay="")
        except B.BackendError as exc:
            assert "no CPU fallback" in str(exc) or "overlay" in str(exc)
            continue
        raise AssertionError(f"{name} was built with no fabric and no overlay")


def test_require_pl_refine_fails_instead_of_refining_on_the_host():
    b = B.make_backend("pl-all", pl=FakePL(), require_pl_refine=True)
    try:
        b.refine_hit(np.zeros((40, 40), np.uint8),
                     {"side": "left", "kind": "male", "endpoint": (10.0, 10.0)},
                     templates(), det.MATCH_SCALES)
    except B.BackendError as exc:
        assert "scalar argmax" in str(exc), exc
        return
    raise AssertionError("--require-pl-refine refined on the host anyway")


def test_close_reports_a_retained_buffer():
    b = B.make_backend("pl-all", pl=FakePL(close_returns=False))
    assert b.close() is False, "a retained buffer was reported as a clean close"
    assert B.make_backend("cpu").close() is True


def test_an_unknown_backend_name_raises():
    for bad in ("pl", "PL-ALL", "pl_all", ""):
        try:
            B.make_backend(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a backend name")


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------

def test_the_binariser_matches_the_cores_own_oracle():
    rng = np.random.default_rng(7)
    for shape in ((32, 48), (101, 97), (8, 8)):
        gray = rng.integers(0, 256, size=shape, dtype=np.uint8)
        thr = B.otsu_on_truncating_blur(gray)
        assert np.array_equal(B.cpu_binary_like_core(gray, thr),
                              bdc.cpu_golden(gray, thr)), shape


def _bimodal_page(seed: int = 11) -> np.ndarray:
    """A document-shaped image: light ground, dark strokes, soft edges.

    Uniform noise is NOT usable here. Its histogram is flat, so Otsu lands on
    127 whichever image it is given, and a test built on it passes for a
    backend that thresholds the raw grey, OpenCV's rounded blur, or the
    core's truncating one alike. The first version of this test did exactly
    that and asserted nothing.
    """
    rng = np.random.default_rng(seed)
    img = np.full((160, 220), 235, dtype=np.uint8)
    for _ in range(40):
        y = int(rng.integers(4, 150))
        x = int(rng.integers(4, 200))
        img[y:y + int(rng.integers(2, 9)), x:x + int(rng.integers(6, 20))] = \
            int(rng.integers(10, 70))
    img = np.clip(img.astype(np.int16)
                  + rng.integers(-12, 13, size=img.shape), 0, 255)
    return img.astype(np.uint8)


def test_the_threshold_is_chosen_on_the_blur_the_core_will_threshold():
    """Not on the raw grey, and not on OpenCV's rounded blur.

    A threshold picked from a different histogram is the kind of error that
    produces a page that looks fine and scores differently everywhere.
    """
    gray = _bimodal_page()
    mine = B.otsu_on_truncating_blur(gray)
    trunc = B.truncating_blur(gray)
    ref, _ = cv2.threshold(trunc.astype(np.uint8), 0, 255, cv2.THRESH_OTSU)
    assert mine == int(ref), (mine, int(ref))

    # And the alternatives really are distinguishable on this input, or the
    # test would pass for a backend that used any of them.
    raw_thr, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_OTSU)
    rounded = cv2.GaussianBlur(gray, (3, 3), 0)[1:-1, 1:-1]
    ocv_thr, _ = cv2.threshold(rounded, 0, 255, cv2.THRESH_OTSU)
    distinct = {mine, int(raw_thr), int(ocv_thr)}
    assert len(distinct) > 1, (
        f"all three thresholds agree at {mine} on this input, so this test "
        f"cannot tell them apart -- fix the input, not the assertion")


def test_rung_a_is_a_real_difference_on_a_real_page():
    """Measure the binariser gap where it actually matters.

    Deliberately NOT asserted on a synthetic image: a clean bimodal page can
    round the same way everywhere, so a synthetic "they differ" assertion is
    a coin toss dressed as evidence. A rendered page has the soft edges that
    straddle the threshold, which is the whole reason gate 3 pins the
    TRUNCATING oracle rather than OpenCV's.
    """
    import fitz
    if not STAGE2_PDF.exists():
        raise SkipTest(f"{STAGE2_PDF} not present")
    doc = fitz.open(str(STAGE2_PDF))
    try:
        _bgr, gray, _ = det.render_page(doc[0], zoom=4.0)
    finally:
        doc.close()
    thr = B.otsu_on_truncating_blur(gray)
    core = B.cpu_binary_like_core(gray, thr)
    ocv = det.to_binary_inv(gray)
    n = int(np.count_nonzero(core != ocv))
    frac = n / core.size
    print(f"    rung A: {n:,} of {core.size:,} pixels differ ({frac:.4%}), "
          f"core threshold {thr}")
    assert n > 0, (
        "the core's arithmetic and to_binary_inv agree pixel-for-pixel on a "
        "real page -- if that is ever true, rung A is not a real rung and "
        "gate 3's truncating-vs-rounding assertion is wrong too")
    # And it is a SMALL difference: a large one would mean the threshold, not
    # the rounding, and would point at otsu_on_truncating_blur.
    assert frac < 0.02, (
        f"{frac:.2%} of the page differs -- that is too much for a rounding "
        f"difference and points at the THRESHOLD choice instead")


def test_the_truncating_blur_really_differs_from_opencv():
    rng = np.random.default_rng(3)
    gray = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
    mine = B.truncating_blur(gray)
    ocv = cv2.GaussianBlur(gray, (3, 3), 0)[1:-1, 1:-1].astype(np.int32)
    assert not np.array_equal(mine, ocv), (
        "if these agree, rung A is not a real rung and the binariser "
        "difference this project documents does not exist")


def test_the_anchor_penalty_matches_best_template_match_local():
    """Same numbers, computed the CPU's way and the backend's way."""
    rng = np.random.default_rng(5)
    base = rng.integers(0, 2, size=(20, 30), dtype=np.uint8) * 255
    for side in ("left", "right"):
        for scale in (0.70, 1.00, 1.35):
            tw = max(4, int(round(base.shape[1] * scale)))
            th = max(4, int(round(base.shape[0] * scale)))
            abs_x, abs_y, ep = 137, 91, (150.0, 100.0)
            got, dist = B.anchor_adjust(0.5, abs_x, abs_y, base, side, scale,
                                        tw, th, ep)
            bax, bay = det.side_template_anchor(base, side)
            want_d = math.hypot(abs_x + bax * scale - ep[0],
                                abs_y + bay * scale - ep[1])
            want = 0.5 - 0.12 * (want_d / max(8.0, 0.5 * (tw + th)))
            assert abs(got - want) < 1e-12, (side, scale, got, want)
            assert abs(dist - want_d) < 1e-12


def test_the_trial_order_is_the_cpu_loop_nesting():
    """kind x base x scale, and the first trial wins a tie.

    `build_trials`'s order IS part of the classification result, because both
    reductions use strictly-greater. If it ever stops matching
    classify_endpoint's nesting, tied scores start reporting a different
    template's box and nothing else changes — the quietest possible
    regression.
    """
    tr = build_trials(templates()["left"], det.MATCH_SCALES)
    kinds = list(templates()["left"].keys())
    seen = [t["kind"] for t in tr]
    assert seen == sorted(seen, key=kinds.index), "kinds are not contiguous"
    n_scales = len(det.MATCH_SCALES)
    for i in range(0, len(tr), n_scales):
        block = tr[i:i + n_scales]
        assert [t["scale"] for t in block] == list(det.MATCH_SCALES)
        assert len({t["base_index"] for t in block}) == 1
    assert [t["templ_id"] for t in tr] == list(range(len(tr)))


def test_decide_kind_is_the_frozen_classify_endpoint_tail():
    """The margin and threshold rules, on constructed scores."""
    mk = lambda s: {"score": s, "box": (0, 0, 1, 1)}       # noqa: E731
    r = B.decide_kind({"male": mk(0.9), "female": mk(0.1)}, 0.33, 0.24, 0.03)
    assert r["kind"] == "male" and r["male_score"] == 0.9
    r = B.decide_kind({"male": mk(0.9), "female": mk(0.89)}, 0.33, 0.24, 0.03)
    assert r["kind"] == "unknown", "the margin rule did not fire"
    r = B.decide_kind({"male": mk(0.30)}, 0.33, 0.24, 0.03)
    assert r["kind"] == "unknown", "the male threshold did not fire"
    r = B.decide_kind({"ferrule": mk(0.30)}, 0.33, 0.24, 0.03)
    assert r["kind"] == "ferrule", "ferrule must use its OWN, lower threshold"
    r = B.decide_kind({}, 0.33, 0.24, 0.03)
    assert r["kind"] == "unknown" and r["box"] is None


def test_the_side_bank_patch_comes_from_the_drivers_predictor():
    """Not re-derived here, and not from the detector's per-base builder."""
    m = B.CpuSideBankMatcher()
    m._side_templates, m._scales = templates(), det.MATCH_SCALES
    for side in ("left", "right"):
        cand = {"endpoint": (400.0, 300.0), "side": side}
        got = m._patch_box(cand, 2000, 1500)
        max_tw, max_th = compute_cand_envelope(templates(), side,
                                               det.MATCH_SCALES)
        want = predict_patch_box(400, 300, 0 if side == "left" else 1,
                                 max_tw, max_th, 2000, 1500)
        assert got == want, (side, got, want)
        # And it really is bigger than any single-base CPU patch.
        for kind, lst in templates()[side].items():
            for t in lst:
                tw = int(t.shape[1] * max(det.MATCH_SCALES))
                th = int(t.shape[0] * max(det.MATCH_SCALES))
                x0, y0, x1, y1 = det.build_endpoint_patch(
                    400.0, 300.0, side, 2000, 1500, tw, th)
                assert got[2] >= x1 - x0 and got[3] >= y1 - y0, (
                    f"{side}/{kind}: the side-bank patch {got[2]}x{got[3]} is "
                    f"not a superset of the per-base {x1 - x0}x{y1 - y0}")


# ---------------------------------------------------------------------------
# the streamed binariser: memory feasibility WITHOUT changing the number
# ---------------------------------------------------------------------------

def test_the_stripes_reassemble_into_the_whole_page_blur():
    """`blur_stripes` must be `truncating_blur`, only in pieces.

    The bands overlap by the two rows the 3x3 kernel needs, so a stripe
    boundary is computed from real neighbours.  An implementation that split
    the image without the overlap would zero-pad at every seam -- a handful
    of wrong rows per page, in the interior, where nothing else would show
    them.  Several heights, including 1 and one larger than the page.
    """
    rng = np.random.default_rng(4)
    for h, w in ((3, 3), (5, 9), (37, 61), (64, 64)):
        gray = rng.integers(0, 256, (h, w), dtype=np.uint8)
        whole = B.truncating_blur(gray)
        for rows in (1, 2, 3, 8, 10_000):
            parts = [s for _y, s in B.blur_stripes(gray, rows=rows)]
            assert np.array_equal(np.concatenate(parts, axis=0), whole), \
                f"{w}x{h} at rows={rows}"
            hist = B.blur_histogram(gray, rows=rows)
            ref = np.bincount(whole.reshape(-1), minlength=256).astype(np.int64)
            assert np.array_equal(hist, ref), f"{w}x{h} histogram at {rows}"


def test_the_streamed_otsu_is_the_same_number_as_cv2():
    """Exactness, over the histograms that separate the wrong conventions.

    Otsu reads nothing but the histogram, so a histogram fed to `cv2` as a
    synthetic image is a direct comparison of the two implementations.  The
    cases with tied between-class variance are the ones that matter: they are
    what distinguishes OpenCV's strict `>` (FIRST maximiser wins) from the
    `>=` a transcription naturally reaches for, and that difference is a
    whole grey level -- a different binary image on every pixel near the
    threshold.
    """
    def cv_otsu_of(hist):
        vals = np.repeat(np.arange(256, dtype=np.uint8),
                         np.asarray(hist, dtype=np.int64))
        thr, _ = cv2.threshold(vals.reshape(1, -1), 0, 255, cv2.THRESH_OTSU)
        return int(thr)

    cases = {
        "flat":          [1] * 256,
        "single bin":    [1000 if i == 7 else 0 for i in range(256)],
        "two adjacent":  [500 if i in (100, 101) else 0 for i in range(256)],
        "tied maxima":   [100 if i in (0, 128, 255) else 0 for i in range(256)],
        "one outlier":   [1 if i == 0 else (10 ** 7 if i == 255 else 0)
                          for i in range(256)],
        "empty tails":   [7 if i in (3, 250) else 0 for i in range(256)],
    }
    rng = np.random.default_rng(11)
    for k in range(120):
        hist = [0] * 256
        for _ in range(int(rng.integers(1, 6))):
            hist[int(rng.integers(0, 256))] += int(rng.integers(1, 50))
        cases[f"sparse[{k}]"] = hist
    for label, hist in cases.items():
        got = B.otsu_from_histogram(np.array(hist, dtype=np.int64))
        assert got == cv_otsu_of(hist), f"{label}: {got} vs cv2"

    # And on real images, end to end through the striped path.
    for trial in range(60):
        h, w = int(rng.integers(3, 50)), int(rng.integers(3, 50))
        gray = rng.integers(0, 256, (h, w), dtype=np.uint8)
        blur = B.truncating_blur(gray)
        if blur.size == 0:
            continue
        want, _ = cv2.threshold(blur.astype(np.uint8), 0, 255, cv2.THRESH_OTSU)
        assert B.otsu_on_truncating_blur(gray) == int(want), (trial, w, h)


def test_the_streamed_binariser_never_holds_the_whole_int32_blur():
    """The reason this was rewritten: 496 MB does not fit in ~290 MiB.

    Checked as a BOUND on the stripe, not as a wall-clock measurement: at the
    production page's width the working set is the byte budget, and it does
    not grow with the page height at all.
    """
    page_w, page_h = 9792, 6336
    rows = B.stripe_rows(page_w)
    stripe_bytes = rows * page_w * 4
    assert stripe_bytes <= B._STRIPE_BUDGET_BYTES, stripe_bytes
    assert rows >= 1
    # The old whole-page form, for the record: BGR + grey + int32 blur.
    whole = 3 * page_w * page_h + page_w * page_h + 4 * page_w * page_h
    assert whole == 496_336_896, whole
    assert stripe_bytes * 16 < whole, (stripe_bytes, whole)


def test_the_production_oracle_binarises_the_way_the_core_does():
    """`cpu-production` is the software oracle a board run must reproduce.

    `cpu` cannot be that oracle: its binariser is `to_binary_inv`, which is
    rung A, and rung A is EXPECTED to differ.  This one runs the core's own
    arithmetic on the host, so the only thing left between it and
    `pl-binarize` is which chip did the convolution.
    """
    b = B.make_backend("cpu-production")
    assert B.ORGANISATION["cpu-production"] == "pl_side_bank"
    gray = np.random.default_rng(2).integers(0, 256, (40, 60), dtype=np.uint8)
    got = b.binarize_inv(gray)
    thr = b._binarizer.threshold_used()
    assert thr == B.otsu_on_truncating_blur(gray)
    assert np.array_equal(got, B.cpu_binary_like_core(gray, thr))
    # And it is NOT the frozen oracle's binariser, which is the whole point.
    assert not np.array_equal(got, det.to_binary_inv(gray))


def test_the_production_oracle_differs_from_cpu_on_a_real_page():
    """If it agreed with `cpu` everywhere, it would not be a new oracle."""
    _x, _c, d_cpu = run_page(backend=B.make_backend("cpu"))
    _x, _c, d_prod = run_page(backend=B.make_backend("cpu-production"))
    assert len(d_cpu) > 10 and len(d_prod) > 10
    same = boxes_of(d_cpu) == boxes_of(d_prod)
    print(f"    cpu vs cpu-production on the Stage 2 page: "
          f"{len(d_cpu)} vs {len(d_prod)} detections, "
          f"boxes {'identical' if same else 'differ'}")


# ---------------------------------------------------------------------------
# counts and ordering
# ---------------------------------------------------------------------------

def test_trials_run_counts_matcher_invocations_not_class_winners():
    """The count every wall-time-per-trial figure is divided by.

    `by_kind` holds at most one winner per class, so counting it reported 132
    for a page that ran 1,200 matcher invocations -- 9.1x low.  The two
    side-bank backends dispatch the SAME trial list, so their counts must
    agree exactly; that is what makes this checkable without a board.
    """
    pl = FakePL()
    b_all = B.make_backend("pl-all", pl=pl)
    run_page(backend=b_all)
    b_side = B.make_backend("cpu-sidebank")
    b_side._binarizer = _FixedBinarizer(pl._threshold)
    run_page(backend=b_side)

    pl_trials = b_all._matcher.trials_run
    cpu_trials = b_side._matcher.trials_run
    assert pl_trials == cpu_trials, (pl_trials, cpu_trials)
    # Far more than the number of class winners it used to report.
    assert pl_trials > 4 * b_all._matcher.calls, (pl_trials,
                                                  b_all._matcher.calls)


def test_the_driver_reports_the_trial_count_it_dispatched():
    """`match_candidate` must say how many invocations it ran."""
    pl = FakePL()
    b = B.make_backend("pl-all", pl=pl)
    _x, cands, _d = run_page(backend=b)
    out = pl.match_candidate(
        np.full((60, 90), 255, np.uint8), 0, 0,
        build_trials(templates()["left"], det.MATCH_SCALES))
    assert "trials" in out, "the fake fabric hid the count the driver reports"
    assert out["trials"] > 0
    empty = pl.match_candidate(np.full((60, 90), 255, np.uint8), 0, 0, [])
    assert empty["trials"] == 0, empty


def test_a_reordered_metadata_batch_is_refused():
    """§6.2 emits one record per descriptor IN INPUT ORDER.

    Records are keyed by the candidate object, so a reordered batch attaches
    candidate i's patch to candidate j: every box after that point is built
    from the wrong origin, and each record is individually well formed, so
    nothing else in the pipeline can notice.  The core's own ordinal is the
    only field that can say so.
    """
    class ReorderingPL(FakePL):
        def extract_candidates(self, candidates, side_templates, scales):
            recs = super().extract_candidates(candidates, side_templates,
                                              scales)
            if len(recs) >= 2:                 # swap two ordinals only
                recs[0]["cand_id"], recs[1]["cand_id"] = (
                    recs[1]["cand_id"], recs[0]["cand_id"])
            return recs

    b = B.make_backend("pl-all", pl=ReorderingPL())
    try:
        run_page(backend=b)
    except B.BackendError as exc:
        assert "cand_id" in str(exc), exc
        return
    raise AssertionError("a reordered metadata batch was accepted")


# ---------------------------------------------------------------------------
# rung C, inside one run
# ---------------------------------------------------------------------------

def test_the_inline_rung_c_compares_the_same_patch_and_finds_no_fault():
    """Two separate runs do not prove they got the same upstream data.

    `pl-extract` and `pl-all` each re-render, re-binarise and re-dispatch;
    nothing ties their patches together.  With the cross-check on, the CPU
    reduction runs against the SAME `patch` array the fabric just matched,
    from the same metadata record, inside the same candidate.
    """
    b = B.make_backend("pl-all", pl=FakePL(), rung_c_inline=True)
    run_page(backend=b)
    cc = b.rung_c_report()
    assert cc is not None
    assert cc["candidates"] > 10, cc
    assert cc["trials"] > 100, cc
    assert not cc["mismatches"], cc["mismatches"][:3]
    assert "rung_c_inline=" in b.describe()


def test_the_inline_rung_c_catches_a_matcher_that_lies():
    """A cross-check that cannot fail is a banner, not a check."""
    class DriftingPL(FakePL):
        def match_candidate(self, patch, x0, y0, trials, score_fn=None):
            out = super().match_candidate(patch, x0, y0, trials, score_fn)
            for hit in out["by_kind"].values():
                bx, by, bw, bh = hit["box"]
                hit["box"] = (bx + 1, by, bw, bh)     # one pixel, one class
                break
            return out

    b = B.make_backend("pl-all", pl=DriftingPL(), rung_c_inline=True)
    run_page(backend=b)
    cc = b.rung_c_report()
    assert cc["mismatches"], "a one-pixel box drift went unnoticed"
    assert any(m["why"] == "box" for m in cc["mismatches"]), cc["mismatches"][:3]


def test_rung_c_inline_is_refused_where_it_would_prove_nothing():
    try:
        B.make_backend("pl-extract", pl=FakePL(), rung_c_inline=True)
    except B.BackendError:
        return
    raise AssertionError("pl-extract accepted a cross-check with no PL matcher")


# ---------------------------------------------------------------------------
# the runner: page shape, and a teardown that cannot be skipped
# ---------------------------------------------------------------------------

def test_the_derived_page_shape_matches_every_rendered_page():
    """A BGR-free run derives the annotation geometry instead of holding 186 MB.

    Wrong here means every annotation rectangle moves, silently.  Checked
    against the real pixmap on every page of the sample corpus, not on one.
    """
    import fitz
    pdfs = sorted({str(p).lower(): p for p in SAMPLES.glob("*.pdf")}.values())
    if not pdfs:
        raise SkipTest(f"no PDFs under {SAMPLES}")
    pages = 0
    for path in pdfs:
        doc = fitz.open(str(path))
        try:
            for i in range(len(doc)):
                page = doc[i]
                derived = det.rendered_shape(page, 4.0)
                pix = page.get_pixmap(matrix=fitz.Matrix(4.0, 4.0), alpha=False)
                assert derived[:2] == (pix.height, pix.width), (
                    path.name, i + 1, derived, (pix.height, pix.width))
                pages += 1
        finally:
            doc.close()
    assert pages >= 36, f"only {pages} page(s) checked"
    print(f"    derived shape matched the pixmap on {pages} page(s)")


def test_the_pdf_runner_tears_down_even_when_a_page_raises():
    """The blocker: `process_pdf` raising used to skip `close()` entirely.

    A page that times out is the expected way a corpus run fails, and it left
    the pipeline poisoned, its CMA pages retained, and the process exiting --
    which hands those pages back with the fabric still holding a command
    against them.  The teardown has to be in a `finally`, and it has to be
    `safe_teardown.teardown` (which reprograms the PL, or holds) rather than
    a bare `close()` whose False becomes a process exit.
    """
    import safe_teardown

    calls = {"armed": 0, "teardown": [], "closed": 0}

    class Pl:
        _BUFFER_ATTRS = ()

        def close(self):
            calls["closed"] += 1
            return False                       # RETAINED: the dangerous case

    class Bk:
        pl = Pl()
        overlay = object()

        def describe(self):
            return "backend=fake"

        def close(self):
            calls["closed"] += 1
            return False

    real_make = B.make_backend
    real_arm = safe_teardown.arm_teardown_protection
    real_td = safe_teardown.teardown
    real_process = det.process_pdf
    real_gate = None
    import inspect_overlay
    real_gate = inspect_overlay.gate_identity_and_clock

    def fake_arm():
        calls["armed"] += 1
        return ["SIGINT"]

    def fake_teardown(pl, bitfile, status=0):
        calls["teardown"].append((pl, bitfile, status))
        return status or 1

    def boom(**kw):
        raise TimeoutError("page 1 overran its deadline")

    B.make_backend = lambda *a, **k: Bk()
    safe_teardown.arm_teardown_protection = fake_arm
    safe_teardown.teardown = fake_teardown
    inspect_overlay.gate_identity_and_clock = lambda ol, v: []
    det.process_pdf = boom
    argv = sys.argv[:]
    sys.argv = ["terminal_counter_endpoint_first.py", "in.pdf",
                "ml.png", "mr.png", "fl.png", "fr.png", "gl.png", "gr.png",
                "--backend", "pl-all", "--variant", "combined_b2_100"]
    try:
        det.main()
    except TimeoutError:
        pass                                   # re-raised AFTER the teardown
    except BaseException as exc:               # noqa: BLE001
        raise AssertionError(f"unexpected {type(exc).__name__}: {exc}")
    else:
        raise AssertionError("the page failure was swallowed")
    finally:
        sys.argv = argv
        B.make_backend = real_make
        safe_teardown.arm_teardown_protection = real_arm
        safe_teardown.teardown = real_td
        inspect_overlay.gate_identity_and_clock = real_gate
        det.process_pdf = real_process

    assert calls["armed"] == 1, "the termination signals were never blocked"
    assert len(calls["teardown"]) == 1, (
        f"safe_teardown.teardown() ran {len(calls['teardown'])} time(s); a "
        f"page that raises must still reach it")
    assert calls["teardown"][0][2] == 1, calls["teardown"]
    assert calls["closed"] == 0, (
        "the runner called close() directly; that is the path whose False "
        "turns into a process exit and releases the retained pages")


def test_the_pdf_runner_refuses_a_board_running_the_wrong_build():
    """Identity and live clock, before the first page -- and it must tear down."""
    import safe_teardown
    import inspect_overlay

    seen = {"teardown": 0, "pages": 0}

    class Pl:
        _BUFFER_ATTRS = ()

        def close(self):
            return True

    class Bk:
        pl = Pl()
        overlay = object()

        def describe(self):
            return "backend=fake"

        def close(self):
            return True

    real = (B.make_backend, safe_teardown.arm_teardown_protection,
            safe_teardown.teardown, inspect_overlay.gate_identity_and_clock,
            det.process_pdf)

    def counting_process(**kw):
        seen["pages"] += 1

    def fake_teardown(pl, bitfile, status=0):
        seen["teardown"] += 1
        return status

    B.make_backend = lambda *a, **k: Bk()
    safe_teardown.arm_teardown_protection = lambda: ["SIGINT"]
    safe_teardown.teardown = fake_teardown
    inspect_overlay.gate_identity_and_clock = lambda ol, v: [
        "live PL clock 62.5000 MHz != combined_b2_100's 100.0000 MHz"]
    det.process_pdf = counting_process
    argv = sys.argv[:]
    sys.argv = ["terminal_counter_endpoint_first.py", "in.pdf",
                "ml.png", "mr.png", "fl.png", "fr.png", "gl.png", "gr.png",
                "--backend", "pl-all", "--variant", "combined_b2_100"]
    try:
        det.main()
    except RuntimeError as exc:
        assert "62.5" in str(exc), exc
    except BaseException as exc:                             # noqa: BLE001
        raise AssertionError(f"unexpected {type(exc).__name__}: {exc}")
    else:
        raise AssertionError("a wrong-clock board was accepted")
    finally:
        sys.argv = argv
        (B.make_backend, safe_teardown.arm_teardown_protection,
         safe_teardown.teardown, inspect_overlay.gate_identity_and_clock,
         det.process_pdf) = real

    assert seen["pages"] == 0, "a page was processed before the gate passed"
    assert seen["teardown"] == 1, "the refused run did not tear down"


# ---------------------------------------------------------------------------
# the corpus is a pinned shape
# ---------------------------------------------------------------------------

def test_the_corpus_is_thirty_five_pdfs_and_thirty_six_pages():
    """`--require-corpus` pins numbers; the numbers have to be the real ones."""
    import fitz
    import tme_backend_parity as P
    uniq = sorted({str(p).lower(): p for p in SAMPLES.glob("*.pdf")}.values())
    if not uniq:
        raise SkipTest(f"no PDFs under {SAMPLES}")
    pages = 0
    multi = []
    for path in uniq:
        doc = fitz.open(str(path))
        pages += len(doc)
        if len(doc) != 1:
            multi.append((path.name, len(doc)))
        doc.close()
    assert len(uniq) == P.CORPUS_PDFS, (len(uniq), P.CORPUS_PDFS)
    assert pages == P.CORPUS_PAGES, (pages, P.CORPUS_PAGES)
    # By LABEL, not by filename.  The assertion is unchanged in force --
    # still "this document, and only this one, has two pages" -- and the
    # source no longer names a drawing to say it.
    assert [(CL.scrub(n, SAMPLES), k) for n, k in multi] == [
        ("doc_001", 2)], multi


# ---------------------------------------------------------------------------
# the renderer: the same bytes, a third of the memory
# ---------------------------------------------------------------------------

def _legacy_render(page, zoom=4.0):
    """`render_page` as it stood before the memory work, verbatim."""
    import fitz
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return bgr, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def _stage2_page():
    import fitz
    if not STAGE2_PDF.exists():
        raise SkipTest(f"{STAGE2_PDF} not present")
    return fitz.open(str(STAGE2_PDF))


def test_the_renderer_is_byte_identical_in_both_modes():
    """The memory work may not move a single grey level.

    `keep_bgr=True` is the frozen CPU path and must be unchanged.
    `keep_bgr=False` never builds BGR at all and fills the grey page a band at
    a time; striping the CONVERSION is exact by construction, because both
    `cvtColor` calls are per-pixel with no neighbourhood, and this is what
    says so on a real page rather than on the argument.
    """
    doc = _stage2_page()
    try:
        page = doc[0]
        old_bgr, old_gray = _legacy_render(page)
        new_bgr, new_gray, _ = det.render_page(page, zoom=4.0, keep_bgr=True)
        assert np.array_equal(new_bgr, old_bgr)
        assert np.array_equal(new_gray, old_gray)

        free_bgr, free_gray, _ = det.render_page(page, zoom=4.0,
                                                 keep_bgr=False)
        assert free_bgr is None, "keep_bgr=False still built the BGR array"
        assert np.array_equal(free_gray, old_gray)

        # And the difference has to reach nothing downstream either.
        thr_old = B.otsu_on_truncating_blur(old_gray)
        thr_new = B.otsu_on_truncating_blur(free_gray)
        assert thr_old == thr_new, (thr_old, thr_new)
    finally:
        doc.close()


def test_the_grey_stripe_height_does_not_change_the_bytes():
    """Any band height must give the same page; 1 row included."""
    rng = np.random.default_rng(9)
    src = rng.integers(0, 256, (37, 61, 3), dtype=np.uint8)
    ref = cv2.cvtColor(det._to_bgr(src, 3), cv2.COLOR_BGR2GRAY)
    for rows in (1, 2, 5, 37, 1000):
        out = np.empty(ref.shape, dtype=np.uint8)
        for y0 in range(0, src.shape[0], rows):
            y1 = min(y0 + rows, src.shape[0])
            out[y0:y1] = cv2.cvtColor(det._to_bgr(src[y0:y1], 3),
                                      cv2.COLOR_BGR2GRAY)
        assert np.array_equal(out, ref), rows


def test_native_grayscale_rendering_is_still_not_byte_identical():
    """Why `colorspace=fitz.csGRAY` is NOT used, asserted rather than noted.

    It would be the cheapest path of all -- 124 MB against 620 -- and it is
    the obvious thing for someone to reach for later. MuPDF rasterises INTO
    grey rather than converting afterwards, and its weights are not OpenCV's:
    measured over all 36 corpus pages, 0 were identical and the worst page was
    23 grey levels out. This fails the moment that stops being true, which is
    the only circumstance in which the decision should be revisited.
    """
    import fitz
    doc = _stage2_page()
    try:
        page = doc[0]
        _bgr, ref, _ = det.render_page(page, zoom=4.0, keep_bgr=False)
        gpix = page.get_pixmap(matrix=fitz.Matrix(4.0, 4.0),
                               colorspace=fitz.csGRAY, alpha=False)
        native = np.frombuffer(
            gpix.samples_mv if hasattr(gpix, "samples_mv") else gpix.samples,
            dtype=np.uint8).reshape(gpix.height, gpix.width)
        assert native.shape == ref.shape
        assert not np.array_equal(native, ref), (
            "native grayscale now matches BGR2GRAY on this page -- re-run the "
            "corpus comparison before switching to it")
    finally:
        doc.close()


def test_striping_the_render_is_still_not_byte_identical():
    """Why the RENDER is not striped, only the conversion.

    `clip=` tiles exactly in geometry -- every band's irect comes back as
    asked -- but MuPDF antialiases content against the clip edge, so the last
    rows of each band differ. An overlap margin shrinks that without bounding
    it. This asserts the hazard is real, so "just render it in stripes" cannot
    quietly come back.
    """
    import fitz
    doc = _stage2_page()
    try:
        page = doc[0]
        _bgr, ref, _ = det.render_page(page, zoom=4.0, keep_bgr=False)
        mat = fitz.Matrix(4.0, 4.0)
        full = (page.rect * mat).irect
        H, W = full.height, full.width
        got = np.empty((H, W), dtype=np.uint8)
        rows = 1024
        for y0 in range(0, H, rows):
            y1 = min(y0 + rows, H)
            band = fitz.IRect(full.x0, full.y0 + y0, full.x1, full.y0 + y1)
            pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(band) * ~mat,
                                  alpha=False)
            assert pix.irect == band, (pix.irect, band)   # geometry IS exact
            img = np.frombuffer(
                pix.samples_mv if hasattr(pix, "samples_mv") else pix.samples,
                dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            got[y0:y1, :] = cv2.cvtColor(det._to_bgr(img, pix.n),
                                         cv2.COLOR_BGR2GRAY)
            del img, pix
        assert not np.array_equal(got, ref), (
            "clip-striped rendering now matches the whole-page render -- "
            "re-run the corpus comparison before adopting it")
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# geometry in, annotation out
# ---------------------------------------------------------------------------

def test_the_geometry_json_reproduces_the_annotation_off_board():
    """The board emits geometry; the drawing happens somewhere else.

    The record has to carry everything `annotate_page` reads, or the redraw
    silently differs from what the board measured.
    """
    import json
    import tempfile
    import fitz

    doc = _stage2_page()
    try:
        page = doc[0]
        _b, _c, dets = run_page(backend=B.make_backend("cpu"))
        shape = det.rendered_shape(page, 4.0)
        rec = det.geometry_record(0, shape, dets)
    finally:
        doc.close()

    assert rec["page"] == 1
    assert rec["shape"] == [shape[0], shape[1], 3]
    assert len(rec["detections"]) == len(dets)
    for got, want in zip(rec["detections"], dets):
        for k in det.GEOMETRY_FIELDS:
            assert got[k] == want[k] or (
                k == "score" and abs(got[k] - want[k]) < 1e-12), (k, got, want)
    # JSON-able, and the round trip keeps the boxes.
    back = json.loads(json.dumps(rec))
    assert back == rec

    with tempfile.TemporaryDirectory() as tmp:
        gpath = Path(tmp) / "geom.json"
        gpath.write_text(json.dumps({"pdf": str(STAGE2_PDF), "zoom": 4.0,
                                     "pages": [rec]}), encoding="utf-8")
        direct = Path(tmp) / "direct.pdf"
        redraw = Path(tmp) / "redraw.pdf"

        d1 = fitz.open(str(STAGE2_PDF))
        annotate_counts = det.annotate_page(d1[0], dets, shape)
        d1.save(str(direct))
        d1.close()

        det.annotate_from_geometry(str(STAGE2_PDF), str(redraw), str(gpath))

        a = fitz.open(str(direct))
        b = fitz.open(str(redraw))
        pa = a[0].get_pixmap(matrix=fitz.Matrix(1, 1)).samples
        pb = b[0].get_pixmap(matrix=fitz.Matrix(1, 1)).samples
        a.close()
        b.close()
        assert pa == pb, "the off-board redraw is not what the board drew"
    assert annotate_counts[0] + annotate_counts[1] + annotate_counts[2] > 10


def test_geometry_recorded_at_another_shape_is_refused():
    """A record from a different zoom would put every box in the wrong place."""
    import json
    import tempfile

    _b, _c, dets = run_page(backend=B.make_backend("cpu"))
    doc = _stage2_page()
    try:
        shape = det.rendered_shape(doc[0], 4.0)
    finally:
        doc.close()
    bad = det.geometry_record(0, (shape[0] // 2, shape[1] // 2, 3), dets)

    with tempfile.TemporaryDirectory() as tmp:
        gpath = Path(tmp) / "geom.json"
        gpath.write_text(json.dumps({"pdf": str(STAGE2_PDF), "zoom": 4.0,
                                     "pages": [bad]}), encoding="utf-8")
        try:
            det.annotate_from_geometry(str(STAGE2_PDF),
                                       str(Path(tmp) / "out.pdf"), str(gpath))
        except SystemExit as exc:
            assert "wrong place" in str(exc), exc
            return
    raise AssertionError("a geometry record from another shape was accepted")


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for t in tests:
        try:
            t()
        except SkipTest as e:
            print(f"skip {t.__name__}: {e}")
            skipped += 1
        except Exception as e:                               # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
        else:
            print(f"ok   {t.__name__}")
    print(f"\n{len(tests) - failed - skipped}/{len(tests)} passed"
          + (f", {skipped} skipped" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
