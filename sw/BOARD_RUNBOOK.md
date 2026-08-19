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

## Before you start

**Boot the board fresh, and close any other notebook holding an overlay or
CMA buffers.** Even at `cma=192M`, a stale kernel still holding a 60 MiB
buffer is enough to fail gate 1 for a reason that has nothing to do with the
design.

## Acceptance criteria

Exit 0 is necessary but not sufficient. Proceed to the next gate only if:

| Gate | Proceed only if |
|---|---|
| 1 — CMA | exit 0; `CmaTotal` **≥ 192 MiB**; the output says **driver order** (not the weaker two-buffer preflight); real `CmaTotal`/`CmaFree` figures were read, not "unavailable" |
| 2 — overlay | exit 0; all **3 cores** and **5 DMAs** present; binarize DMA transfer bound **≥ 63,078,400 B**; measured PL clock **≤ 50 MHz** (the image is constrained at 20 ns) |
| 3 — full DMA | exit 0; **63,078,400 B** each direction; guard **64 B intact**; **zero** sentinel bytes remaining; **zero** oracle mismatches; teardown freed the buffers (no `UNSAFE TEARDOWN` block) |
| 4 — extractor + matcher | exit 0; all **8 fixture hashes OK**; **480/480** binary bytes; record `valid=1` at **(3,4)**, **14×12**; patch S2MM **received 168 B**; `sts_flags=0`, `rejected=0`, `processed=1`; **168/168** patch bytes; **9/9** matcher cases, the **251,740 B** case programmed and completed; teardown freed the buffers |

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
probe_cma_budget.py
inspect_overlay.py
board_gate_full_dma.py
board_gate_extract.py
board_gate_protocol.py        # gate 5; imports board_gate_extract for its Report
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
sudo python3 probe_cma_budget.py --overlay three_stage_combined.bit
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
sudo python3 inspect_overlay.py --overlay three_stage_combined.bit
```

Prints `ip_dict`, `hierarchy_dict`, the measured PL clock, every core's
`register_map` and every DMA's channel configuration, and checks them against
what `tme_driver.py` resolves by name. Capture the full output into the
repo (`docs/` or the bundle directory) — it is the board-side record of the
HWH inspection.

- PASS (exit 0) → the driver's names are right; proceed.
- FAIL (exit 1) → the overlay and driver disagree; fix `_CORE_NAMES`/
  `_DMA_NAMES` or the block design **before** any driver call. Do not "just
  try" gate 3.
- exit 2 → the overlay could not be loaded at all (missing `.bit`, missing
  `.hwh` sidecar, no PYNQ). An environment problem, not a mismatch — do not
  go editing the driver's name lists.

## Gate 3 — the real 63,078,400-byte DMA transfer

```
sudo python3 board_gate_full_dma.py --overlay three_stage_combined.bit
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
sudo python3 board_gate_extract.py --overlay three_stage_combined.bit
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
sudo python3 board_gate_protocol.py --overlay three_stage_combined.bit
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

## After the gates — integration

Then connect `detect_page()` one stage at a time behind **explicit** backends
(`cpu` / `pl-binarize` / `pl-extract` / `pl-all`), with no silent fallback — a
PL failure must fail the run, not quietly hand the work to the CPU. Only then
the strict 36-page comparison against the retained baseline:

```
python3 cpu_baseline_snapshot.py compare "../../sample/*" \
    --manifest cpu_baseline_20260811.csv
```

All 36 manifest pages, exactly once each, byte-identical digests. Benchmark
PS classification after that, and only then reconsider `class_score_core` —
it is out of the MVP by decision (contract §10 items 4–5) and its source is
not integration-ready.
