#!/usr/bin/env python3
"""Board-side probe for contract §2.2 — can CMA satisfy the image buffers?

RUN THIS ON THE BOARD, AFTER LOADING THE OVERLAY, BEFORE FURTHER INTEGRATION.
§2.2 is an OPEN gate in docs/pl_interface_contract.md and everything
downstream of it assumes the answer is yes:

    9856 x 6400 = 63,078,400 B ~= 60.2 MiB per full-size image buffer,
    and the pipeline needs TWO of them (grayscale + binary), ~120.3 MiB total.

They must be *separately* contiguous.  They do not need to be contiguous with
each other, and this probe does not require that.

**REQUIRED PLATFORM SETTING: `cma=192M`.**  The PYNQ default pool is 128 MiB
against a ~120.8 MiB driver-order requirement, and that 7 MiB margin has been
tried twice and failed twice.  This probe refuses to run below 192 MiB and
exits 2 (misconfigured platform — *not* a §2.2 capacity failure, and not a
reason to trigger the tiling branch).  See REQUIRED_CMA_BYTES below for how
to set it.

**It allocates in the driver's real order**, not just the two big buffers:
`PLPipeline.__init__` takes five smaller regions (candidate, metadata, patch
receive, matcher patch and matcher template) out of the pool BEFORE
`binarize_page()` asks for the two full-page ones.  CMA fragmentation is
order-dependent — a small buffer landing mid-pool is exactly what breaks a
later 60.2 MiB contiguous request — so probing the easy order would answer a
different question than the driver asks.  Sizes are imported from
`tme_driver` so the two cannot drift; if that import fails the probe says so,
falls back to the two-buffer sequence, and reports a **weaker capacity
preflight** (exit 2) rather than claiming the §2.2 gate.

    sudo python3 probe_cma_budget.py --overlay /path/to/terminal_counter.bit

Exit status: 0 = both buffers allocated and usable, 1 = the §2.2 gate FAILS
and tiling becomes a platform requirement (§2.2's stated alternative),
2 = could not run the probe at all.

-----------------------------------------------------------------------------
WHAT A PASS DOES AND DOES NOT PROVE

A PASS is **necessary evidence, not permanent proof**.  It says: at this
moment, in this boot, with the CMA pool in its current fragmentation state and
with the overlay already resident, these two allocations succeed.  It does not
say they will succeed later in the same session, after other allocations have
come and gone, or on the next boot.  CMA hands out physically contiguous
regions, and contiguity is exactly the property that degrades with use.

So: re-run after a reboot, and re-run in the real application's environment
and allocation order before trusting the result.  Which is why --overlay
exists.  Loading a bitstream itself allocates from the pool, and the driver
allocates _cand_buf and _meta_buf as well; probing a pristine pool and then
allocating in a different order in production is how a gate passes here and
fails in the field.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Contract §2 maxima.  Keep in sync with PE_MAX_IMG_W / PE_MAX_IMG_H in
# hls/patch_extract/patch_extract_core.h.
IMG_W = 9856
IMG_H = 6400
BUF_BYTES = IMG_W * IMG_H
MIB = 1024 * 1024
PAGE = 4096

# §3: the linear DDR offset is 32-bit, and patch_extract_core's §2.1 check
# rejects a footprint above 0xFFFFFFFF.  A buffer whose physical range crosses
# 2^32 cannot be addressed by the current contract even if CMA hands it over.
ADDR_LIMIT = 1 << 32

# REQUIRED PLATFORM SETTING: cma=192M on the kernel command line.
#
# This is not a recommendation.  The driver-order allocation needs ~120.8 MiB
# of *separately contiguous* CMA, and the PYNQ default pool is 128 MiB — a
# 7 MiB margin, out of which any fragmentation at all has to come.  **It was
# tried twice at 128 MiB and failed both times**, which is what turned this
# from "tight" into a platform requirement: the pool is not merely close to
# the requirement, it does not reliably satisfy it, and the failure arrives as
# a refused 60.2 MiB allocation partway through construction rather than as
# anything that names the pool size.
#
# 192 MiB leaves ~71 MiB of headroom over the allocation and still leaves the
# board ~290 MiB of userspace, which the row-strip verification in
# board_gate_full_dma.py (measured 98.7 MiB peak) fits inside comfortably.
#
# Set it in /boot/uEnv.txt (or the platform's equivalent) and REBOOT — CMA is
# reserved at boot and cannot be resized afterwards:
#
#     bootargs=... cma=192M
#
# then confirm with `grep Cma /proc/meminfo` before running anything here.
REQUIRED_CMA_BYTES = 192 * 1024 * 1024
DEFAULT_CMA_BYTES = 128 * 1024 * 1024


def _fmt(n: int) -> str:
    return f"{n:,} B ({n / MIB:.1f} MiB)"


def _read_cma_meminfo() -> dict[str, int]:
    """CmaTotal/CmaFree from /proc/meminfo, in bytes.  Empty if unavailable."""
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("CmaTotal", "CmaFree"):
                    out[key] = int(rest.strip().split()[0]) * 1024
    except OSError:
        pass
    return out


def _stamp(buf, seed: int) -> None:
    """Write one distinct byte per 4 KiB page, plus the final byte.

    Touching only the first and last byte proves almost nothing: a pool can
    return a descriptor whose interior is not actually backed, and a
    first/last check walks straight past it.  One byte per page is the
    coarsest stride that still touches every page the allocation claims.

    The value varies with the offset so that a buffer aliasing ANOTHER
    buffer, or wrapping onto itself, produces a mismatch rather than reading
    back a plausible constant.
    """
    n = len(buf)
    for off in range(0, n, PAGE):
        buf[off] = (seed + (off // PAGE)) & 0xFF
    buf[n - 1] = (seed + 0x5A) & 0xFF


def _verify(buf, seed: int, name: str) -> str | None:
    """Read back what _stamp wrote.  Returns an error string, or None."""
    n = len(buf)
    for off in range(0, n, PAGE):
        want = (seed + (off // PAGE)) & 0xFF
        got = int(buf[off])
        if got != want:
            return (f"{name}: page {off // PAGE} (byte offset {off:,}) read "
                    f"back 0x{got:02X}, expected 0x{want:02X}")
    want = (seed + 0x5A) & 0xFF
    if int(buf[n - 1]) != want:
        return (f"{name}: final byte read back 0x{int(buf[n - 1]):02X}, "
                f"expected 0x{want:02X}")
    return None


def driver_allocation_plan() -> tuple[list, str]:
    """The driver's real allocation sequence: (name, nbytes) in order.

    Returns (plan, note).  Fragmentation is order-dependent, so a probe that
    asks only for the two big buffers is answering an easier question than
    the driver asks: PLPipeline.__init__ takes five smaller regions out of
    the pool FIRST, and only then does binarize_page() request the two
    full-page ones.  Those five can land anywhere, including in the middle of
    what would otherwise have been a contiguous 60.2 MiB run.

    Sizes are imported from tme_driver rather than restated, so the probe
    cannot drift from the thing it is meant to predict.  If the driver is not
    importable the caller falls back to the two-buffer probe and must
    describe the result as the weaker preflight it is.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tme_driver import (_CAND_STRUCT_SIZE, _MAX_CANDIDATES,
                            _MAX_PATCH_BYTES, _MAX_TEMPL_H, _MAX_TEMPL_W,
                            _META_STRUCT_SIZE, _OUTPUT_GUARD_BYTES)

    plan = [
        # PLPipeline.__init__, in source order.
        ("cand_buf",      _MAX_CANDIDATES * _CAND_STRUCT_SIZE),
        ("meta_buf",      _MAX_CANDIDATES * _META_STRUCT_SIZE),
        ("patch_rx_buf",  _MAX_PATCH_BYTES),
        ("tme_patch_buf", _MAX_PATCH_BYTES),
        ("tme_templ_buf", _MAX_TEMPL_W * _MAX_TEMPL_H),
        # binarize_page -> _ensure_image_bufs, at the §2 maximum page.
        ("grayscale",     BUF_BYTES),
        ("binary",        BUF_BYTES + _OUTPUT_GUARD_BYTES),
    ]
    small = sum(n for name, n in plan[:5])
    note = (f"driver order: 5 smaller buffers ({_fmt(small)} total) before "
            f"the two full-page ones")
    return plan, note


def fallback_allocation_plan() -> tuple[list, str]:
    """Just the two page buffers — the weaker preflight."""
    return ([("grayscale", BUF_BYTES), ("binary", BUF_BYTES)],
            "two-buffer preflight only (driver order NOT reproduced)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlay", metavar="BITFILE",
                    help="load this overlay before probing, so the pool is in "
                         "the state the real application sees (strongly "
                         "recommended — see the module docstring)")
    ap.add_argument("--allow-small-pool", action="store_true",
                    help="probe anyway on a pool below the required "
                         "cma=192M. The result is NOT the §2.2 gate and "
                         "labels itself so.")
    args = ap.parse_args()

    print("contract §2.2 CMA probe")
    print(f"  target: 2 x {_fmt(BUF_BYTES)}  (image {IMG_W} x {IMG_H})")
    print(f"  required pool: cma=192M ({_fmt(REQUIRED_CMA_BYTES)})")

    info = _read_cma_meminfo()
    if info:
        total = info.get("CmaTotal", 0)
        print(f"  CmaTotal: {_fmt(total)}")
        print(f"  CmaFree : {_fmt(info.get('CmaFree', 0))}")
        if total < REQUIRED_CMA_BYTES:
            near_default = abs(total - DEFAULT_CMA_BYTES) < 8 * MIB
            print("\n" + "!" * 72)
            print(f"PLATFORM NOT CONFIGURED: CmaTotal is {_fmt(total)}, "
                  f"below the required {_fmt(REQUIRED_CMA_BYTES)}.")
            if near_default:
                print("That is the PYNQ default 128 MiB pool. The "
                      "driver-order allocation needs ~120.8 MiB of separately")
                print("contiguous CMA and HAS BEEN TRIED TWICE AT 128 MiB, "
                      "failing both times — the 7 MiB margin does not")
                print("survive fragmentation. This is a misconfigured "
                      "platform, NOT a §2.2 capacity failure, and it must")
                print("not be recorded as one or used to trigger the tiling "
                      "branch.")
            print("REMEDY: add `cma=192M` to the kernel command line "
                  "(/boot/uEnv.txt bootargs) and REBOOT — CMA is")
            print("reserved at boot and cannot be resized afterwards. Then "
                  "`grep Cma /proc/meminfo` to confirm.")
            print("!" * 72)
            if not args.allow_small_pool:
                print("\nNot probing. Re-run after the reboot, or pass "
                      "--allow-small-pool to probe anyway (whose result is "
                      "not the gate).")
                return 2
            print("\n--allow-small-pool: probing anyway. WHATEVER THIS "
                  "PRINTS IS NOT THE §2.2 GATE.")
        if total < 2 * BUF_BYTES:
            print("  NOTE: CmaTotal is below the two image buffers outright — "
                  "the allocation below cannot succeed at all.")
    else:
        print("  CmaTotal/CmaFree: unavailable (not Linux, or no CMA)")
        print("  NOTE: the required cma=192M setting could not be verified "
              "from /proc/meminfo.")

    try:
        from pynq import allocate
    except ImportError as exc:
        print(f"\nCANNOT RUN: pynq is not importable ({exc}).")
        print("This probe is meaningless off the board — it is measuring the "
              "board's CMA pool, not this machine's RAM.")
        return 2

    overlay = None
    if args.overlay:
        try:
            from pynq import Overlay
            overlay = Overlay(args.overlay)
            print(f"\n  overlay loaded: {args.overlay}")
            after = _read_cma_meminfo()
            if after:
                print(f"  CmaFree after overlay: "
                      f"{_fmt(after.get('CmaFree', 0))}")
        except Exception as exc:                 # noqa: BLE001 — report anything
            print(f"\nCANNOT RUN: overlay {args.overlay} failed to load "
                  f"({exc}).")
            print("Probing without it would measure the wrong pool state; "
                  "fix the overlay or drop --overlay deliberately.")
            return 2
    else:
        print("\n  WARNING: probing WITHOUT an overlay.  The bitstream itself "
              "allocates from this pool, so a pass here is weaker evidence "
              "than a pass with --overlay.")

    # Reproduce the DRIVER'S allocation sequence, not a convenient one.
    try:
        plan, plan_note = driver_allocation_plan()
        faithful = True
    except Exception as exc:                     # noqa: BLE001
        plan, plan_note = fallback_allocation_plan()
        faithful = False
        print(f"\n  WARNING: could not import the driver's buffer sizes "
              f"({exc}).")
        print("  Falling back to allocating only the two full-page buffers. "
              "CMA fragmentation is ORDER-DEPENDENT, so this asks an easier "
              "question than the driver does and the result below is a "
              "weaker capacity preflight, not the §2.2 gate.")
    print(f"\n  allocation plan — {plan_note}")
    for name, nbytes in plan:
        print(f"    {name:<14} {_fmt(nbytes)}")

    bufs: list = []
    names = tuple(name for name, _ in plan)
    try:
        for name, nbytes in plan:
            try:
                buf = allocate(shape=(nbytes,), dtype="u1")
            except Exception as exc:             # noqa: BLE001 — report anything
                print(f"\nFAIL: {name} buffer ({_fmt(nbytes)}) — {exc}")
                if bufs:
                    print(f"  ({len(bufs)} earlier buffer(s) were already "
                          f"held when this one failed — that is the point of "
                          f"probing in the driver's order.)")
                return _fail_report()
            bufs.append(buf)
            base = buf.physical_address
            print(f"\n  {name:<14} allocated  phys=0x{base:X}..0x"
                  f"{base + nbytes - 1:X}  {_fmt(nbytes)}")

            # §3 / §2.1: the linear offset is 32-bit.  A region above 4 GiB is
            # unusable under the current address contract even though CMA was
            # perfectly willing to hand it over.
            end = base + nbytes
            if end > ADDR_LIMIT:
                print(f"FAIL: {name} ends at 0x{end:X}, past the 2^32 limit "
                      f"the 32-bit linear-offset contract assumes (§2.1, §3). "
                      f"Either the address registers widen to 64-bit "
                      f"end-to-end or this allocation cannot be used.")
                return _fail_report()

        # Overlap, checked across every pair.  Two allocators, two
        # descriptors, one region is a real failure mode and neither buffer's
        # own readback would notice it: each would find its own last write
        # intact.  The distinct seeds below are what make aliasing visible.
        for i in range(len(bufs)):
            for j in range(i + 1, len(bufs)):
                pa, na = bufs[i].physical_address, plan[i][1]
                pb, nb = bufs[j].physical_address, plan[j][1]
                if pa < pb + nb and pb < pa + na:
                    print(f"\nFAIL: {names[i]} and {names[j]} OVERLAP — "
                          f"0x{pa:X}..0x{pa + na - 1:X} and "
                          f"0x{pb:X}..0x{pb + nb - 1:X}. They must be "
                          f"independent regions.")
                    return _fail_report()
        pg, pb_ = (bufs[-2].physical_address, bufs[-1].physical_address)
        print(f"\n  no overlap between any pair; gap between the two "
              f"full-page regions: 0x{abs(pg - pb_) - BUF_BYTES:X} B")
        print("  (they need not be adjacent — §2.2 requires two separately "
              "contiguous regions, not one 120.3 MiB region)")

        # Stamp BOTH buffers with distinct seeds before verifying EITHER, so
        # that an aliased pair fails: the second stamp would overwrite the
        # first, and the first buffer's readback then finds the wrong seed.
        # Verifying each buffer immediately after stamping it would pass.
        print(f"\n  stamping one byte per {PAGE} B page across all "
              f"{len(bufs)} buffers...")
        for i, buf in enumerate(bufs):
            _stamp(buf, seed=0xA5 + i * 0x11)

        # Flush before readback.  These buffers are written by the CPU here
        # but read by the PL by physical address in production, and the same
        # cache ownership question applies to our own readback: without the
        # flush we may be reading our own dirty cache lines rather than the
        # memory we are trying to test.
        # A failed flush or invalidate is NOT a warning to continue past. The
        # readback below would then be served from the CPU's own dirty cache
        # lines and would pass without touching the memory under test — which
        # is worse than not running, because it prints PASS. Report "could not
        # verify" (exit 2) instead.
        for buf, name in zip(bufs, names):
            try:
                buf.flush()
            except Exception as exc:             # noqa: BLE001
                print(f"\nCANNOT VERIFY: flush() failed on the {name} buffer "
                      f"({exc}).")
                print("Without a flush the readback may be answered from "
                      "cache, so a PASS would prove nothing about the "
                      "underlying memory. Not reporting a result.")
                return 2
        for buf, name in zip(bufs, names):
            try:
                buf.invalidate()
            except Exception as exc:             # noqa: BLE001
                print(f"\nCANNOT VERIFY: invalidate() failed on the {name} "
                      f"buffer ({exc}).")
                print("Same reason as flush: the readback would not be "
                      "trustworthy. Not reporting a result.")
                return 2
        print(f"  flushed and invalidated all {len(bufs)} buffers")

        for i, (buf, name) in enumerate(zip(bufs, names)):
            err = _verify(buf, seed=0xA5 + i * 0x11, name=name)
            if err:
                print(f"\nFAIL: {err}")
                print("The allocation succeeded but the memory does not hold "
                      "what was written to it. If several buffers report "
                      "this, suspect aliasing; if one does, suspect an "
                      "unbacked region.")
                return _fail_report()
            print(f"  {name:<14} verified  ({max(1, len(buf) // PAGE):,} pages)")

        after = _read_cma_meminfo()
        if after:
            print(f"\n  CmaFree with both held: "
                  f"{_fmt(after.get('CmaFree', 0))}")

        small_pool = bool(info) and info.get("CmaTotal", 0) < REQUIRED_CMA_BYTES
        if small_pool:
            print("\nNOT THE §2.2 GATE — this ran under --allow-small-pool on "
                  "a pool below the required cma=192M. A pass here says the "
                  "allocation happened to succeed once; it says nothing "
                  "about a platform that has already failed this twice at "
                  "128 MiB. Fix the kernel argument and re-run.")
            return 2
        if faithful:
            print("\n§2.2 GATE PASSES — for this boot, this pool state, and "
                  "the driver's own allocation order.")
        else:
            print("\nWEAKER CAPACITY PREFLIGHT PASSES — the two full-page "
                  "buffers can be allocated, but NOT in the driver's order "
                  "(its five smaller buffers were never taken out of the "
                  "pool first). Do NOT record this as the §2.2 gate: copy "
                  "tme_driver.py next to this script and re-run.")
        if not args.overlay:
            print("Re-run with --overlay before recording it; a no-overlay "
                  "pass is the weaker result.")
        print("The driver allocates its image buffers lazily, at the actual "
              "page size, so a page smaller than the §2 maximum probed here "
              "asks less of the pool than this run did.")
        return 0 if faithful else 2
    finally:
        for buf in bufs:
            try:
                buf.freebuffer()
            except Exception:                    # noqa: BLE001 — best effort
                pass
        if overlay is not None:
            try:
                overlay.free()
            except Exception:                    # noqa: BLE001 — best effort
                pass


def _fail_report() -> int:
    print("\n§2.2 GATE FAILS.")
    print("Do not continue with integration against a full-size image. Per "
          "§2.2 the choice is explicit: either tile / stream the image (which "
          "changes the extractor's design again), or lower the render zoom "
          "and regenerate AND revalidate every template, since template pixel "
          "dimensions are tied to render scale. Neither is free; pick one "
          "deliberately.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
