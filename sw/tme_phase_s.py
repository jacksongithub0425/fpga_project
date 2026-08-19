#!/usr/bin/env python3
"""Priority 3 - Phase S prototype: a 96x64 result search area, cropped on the PS.

Phase S does NOT change the extractor.  The compiled MAX_PATCH stays 820x307 and
the patch the PL delivers is unchanged; the PS crops a sub-rectangle out of the
patch it already has, and only that sub-rectangle is correlated.  So this is a
DRIVER change, not an ABI change, and it can be prototyped entirely in software
before any RTL moves.

WHY A CROP IS EXACT, AND WHAT THAT DOES NOT PROMISE
---------------------------------------------------
TM_CCOEFF_NORMED at result position (x, y) is a function of the template and of
the (th x tw) image window at (x, y) alone.  Nothing outside that window enters
the value.  Therefore the score map of a cropped patch is EXACTLY the
corresponding sub-rectangle of the score map of the full patch - there is no
border effect to reason about, because no padding is introduced.  --selftest
proves this on synthetic data and a corpus run re-proves it on all 20,680 real
trials by computing both maps and differencing them.

What the crop does change is the ARGMAX: a maximum that lay outside the crop is
no longer reachable.  That is a policy change, not an implementation error, and
it is the thing this tool measures rather than assumes.

THE ROI PLACEMENT RULE
----------------------
The crop is centred on the result position that would put the template's anchor
exactly on the endpoint - the position the detector's own distance penalty is
built around:

    anchor_x = tw - 1 (left) or 0 (right)      anchor_y = th // 2
    ideal    = (endpoint_i - patch_origin) - anchor
    roi_x0   = clamp(ideal_x - roi_w//2, 0, rw_full - roi_w)
    roi_y0   = clamp(ideal_y - roi_h//2, 0, rh_full - roi_h)

This uses the RESIZED template's integer anchor, not the detector's
`base_anchor * scale`, and it uses int(round(endpoint)), the same integer
endpoint build_endpoint_patch and patch_extract_core use.  Both choices are
deliberate: the ROI rule only decides WHERE to look, so it may be clean integer
arithmetic, while the scoring and penalty arithmetic downstream is untouched and
still runs on the returned global location.  The two anchors differ by well
under a pixel and cannot affect parity except through the crop bound itself.

THE int() TRUNCATION FIX
------------------------
best_template_match_local sizes the patch envelope with

    max_tw = int(template.shape[1] * max(scales))          <- truncates

while the template it actually resizes to at that same scale is

    tw = max(4, int(round(template.shape[1] * scale)))     <- rounds

so at max(scales) the envelope can be one pixel smaller than the template it is
supposed to contain.  --fix-truncation switches the envelope to the rounded
geometry.  It is a SEPARATE knob from the crop because they are separate
changes, and confounding them would make a parity result uninterpretable.

USAGE
-----
    python tme_phase_s.py --selftest
    python tme_phase_s.py --truncation-report
    python tme_phase_s.py "../../sample/*" --control
    python tme_phase_s.py "../../sample/*" --phase-s
    python tme_phase_s.py "../../sample/*" --phase-s --fix-truncation
    python tme_phase_s.py "../../sample/*" --phase-s --roi 128x96

Run with the HLS venv python.
"""

from __future__ import annotations

import argparse
import csv
import glob as globmod
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import cv2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tme_cycle_model as model                        # noqa: E402

ROI_W, ROI_H = 96, 64
ENVELOPE_SCALE = 1.50
ANCHOR_WEIGHT = 0.12
SCORE_THRESH = 0.33
FERRULE_SCORE_THRESH = 0.24
SCORE_MARGIN = 0.03

# Sentinel written outside the ROI.  TM_CCOEFF_NORMED is bounded to [-1, 1] and
# the refinement path subtracts a penalty of at most ~0.9 from it, so any value
# below -2 would already lose to every real score; -1000 leaves no room to argue
# about the margin.
OUTSIDE = -1000.0


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------
def phase_s_roi(px0, py0, pw, ph, tw, th, endpoint_x, endpoint_y, side,
                roi_w=ROI_W, roi_h=ROI_H):
    """Result-space ROI for one trial.  Pure integer arithmetic, PS-reproducible.

    Returns (x0, y0, w, h) in RESULT coordinates of the full patch, where
    (w, h) is (roi_w, roi_h) clipped down when the result map is smaller than
    the ROI.
    """
    rw_full = pw - tw + 1
    rh_full = ph - th + 1
    if rw_full <= 0 or rh_full <= 0:
        raise ValueError("template does not fit the patch: "
                         "{}x{} in {}x{}".format(tw, th, pw, ph))
    w = min(roi_w, rw_full)
    h = min(roi_h, rh_full)

    exi = int(round(endpoint_x))
    eyi = int(round(endpoint_y))
    anchor_x = (tw - 1) if side == "left" else 0
    anchor_y = th // 2

    x0 = (exi - px0) - anchor_x - (w // 2)
    y0 = (eyi - py0) - anchor_y - (h // 2)
    x0 = max(0, min(x0, rw_full - w))
    y0 = max(0, min(y0, rh_full - h))
    return x0, y0, w, h


def crop_bounds(roi, tw, th):
    """PATCH-space slice a result ROI needs: (x0, y0, crop_w, crop_h)."""
    x0, y0, w, h = roi
    return x0, y0, w + tw - 1, h + th - 1


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------
class PhaseS:
    """Runs the full and the cropped correlation for every trial and compares.

    In crop mode the detector is handed a full-size map whose ROI holds the
    values computed FROM THE CROP and whose exterior is OUTSIDE.  That is
    deliberate: it exercises the crop and the local->global conversion (the
    cropped values must be written back at the right offset for the detector's
    coordinates to come out right) while leaving the detector's own argmax, tie
    rule, penalty and ranking code completely untouched.
    """

    def __init__(self, phase_s=True, fix_truncation=False, roi=(ROI_W, ROI_H)):
        self.phase_s = phase_s
        self.fix_truncation = fix_truncation
        self.roi_w, self.roi_h = roi
        self.ctx = None
        self.stats = defaultdict(int)
        self.max_crop_delta = 0.0
        self.worst_crop = None
        self.cycles = defaultdict(int)
        self.records = []
        self._det = None
        self._orig_btml = self._orig_bep = self._orig_mt = None

    # -- envelope ------------------------------------------------------------
    def _bep(self, ex, ey, side, img_w, img_h, max_tw, max_th):
        if self.ctx is not None and self.fix_truncation:
            max_tw, max_th = self.ctx["max_tw_fix"], self.ctx["max_th_fix"]
        return self._orig_bep(ex, ey, side, img_w, img_h, max_tw, max_th)

    # -- the scale loop ------------------------------------------------------
    def _btml(self, page_bin, template_bin, endpoint_xy, side, scales,
              anchor_distance_weight=ANCHOR_WEIGHT, prefer_local_alignment=False):
        bw, bh = int(template_bin.shape[1]), int(template_bin.shape[0])
        ms = max(scales)
        self.ctx = {
            "side": side,
            "endpoint": (float(endpoint_xy[0]), float(endpoint_xy[1])),
            "base": (bw, bh),
            "prefer": bool(prefer_local_alignment),
            "max_tw_fix": max(4, int(round(bw * ms))),
            "max_th_fix": max(4, int(round(bh * ms))),
        }
        # Recompute the patch origin the detector is about to use, so _mt can
        # convert local coordinates without reaching into the detector's frame.
        img_h, img_w = page_bin.shape[:2]
        if self.fix_truncation:
            mtw, mth = self.ctx["max_tw_fix"], self.ctx["max_th_fix"]
        else:
            mtw, mth = int(bw * ms), int(bh * ms)
        px0, py0, px1, py1 = self._orig_bep(endpoint_xy[0], endpoint_xy[1], side,
                                            img_w, img_h, mtw, mth)
        self.ctx.update(px0=px0, py0=py0, pw=px1 - px0, ph=py1 - py0)
        try:
            return self._orig_btml(
                page_bin, template_bin, endpoint_xy, side, scales,
                anchor_distance_weight=anchor_distance_weight,
                prefer_local_alignment=prefer_local_alignment)
        finally:
            self.ctx = None

    # -- the correlation -----------------------------------------------------
    def _mt(self, patch, templ, method):
        full = self._orig_mt(patch, templ, method)
        c = self.ctx
        if c is None:
            return full

        th, tw = int(templ.shape[0]), int(templ.shape[1])
        ph, pw = int(patch.shape[0]), int(patch.shape[1])
        kind = "refinement" if c["prefer"] else "initial"
        self.stats[(kind, "trials")] += 1

        # The geometry this wrapper predicted must be the geometry the detector
        # actually used, or every coordinate below is meaningless.
        if (pw, ph) != (c["pw"], c["ph"]):
            self.stats[(kind, "GEOMETRY MISMATCH")] += 1
            return full

        roi = phase_s_roi(c["px0"], c["py0"], pw, ph, tw, th,
                          c["endpoint"][0], c["endpoint"][1], c["side"],
                          self.roi_w, self.roi_h)
        x0, y0, w, h = roi
        cx0, cy0, cw, ch = crop_bounds(roi, tw, th)

        # THE CROP: a real sub-array of the patch, correlated on its own.
        sub = self._orig_mt(np.ascontiguousarray(patch[cy0:cy0 + ch, cx0:cx0 + cw]),
                            templ, method)

        # Crop contents / border behaviour: the cropped map must BE the
        # sub-rectangle of the full map, to within OpenCV's own arithmetic.
        if sub.shape != (h, w):
            self.stats[(kind, "CROP SHAPE WRONG")] += 1
            return full
        ref = full[y0:y0 + h, x0:x0 + w]
        delta = float(np.max(np.abs(sub.astype(np.float64) - ref.astype(np.float64))))
        if delta > self.max_crop_delta:
            self.max_crop_delta = delta
            self.worst_crop = dict(tw=tw, th=th, pw=pw, ph=ph, roi=roi, delta=delta)
        self.stats[(kind, "crop_exact")] += int(delta == 0.0)

        # local -> global: write the cropped values back at the ROI offset.
        out = np.full(full.shape, OUTSIDE, dtype=full.dtype)
        out[y0:y0 + h, x0:x0 + w] = sub

        _, o_val, _, o_loc = cv2.minMaxLoc(full)
        _, p_val, _, p_loc = cv2.minMaxLoc(out)
        inside = (x0 <= o_loc[0] < x0 + w) and (y0 <= o_loc[1] < y0 + h)
        self.stats[(kind, "oracle_argmax_in_roi")] += int(inside)
        self.stats[(kind, "same_loc")] += int(tuple(o_loc) == tuple(p_loc))
        self.stats[(kind, "same_score")] += int(float(o_val) == float(p_val))

        # Modelled cost of this trial under both policies.
        self.cycles[(kind, "current")] += model.cycles(pw, ph, tw, th)
        for variant in ("cur", "B1", "B2"):
            self.cycles[(kind, variant)] += model.cycles(tw + w - 1, th + h - 1,
                                                         tw, th, variant=variant)

        self.records.append({
            "kind": kind, "tw": tw, "th": th, "pw": pw, "ph": ph,
            "roi": [x0, y0, w, h],
            "oracle_loc": [int(o_loc[0]), int(o_loc[1])],
            "phase_s_loc": [int(p_loc[0]), int(p_loc[1])],
            "oracle_score": round(float(o_val), 6),
            "phase_s_score": round(float(p_val), 6),
            "crop_delta": delta, "inside": bool(inside),
        })
        return out if self.phase_s else full

    def __enter__(self):
        import terminal_counter_endpoint_first as det
        self._det = det
        self._orig_btml = det.best_template_match_local
        self._orig_bep = det.build_endpoint_patch
        self._orig_mt = cv2.matchTemplate
        det.best_template_match_local = self._btml
        det.build_endpoint_patch = self._bep
        cv2.matchTemplate = self._mt
        return self

    def __exit__(self, *exc):
        self._det.best_template_match_local = self._orig_btml
        self._det.build_endpoint_patch = self._orig_bep
        cv2.matchTemplate = self._orig_mt
        return False


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def selftest():
    """Prove the crop rule and the crop identity without touching the corpus."""
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [{}] {}{}".format("PASS" if cond else "FAIL", name,
                                   "  " + detail if detail else ""))

    print("ROI RULE")
    print("-" * 78)
    r = phase_s_roi(1000, 500, 400, 200, 52, 31, 1266.4, 592.0, "left")
    check("interior ROI is exactly {}x{}".format(ROI_W, ROI_H),
          r[2:] == (ROI_W, ROI_H), str(r))
    ideal_x = int(round(1266.4)) - 1000 - (52 - 1)
    check("interior ROI centred on the anchor-ideal x",
          r[0] == ideal_x - ROI_W // 2, "x0={} ideal={}".format(r[0], ideal_x))

    r = phase_s_roi(1000, 500, 400, 200, 52, 31, 1000.0, 500.0, "left")
    check("clamps to x0=0, y0=0 at the near border", r[0] == 0 and r[1] == 0, str(r))

    r = phase_s_roi(0, 0, 400, 200, 52, 31, 10000.0, 10000.0, "right")
    check("clamps to the far border",
          r[0] == 400 - 52 + 1 - ROI_W and r[1] == 200 - 31 + 1 - ROI_H, str(r))

    r = phase_s_roi(0, 0, 100, 40, 52, 31, 50.0, 20.0, "left")
    check("shrinks to the full map when the map is smaller than the ROI",
          r == (0, 0, 100 - 52 + 1, 40 - 31 + 1), str(r))

    rl = phase_s_roi(0, 0, 400, 200, 52, 31, 200.0, 100.0, "left")
    rr = phase_s_roi(0, 0, 400, 200, 52, 31, 200.0, 100.0, "right")
    check("left/right ROIs differ by exactly tw-1", rr[0] - rl[0] == 52 - 1,
          "{} vs {}".format(rl[0], rr[0]))

    r = phase_s_roi(0, 0, 52, 31, 52, 31, 0.0, 0.0, "left")
    check("degenerate 1x1 result map", r == (0, 0, 1, 1), str(r))

    rng = np.random.default_rng(20260818)
    inb = True
    for _ in range(20000):
        tw = int(rng.integers(4, 220))
        th = int(rng.integers(4, 160))
        pw = tw + int(rng.integers(0, 700))
        ph = th + int(rng.integers(0, 400))
        side = "left" if rng.integers(0, 2) else "right"
        px0 = int(rng.integers(0, 5000))
        py0 = int(rng.integers(0, 5000))
        ex = px0 + float(rng.integers(-2000, 2000))
        ey = py0 + float(rng.integers(-2000, 2000))
        x0, y0, w, h = phase_s_roi(px0, py0, pw, ph, tw, th, ex, ey, side)
        if not (0 <= x0 and x0 + w <= pw - tw + 1 and 0 <= y0 and y0 + h <= ph - th + 1):
            inb = False
            break
    check("20,000 random geometries stay inside the result map", inb)

    print()
    print("CROP IDENTITY  (score map of a crop == sub-rectangle of the full map)")
    print("-" * 78)
    worst = 0.0
    exact = total = 0
    shape_ok = True
    for trial in range(60):
        pw = int(rng.integers(120, 500))
        ph = int(rng.integers(90, 320))
        tw = int(rng.integers(8, min(pw // 2, 200)))
        th = int(rng.integers(8, min(ph // 2, 140)))
        patch = (rng.integers(0, 2, size=(ph, pw)) * 255).astype(np.uint8)
        templ = (rng.integers(0, 2, size=(th, tw)) * 255).astype(np.uint8)
        full = cv2.matchTemplate(patch, templ, cv2.TM_CCOEFF_NORMED)
        roi = phase_s_roi(0, 0, pw, ph, tw, th,
                          float(rng.integers(0, pw)), float(rng.integers(0, ph)),
                          "left" if trial % 2 else "right")
        x0, y0, w, h = roi
        cx0, cy0, cw, ch = crop_bounds(roi, tw, th)
        sub = cv2.matchTemplate(np.ascontiguousarray(patch[cy0:cy0 + ch, cx0:cx0 + cw]),
                                templ, cv2.TM_CCOEFF_NORMED)
        total += 1
        if sub.shape != (h, w):
            shape_ok = False
            break
        d = float(np.max(np.abs(sub.astype(np.float64) -
                                full[y0:y0 + h, x0:x0 + w].astype(np.float64))))
        exact += int(d == 0.0)
        worst = max(worst, d)
    check("every cropped map has the shape the ROI asked for", shape_ok)
    check("cropped values match the full map to < 1e-5", worst < 1e-5,
          "max |delta| = {:.3e} over {} random crops ({} bit-exact)".format(
              worst, total, exact))

    print()
    print("FLAT / DEGENERATE WINDOWS")
    print("-" * 78)
    for name, mk in (("all-zero patch", lambda s: np.zeros(s, np.uint8)),
                     ("all-255 patch", lambda s: np.full(s, 255, np.uint8))):
        patch = mk((200, 300))
        templ = (rng.integers(0, 2, (31, 52)) * 255).astype(np.uint8)
        full = cv2.matchTemplate(patch, templ, cv2.TM_CCOEFF_NORMED)
        roi = phase_s_roi(0, 0, 300, 200, 52, 31, 150.0, 100.0, "left")
        x0, y0, w, h = roi
        cx0, cy0, cw, ch = crop_bounds(roi, 52, 31)
        sub = cv2.matchTemplate(np.ascontiguousarray(patch[cy0:cy0 + ch, cx0:cx0 + cw]),
                                templ, cv2.TM_CCOEFF_NORMED)
        d = float(np.max(np.abs(sub.astype(np.float64) -
                                full[y0:y0 + h, x0:x0 + w].astype(np.float64))))
        check("{} crops identically".format(name), d < 1e-5,
              "max |delta| {:.3e}".format(d))

    print()
    print("TIE ORDER")
    print("-" * 78)
    # A map with exact ties: minMaxLoc scans row-major, so masking the exterior
    # must leave the FIRST in-ROI tie winning, not some other one.
    m = np.full((200, 300), 0.5, np.float32)
    for (yy, xx) in ((10, 10), (120, 140), (121, 140), (121, 141)):
        m[yy, xx] = 0.9
    x0, y0, w, h = 100, 100, ROI_W, ROI_H
    out = np.full(m.shape, OUTSIDE, np.float32)
    out[y0:y0 + h, x0:x0 + w] = m[y0:y0 + h, x0:x0 + w]
    _, _, _, loc = cv2.minMaxLoc(out)
    check("masked map keeps row-major first-tie order", tuple(loc) == (140, 120),
          "got {}".format(tuple(loc)))
    _, _, _, floc = cv2.minMaxLoc(m)
    check("and the full map's earlier out-of-ROI tie is the one dropped",
          tuple(floc) == (10, 10), "full picks {}".format(tuple(floc)))

    print()
    print("SELFTEST {}".format("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# truncation report
# ---------------------------------------------------------------------------
def truncation_report():
    import cpu_baseline_snapshot as snap

    class _A:
        zoom = 4.0

    side_templates = snap.load_templates(_A)
    print("int() vs round() ENVELOPE GEOMETRY at max(scales) = {:.2f}".format(ENVELOPE_SCALE))
    print("=" * 78)
    print("  {:<6} {:<8} {:>4} {:>4}   {:>9} {:>9}   {:>11} {:>11}   {}".format(
        "side", "kind", "bw", "bh", "max_tw", "max_th", "patch int", "patch rnd", "delta"))
    seen = set()
    n_diff = 0
    for side, kinds in sorted(side_templates.items()):
        for kind, bank in sorted(kinds.items()):
            for t in bank:
                bw, bh = int(t.shape[1]), int(t.shape[0])
                key = (side, kind, bw, bh)
                if key in seen:
                    continue
                seen.add(key)
                ti, tj = int(bw * ENVELOPE_SCALE), int(bh * ENVELOPE_SCALE)
                ri, rj = int(round(bw * ENVELOPE_SCALE)), int(round(bh * ENVELOPE_SCALE))
                pi = model.patch_geometry(ti, tj)
                pr = model.patch_geometry(ri, rj)
                d = "" if pi == pr else "+{} x +{}".format(pr[0] - pi[0], pr[1] - pi[1])
                n_diff += int(pi != pr)
                print("  {:<6} {:<8} {:>4} {:>4}   {:>4}/{:<4} {:>4}/{:<4}   {:>4}x{:<6} {:>4}x{:<6}   {}".format(
                    side, kind, bw, bh, ti, ri, tj, rj,
                    pi[0], pi[1], pr[0], pr[1], d))
    print()
    print("  {} of {} distinct templates change patch geometry under the fix.".format(
        n_diff, len(seen)))
    print("  The envelope only ever GROWS, so the fix cannot make a template stop fitting.")
    return 0


# ---------------------------------------------------------------------------
# corpus run
# ---------------------------------------------------------------------------
class _Args:
    zoom = 4.0
    score_thresh = SCORE_THRESH
    ferrule_score_thresh = FERRULE_SCORE_THRESH
    score_margin = SCORE_MARGIN


def run_corpus(patterns, phase_s, fix_truncation, roi, baseline_dir):
    import cpu_baseline_snapshot as snap

    manifest = {}
    with open(os.path.join(baseline_dir, "baseline_manifest.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            manifest[(row["input_sha256"], int(row["page"]))] = row
    page_counts = []

    pdfs = sorted({p for pat in patterns for p in globmod.glob(pat)
                   if os.path.isfile(p) and p.lower().endswith(".pdf")},
                  key=lambda p: os.path.basename(p).lower())
    if not pdfs:
        raise SystemExit("no PDFs matched")

    side_templates = snap.load_templates(_Args)
    same = diff = count_delta = 0
    delta_total = {"male": 0, "female": 0, "ferrule": 0, "unknown": 0}
    changed_pages = []
    pages = 0

    with PhaseS(phase_s=phase_s, fix_truncation=fix_truncation, roi=roi) as ps:
        for pdf in pdfs:
            p = Path(pdf)
            sha = snap._sha256_file(p)
            for page_index, detections, _n in snap.run_one(p, side_templates, _Args):
                pages += 1
                base = manifest.get((sha, page_index))
                if base is None:
                    print("  {} p{}  NO BASELINE ROW".format(p.name, page_index))
                    continue
                got = snap.detections_digest(detections)
                nc = snap.counts_of(detections)
                bc = {k: int(base[k]) for k in ("male", "female", "ferrule", "unknown")}
                page_counts.append(dict(sha=sha, anon=base["anon_id"],
                                        page=page_index, run=nc, base=bc))
                if got == base["detections_sha256"]:
                    same += 1
                    continue
                diff += 1
                if bc != nc:
                    count_delta += 1
                    for k in delta_total:
                        delta_total[k] += nc[k] - bc[k]
                    changed_pages.append((base["anon_id"], page_index, bc, nc))
    return ps, dict(pages=pages, same=same, diff=diff, count_delta=count_delta,
                    delta_total=delta_total, changed_pages=changed_pages,
                    page_counts=page_counts, pdfs=pdfs)


# ---------------------------------------------------------------------------
# labelled accuracy
# ---------------------------------------------------------------------------
def truth_eval(res, truth_xlsx):
    """Corpus count accuracy against the labelled workbook, baseline vs this run.

    Byte-parity against baseline_cpu_20260811 answers "did anything move".  It
    cannot answer "did it get better", because the baseline is a snapshot of CPU
    BEHAVIOUR, not ground truth.  This does, for the subset of the corpus the
    workbook actually labels.

    Truth is per part number, i.e. per input file, so per-page counts are summed
    over the file's pages before comparing.  Only male/female/ferrule are
    labelled; `unknown` is a detector state with no truth column and is reported
    but never scored.
    """
    sys.path.insert(0, str(HERE.parents[1] / "sw"))
    from evaluate_expected_results import load_workbook_truth, normalize_part_number
    import hashlib

    truth = {r.part_number: r for r in load_workbook_truth(Path(truth_xlsx))}

    def sha256(p):
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    part_of = {}
    for p in res["pdfs"]:
        part_of[sha256(p)] = normalize_part_number(Path(p).stem)

    files = {}
    for rec in res["page_counts"]:
        f = files.setdefault(rec["sha"], {
            "anon": rec["anon"],
            "run": {"male": 0, "female": 0, "ferrule": 0, "unknown": 0},
            "base": {"male": 0, "female": 0, "ferrule": 0, "unknown": 0}})
        for which in ("run", "base"):
            for k in f[which]:
                f[which][k] += rec[which][k]

    print()
    print("LABELLED ACCURACY  (workbook truth; counts summed per input file)")
    print("-" * 78)
    print("  {:<16} {:>12} {:>14} {:>14} {:>9} {:>9}".format(
        "file", "truth m/f/fe", "baseline", "this run", "|err| b", "|err| r"))
    tb = tr = 0
    n_better = n_worse = n_same = n_unlabelled = 0
    rows = []
    for sha, f in sorted(files.items(), key=lambda kv: kv[1]["anon"]):
        t = truth.get(part_of.get(sha))
        if t is None or all(v is None for v in (t.male_true, t.female_true, t.ferrule_true)):
            n_unlabelled += 1
            continue
        tt = [t.male_true or 0, t.female_true or 0, t.ferrule_true or 0]
        bb = [f["base"][k] for k in ("male", "female", "ferrule")]
        rr = [f["run"][k] for k in ("male", "female", "ferrule")]
        eb = sum(abs(bb[i] - tt[i]) for i in range(3))
        er = sum(abs(rr[i] - tt[i]) for i in range(3))
        tb += eb
        tr += er
        if er < eb:
            n_better += 1
        elif er > eb:
            n_worse += 1
        else:
            n_same += 1
        rows.append((f["anon"], tt, bb, rr, eb, er, f["base"]["unknown"], f["run"]["unknown"]))
    for anon, tt, bb, rr, eb, er, ub, ur in rows:
        if eb == er:
            continue
        print("  {:<16} {:>12} {:>14} {:>14} {:>9} {:>9}   {}".format(
            anon.replace(".pdf", ""), "{}/{}/{}".format(*tt),
            "{}/{}/{} u{}".format(*bb, ub), "{}/{}/{} u{}".format(*rr, ur),
            eb, er, "BETTER" if er < eb else "WORSE"))
    print()
    print("  files scored                {}  ({} unlabelled, skipped)".format(
        len(rows), n_unlabelled))
    print("  files improved / unchanged / regressed   {} / {} / {}".format(
        n_better, n_same, n_worse))
    print("  TOTAL absolute count error  baseline {}   this run {}   ({:+d})".format(
        tb, tr, tr - tb))
    print()
    print("  This is the only measurement here that speaks to DETECTION QUALITY.")
    print("  Byte-parity against the CPU baseline does not, and a page that")
    print("  fails parity while moving TOWARD the labels is not a regression.")
    return tb, tr


def report(ps, res, phase_s, fix_truncation, roi):
    print()
    print("PER-TRIAL  (oracle = full patch, Phase S = {}x{} crop)".format(*roi))
    print("-" * 78)
    for kind in ("initial", "refinement"):
        n = ps.stats[(kind, "trials")]
        if not n:
            continue
        print("  {:<11} trials {:<7} crop bit-exact {:<7} ({:6.2f}%)".format(
            kind, n, ps.stats[(kind, "crop_exact")],
            100.0 * ps.stats[(kind, "crop_exact")] / n))
        print("  {:<11} oracle argmax in ROI {:<6} ({:6.2f}%)   same loc {:<6} ({:6.2f}%)   same score {:6.2f}%".format(
            "", ps.stats[(kind, "oracle_argmax_in_roi")],
            100.0 * ps.stats[(kind, "oracle_argmax_in_roi")] / n,
            ps.stats[(kind, "same_loc")], 100.0 * ps.stats[(kind, "same_loc")] / n,
            100.0 * ps.stats[(kind, "same_score")] / n))
        for bad in ("GEOMETRY MISMATCH", "CROP SHAPE WRONG"):
            if ps.stats[(kind, bad)]:
                print("  {:<11} *** {} on {} trials ***".format("", bad, ps.stats[(kind, bad)]))
    print()
    print("  max |cropped - full| over every trial   {:.3e}{}".format(
        ps.max_crop_delta,
        "" if ps.worst_crop is None else
        "   worst at tw={tw} th={th} pw={pw} ph={ph}".format(**ps.worst_crop)))
    print("  A nonzero value is OpenCV switching between spatial and DFT")
    print("  correlation with the array size, not a crop-placement error; the")
    print("  exact-integer HLS core has no such term.")

    print()
    print("MODELLED MATCHER TIME  s/page @ {:g} MHz  (this run's own geometry;".format(
        model.TARGET_CLOCK_HZ / 1e6))
    print("  initial trials only - no DMA, no PS work, no refinement)")
    print("-" * 78)
    pages = res["pages"]
    cur = ps.cycles[("initial", "current")] / pages / model.TARGET_CLOCK_HZ
    print("  full-patch search                            {:9.3f}".format(cur))
    for v, label in (("cur", "Phase S"), ("B1", "Phase S + B1"), ("B2", "Phase S + B2")):
        s = ps.cycles[("initial", v)] / pages / model.TARGET_CLOCK_HZ
        print("  {:<44} {:9.3f}   {:6.2f}x".format(label, s, cur / max(s, 1e-9)))
    rf = ps.cycles[("refinement", "cur")] / pages / model.TARGET_CLOCK_HZ
    print("  refinement, at Phase S geometry              {:9.3f}".format(rf))

    print()
    print("PAGE PARITY vs baseline_cpu_20260811")
    print("-" * 78)
    total = res["same"] + res["diff"]
    print("  pages compared             {}".format(total))
    print("  byte-identical detections  {} ({:.1f}%)".format(
        res["same"], 100.0 * res["same"] / max(total, 1)))
    print("  differing detections       {}".format(res["diff"]))
    print("  of which counts changed    {}".format(res["count_delta"]))
    print("  net count delta            {}".format(
        {k: v for k, v in res["delta_total"].items() if v} or "none"))
    for anon, pi, bc, nc in res["changed_pages"]:
        print("    {:<28} p{}  {} -> {}".format(
            anon, pi, {k: bc[k] for k in bc if bc[k] != nc[k]},
            {k: nc[k] for k in nc if bc[k] != nc[k]}))
    print()
    if not phase_s and not fix_truncation:
        print("  CONTROL: this must be {}/{} identical for the harness to be trusted.".format(
            total, total))
    else:
        print("  GATE: Phase S is selectable only once this accuracy result is accepted.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("patterns", nargs="*", help="PDF glob(s)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--truncation-report", action="store_true")
    ap.add_argument("--phase-s", action="store_true", help="crop to the ROI")
    ap.add_argument("--control", action="store_true",
                    help="instrument but change nothing; must reproduce the baseline")
    ap.add_argument("--fix-truncation", action="store_true",
                    help="size the patch envelope with round() instead of int()")
    ap.add_argument("--roi", default="{}x{}".format(ROI_W, ROI_H))
    ap.add_argument("--baseline", default=str(HERE.parents[1] / "baseline_cpu_20260811"))
    ap.add_argument("--truth", nargs="?", const=str(HERE.parents[1] / "sw" / "expected_result.xlsx"),
                    help="score counts against the labelled workbook as well as the baseline")
    ap.add_argument("--dump", help="write per-trial JSONL here")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.truncation_report:
        return truncation_report()
    if not args.patterns:
        raise SystemExit("need a PDF glob (or --selftest / --truncation-report)")

    w, h = (int(v) for v in args.roi.lower().split("x"))
    phase_s = args.phase_s and not args.control

    print("PHASE S PROTOTYPE   crop {}   envelope {}   ROI {}x{}".format(
        "ON" if phase_s else "OFF (control)",
        "round() [fixed]" if args.fix_truncation else "int() [as shipped]", w, h))
    print("=" * 78)
    ps, res = run_corpus(args.patterns, phase_s, args.fix_truncation, (w, h), args.baseline)
    report(ps, res, phase_s, args.fix_truncation, (w, h))
    if args.truth:
        truth_eval(res, args.truth)

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            for r in ps.records:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        print()
        print("  per-trial records -> {}".format(args.dump))
    return 0


if __name__ == "__main__":
    sys.exit(main())
