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
- **Gate 4's eight fixtures are committed and hashed** (~0.56 MB), so cell 1
  copies them straight out of the pinned checkout and verifies their SHA-256
  before anything loads the overlay. Do not regenerate them on the board.

## Acceptance criteria

Exit 0 is necessary, not sufficient. Proceed only if:

| Gate | Proceed only if |
|---|---|
| 1 — CMA | exit 0; `CmaTotal` **≥ 192 MiB**; output says **driver order**, not the weaker two-buffer preflight; real `CmaTotal`/`CmaFree` values were read |
| 2 — overlay | exit 0; all **3 cores** and **5 DMAs** present; transfer bound **≥ 63,078,400 B**; measured clock **≤ 50 MHz** |
| 3 — full DMA | exit 0; **63,078,400 B** each direction; guard **64 B intact**; **zero** sentinel bytes; **zero** oracle mismatches; no `UNSAFE TEARDOWN` block |
| 4 — extractor + matcher | exit 0; all **8 fixture hashes OK**; **480/480** binary bytes; record `valid=1` at **(3,4)** **14×12**; patch S2MM **received 168 B**; `sts_flags=0`/`rejected=0`/`processed=1`; **9/9** matcher cases, the **251,740 B** case programmed and completed |

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
# Pinned, and this is the ONLY authenticated way to get the scripts onto the
# board: the .bit/.hwh and the eight fixtures are hash-checked below, but
# nothing checks the gate scripts themselves except this commit.
#
# This commit holds gate 4's fixtures IN THE REPOSITORY (hls/integration/ and
# hls/template_match/) with their SHA-256 record, and BOTH gates on the shared
# safe_teardown: signals blocked, a complete-or-refused buffer snapshot,
# recovery output that cannot raise, an in-process PL reset, and a fail-stop
# that holds the pages rather than exiting if that reset fails (see cells 5-6).
# Earlier: dfd54a9 has all of that in gate 4 only — gate 3 still exits on a
# raising close(), releasing ~120 MiB with the DMAs unproved; 36298b9 resets
# the PL but exits on a FAILED reset, releasing the pages it was protecting;
# c7a39e0 has gate 4 but no committed vectors; 6c19cbb and before have a
# close() that retains every buffer after a clean run and no extractor gate at
# all; 01f6cad and before have a gate 3 that cannot distinguish a truncating
# core from a rounding one.
PIN = "775f5bbae916fdd40625f04fc71076b1dcad4f30"

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
          "tme_driver.py", "tme_standalone_bringup.py", "binarize_dma_checks.py",
          # gates 3 and 4 both import this; without it neither starts
          "safe_teardown.py"]:
    shutil.copy(src / f, WORK / f)
for f in ["three_stage_combined.bit", "three_stage_combined.hwh",
          "BUILD_INFO.txt"]:
    shutil.copy(bundle / f, WORK / f)

# Gate 4's eight fixtures come from the PINNED CHECKOUT, never regenerated
# here: a vector regenerated on the board would make the gate agree with
# whatever it had just produced. shutil.copy raises if one is absent, which
# is what we want - a partial payload must not reach the fabric.
shutil.copy(src / "GATE4_VECTORS.sha256", WORK / "GATE4_VECTORS.sha256")
for f in ["tb_bpe_tme_cases.txt", "tb_bpe_tme_gray.bin", "tb_bpe_tme_bin.bin",
          "tb_bpe_tme_patch.bin", "tb_bpe_tme_templs.bin"]:
    shutil.copy(repo / "hls" / "integration" / f, WORK / f)
for f in ["tb_tme_cases_hw.txt", "tb_tme_patches_hw.bin",
          "tb_tme_templs_hw.bin"]:
    shutil.copy(repo / "hls" / "template_match" / f, WORK / f)

# The .bit/.hwh are the only things tying a board result to a build, and
# gate 4's fixtures are the only things tying its verdict to exact bytes.
# BOTH are checked here, before anything is loaded into the fabric.
ok = True

info = (WORK / "BUILD_INFO.txt").read_text()
for key, fname in (("bit_sha256", "three_stage_combined.bit"),
                   ("hwh_sha256", "three_stage_combined.hwh")):
    want = re.search(rf"{key}=([0-9A-Fa-f]+)", info).group(1).lower()
    got = hashlib.sha256((WORK / fname).read_bytes()).hexdigest()
    print(f"{got}  {fname}  {'OK' if got == want else 'MISMATCH'}")
    ok &= got == want

# Gate 4's fixtures, against the committed record. Printed in full: these
# eight hashes are what a gate-4 result is quoted against.
print()
record = {}
for line in (WORK / "GATE4_VECTORS.sha256").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        digest, _, path = line.partition("  ")
        record[Path(path.strip()).name] = digest.lower()
if len(record) != 8:
    raise RuntimeError(f"the hash record lists {len(record)} files, expected 8")
total = 0
for fname, want in record.items():
    blob = (WORK / fname).read_bytes()
    got = hashlib.sha256(blob).hexdigest()
    total += len(blob)
    print(f"{got}  {fname}  ({len(blob):,} B)  "
          f"{'OK' if got == want else 'MISMATCH'}")
    ok &= got == want
print(f"{len(record)} fixtures, {total:,} B")

if not ok:
    raise RuntimeError(
        "payload hash mismatch; STOP. Do not load the overlay and do not "
        "regenerate the vectors here - re-fetch them from the pinned commit.")

print("\nPAYLOAD VERIFIED")
print(subprocess.run(["ls", "-la", str(WORK)], capture_output=True,
                     text=True).stdout)
```

If the repository is private and the clone fails, upload the eight scripts,
the three bundle artifacts and the nine gate-4 files (the record plus its
eight vectors) into `/home/xilinx/gates` by hand and re-run from the
`ok = True` line.

**Not for signoff.** That fallback is a debugging convenience, not an
authenticated payload: the hash check below compares the vectors against
`GATE4_VECTORS.sha256`, and a hand upload can replace the record and the
vectors together, so the two agree and prove nothing. The gate scripts are
not hashed at all — only `PIN` vouches for them. A result quoted as a gate
signoff must come from the clone path, from `PIN`, with cell 1's output
showing the checkout hash it printed.

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

The gate handles its own recovery now — read what it printed and match it to
one of these four. **Restarting the kernel is never the answer:** it does not
touch the PL, and the gate is its own `sudo` process whose in-process guard
died with it.

| What you saw | Do this |
|---|---|
| FAIL or exit 2, no `UNSAFE TEARDOWN` block | **Nothing.** Every armed DMA was proved halted; fix the reported problem and re-run. |
| `UNSAFE TEARDOWN` then `PL reset:` | Gate failed, board is usable — the gate reloaded the overlay itself. Re-run cell 2 (gate 1) before continuing. |
| `PL RESET FAILED` then `FAIL-STOP`, cell still running | **POWER-CYCLE.** Not `reboot`, not `kill -9`: both free the pages while the fabric is live. |
| The process died anyway (SIGKILL, OOM-killer, crash, power blip) | **POWER-CYCLE before any further CMA use.** Its pages went back with the fabric in an unknown state, and no software can clean that up. |

The gates ignore SIGINT/SIGTERM/SIGHUP/SIGQUIT from the moment the pipeline
exists, which is what stops row two from becoming row four. You will see a
`teardown protection: ignoring ...` line right after the overlay loads; if it
is missing, the run is not protected — stop and report it.

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
silicon manifest through **this** overlay.

Expect seconds, not minutes: everything here is small except the two 820×307
stress cases.

**Three things to get right when writing up a PASS.**

- It is the **first extractor run through `PLPipeline` in the combined
  overlay**, not the first extractor run on silicon. Both cores already have
  standalone silicon results; what is new is the two of them in one overlay
  behind one driver.
- It is a **combined-overlay smoke gate**: ONE extractor candidate plus the
  nine matcher cases. It does not exercise multi-candidate rearming or TLAST
  across a batch, and it does not exercise the per-kind argmax: phase D gives
  each kind exactly ONE trial, so `by_kind[kind]` reduces over a single
  element and cannot choose. Its two kinds scoring 1.0 apiece tests the
  GLOBAL best and the tie rule instead — a different reduction. A real page
  carries several templates per kind. Those are the next protocol tests, and
  they come before the strict 36-page PL-backend comparison, not after it.
- The envelope case is **251,740 B programmed; the core completed and the
  DMA became idle without error**. Both matcher channels are MM2S, so no
  engine anywhere on that path counts received bytes — `MM2S_LENGTH` is
  essentially the length the driver asked for. The gate prints it in exactly
  those words; keep them.

A phase that fails names itself and stops; the phases after it did not run,
so do not report them as passing. Exit 2 means a missing or mismatched
fixture, or an overlay that would not load — a payload or environment
problem, and never evidence about the hardware.

**Unsafe teardown is handled inside the gate.** If `close()` cannot prove a
DMA halted, `board_gate_extract.py` reloads the overlay itself before
returning, while the retained buffers are still referenced. It has to: those
references live only as long as the gate's own `sudo` process, so by the time
this notebook sees the exit code the CMA pages would already be back in the
pool. The same applies if `close()` raises instead of returning False. You
will see an `UNSAFE TEARDOWN` block followed by one of two things:

- `PL reset` — the overlay reloaded, board recoverable, gate still failed.
  Re-check gate 1 before running anything else.
- `PL RESET FAILED`, then a `FAIL-STOP` banner — **and the cell will not
  finish.** That is deliberate, not a hang: the process is holding the
  pipeline and all seven CMA buffers because exiting would release them with
  the fabric in an unknown state, and it heartbeats every five minutes to show
  it is still holding.

  **POWER-CYCLE the board.** Not `reboot`: shutdown terminates userspace
  first and resets the hardware after, so it would kill the holder while the
  fabric is still live — the same window with a friendlier name. Do not
  `kill -9` it, and do not restart the kernel to "clear" it. Interrupting the
  kernel will not work anyway: SIGINT, SIGTERM, SIGHUP and SIGQUIT are all
  ignored from the moment teardown starts.

**Gate 3 (cell 5) behaves identically** and holds far more — the two ~60 MiB
image buffers. Same banner, same answer; the four-row table under gate 3
applies to gate 4 unchanged.

---

## Report back

The checkout hash cell 1 printed (a result without it is not a signoff), the
four `GATE*_EXIT` lines, gate 1's `CmaTotal`/`CmaFree`, gate 2's `ip_dict`
and `register_map` dump, gate 3's `DMA envelope:` line, and gate 4's final
`EXTRACTOR GATE PASSED` summary.

Those close contract §2.2 and §10 item 3 and put all three driver stages
through the combined overlay once. What they do NOT do is qualify the PL
backend for real pages — gate 4 is a smoke gate on one candidate. Next, in
order: multi-candidate extractor rearming and TLAST across a batch, and a
per-kind argmax case with two differently-scoring trials of the SAME kind. Then `detect_page()`
behind explicit backends, and only then the strict 36-page comparison.
