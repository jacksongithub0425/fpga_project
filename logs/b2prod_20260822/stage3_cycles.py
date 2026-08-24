"""Reproduce Stage 3's modelled cycle total from the page itself.

The acceptance target for `page_004` is the 1,483.236 s
(24m43s) initial-matching model. The historical 161,607,184,773-cycle figure
adds a separately captured, 176-record frozen-trace refinement organization;
it is retained only as a labelled diagnostic.

This drives the page through the exact fake fabric and sums
`board_gate_clock.cycles(pw, ph, tw, th, "B2")` over every trial the matcher
actually dispatches -- initial matching and, separately, whatever the host
refinement would cost if it ran on the same law. Nothing here is measured on
silicon; it is the analytical model evaluated on the real geometry.
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
from test_pl_backends import FakePL

PDF = CL.resolve("doc_003")          # page_004
MHZ = 100.0


class CountingPL(FakePL):
    """Adds up the modelled cycles of every trial it dispatches."""

    def __init__(self):
        super().__init__()
        self.cycles = 0
        self.trials = 0
        self.per_call = []
        self.patch_shapes = []

    def match_candidate(self, patch, x0, y0, trials, score_fn=None):
        ph, pw = patch.shape[:2]
        self.patch_shapes.append((pw, ph))
        before = self.cycles
        n = 0
        for trial in trials:
            if not trial["legal"]:
                continue
            t = trial["pixels"]
            th_, tw_ = t.shape
            if tw_ >= pw or th_ >= ph:
                continue
            self.cycles += C.cycles(pw, ph, tw_, th_, "B2")
            n += 1
        self.trials += n
        self.per_call.append((n, self.cycles - before))
        return super().match_candidate(patch, x0, y0, trials, score_fn)


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
doc = fitz.open(PDF)
_bgr, cands, dets = det.detect_page(
    doc[0], side_templates=st, zoom=4.0, score_thresh=0.33,
    ferrule_score_thresh=0.24, score_margin=0.03, backend=backend,
    keep_bgr=False)
doc.close()

init_cycles = pl.cycles
init_s = init_cycles / (MHZ * 1e6)
pw_max = max(p[0] for p in pl.patch_shapes) if pl.patch_shapes else 0
ph_max = max(p[1] for p in pl.patch_shapes) if pl.patch_shapes else 0

# The LABEL. This line is the first line of stage3_cycles.txt, which is
# committed, so it is the one print in this file that must not name a
# drawing -- and it was still doing so after the rest was converted.
print(f"page              : {CL.labels().page(PDF.name, 1)}")
print(f"candidates        : {len(cands)}   "
      f"(chunking: {len(pl.batches)} batch(es) of {pl.batches})")
print(f"classify calls    : {backend._matcher.calls}")
print(f"matcher trials    : {pl.trials:,}")
print(f"largest patch     : {pw_max}x{ph_max}")
print(f"detections        : {len(dets)}")
print(f"host refine calls : {backend.refine_calls}")
print()
print(f"INITIAL matching, modelled at {MHZ:g} MHz:")
print(f"  cycles          : {init_cycles:,}")
print(f"  seconds         : {init_s:.4f}  "
      f"({int(init_s // 60)}m{init_s % 60:.1f}s)")
print()
for label, target in (("frozen-trace mixed diagnostic", 161_607_184_773),
                      ("audit's initial model", 148_323_600_000)):
    d = init_cycles - target
    print(f"vs {label:<22} {target:>15,}  delta {d:>+15,} "
          f"({100.0 * d / target:+.3f}%)")
print()
print(f"frozen-trace mixed diagnostic: 1,616.0718 s = "
      f"{1616.0718 / 60:.4f} min")
print(f"initial acceptance target    : 1,483.236 s = "
      f"{1483.236 / 60:.4f} min")
