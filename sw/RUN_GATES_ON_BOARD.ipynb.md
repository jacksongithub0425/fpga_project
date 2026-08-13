# Running the board gates from Jupyter (http://pynq:9090/tree)

Paste each cell into a new notebook on the board, in order. Every cell
**raises** on failure rather than printing a warning, so a red cell means
stop — you cannot walk past a bad payload or a failed gate by scrolling.

## Before you start

- **`cma=192M` must be on the kernel command line.** This is a platform
  requirement, not a tuning note: the driver-order allocation needs about
  **120.8 MiB of separately contiguous CMA**, and the default **128 MiB**
  pool was tried twice and failed both times. Add `cma=192M` to `bootargs`
  in `/boot/uEnv.txt`, reboot, and confirm with `grep Cma /proc/meminfo`
  (`CmaTotal` ≈ 196608 kB). Gate 1 refuses to run below that and exits 2.
- **Boot the board fresh.**
- **Close any other notebook holding an overlay or CMA buffers.** Even at
  192 MiB, one stale kernel still holding a 60 MiB buffer fails gate 1 for a
  reason unrelated to the design.
- **Gate 4 needs generated vectors that are not in the repository.** Build
  them on the build machine and upload the eight files (cell 1 checks for
  them and says exactly which are missing).

## Acceptance criteria

Exit 0 is necessary, not sufficient. Proceed only if:

| Gate | Proceed only if |
|---|---|
| 1 — CMA | exit 0; `CmaTotal` **≥ 192 MiB**; output says **driver order**, not the weaker two-buffer preflight; real `CmaTotal`/`CmaFree` values were read |
| 2 — overlay | exit 0; all **3 cores** and **5 DMAs** present; transfer bound **≥ 63,078,400 B**; measured clock **≤ 50 MHz** |
| 3 — full DMA | exit 0; **63,078,400 B** each direction; guard **64 B intact**; **zero** sentinel bytes; **zero** oracle mismatches |
| 4 — extractor + matcher | exit 0; **480/480** binary bytes; record `valid=1` at **(3,4)** **14×12**; **168 B** patch DMA; `sts_flags=0`/`rejected=0`/`processed=1`; **9/9** matcher cases incl. the **251,740 B** envelope |

Passing all four validates CMA, overlay/driver compatibility, the full-size
binarizer, and — on one small pinned page — the extractor, the matcher and
the PS reduction between them. It does **not** validate failure recovery or
end-to-end PDF detection.

---

## Cell 1 — fetch the payload, pinned and verified

```python
import subprocess, os, shutil, hashlib, re, sys
from pathlib import Path

WORK = Path("/home/xilinx/gates")
REPO = "https://github.com/jacksongithub0425/FPGA_Accelerator.git"
# Pinned. This commit carries the gate-3 truncation fix and the corrected
# MM2S/S2MM reporting; earlier commits (01f6cad and before) have a gate that
# cannot distinguish a truncating core from a rounding one.
PIN = "5baf875a9514948cd9931c398aef6ece2f1a2a2a"

WORK.mkdir(parents=True, exist_ok=True)
os.chdir(WORK)
repo = WORK / "repo"

# check=True throughout: a failed clone or fetch must not leave stale
# software in place to be tested as though it were current.
if not (repo / ".git").is_dir():
    subprocess.run(["git", "clone", REPO, str(repo)], check=True)
subprocess.run(["git", "-C", str(repo), "fetch", "origin"], check=True)
subprocess.run(["git", "-C", str(repo), "checkout", "--force", PIN], check=True)

head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                      capture_output=True, text=True, check=True).stdout.strip()
if head != PIN:
    raise RuntimeError(f"checkout is {head}, expected {PIN}; STOP")
print("checked out", head)

src = repo / "sw"
bundle = repo / "vivado" / "three_stage_combined" / "board_bundle"
for f in ["probe_cma_budget.py", "inspect_overlay.py", "board_gate_full_dma.py",
          "board_gate_extract.py",
          "tme_driver.py", "tme_standalone_bringup.py", "binarize_dma_checks.py"]:
    shutil.copy(src / f, WORK / f)
for f in ["three_stage_combined.bit", "three_stage_combined.hwh",
          "BUILD_INFO.txt"]:
    shutil.copy(bundle / f, WORK / f)

# Gate 4's vectors are GENERATED and deliberately not committed, so they
# cannot come from the checkout. Name the missing ones rather than letting
# gate 4 discover them later: on the build machine run
#   cd hls/integration    && python3 pe_tme_generate_golden.py
#   cd hls/template_match && python3 tme_generate_golden.py
# and upload the eight files into /home/xilinx/gates.
VECTORS = ["tb_bpe_tme_cases.txt", "tb_bpe_tme_gray.bin", "tb_bpe_tme_bin.bin",
           "tb_bpe_tme_patch.bin", "tb_bpe_tme_templs.bin",
           "tb_tme_cases_hw.txt", "tb_tme_patches_hw.bin",
           "tb_tme_templs_hw.bin"]
absent = [f for f in VECTORS if not (WORK / f).is_file()]
if absent:
    print("GATE 4 VECTORS MISSING (gates 1-3 can still run):")
    for f in absent:
        print("   ", f)

# The .bit/.hwh are the only things tying a board result to a build. Check
# them before loading anything into the fabric.
info = (WORK / "BUILD_INFO.txt").read_text()
ok = True
for key, fname in (("bit_sha256", "three_stage_combined.bit"),
                   ("hwh_sha256", "three_stage_combined.hwh")):
    want = re.search(rf"{key}=([0-9A-Fa-f]+)", info).group(1).lower()
    got = hashlib.sha256((WORK / fname).read_bytes()).hexdigest()
    print(f"{fname}: {'OK' if got == want else 'MISMATCH'}")
    ok &= got == want
if not ok:
    raise RuntimeError("artifact hash mismatch; STOP")

print("\nPAYLOAD VERIFIED")
print(subprocess.run(["ls", "-la", str(WORK)], capture_output=True,
                     text=True).stdout)
```

If the repository is private and the clone fails, upload the nine files into
`/home/xilinx/gates` by hand and re-run from the `info = ...` line.

---

## Cell 2 — the gate runner

```python
import subprocess, sys, os
from pathlib import Path

WORK = Path("/home/xilinx/gates")

# Diagnose sudo once, here, so that a sudo problem is never reported as a
# gate failure. -n means "never prompt": it fails instead of hanging on a
# password prompt the notebook cannot answer.
PREFIX = [] if os.geteuid() == 0 else ["sudo", "-n"]
if PREFIX:
    probe = subprocess.run(PREFIX + ["true"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise RuntimeError(
            "passwordless sudo is unavailable, so the gates cannot run as "
            f"root: {probe.stderr.strip()!r}. Run the notebook as root or "
            "configure sudo; do NOT read this as a gate failure.")
print("running as", "root" if not PREFIX else "xilinx via sudo -n")

def run_gate(label, script):
    p = subprocess.run(
        PREFIX + [sys.executable, "-u", script,
                  "--overlay", "three_stage_combined.bit"],
        cwd=WORK
    )
    print(f"{label}_EXIT={p.returncode}", flush=True)
    if p.returncode != 0:
        raise RuntimeError(f"{label} did not pass; STOP here")
```

`-u` keeps the child's output unbuffered so it interleaves in the notebook
as it happens rather than arriving in a lump at the end.

---

## Cell 3 — Gate 1: CMA budget (contract §2.2)

```python
run_gate("GATE1", "probe_cma_budget.py")
```

Read the output, not just the exit code:

- it must say the plan is the **driver order** — five smaller buffers before
  the two full-page ones. If it fell back to the two-buffer preflight it
  exits 2 and `run_gate` raises, which is correct: that is not the §2.2 gate;
- `CmaTotal` / `CmaFree` must be real numbers, not "unavailable";
- exit 1 = genuine capacity failure. Tiling becomes a platform requirement
  and contract §2 changes. Stop; nothing downstream is worth running.

---

## Cell 4 — Gate 2: overlay introspection

Only after gate 1 has passed.

```python
run_gate("GATE2", "inspect_overlay.py")
```

Check in the output: all **3 cores** (`binarize_core_0`,
`patch_extract_core_0`, `tme_top_0`) and all **5 DMAs**
(`axi_dma_binarize`, `dma_pe_data`, `dma_pe_meta`, `axi_dma_patch`,
`axi_dma_templ`); the binarize DMA's single-transfer bound **≥ 63,078,400 B**;
and the measured PL clock **≤ 50 MHz** (the image is constrained at 20 ns —
anything faster invalidates the timing closure recorded in contract §8).

Keep this cell's full output. It is the board-side record of the HWH
inspection, and the first place a re-laid-out register map shows up.

---

## Cell 5 — Gate 3: the real 63,078,400-byte transfer

Only after gate 2 has passed.

```python
run_gate("GATE3", "board_gate_full_dma.py")
```

Expect a few minutes — the CPU-side verification walks 63 M pixels through
numpy on the PS. The PL round trip itself should be seconds.

PASS requires the bit-exact compare **and** the DMA-envelope line:
`63,078,400 B` received on S2MM, MM2S programmed to the same, guard 64 B
intact, zero sentinel bytes. Note the asymmetry when quoting it —
`S2MM_LENGTH` is written by the engine with what it actually received, so
that count is a measurement; `MM2S_LENGTH` is essentially the length the
driver programmed, so it corroborates rather than measures, and the outbound
direction rests on the channel going idle with no error, `ap_done`, and the
core consuming a fixed beat count by construction.

### If gate 3 fails after the DMA has started

**Restarting the kernel is not enough.** Each gate runs as its own `sudo`
process; when it dies, the driver's in-process guard dies with it while the
hardware keeps its state, and an S2MM with an open command can still write
into pages the kernel later hands to someone else. The notebook kernel does
not touch the PL.

Before any further CMA use:

```python
# 1. reprogram the PL — this resets the DMA engines and the cores
from pynq import Overlay
Overlay("/home/xilinx/gates/three_stage_combined.bit")
```

If a transfer still cannot be shown to have stopped, or allocation behaves
oddly afterwards, **reboot**; power-cycle if the board stops responding.

---

## Cell 6 — Gate 4: the extractor and the matcher

Only after gate 3 has passed, and only with the eight vector files present
(cell 1 lists any that are missing).

```python
run_gate("GATE4", "board_gate_extract.py")
```

Five phases on one pinned 24×20 page whose every intermediate byte is known
in advance — 480 gray → 480 binary → a 168-byte 14×12 patch at (3,4) →
matcher `+1.000000` at page (7,5) — followed by the matcher's own 9-case
silicon manifest through **this** overlay, including the 251,740-byte
maximum-envelope transfer.

Expect seconds, not minutes: everything here is small except the two 820×307
stress cases.

A phase that fails names itself and stops; the phases after it did not run,
so do not report them as passing. Exit 2 means missing vectors or an overlay
that would not load — an environment problem, not a gate failure.

---

## Report back

The four `GATE*_EXIT` lines, gate 1's `CmaTotal`/`CmaFree`, gate 2's
`ip_dict` and `register_map` dump, gate 3's `DMA envelope:` line, and gate
4's final `EXTRACTOR GATE PASSED` summary. Those close contract §2.2 and §10
item 3 and validate all three driver stages; the remaining step is wiring
`detect_page()` behind explicit backends and running the strict 36-page
comparison.
