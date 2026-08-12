#!/usr/bin/env python3
"""Board-side probe for contract §2.2 — can CMA satisfy the image buffers?

RUN THIS ON THE BOARD, AFTER LOADING THE OVERLAY, BEFORE FURTHER INTEGRATION.
§2.2 is an OPEN gate in docs/pl_interface_contract.md and everything
downstream of it assumes the answer is yes:

    9856 x 6400 = 63,078,400 B ~= 60.2 MiB per full-size image buffer,
    and the pipeline needs TWO of them (grayscale + binary), ~120.3 MiB total.

They must be *separately* contiguous.  They do not need to be contiguous with
each other, and this probe does not require that.

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlay", metavar="BITFILE",
                    help="load this overlay before probing, so the pool is in "
                         "the state the real application sees (strongly "
                         "recommended — see the module docstring)")
    args = ap.parse_args()

    print("contract §2.2 CMA probe")
    print(f"  target: 2 x {_fmt(BUF_BYTES)}  (image {IMG_W} x {IMG_H})")

    info = _read_cma_meminfo()
    if info:
        print(f"  CmaTotal: {_fmt(info.get('CmaTotal', 0))}")
        print(f"  CmaFree : {_fmt(info.get('CmaFree', 0))}")
        if info.get("CmaTotal", 0) < 2 * BUF_BYTES:
            print("  NOTE: CmaTotal is below the requirement outright — the "
                  "allocation below cannot succeed without raising the "
                  "cma= kernel parameter.")
    else:
        print("  CmaTotal/CmaFree: unavailable (not Linux, or no CMA)")

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

    bufs: list = []
    names = ("grayscale", "binary")
    try:
        for i, name in enumerate(names):
            try:
                buf = allocate(shape=(BUF_BYTES,), dtype="u1")
            except Exception as exc:             # noqa: BLE001 — report anything
                print(f"\nFAIL: {name} buffer ({_fmt(BUF_BYTES)}) — {exc}")
                return _fail_report()
            bufs.append(buf)
            base = buf.physical_address
            print(f"\n  {name:<10} allocated  phys=0x{base:X}..0x"
                  f"{base + BUF_BYTES - 1:X}  {_fmt(BUF_BYTES)}")

            # §3 / §2.1: the linear offset is 32-bit.  A region above 4 GiB is
            # unusable under the current address contract even though CMA was
            # perfectly willing to hand it over.
            end = base + BUF_BYTES
            if end > ADDR_LIMIT:
                print(f"FAIL: {name} ends at 0x{end:X}, past the 2^32 limit "
                      f"the 32-bit linear-offset contract assumes (§2.1, §3). "
                      f"Either the address registers widen to 64-bit "
                      f"end-to-end or this allocation cannot be used.")
                return _fail_report()

        # Overlap.  Two allocators, two descriptors, one region is a real
        # failure mode and neither buffer's own readback would notice it: each
        # would find its own last write intact.  The distinct seeds below are
        # what make aliasing visible.
        pa, pb = bufs[0].physical_address, bufs[1].physical_address
        if pa < pb + BUF_BYTES and pb < pa + BUF_BYTES:
            print(f"\nFAIL: the two buffers OVERLAP — "
                  f"0x{pa:X}..0x{pa + BUF_BYTES - 1:X} and "
                  f"0x{pb:X}..0x{pb + BUF_BYTES - 1:X}. They must be two "
                  f"independent regions.")
            return _fail_report()
        print(f"\n  no overlap; gap between regions: "
              f"0x{abs(pa - pb) - BUF_BYTES:X} B")
        print("  (they need not be adjacent — §2.2 requires two separately "
              "contiguous regions, not one 120.3 MiB region)")

        # Stamp BOTH buffers with distinct seeds before verifying EITHER, so
        # that an aliased pair fails: the second stamp would overwrite the
        # first, and the first buffer's readback then finds the wrong seed.
        # Verifying each buffer immediately after stamping it would pass.
        print(f"\n  stamping one byte per {PAGE} B page "
              f"({BUF_BYTES // PAGE:,} pages per buffer)...")
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
        print("  flushed and invalidated both buffers")

        for i, (buf, name) in enumerate(zip(bufs, names)):
            err = _verify(buf, seed=0xA5 + i * 0x11, name=name)
            if err:
                print(f"\nFAIL: {err}")
                print("The allocation succeeded but the memory does not hold "
                      "what was written to it. If both buffers report this, "
                      "suspect aliasing; if one does, suspect an unbacked "
                      "region.")
                return _fail_report()
            print(f"  {name:<10} verified  ({BUF_BYTES // PAGE:,} pages)")

        after = _read_cma_meminfo()
        if after:
            print(f"\n  CmaFree with both held: "
                  f"{_fmt(after.get('CmaFree', 0))}")

        print("\n§2.2 GATE PASSES — for this boot, this pool state, and this "
              "allocation order.")
        if not args.overlay:
            print("Re-run with --overlay before recording it; a no-overlay "
                  "pass is the weaker result.")
        print("Note the driver still allocates 2560 x 3600 (~8.8 MiB) each — "
              "raising PLPipeline's max_img to the §2 maxima is a separate "
              "change, and it must allocate in the same order probed here.")
        return 0
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
