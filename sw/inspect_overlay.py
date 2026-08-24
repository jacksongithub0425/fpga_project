#!/usr/bin/env python3
"""Board-side introspection of the three_stage_combined overlay.

RUN THIS ON THE BOARD, after the CMA probe, before any driver bring-up:

    sudo -E python3 inspect_overlay.py --overlay /home/xilinx/three_stage_combined.bit
    sudo -E python3 inspect_overlay.py --overlay ... --variant combined_b2_100

It prints overlay.ip_dict, overlay.hierarchy_dict, the PL clock, each HLS
core's register_map and each DMA's channel configuration -- and CHECKS them
against what sw/tme_driver.py resolves by name and what sw/board_expect.py
pins for the selected VARIANT.  Exit status: 0 = the overlay matches, 1 =
mismatch (fix the driver or the overlay before bring-up), 2 = could not run.

Capture the output: it is the record that discharges the "inspect HWH and
driver" work item's board half, and the first place a renamed instance or a
re-laid-out register map becomes visible (contract 7.1.2: adding or reordering
a port moves every offset after it).

WHAT --variant CHANGES, AND WHY IT HAD TO EXIST
-----------------------------------------------
Three of this script's checks used to be literals of the shipping 2026-08-11
image, and each becomes wrong -- silently, and in the permissive direction --
the moment a different build is loaded:

* the PL clock.  It was a NOTE ("contract 8 records 31.25 MHz"), never a gate,
  so a 100 MHz build that landed on the 62.5 MHz divisor trap would have
  printed a friendly note and exited 0.  It is now a HARD GATE against the
  variant's derived board frequency, and it is the only place in the whole
  toolchain where the board's real clock is established: every Vivado report
  in the build agrees on 10.000 ns whether or not the board runs 100 MHz.
* the matcher VLNV.  Nothing checked it here, so a bitstream built from the
  wrong tme_top would pass every name-based check in this file.
* the register offsets.  Names were checked, offsets were not, so an ABI
  drift that moved every offset after a new port would have passed too.

`--variant baseline` is the default and reproduces the old expectations
exactly, so an un-parameterised invocation keeps its old meaning.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_expect as X                                       # noqa: E402

# What tme_driver.PLPipeline resolves by name.  Derived from board_expect's
# pinned register map rather than re-transcribed: two copies of this list is
# how the driver's expectations and the board check drift apart.
EXPECT_CORES: dict[str, set[str]] = {
    core: set(regs) for core, regs in X.REGISTER_MAP.items()
}

# (instance, needs MM2S sendchannel, needs S2MM recvchannel)
EXPECT_DMAS = [
    (name, cfg["mm2s"], cfg["s2mm"]) for name, cfg in X.DMA_PARAMS.items()
]

FULL_PAGE_BYTES = X.FULL_PAGE_BYTES          # 63,078,400 (contract 2.2)


def _check_clock(cfg: dict, failures: list[str]) -> None:
    """The live clock gate.  This is the one check silicon alone can make.

    Vivado's period says nothing about the board: it is
    `PCW_IO_IO_PLL_FREQMHZ / (div0 * div1)` against Vivado's own PS7 model,
    while the board runs `1000 / (div0 * div1)` against the IO PLL PYNQ's boot
    image programmed.  A bare 100 MHz request produces a flawless 10.000 ns
    constraint and a board that runs 62.5 MHz, and NOTHING in the build
    catches it -- the shipped HWH divisor check predicts it, this measures it.

    WHY FCLK1 IS GATED TOO, AND WHY THAT IS NOT BELT-AND-BRACES.  On this
    board PYNQ's power-on default for fclk0 is **100.0 MHz** -- the very value
    the 100 MHz variants require.  So for those variants an fclk0-only gate is
    fail-OPEN in one specific and entirely plausible way: an overlay that
    never programmed the clocks at all would read 100.0 and pass.  fclk1 is
    what closes it.  The 100 MHz recipe only works because FCLK1 is enabled at
    125 MHz (that is what forces the PS7 solver off its 1600 MHz IO PLL
    model), and PYNQ's default fclk1 is 142.857143 -- a different number.  So
    fclk1 reading 125.0 is positive evidence that THIS overlay's divisors were
    applied, which is exactly what fclk0 cannot supply here.
    """
    want = cfg["board_mhz"]
    try:
        from pynq import Clocks
        mhz = float(Clocks.fclk0_mhz)
    except Exception as exc:                           # noqa: BLE001
        # Unreadable is a FAILURE, not a note.  "Could not verify" must never
        # read as "verified" for the one measurement that decides whether the
        # board is running the frequency the whole qualification assumes.
        print(f"PL clock: UNREADABLE ({type(exc).__name__}: {exc})")
        failures.append(
            f"PL clock could not be read, so the variant's {want:.4f} MHz "
            f"is unverified; treat this as a clock failure, not a note")
        return

    delta = abs(mhz - want)
    ok = delta <= X.CLOCK_TOL_MHZ
    print(f"PL clock (measured): {mhz!r} MHz = {mhz:.4f} MHz "
          f"({1000.0 / mhz:.3f} ns)")
    print(f"  variant {cfg['name']}: divisor product {cfg['div_product']} -> "
          f"1000/{cfg['div_product']} = {want:.4f} MHz expected on the board; "
          f"Vivado constrained {cfg['period_ns']:.3f} ns against a "
          f"{cfg['io_pll_mhz']:.0f} MHz IO PLL model")
    # Name the neighbouring rungs so a failure reads as a divisor, not a
    # mystery: the 100 MHz trap lands on div 16 and reads 62.5.
    near = min(range(1, 65),
               key=lambda d: abs(X.BOARD_IO_PLL_MHZ / d - mhz))
    print(f"  measured {mhz:.4f} MHz is 1000/{near} "
          f"(= {X.BOARD_IO_PLL_MHZ / near:.4f}); "
          f"delta from expected {delta:.6f} MHz, tolerance "
          f"{X.CLOCK_TOL_MHZ:g}")
    print(f"LIVE_FCLK0_MHZ={mhz!r};EXPECTED={want!r};DIV_PRODUCT="
          f"{cfg['div_product']};GATE=live_clock;RESULT={'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append(
            f"live PL clock {mhz:.4f} MHz != variant {cfg['name']}'s "
            f"{want:.4f} MHz (divisor product {cfg['div_product']}); the "
            f"measured value corresponds to divisor product {near}. Do NOT "
            f"continue to staged board qualification -- every cycle figure "
            f"downstream is scaled by this number")

    # fclk1: for the 100 MHz variants this is the check that is not fail-open.
    try:
        from pynq import Clocks
        mhz1 = float(Clocks.fclk1_mhz)
    except Exception as exc:                           # noqa: BLE001
        mhz1 = None
        print(f"PL fclk1: UNREADABLE ({type(exc).__name__}: {exc})")
    if cfg["fclk1_enabled"]:
        want1 = X.board_mhz(cfg["fclk1_div_product"])
        ok1 = mhz1 is not None and abs(mhz1 - want1) <= X.CLOCK_TOL_MHZ
        print(f"PL fclk1 (measured): {mhz1!r} MHz; variant expects "
              f"{want1:.4f} MHz (1000/{cfg['fclk1_div_product']}). PYNQ's "
              f"default is 142.857143, so this reading is positive evidence "
              f"that THIS overlay's divisors were applied -- which fclk0 "
              f"cannot give when its target equals the board default.")
        print(f"LIVE_FCLK1_MHZ={mhz1!r};EXPECTED={want1!r};DIV_PRODUCT="
              f"{cfg['fclk1_div_product']};GATE=live_fclk1;"
              f"RESULT={'PASS' if ok1 else 'FAIL'}")
        if not ok1:
            failures.append(
                f"live fclk1 {mhz1} MHz != variant {cfg['name']}'s "
                f"{want1:.4f} MHz (divisor product "
                f"{cfg['fclk1_div_product']}). For this variant fclk0 alone "
                f"proves nothing -- its target equals PYNQ's power-on "
                f"default -- so a wrong fclk1 means the overlay's clock "
                f"configuration did not take effect")
    else:
        print(f"PL fclk1 (measured): {mhz1!r} MHz -- variant "
              f"{cfg['name']} does not drive FCLK1, so this is recorded, "
              f"not gated")


def _check_vlnv(ol, cfg: dict, failures: list[str]) -> None:
    """The matcher is the only IP that differs between these variants."""
    entry = ol.ip_dict.get("tme_top_0")
    if entry is None:
        return                                   # already reported as missing
    got = entry.get("type")
    want = cfg["matcher_vlnv"]
    ok = got == want
    print(f"\nmatcher VLNV: {got}")
    print(f"MATCHER_VLNV={got};EXPECTED={want};GATE=matcher_vlnv;"
          f"RESULT={'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append(
            f"tme_top_0 is {got!r}, but variant {cfg['name']} requires "
            f"{want!r} -- this overlay was built from a different matcher")


def gate_identity_and_clock(ol, variant_name: str) -> list[str]:
    """The two live checks a production run must pass BEFORE its first page.

    Returns the failure list; empty means the board is running the build the
    caller thinks it is.  Both halves are here because either one alone is
    fail-open for these variants:

      * the VLNV alone cannot tell a B2 bitstream clocked at 62.5 MHz from
        one at 100, and every performance figure downstream is scaled by
        that;
      * the clock alone cannot tell B2 from the baseline matcher, and
        `Clocks.fclk0_mhz` at 100.0 is ALSO PYNQ's power-on default -- which
        is why `_check_clock` gates fclk1 as well for the 100 MHz variants.

    Split out of `main()` so `terminal_counter_endpoint_first.py` and
    `tme_backend_parity.py` gate on exactly the same code as the preflight
    rather than on a second, drifting copy of the constants.
    """
    cfg = X.variant(variant_name)
    failures: list[str] = []
    print(f"\n=== build identity gate: variant {cfg['name']} ===")
    _check_vlnv(ol, cfg, failures)
    _check_clock(cfg, failures)
    return failures


def _check_addresses(ol, failures: list[str]) -> None:
    """Base address and range of every instance the driver touches.

    Pinned from the shipped HWH and identical across all three variants: the
    B2 swap was required to preserve the address map, and this is what makes
    that a checked property rather than an assertion in a document.
    """
    print("\n=== address map (pinned) ===")
    for inst in sorted(X.ADDRESS_MAP):
        want_base, want_range = X.ADDRESS_MAP[inst]
        e = ol.ip_dict.get(inst)
        if e is None:
            continue                              # already reported as missing
        base = e.get("phys_addr")
        rng = e.get("addr_range")
        ok = (base == want_base and rng == want_range)
        print(f"  {inst:<24} base=0x{base:08X} range=0x{rng:X} "
              f"expect base=0x{want_base:08X} range=0x{want_range:X} "
              f"-> {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(
                f"{inst}: base/range 0x{base:08X}/0x{rng:X} != pinned "
                f"0x{want_base:08X}/0x{want_range:X}")


def _check_register_offsets(ol, failures: list[str]) -> None:
    """Not just the names -- the OFFSETS.

    A name check passes an IP whose ports were reordered, because HLS keeps
    the names and moves the addresses.  The driver writes offsets.
    """
    print("\n=== register offsets (pinned) ===")
    for core, want_regs in X.REGISTER_MAP.items():
        e = ol.ip_dict.get(core)
        if e is None:
            continue
        regs = e.get("registers") or {}
        if not regs:
            failures.append(
                f"{core}: ip_dict carries no 'registers' -- the .hwh has no "
                f"register data for this IP, so the offsets cannot be checked")
            print(f"  {core:<24} (no register data in ip_dict)")
            continue
        bad = []
        for name, want_off in sorted(want_regs.items(), key=lambda kv: kv[1]):
            got = regs.get(name, {}).get("address_offset")
            if got != want_off:
                bad.append(f"{name} at "
                           f"{'absent' if got is None else hex(got)} "
                           f"!= {hex(want_off)}")
        extra = sorted(set(regs) - set(want_regs))
        print(f"  {core:<24} {len(want_regs)} pinned offsets -> "
              f"{'OK' if not bad else 'MISMATCH'}"
              + (f"; ip_dict also carries {extra}" if extra else ""))
        for b in bad:
            print(f"      {b}")
        if bad:
            failures.append(f"{core}: register offsets moved: {'; '.join(bad)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", required=True, metavar="BITFILE")
    ap.add_argument("--variant", default=X.DEFAULT_VARIANT,
                    choices=sorted(X.VARIANTS),
                    help="which build's expectations to check against "
                         f"(default: {X.DEFAULT_VARIANT})")
    args = ap.parse_args()

    try:
        cfg = X.variant(args.variant)
    except Exception as exc:                           # noqa: BLE001
        print(f"CANNOT RUN: {exc}")
        return 2

    print(f"variant={cfg['name']}  matcher={cfg['matcher_vlnv']}  "
          f"expected board fclk0={cfg['board_mhz']:.4f} MHz "
          f"(div product {cfg['div_product']}), Vivado period "
          f"{cfg['period_ns']:.3f} ns\n")

    try:
        from pynq import Overlay
    except ImportError as exc:
        print(f"CANNOT RUN: pynq is not importable ({exc})")
        return 2

    # Loading the overlay is an environment step, not a check: a missing .bit
    # or an absent .hwh sidecar must exit 2, because the runbook reads a 1
    # from this script as "the overlay and the driver disagree -- go fix
    # _CORE_NAMES or the block design", which would be the wrong hunt
    # entirely for a file that was never copied.
    try:
        ol = Overlay(args.overlay)
    except Exception as exc:                           # noqa: BLE001
        print(f"CANNOT RUN: could not load {args.overlay} "
              f"({type(exc).__name__}: {exc}) -- check that the .bit and its "
              f"matching .hwh are both present, with the same basename")
        return 2
    failures: list[str] = []

    _check_clock(cfg, failures)

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
        print("  (flat design -- no hierarchies)")

    print("\n=== HLS core register maps ===")
    for core_name, expected_regs in EXPECT_CORES.items():
        if core_name not in ol.ip_dict:
            failures.append(f"missing IP: {core_name}")
            continue
        ip = getattr(ol, core_name)
        try:
            rmap = ip.register_map
        except Exception as exc:                       # noqa: BLE001
            failures.append(
                f"{core_name}: register_map unavailable "
                f"({type(exc).__name__}: {exc}) -- the .hwh carries no "
                f"register data for this IP, so the driver cannot address "
                f"it by name")
            continue
        print(f"\n--- {core_name} ---\n{rmap}")
        have = {r for r in dir(rmap) if not r.startswith("_")}
        missing = expected_regs - have
        if missing:
            failures.append(f"{core_name}: register_map lacks {sorted(missing)}"
                            f" -- tme_driver.py addresses these by name")

    _check_vlnv(ol, cfg, failures)
    _check_addresses(ol, failures)
    _check_register_offsets(ol, failures)

    # NOTE, and it is load-bearing for whatever runs next: resolving
    # `overlay.<dma>` constructs `pynq.lib.dma.DMA`, whose channel
    # constructors write `DMACR.RS = 1`.  So the seven engines are RUNNING
    # from this point on -- measured 2026-08-21, all seven went
    # 0x00010002/0x00000001 -> 0x00010003/0x00000000 with no transfer asked
    # for.  Nothing here can inspect a DMA without that happening, which is
    # why `board_idle_check.py` reloads the overlay and uses raw MMIO instead
    # of judging the state this script leaves behind.
    print("\n=== DMA engines ===")
    print("  (resolving these constructs PYNQ's DMA driver, which STARTS "
          "each channel; the idle gate reloads the overlay for that reason)")
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
        if not want_send and send is not None:
            failures.append(f"{dma_name}: has an MM2S sendchannel it should "
                            f"not -- this is not the block design the driver "
                            f"was written against")
        if not want_recv and recv is not None:
            failures.append(f"{dma_name}: has an S2MM recvchannel it should "
                            f"not -- this is not the block design the driver "
                            f"was written against")

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
            ok_bound = bound >= FULL_PAGE_BYTES
            verdict = "OK" if ok_bound else "TOO SMALL"
            print(f"\naxi_dma_binarize single-transfer bound: {bound:,} B "
                  f"vs full page {FULL_PAGE_BYTES:,} B -> {verdict}")
            print(f"BINARIZE_BOUND_BYTES={bound};REQUIRED={FULL_PAGE_BYTES};"
                  f"GATE=binarize_transfer_bound;"
                  f"RESULT={'PASS' if ok_bound else 'FAIL'}")
            if bound < FULL_PAGE_BYTES:
                failures.append(
                    f"axi_dma_binarize max transfer {bound:,} B cannot carry "
                    f"a {FULL_PAGE_BYTES:,} B page -- sg_length_width in the "
                    f"BD does not match the HWH this was built against")
        else:
            # Unavailable is a FAILURE, not a note.  This was fail-open: with
            # no bound reported the gate printed a line and exited 0, so an
            # overlay whose binarize DMA cannot carry a page in one transfer
            # would pass here and die in gate 3 instead -- which is the same
            # mistake the clock check used to make, in the same direction.
            # "Could not verify" must never read as "verified".
            print("\naxi_dma_binarize: PYNQ did not report a transfer bound "
                  "-> CANNOT VERIFY")
            print(f"BINARIZE_BOUND_BYTES=none;REQUIRED={FULL_PAGE_BYTES};"
                  f"GATE=binarize_transfer_bound;RESULT=CANNOT_VERIFY")
            failures.append(
                f"axi_dma_binarize: PYNQ reported no single-transfer bound, "
                f"so the {FULL_PAGE_BYTES:,} B one-transfer page requirement "
                f"is UNVERIFIED. Not a pass: gate 3 depends on this and "
                f"neither buffer_max_size nor sendchannel._max_size answered")

    print()
    if failures:
        print("MISMATCHES:")
        for f in failures:
            print(f"  - {f}")
        print(f"\nFAIL: the overlay does not match variant "
              f"{cfg['name']}'s expectations -- resolve before driver "
              f"bring-up.")
        return 1
    print(f"PASS: overlay matches variant {cfg['name']} and "
          f"tme_driver.py's expectations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
