#!/usr/bin/env python3
"""Drive the board preflight off the board, against fake silicon.

    python test_board_preflight.py        # from sw/
    pytest test_board_preflight.py

No PYNQ and no board.  A fake `pynq` module is injected into `sys.modules`
with an `Overlay` that serves a synthetic `ip_dict` built from
`board_expect`'s pinned tables, and a `Clocks` whose `fclk0_mhz` is whatever
the scenario says.  `inspect_overlay.main()` and `board_idle_check.main()`
then run for real against it.

WHY.  Three of these checks are new and two of them are the ones the whole
B2/100 qualification rests on -- the live clock and the quiescent-before-DMA
state -- so their first execution must not be on the board, where a typo in a
register offset costs a session and looks exactly like a hardware fault.

And because a suite whose cases all pass proves only that it CAN pass, every
assertion is deliberately broken in turn and REQUIRED to fail:

    the 62.5 MHz divisor trap (a 100 MHz build on the wrong divisor)
    an unreadable clock (must fail, never "note and continue")
    the right clock checked against the WRONG variant
    the baseline matcher in a build that must carry B2
    a moved register offset, with every name still present
    a moved base address
    a DMA missing a channel, and a DMA carrying one it should not
    a binarize DMA that cannot carry a full page in one transfer
    a binarize DMA whose bound PYNQ does not report at all
    a core that is not idle; a DMA still running; a soft reset in flight
    a DMA carrying an error bit
    a stale ap_vld from a previous session
    a read that starts the engine it is measuring
    an AXI-Lite write that does not land
    a PL reload that does not clear the fabric
    a payload whose digest is not the pinned one
    a payload whose BUILD_INFO disagrees with its own bytes
    a variant that was never built as a board bundle
    a warm-boot run that passes every technical check

The clock trap is the sharpest of them: it is the one defect that every
Vivado report in the build declares clean, because Vivado constrains 10.000 ns
in both cases and only the board knows the difference.

Three of the mutants are things this suite ONCE let through and no longer
does, which is the only reason to trust the rest of it:

* the read that starts what it measures -- the 2026-08-21 board defect, where
  `getattr(overlay, dma)` constructed a PYNQ driver that started all seven
  engines and the gate then reported them as running;
* the unreported transfer bound -- `inspect_overlay` printed a line and
  exited 0, so "could not verify" read as "verified";
* the warm-boot pass -- `board_preflight` recorded uptime and printed
  `PREFLIGHT=PASS` anyway, making a non-compliant run indistinguishable from a
  compliant one. The end-to-end cases here check the exit code AND that
  exactly one line starts with `PREFLIGHT=`, because
  `WARM_BOOT_TECHNICAL_PREFLIGHT=PASS` contains the substring
  `PREFLIGHT=PASS`.
"""

from __future__ import annotations

import io
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_expect as X                                        # noqa: E402
from tme_standalone_bringup import (DMA_DMACR, DMA_DMASR,       # noqa: E402
                                    DMACR_RESET, DMACR_RS,
                                    DMASR_HALTED)

VARIANT = "combined_b2_100"


# -- fake silicon ----------------------------------------------------------

# Every instance's AXI-Lite aperture, keyed by base address.  `board_idle_
# check` addresses hardware through `pynq.MMIO(phys_addr, ...)` rather than a
# driver object, so the fake has to be addressable the same way.
_BANKS: dict[int, "FakeBank"] = {}


class FakeBank:
    """One instance's registers.

    `gier_writable=False` models a dead AXI-Lite write path; `read_starts`
    models the defect this suite exists to keep out -- a read that changes
    what it is reading, which is precisely what happened on 2026-08-21 when
    the gate reached the DMAs through PYNQ's driver and started all seven.
    """

    def __init__(self, regs=None, gier_writable=True, read_starts=False):
        self.regs = dict(regs or {})
        self.gier_writable = gier_writable
        self.read_starts = read_starts

    def read(self, off):
        value = self.regs.get(off, 0)
        if self.read_starts and off in (DMA_DMASR, 0x30 + DMA_DMASR):
            self.regs[off - 4] = self.regs.get(off - 4, 0) | DMACR_RS
            self.regs[off] = 0
        return value

    def write(self, off, value):
        if off == 0x04 and not self.gier_writable:
            return
        self.regs[off] = value


class FakeMmio:
    def __init__(self, regs=None):
        self.regs = dict(regs or {})

    def read(self, off):
        return self.regs.get(off, 0)

    def write(self, off, value):
        self.regs[off] = value


class FakeChannel:
    def __init__(self, offset, dmacr=0, dmasr=DMASR_HALTED, mmio=None):
        self._offset = offset
        self._mmio = mmio if mmio is not None else FakeMmio()
        self._mmio.regs.setdefault(offset + DMA_DMACR, dmacr)
        self._mmio.regs.setdefault(offset + DMA_DMASR, dmasr)
        self._max_size = 262143


class FakeDma:
    def __init__(self, mm2s, s2mm, max_size):
        mmio = FakeMmio()
        self.sendchannel = FakeChannel(0x00, mmio=mmio) if mm2s else None
        self.recvchannel = FakeChannel(0x30, mmio=mmio) if s2mm else None
        self.buffer_max_size = max_size
        if max_size is None and self.sendchannel is not None:
            # Neither source answers -- the "PYNQ reported no bound" case.
            self.sendchannel._max_size = None


class FakeCore:
    """An HLS core as `getattr(overlay, name)` sees it: a register_map only.

    Register STATE lives in the FakeBank behind the instance's base address,
    because that is how board_idle_check reaches it.  Nothing in this class
    can be read or written by the idle gate, which is the point: if that gate
    ever went back to driver objects, these tests would stop covering it.
    """

    def __init__(self, names):
        self.register_map = types.SimpleNamespace(**{n: 0 for n in names})


class FakeOverlay:
    """Built from board_expect's pinned tables, then mutated per scenario."""

    def __init__(self, world, sticky_gier=False):
        self.hierarchy_dict = {}
        self.ip_dict = {}
        self._ips = {}
        _BANKS.clear()
        for inst, (base, rng) in X.ADDRESS_MAP.items():
            vlnv = world["vlnv"].get(inst, "xilinx.com:ip:axi_dma:7.1")
            regs = {name: {"address_offset": off}
                    for name, off in world["offsets"].get(inst, {}).items()}
            addr = world["bases"].get(inst, base)
            self.ip_dict[inst] = {
                "type": vlnv,
                "phys_addr": addr,
                "addr_range": rng,
                "registers": regs,
                "fullpath": inst,
            }

        for core, regs in X.REGISTER_MAP.items():
            self._ips[core] = FakeCore(regs)
            bank = {0x00: world["ctrl"].get(core, 1 << 2),
                    0x04: 1 if sticky_gier else 0}
            bank.update(world["vld"].get(core) or {})
            _BANKS[self.ip_dict[core]["phys_addr"]] = FakeBank(
                bank, gier_writable=world["gier_writable"])

        for dma, cfg in X.DMA_PARAMS.items():
            mm2s, s2mm = world["channels"].get(dma, (cfg["mm2s"], cfg["s2mm"]))
            self._ips[dma] = FakeDma(
                mm2s, s2mm,
                world["max_size"] if dma == "axi_dma_binarize" else 262143)
            # Post-programming reset values measured on silicon 2026-08-21:
            # DMACR=0x00010002 (IRQThreshold=1), DMASR=0x00000001 (Halted).
            bank = {}
            for label, off in (("MM2S", 0x00), ("S2MM", 0x30)):
                bank[off + DMA_DMACR] = 0x00010002
                bank[off + DMA_DMASR] = DMASR_HALTED
            if dma in world["dma_state"]:
                label, cr, sr = world["dma_state"][dma]
                off = 0x00 if label == "MM2S" else 0x30
                bank[off + DMA_DMACR] = cr
                bank[off + DMA_DMASR] = sr
            _BANKS[self.ip_dict[dma]["phys_addr"]] = FakeBank(
                bank, read_starts=world["read_starts"])

        for inst in world["drop"]:
            self.ip_dict.pop(inst, None)

    def __getattr__(self, name):
        try:
            return self._ips[name]
        except KeyError:
            raise AttributeError(name) from None


def base_world(**over):
    """A world in which everything is exactly right."""
    cfg = X.variant(VARIANT)
    vlnv = {"tme_top_0": cfg["matcher_vlnv"],
            "binarize_core_0": "TermCount:hls:binarize_core:2.0",
            "patch_extract_core_0": "TermCount:hls:patch_extract_core:0.1"}
    w = {
        "mhz": cfg["board_mhz"],
        "mhz1": X.board_mhz(cfg["fclk1_div_product"]),
        "clock_raises": False,
        "fclk1_raises": False,
        "vlnv": vlnv,
        "bases": {},
        "offsets": {c: dict(r) for c, r in X.REGISTER_MAP.items()},
        "ctrl": {},
        "vld": {},
        "channels": {},
        "dma_state": {},
        "drop": [],
        "max_size": 67108863,
        "gier_writable": True,
        "gier_sticky": False,
        "read_starts": False,
    }
    w.update(over)
    return w


_WORLD: dict = {}


def install_fake_pynq():
    """Inject a `pynq` module whose Overlay/Clocks read the current world."""
    reloads = {"n": 0}

    class _Overlay(FakeOverlay):
        def __init__(self, bitfile):
            reloads["n"] += 1
            w = _WORLD["w"]
            # A real reload returns the fabric to power-on state, so a fresh
            # FakeOverlay rebuilds every bank at its reset value.  `gier_
            # sticky` models the fabric that does NOT, and only from the
            # second load on -- the first load is the one whose state phase 1
            # judges, and it must look clean there or the mutant would be
            # caught in the wrong phase.
            super().__init__(w, sticky_gier=(reloads["n"] > 1
                                             and w["gier_sticky"]))

    class _Clocks:
        @property
        def fclk0_mhz(self):
            if _WORLD["w"]["clock_raises"]:
                raise RuntimeError("fclk0 unreadable (simulated)")
            return _WORLD["w"]["mhz"]

        @property
        def fclk1_mhz(self):
            if _WORLD["w"]["fclk1_raises"]:
                raise RuntimeError("fclk1 unreadable (simulated)")
            return _WORLD["w"]["mhz1"]

    def _MMIO(base, length=None):
        try:
            return _BANKS[base]
        except KeyError:
            raise ValueError(f"no fake register bank at 0x{base:08X}") from None

    mod = types.ModuleType("pynq")
    mod.Overlay = _Overlay
    mod.Clocks = _Clocks()
    mod.MMIO = _MMIO
    mod.allocate = None
    sys.modules["pynq"] = mod
    return reloads


# -- harness ---------------------------------------------------------------

FAILURES: list[str] = []
PASSES = {"n": 0}


def run(module_main, argv, world, want_rc, label, marker=None):
    """Run one scenario.  `marker` pins WHY it failed, not just that it did.

    An exit code alone is a fail-open check: a mutant that broke the script in
    some unrelated way would also exit 1 and score as DETECTED.  Where the
    reason matters, the scenario names the banner line it must produce.
    """
    global _WORLD
    _WORLD["w"] = world
    old = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = module_main()
    except SystemExit as exc:                          # argparse
        rc = exc.code
    finally:
        sys.argv = old
    tail = "\n".join("      " + l
                     for l in buf.getvalue().splitlines()[-14:])
    if rc != want_rc:
        FAILURES.append(f"{label}: exit {rc}, expected {want_rc}\n{tail}")
    elif marker is not None and marker not in buf.getvalue():
        FAILURES.append(f"{label}: exit {rc} as expected, but the transcript "
                        f"does not carry {marker!r} -- it failed for some "
                        f"other reason\n{tail}")
    else:
        PASSES["n"] += 1
    return buf.getvalue()


def main() -> int:
    install_fake_pynq()
    import board_idle_check as I
    import inspect_overlay as O
    import board_preflight as P

    bit = "three_stage_combined.bit"
    insp = ["inspect_overlay.py", "--overlay", bit, "--variant", VARIANT]
    idle = ["board_idle_check.py", "--overlay", bit, "--variant", VARIANT]
    cfg = X.variant(VARIANT)

    # ---- inspect_overlay: the good world, then every mutant --------------
    print("inspect_overlay --variant combined_b2_100")
    out = run(O.main, insp, base_world(), 0, "clean overlay")
    assert "GATE=live_clock;RESULT=PASS" in out, out[-2000:]
    print("  clean overlay                       PASS (exit 0)")

    mutants = [
        ("62.5 MHz divisor trap",
         base_world(mhz=62.5), "GATE=live_clock;RESULT=FAIL"),
        ("31.25 MHz (baseline clock in a 100 build)",
         base_world(mhz=31.25), "GATE=live_clock;RESULT=FAIL"),
        ("clock unreadable",
         base_world(clock_raises=True), "PL clock: UNREADABLE"),
        # THE FAIL-OPEN CASE.  PYNQ's power-on fclk0 on this board is 100.0 --
        # the value the variant wants -- so an overlay that never programmed
        # the clocks reads a perfect fclk0.  Only fclk1, still at PYNQ's
        # 142.857143 default, reveals it.
        ("overlay never programmed the clocks (fclk0 right by coincidence)",
         base_world(mhz1=142.857143), "GATE=live_fclk1;RESULT=FAIL"),
        ("fclk1 unreadable",
         base_world(fclk1_raises=True), "GATE=live_fclk1;RESULT=FAIL"),
        ("baseline matcher VLNV",
         base_world(vlnv={"tme_top_0": "TermCount:hls:tme_top:0.2"}),
         "GATE=matcher_vlnv;RESULT=FAIL"),
        ("moved register offset (names intact)",
         base_world(offsets={**{c: dict(r) for c, r in X.REGISTER_MAP.items()},
                             "tme_top_0": {**X.REGISTER_MAP["tme_top_0"],
                                           "result_y": 0x58}}),
         "register offsets moved"),
        ("moved base address",
         base_world(bases={"tme_top_0": 0x40030000}), "!= pinned"),
        ("DMA missing its S2MM",
         base_world(channels={"dma_pe_meta": (False, False)}),
         "dma_pe_meta: S2MM recvchannel missing"),
        ("DMA carrying a channel it should not",
         base_world(channels={"axi_dma_templ": (True, True)}),
         "axi_dma_templ: has an S2MM recvchannel it should"),
        ("binarize DMA cannot carry a page",
         base_world(max_size=262143), "cannot carry a 63,078,400 B page"),
        # Was fail-open: no bound reported meant a printed line and exit 0,
        # so an overlay that cannot carry a page in one transfer would pass
        # here and die in gate 3 instead.
        ("PYNQ reports no transfer bound at all",
         base_world(max_size=None), "GATE=binarize_transfer_bound;"
                                    "RESULT=CANNOT_VERIFY"),
        ("an instance absent from ip_dict",
         base_world(drop=["dma_pe_data"]), "missing DMA: dma_pe_data"),
    ]
    for label, world, marker in mutants:
        run(O.main, insp, world, 1, f"inspect mutant: {label}", marker)
        print(f"  mutant {label:<36} DETECTED (exit 1)")

    # The right clock, checked against the wrong variant: proves --variant
    # actually selects an expectation rather than decorating the banner.
    run(O.main, ["inspect_overlay.py", "--overlay", bit,
                 "--variant", "baseline"],
        base_world(), 1, "100 MHz overlay checked as baseline",
        "GATE=live_clock;RESULT=FAIL")
    print("  mutant 100 MHz world vs --variant baseline "
          "DETECTED (exit 1)")
    # ...and the converse, so the baseline row is not merely strict.
    # fclk1 is left at PYNQ's default here, as it is on a real baseline board:
    # the baseline design does not drive FCLK1, so that must be recorded and
    # NOT gated -- otherwise the new fclk1 check would break the shipping
    # image's own preflight.
    run(O.main, ["inspect_overlay.py", "--overlay", bit,
                 "--variant", "baseline"],
        base_world(mhz=31.25, mhz1=142.857143,
                   vlnv={"tme_top_0": "TermCount:hls:tme_top:0.2"}),
        0, "baseline world checked as baseline")
    print("  control baseline world vs --variant baseline PASS (exit 0)")

    # ---- board_idle_check ------------------------------------------------
    print("\nboard_idle_check --variant combined_b2_100")
    out = run(I.main, idle, base_world(), 0, "clean idle check")
    assert "IDLE_CHECK=PASS" in out, out[-2000:]
    print("  clean fabric                        PASS (exit 0)")

    P1 = "IDLE_CHECK=FAIL;PHASE=1"
    idle_mutants = [
        ("core not idle (ap_idle clear)",
         base_world(ctrl={"tme_top_0": 0}), P1),
        ("core still running (ap_start set)",
         base_world(ctrl={"binarize_core_0": (1 << 2) | (1 << 0)}), P1),
        ("stale ap_done",
         base_world(ctrl={"patch_extract_core_0": (1 << 2) | (1 << 1)}), P1),
        ("auto_restart armed",
         base_world(ctrl={"tme_top_0": (1 << 2) | (1 << 7)}), P1),
        ("DMA still running (Halted clear)",
         base_world(dma_state={"axi_dma_binarize": ("MM2S", DMACR_RS, 0)}),
         P1),
        ("DMA soft reset in flight",
         base_world(dma_state={"dma_pe_data":
                               ("S2MM", DMACR_RESET, DMASR_HALTED)}), P1),
        ("DMA carrying DMASlvErr",
         base_world(dma_state={"axi_dma_templ":
                               ("MM2S", 0, DMASR_HALTED | (1 << 5))}), P1),
        ("stale ap_vld on the matcher result",
         base_world(vld={"tme_top_0": {0x34: 1}}), P1),
        ("stale ap_vld on the extractor status",
         base_world(vld={"patch_extract_core_0": {0x48: 1}}), P1),
        # THE 2026-08-21 DEFECT, as a standing regression.  Here the fabric is
        # genuinely quiescent and the FIRST reading is clean -- only the fact
        # that reading changed it gives it away.  Without the read-twice
        # guard this world passes.
        ("a read that starts the engine it is measuring",
         base_world(read_starts=True), P1),
        # These two are the reason phase 2 exists, and each must fail in its
        # OWN phase: a dead write path at 2a, a reload that does not reset
        # at 2c.  If either one could be reported as the other, phase 2 would
        # not distinguish "the fabric ignored us" from "the reset did nothing".
        ("AXI-Lite write does not land",
         base_world(gier_writable=False), "IDLE_CHECK=FAIL;PHASE=2a"),
        ("PL reload does not clear the fabric",
         base_world(gier_sticky=True), "IDLE_CHECK=FAIL;PHASE=2c"),
    ]
    for label, world, marker in idle_mutants:
        run(I.main, idle, world, 1, f"idle mutant: {label}", marker)
        print(f"  mutant {label:<36} DETECTED (exit 1)")

    # ---- board_preflight step 0 ------------------------------------------
    print("\nboard_preflight step 0 (payload identity)")
    import hashlib
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        good_bit = b"BITSTREAM-BYTES"
        good_hwh = b"<HWH/>"
        bit_sha = hashlib.sha256(good_bit).hexdigest().upper()
        hwh_sha = hashlib.sha256(good_hwh).hexdigest().upper()

        def write(bit_bytes=good_bit, hwh_bytes=good_hwh, info=None):
            (d / "three_stage_combined.bit").write_bytes(bit_bytes)
            (d / "three_stage_combined.hwh").write_bytes(hwh_bytes)
            (d / "BUILD_INFO.txt").write_text(info if info is not None else
                                              default_info(), encoding="ascii")

        def default_info(**over):
            fields = {
                "variant": VARIANT,
                "matcher_core": cfg["matcher_vlnv"],
                "bit_sha256": bit_sha,
                "hwh_sha256": hwh_sha,
                "hwh_fclk0_div_product": str(cfg["div_product"]),
                "hwh_fclk1_div_product": str(cfg["fclk1_div_product"]),
            }
            fields.update(over)
            return "".join(f"{k}={v}\n" for k, v in fields.items())

        # A variant whose pinned digests are these test bytes.
        test_cfg = dict(cfg, bit_sha256=bit_sha, hwh_sha256=hwh_sha)

        def step0(cfgv=None, **kw):
            write(**kw)
            buf = io.StringIO()
            with redirect_stdout(buf):
                return P.step0_payload(d, cfgv or test_cfg,
                                       "three_stage_combined.bit")

        cases = [
            ("clean payload", step0(), False),
            ("bitstream bytes are not the pinned build",
             step0(bit_bytes=b"OTHER-BITSTREAM",
                   info=default_info(
                       bit_sha256=hashlib.sha256(
                           b"OTHER-BITSTREAM").hexdigest().upper())), True),
            ("BUILD_INFO disagrees with its own bytes",
             step0(info=default_info(bit_sha256="00" * 32)), True),
            ("BUILD_INFO names another variant",
             step0(info=default_info(variant="combined_current_100")), True),
            ("BUILD_INFO names the baseline matcher",
             step0(info=default_info(
                 matcher_core="TermCount:hls:tme_top:0.2")), True),
            ("BUILD_INFO carries the 62.5 MHz divisor product",
             step0(info=default_info(hwh_fclk0_div_product="16")), True),
            ("variant was never built as a board bundle",
             step0(cfgv=X.variant("combined_current_100")), True),
        ]
        for label, faults, want_fault in cases:
            got = bool(faults)
            if got != want_fault:
                FAILURES.append(f"step0 {label}: faults={faults}, "
                                f"expected fault={want_fault}")
            else:
                PASSES["n"] += 1
                verb = "DETECTED" if want_fault else "PASS"
                print(f"  {verb:<8} {label}")

        # ---- the fresh-boot requirement ----------------------------------
        # The plan requires the preflight be performed after a fresh boot,
        # and completion condition 4 is worded the same way.  An earlier
        # version recorded uptime and printed PREFLIGHT=PASS regardless,
        # which made a warm-boot run indistinguishable from a compliant one
        # in the only line anybody greps.
        print("\nboard_preflight fresh-boot requirement")
        cma_ok = {"CmaTotal": 224 * 1024 * 1024, "CmaFree": 120 * 1024 * 1024}
        real_uptime, real_cma = P.read_uptime_s, P._meminfo_cma
        try:
            P._meminfo_cma = lambda: dict(cma_ok)
            for label, up, want_fresh in (
                    ("fresh boot (120 s)", 120.0, True),
                    ("at the bound exactly (3600 s)", 3600.0, True),
                    ("one second over (3601 s)", 3601.0, False),
                    ("the 2026-08-21 run (29,249 s)", 29249.0, False)):
                P.read_uptime_s = lambda u=up: u
                buf = io.StringIO()
                with redirect_stdout(buf):
                    fatal, warn, fresh = P.step1_platform(3600.0)
                marker = ("GATE=fresh_boot;RESULT="
                          + ("PASS" if want_fresh else "HOLD"))
                if fatal or fresh != want_fresh or marker not in buf.getvalue():
                    FAILURES.append(f"fresh-boot {label}: fresh={fresh} "
                                    f"expected {want_fresh}, fatal={fatal}")
                else:
                    PASSES["n"] += 1
                    print(f"  {'FRESH' if want_fresh else 'HOLD ':<6} {label}")

            # Unreadable uptime must HOLD, never assume fresh.
            def _boom():
                raise OSError("no /proc/uptime")
            P.read_uptime_s = _boom
            buf = io.StringIO()
            with redirect_stdout(buf):
                fatal, warn, fresh = P.step1_platform(3600.0)
            if fresh or not warn or "RESULT=HOLD" not in buf.getvalue():
                FAILURES.append(f"unreadable uptime: fresh={fresh} warn={warn}")
            else:
                PASSES["n"] += 1
                print("  HOLD   uptime unreadable (withheld, not assumed)")

            # End to end: with every gate passing, a warm boot must still
            # refuse the formal verdict AND exit 3 -- not 0, and not 1.
            write()
            X.VARIANTS[VARIANT]["bit_sha256"] = bit_sha
            X.VARIANTS[VARIANT]["hwh_sha256"] = hwh_sha
            real_run = P.subprocess.run
            P.subprocess.run = lambda argv, **kw: types.SimpleNamespace(
                returncode=0)
            try:
                for label, up, want_rc, marker in (
                        ("warm boot, all gates pass", 29249.0, 3,
                         "FORMAL_FRESH_BOOT_PREFLIGHT=HOLD"),
                        ("fresh boot, all gates pass", 120.0, 0,
                         "FORMAL_FRESH_BOOT_PREFLIGHT=PASS")):
                    P.read_uptime_s = lambda u=up: u
                    out = run(P.main,
                              ["board_preflight.py", "--variant", VARIANT,
                               "--dir", str(d)],
                              base_world(), want_rc,
                              f"preflight end-to-end: {label}", marker)
                    # Anchored, because WARM_BOOT_TECHNICAL_PREFLIGHT=PASS
                    # CONTAINS the substring PREFLIGHT=PASS.  The verdict is
                    # the line that STARTS with it, and that collision is a
                    # real trap for a project that greps banner lines.
                    verdicts = [l for l in out.splitlines()
                                if l.startswith("PREFLIGHT=")]
                    if want_rc == 3 and any(v.startswith("PREFLIGHT=PASS")
                                            for v in verdicts):
                        FAILURES.append(
                            "warm-boot run still printed PREFLIGHT=PASS")
                    if len(verdicts) != 1:
                        FAILURES.append(
                            f"expected exactly one ^PREFLIGHT= verdict line, "
                            f"got {verdicts}")
                    if (want_rc == 3
                            and "WARM_BOOT_TECHNICAL_PREFLIGHT=PASS"
                            not in out):
                        FAILURES.append(
                            "warm-boot run did not report the technical PASS")
                    print(f"  exit {want_rc}  {label}")

                # --allow-warm-boot changes the exit code and NOTHING else:
                # the formal verdict must still read HOLD.
                P.read_uptime_s = lambda: 29249.0
                out = run(P.main,
                          ["board_preflight.py", "--variant", VARIANT,
                           "--dir", str(d), "--allow-warm-boot"],
                          base_world(), 0,
                          "preflight: --allow-warm-boot",
                          "FORMAL_FRESH_BOOT_PREFLIGHT=HOLD")
                if any(l.startswith("PREFLIGHT=PASS")
                       for l in out.splitlines()):
                    FAILURES.append("--allow-warm-boot upgraded the wording, "
                                    "not just the exit code")
                else:
                    print("  exit 0  --allow-warm-boot (wording still HOLD)")
            finally:
                P.subprocess.run = real_run
                X.VARIANTS[VARIANT]["bit_sha256"] = cfg["bit_sha256"]
                X.VARIANTS[VARIANT]["hwh_sha256"] = cfg["hwh_sha256"]
        finally:
            P.read_uptime_s, P._meminfo_cma = real_uptime, real_cma

        # A missing file must be reported as missing, not as a digest
        # mismatch: the two send you to different places.
        write()
        (d / "BUILD_INFO.txt").unlink()
        buf = io.StringIO()
        with redirect_stdout(buf):
            faults = P.step0_payload(d, test_cfg, "three_stage_combined.bit")
        if not any("missing payload file" in f for f in faults):
            FAILURES.append(f"step0 missing BUILD_INFO: {faults}")
        else:
            PASSES["n"] += 1
            print("  DETECTED missing BUILD_INFO.txt (named as missing)")

    # ---- the modules' own selftests --------------------------------------
    print("\nmodule selftests")
    for name, fn in (("board_expect", X.selftest),
                     ("board_idle_check", I.selftest)):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fn()
        if rc != 0:
            FAILURES.append(f"{name}.selftest() -> {rc}")
        else:
            PASSES["n"] += 1
            print(f"  {name}.selftest()                   OK")

    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} of {len(FAILURES) + PASSES['n']} checks")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK: {PASSES['n']} checks, every injected defect detected")
    return 0


def test_board_preflight():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
