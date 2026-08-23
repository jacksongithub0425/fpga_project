# Board runbook — three_stage_combined bring-up gates

Ordered gates for the combined three-core overlay. Run them **in this order**;
each one's PASS is a precondition of the next.

## Required platform setting: `cma=192M`

**This is a prerequisite, not a tuning note.** The driver-order allocation
needs about **120.8 MiB of separately contiguous CMA**. The PYNQ default pool
is **128 MiB**, a 7 MiB margin that has to absorb all fragmentation — and it
**was tried twice at 128 MiB and failed both times**. Treat 128 MiB as a
known-bad configuration.

```
# /boot/uEnv.txt — append to bootargs, then REBOOT
bootargs=... cma=192M

# confirm before running anything else
grep Cma /proc/meminfo        # CmaTotal should read ~196608 kB
```

CMA is reserved at boot and cannot be resized afterwards, so this costs a
reboot. `probe_cma_budget.py` refuses to run below 192 MiB and exits **2** —
misconfigured platform, **not** a §2.2 capacity failure. Do not record a
small-pool failure as the gate, and do not let it trigger the tiling branch:
the pool being too small says nothing about whether a correctly sized pool
can satisfy the request.

192 MiB leaves ~71 MiB of headroom over the allocation and still leaves about
290 MiB of userspace — comfortably above the 98.7 MiB measured peak of the
row-strip verification in gate 3.

### What the board is actually set to

Measured 2026-08-21: the board's kernel command line carries **`cma=224M`**
(`CmaTotal 229376 kB`), not the `cma=192M` this document asks for. 224 MiB
satisfies the requirement with 32 MiB to spare, so nothing needs changing —
but do not be surprised by the number, and do not "correct" it downward.

**`CmaFree` does not predict whether the allocation will succeed.** The same
2026-08-21 session ran the full driver-order allocation from a starting
`CmaFree` of **20.0 MiB** and it passed: the kernel migrates page-cache pages
out of the CMA region to satisfy a contiguous request. A low `CmaFree` is
worth recording and is not a capacity failure.

The converse does not follow either: **a warm-boot pass is not a "harder"
test than a fresh-boot one.** `CmaFree` and allocator state are non-monotonic,
and the three 2026-08-21 runs were handed identical physical ranges, which is
consistent with reuse of just-freed regions. Do not argue fragmentation
robustness from an uptime figure.

`probe_cma_budget.py` allocates seven buffers in the driver's order, but only
**five** of them come from `PLPipeline.__init__` — the grayscale and binary
buffers are lazy (`_ensure_image_bufs`), so importing the driver does not by
itself decide the §2.2 gate. The probe reproduces the correct overall
sequence, which is the property that matters.

## Before you start

**Boot the board fresh, and close any other notebook holding an overlay or
CMA buffers.** Even at `cma=192M`, a stale kernel still holding a 60 MiB
buffer is enough to fail gate 1 for a reason that has nothing to do with the
design.

### Use `sudo -E`, not `sudo`

On this image plain `sudo python3` fails twice over, and neither failure names
the cause:

```
sudo python3          -> ModuleNotFoundError: No module named 'pynq'
                         (sudo resets PATH, so this is /usr/bin/python3,
                          not /usr/local/share/pynq-venv/bin/python3)
sudo <venv>/python3   -> RuntimeError: No Devices Found
                         ("is the XRT environment sourced?" — sudo dropped
                          XILINX_XRT=/usr)
sudo -E python3       -> works
```

Verified 2026-08-21 (`logs/b2prod_20260821/preflight/10_sudo_invocation.txt`).
A Jupyter cell needs no `sudo` at all: the notebook server already runs as
root inside the venv.

## The variant preflight — run this first

```
sudo -E python3 board_preflight.py --variant combined_b2_100
```

One command that runs the whole preflight in order and stops at the first
failure: payload digests (against both `board_expect.py`'s pinned values and
the shipped `BUILD_INFO.txt`), the CMA pool size, gate 1, gate 2 with the
variant's expectations, and then the reset/idle check.

**It reports two statuses, and only one of them is the verdict.**

```
WARM_BOOT_TECHNICAL_PREFLIGHT=PASS    the six technical checks
FORMAL_FRESH_BOOT_PREFLIGHT=PASS      those checks AND a fresh boot
PREFLIGHT=PASS                        emitted only when both hold
```

The plan requires the preflight be performed after a fresh boot, and
completion condition 4 is worded the same way. A warm-boot run that passes
everything technical prints `FORMAL_FRESH_BOOT_PREFLIGHT=HOLD`,
`PREFLIGHT=HOLD` and exits **3** — a distinct code, because it is not a
technical failure and must not be triaged as one. `--max-uptime-s` (default
3600) moves the bound; `--allow-warm-boot` changes the exit code and **nothing
else**, the wording still reads HOLD.

The uptime bound is a proxy. The property that matters is "nothing has run
since boot", which no counter reports; uptime approximates it and the
`CmaFree` reading beside it is the cross-check.

**Grep `^PREFLIGHT=` anchored.** `WARM_BOOT_TECHNICAL_PREFLIGHT=PASS` contains
the substring `PREFLIGHT=PASS`, so an unanchored search reads a HOLD as a
pass. The verdict is always the line that *starts* with `PREFLIGHT=`, and
there is exactly one.

### What the live clock check does and does not prove

`Clocks.fclk0_mhz` is a **divisor/register read-back**, not an edge count.
PYNQ computes it from the PLL model and the FCLK0 divisors in the SLCR. That
is exactly the right check for the 62.5 MHz trap, which corrupts those
divisors — but a PLL that failed to lock would still read 100.0.

The independent check is the known-cycle case, and it belongs in Stage 1, not
here: **B2 at 820×307 / 216×96 is L = 257,145,732 cycles = 2.5715 s at
100 MHz**, with the nearest wrong clock rung 257 ms away. Run it and compare
wall time. The preflight is not a substitute for it.

**`--variant` is not cosmetic.** `sw/board_expect.py` is the board half of the
variant table in `three_stage_combined/scripts/run_postextract_signoff.tcl`,
and it decides the expected matcher VLNV and the expected *board* frequency —
which is not the requested one. The baseline requests 50 MHz, Vivado
constrains 20.000 ns, and the board runs **31.25** = 1000/32. Default is
`baseline`, so an un-parameterised call keeps its old meaning; an unknown name
is fatal rather than silently checking against the baseline.

| variant | matcher | Vivado | divisors | board fclk0 |
|---|---|---|---|---|
| `baseline` | `TermCount:hls:tme_top:0.2` | 20.000 ns | 8×4 = 32 | **31.25** |
| `combined_current_100` | `TermCount:hls:tme_top:0.2` | 10.000 ns | 5×2 = 10 | **100.00** (no board bundle) |
| `combined_b2_100` | `TermCountB2:hls:tme_top:0.2` | 10.000 ns | 5×2 = 10 | **100.00** |

**Two clocks are gated for the 100 MHz variants, and that is not
belt-and-braces.** PYNQ's power-on `fclk0` on this board is already **100.0**,
so for those variants an fclk0-only check is fail-open: an overlay that never
programmed the clocks would read a perfect 100.0. `fclk1` is what closes it —
the recipe enables FCLK1 at 125 MHz, and PYNQ's default is 142.857143, so
reading **125.0** is positive evidence that this overlay's divisors were
applied. For `baseline`, fclk1 is recorded and not gated.

### Gate 2b — reset and idle, before DMA traffic

```
sudo -E python3 board_idle_check.py --overlay three_stage_combined.bit \
                                    --variant combined_b2_100
python3 board_idle_check.py --selftest      # off-board
```

Phase 1 requires the power-on state: cores `ap_idle` with nothing started or
pending, every DMA channel `Halted` with `RS` clear and no error bit, and
every `ap_vld` sideband clear. Phase 2 writes `GIER` on each core (inert — all
three `interrupt` ports are unconnected in the BD), reprograms the PL, and
requires the write to be gone: that is what proves the AXI-Lite path is live
in both directions and that a reload really does reset the fabric, which every
recovery path in `safe_teardown.py` assumes.

**It reads through raw `pynq.MMIO`, never `getattr(overlay, dma)`, and that is
load-bearing.** `pynq.lib.dma.DMA` starts every channel in its constructor, so
resolving a DMA attribute writes `DMACR.RS = 1`. Measured 2026-08-21: the
seven channels read `0x00010002 / 0x00000001` (Halted) after programming and
`0x00010003 / 0x00000000` (running) after nothing but the driver objects being
constructed — 7 of 7, with no transfer requested. The first version of this
gate used driver objects and reported all seven as running; the fault was its
own. Phase 1 now reads the whole state twice and requires identical words, so
a read with a side effect cannot pass again.

`inspect_overlay.py` still has to construct those drivers to inspect the
channels, so it leaves the engines started and says so. Nothing may judge the
state it leaves behind — that is why the idle check reloads the overlay.

Off-board before any board time:

```
python3 test_board_preflight.py    # 47 checks: 33 injected defects, each
                                   # required to fail, plus 4 clean controls
```

## Acceptance criteria

Exit 0 is necessary but not sufficient. Proceed to the next gate only if:

| Gate | Proceed only if |
|---|---|
| 1 — CMA | exit 0; `CmaTotal` **≥ 192 MiB**; the output says **driver order** (not the weaker two-buffer preflight); real `CmaTotal`/`CmaFree` figures were read, not "unavailable" |
| 2 — overlay | exit 0; all **3 cores** and **5 DMAs** present; binarize DMA transfer bound **≥ 63,078,400 B**; live `Clocks.fclk0_mhz` equal to the **variant's** board frequency (see `--variant` below) |
| 3 — full DMA | exit 0; **63,078,400 B** each direction; guard **64 B intact**; **zero** sentinel bytes remaining; **zero** oracle mismatches; teardown freed the buffers (no `UNSAFE TEARDOWN` block) |
| 4 — extractor + matcher | exit 0; all **8 fixture hashes OK**; **480/480** binary bytes; record `valid=1` at **(3,4)**, **14×12**; patch S2MM **received 168 B**; `sts_flags=0`, `rejected=0`, `processed=1`; **168/168** patch bytes; **9/9** matcher cases, the **251,740 B** case programmed and completed; teardown freed the buffers |
| 5 — protocol | exit 0; all **5 fixture hashes OK**; five batches of **4/4/1/2/2** descriptors; metadata S2MM **received** `n × 16` B every time; patch receives re-armed across **[950, 650, 722, 352]** B; the per-kind argmax picks the higher-scoring trial of the SAME kind, and both controls move the answer |
| 6 — counted clock | exit 0; both probes compute their golden result; absolute residual inside **−0.5 .. +10 ms**; the **differential** probe implies the variant's board frequency within **0.5%**; the nearest `1000/d` rung to that implied clock IS the expected one |
| 7 — recovery | exit 0; the short deadline raises **TimeoutError** bounded and before the transaction would have finished; all three entry points refuse; `close()` returns **False** (retained, not freed); `reset_pl` returns **True**; **9/9** again on a fresh pipeline, twice — after the deadline and after the wedge |

**What passing all four does and does not establish.** It validates the CMA
budget, overlay/driver compatibility, the full-size binarizer, and — on one
small pinned page — the extractor, the matcher and the PS reduction between
them. It does **not** validate any failure-recovery path or end-to-end PDF
detection; the 36-page comparison against the CPU baseline is still owed and
is the last step. All run on the PYNQ board as root (`sudo`), from one
directory containing:

```
three_stage_combined.bit      # from vivado/three_stage_combined/board_bundle/
three_stage_combined.hwh      # same bundle — MUST sit next to the .bit, same basename
BUILD_INFO.txt                # same bundle — the SHA-256 record; copy it too
board_preflight.py            # the orchestrator; run this first
board_expect.py               # the variant table it and gate 2 read
board_idle_check.py           # gate 2b
probe_cma_budget.py
inspect_overlay.py
board_gate_full_dma.py
board_gate_extract.py
board_gate_protocol.py        # gate 5; imports board_gate_extract for its Report
board_gate_clock.py           # gate 6; same, and reuses gate 4's hw fixtures
board_gate_recovery.py        # gate 7; same
tme_driver.py
tme_standalone_bringup.py
binarize_dma_checks.py
safe_teardown.py              # the shared teardown; gates 3, 4 and 5 import it

# gate 4's fixtures — COMMITTED, ~0.56 MB; copy, do not regenerate
GATE4_VECTORS.sha256          # the hash record; gate 4 refuses to run without it
tb_bpe_tme_cases.txt          # from hls/integration/
tb_bpe_tme_gray.bin
tb_bpe_tme_bin.bin
tb_bpe_tme_patch.bin
tb_bpe_tme_templs.bin
tb_tme_cases_hw.txt           # from hls/template_match/
tb_tme_patches_hw.bin
tb_tme_templs_hw.bin

# gate 5's fixtures — COMMITTED, ~16 KB; same rule, copy do not regenerate
GATE5_VECTORS.sha256          # the hash record; gate 5 refuses to run without it
tb_proto_cases.txt            # from hls/integration/
tb_proto_gray.bin
tb_proto_bin.bin
tb_proto_patches.bin
tb_proto_templs.bin
```

**Copy gate 4's eight fixtures from the pinned checkout; do not regenerate
them on the board.** They are committed precisely so that a board result can
be tied to exact bytes — a vector regenerated on the board would make the
gate agree with whatever it had just produced. `board_gate_extract.py`
SHA-256s all eight against `GATE4_VECTORS.sha256` **before it loads the
overlay**, prints each hash, and a missing or mismatched file is fatal
(exit 2). A directory holding some but not all of a group is fatal too,
rather than falling back to the repository for the rest: that would run the
gate against a mixture of two payloads.

The three `tb_tme_*_hw.*` files are byte-identical to the vectors that
passed 9/9 on silicon on 2026-08-07 — verified, not assumed.

Gate 5's five fixtures follow the identical rule and `board_gate_protocol.py`
enforces it the identical way, against `GATE5_VECTORS.sha256`. It also checks
the four blobs against SHA256 rows carried inside `tb_proto_cases.txt` itself:
the record ties the payload to the commit, while those rows tie the blobs to
THAT manifest, so a manifest and a blob from two different regenerations
cannot be silently combined.

`BUILD_INFO.txt` is part of the payload, not documentation left behind: it is
the only thing on the board that ties a result to a specific build, and the
verification below cannot be done without it.

The bitstream pair is committed at `vivado/three_stage_combined/board_bundle/`
(the deliberate exception to the no-Vivado-outputs data policy), together
with `BUILD_INFO.txt`. Check the copies you put on the board against the
SHA-256 values recorded there before running anything — that record is the
only thing tying a board result to a specific build:

```
sha256sum three_stage_combined.bit three_stage_combined.hwh
grep -E 'bit_sha256|hwh_sha256' BUILD_INFO.txt
```

On the build machine the same pair also lives in the untracked Vivado
project at `../../three_stage_combined/postextract_board_bundle_20260811_003143/`
(relative to the repository root); the two are byte-identical.

## Gate 0 — retain the CPU baseline (do this before anything else)

Not a board step, and it must happen **before** any PL stage is wired into
`detect_page()` — once integration starts, the un-accelerated behaviour is no
longer reproducible on demand, and "the FPGA agrees with the CPU" becomes a
claim with nothing behind it.

```
cd sw
python3 cpu_baseline_snapshot.py capture "../../sample/*" \
    --out ../../baseline_cpu_<date>
```

**Quote the pattern.** The script expands globs itself and matches suffixes
case-insensitively, because the shell cannot be relied on to: PowerShell does
not expand wildcards for native programs at all, and a bare `*.PDF` misses
the three lowercase `.pdf` files in this corpus on a case-sensitive
filesystem. A pattern matching nothing is an error, not an empty run.

Retained in the repo as `sw/cpu_baseline_20260811.csv` (36 pages over 35
sample drawings) with `sw/cpu_baseline_20260811_provenance.json` beside it,
recording the capture time, git revision (marked `-dirty` when the tree had
uncommitted edits), the detector's SHA-256, every threshold, and the
numpy/OpenCV versions — so a future mismatch can be attributed to the
detector or to the parameters rather than guessed at. Each manifest row
carries an anonymized id, the input PDF's SHA-256, the per-kind counts, and
a SHA-256 over the canonical detection list.

The per-page dumps stay **local and uncommitted** — they contain labels read
off confidential drawings; the digests do not. `capture` refuses a `--out`
inside the repository, and `.gitignore` covers `*_detections.json` and
`baseline_cpu_*/` as a backstop.

To check a PL-integrated detector against it:

```
python3 cpu_baseline_snapshot.py compare "../../sample/*" \
    --manifest cpu_baseline_20260811.csv
```

**Full coverage is required and is part of the check.** All 36 manifest
pages must be produced exactly once; missing, extra and duplicated pages
each fail alongside any digest divergence. Exit 0 therefore means "all 36
checked and byte-identical", never "the pages I happened to see matched".
`--allow-subset` relaxes coverage for spot checks and labels its own output
`SUBSET OK (NOT A GATE)` — do not record it as parity.

Counts alone would not be enough either: a box that moved or two detections
that swapped kinds leave the totals untouched, which is exactly the kind of
drift a PL stage introduces. Verified 2026-08-11 — 36/36 byte-identical, a
perturbed `--score-thresh` diverges 23 of 36 pages, and a single-page run
now fails on coverage.

## Gate 1 — CMA budget (contract §2.2, §10 item 3)

```
sudo -E python3 probe_cma_budget.py --overlay three_stage_combined.bit
```

Proves the two **separately contiguous** ~60.2 MiB image buffers can be
allocated with the overlay resident, **in the driver's own allocation
order** — the five smaller buffers `PLPipeline.__init__` takes first, then
the two full-page ones. CMA fragmentation is order-dependent, so that
sequence is the question that matters; the probe imports its sizes straight
from `tme_driver` so the two cannot drift. `--overlay` is not optional in
spirit: probing a pristine pool and then allocating in a different order in
production is how this gate passes on the bench and fails in the field.

If `tme_driver.py` is not next to the probe it falls back to allocating only
the two page buffers, says so, and exits 2 — that result is a weaker
capacity preflight and must not be recorded as the §2.2 gate.

- PASS (exit 0) → record the CmaFree numbers it prints and close §10 item 3.
- FAIL (exit 1) → **tiling becomes a platform requirement and §2 changes.**
  Stop here; nothing downstream is worth running.
- exit 2 → *could not verify* (for example `/proc/meminfo` carries no CMA
  fields, or the overlay would not load). This is inconclusive, **not** a
  capacity failure: do not trigger the tiling branch on it. Fix the
  environment and re-run.

A PASS is evidence for this boot, not permanent proof — re-run after reboots.

## Gate 2 — overlay introspection (the HWH-and-driver inspection, board half)

```
sudo -E python3 inspect_overlay.py --overlay three_stage_combined.bit \
                                   --variant combined_b2_100
```

Prints `ip_dict`, `hierarchy_dict`, the measured PL clocks, every core's
`register_map` and every DMA's channel configuration, and checks them against
what `tme_driver.py` resolves by name and what `board_expect.py` pins for the
variant. Capture the full output into the repo (`docs/` or the bundle
directory) — it is the board-side record of the HWH inspection.

Five things are gated, and three of them used to be literals of the shipping
image that would have been silently wrong for any other build:

1. the **live `Clocks.fclk0_mhz`**, against the variant's derived board
   frequency — previously only a printed NOTE, so a build that landed on the
   62.5 MHz divisor trap would have exited 0;
2. **`fclk1`** for variants that drive it, which is the check that is not
   fail-open when the fclk0 target equals PYNQ's power-on default;
3. the **matcher VLNV**, which nothing checked, so a bitstream built from the
   wrong `tme_top` passed every name-based check in the file;
4. the **base addresses**, pinned from the shipped HWH;
5. the **register offsets** — not just the names. A name check passes an IP
   whose ports were reordered, because HLS keeps the names and moves the
   addresses, and the driver writes offsets (§7.1.2).

The binarize single-transfer bound is gated too, and a bound PYNQ does not
report is a **failure**, not a note — it used to print a line and exit 0, so
"could not verify" read as "verified".

- PASS (exit 0) → the driver's names are right; proceed.
- FAIL (exit 1) → the overlay and driver disagree; fix `_CORE_NAMES`/
  `_DMA_NAMES` or the block design **before** any driver call. Do not "just
  try" gate 3. If the failing line is `GATE=live_clock` or `GATE=live_fclk1`,
  do **not** continue to any qualification at all: every wall time and every
  modelled cycle count downstream is scaled by that frequency, and no Vivado
  report in the build can reveal it.
- exit 2 → the overlay could not be loaded at all (missing `.bit`, missing
  `.hwh` sidecar, no PYNQ). An environment problem, not a mismatch — do not
  go editing the driver's name lists.

## Gate 3 — the real 63,078,400-byte DMA transfer

```
sudo -E python3 board_gate_full_dma.py --overlay three_stage_combined.bit
```

Moves a full 9856 × 6400 procedural page through `binarize_core_0` — one
63,078,400-byte MM2S and one 63,078,400-byte S2MM, each a **single** transfer
(the binarize DMA's 26-bit length register, max 67,108,863 B, is what makes
that legal; this is its first full-size exercise) — and compares the output
**bit-exactly** against the truncating-Gaussian CPU oracle.

A bit-exact page is **not** by itself proof that the full envelope moved: a
short S2MM leaves the tail of the destination holding whatever was there
before, and the compare can walk straight over bytes the PL never wrote. So
the gate also asserts, and prints:

- S2MM `transferred` == 63,078,400 B — a real measurement, since
  `S2MM_LENGTH` is written by the engine with the bytes actually received;
- MM2S `transferred` == 63,078,400 B — **corroboration, not measurement**:
  `MM2S_LENGTH` is principally the length the driver programmed, so it
  confirms the request rather than the movement. The outbound direction is
  supported instead by the channel going idle with no error, `ap_done` from
  the core, and `binarize_core` consuming exactly `img_w * img_h` beats by
  construction — a short feed leaves it blocked in a stream read, so the
  gate times out rather than passing;
- no `0xAA` pre-fill sentinel survives anywhere in the page — that value
  cannot be a legitimate output, since the core emits only 0 or 255;
- a 64-byte guard tail past the page is untouched, catching the opposite
  error of an S2MM that wrote too far.

Quote them in that order: the S2MM count, the sentinel scan and the guard
are the direct evidence; the MM2S count corroborates.

The page also carries a low-bit parity term, and that is load-bearing rather
than decorative. Without it every 3×3 Gaussian weighted sum over this page
is divisible by 16, the `>> 4` is exact, and a core that **rounded** would
emit a byte-identical page — the gate would have "verified" truncation it
could not distinguish from rounding. `--selftest` asserts a rounding oracle
really does differ on this page (it differs at 4,233 of 534,006 sampled
pixels).

This is deliberately a different gate from gate 1: allocation success says
nothing about a transfer completing, and a transfer completing says nothing
about the data being right. A PASS here also validates
`PLPipeline.binarize_page()` — the first of the three per-stage driver
validations.

Both the page generation and the verification run in row strips, and that is
a hard requirement rather than tuning: whole-page numpy needs about 1 GiB
against roughly 290 MiB of userspace, so it would be OOM-killed (exit 137,
no verdict) before touching the PL. Measured peak for the strip version is
98.7 MiB. `python3 board_gate_full_dma.py --selftest` checks the strip
decomposition against the whole-page oracle offline, with no board — run it
after editing that file.

Expect the CPU-side verification to take a while on the Zynq PS (numpy over
63 M pixels); the PL round trip itself should be seconds.

Exit 2 here means the overlay or a module could not be loaded — a file-copy
problem, not a DMA fault.

### What to do after a gate 3 failure

Both gates now handle their own recovery in-process, so the manual
`Overlay(...)` reprogram is no longer the first response — read what the gate
printed and match it to one of these four. Restarting the Jupyter kernel is
never one of them: the kernel does not touch the PL, and each gate is its own
`sudo` process whose in-process guard died with it.

| What you saw | State of the fabric | Do this |
|---|---|---|
| **FAIL or exit 2, teardown clean** (no `UNSAFE TEARDOWN` block) | Every armed DMA was proved halted before any page went back | **Nothing.** No reset is required; fix the reported problem and re-run. |
| **`UNSAFE TEARDOWN` then `PL reset:`** | A DMA could not be proved halted, so the gate reloaded the overlay itself — engines and cores are back at power-on and the pages were safe to release | Gate failed; the board is usable. Re-run gate 1 before continuing. |
| **`PL RESET FAILED` then `FAIL-STOP`** (the process is still running) | Unknown, and the gate is holding its CMA pages open because it cannot prove they are safe | **POWER-CYCLE.** Not `reboot`, not `kill -9` — both free the pages with the fabric live. |
| **The process died anyway** — SIGKILL, OOM-killer, a crash, a power blip | Unknown, and its pages went back to the pool while whatever was running kept running | **POWER-CYCLE before any further CMA use.** This is the one case no software can clean up. |

The third and fourth rows are the reason the gates ignore SIGINT, SIGTERM,
SIGHUP and SIGQUIT from the moment the pipeline exists: those are the signals
that would otherwise turn row two into row four.

## Gate 4 — the extractor and the matcher, on a pinned golden

```
sudo -E python3 board_gate_extract.py --overlay three_stage_combined.bit
```

**Quote this one precisely.** Both cores already have silicon results in
their own standalone images — `patch_extract_core` from its bring-up, and
`template_match_core` at 9/9 on 2026-08-07. What has never run is either of
them **in `three_stage_combined`, driven by `PLPipeline`**: three cores
sharing HP0/HP1/HP2, five DMAs, one Python driver sequencing all of it. So a
PASS here is the **first extractor run through `PLPipeline` in the combined
overlay**, not the first extractor run on silicon.

**And it is a smoke gate, not a qualification.** One extractor candidate plus
the nine matcher cases. Three things a real page needs are NOT covered here:
multi-candidate rearming, TLAST across a batch rather than on a single
transfer, and the per-kind argmax. Phase D gives each kind exactly ONE trial,
so `by_kind[kind]` is a reduction over a single element — it cannot pick
anything. Its two kinds also both score exactly 1.0, so what phase D really
tests there is the GLOBAL best and its tie rule, which is a different
reduction. Those are the next protocol tests, and they
come before the strict 36-page PL-backend comparison, not after it. A gate 4
PASS says the combined overlay works once end to end; it does not say the PL
backend is ready to be compared against the CPU baseline.

**A pinned 24×20 golden, deliberately not a real PDF.** A corpus page yields
a detection count, and a count can be right for the wrong reasons: a patch
clipped one pixel short, or a location reported in patch instead of page
coordinates, would very likely leave the totals intact. What is needed first
is a case whose every intermediate byte is known in advance, and one already
exists — the three-stage C/golden in `hls/integration/`, which passed Vitis
HLS CSim on 2026-08-09 and is composed from the binarizer, extractor and
matcher oracles that were each already proved separately. This gate feeds
the same vectors to real hardware and demands the same bytes:

```
480 gray bytes -> 480 binary bytes -> one 168-byte 14x12 patch at (3,4)
-> matcher +1.000000 at local (4,1), page (7,5)
```

Five phases, each PASS gating the next:

| phase | asserts |
|---|---|
| A binarize | all **480** binary bytes byte-exact; 480 B each way; guard intact; no sentinel survives |
| B extract | record `valid=1`, origin **(3,4)**, patch **14×12**; `sts_flags=0`, `sts_rejected=0`, `sts_processed=1`; the patch S2MM moved **exactly 168 B** (TLAST framing); all **168** pixels byte-exact |
| C matcher | the 9-case `hw` manifest through `tme_top_0`, score **and** exact location on every case, including the **251,740 B** maximum-envelope case (§3.1's only exercise), plus a re-invocation afterwards to catch stale `static` BRAM |
| D reduce | `match_candidate()`: absolute page boxes, the strict-`>` tie going to the **first** trial, and the per-kind split keeping both kinds apart — each with a control that fails if the rule were reversed. One trial per kind, so the per-kind *argmax* is not exercised; see §"smoke gate" above |
| E chain | phases A→B→D again on the patch the **PL** produced, required to give bit-identical results to the golden-fed run |

**How to state the envelope case.** Both matcher channels are MM2S, so
nothing on that path counts *received* bytes — `MM2S_LENGTH` is essentially
the length the driver programmed. The supportable claim is "**251,740 B
programmed; the core completed and the DMA became idle without error**", and
that is what the gate prints. It is not nothing: `tme_top` reads exactly
`patch_w * patch_h` beats by construction, so a short feed leaves it blocked
in a stream read and the gate times out instead of passing, and the score and
exact location come back correct, which a truncated patch would not produce.
But it is not a measured byte count, and must not be written up as one.

Two templates are used in phases D and E, both exact crops of the patch, so
both score exactly 1.0 at different locations. That is what makes the tie
testable: the winner is decided purely by trial order, with no float
coincidence involved, and reversing the bank must move it.

This gate is CMA-light — the page is 480 bytes — so a failure here is about
the cores, not the pool.

Before booking board time, run the two off-board checks. Neither needs PYNQ:

```
python3 board_gate_extract.py --selftest    # constants vs the golden vectors
python3 test_board_gate_extract.py          # the gate itself, on fake silicon
python3 tme_driver.py --selftest-predictor  # the reject predictor vs the core's golden
```

`--selftest` catches vectors that were regenerated from a changed oracle (the
gate refuses to run rather than move its own goalposts).
`test_board_gate_extract.py` runs all five phases against simulated hardware
and then breaks each assertion in turn, requiring the gate to fail — so a
board run is not the first time any of this code executes. The predictor
self-test is what makes `extract_candidates()`'s pre-dispatch rejection safe
to rely on: a rejected candidate emits no pixels, so a descriptor the PL
would reject would strand the patch receive armed for it.

The vectors are committed, so there is nothing to regenerate. If you ever
do regenerate them (an oracle changed), `GATE4_VECTORS.sha256` must be
regenerated with them and the change explained — a silent hash update is how
a gate stops testing what it says it tests.

- FAIL (exit 1) → the hardware or the driver is wrong. The phase that failed
  is named; the phases after it did not run and did not pass.
- exit 2 → a missing or mismatched fixture, a missing module, or an overlay
  that would not load. An environment or payload problem, not a gate failure
  — and never evidence about the hardware.

### If gate 4 tears down unsafely

`board_gate_extract.py` **reprograms the PL itself** when `close()` cannot
prove a DMA halted, and it does so before returning. That is not tidiness:
the retained buffers are strong references inside the gate's own `sudo`
process, so the moment it exits they are collected and the CMA pages go back
to the pool — possibly while an S2MM still has a command against them. By
the time you read the exit code, the pages you were going to protect are
already released. The reset happens while they are still held.

The gate still fails (an unsafe teardown is a failure), but the board is
recoverable and the next gate can run after re-checking gate 1. The same
recovery runs if `close()` *raises* rather than returning False — a teardown
that died halfway has proved nothing about the DMAs either.

**If the reset itself fails, the gate does not exit — it blocks, on purpose.**
That is the double failure: the fabric may still have a command against those
pages and nothing in the process can retire it, so handing them back is not an
option, and *exiting is handing them back* (the buffers are strong references
in this process and go with it). There is no exit code that means "do not reap
me", so the gate prints a `FAIL-STOP` banner, holds the pipeline and all seven
buffers, and waits. SIGINT, SIGTERM, SIGHUP and SIGQUIT are all ignored from
the start of teardown, so Ctrl-C, a closed notebook and a shutdown's SIGTERM
cannot end it; it heartbeats every five minutes so you can see it is holding
rather than hung.

    Right response:  POWER-CYCLE the board.
    Wrong response:  `reboot`. Shutdown terminates userspace and only then
                     resets the hardware, so it kills the holder while the
                     fabric is still live — the same window under a friendlier
                     name. (SIGTERM is ignored, so it also escalates to
                     SIGKILL, which nothing can catch.)
    Wrong response:  kill -9. The same release, by hand.

So gate 4 has one more outcome than the exit codes describe: no exit at all.
A gate 4 cell that never returns and shows `FAIL-STOP` has not hung; read the
banner and cut the power.

**Gate 3 does exactly the same thing**, and it matters more there: gate 3 holds
the two biggest CMA buffers of the whole session (~120 MiB). Its teardown used
to be a bare `close()` in a `finally` with `freed` initialised to True, so a
close() that raised was reported as a clean free and the process exited with
the pages going back. Both gates now share `safe_teardown.py`.

## Gate 5 — the multi-candidate stream protocol

```
sudo -E python3 board_gate_protocol.py --overlay three_stage_combined.bit
```

Run it after gate 4 passes. It is a separate gate, not more phases in gate 4,
because gate 4's result is pinned evidence for the 0.2 build and must not
move: gate 4 keeps its eight fixtures and its meaning, and gate 5 brings its
own five (`tb_proto_*`, ~16 KB, committed and hashed in
`GATE5_VECTORS.sha256`; copy them, do not regenerate them on the board).

**What only a batch can reach.** Contract §5 gives the extractor two output
streams with two different TLAST disciplines — the metadata stream carries
TLAST at BATCH end and is armed once for `n × 16` bytes, while the patch
stream carries TLAST per patch and is armed `n` times at `n` different
lengths. At `n = 1` those are the same thing, so every skew between them is
invisible in gate 4 by construction. Gate 5 runs five batches — 4, 4, 1, 2
and 2 descriptors, one permuted and one a repeat of a single descriptor —
over four patch sizes that go **down, up and down**: 950, 650, 722, 352 B.

The increase at candidate 2 is the load-bearing part. A receive that re-armed
at the previous candidate's length would merely be over-armed in a batch that
only shrinks, and would pass.

Five failures that cannot happen at `n = 1`, one check each:

1. records arriving in an order other than the descriptor order;
2. the batch TLAST landing early — the metadata S2MM completes short and the
   records are parsed out of stale buffer content. `sts_flags` bit 1 does not
   cover this (it compares the INPUT descriptor count with `num_cands`, the
   other end of the same batch), and the driver's framing cross-check covered
   the patch stream only. `tme_driver.extract_candidates` now reads the
   metadata engine's own received-byte count, publishes it as
   `last_extract_stats["meta_bytes_measured"]` and refuses the batch on a
   mismatch. **This one is a measurement** — the metadata channel is an S2MM,
   so the engine writes the count — unlike the matcher's 251,740 B envelope
   figure, which is MM2S and is only a programmed length;
3. the patch receive not re-arming between candidates;
4. it re-arming at the previous candidate's length;
5. `static` extractor state surviving into the next batch.

**And the one gate 4 named but could not test.** Gate 4's phase D says: "each
kind gets one trial, so `by_kind[kind]` reduces over a single element and its
argmax cannot be wrong here." Gate 5's bank gives kind `alpha` two trials —
a partial match first, an exact match second — so `by_kind["alpha"]` must pick
the SECOND. Two controls follow: reversing the bank moves the global tie to
`beta`, and putting the winning `alpha` trial FIRST must still return it,
which is what separates a real argmax from "keep the last thing seen". A page
carries several templates per kind, so this is the ordinary case, not an edge
case.

**Not covered, deliberately:** a descriptor the extractor REJECTS mid-batch.
`extract_candidates` refuses to dispatch one — a rejected candidate emits no
pixels, so the receive armed for it would strand — and that refusal is correct
behaviour, not a gap. The record/patch cursor skew a mid-batch reject would
cause is covered in C simulation by `hls/integration/pe_tme_generate_golden.py`
(`mid-batch-reject`).

Two off-board checks should pass before booking board time, exactly as for
gate 4:

```
python3 board_gate_protocol.py --selftest      # constants vs vectors vs driver
python3 test_board_gate_protocol.py           # all 5 phases vs fake silicon,
                                              # then 13 injected defects
```

The self-test includes a pure-Python dry run of the PS reduction over the
golden scores, so a wrong expectation is caught there rather than discovered
on the board as a hardware failure. The fake-silicon test decodes the packed
descriptors the driver actually wrote rather than replaying a script, so a
mis-packed or mis-ordered batch fails it.

Exit status is gate 4's: 0 = passed, 1 = a phase failed, 2 = could not run.
Teardown is the shared `safe_teardown.py`, so the `UNSAFE TEARDOWN` /
`PL reset:` / `FAIL-STOP` outcomes documented for gates 3 and 4 apply here
unchanged.

## Gate 6 — the counted clock check

```
sudo -E python3 board_gate_clock.py --overlay three_stage_combined.bit \
                                    --variant combined_b2_100
```

Run it after gate 4 passes; it reuses gate 4's three `hw` fixtures and the same
`GATE4_VECTORS.sha256` record, and brings no vectors of its own.

**What it closes.** The preflight's `Clocks.fclk0_mhz` is a divisor/register
read-back, not an edge count — right for the 62.5 MHz trap, which corrupts
those divisors, but a PLL that failed to lock would still read a perfect 100.0.
And gates 1–5 are all correctness gates: every one of them would pass at
whatever clock the fabric happened to run at, because a slow matcher returns
the same score, just later. Time is the only independent evidence available,
and the matcher's cycle count at a fixed geometry is known exactly.

**Two probes, one envelope.** `820×307 / 216×96` and `820×307 / 4×4` — hw
cases 7 and 8, the same 251,740-byte patch MM2S, differing only in the
template. Five repetitions of each; the **minimum** is used, because every
contaminant on this path is additive and positive, so a mean would drift with
system load in the direction that flatters a slow clock.

**Read the differential, not the absolute.** The absolute figure carries the
fixed per-invocation overhead — two arms, four scalar writes, poll
granularity — which was +1.6 to +2.5 ms across the nine cases of the
2026-08-20 B2 session, so its band has to be one-sided and loose. The
difference of the two probes cancels almost all of it and leaves 249,549,328
modelled cycles of pure fabric time. That is the number to quote.

**Neither is a cycle counter.** The RTL has none. The cycle totals are
MODELLED, from the law RTL co-simulation pinned; what is measured is PS-side
wall time. Say "consistent with 100 MHz", never "measured 257,145,732
hardware cycles".

`--variant` decides the expected frequency **and** the cycle law — `TermCount`
takes the `cur` law, `TermCountB2` the `B2` one — derived from the variant
table's VLNV so that timing against the wrong law is not reachable by a flag.

Off-board, before booking board time:

```
python3 board_gate_clock.py --selftest --variant combined_b2_100
python3 test_board_gate_clock.py     # 12 tests, 7 injected clock defects,
                                     # and the cycle law checked against
                                     # tme_cycle_model.py for all 9 cases
```

## Gate 7 — timeout and reset recovery

```
sudo -E python3 board_gate_recovery.py --overlay three_stage_combined.bit \
                                       --natural 2.5715
```

**This gate deliberately breaks the board.** Run it last in the synthetic
sequence. It reprograms the PL four times and leaves the fabric in its
power-on state.

Gates 1–6 are happy-path gates — passing them "does not validate any
failure-recovery path". But the recovery path is the documented contract of
this driver (`_require_usable`: "There is no in-process recovery from that and
this driver does not pretend otherwise"), and a 36-page corpus run will
eventually hit a timeout. This makes that first time deliberate and observed.

| phase | asserts |
|---|---|
| R0 baseline | the nine `hw` cases, before anything is broken, so an R4/R5 failure is attributable to the recovery |
| R1 deadline | a 0.5 s deadline on a ~2.57 s transaction raises `TimeoutError`, bounded, and demonstrably before the transaction would have finished on its own |
| R2 latch | `match_template`, `binarize_page` and `extract_candidates` all refuse with a `RuntimeError` naming the outstanding transfer |
| R3 reset | `close()` returns **False** and RETAINS the CMA pages — a True here would be a gate failure, the one place in the suite where that is so — then `reset_pl` reprograms |
| R4 after | a fresh `PLPipeline` gives 9/9 again. Liveness is not correctness; this is what separates "the board survived" from "the board recovered" |
| R5 wedge | `tme_top` started straight through AXI-Lite at 820×307 with **no MM2S armed**, so it blocks needing 251,740 beats that never come. The wedge is observed directly (`ap_done` never rises, `ap_idle` never returns across 300 ms), `_start`'s `ap_ctrl_hs` idle guard must refuse the next invocation, and the retain/reset/re-verify recovery must work from a wedge too |

R5's stall geometry is the maximum envelope on purpose: a later small probe
arms 3,072 beats at a core waiting for 251,740, so the core stays wedged and
the observation is deterministic rather than a race against a short feed
draining the stall. Starting the core behind the driver's back is the only
place in this suite that does it, and it is the only way to produce a stall
the driver cannot itself have caused. No DMA is armed at the moment of the
wedge, so no CMA page is targeted by it.

**Not exercised on the board, deliberately:** a DMA error interrupt, an AXI
decode error, a core that hangs mid-write to an S2MM, and `reset_pl`
returning False. That last one is the fail-stop path, which exists for a
board that cannot be reprogrammed — provoking it on silicon would leave a
board that cannot be reprogrammed. It **is** coded, and it **is** provoked
off-board by three tests and five mutants.

### If a reprogram fails, this gate will NOT exit

R3 and R5 both reach their `reset_pl` call holding CMA pages that `close()`
refused to free. If that reprogram fails there is no in-process recovery
left, and returning a verdict would end `main()`, drop those references and
hand the pages back while the fabric is in an unknown state. So the gate
hands over to `safe_teardown.fail_stop_holding` instead, which never
returns:

* the process keeps running and keeps holding the pages;
* `SIGINT`/`SIGTERM`/`SIGHUP`/`SIGQUIT` are ignored from that point;
* **POWER-CYCLE the board.** Not `reboot` — shutdown kills the holder first
  and resets the hardware after, which frees the pages while the fabric is
  still live. Not `kill -9` — same release, by hand.

A hung-looking gate 7 that has printed the `FAIL-STOP` banner is doing its
job. There is no exit code that means "do not reap me", so not exiting is
the only protection left. The same hold guards a failed *closing* reprogram
in `main()`, but only when buffers are actually still retained.

The two recoveries in R3 and R5 print `safe_teardown`'s `UNSAFE TEARDOWN`
banner, and that is correct there: an unrecovered DMA is exactly what has just
happened. The closing tidy-up reprogram does **not** go through `reset_pl`
for that reason — see `final_reprogram()`.

Off-board, before booking board time:

```
python3 test_board_gate_recovery.py   # 18 tests, 12 injected defects, all
                                      # five phases against fake silicon,
                                      # including the fail-stop hold
```

**Gate 7 must be RE-RUN on silicon before its result is quoted.** The
2026-08-22 audit found two fail-open paths that the passing run did not
exercise, and both are now closed:

* **R5 ran outside `run_all`'s abort handler.** R5 makes three `require`s
  before it reaches its own `close()`/reset, and each raises on an unexpected
  result. Any of them left the wedged core un-reset and the pipeline
  unclosed — measured as `resets=1, holds=0, outstanding=True, closed=False`,
  with the retained CMA pages reachable from nothing but a dying frame. R4
  had the same shape of hole for a page that times out mid-transfer. Both
  now go through `abort_recovery()`, which closes, reprograms, and hands over
  to the fail-stop hold if the reprogram fails.
* **`fail_stop()` announced itself with `print()` before engaging the hold.**
  On the teardown path stdout is a notebook's and can already be gone; a
  `print` that raised there propagated out *before* `hold_fn` was ever
  called. Measured: `BrokenPipeError`, `hold_fn` called zero times. That
  site, `final_reprogram()`'s failure message, and `main()`'s hold site all
  use `safe_teardown.say()` now.

The suite requires the invariant directly: over the clean run and all twelve
injected defects, every pipeline must end **closed and freed**, **reprogrammed
after the last time it refused to free**, or **inside a non-empty fail-stop
hold that carries the pipeline and its retained buffers**. Each fix is
mutation-controlled — reverted on its own, it fails a named test.

## After the gates — integration

`detect_page()` now takes a **backend**, and the four are explicit
(`sw/pl_backends.py`). A `pl-*` backend that cannot reach the fabric FAILS the
run; there is no silent hand-back to the CPU, and `describe()` prints which
stage ran where so a transcript can never be ambiguous about what produced a
number.

```
python3 terminal_counter_endpoint_first.py <pdf> \
    male_ter/male_left.png male_ter/male_right.png \
    female_ter/female_left.png female_ter/female_right.png \
    ferrule_ter/ferrule_left.png ferrule_ter/ferrule_right.png \
    --backend pl-all --overlay three_stage_combined.bit
```

| backend | binarize | patch | match |
|---|---|---|---|
| `cpu` | cpu | cpu, per template base | cpu |
| `pl-binarize` | **PL** | cpu, per template base | cpu |
| `pl-extract` | **PL** | **PL**, one per candidate | cpu |
| `pl-all` | **PL** | **PL**, one per candidate | **PL** |
| `cpu-sidebank` | cpu | cpu, one per candidate | cpu |
| `cpu-production` | cpu, **core-equivalent** | cpu, one per candidate | cpu |

`cpu-sidebank` and `cpu-production` are **diagnostics**, not part of the four.

`cpu-sidebank` is the PL's organisation run entirely on the host, with the
binariser held at the frozen oracle's, so rung B can be measured with no
board.

`cpu-production` is **the software oracle a B2/100 board run has to
reproduce**, and it exists because `cpu` cannot be that oracle. `cpu`
binarises with `to_binary_inv` — OpenCV's *rounding* blur, a reflected border,
Otsu over that — and all of it is rung A, which is *expected* to differ.
Requiring the board to match `cpu` therefore means waiving detection-level
parity on most of the corpus: a direct comparison found only **7 of 36** pages
identical under the ladder criterion, and "35/36" was only aggregate per-class
count equality, which two pages can satisfy with different boxes.
`cpu-production` closes rungs A and B on the host — the core's own truncating
arithmetic and threshold choice, the PL's side-bank patch organisation, the
same trial order and tie rule, host refinement — so the only thing left
between it and `pl-all` is which chip ran them.

Off the board, against the exact fake fabric, `cpu-production` and `pl-all`
agree on the Stage 2 page **exactly**: same threshold (162), same 44
classification calls, same 1,200 matcher invocations, same 6 refinement
calls, and 1/1 pages identical.

### Do NOT compare `pl-all` straight to the frozen oracle

It differs from it in **three** independent ways, and one answer cannot tell
them apart — which is how a real matcher fault gets filed under "expected
geometry difference" and never looked at again. Walk the ladder instead:

```
python3 tme_backend_parity.py "../../sample/*" \
    --backends cpu pl-binarize pl-extract pl-all --assert-rung-c
```

| rung | change | expectation |
|---|---|---|
| **A** `cpu` → `pl-binarize` | the binariser | **expected to differ** |
| **B** `pl-binarize` → `pl-extract` | the patch organisation | **expected to differ** |
| **C** `pl-extract` → `pl-all` | the matcher silicon | **MUST be identical** |
| **P** `cpu-production` → `pl-all` | the fabric, production semantics held | **MUST be identical — this is the 36/36 requirement** |

**Stage 3 wall time.** `doc_003` p1 is 68 candidates → 2 batches
of [64, 4], 2,040 matcher trials, largest patch 622×300. Recomputed from the
page's own geometry, initial matching models **148,323,642,023 cycles =
1,483.236 s (24m43s)** at 100 MHz. The production organization expects 26
host-refinement calls / 208 ARM correlations, whose Cortex-A9 wall time is not
yet measured. The older 161,607,184,773-cycle / 1,616.0718 s figure instead
adds **176 frozen-trace refinement records**, repriced at
`pl_side_bank`/B2, to the common initial total. It is a conditional diagnostic,
not a production or fabric-time estimate. Never divide its 13,283,542,750-
cycle delta by the production path's 208 correlations. Compare measured
**DMA/core** time against 1,483.236 s and record ARM refinement separately.

* **A** — `binarize_core` uses an integer 3×3 Gaussian with a **truncating**
  `sum >> 4`; `cv2.GaussianBlur` rounds, and gate 3 asserts on a real page
  that the two oracles genuinely disagree. The core also zeroes the 1-pixel
  border OpenCV fills by reflection, and takes a fixed threshold register
  where `to_binary_inv` runs Otsu. `PlBinarizer` picks the threshold on the
  core's *own* blur, so the fabric is the only thing left in this rung.
* **B** — `cpu_per_base` vs `pl_side_bank`, the two policies
  `tme_full_search_baseline.py` refuses to add together. The PL patch is a
  strict superset of every CPU patch, so the search domain and the
  TM_CCOEFF_NORMED denominators both change. Winners can move.
* **C** — same patch, same template, same arithmetic. Exact (x, y) and
  |score − gold| ≤ 0.005, the board PASS criterion. A difference here is the
  matcher, not the geometry. `--assert-rung-c` exits non-zero on one.
* **P** — nothing differs but the chip. This is the rung the corpus run has
  to pass 36/36; there is no expected arithmetic difference left to file a
  fault under.

### On the board: no BGR, no PNG, geometry as JSON

`render_page()` used to hold 620,421,120 B at once on a 9792×6336 page — the
Pixmap, a `pix.samples` **copy**, BGR, and grey. `keep_bgr=False` is passed
DOWN into it (both runners do) so BGR is never built, and `samples_mv` removes
the copy; the peak inside the function drops to about 248 MB and the grey page
is what survives. Do **not** quote that as a process bound. The board memory
gate must exercise the entire maximum-page pipeline: render, binarization with
`page_bin` and `clean_bin` alive, PL extraction/matching, geometry output and
safe teardown, followed by a known-small page. Record RSS, `MemAvailable` and
CMA availability across those phases; a render-only measurement cannot pass
the gate.

Two cheaper renderers were measured and rejected, and both are asserted as
still-failing in `test_pl_backends.py` so they cannot quietly come back:

* **native grayscale** (`colorspace=fitz.csGRAY`): **0/36** corpus pages
  byte-identical, up to 23 grey levels out. MuPDF rasterises into grey rather
  than converting afterwards.
* **striping the render** with `clip=`: tiles exactly in geometry, but MuPDF
  antialiases against the clip edge and the last ~15 rows of each band differ.
  An overlap margin shrinks it without bounding it.

Only the **conversion** is striped, which is exact by construction (both
`cvtColor` calls are per-pixel).

For a board run, emit geometry and draw somewhere else:

```
python3 terminal_counter_endpoint_first.py <pdf> <6 templates> \
    --backend pl-all --variant combined_b2_100 \
    --geometry-json page_geometry.json --no-annotate
```

then off-board, from the same source PDF:

```
python3 terminal_counter_endpoint_first.py <pdf> <6 templates> \
    -o annotated.pdf --from-geometry page_geometry.json
```

The redraw is byte-identical to annotating directly, and a record whose page
shape does not match the PDF at that zoom is refused rather than drawn in the
wrong place. `--debug-images` is already off for `pl-*`.

### The memory gate: `--mem-sampler`, one page per process

`sw/mem_sampler.py` is the instrument for the gate described above. Add
`--mem-sampler PATH` to any detector run and it writes one JSON record per
phase — `pipeline_ready`, `render_complete`, `preprocess_complete`,
`segments_complete`, `extraction_complete`, `initial_match_complete`,
`page_complete`, `geometry_flushed`, `teardown_complete` — each carrying
`VmRSS` / `VmHWM` / `VmSwap`, `MemAvailable` / `SwapFree` / `CmaFree`, the
live array sizes **and their aliasing**, candidate counts, the extractor's
retained patch bytes and the geometry byte count.

`initial_match_complete` is named for where it sits: between the
classification loop and `refine_misaligned_terminal_boxes`. On a `pl-*`
backend that is the boundary between the pass that runs on the fabric and
the refinement and dedupe that run on the ARM, so the phase it closes is the
initial match and not matching as a whole.

On a `pl-*` backend the array set also carries **`cma_gray` and
`cma_binary`**, the driver's two full-page CMA buffers. `cma_binary` is the
allocation `page_bin` and `clean_bin` are views of, and they appear in one
alias group; `cma_gray` is referenced by nothing on the host side, and
without it the byte totals under-report a production page by 62 MB that
`VmRSS` and `CmaFree` can both see.

Every record is written, flushed and `fsync`ed before the next phase starts.
That is the point: the run this gate exists to catch is the one the OOM
killer ends — SIGKILL, exit 137, no traceback — and the last record on disk
is what names the phase it died in. Summarise with:

```
python3 mem_sampler.py run.jsonl [--require-pass]
```

**Verdicts**, in precedence order. `NOT-A-GATE` is anything not read from
`/proc` — so an off-board run can never close this — and also any run whose
records claim `/proc` without carrying the fields the rules read, because a
rule that cannot fire is not a rule that passed. `FAIL` is a run that raised
or whose PL teardown returned non-zero: the numbers can look perfectly
healthy and still describe a run that did not do the work. **`HOLD` on any
swap**: the board has 511 MiB of swap and a pipeline that "fits" by swapping
has not fitted — it has moved the failure into wall time that would then be
filed against the fabric. `MALFORMED` is a checkpoint sequence that is not
one a page can have passed through — reordered or repeated — as distinct
from `INCOMPLETE`, which is a PREFIX of the real sequence and is the OOM
signature. `PASS` is all of: the whole sequence in order, one or more page
blocks, every required field present, no swap, and a clean teardown.

**One page per process.** `VmHWM` never falls, so a second page in the same
process inherits the first page's peak; `summarise()` refuses to attribute a
peak when it sees two pages in one file. Run the small page as a **separate
invocation**, not a second iteration.

Within a page the same monotonicity hides phases — the render peak masks
everything after it — so add `--mem-sampler-per-phase-peak` to reset `VmHWM`
at each checkpoint. It needs `/proc/self/clear_refs`
(`CONFIG_PROC_PAGE_MONITOR`); where that is absent the records say `"run"`
and cannot be misread as per-phase.

**Which pages.** Three runs, and the maximum-dimension page is *not* the
most path-dense one:

| run | page | why |
|---|---|---|
| 1 | `doc_003` p1 (9792×6336, 68 candidates) | largest dimensions, most candidates, the Stage 3 page |
| 2 | `doc_006` p1 (9792×6336, 3,258 drawings, 80 candidates) | densest **full-size** drawing; also the page whose classes move under rung B, and one of the four that needs two extractor batches |
| 3 | `doc_035` p1 (4896×3168, 3,654 drawings) | the small-page re-invocation — and, as it happens, the densest page in the corpus |

**Off-board prediction to check against** (`logs/b2prod_20260823/06_sampler_offboard_*`,
dev x86, PyMuPDF 1.28.0/MuPDF 1.29.0, cv2 5.0.0, `NOT-A-GATE` by
construction):

* full-size page peak **+253,340 kB = 247.4 MiB above the `pipeline_ready`
  baseline** (densest page 253,616 kB = 247.7 MiB), and
  all of it is the renderer: `VmHWM` reaches its final value at
  `render_complete` and no later phase adds to it;
* steady state across binarise→match is **177.5 MiB** = grey + `page_bin` +
  `clean_bin` at 62,042,112 B each, *not aliased* on the CPU path;
* path density did **not** move the peak — the 3,258-drawing page peaked
  276 kB above the sparse one, so `get_drawings()` is not the excursion it
  was flagged as at this corpus's density. What the run bounds is only "did
  not exceed the render peak"; `--mem-sampler-per-phase-peak` is what turns
  that into a measurement;
* the small page peaked at +69,936 kB = 68.3 MiB — 3.62× lower for 4× less
  page area.

The board's own baseline will differ (32-bit, Python 3.10, NumPy 1.21), so
the number to carry over is the **+247.4 MiB delta**, not the 321 MiB total.
Against ~290 MiB of userspace that is the whole margin, and it says the
renderer — not the matcher — is what decides whether Stage 2 fits.

**This gate cannot run yet.** `detect_page()` needs `fitz` for three inputs
(render, words, vector segments) and the board has no PyMuPDF; see
`logs/b2prod_20260823/05_board_environment.md`. The sampler is
version-agnostic and records `VersionBind` / `VersionFitz` / `samples_mv` in
its header, so it works unchanged either side of the 1.19.2 rebase.

### Rung C in ONE run, and the counts that go with it

Separate `pl-extract` and `pl-all` runs each re-render, re-binarise and
re-dispatch the batch: **nothing proves the two matchers were handed the same
pixels**, so a "rung C passed" built from two runs is weaker than it reads.
`--rung-c-inline` runs the CPU reduction against the *same* `patch` array the
fabric just matched, from the same metadata record, inside the same
candidate:

```
python3 tme_backend_parity.py "../../sample/*" \
    --backends cpu-production pl-all --rung-c-inline \
    --require-corpus --assert-rung-c --variant combined_b2_100
```

* `--assert-rung-c` now fails when the rung **did not run at all**. It used to
  exit 0 for `--backends cpu cpu-sidebank`, having compared no silicon.
* `--require-corpus` refuses to report unless the run covered exactly **35
  unique PDFs and 36 pages**. Globs are de-duplicated by resolved path first:
  on Windows `glob` matches case-insensitively, so `"*.PDF" "*.pdf"` expanded
  to every file twice and reported 72 pages.
* `trials=` in `describe()` is the count of **matcher invocations**. It used
  to count returned class winners — at most one per class — and reported 132
  for a Stage 2 page that ran **1,200** invocations, 9.1x low. Every
  wall-time-per-trial and cycle-model comparison divides by this number.
* Count **this run's** calls. Do not reuse the 808-call CPU trace or the
  968-call PL-geometry trace.

`--fake-pl` drives the `pl-*` backends from `test_pl_backends.FakePL`, an
arithmetically exact stand-in, so the whole ladder runs with no board. It
prints a banner on every run and stamps `"fabric": "FAKE …"` into the JSON:
**it proves the wiring, never the silicon.**

### Refinement runs on the host, and that is not a fallback

`refine_misaligned_terminal_boxes` matches with `prefer_local_alignment=True`,
the argmax of the anchor-adjusted correlation **map**. `tme_top` reports a
scalar argmax of the **raw** map and no map at all, so that argmax is not
recoverable from what the hardware returns. Refinement therefore runs on the
host under every backend; it is declared and counted in `describe()`.
`--require-pl-refine` exists only so a future RTL can be made to fail loudly
here rather than be missed.

### Then the corpus

```
python3 cpu_baseline_snapshot.py compare "../../sample/*" \
    --manifest cpu_baseline_20260811.csv
```

All 36 manifest pages, exactly once each, byte-identical digests — **against
the `cpu` backend**, which is the organisation that oracle was captured on.
Hold the ladder separately; do not mix the two comparisons. Benchmark PS
classification after that, and only then reconsider `class_score_core` — it
is out of the MVP by decision (contract §10 items 4–5) and its source is not
integration-ready.

Off-board, before booking board time:

```
python3 pl_backends.py --selftest      # 6 checks
python3 test_pl_backends.py            # 24 tests, incl. rung C by construction
```
