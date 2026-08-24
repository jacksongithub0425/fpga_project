#!/usr/bin/env python3
"""Reset and idle behaviour, BEFORE any DMA traffic.

    sudo -E python3 board_idle_check.py --overlay three_stage_combined.bit \
                                     --variant combined_b2_100
    python3 board_idle_check.py --selftest      # off-board, no PYNQ

Runs after `inspect_overlay.py` and before `board_gate_full_dma.py`.  It is
READ-ONLY with respect to every register that can move data: it never writes
DMACR, never arms a channel, never writes ap_start.  That restriction is the
gate -- "the fabric is quiescent" is only meaningful if establishing it did
not itself start something.

EVERYTHING HERE IS RAW MMIO.  THIS IS NOT A STYLE CHOICE.
---------------------------------------------------------
`pynq.lib.dma.DMA` **starts every channel it wraps in its constructor**, so
merely evaluating `overlay.axi_dma_binarize` writes `DMACR.RS = 1` and the
engine leaves the halted state.  Measured on this board, 2026-08-21: the
seven channels read `DMACR=0x00010002 DMASR=0x00000001` (Halted=1, RS=0)
straight after programming, and `DMACR=0x00010003 DMASR=0x00000000`
(Halted=0, RS=1) after nothing but the driver objects being constructed --
7 of 7 changed, with no transfer requested.  The first version of this gate
used `getattr(overlay, dma_name)` and reported all seven as running; the
fault was its own.  So this file addresses every register through
`pynq.MMIO` on the pinned base address and never instantiates a driver.

Phase 1 also reads the whole state TWICE and requires the two readings to be
identical, which is the standing guard on exactly that defect: a read path
with a side effect can no longer pass unnoticed.

TWO PHASES, AND THE SECOND IS THE ONE THAT MEANS ANYTHING
---------------------------------------------------------
Phase 1 reads the state after a fresh overlay load and requires it to be the
power-on state: every core `ap_idle` with `ap_start`/`ap_done`/`ap_ready`
clear, every DMA channel `DMASR.Halted` with `DMACR.RS` and `DMACR.Reset`
clear and no error bit set, and every `ap_vld` sideband register clear so no
stale result from an earlier session can be mistaken for this run's.

But phase 1 alone proves almost nothing: a fabric that ignored every write and
returned zeros would pass it, and so would a board still running the previous
session's overlay.  So phase 2 PERTURBS state that survives nothing but a
reset, reprograms the PL, and requires the perturbation to be gone.

The perturbation target is `GIER` (0x04), the master interrupt enable of each
HLS core.  It is chosen because it is the only writable, readable, and
completely inert register in the design: all three cores' `interrupt` output
ports have **zero connections** in the block design (verified in the shipped
HWH -- they are dangling), so enabling an interrupt cannot raise one, cannot
reach the PS, and cannot perturb anything a later gate measures.  Writing a
parameter register instead would be no good: `patch_w`, `img_w` and friends
are write-only, so nothing could be read back to prove the write landed.

So phase 2 establishes three things phase 1 cannot:

  1. the AXI-Lite path is live in both directions (the write is read back);
  2. reloading the bitstream really does return the fabric to power-on state
     (the perturbation is gone), which is what every recovery path in
     `safe_teardown.py` depends on being true;
  3. the quiescent state of phase 1 is reproducible rather than a coincidence
     of whatever the board happened to be doing.

Exit status: 0 = quiescent and reset-clean, 1 = a check failed (the fabric is
NOT in a state to start DMA traffic), 2 = could not run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_expect as X                                        # noqa: E402
from tme_standalone_bringup import (DMA_DMACR, DMA_DMASR,       # noqa: E402
                                    DMACR_RESET, DMACR_RS,
                                    DMASR_HALTED)

# ap_ctrl_hs CTRL bits at offset 0x00 -- fixed by the protocol, not by any
# per-core map (same reason tme_driver hardcodes them and only them).
AP_CTRL_OFF   = 0x00
AP_START      = 1 << 0
AP_DONE       = 1 << 1
AP_IDLE       = 1 << 2
AP_READY      = 1 << 3
AP_AUTORESTART = 1 << 7

GIER_OFF = 0x04                 # master interrupt enable; inert here

# DMASR error bits (PG021).  Scatter-gather is not included in any of these
# five engines (C_INCLUDE_SG=0), so the SG error bits are reserved; the three
# gated here are the ones a real engine can raise.
DMASR_DMA_INT_ERR = 1 << 4
DMASR_DMA_SLV_ERR = 1 << 5
DMASR_DMA_DEC_ERR = 1 << 6
DMASR_ERR_MASK = DMASR_DMA_INT_ERR | DMASR_DMA_SLV_ERR | DMASR_DMA_DEC_ERR

# The ap_vld sideband registers.  Reading the *_ctrl register is what clears
# the valid flag in the generated s_axi block, so these are read once, first,
# and a set bit means a result from before this overlay load survived.
VLD_REGS: dict[str, dict[str, int]] = {
    "tme_top_0": {
        "result_score_ctrl": 0x34,
        "result_x_ctrl": 0x44,
        "result_y_ctrl": 0x54,
    },
    "patch_extract_core_0": {
        "sts_flags_ctrl": 0x48,
        "sts_rejected_ctrl": 0x58,
        "sts_processed_ctrl": 0x68,
    },
}


# -- pure decision functions (no PYNQ; exercised by --selftest) -------------

def core_faults(name: str, ctrl: int) -> list[str]:
    """What is wrong with one core's CTRL word at rest.  [] means fine."""
    bad = []
    if ctrl & AP_START:
        bad.append(f"{name}: ap_start is SET (CTRL=0x{ctrl:08X}) -- an "
                   f"invocation is pending or in flight")
    if ctrl & AP_DONE:
        bad.append(f"{name}: ap_done is SET (CTRL=0x{ctrl:08X}) -- a result "
                   f"from an earlier invocation has not been collected")
    if not ctrl & AP_IDLE:
        bad.append(f"{name}: ap_idle is CLEAR (CTRL=0x{ctrl:08X}) -- the core "
                   f"is running or blocked in a stream read")
    if ctrl & AP_READY:
        bad.append(f"{name}: ap_ready is SET (CTRL=0x{ctrl:08X})")
    if ctrl & AP_AUTORESTART:
        bad.append(f"{name}: auto_restart is SET (CTRL=0x{ctrl:08X}) -- the "
                   f"core would relaunch itself on the next done")
    return bad


def channel_faults(label: str, dmacr: int, dmasr: int) -> list[str]:
    """What is wrong with one DMA channel at rest.  [] means fine.

    Quiescence needs BOTH `DMASR.Halted == 1` and `DMACR.Reset == 0`: per
    PG021 a soft reset does not abort an AXI transaction already in flight, it
    lets that transaction finish while holding Reset asserted, so Halted alone
    can mean "still draining a read".  Same rule as
    `PLPipeline._verify_quiescent`, but with no write -- this runs before any
    traffic, so there is nothing to halt and nothing that may be halted.
    """
    bad = []
    if not dmasr & DMASR_HALTED:
        bad.append(f"{label}: DMASR.Halted is CLEAR "
                   f"(DMASR=0x{dmasr:08X}) -- the engine is running")
    if dmacr & DMACR_RS:
        bad.append(f"{label}: DMACR.RS is SET (DMACR=0x{dmacr:08X}) -- the "
                   f"engine has been started")
    if dmacr & DMACR_RESET:
        bad.append(f"{label}: DMACR.Reset is SET (DMACR=0x{dmacr:08X}) -- a "
                   f"soft reset is still in flight")
    if dmasr & DMASR_ERR_MASK:
        names = []
        if dmasr & DMASR_DMA_INT_ERR:
            names.append("DMAIntErr")
        if dmasr & DMASR_DMA_SLV_ERR:
            names.append("DMASlvErr")
        if dmasr & DMASR_DMA_DEC_ERR:
            names.append("DMADecErr")
        bad.append(f"{label}: DMASR carries {'+'.join(names)} "
                   f"(DMASR=0x{dmasr:08X})")
    return bad


def vld_faults(core: str, reg: str, value: int) -> list[str]:
    """A set ap_vld before any invocation is stale state, not a reading."""
    if value & 1:
        return [f"{core}.{reg} = 0x{value:08X}: ap_vld is SET before any "
                f"invocation -- a result from a previous session survived, "
                f"so the fabric was not reset"]
    return []


# -- board-side reads ------------------------------------------------------

def window(ol, inst):
    """A raw MMIO over one instance's AXI-Lite aperture.

    Deliberately NOT `getattr(overlay, inst)`: for the five DMAs that
    constructs `pynq.lib.dma.DMA`, whose channel constructors start the
    engines (see the module docstring).  Raw MMIO reads and writes the same
    addresses with no driver behaviour attached.
    """
    from pynq import MMIO
    e = ol.ip_dict[inst]
    return MMIO(e["phys_addr"], e["addr_range"])


# Channel apertures inside an AXI DMA's register space, fixed by the IP.
CH_OFFSETS = {"MM2S": 0x00, "S2MM": 0x30}


def _channel_windows(ol):
    """(label, mmio, channel_offset) for every DMA channel in the design."""
    out = []
    for dma_name, cfg in X.DMA_PARAMS.items():
        if dma_name not in ol.ip_dict:
            continue
        mmio = window(ol, dma_name)
        for label, present in (("MM2S", cfg["mm2s"]), ("S2MM", cfg["s2mm"])):
            if present:
                out.append((f"{dma_name}.{label}", mmio, CH_OFFSETS[label]))
    return out


def read_state(ol) -> tuple[dict, list[str]]:
    """Read every register this gate judges.  (state, unreachable-faults)."""
    state: dict = {"cores": {}, "vld": {}, "channels": {}, "gier": {}}
    faults: list[str] = []

    for core in X.REGISTER_MAP:
        if core not in ol.ip_dict:
            faults.append(f"missing IP: {core}")
            continue
        try:
            m = window(ol, core)
            # *_ctrl first: reading it is what clears the valid flag, so a
            # read of the data register first would destroy the evidence.
            for reg, off in VLD_REGS.get(core, {}).items():
                state["vld"][(core, reg)] = m.read(off)
            state["cores"][core] = m.read(AP_CTRL_OFF)
            state["gier"][core] = m.read(GIER_OFF)
        except Exception as exc:                       # noqa: BLE001
            faults.append(f"{core}: registers unreachable "
                          f"({type(exc).__name__}: {exc})")

    for label, mmio, base in _channel_windows(ol):
        try:
            state["channels"][label] = (mmio.read(base + DMA_DMACR),
                                        mmio.read(base + DMA_DMASR))
        except Exception as exc:                       # noqa: BLE001
            faults.append(f"{label}: register read raised "
                          f"{type(exc).__name__}: {exc}")
    return state, faults


def judge(state: dict, faults: list[str], phase: str) -> list[str]:
    """Print the state and return every fault in it."""
    bad = list(faults)
    print(f"\n--- {phase}: HLS cores ---")
    for core, ctrl in state["cores"].items():
        f = core_faults(core, ctrl)
        print(f"  {core:<24} CTRL=0x{ctrl:08X} "
              f"start={bool(ctrl & AP_START):d} done={bool(ctrl & AP_DONE):d} "
              f"idle={bool(ctrl & AP_IDLE):d} ready={bool(ctrl & AP_READY):d}"
              f"  -> {'IDLE' if not f else 'FAULT'}")
        bad += f

    print(f"\n--- {phase}: ap_vld sideband registers ---")
    if not state["vld"]:
        print("  (none read)")
    for (core, reg), value in state["vld"].items():
        f = vld_faults(core, reg, value)
        print(f"  {core}.{reg:<20} = 0x{value:08X} "
              f"-> {'clear' if not f else 'STALE'}")
        bad += f

    print(f"\n--- {phase}: DMA channels (read-only) ---")
    for label, (cr, sr) in state["channels"].items():
        f = channel_faults(label, cr, sr)
        print(f"  {label:<24} DMACR=0x{cr:08X} DMASR=0x{sr:08X} "
              f"halted={bool(sr & DMASR_HALTED):d} rs={bool(cr & DMACR_RS):d} "
              f"-> {'HALTED' if not f else 'FAULT'}")
        bad += f
    expected_channels = sum((1 if c["mm2s"] else 0) + (1 if c["s2mm"] else 0)
                            for c in X.DMA_PARAMS.values())
    if len(state["channels"]) != expected_channels:
        bad.append(f"{phase}: read {len(state['channels'])} DMA channels, "
                   f"expected {expected_channels} -- an engine the driver "
                   f"arms was not inspected")
    return bad


def perturb(ol) -> list[str]:
    """Write GIER on every core and prove the write landed.  Faults if not."""
    bad = []
    print("\n--- phase 2a: perturb GIER (inert: interrupt ports are "
          "unconnected) ---")
    for core in X.REGISTER_MAP:
        if core not in ol.ip_dict:
            continue
        try:
            m = window(ol, core)
            m.write(GIER_OFF, 1)
            got = m.read(GIER_OFF)
        except Exception as exc:                       # noqa: BLE001
            bad.append(f"{core}: GIER write/read raised "
                       f"{type(exc).__name__}: {exc}")
            continue
        ok = bool(got & 1)
        print(f"  {core:<24} wrote GIER=1, read back 0x{got:08X} "
              f"-> {'OK' if ok else 'WRITE DID NOT LAND'}")
        if not ok:
            bad.append(f"{core}: GIER read back 0x{got:08X} after writing 1 "
                       f"-- the AXI-Lite write path is not live, so nothing "
                       f"this preflight read can be trusted")
    return bad


def check_reset_cleared(state: dict) -> list[str]:
    """After the reload, every perturbed GIER must be back at its reset 0."""
    bad = []
    print("\n--- phase 2c: perturbation must be gone ---")
    for core, gier in state["gier"].items():
        ok = not gier & 1
        print(f"  {core:<24} GIER=0x{gier:08X} "
              f"-> {'cleared by reset' if ok else 'SURVIVED THE RELOAD'}")
        if not ok:
            bad.append(f"{core}: GIER still 0x{gier:08X} after reprogramming "
                       f"the PL -- the reload did not reset the fabric, so "
                       f"every recovery path that relies on it is unsound")
    return bad


# -- selftest --------------------------------------------------------------

def selftest() -> int:
    """The decision functions, on synthetic words.  No PYNQ, no board."""
    bad = []

    def expect(cond, msg):
        if not cond:
            bad.append(msg)

    expect(core_faults("c", AP_IDLE) == [], "idle core reported a fault")
    expect(core_faults("c", AP_IDLE | AP_START), "ap_start not caught")
    expect(core_faults("c", AP_IDLE | AP_DONE), "ap_done not caught")
    expect(core_faults("c", 0), "cleared ap_idle not caught")
    expect(core_faults("c", AP_IDLE | AP_READY), "ap_ready not caught")
    expect(core_faults("c", AP_IDLE | AP_AUTORESTART),
           "auto_restart not caught")

    expect(channel_faults("d", 0, DMASR_HALTED) == [],
           "halted channel reported a fault")
    expect(channel_faults("d", 0, 0), "running channel not caught")
    expect(channel_faults("d", DMACR_RS, DMASR_HALTED), "RS set not caught")
    expect(channel_faults("d", DMACR_RESET, DMASR_HALTED),
           "Reset in flight not caught")
    for bit, nm in ((DMASR_DMA_INT_ERR, "DMAIntErr"),
                    (DMASR_DMA_SLV_ERR, "DMASlvErr"),
                    (DMASR_DMA_DEC_ERR, "DMADecErr")):
        expect(channel_faults("d", 0, DMASR_HALTED | bit),
               f"{nm} not caught")

    expect(vld_faults("c", "r", 0) == [], "clear ap_vld reported a fault")
    expect(vld_faults("c", "r", 1), "set ap_vld not caught")

    expect(check_reset_cleared({"gier": {"c": 0}}) == [],
           "cleared GIER reported a fault")
    expect(check_reset_cleared({"gier": {"c": 1}}),
           "surviving GIER not caught")

    # The channel-count guard: a state missing an engine must fail even when
    # every engine it DID read is perfectly quiescent.  The expected count is
    # derived from DMA_PARAMS (7 channels: 5 MM2S + ... see board_expect), so
    # adding an engine to the block design cannot leave this test asserting an
    # obsolete literal.
    n = sum((1 if c["mm2s"] else 0) + (1 if c["s2mm"] else 0)
            for c in X.DMA_PARAMS.values())
    full = {"cores": {}, "vld": {}, "gier": {},
            "channels": {f"d{i}": (0, DMASR_HALTED) for i in range(n)}}
    expect(judge(full, [], "selftest-count-ok") == [],
           f"{n} quiescent channels should pass the count guard")
    short = dict(full, channels=dict(list(full["channels"].items())[:n - 1]))
    expect(judge(short, [], "selftest-count-short"),
           "a missing DMA channel was not caught")
    over = dict(full, channels=dict(full["channels"],
                                    **{f"d{n}": (0, DMASR_HALTED)}))
    expect(judge(over, [], "selftest-count-over"),
           "an unexpected extra DMA channel was not caught")

    if bad:
        print("\nSELFTEST FAIL:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nboard_idle_check selftest OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", metavar="BITFILE")
    ap.add_argument("--variant", default=X.DEFAULT_VARIANT,
                    choices=sorted(X.VARIANTS))
    ap.add_argument("--selftest", action="store_true",
                    help="check the decision functions off-board and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.overlay:
        print("CANNOT RUN: --overlay is required (or use --selftest)")
        return 2

    try:
        cfg = X.variant(args.variant)
    except Exception as exc:                           # noqa: BLE001
        print(f"CANNOT RUN: {exc}")
        return 2
    print(f"variant={cfg['name']}  overlay={args.overlay}")

    try:
        from pynq import Overlay
    except ImportError as exc:
        print(f"CANNOT RUN: pynq is not importable ({exc})")
        return 2

    try:
        ol = Overlay(args.overlay)
    except Exception as exc:                           # noqa: BLE001
        print(f"CANNOT RUN: could not load {args.overlay} "
              f"({type(exc).__name__}: {exc})")
        return 2

    print("\n=== phase 1: state after a fresh overlay load ===")
    state1, faults1 = read_state(ol)
    bad = judge(state1, faults1, "phase 1")

    # Read-only, proved rather than asserted.  The first version of this gate
    # reported all seven engines running because evaluating `overlay.<dma>`
    # constructs a PYNQ driver that starts them; this repeat catches any read
    # path that moves the thing it is measuring.  The ap_vld registers are
    # excluded because reading a *_ctrl register legitimately clears its valid
    # flag -- but they are already required to be zero, so a second reading of
    # zero is the only outcome consistent with the first.
    state1b, faults1b = read_state(ol)
    for key in ("cores", "gier", "channels", "vld"):
        if state1[key] != state1b[key]:
            diff = {k: (state1[key][k], state1b[key].get(k))
                    for k in state1[key]
                    if state1[key][k] != state1b[key].get(k)}
            bad.append(f"phase 1: reading the state CHANGED it ({key}: "
                       f"{diff}) -- this gate's own reads have a side "
                       f"effect, so its verdict means nothing")
    bad += [f"phase 1 (second read): {f}" for f in faults1b]
    if not bad:
        print("\n  read-only confirmed: a second full read returned "
              "identical words.")
    if bad:
        # Stop before perturbing: writing to a fabric that is not at rest is
        # exactly what this gate exists to prevent.
        print("\nIDLE FAULTS (phase 1):")
        for b in bad:
            print(f"  - {b}")
        print("\nFAIL: the fabric is not quiescent after a fresh overlay "
              "load. Do NOT start DMA traffic.")
        print("IDLE_CHECK=FAIL;PHASE=1")
        return 1
    print("\nphase 1 OK: every core idle, every DMA channel halted, no stale "
          "ap_vld.")

    bad = perturb(ol)
    if bad:
        print("\nPERTURBATION FAULTS:")
        for b in bad:
            print(f"  - {b}")
        print("IDLE_CHECK=FAIL;PHASE=2a")
        return 1

    print("\n--- phase 2b: reprogram the PL ---")
    try:
        ol = Overlay(args.overlay)
    except Exception as exc:                           # noqa: BLE001
        print(f"PL RESET FAILED: {type(exc).__name__}: {exc}")
        print("IDLE_CHECK=FAIL;PHASE=2b")
        return 1
    print(f"  reloaded {args.overlay}")

    state2, faults2 = read_state(ol)
    bad = check_reset_cleared(state2)
    bad += judge(state2, faults2, "phase 2")
    if bad:
        print("\nRESET FAULTS:")
        for b in bad:
            print(f"  - {b}")
        print("\nFAIL: reprogramming the PL did not return it to power-on "
              "state. Do NOT start DMA traffic.")
        print("IDLE_CHECK=FAIL;PHASE=2c")
        return 1

    print("\nPASS: quiescent after load, AXI-Lite live in both directions, "
          "and a PL reload returns the fabric to power-on state.")
    print("IDLE_CHECK=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
