#!/usr/bin/env python3
"""Board gate: the real 63,078,400-byte DMA transfer, end to end.

RUN THIS ON THE BOARD, after probe_cma_budget.py PASSES and
inspect_overlay.py PASSES:

    sudo python3 board_gate_full_dma.py --overlay /home/xilinx/three_stage_combined.bit

probe_cma_budget.py proves the two ~60.2 MiB buffers can be ALLOCATED (§2.2).
This script is the separate gate the allocation probe deliberately is not: it
MOVES a full 9856 x 6400 page through the PL — one 63,078,400-byte MM2S
transfer into binarize_core_0 and one 63,078,400-byte S2MM transfer out —
and verifies the output bit-exactly against the truncating-Gaussian CPU
oracle (cpu_golden in binarize_dma_checks.py, the v2.0 logical-layout
reference).  A 26-bit DMA length register (67,108,863 B max) is what makes
the single transfer legal; this is its first exercise at full size.

It drives the transfer through tme_driver.PLPipeline.binarize_page(), so a
PASS also validates the driver's binarize path — the first of the three
per-stage validations (contract §9, sw/tme_driver.py row).

Needs on the board, same directory: tme_driver.py, tme_standalone_bringup.py,
binarize_dma_checks.py, and the overlay .bit + .hwh pair.

Exit status: 0 = full-size transfer moved and verified bit-exact,
1 = the gate FAILS, 2 = could not run.

The gray page is procedural (blocks, gradients, and a diagonal edge burst) so
the binary result has structure in every region — an all-flat page would let
a stuck data lane pass the compare.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

IMG_W = 9856      # contract §2 maxima; keep in sync with PE_MAX_IMG_W/H
IMG_H = 6400
THRESHOLD = 140   # same value the three-stage C/golden case pinned


def procedural_page(w: int, h: int) -> np.ndarray:
    """Deterministic full-page gray image with structure everywhere."""
    x = np.arange(w, dtype=np.uint32)
    y = np.arange(h, dtype=np.uint32)
    # Horizontal gradient + vertical bands + a diagonal interference term:
    # every 3x3 neighbourhood differs from its neighbours somewhere.
    page = ((x[None, :] * 251 + y[:, None] * 199) % 256).astype(np.uint8)
    page[(y[:, None] // 64 + x[None, :] // 64) % 2 == 0] //= 2
    return page


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", required=True, metavar="BITFILE")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds before the transfer is declared hung")
    args = ap.parse_args()

    try:
        from tme_driver import PLPipeline
        from binarize_dma_checks import cpu_golden
    except ImportError as exc:
        print(f"CANNOT RUN: {exc} — copy tme_driver.py, "
              f"tme_standalone_bringup.py and binarize_dma_checks.py next to "
              f"this script")
        return 2

    n = IMG_W * IMG_H
    print(f"full-size DMA gate: {IMG_W} x {IMG_H} = {n:,} B each way")

    print("generating procedural page + CPU golden (numpy, exact)...")
    gray = procedural_page(IMG_W, IMG_H)
    t0 = time.monotonic()
    golden = cpu_golden(gray, THRESHOLD)
    print(f"  golden computed in {time.monotonic() - t0:.1f} s")

    # PLPipeline raises on any missing IP — no silent fallback, ever.
    try:
        pl = PLPipeline(args.overlay, timeout_s=args.timeout)
    except Exception as exc:                           # noqa: BLE001
        print(f"FAIL: could not bring up the overlay/driver: {exc}")
        return 1

    try:
        t0 = time.monotonic()
        binary = pl.binarize_page(gray, THRESHOLD)
        elapsed = time.monotonic() - t0
        print(f"  PL round trip (copy-in + 2 x {n:,} B DMA + core + "
              f"copy-out-free view): {elapsed:.2f} s")

        mism = int(np.count_nonzero(binary != golden))
        if mism:
            # Locate the first divergence — row/col is the difference between
            # "a data-lane fault" and "a layout/stride bug" at a glance.
            idx = np.argwhere(binary != golden)
            r, c = int(idx[0][0]), int(idx[0][1])
            print(f"FAIL: {mism:,} of {n:,} bytes mismatch the exact CPU "
                  f"oracle; first at logical ({c},{r}): "
                  f"PL={int(binary[r, c])} CPU={int(golden[r, c])}")
            return 1

        print(f"PASS: full 63,078,400-byte transfer each way, output "
              f"bit-exact against the truncating-Gaussian oracle "
              f"({elapsed:.2f} s wall).")
        return 0
    except Exception as exc:                           # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        pl.close()


if __name__ == "__main__":
    sys.exit(main())
