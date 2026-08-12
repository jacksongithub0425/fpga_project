"""
Generate testbench golden data for binarize_core.

Run from the binarize/ directory:
    python binarize_generate_golden.py

Writes:
    tb_gray.bin              - raw uint8 grayscale pixels, row-major
    tb_bin_golden.bin        - exact compact logical output (uint8, 0 or 255)
    tb_binarize_params.txt   - "threshold img_w img_h"
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import fitz

SW_DIR = Path(__file__).resolve().parents[2] / "sw"
sys.path.insert(0, str(SW_DIR))
from terminal_counter_endpoint_first import render_page

SAMPLE_PDF = SW_DIR / "test_sample" / "_A.PDF"
ZOOM = 4.0

# ---- Render and binarize using cv2 (ground truth) -----------------------
doc  = fitz.open(str(SAMPLE_PDF))
page = doc[0]
_, gray, _ = render_page(page, zoom=ZOOM)
img_h, img_w = gray.shape
print(f"Gray image: {img_w}×{img_h} px ({gray.nbytes // 1024} KB)")

# Compute Otsu threshold on 4× downsampled image (same as PS strategy in Phase A)
small = cv2.resize(gray, (img_w // 4, img_h // 4))
_, _ = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
# Get threshold value
_, thresh_img = cv2.threshold(small, 0, 255, cv2.THRESH_OTSU)
# cv2 doesn't directly return the threshold in the normal call, use retval
retval, _ = cv2.threshold(small, 0, 255, cv2.THRESH_OTSU)
threshold = int(retval)
print(f"Otsu threshold (from 4× downsampled): {threshold}")

# Replicate binarize_core.cpp exactly using pure numpy integer arithmetic.
#
# The HLS arithmetic uses a truncating shift. The stream internally produces
# a raw result at (row,col) whose 3×3 window is centred at (row-1,col-1).
# binarize_core's output scheduler maps that beat to the same logical
# (row-1,col-1) coordinate before compact S2MM storage.
#
# blur_valid[r, c] = floor(kernel_sum / 16) for 3×3 center at (r+1, c+1).
# Shape: (img_h-2, img_w-2).
gray_i = gray.astype(np.int32)
blur_valid = (
    gray_i[0:-2, 0:-2]   + gray_i[0:-2, 1:-1]*2 + gray_i[0:-2, 2:]   +
    gray_i[1:-1, 0:-2]*2 + gray_i[1:-1, 1:-1]*4 + gray_i[1:-1, 2:]*2 +
    gray_i[2:,   0:-2]   + gray_i[2:,   1:-1]*2 + gray_i[2:,   2:]
) >> 4
blur_valid = blur_valid.clip(0, 255).astype(np.uint8)

# In logical storage the valid centres occupy rows 1..h-2 and cols 1..w-2.
# Logical row/col 0 are the natural pipeline-fill zeros; the final row/col are
# explicitly appended as zeros so stale DDR can never fabricate border ink.
# THRESH_BINARY_INV: 255 where blurred <= threshold, else 0.
bin_img = np.zeros((img_h, img_w), dtype=np.uint8)
bin_img[1:-1, 1:-1] = np.where(
    blur_valid <= threshold, np.uint8(255), np.uint8(0))
print(
    "Logical binary image: "
    f"{np.count_nonzero(bin_img)} white pixels (lines); "
    "final row/column zero-filled"
)

# ---- Write files --------------------------------------------------------
Path("tb_gray.bin").write_bytes(gray.tobytes())
Path("tb_bin_golden.bin").write_bytes(bin_img.tobytes())
Path("tb_binarize_params.txt").write_text(f"{threshold} {img_w} {img_h}\n")

print("Written: tb_gray.bin, tb_bin_golden.bin, tb_binarize_params.txt")
print("Run 'source run_hls.tcl' in Vivado HLS.")
