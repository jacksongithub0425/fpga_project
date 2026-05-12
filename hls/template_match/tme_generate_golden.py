"""
Generate testbench golden data for tme_tb.cpp.

Run this once from the template_match/ directory:
    python tme_generate_golden.py

Picks a real terminal location on the sample PDF (via cv2.matchTemplate
over the whole binarized page), then computes the reference score using
the SAME formula the HLS implementation uses:

    score(u,v) = sum( I(u+x,v+y) * T'(x,y) ) /
                 sqrt( sum(T'^2) * sum(I(u+x,v+y)^2) )

where T' = T - mean(T) is the mean-subtracted template.

NOTE: this is NOT identical to cv2.matchTemplate(..., TM_CCOEFF_NORMED),
which uses sum((I - mean(I))^2) in the denominator. The HLS does not
mean-subtract the patch. The testbench validates that HLS matches its
own intended math; end-to-end accuracy vs OpenCV is measured in A5.

Outputs:
    tb_patch.bin     - raw uint8 patch pixels, row-major
    tb_templ.bin     - mean-subtracted template as uint8+128, row-major
    tb_golden.txt    - "score x y patch_w patch_h templ_w templ_h"
"""

import sys
from pathlib import Path

import cv2
import numpy as np

# Add sw/ to path so we can import the existing script
SW_DIR = Path(__file__).resolve().parents[2] / "sw"
sys.path.insert(0, str(SW_DIR))

from terminal_counter_endpoint_first import (
    render_page,
    to_binary_inv,
    build_endpoint_patch,
    MATCH_SCALES,
)
import fitz

# ---- Configuration ------------------------------------------------------
SAMPLE_PDF = SW_DIR / "test_sample" / "_A.PDF"
MALE_LEFT_TEMPL = SW_DIR / "male_ter" / "male_left.png"
SCALE_IDX = 4     # MATCH_SCALES[4] = 1.10 - middle scale
ZOOM = 4.0
PAGE_IDX = 0      # first page
SIDE = "left"

# ---- Load PDF and render ------------------------------------------------
doc = fitz.open(str(SAMPLE_PDF))
page = doc[PAGE_IDX]
bgr, gray, _ = render_page(page, zoom=ZOOM)
page_bin = to_binary_inv(gray)
print(f"Page rendered: {page_bin.shape[1]}x{page_bin.shape[0]} px")

# ---- Load template ------------------------------------------------------
base_templ = cv2.imread(str(MALE_LEFT_TEMPL), cv2.IMREAD_GRAYSCALE)
base_templ = to_binary_inv(base_templ)

scale = MATCH_SCALES[SCALE_IDX]
tw = max(4, int(round(base_templ.shape[1] * scale)))
th = max(4, int(round(base_templ.shape[0] * scale)))
templ = cv2.resize(base_templ, (tw, th), interpolation=cv2.INTER_NEAREST)
print(f"Template at scale {scale}: {tw}x{th} px")

# ---- Find a real terminal location -------------------------------------
# Run cv2.matchTemplate over the whole page to get a strong-match location.
# We use TM_CCOEFF_NORMED here only to find WHERE a terminal is - the
# golden score itself is recomputed below using the HLS formula.
img_h, img_w = page_bin.shape
full_score = cv2.matchTemplate(page_bin, templ, cv2.TM_CCOEFF_NORMED)
_, top_val, _, top_loc = cv2.minMaxLoc(full_score)
ep_x = float(top_loc[0] + tw // 2)   # center of best match
ep_y = float(top_loc[1] + th // 2)
print(f"Real terminal seed: cv2 score={top_val:.4f} at ({top_loc[0]},{top_loc[1]})")
print(f"Endpoint center for patch: ({ep_x:.0f},{ep_y:.0f})")

# ---- Build the search patch around that location ------------------------
max_tw = int(base_templ.shape[1] * max(MATCH_SCALES))
max_th = int(base_templ.shape[0] * max(MATCH_SCALES))
px0, py0, px1, py1 = build_endpoint_patch(ep_x, ep_y, SIDE, img_w, img_h, max_tw, max_th)
patch = page_bin[py0:py1, px0:px1].copy()
print(f"Patch: ({px0},{py0}) to ({px1},{py1}) -> {patch.shape[1]}x{patch.shape[0]} px")

if patch.shape[0] < th + 1 or patch.shape[1] < tw + 1:
    print("ERROR: patch too small for template")
    sys.exit(1)

# ---- Encode template (mean-subtract, clip to int8 range, store as +128) -
# NB: for binary templates with sparse foreground, T - mean(T) can exceed
# the int8 range (e.g. T=255, mean=27 -> T'=228 wraps to -28 in int8).
# Both the HLS encoding and the reference must clip the same way.
templ_float = templ.astype(np.float32)
templ_mean  = templ_float.mean()
templ_ms_clipped = np.clip(templ_float - templ_mean, -128, 127).astype(np.int8)
templ_encoded = (templ_ms_clipped.astype(np.int16) + 128).astype(np.uint8)
print(f"Template T' range: [{templ_ms_clipped.min()}, {templ_ms_clipped.max()}] "
      f"(clipped from [{(templ_float-templ_mean).min():.1f}, {(templ_float-templ_mean).max():.1f}])")

# ---- Compute golden using the HLS formula (NOT cv2.TM_CCOEFF_NORMED) ----
#   num(u,v)   = sum I(u+x,v+y) * T'(x,y)
#   isq(u,v)   = sum I(u+x,v+y)^2
#   denom_sq   = templ_energy * isq
#   score(u,v) = num / sqrt(denom_sq)   if denom_sq > 1e-4 else 0
# (the denom_sq <= 1e-4 guard mirrors norm_rsqrt.cpp:21)
DENOM_SQ_MIN = 1e-4

patch_f32 = patch.astype(np.float32)
templ_ms_f32 = templ_ms_clipped.astype(np.float32)

# Numerator via cross-correlation (cv2 TM_CCORR is exactly sum I*T)
num_map = cv2.matchTemplate(patch_f32, templ_ms_f32, cv2.TM_CCORR)

# sum(I^2) over each (th x tw) window via integral image
patch_sq = patch_f32 ** 2
ii = cv2.integral(patch_sq)   # shape (ph+1, pw+1)
isq_map = (ii[th:, tw:] - ii[:-th, tw:] - ii[th:, :-tw] + ii[:-th, :-tw])

templ_energy = float((templ_ms_f32 ** 2).sum())
denom_sq_map = templ_energy * isq_map
score_map = np.zeros_like(num_map, dtype=np.float32)
valid_denom = denom_sq_map > DENOM_SQ_MIN
score_map[valid_denom] = num_map[valid_denom] / np.sqrt(denom_sq_map[valid_denom])
np.clip(score_map, -1.0, 1.0, out=score_map)

# HLS evaluates rh = ph - th, rw = pw - tw (one less than cv2 in each dim).
# Match that exactly so the testbench compares apples-to-apples.
ph, pw = patch.shape
rh_hls = ph - th
rw_hls = pw - tw
score_map_hls = score_map[:rh_hls, :rw_hls]

gold_y, gold_x = np.unravel_index(int(np.argmax(score_map_hls)), score_map_hls.shape)
gold_score = float(score_map_hls[gold_y, gold_x])
print(f"Golden (HLS formula): score={gold_score:.4f}  loc=({gold_x},{gold_y})")

if gold_score < 0.2:
    print("WARNING: golden score is low - testbench may not be discriminating")

# ---- Write output files --------------------------------------------------
patch_path  = Path("tb_patch.bin")
templ_path  = Path("tb_templ.bin")
golden_path = Path("tb_golden.txt")

patch_path.write_bytes(patch.tobytes())
templ_path.write_bytes(templ_encoded.tobytes())
golden_path.write_text(
    f"{gold_score:.6f} {gold_x} {gold_y} "
    f"{patch.shape[1]} {patch.shape[0]} {tw} {th}\n"
)

print(f"Written: {patch_path} ({patch.nbytes} bytes)")
print(f"Written: {templ_path} ({templ_encoded.nbytes} bytes)")
print(f"Written: {golden_path}")
print("Run 'vitis-run --mode hls --tcl run_hls.tcl' to launch C-simulation.")
