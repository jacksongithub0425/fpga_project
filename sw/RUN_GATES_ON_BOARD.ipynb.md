# Running the board gates from Jupyter (http://pynq:9090/tree)

Paste each cell into a new notebook on the board, in order. Cell 1 fetches
everything; cells 2–4 are gates 1, 2 and 3. Keep the output — it is the
record that discharges each gate.

Gate order is not a suggestion: each PASS is a precondition of the next, and
gate 2 exists so that a renamed block-design instance is caught cheaply
instead of as a hang at 60 MiB.

---

## Cell 1 — fetch the payload onto the board

```python
import subprocess, os, sys
WORK = "/home/xilinx/gates"
REPO = "https://github.com/jacksongithub0425/FPGA_Accelerator.git"
BRANCH = "agent/publish-hls-sw-docs"

os.makedirs(WORK, exist_ok=True)
os.chdir(WORK)
if not os.path.isdir("repo/.git"):
    print(subprocess.run(["git", "clone", "--depth", "1", "-b", BRANCH,
                          REPO, "repo"], capture_output=True,
                         text=True).stderr[-2000:])
else:
    subprocess.run(["git", "-C", "repo", "fetch", "--depth", "1", "origin",
                    BRANCH], check=False)
    subprocess.run(["git", "-C", "repo", "reset", "--hard",
                    f"origin/{BRANCH}"], check=False)

# Flatten what the gates need into one directory, as BOARD_RUNBOOK.md lists.
import shutil
src = "repo/sw"
bundle = "repo/vivado/three_stage_combined/board_bundle"
for f in ["probe_cma_budget.py", "inspect_overlay.py", "board_gate_full_dma.py",
          "tme_driver.py", "tme_standalone_bringup.py", "binarize_dma_checks.py"]:
    shutil.copy(f"{src}/{f}", f"{WORK}/{f}")
for f in ["three_stage_combined.bit", "three_stage_combined.hwh",
          "BUILD_INFO.txt"]:
    shutil.copy(f"{bundle}/{f}", f"{WORK}/{f}")

# The .bit and .hwh must match the build they were signed as. This is the
# only thing tying a board result to a specific bitstream, so check it
# before loading anything.
import hashlib, re
info = open(f"{WORK}/BUILD_INFO.txt").read()
ok = True
for key, fname in (("bit_sha256", "three_stage_combined.bit"),
                   ("hwh_sha256", "three_stage_combined.hwh")):
    want = re.search(rf"{key}=([0-9A-Fa-f]+)", info).group(1).lower()
    got = hashlib.sha256(open(f"{WORK}/{fname}", "rb").read()).hexdigest()
    print(f"{fname}: {'OK' if got == want else 'MISMATCH'}")
    ok &= got == want
print("\nPAYLOAD VERIFIED" if ok else "\nSTOP: artifact hashes do not match BUILD_INFO")
print(subprocess.run(["ls", "-la", WORK], capture_output=True, text=True).stdout)
```

If the repository is private and the clone fails, upload the nine files
through the Jupyter file browser into `/home/xilinx/gates` instead, then
re-run the hash check portion.

---

## Cell 2 — Gate 1: CMA budget (contract §2.2)

```python
!cd /home/xilinx/gates && sudo python3 probe_cma_budget.py --overlay three_stage_combined.bit
```

Exit 0 = the §2.2 gate passes **in the driver's allocation order**.
Exit 1 = capacity failure; tiling becomes a platform requirement and §2
changes — stop, nothing downstream is worth running.
Exit 2 = could not verify (inconclusive, *not* a capacity failure); it also
means the driver's sizes could not be imported, so the run was only the
weaker two-buffer preflight.

---

## Cell 3 — Gate 2: overlay introspection

```python
!cd /home/xilinx/gates && sudo python3 inspect_overlay.py --overlay three_stage_combined.bit
```

Exit 0 = every IP the driver resolves by name is present with the expected
registers. Exit 1 = overlay and driver disagree — fix `_CORE_NAMES` /
`_DMA_NAMES` or the block design before any driver call. Exit 2 = the
overlay would not load at all (a file problem, not a mismatch).

Capture this output in full: it is the board-side record of the HWH
inspection, and the first place a re-laid-out register map becomes visible.

---

## Cell 4 — Gate 3: the real 63,078,400-byte transfer

```python
!cd /home/xilinx/gates && sudo python3 board_gate_full_dma.py --overlay three_stage_combined.bit
```

Expect a few minutes: the CPU-side verification walks 63 M pixels in numpy
on the PS. The PL round trip itself should be seconds.

PASS requires all five of: MM2S transferred == 63,078,400, S2MM transferred
== 63,078,400, no `0xAA` sentinel byte left in the page, the 64-byte guard
tail untouched, and the output bit-exact against the truncating-Gaussian
oracle. The gate prints each one.

If it fails mid-transfer, **reload the overlay before retrying** — the
driver refuses further work once a stage leaves a transfer outstanding, and
`close()` retains rather than frees those buffers. Restart the kernel.

---

## After the gates

Report back: the three exit codes, the CmaFree figures from gate 1, gate 2's
`ip_dict` / `register_map` dump, and gate 3's DMA-envelope line. Those are
what let §2.2 and §10 item 3 be closed in the contract, and what the
per-stage driver validation builds on.
