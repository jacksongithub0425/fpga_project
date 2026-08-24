# Priority B2-PROD — Board preflight

Date 2026-08-21. Board session, authorised for this task.
Transcripts in `logs/b2prod_20260821/preflight/`.

## Result

    WARM_BOOT_TECHNICAL_PREFLIGHT = PASS
    FORMAL_FRESH_BOOT_PREFLIGHT   = PASS
    PREFLIGHT                     = PASS

**Completion condition 4 is CLOSED.** Remaining B2/100 blockers are **5–7**.

The authoritative run is the **fresh-boot** one:
`preflight/16_preflight_freshboot.txt`, at **523.27 s uptime** against the
3,600 s bound, exit 0.

    UPTIME_S=523.27;MAX_UPTIME_S=3600;GATE=fresh_boot;RESULT=PASS
    PREFLIGHT=PASS;VARIANT=combined_b2_100;BOARD_FCLK0_MHZ=100.0

### How it got here — the earlier runs were warm, and that mattered

Runs 1–3 were performed at ~8 h uptime. The plan requires a fresh boot both
for the preflight and in the wording of condition 4, so those runs establish
the technical checks and **not** the formal verdict. An audit caught that the
script recorded uptime without gating on it and printed `PREFLIGHT=PASS`
regardless, which made a non-compliant run indistinguishable from a compliant
one in the only line anybody greps.

Fixed before the re-run: `board_preflight.py` gates uptime against
`--max-uptime-s` (default 3600), reports the two statuses separately, emits
`PREFLIGHT=PASS` only when both hold, and exits **3** on a warm boot — a
distinct code, because a warm boot is not a technical failure. The board was
then rebooted (`preflight/13_reboot.txt`, `14_wait_for_boot.txt`) and the
command re-run unchanged.

**Grep `^PREFLIGHT=` anchored.** `WARM_BOOT_TECHNICAL_PREFLIGHT=PASS` contains
the substring `PREFLIGHT=PASS`; an unanchored search reads a HOLD as a pass.
There is exactly one line that starts with `PREFLIGHT=`, and it is the verdict.

### The transcript

Authoritative: **`preflight/16_preflight_freshboot.txt`** (fresh boot, exit 0),
produced by the bytes now in `sw/` and snapshotted in `preflight/as_run/`.
Retained deliberately: runs 2 and 3 (`08_`, `11_`) are the same technical PASS
from a warm boot under an earlier revision, and run 1 (`06_`) is the FAILING
run that exposed finding 2.

Conditions 5–7 (synthetic gates 1–5, the first PDF and stress page, the
36-page corpus) are **NOT STARTED**. Board preflight is separate from Stage 1's
synthetic gates.

| gate line | value |
|---|---|
| `GATE=cma_pool_size;RESULT=PASS` | `CmaTotal 234,881,024 B` (224.0 MiB) vs required 201,326,592 B (192 MiB) |
| `GATE=live_clock;RESULT=PASS` | `LIVE_FCLK0_MHZ=100.0 EXPECTED=100.0 DIV_PRODUCT=10` |
| `GATE=live_fclk1;RESULT=PASS` | `LIVE_FCLK1_MHZ=125.0 EXPECTED=125.0 DIV_PRODUCT=8` |
| `GATE=matcher_vlnv;RESULT=PASS` | `TermCountB2:hls:tme_top:0.2` |
| `IDLE_CHECK=PASS` | quiescent, AXI-Lite live, reload returns power-on state |

The bytes that ran are snapshotted in `preflight/as_run/` and the payload was
verified byte-for-byte on the board before execution
(`preflight/05_payload_verify.txt`, 10/10 `OK`).

    bit C9E6EE67F07531CA187DA84798E422990EC9A5A23FC90011325D94866DD6FDE8
    hwh 32AC478E76F72F85F939CAF206F3CDD84BF27EB0B4A4DDD044559EAD459CF5B7

## The checklist, item by item

**1. CMA at least 192 MiB — PASS, and the platform is set to 224M, not 192M.**
`cma=224M` is on the kernel command line (`/proc/cmdline` and
`/boot/uEnv.txt`), giving `CmaTotal 229376 kB`. That is 32 MiB *above* the
documented requirement, so the requirement is satisfied — but the runbook and
`probe_cma_budget.py` both say `cma=192M`, and the board does not match that
text. Recorded rather than "fixed": 224M is the safe direction and changing it
costs a reboot for no gain.

**2. Driver-order buffer allocation — PASS.** Seven buffers, in the driver's
real order, stamped one byte per 4 KiB page, flushed, invalidated and verified:
512 B, 1,024 B, 251,740 B, 251,740 B, 20,736 B, then 63,078,400 B and
63,078,464 B. No overlap; the two full-page regions are 0x3BD8000 B apart,
which §2.2 permits (it requires two *separately* contiguous regions, not one
120.3 MiB region).

**Five of those seven, not all seven, come from `PLPipeline.__init__`.** The
grayscale and binary buffers are allocated lazily by `_ensure_image_bufs`, so
that importing the driver does not by itself decide the §2.2 gate. The probe
reproduces the correct overall sequence — five small, then the two full-page —
which is the property that matters, because CMA fragmentation is
order-dependent.

The first run passed with **`CmaFree` at 20,996,096 B (20.0 MiB)** before the
overlay was even loaded, far less than the 120.3 MiB it then allocated: the
kernel migrates page-cache pages out of the CMA region to satisfy a contiguous
request. So `CmaFree` is **not** a predictor of success and a low reading is
not a capacity problem.

**No claim is made that the warm runs were a "harder" test.** An earlier draft
said so and it does not follow. `CmaFree` and allocator state are
non-monotonic, and the three warm runs handed back **identical physical
ranges** (`0xF84A000`, `0xF880000`, `0xF900000`, `0x17100000`), consistent with
reuse of just-freed regions rather than with allocating against a genuinely
more fragmented pool.

The fresh-boot run settles it in the opposite direction from the original
claim: it placed `binary` at a **different** address (`0x13600000`) and ended
with **`CmaFree` at 3,608,576 B (3.4 MiB)** while holding both pages —
markedly tighter than the warm runs' 20.7–26.3 MiB. So the fresh boot was the
tighter allocation, not the looser one. Neither uptime figure supports a
fragmentation-robustness argument; each run is evidence only that the
allocation succeeded under its own conditions.

**3. Load the matching B2/100 `.bit` and `.hwh` — PASS.** Digests checked
twice over: against the values pinned in `sw/board_expect.py` and against the
`BUILD_INFO.txt` shipped beside the bytes, with `variant=combined_b2_100`,
`matcher_core=TermCountB2:hls:tme_top:0.2` and both divisor products
(`fclk0 10`, `fclk1 8`) confirmed. Both must agree: the pinned value ties the
result to a build this repository knows about, the shipped record ties it to
the bytes.

**4. `ip_dict` and register addresses — PASS.** Nine IPs enumerated. All 8
base addresses and all **38 register offsets** (tme_top_0 14, binarize_core_0
7, patch_extract_core_0 17) match values pinned from the shipped HWH. Every
DMA has exactly the channels it should and no others;
`axi_dma_binarize buffer_max_size = 67,108,863 B` ≥ the 63,078,400 B page.

The offsets are checked, not just the names. A name-only check passes an IP
whose ports were reordered, because HLS keeps the names and moves the
addresses — and the driver writes offsets.

**5. Live `Clocks.fclk0_mhz == 100.0` — PASS, with two limits.** First, fclk0
alone could not have established it (finding 1 below). Second, and more
important:

**`Clocks.fclk0_mhz` is a divisor/register read-back, not an independent
measurement of the clock.** PYNQ computes it from the PLL model and the FCLK0
divisors it reads out of the SLCR. It proves the divisors this overlay
programmed are the ones in the register — which is exactly what the 62.5 MHz
trap corrupts, so it is the right check for that failure — but **no edge was
counted**. A PLL that failed to lock, or a clock that is not actually toggling
at the programmed rate, would still read 100.0.

The independent check exists and must still be run: **B2 at 820×307 / 216×96 is
L = 257,145,732 cycles = 2.5715 s at 100 MHz, with the nearest wrong clock rung
257 ms away** (`GATE_B_C_EVIDENCE.md` §8). A wall-time measurement of that case
is the edge-count evidence; the preflight is not a substitute for it. It
belongs in Stage 1, not here.

**6. Reset and idle before DMA traffic — PASS.** Three cores at
`CTRL=0x00000004` (idle, nothing started, nothing pending, no auto-restart);
all six `ap_vld` sideband registers clear, so no result from a previous session
survived; all seven DMA channels at `DMACR=0x00010002 DMASR=0x00000001`
(Halted=1, RS=0, no error bits). A second full read returned identical words.
Then `GIER=1` was written to each core and read back, the PL was reprogrammed,
and all three read `0x00000000` again with the whole quiescent state intact.

## Finding 1 — an fclk0-only gate is fail-open on this board

PYNQ's power-on `fclk0` on this board is **100.0 MHz** — read before our
overlay was loaded (`preflight/04_clocks_preexisting.txt`). That is exactly the
value `combined_b2_100` requires. So an overlay that never programmed the
clocks at all would read a perfect 100.0 and pass an fclk0-only gate.

`fclk1` closes it. The 100 MHz recipe works *because* FCLK1 is enabled at
125 MHz (that is what forces the PS7 solver off its 1600 MHz IO PLL model), and
PYNQ's default fclk1 is **142.857143** — a different number. Reading
**125.0** is therefore positive evidence that this overlay's divisors were
applied, which fclk0 cannot supply here. `inspect_overlay.py` now gates both,
and the off-board suite carries the exact fail-open world as a mutant
("overlay never programmed the clocks (fclk0 right by coincidence)").

For `--variant baseline`, fclk1 is recorded and **not** gated: that design does
not drive FCLK1, and a gate there would break the shipping image's own
preflight. A control asserts that.

## Finding 3 — `inspect_overlay` was fail-open on the DMA transfer bound

If neither `buffer_max_size` nor `sendchannel._max_size` answered, the script
printed a line and carried on to exit 0 — "could not verify" reading as
"verified", the same mistake the clock check used to make and in the same
direction. It is now a failure with a `GATE=binarize_transfer_bound;
RESULT=CANNOT_VERIFY` line, and the off-board suite carries it as a mutant.

**This run is unaffected**: the bound was reported and measured
67,108,863 B ≥ 63,078,400 B.

## Finding 2 — the first idle gate reported a fault that was its own

Run 1 failed step 4 with all seven DMA channels reading
`DMACR=0x00010003 DMASR=0x00000000` — RS set, not halted. That was not the
hardware.

`pynq.lib.dma.DMA` **starts every channel in its constructor**, so merely
evaluating `overlay.axi_dma_binarize` writes `DMACR.RS = 1`. Measured directly
(`preflight/07_probe_dma_driver_starts.txt`): read through raw MMIO with no
driver constructed, the seven channels are `0x00010002 / 0x00000001`
(Halted=1, RS=0); after nothing but constructing the five driver objects, all
seven are `0x00010003 / 0x00000000` — **7 of 7 changed, with no transfer
requested**.

So the "read-only" check was writing, and it was reporting the state it had
just created. Fixed by addressing every register through `pynq.MMIO` on the
pinned base address and never instantiating a driver.

Two things follow, and both are now permanent:

* Phase 1 reads the entire state **twice** and requires the two readings to be
  identical. A read path with a side effect can no longer pass unnoticed, and
  the off-board suite carries that exact defect as a mutant ("a read that
  starts the engine it is measuring") — a world where the fabric is genuinely
  quiescent and only the *change under reading* gives it away.
* `inspect_overlay.py` prints that resolving the DMAs starts them. It is not a
  bug there — it has to construct the drivers to inspect the channels — but
  whatever runs next must not judge the state it leaves behind. `board_idle_
  check.py` reloads the overlay for exactly that reason.

## The fresh-boot re-run, and what it added

Rebooted 2026-08-22 00:22 UTC and re-run at **523.27 s uptime** with the
command unchanged. Everything reproduced: same digests, same divisors, same
VLNV, same 8 addresses and 38 offsets, same seven halted channels, same idle
and reset behaviour, `PREFLIGHT=PASS`, exit 0.

Two things only a fresh boot could give:

* **The fclk1 discriminator was genuinely armed.** After the reboot and before
  any overlay load, `fclk1` read PYNQ's default **142.857143**
  (`preflight/15_post_boot_state.txt`); after loading, **125.0**. On the warm
  runs it was already 125.0 from the previous session's overlay, so the
  transition itself had never been observed. It has now.
* **The CMA allocation succeeded from a pool that had never served this
  design**, ending at 3.4 MiB free — see item 2.

The board's DHCP lease moved across the reboot (10.0.0.97 → 10.0.0.20).
`board.py` resolves the `pynq` hostname, so nothing needed changing; a cached
address does not survive a reboot here and must never be hardcoded.

## What this does and does not establish

It establishes that the shipped B2/100 bytes load, that the FCLK0 divisors in
the SLCR are the 100 MHz ones and not the 62.5 MHz trap (a register read-back,
not an edge count — see item 5), that the matcher in the fabric is
`TermCountB2`, that every address and offset the driver writes is where it
expects, that the CMA pool satisfies the driver's own allocation order under
these conditions, and that the fabric is quiescent and resettable before any
traffic.

It establishes **nothing about results**. No DMA moved, no core ran, no score
was computed. Numerical behaviour, `TLAST` discipline, ordering and tie
behaviour are all still owed, and they start at synthetic gate 1.

A pass is evidence for **this boot**. Re-run after any reboot or power cycle.

## Board state left behind

`fclk0 = 100.0`, `fclk1 = 125.0` (PYNQ default 142.857143 — restored by a
reboot or by loading a different overlay), `CmaFree 100,980 kB` with every
preflight buffer freed, B2/100 overlay resident, all engines halted, board at
**10.0.0.20** (`preflight/17_final_board_state.txt`).

The payload is left on the board at
`/home/xilinx/jupyter_notebooks/b2prod_preflight/`, with `PAYLOAD.sha256`
covering all ten files.

## Changed and added files

    sw/board_expect.py        NEW  variant table (board half of the Tcl one),
                                   pinned address map, register offsets, DMA params
    sw/board_idle_check.py    NEW  reset/idle gate, raw MMIO, two phases
    sw/board_preflight.py     NEW  orchestrator + payload identity + stop rule
    sw/test_board_preflight.py NEW off-board suite: 47 checks — 33 injected
                                   defects, each required to fail, plus the
                                   fresh-boot cases, clean controls and the
                                   two module selftests
    sw/inspect_overlay.py     MOD  --variant; live fclk0 AND fclk1 gates;
                                   matcher VLNV; base addresses; register
                                   offsets; transfer-bound fail-open closed
    sw/BOARD_RUNBOOK.md       MOD  preflight section; gate 2's stale clock
                                   criterion; `sudo -E` throughout
    sw/board_gate_extract.py       header only: `sudo` -> `sudo -E`
    sw/board_gate_full_dma.py      header only: `sudo` -> `sudo -E`
    sw/board_gate_protocol.py      header only: `sudo` -> `sudo -E`
    sw/probe_cma_budget.py         header only: `sudo` -> `sudo -E`

Every script header recommended plain `sudo python3`, which does not work on
this image — it resolves `/usr/bin/python3` (no pynq), and the venv
interpreter under `sudo` dies with "No Devices Found" because `XILINX_XRT` is
dropped. All corrected to `sudo -E`.

Off-board, before any board time: `python3 test_board_preflight.py` (47 checks),
`python3 board_idle_check.py --selftest`, `python3 board_expect.py`.
