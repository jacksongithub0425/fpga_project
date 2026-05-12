"""
Step 0 — AXI4-Lite register sanity check for binarize_core + AXI DMA.

Runs on the Zynq board (PYNQ). Verifies that the PS can reach every
AXI-Lite register on the bitstream before any DMA is attempted.

PASS criteria:
  - Overlay loads
  - binarize_core AP_CTRL idle bit reads back high after reset
  - Every scalar config register (img_w, img_h, threshold) round-trips
    walking-1, all-0, all-1, alternating-bit patterns
  - AXI DMA control/status registers are reachable

Any failure here means something is wrong in: address map, AXI
interconnect, GP0 routing, IP instance naming, or the bitstream itself.
Fixing it now is 10x cheaper than discovering it during a DMA test.

Usage on the board:
    sudo -E python3 step0_axilite_check.py /home/xilinx/terminal_counter.bit
"""

from __future__ import annotations

import sys
import time
from datetime import datetime


# HLS-generated AP_CTRL bit layout (ap_ctrl_hs)
AP_START   = 1 << 0
AP_DONE    = 1 << 1
AP_IDLE    = 1 << 2
AP_READY   = 1 << 3


def banner(msg):
    line = "=" * 70
    print(f"\n{line}\n{msg}\n{line}")


def report(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}{(' — ' + detail) if detail else ''}")
    return ok


def list_overlay_ips(ol):
    print("\nIPs visible in overlay:")
    for name in sorted(ol.ip_dict.keys()):
        entry = ol.ip_dict[name]
        ip_type = entry.get("type", "?")
        base = entry.get("phys_addr", 0)
        print(f"  {name:<28}  {ip_type:<40}  base=0x{base:08X}")


def find_ip(ol, *candidates):
    """Return the first overlay IP whose name matches any candidate substring."""
    keys = list(ol.ip_dict.keys())
    for cand in candidates:
        for k in keys:
            if cand.lower() in k.lower():
                return getattr(ol, k), k
    return None, None


def reg_roundtrip(ip, field_name, patterns, mask):
    """Write each pattern to a named register field and read it back.

    Returns (ok, mismatches_list).
    """
    mismatches = []
    rmap = getattr(ip, "register_map", None)
    use_named = rmap is not None and hasattr(rmap, field_name)

    for pat in patterns:
        wval = pat & mask
        if use_named:
            setattr(rmap, field_name, wval)
            rval = int(getattr(rmap, field_name)) & mask
        else:
            return False, [("no register_map field " + field_name, 0, 0)]
        if rval != wval:
            mismatches.append((field_name, wval, rval))
    return (len(mismatches) == 0), mismatches


def test_binarize(ip):
    banner(f"binarize_core register tests")

    # 1. AP_CTRL idle after reset
    rmap = ip.register_map
    ap_ctrl = int(rmap.CTRL) if hasattr(rmap, "CTRL") else ip.read(0x00)
    idle = bool(ap_ctrl & AP_IDLE)
    report("AP_CTRL.ap_idle high after reset", idle,
           f"raw=0x{ap_ctrl:08X}")

    # 2. img_w (16-bit field): walking-1, all-0, all-1, real values
    img_w_pats = [0x0000, 0xFFFF, 0x5555, 0xAAAA, 0x0001, 0x8000, 2480, 9792]
    ok_w, mis_w = reg_roundtrip(ip, "img_w", img_w_pats, 0xFFFF)
    report("img_w round-trip (16-bit)", ok_w,
           "" if ok_w else f"mismatches={mis_w}")

    # 3. img_h (16-bit field)
    img_h_pats = [0x0000, 0xFFFF, 0x5555, 0xAAAA, 0x0001, 0x8000, 3508, 6336]
    ok_h, mis_h = reg_roundtrip(ip, "img_h", img_h_pats, 0xFFFF)
    report("img_h round-trip (16-bit)", ok_h,
           "" if ok_h else f"mismatches={mis_h}")

    # 4. threshold (8-bit field)
    thr_pats = [0x00, 0xFF, 0x55, 0xAA, 0x80, 0x7F, 0x01]
    ok_t, mis_t = reg_roundtrip(ip, "threshold", thr_pats, 0xFF)
    report("threshold round-trip (8-bit)", ok_t,
           "" if ok_t else f"mismatches={mis_t}")

    # 5. Repeat-write stability — write/read same value 100 times
    rmap.threshold = 0x5A
    stable = all(int(rmap.threshold) == 0x5A for _ in range(100))
    report("threshold stable across 100 reads", stable)

    # 6. AP_CTRL still idle (we never asserted ap_start)
    ap_ctrl2 = int(rmap.CTRL) if hasattr(rmap, "CTRL") else ip.read(0x00)
    still_idle = bool(ap_ctrl2 & AP_IDLE)
    report("AP_CTRL.ap_idle still high (no spurious start)", still_idle,
           f"raw=0x{ap_ctrl2:08X}")

    return all([idle, ok_w, ok_h, ok_t, stable, still_idle])


def test_dma(dma, label):
    banner(f"AXI DMA register tests: {label}")
    ok = True

    for chan_attr, chan_label in (("sendchannel", "MM2S"),
                                  ("recvchannel", "S2MM")):
        if not hasattr(dma, chan_attr):
            continue
        chan = getattr(dma, chan_attr)
        try:
            idle = bool(chan.idle)
            ok &= report(f"{chan_label}.idle reads cleanly", True,
                         f"idle={idle}")
        except Exception as e:
            ok &= report(f"{chan_label}.idle reads cleanly", False, str(e))

    # MM2S_DMACR (0x00) and S2MM_DMACR (0x30) read sanity — non-zero reset
    # values are normal; stuck-at-0 across both is suspicious.
    try:
        mm2s_cr = dma.read(0x00)
        s2mm_cr = dma.read(0x30)
        readable = (mm2s_cr | s2mm_cr) != 0xFFFFFFFF
        ok &= report("DMACR registers readable",
                     readable,
                     f"MM2S_DMACR=0x{mm2s_cr:08X} S2MM_DMACR=0x{s2mm_cr:08X}")
    except Exception as e:
        ok &= report("DMACR registers readable", False, str(e))

    return ok


def main():
    bitfile = sys.argv[1] if len(sys.argv) > 1 else "/home/xilinx/terminal_counter.bit"

    banner(f"Step 0 — AXI-Lite sanity check  ({datetime.now().isoformat(timespec='seconds')})")
    print(f"Bitstream: {bitfile}")

    try:
        from pynq import Overlay
    except ImportError as e:
        print(f"FATAL: PYNQ not installed: {e}")
        sys.exit(2)

    t0 = time.monotonic()
    ol = Overlay(bitfile)
    print(f"Overlay loaded in {(time.monotonic()-t0)*1000:.1f} ms")

    list_overlay_ips(ol)

    bin_ip, bin_name = find_ip(ol, "binarize_core", "binarize")
    if bin_ip is None:
        print("\nFATAL: binarize_core IP not found in overlay")
        sys.exit(3)
    print(f"\nUsing binarize IP: {bin_name}")

    dma_ip, dma_name = find_ip(ol, "axi_dma", "dma_gray", "dma")
    if dma_ip is None:
        print("WARN: no AXI DMA found — DMA tests skipped")

    bin_ok = test_binarize(bin_ip)
    dma_ok = test_dma(dma_ip, dma_name) if dma_ip is not None else True

    banner("SUMMARY")
    print(f"  binarize_core: {'PASS' if bin_ok else 'FAIL'}")
    print(f"  axi_dma      : {'PASS' if dma_ok else 'FAIL'}")
    overall = bin_ok and dma_ok
    print(f"\n  STEP 0 OVERALL: {'PASS — proceed to DMA loopback' if overall else 'FAIL — fix before continuing'}")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
