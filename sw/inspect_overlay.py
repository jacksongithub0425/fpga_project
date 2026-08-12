#!/usr/bin/env python3
"""Board-side introspection of the three_stage_combined overlay.

RUN THIS ON THE BOARD, after the §2.2 CMA probe, before any driver bring-up:

    sudo python3 inspect_overlay.py --overlay /home/xilinx/three_stage_combined.bit

It prints overlay.ip_dict, overlay.hierarchy_dict, the PL clock, each HLS
core's register_map and each DMA's channel configuration — and CHECKS them
against what sw/tme_driver.py was written to expect (from
three_stage_combined.hwh, 2026-08-11).  Exit status: 0 = the overlay matches
the driver's expectations, 1 = mismatch (fix the driver or the overlay before
bring-up), 2 = could not run.

Capture the output: it is the record that discharges the "inspect HWH and
driver" work item's board half, and the first place a renamed instance or a
re-laid-out register map becomes visible (§7.1.2: adding or reordering a port
moves every offset after it).
"""

from __future__ import annotations

import argparse
import sys

# What tme_driver.PLPipeline resolves by name.
EXPECT_CORES: dict[str, set[str]] = {
    "binarize_core_0": {"CTRL", "img_w", "img_h", "threshold"},
    "patch_extract_core_0": {
        "CTRL", "bin_image_1", "bin_image_2", "img_w", "img_h",
        "stride_bytes", "buffer_bytes", "num_cands",
        "sts_flags", "sts_flags_ctrl", "sts_rejected", "sts_rejected_ctrl",
        "sts_processed", "sts_processed_ctrl"},
    "tme_top_0": {
        "CTRL", "patch_w", "patch_h", "templ_w", "templ_h",
        "result_score", "result_score_ctrl", "result_x", "result_x_ctrl",
        "result_y", "result_y_ctrl"},
}

# (instance, needs MM2S sendchannel, needs S2MM recvchannel)
EXPECT_DMAS = [
    ("axi_dma_binarize", True, True),    # gray in / binary out, 26-bit length
    ("dma_pe_data",      True, True),    # candidates in / patch pixels out
    ("dma_pe_meta",      False, True),   # §6.2 metadata records out
    ("axi_dma_patch",    True, False),   # patch pixels -> tme_top
    ("axi_dma_templ",    True, False),   # template pixels -> tme_top
]

FULL_PAGE_BYTES = 9856 * 6400            # 63,078,400 (§2.2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", required=True, metavar="BITFILE")
    args = ap.parse_args()

    try:
        from pynq import Overlay
    except ImportError as exc:
        print(f"CANNOT RUN: pynq is not importable ({exc})")
        return 2

    ol = Overlay(args.overlay)
    failures: list[str] = []

    try:
        from pynq import Clocks
        mhz = float(Clocks.fclk0_mhz)
        print(f"PL clock (measured): {mhz:.4f} MHz ({1000.0 / mhz:.3f} ns)")
        if abs(mhz - 31.25) > 0.01:
            print("  NOTE: contract §8 records 31.25 MHz for the standalone "
                  "image; update §8 if this combined image differs.")
    except Exception as exc:                           # noqa: BLE001
        print(f"PL clock: unreadable ({exc})")

    print("\n=== overlay.ip_dict ===")
    for name in sorted(ol.ip_dict):
        e = ol.ip_dict[name]
        print(f"  {name:<28} {e.get('type', '?'):<44} "
              f"base=0x{e.get('phys_addr', 0):08X} "
              f"range=0x{e.get('addr_range', 0):X}")

    print("\n=== overlay.hierarchy_dict ===")
    if ol.hierarchy_dict:
        for name in sorted(ol.hierarchy_dict):
            print(f"  {name}")
    else:
        print("  (flat design — no hierarchies)")

    print("\n=== HLS core register maps ===")
    for core_name, expected_regs in EXPECT_CORES.items():
        if core_name not in ol.ip_dict:
            failures.append(f"missing IP: {core_name}")
            continue
        ip = getattr(ol, core_name)
        rmap = ip.register_map
        print(f"\n--- {core_name} ---\n{rmap}")
        have = {r for r in dir(rmap) if not r.startswith("_")}
        missing = expected_regs - have
        if missing:
            failures.append(f"{core_name}: register_map lacks {sorted(missing)}"
                            f" — tme_driver.py addresses these by name")

    print("\n=== DMA engines ===")
    for dma_name, want_send, want_recv in EXPECT_DMAS:
        if dma_name not in ol.ip_dict:
            failures.append(f"missing DMA: {dma_name}")
            continue
        dma = getattr(ol, dma_name)
        send = getattr(dma, "sendchannel", None)
        recv = getattr(dma, "recvchannel", None)
        max_send = getattr(dma, "buffer_max_size", None)
        print(f"  {dma_name:<18} sendchannel={'yes' if send else 'no ':<3} "
              f"recvchannel={'yes' if recv else 'no ':<3} "
              f"buffer_max_size={max_send}")
        if want_send and send is None:
            failures.append(f"{dma_name}: MM2S sendchannel missing")
        if want_recv and recv is None:
            failures.append(f"{dma_name}: S2MM recvchannel missing")

    # The one quantitative check: the binarize DMA must carry a full page in
    # ONE transfer (its BD sg_length_width is 26 -> 67,108,863 B).  If PYNQ
    # reports a smaller bound, the full-size gate below cannot pass.
    if "axi_dma_binarize" in ol.ip_dict:
        dma = ol.axi_dma_binarize
        bound = None
        for obj, attr in ((dma, "buffer_max_size"),
                          (getattr(dma, "sendchannel", None), "_max_size")):
            v = getattr(obj, attr, None) if obj is not None else None
            if isinstance(v, int) and v > 0:
                bound = v
                break
        if bound is not None:
            verdict = "OK" if bound >= FULL_PAGE_BYTES else "TOO SMALL"
            print(f"\naxi_dma_binarize single-transfer bound: {bound:,} B "
                  f"vs full page {FULL_PAGE_BYTES:,} B -> {verdict}")
            if bound < FULL_PAGE_BYTES:
                failures.append(
                    f"axi_dma_binarize max transfer {bound:,} B cannot carry "
                    f"a {FULL_PAGE_BYTES:,} B page — sg_length_width in the "
                    f"BD does not match the HWH this was built against")
        else:
            print("\naxi_dma_binarize: PYNQ did not report a transfer bound")

    print()
    if failures:
        print("MISMATCHES:")
        for f in failures:
            print(f"  - {f}")
        print("\nFAIL: the overlay does not match tme_driver.py's "
              "expectations — resolve before driver bring-up.")
        return 1
    print("PASS: overlay matches tme_driver.py's expectations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
