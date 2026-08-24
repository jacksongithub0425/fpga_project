#!/usr/bin/env python3
"""What a given build VARIANT must look like on the board, pinned.

The build side already parameterises itself: `three_stage_combined/scripts/
run_postextract_signoff.tcl` carries a `variants` dict keyed by
`B2PROD_VARIANT`, and every Vivado gate reads its matcher VLNV, requested
FCLK0, period and divisor product from there.  This module is the **board**
half of that table, and it exists for one reason: every board-side check that
used to be a literal (`31.25`, `TermCount:hls:tme_top:0.2`, 20 ns) silently
becomes wrong the moment a variant is loaded, and a check that is wrong in the
permissive direction is worse than no check.

Two frequencies, never one.  Vivado constrains `clk_fpga_0` at
`PCW_IO_IO_PLL_FREQMHZ / (div0 * div1)`; the board runs it at
`1000 MHz / (div0 * div1)`, because PYNQ applies the HWH's raw divisors
against the 1000 MHz IO PLL its own boot image programmed.  The two agree only
when Vivado's IO PLL model is also 1000.  The shipping baseline is the proof:
it *requests* 50 MHz, Vivado constrains 20.000 ns, and the board measures
31.25 = 1000/32.  So the table carries `request_mhz` (what the build asked
for), `period_ns` (what Vivado constrained) and `board_mhz` (what
`Clocks.fclk0_mhz` must read) as three separate fields, and only the third one
is what a live gate may compare against.

`board_mhz` is not stored as an independent number either -- it is DERIVED
from `div_product` by `board_mhz()` below and cross-checked against the stored
value, so a table entry cannot claim a frequency its divisors do not produce.

The address map and register offsets are pinned from the shipped HWH files and
are IDENTICAL in the baseline and the B2 build: only `tme_top_0`'s VLNV
differs (`TermCount` -> `TermCountB2`).  That is the whole point of pinning
them -- "we preserved the register map" is a claim, and this is what turns it
into something the board can refuse.

Importable without PYNQ and without a board.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Clock law.  1000 MHz is the IO PLL frequency PYNQ's boot image programmes;
# our bitstream cannot move it, so the board frequency of ANY build in this
# project is 1000 / (div0 * div1).
BOARD_IO_PLL_MHZ = 1000.0

# Tolerance for the live clock comparison, in MHz.  This is deliberately about
# float representation and nothing else: 1000/10 and 1000/32 are both exactly
# representable, so a correct build reads exactly and any real misconfiguration
# is a whole divisor rung away (the 100 -> 62.5 trap is 37.5 MHz off).  It must
# never be widened to "about right".
CLOCK_TOL_MHZ = 1e-6


def board_mhz(div_product: int) -> float:
    """Board fclk from a divisor product.  The only sanctioned derivation."""
    if not isinstance(div_product, int) or div_product <= 0:
        raise ValueError(f"divisor product must be a positive int, "
                         f"got {div_product!r}")
    return BOARD_IO_PLL_MHZ / div_product


# --------------------------------------------------------------------------
# Variant table.  Mirrors run_postextract_signoff.tcl's `variants` dict; the
# `baseline` row is the shipping 2026-08-11 image and is the default
# everywhere, so an un-parameterised caller keeps its old meaning exactly.
VARIANTS: dict[str, dict] = {
    "baseline": {
        "matcher_vlnv":  "TermCount:hls:tme_top:0.2",
        "request_mhz":   50,          # PCW_FPGA0_PERIPHERAL_FREQMHZ
        "period_ns":     20.0,        # what Vivado constrained
        "div_product":   32,          # 8 x 4
        "io_pll_mhz":    1600.0,      # Vivado's PS7 model for this build
        "board_mhz":     31.25,       # 1000 / 32 -- MEASURED 2026-08-07
        "fclk1_enabled": False,
        "fclk1_div_product": None,
        "bit_sha256":    "3B910C390EC129F48338E7948F0BCEDE7AA596B5E1D8724ABAE1D36BE99A88AA",
        "hwh_sha256":    "E531D7088B877272C99FA8E4A90D2E959BAD09C54315B85A7738F1461FD09793",
        "build_info_variant": None,   # predates the variant field
        "bundle": "vivado/three_stage_combined/board_bundle",
    },
    "combined_current_100": {
        "matcher_vlnv":  "TermCount:hls:tme_top:0.2",
        "request_mhz":   100,
        "period_ns":     10.0,
        "div_product":   10,          # 5 x 2
        "io_pll_mhz":    1000.0,
        "board_mhz":     100.0,
        "fclk1_enabled": True,
        "fclk1_div_product": 8,       # 4 x 2 -> 125.0
        # Gate A produced route reports, not a board bundle: this variant was
        # built to isolate the 100 MHz clock increase and was never intended
        # to reach silicon.  No digests to pin, and staging it is refused.
        "bit_sha256":    None,
        "hwh_sha256":    None,
        "build_info_variant": "combined_current_100",
        "bundle": None,
    },
    "combined_b2_100": {
        "matcher_vlnv":  "TermCountB2:hls:tme_top:0.2",
        "request_mhz":   100,
        "period_ns":     10.0,
        "div_product":   10,          # 5 x 2
        "io_pll_mhz":    1000.0,
        "board_mhz":     100.0,
        "fclk1_enabled": True,
        "fclk1_div_product": 8,       # 4 x 2 -> 125.0
        "bit_sha256":    "C9E6EE67F07531CA187DA84798E422990EC9A5A23FC90011325D94866DD6FDE8",
        "hwh_sha256":    "32AC478E76F72F85F939CAF206F3CDD84BF27EB0B4A4DDD044559EAD459CF5B7",
        "build_info_variant": "combined_b2_100",
        "bundle": "combined_b2_100/postextract_board_bundle_20260821_160540",
    },
}

DEFAULT_VARIANT = "baseline"


def variant(name: str) -> dict:
    """One variant's expectations.  An unknown name is fatal, never a default.

    A typo in --variant must not quietly qualify a build against the
    baseline's 31.25 MHz, which would pass a 100 MHz image only by accident
    and fail it the rest of the time.
    """
    if name not in VARIANTS:
        raise KeyError(f"unknown variant {name!r}; known: {sorted(VARIANTS)}")
    cfg = dict(VARIANTS[name])
    cfg["name"] = name
    derived = board_mhz(cfg["div_product"])
    if abs(derived - cfg["board_mhz"]) > CLOCK_TOL_MHZ:
        raise ValueError(
            f"variant {name}: board_mhz {cfg['board_mhz']} does not follow "
            f"from div_product {cfg['div_product']} "
            f"(1000/{cfg['div_product']} = {derived})")
    cfg["board_mhz"] = derived
    return cfg


# --------------------------------------------------------------------------
# Pinned from the shipped HWH files.  Identical in baseline and
# combined_b2_100 -- verified by diffing both files' MODULE/ADDRESSBLOCK
# trees, where the ONLY difference in the whole design is tme_top_0's VLNV.
ADDRESS_MAP: dict[str, tuple[int, int]] = {          # instance -> (base, range)
    "tme_top_0":            (0x40000000, 0x10000),
    "binarize_core_0":      (0x40010000, 0x10000),
    "patch_extract_core_0": (0x40020000, 0x10000),
    "axi_dma_patch":        (0x41E00000, 0x10000),
    "axi_dma_templ":        (0x41E10000, 0x10000),
    "axi_dma_binarize":     (0x41E20000, 0x10000),
    "dma_pe_data":          (0x41E30000, 0x10000),
    "dma_pe_meta":          (0x41E40000, 0x10000),
}

# Offsets the driver addresses by name.  Contract 7.1.2: adding or reordering
# a port moves every offset after it, so this is the check that catches an ABI
# drift that a name-only check would sail straight past.
REGISTER_MAP: dict[str, dict[str, int]] = {
    "tme_top_0": {
        "CTRL": 0x00, "GIER": 0x04, "IP_IER": 0x08, "IP_ISR": 0x0C,
        "patch_w": 0x10, "patch_h": 0x18, "templ_w": 0x20, "templ_h": 0x28,
        "result_score": 0x30, "result_score_ctrl": 0x34,
        "result_x": 0x40, "result_x_ctrl": 0x44,
        "result_y": 0x50, "result_y_ctrl": 0x54,
    },
    "binarize_core_0": {
        "CTRL": 0x00, "GIER": 0x04, "IP_IER": 0x08, "IP_ISR": 0x0C,
        "img_w": 0x10, "img_h": 0x18, "threshold": 0x20,
    },
    "patch_extract_core_0": {
        "CTRL": 0x00, "GIER": 0x04, "IP_IER": 0x08, "IP_ISR": 0x0C,
        "bin_image_1": 0x10, "bin_image_2": 0x14,
        "img_w": 0x1C, "img_h": 0x24,
        "stride_bytes": 0x2C, "buffer_bytes": 0x34, "num_cands": 0x3C,
        "sts_flags": 0x44, "sts_flags_ctrl": 0x48,
        "sts_rejected": 0x54, "sts_rejected_ctrl": 0x58,
        "sts_processed": 0x64, "sts_processed_ctrl": 0x68,
    },
}

# The binarize DMA's 26-bit length register is what makes a 63,078,400 B page
# legal as ONE transfer.
DMA_PARAMS: dict[str, dict] = {
    "axi_dma_binarize": {"mm2s": True,  "s2mm": True,  "sg_length_width": 26},
    "dma_pe_data":      {"mm2s": True,  "s2mm": True,  "sg_length_width": 18},
    "dma_pe_meta":      {"mm2s": False, "s2mm": True,  "sg_length_width": 18},
    "axi_dma_patch":    {"mm2s": True,  "s2mm": False, "sg_length_width": 18},
    "axi_dma_templ":    {"mm2s": True,  "s2mm": False, "sg_length_width": 18},
}

FULL_PAGE_BYTES = 9856 * 6400        # 63,078,400 (contract 2.2)


def selftest() -> int:
    """Internal consistency of the table itself.  No board, no PYNQ."""
    bad: list[str] = []
    for name in VARIANTS:
        try:
            cfg = variant(name)
        except Exception as exc:                       # noqa: BLE001
            bad.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        vivado_mhz = cfg["io_pll_mhz"] / cfg["div_product"]
        if abs(1000.0 / vivado_mhz - cfg["period_ns"]) > 1e-9:
            bad.append(f"{name}: period_ns {cfg['period_ns']} does not follow "
                       f"from io_pll {cfg['io_pll_mhz']} / div "
                       f"{cfg['div_product']} = {vivado_mhz} MHz")
        print(f"  {name:<22} matcher={cfg['matcher_vlnv']:<28} "
              f"request={cfg['request_mhz']} MHz  "
              f"vivado={vivado_mhz:.4f} MHz ({cfg['period_ns']:.3f} ns)  "
              f"div={cfg['div_product']}  BOARD={cfg['board_mhz']:.4f} MHz")
    try:
        variant("no_such_variant")
    except KeyError:
        pass
    else:
        bad.append("variant() accepted an unknown name")
    if len(REGISTER_MAP) != 3:
        bad.append("REGISTER_MAP must cover exactly the three HLS cores")
    for core, regs in REGISTER_MAP.items():
        if core not in ADDRESS_MAP:
            bad.append(f"{core} has registers but no address")
        if len(set(regs.values())) != len(regs):
            bad.append(f"{core}: duplicate register offsets")
    if set(DMA_PARAMS) | set(REGISTER_MAP) != set(ADDRESS_MAP):
        bad.append("ADDRESS_MAP does not equal the cores plus the DMAs")
    if bad:
        print("\nSELFTEST FAIL:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nboard_expect selftest OK")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
