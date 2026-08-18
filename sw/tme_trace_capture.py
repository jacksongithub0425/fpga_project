#!/usr/bin/env python3
"""Capture the per-trial matcher workload trace over the page corpus.

Priority 0 artifact, second half.  `tme_cycle_model.py` freezes what a trial
COSTS; this freezes which trials actually run, in what order, and what each one
scored.  Priority 1 (scale-policy validation) is an offline study over the
output of this tool, so the corpus is rendered and matched exactly once.

    python tme_trace_capture.py "../../sample/*"  --out ../../trace_20260817
    python tme_trace_capture.py "../../sample/*"  --out ... --limit 1   # smoke test

Run it with the HLS venv python:

    C:/Users/lychee/Desktop/FPGA/hls/.venv/Scripts/python.exe tme_trace_capture.py ...

Quote the glob.  It is expanded here, not by the shell (PowerShell does not
expand wildcards for native programs), matching is case-insensitive, and a
pattern that matches nothing is an error.

WHAT IS RECORDED
----------------
One record per cv2.matchTemplate call the detector makes, in the exact global
order it makes them, carrying:

    global_index            order across the whole page, ties resolve on this
    call_kind               "initial" (20,680) or "refinement" (808)
    side, kind, templ_index which template bank entry this is
    scale, tw, th           the resized template actually correlated
    pw, ph, rw, rh          patch and result-map geometry
    raw_score, raw_loc      cv2.minMaxLoc of the result map
    adj_score, adj_loc      location-penalised argmax, for refinement calls
    cycles_*                modelled matcher cost under each architecture

`adj_*` is what `prefer_local_alignment` selects, and it is recorded because a
scalar-output PL core cannot reproduce it: it is the argmax of
`result - 0.12 * norm_dist` over the WHOLE map, not of `result`.  Priority 8
needs this column to decide whether refinement stays on the PS.

CONFIDENTIALITY SPLIT (same rule as the CPU baseline snapshot)
--------------------------------------------------------------
The per-page JSONL carries endpoint coordinates and scores lifted off
confidential drawings, so `--out` is LOCAL and must not be committed.  The
summary CSV written beside it is anonymized (example_NN.pdf ids, geometry and
counts only, no coordinates) and is the committable artifact.
"""

from __future__ import annotations

import argparse
import glob as globmod
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import cv2
import fitz
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import terminal_counter_endpoint_first as det          # noqa: E402
import tme_cycle_model as model                        # noqa: E402

# Baseline detector settings.  These must match baseline_provenance.json or the
# trace describes a different workload than the frozen figures.
ZOOM = 4.0
SCORE_THRESH = 0.33
FERRULE_SCORE_THRESH = 0.24
SCORE_MARGIN = 0.03
ANCHOR_WEIGHT = 0.12          # best_template_match_local default

KINDS = ("male", "female", "ferrule")


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------
class Tracer:
    """Wraps the detector's two hot functions and records every trial."""

    def __init__(self):
        self.records = []
        self.ctx = None
        self._orig_btml = None
        self._orig_mt = None
        self._counter = 0
        self._bank_index = {}

    def index_templates(self, side_templates):
        """Map id(template array) -> (side, kind, position in bank)."""
        for side, kinds in side_templates.items():
            for kind, bank in kinds.items():
                for i, t in enumerate(bank):
                    self._bank_index[id(t)] = (side, kind, i)

    # -- the wrapped scale loop ---------------------------------------------
    def _btml(self, page_bin, template_bin, endpoint_xy, side, scales,
              anchor_distance_weight=ANCHOR_WEIGHT, prefer_local_alignment=False):
        img_h, img_w = page_bin.shape[:2]

        # Mirror the detector's own geometry exactly, including the int()
        # truncation on max scale (Priority 3 flags this as a defect: the
        # resized template uses round(), the patch envelope uses int()).
        max_tw = int(template_bin.shape[1] * max(scales))
        max_th = int(template_bin.shape[0] * max(scales))
        px0, py0, px1, py1 = det.build_endpoint_patch(
            endpoint_xy[0], endpoint_xy[1], side, img_w, img_h, max_tw, max_th)
        pw, ph = px1 - px0, py1 - py0

        # Which scales survive the detector's skip test, in order.  The k-th
        # matchTemplate call inside this invocation is the k-th survivor.
        plan = []
        for sc in scales:
            tw = max(4, int(round(template_bin.shape[1] * sc)))
            th = max(4, int(round(template_bin.shape[0] * sc)))
            if tw >= pw or th >= ph:
                continue
            plan.append((sc, tw, th))

        anchor = det.side_template_anchor(template_bin, side)
        self.ctx = {
            "plan": plan, "i": 0,
            "side": side, "endpoint": (float(endpoint_xy[0]), float(endpoint_xy[1])),
            "px0": px0, "py0": py0, "pw": pw, "ph": ph,
            "max_tw_int": max_tw, "max_th_int": max_th,
            "max_tw_round": int(round(template_bin.shape[1] * max(scales))),
            "max_th_round": int(round(template_bin.shape[0] * max(scales))),
            "anchor": anchor,
            "weight": anchor_distance_weight,
            "prefer": bool(prefer_local_alignment),
            "templ_id": self._bank_index.get(id(template_bin), (side, "?", -1)),
            "base_wh": (int(template_bin.shape[1]), int(template_bin.shape[0])),
        }
        try:
            return self._orig_btml(
                page_bin, template_bin, endpoint_xy, side, scales,
                anchor_distance_weight=anchor_distance_weight,
                prefer_local_alignment=prefer_local_alignment)
        finally:
            self.ctx = None

    # -- the wrapped correlation --------------------------------------------
    def _mt(self, patch, templ, method):
        result = self._orig_mt(patch, templ, method)
        c = self.ctx
        if c is None or c["i"] >= len(c["plan"]):
            return result
        sc, tw, th = c["plan"][c["i"]]
        c["i"] += 1

        _, raw_max, _, raw_loc = cv2.minMaxLoc(result)
        rh, rw = result.shape[:2]

        adj_score = adj_loc = adj_raw = None
        if c["prefer"]:
            # Exactly the detector's location-adjusted argmax (lines 566-576).
            rows = np.arange(rh, dtype=np.float32)[:, None]
            cols = np.arange(rw, dtype=np.float32)[None, :]
            ax = (c["px0"] + cols) + c["anchor"][0] * sc
            ay = (c["py0"] + rows) + c["anchor"][1] * sc
            nd = np.hypot(ax - c["endpoint"][0], ay - c["endpoint"][1]) / max(8.0, 0.5 * (tw + th))
            adjusted = result.astype(np.float32) - (c["weight"] * nd.astype(np.float32))
            bl = np.unravel_index(int(np.argmax(adjusted)), adjusted.shape)
            adj_loc = [int(bl[1]), int(bl[0])]
            adj_score = float(adjusted[bl])
            adj_raw = float(result[bl])

        side, kind, ti = c["templ_id"]
        self.records.append({
            "global_index": self._counter,
            "call_kind": "refinement" if c["prefer"] else "initial",
            "side": side, "kind": kind, "templ_index": ti,
            "base_w": c["base_wh"][0], "base_h": c["base_wh"][1],
            "scale": sc, "tw": tw, "th": th,
            "pw": c["pw"], "ph": c["ph"], "rw": rw, "rh": rh,
            "px0": c["px0"], "py0": c["py0"],
            "endpoint": c["endpoint"],
            "raw_score": round(float(raw_max), 6),
            "raw_loc": [int(raw_loc[0]), int(raw_loc[1])],
            "adj_score": None if adj_score is None else round(adj_score, 6),
            "adj_raw_score": None if adj_raw is None else round(adj_raw, 6),
            "adj_loc": adj_loc,
            "max_tw_int": c["max_tw_int"], "max_tw_round": c["max_tw_round"],
            "max_th_int": c["max_th_int"], "max_th_round": c["max_th_round"],
            "cycles_current": model.cycles(c["pw"], c["ph"], tw, th),
            "cycles_S": model.cycles(tw + 95, th + 63, tw, th),
            "cycles_S_B1": model.cycles(tw + 95, th + 63, tw, th, "B1"),
            "cycles_S_B2": model.cycles(tw + 95, th + 63, tw, th, "B2"),
            # B0b in two independent pieces, because they scale differently.
            # cycles_S_B0b_base is the DELETION of the window statistics; it is
            # not B0b on its own.  count_pass_iterations is the hoisted pass's
            # iteration count for THIS invocation -- one JSONL row is one
            # invocation, so the shape is directly available here.  Converting
            # iterations to cycles needs an initiation interval, which is
            # PROJECTED rather than measured, so that multiplication is left to
            # the model instead of being baked into the trace.
            "cycles_S_B0b_base": model.cycles(tw + 95, th + 63, tw, th, "B0b_base"),
            "count_pass_iterations": model.b0b_count_pass_iterations(
                tw + 95, th + 63, tw, th),
        })
        self._counter += 1
        return result

    def __enter__(self):
        self._orig_btml = det.best_template_match_local
        self._orig_mt = cv2.matchTemplate
        det.best_template_match_local = self._btml
        cv2.matchTemplate = self._mt
        return self

    def __exit__(self, *exc):
        det.best_template_match_local = self._orig_btml
        cv2.matchTemplate = self._orig_mt
        return False


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def load_side_templates():
    banks = {}
    for side in ("left", "right"):
        banks[side] = {}
        for kind in KINDS:
            key = kind + "_" + side
            path = HERE / det.STANDARD_TEMPLATE_DIRS[key] / (key + ".png")
            banks[side][kind] = det.load_template_bank(str(path))
    return banks


def expand(patterns):
    out = []
    for pat in patterns:
        hits = [p for p in globmod.glob(pat)
                if os.path.isfile(p) and p.lower().endswith((".pdf",))]
        if not hits:
            raise SystemExit("pattern matched no PDF: " + pat)
        out.extend(hits)
    return sorted(set(out), key=lambda p: os.path.basename(p).lower())


def sha256_file(path):
    """SHA-256 of the bytes on disk, for data files we write ourselves."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_source(path):
    """SHA-256 of a source file with line endings normalised to LF.

    Source is hashed EOL-INDEPENDENTLY on purpose.  git's core.autocrlf=true is
    set on the capture machine, so a file stored as LF is checked out as CRLF
    here and as LF on Linux -- hashing raw bytes would make "was this the same
    code?" answerable only on the platform that captured it.  Normalising means
    the answer is the same everywhere, which is the question provenance is
    actually asking.  Data files keep their raw-byte digest, because for those
    the bytes ARE the artifact.
    """
    b = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(b).hexdigest()


def write_provenance(outdir, args, pdfs, t_start, pages_done, totals):
    """Record everything needed to re-run this capture and detect drift.

    A correct number in a directory nobody can regenerate is not a freeze.  This
    pins the command, the interpreter, the code that ran (by hash, including the
    detector the workload is imported from), the inputs, and the outputs.  The
    input digests are the same values the summary's `input_sha256` column
    carries, so a summary row cannot be paired with a different source PDF.
    """
    import platform

    here = Path(__file__).resolve().parent
    code = {}
    for name in ("tme_trace_capture.py", "tme_cycle_model.py"):
        f = here / name
        if f.exists():
            code[name] = sha256_source(f)
    det_file = Path(det.__file__).resolve()
    code[det_file.name] = sha256_source(det_file)

    versions = {"python": sys.version.split()[0], "platform": platform.platform()}
    for mod, label in ((cv2, "opencv"), (np, "numpy"), (fitz, "pymupdf")):
        versions[label] = getattr(mod, "__version__", None) or getattr(
            mod, "version", ("unknown",))[0]

    outputs = {}
    for f in sorted(outdir.iterdir()):
        if f.is_file() and f.name != "provenance.json":
            outputs[f.name] = {"bytes": f.stat().st_size, "sha256": sha256_file(f)}

    prov = {
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_seconds": round(time.time() - t_start, 1),
        "command": {
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "interpreter": sys.executable,
        },
        "versions": versions,
        "code_sha256": code,
        "inputs": [{"path": str(f), "sha256": sha256_file(f)} for f in pdfs],
        "outputs_sha256": outputs,
        "workload": {
            "pages": pages_done,
            "initial_trials": totals["initial"],
            "refinement_calls": totals["refinement"],
            "candidates_left": totals["left"],
            "candidates_right": totals["right"],
        },
        "reproduced_frozen_figures": {
            k: model.FROZEN["s_per_page_at_125mhz"][k]
            for k in ("per_trial_roi", "B1", "B2", "B0b_base",
                      "B0b_at_1_cyc", "B0b_at_3_cyc")
        },
        "note": ("trials JSONL is LOCAL AND CONFIDENTIAL (endpoint coordinates); "
                 "trace_summary.csv and this file are committable.  Verify with "
                 "sha256sum against outputs_sha256."),
    }
    (outdir / "provenance.json").write_text(
        json.dumps(prov, indent=2), encoding="utf-8")
    print("wrote {}".format(outdir / "provenance.json"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("patterns", nargs="+", help="quoted glob(s) over the source PDFs")
    ap.add_argument("--out", required=True, help="LOCAL output dir (not committable)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N pages (smoke test)")
    ap.add_argument("--scales", default="",
                    help="comma-separated scale ladder to capture instead of the "
                         "detector's own MATCH_SCALES (algorithm experiment)")
    ap.add_argument("--pin-envelope", action="store_true",
                    help="hold the patch envelope at the 1.50 geometry while the "
                         "ladder changes, so scale choice and geometry stay separable")
    args = ap.parse_args()

    pdfs = expand(args.patterns)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    # A re-centred ladder is an ALGORITHM EXPERIMENT, not an acceleration: it
    # changes which templates the detector ever sees, so its output is not
    # comparable to the canonical baseline by digest.  Keeping the envelope
    # pinned at 1.50 is what makes the scale question separable from the
    # geometry question -- without it, changing the ladder also moves every
    # patch and the two effects cannot be told apart.
    orig_scales = det.MATCH_SCALES
    orig_bep = det.build_endpoint_patch
    if args.scales:
        det.MATCH_SCALES = tuple(float(s) for s in args.scales.split(","))
        print("ladder override: {}".format(
            ", ".join("{:.2f}".format(s) for s in det.MATCH_SCALES)))
    if args.pin_envelope:
        ratio = 1.50 / max(det.MATCH_SCALES)

        def pinned(ex, ey, side, img_w, img_h, max_tw, max_th):
            return orig_bep(ex, ey, side, img_w, img_h,
                            int(max_tw * ratio), int(max_th * ratio))
        det.build_endpoint_patch = pinned
        print("envelope pinned at the 1.50 geometry (ratio {:.4f})".format(ratio))

    side_templates = load_side_templates()
    summary_rows = []
    totals = {"initial": 0, "refinement": 0, "left": 0, "right": 0}
    cyc_totals = {k: 0 for k in ("current", "S", "S_B1", "S_B2", "S_B0b_base")}
    cp_iters = {"initial": 0, "refine": 0}
    cyc_refine = {}
    pages_done = 0
    t_start = time.time()

    for n_pdf, pdf in enumerate(pdfs, 1):
        anon = "example_{:02d}.pdf".format(n_pdf)
        sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
        doc = fitz.open(pdf)
        for pno in range(doc.page_count):
            if args.limit and pages_done >= args.limit:
                break
            t0 = time.time()
            tracer = Tracer()
            tracer.index_templates(side_templates)
            with tracer:
                det.detect_page(doc[pno], side_templates, ZOOM,
                                SCORE_THRESH, FERRULE_SCORE_THRESH, SCORE_MARGIN)
            dt = time.time() - t0

            recs = tracer.records
            init = [r for r in recs if r["call_kind"] == "initial"]
            refi = [r for r in recs if r["call_kind"] == "refinement"]
            # One candidate = one classify_endpoint pass = 4 (left) or 3 (right)
            # best_template_match_local calls, i.e. 32 / 24 initial trials.
            cl = len({(r["endpoint"][0], r["endpoint"][1]) for r in init if r["side"] == "left"})
            cr = len({(r["endpoint"][0], r["endpoint"][1]) for r in init if r["side"] == "right"})

            stem = Path(pdf).stem + "_p{}".format(pno)
            # Per-record terminator, not a separator: a page with no candidates
            # must produce an empty file, not a file containing one blank line.
            (outdir / (stem + "_trials.jsonl")).write_text(
                "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs),
                encoding="utf-8")

            totals["initial"] += len(init)
            totals["refinement"] += len(refi)
            totals["left"] += cl
            totals["right"] += cr
            # Initial trials only: the frozen s/page figures exclude refinement,
            # so summing over `recs` here would compare unlike quantities.
            for k, col in (("current", "cycles_current"), ("S", "cycles_S"),
                           ("S_B1", "cycles_S_B1"), ("S_B2", "cycles_S_B2"),
                           ("S_B0b_base", "cycles_S_B0b_base")):
                cyc_totals[k] += sum(r[col] for r in init)
                cyc_refine[k] = cyc_refine.get(k, 0) + sum(r[col] for r in refi)
            cp_iters["initial"] += sum(r["count_pass_iterations"] for r in init)
            cp_iters["refine"] += sum(r["count_pass_iterations"] for r in refi)

            summary_rows.append({
                "anon_id": anon, "page": pno, "input_sha256": sha,
                "initial_trials": len(init), "refinement_trials": len(refi),
                "candidates_left": cl, "candidates_right": cr,
                "cycles_current": sum(r["cycles_current"] for r in recs),
                "cycles_S": sum(r["cycles_S"] for r in recs),
                "cycles_S_B1": sum(r["cycles_S_B1"] for r in recs),
                "cycles_S_B2": sum(r["cycles_S_B2"] for r in recs),
                "cycles_S_B0b_base": sum(r["cycles_S_B0b_base"] for r in recs),
                "count_pass_iterations": sum(r["count_pass_iterations"] for r in recs),
                "wall_seconds": round(dt, 3),
            })
            pages_done += 1
            print("  {:<16} p{}  {:5d} initial  {:4d} refine  {:5.1f}s".format(
                anon, pno, len(init), len(refi), dt), flush=True)
        doc.close()
        if args.limit and pages_done >= args.limit:
            break

    det.MATCH_SCALES = orig_scales
    det.build_endpoint_patch = orig_bep

    import csv
    with (outdir / "trace_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)

    write_provenance(outdir, args, pdfs, t_start, pages_done, totals)

    print()
    print("pages            {}".format(pages_done))
    print("initial trials   {}".format(totals["initial"]))
    print("refinement calls {}".format(totals["refinement"]))
    print("candidates       {} left + {} right = {}".format(
        totals["left"], totals["right"], totals["left"] + totals["right"]))
    print("wall             {:.1f} s".format(time.time() - t_start))
    if pages_done:
        print()
        print("measured trace vs frozen model, INITIAL TRIALS ONLY, s/page @ 125 MHz:")
        for k, frozen_key in (("S", "per_trial_roi"), ("S_B1", "B1"), ("S_B2", "B2"),
                              ("S_B0b_base", "B0b_base")):
            got = cyc_totals[k] / pages_done / model.TARGET_CLOCK_HZ
            want = model.FROZEN["s_per_page_at_125mhz"][frozen_key]
            flag = "OK" if abs(got - want) <= 5e-3 else "DRIFT"
            print("  {:<8} {:8.3f}   frozen {:<8} {:8.3f}   {}".format(
                k, got, frozen_key, want, flag))
        print()
        print("refinement adds, s/page @ 125 MHz:")
        for k in ("S", "S_B1", "S_B2", "S_B0b_base"):
            print("  {:<12} {:8.3f}".format(k, cyc_refine.get(k, 0) / pages_done / model.TARGET_CLOCK_HZ))
        print()
        cp = model.FROZEN["b0b_count_pass"]
        print()
        print("B0b count pass, I = pw*(th + 2*(rh-1)), summed from this trace:")
        print("  initial trials   {:>15,} iterations   frozen {:>15,}   {}".format(
            cp_iters["initial"], cp["corpus_iterations"],
            "OK" if cp_iters["initial"] == cp["corpus_iterations"] else "DRIFT"))
        print("  refinement       {:>15,} iterations".format(cp_iters["refine"]))
        base = cyc_totals["S_B0b_base"] / pages_done / model.TARGET_CLOCK_HZ
        for n in (1, 3):
            ep = base + n * cp_iters["initial"] / pages_done / model.TARGET_CLOCK_HZ
            key = "B0b_at_{}_cyc".format(n)
            want = model.FROZEN["s_per_page_at_125mhz"][key]
            print("  endpoint II={}    {:18.12f} s/page   frozen {:18.12f}   {}".format(
                n, ep, want, "OK" if abs(ep - want) < 1e-9 else "DRIFT"))
        print()
        print("cycles_S_B0b_base is the WINDOW-STATISTICS DELETION only; adding the")
        print("count pass above gives B0b.  The ITERATION COUNT is derived per")
        print("invocation and recorded in the trace; the II is PROJECTED (achieved")
        print("throughput, not operator latency) and excludes pipeline setup/drain")
        print("and FSM overhead until synthesis reports it -- which is why the")
        print("iterations, not a cycle figure, are what this trace stores.")
    print()
    print("trials JSONL is LOCAL AND CONFIDENTIAL; trace_summary.csv is committable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
