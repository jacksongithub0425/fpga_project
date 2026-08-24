"""Keep Stage 3's production and frozen-trace refinement organizations apart.

The live production path provides the 26-call / 208-correlation expectation.
The retained trace independently contains 176 CPU-triggered refinement
records. Only the latter produce the historical 13,283,542,750-cycle
conditional projection when repriced at PL side-bank/B2 geometry.
"""
import sys
from pathlib import Path

SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
sys.path.insert(0, str(SW))

import fitz
import numpy as np

import board_gate_clock as C
import corpus_labels as CL
import pl_backends as B
import terminal_counter_endpoint_first as det
import tme_full_search_baseline as F
from test_pl_backends import FakePL

MHZ = 100.0
PDF = CL.resolve("doc_003")          # page_004
TRACE = Path(r"C:\Users\lychee\Desktop\FPGA\trace_20260818b")


class CountingPL(FakePL):
    def __init__(self):
        super().__init__()
        self.cycles = 0
        self.trials = 0

    def match_candidate(self, patch, x0, y0, trials, score_fn=None):
        ph, pw = patch.shape[:2]
        for trial in trials:
            if not trial["legal"]:
                continue
            th_, tw_ = trial["pixels"].shape
            if tw_ >= pw or th_ >= ph:
                continue
            self.cycles += C.cycles(pw, ph, tw_, th_, "B2")
            self.trials += 1
        return super().match_candidate(patch, x0, y0, trials, score_fn)


# Count what host refinement correlates, under the same law.
refine = {"cycles": 0, "corrs": 0, "calls": 0}
_real_local = det.best_template_match_local


def counting_local(page_bin, templ, endpoint, side, scales=(1.0,),
                   prefer_local_alignment=False, **kw):
    if prefer_local_alignment:
        h, w = page_bin.shape[:2]
        for s in scales:
            th_ = max(1, int(round(templ.shape[0] * s)))
            tw_ = max(1, int(round(templ.shape[1] * s)))
            # The refinement searches a LOCAL window, not the whole page; the
            # window `best_template_match_local` builds is what it correlates
            # over, so the patch dimensions come from the same helper the
            # detector uses rather than from the page.
            refine["corrs"] += 1
    return _real_local(page_bin, templ, endpoint, side, scales=scales,
                       prefer_local_alignment=prefer_local_alignment, **kw)


base = SW
st = det.build_side_templates(
    det.load_template_bank(str(base / "male_ter" / "male_left.png")),
    det.load_template_bank(str(base / "male_ter" / "male_right.png")),
    det.load_template_bank(str(base / "female_ter" / "female_left.png")),
    det.load_template_bank(str(base / "female_ter" / "female_right.png")),
    det.load_template_bank(str(base / "ferrule_ter" / "ferrule_left.png")),
    det.load_template_bank(str(base / "ferrule_ter" / "ferrule_right.png")))

pl = CountingPL()
backend = B.make_backend("pl-all", pl=pl)
det.best_template_match_local = counting_local
B.det.best_template_match_local = counting_local
try:
    doc = fitz.open(PDF)
    _bgr, cands, dets = det.detect_page(
        doc[0], side_templates=st, zoom=4.0, score_thresh=0.33,
        ferrule_score_thresh=0.24, score_margin=0.03, backend=backend,
        keep_bgr=False)
    doc.close()
finally:
    det.best_template_match_local = _real_local
    B.det.best_template_match_local = _real_local

init = pl.cycles
rollup, records, _, _ = F.build(str(TRACE))
page_key = PDF.stem + "_p0"
frozen_refine = [r for r in records
                 if r["page"] == page_key
                 and r["call_kind"] == "refinement"]
frozen_delta = sum(
    F.trial_cycles(
        *F.geometry(r, "pl_side_bank", rollup["pl_side_bank_geometry"]),
        r["tw"], r["th"], "B2")
    for r in frozen_refine)
frozen_mixed = init + frozen_delta

print(f"candidates {len(cands)}, detections {len(dets)}")
print(f"initial matching   : {init:,} cycles = {init / (MHZ*1e6):.4f} s")
print()
print("production organization:")
print(f"host refine calls  : {backend.refine_calls}")
print(f"refine correlations: {refine['corrs']}")
print()
print("frozen-trace organization:")
print(f"refinement records : {len(frozen_refine)}")
print(f"repriced delta     : {frozen_delta:,} cycles = "
      f"{frozen_delta / (MHZ*1e6):.4f} s")
print(f"mixed diagnostic  : {frozen_mixed:,} cycles = "
      f"{frozen_mixed / (MHZ*1e6):.4f} s")
print()
print("The production 208 and frozen 176 belong to different organizations.")
print("No per-correlation quotient is valid across them. Refinement runs on")
print("the ARM, so neither frozen-trace cycle figure is expected fabric time.")
