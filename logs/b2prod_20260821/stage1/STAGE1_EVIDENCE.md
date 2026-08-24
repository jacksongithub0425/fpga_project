# Stage 1 — synthetic gates, B2/100 on silicon

    STAGE1 = PASS

Run 2026-08-22, on a board **12 minutes from a cold boot**, against the
`combined_b2_100` bitstream pair (`bit_sha256` `C9E6EE67…6DD6FDE8`,
`hwh_sha256` `32AC478E…459CF5B7`). Six board GATE runs, every one exit 0, no
retries, no indeterminate outcomes. That is a claim about the gates, not about
the session: one correction was needed before them — see Provenance.

**Completion condition 5 is closed. Remaining B2/100 blockers are 6 and 7** —
the first PDF plus the stress page, and the 36-page corpus.

| run | verdict | checks | transcript |
|---|---|---|---|
| preflight (fresh boot) | `PREFLIGHT=PASS` | 4 steps | `05_preflight.txt` |
| gate 3 — full-page DMA | PASS | — | `06_gate3_full_dma.txt` |
| gate 4 — extractor + matcher | PASS | 61 / 0 failures | `07_gate4_extract.txt` |
| gate 5 — stream protocol | PASS | 120 / 0 failures | `08_gate5_protocol.txt` |
| gate 6 — counted clock | PASS | 15 / 0 failures | `09_gate6_clock.txt` |
| gate 7 — timeout & reset recovery | PASS | 42 / 0 failures | `10_gate7_recovery.txt` |

238 asserted checks across gates 4–7, plus gate 3's bit-exact comparison of
63,078,400 bytes.

## What the plan asked for, and where each item was met

| Stage 1 bullet | met by | result |
|---|---|---|
| Full binarizer DMA test | gate 3 | 63,078,400 B each way, bit-exact |
| Extractor/matcher smoke | gate 4 phases A–B | 480/480 binary, 168/168 patch bytes |
| B2 9/9 matcher suite | gate 4 phase C | 9/9 — and three more 9/9 runs inside gate 7 |
| Maximum-patch case | gate 4 phase C case 7 | 251,740 B programmed, core completed |
| Small invocation right after the maximum case | gate 4 phase C re-invocation | `+1.000000 @(0,0)`, no stale BRAM |
| Multi-candidate + repeated-invocation protocol | gate 5 phases B–C | 4/4/1/2/2 descriptors, one permuted, one duplicated |
| Increasing and decreasing transaction sizes | gate 5 | `[950, 650, 722, 352]` B — down, **up**, down |
| Timeout/reset recovery | **gate 7 (new)** | all five phases |
| Exact CPU-reference comparison | gate 3 oracle + gates 4/5 pinned goldens | bit-exact |

Numerical acceptance, as specified: **exact winning locations** on every case
of every suite; **exact row-major tie behaviour**, with the reversal control
moving the answer in both gate 4 and gate 5; **score error ≤ 0.005** — the
tolerance was never approached, every score printed to six decimals equal to
its golden.

Two Stage-1 items had no gate before this session. Both were built, both were
proved off-board against injected defects first, and both passed on silicon
first time:

* **gate 6**, the counted clock check — required by the preflight's own
  finding that `Clocks.fclk0_mhz` is a divisor read-back and not an edge
  count. Not in the plan's bullet list; it belongs to Stage 1 because that is
  where the independent clock evidence was deferred to.
* **gate 7**, timeout and reset recovery — an explicit bullet, and the
  runbook had said in as many words that gates 1–4 "do not validate any
  failure-recovery path".

## Preflight — fresh boot, condition 4 re-closed for this boot

    UPTIME_S=350.82;MAX_UPTIME_S=3600;GATE=fresh_boot;RESULT=PASS
    CMA_TOTAL_BYTES=234881024;CMA_FREE_BYTES=25878528;GATE=cma_pool_size;RESULT=PASS
    LIVE_FCLK0_MHZ=100.0;EXPECTED=100.0;DIV_PRODUCT=10;GATE=live_clock;RESULT=PASS
    LIVE_FCLK1_MHZ=125.0;EXPECTED=125.0;DIV_PRODUCT=8;GATE=live_fclk1;RESULT=PASS
    MATCHER_VLNV=TermCountB2:hls:tme_top:0.2;GATE=matcher_vlnv;RESULT=PASS
    BINARIZE_BOUND_BYTES=67108863;REQUIRED=63078400;RESULT=PASS
    IDLE_CHECK=PASS
    PREFLIGHT=PASS;VARIANT=combined_b2_100;BOARD_FCLK0_MHZ=100.0

`fclk1 = 125.0` against PYNQ's default 142.857143 is what makes this a real
clock check: the board's power-on `fclk0` is already 100.0, so an fclk0-only
gate would pass an overlay that never programmed the clocks.

**199.3 MiB of the 224 MiB CMA pool was already in use before the preflight
allocated anything**, and the preflight said so. It is not a fault and it did
not become one: `CmaFree` counts free pages *inside* the CMA region, and the
kernel lends those pages to ordinary movable allocations until a CMA request
migrates them out. The driver-order allocation then succeeded in full — all
seven buffers, two separately contiguous 60.2 MiB regions, every page stamped
and verified, ending at `CmaFree` 1.8 MiB. Gate 3 moved a full page
afterwards without trouble. **Do not read a low `CmaFree` at rest as a
predictor of allocation failure**; the probe is the predictor.

## Gate 3 — the full page

    S2MM 63,078,400 B received (MEASURED — the engine wrote the count)
    MM2S 63,078,400 B programmed
    guard 64 B intact, 0 sentinel bytes left
    bit-exact against the truncating Gaussian CPU oracle
    PL round trip 10.74 s   (page build 21.4 s, oracle verify 38.4 s, both PS-side)

The 10.74 s is not 31.25/100 of the baseline's 11.96 s and should not be
quoted as a speed-up: this path is dominated by the two 63 MB DMA transfers
through HP1, not by binarizer compute.

## Gate 6 — the counted clock, and what it does and does not say

Two probes over the same 251,740-byte patch envelope, five repetitions each,
minimum taken:

| probe | modelled cycles | modelled at 100 MHz | measured (min of 5) | spread |
|---|---|---|---|---|
| long `820×307 / 216×96` | 257,145,732 | 2.5715 s | **2.5729 s** | 0.43 ms |
| short `820×307 / 4×4` | 7,596,404 | 0.0760 s | **0.0773 s** | 0.17 ms |
| difference | 249,549,328 | 2.4955 s | **2.4957 s** | — |

    absolute      residual +1.47 ms          (band −0.5 .. +10.0 ms)
    differential  implied 99.9929 MHz        (−0.007% from 100.0)
    rung          nearest 1000/d is 1000/10 = 100.0000 MHz

The differential is the figure to quote: both probes program an identical
251,740-byte patch MM2S and differ only in a template transfer of 20,736 vs
16 bytes, so the fixed per-invocation overhead very nearly cancels. The
+1.47 ms absolute residual sits just under the +1.6 to +2.5 ms seen across
the nine cases of the 2026-08-20 B2 session at 125 MHz — consistent with a
fixed per-invocation overhead, which is all that is claimed; nothing here
isolates its cause.

**The discrimination is not marginal.** At 125 MHz the long probe would be
2.0572 s and at 62.5 MHz 4.1143 s; the measured 2.5729 s is 25% and 60% away
from those. The nearest *reachable* rungs to 100 are 1000/9 = 111.11 and
1000/11 = 90.91, both more than 9% away, against a 0.5% acceptance band.

**These are MODELLED cycles.** The RTL has no page-level or transaction-level
cycle counter. The cycle totals come from the law RTL co-simulation pinned
(`tile = T*(tw+44) + tw − 2`, B2); what was measured is PS-side wall time.
Write "wall time consistent with 100 MHz", never "measured 257,145,732
hardware cycles". `test_board_gate_clock.py` checks the gate's transcription
of that law against `tme_cycle_model.cycles()` for all nine manifest cases
under both laws, because a typo in a transcription moves expectations rather
than failing.

## Gate 7 — the failure path, exercised on purpose

    R1  TimeoutError raised after 0.539 s against a 0.5 s deadline
        AP_CTRL=0x00000001, seen_busy=True
        — a transaction modelled at 2.571 s, cut short while genuinely running
    R2  match_template / binarize_page / extract_candidates all refused
        with RuntimeError naming the outstanding transfer
    R3  close() returned False and RETAINED 5 DMA buffers; reset_pl True
    R4  fresh PLPipeline: 9/9 golden
    R5  tme_top started through AXI-Lite at 820×307 with no MM2S armed:
        busy for 300 ms across 141 samples, ap_done never rose, ap_idle
        never returned (AP_CTRL=0x00000001)
        _start refused the next invocation: "core is not idle before start"
        close() retained; reset_pl True
    R5-after  fresh PLPipeline: 9/9 golden

`_start`'s `ap_ctrl_hs` idle guard had never run on silicon before this. It
is the guard against the worst silent failure this hardware has — writing
`ap_start` to a busy core leaves the bit pending, and the still-running
invocation consumes the beats armed for the new one, registering a
plausible-looking result computed from the wrong pixels. It refused, and
named the reason.

**Not established by this gate:** a DMA error interrupt, an AXI decode error,
a core hanging mid-write to an S2MM, and the fail-stop path where `reset_pl`
itself fails. Two stall shapes were exercised — a deadline on a healthy
transaction, and a core blocked on a starved input stream — and that is what
may be claimed.

## Provenance

Payload staged by `sw/stage_board_payload.py --variant combined_b2_100
--stage 1`: 32 files, 6,026,533 bytes, each verified before copying against
the record that governs it (`board_expect.VARIANTS` **and** the shipped
`BUILD_INFO.txt` for the bitstream pair; `GATE4_VECTORS.sha256` for gate 4's
eight vectors; `GATE5_VECTORS.sha256` for gate 5's five). `PAYLOAD.sha256`
was written over all 32 and re-verified **on the board**: 32/32 OK before any
gate ran, and 32/32 OK again after gate 7.

**That verification did not pass on the first attempt.** `PAYLOAD.sha256`
reached the board with CRLF line endings, so `sha256sum -c` looked for
`three_stage_combined.hwh` and every one of the 32 entries failed to open
(`02_upload.txt`, lines 90-99: "WARNING: 32 listed files could not be read").
The manifest was rewritten with LF and re-verified 32/32 before any gate ran.
No payload byte was in question at any point — the failure was in the manifest's
own line endings, not in the files it names — but the session did contain a
correction and this is it.

`as_run/` holds the exact bytes of the nine load-bearing modules, each
`.as_run` snapshot verified byte-identical to its `PAYLOAD.sha256` entry.

**Two changes were made after the run and they are named here rather than left
to be discovered.** Both are to `board_gate_recovery.py`; the `.as_run` snapshot is
unchanged and still matches its `PAYLOAD.sha256` entry
(`2a81bcda…4ac7715`), so every transcript in this directory continues to name
the bytes that produced it.

1. **The closing banner (during the session, print-only).** The tidy-up
   reprogram used `safe_teardown.reset_pl`, which opens with the `UNSAFE
   TEARDOWN` banner and closes with "The gate still FAILS" — correct at R3
   and R5, misleading as the last thing a passing transcript prints. It now
   calls a local `final_reprogram()` instead. **No assertion, no phase and no
   hardware interaction changed.** The transcript in `10_gate7_recovery.txt`
   was produced by the `.as_run` version and still carries that banner at the
   end.

2. **The fail-open exit on a failed reprogram (after the session, BEHAVIOURAL).**
   R3 and R5 both reach their `reset_pl` call holding CMA pages that `close()`
   refused to free. If that reprogram failed, the gate recorded the failure,
   ran on, and let `main()` return a status — ending the process and handing
   those pages back while the fabric was in an unknown state, which is the
   exact release the retention exists to refuse. `safe_teardown` has always
   had `fail_stop_holding` for this and the gate never called it. Both sites,
   `run_all`'s abort handler, and a failed *closing* reprogram in `main()`
   now hand over to it; it never returns.

   **This does not touch Stage 1's result.** The changed branch is reached
   only when a reprogram returns False, and neither did: `10_gate7_recovery.txt`
   lines 58 and 91 record `reset_pl returned True` at R3 and at R5, and the
   gate's own closing line reports 0 failures over 42 checks. The run was on
   the fail-open path's *passing* side throughout, so no assertion in this
   document is re-derived and **Stage 1 remains PASS**.

   Off-board proof is in `FAILSTOP_FIX.md`, beside this document: the suite is
   now 13 tests and 10 injected defects
   (`reset_fails_late` added), and five separate mutants that re-open the
   fail-open path — R3 runs on, R5 runs on, both run on, the hold is handed an
   empty list, the return-guard is deleted — are each caught by the test that
   targets them, with an unmutated control run proving the tests pass when the
   defect is absent. Every mutant was compiled and imported before it was
   scored, so none was "detected" for failing to build.

   **Gate 7 must be re-run before its next use is quoted**, because the
   version that passed on silicon is not the version that would run now.

## Off-board evidence behind the two new gates

Neither gate's first execution was on hardware.

    board_gate_clock.py --selftest        5 checks, all three variants
    test_board_gate_clock.py             12 tests, 7 injected clock defects
                                         (125 / 62.5 / 90.9 / ±1% / huge
                                         overhead / wrong cycle law), each
                                         required to fail the gate; plus the
                                         cycle law checked against
                                         tme_cycle_model.cycles() on 22
                                         geometry/law pairs
    test_board_gate_recovery.py          10 tests, 9 injected defects
                                         (no_timeout, late_timeout,
                                         latch_open, close_frees,
                                         reset_fails, stale_after_reset,
                                         wedge_completes, starts_on_busy,
                                         retain_open_after_wedge)

Both suites run against a **virtual clock** substituted for `time` inside
`tme_driver`, so a 0.5-second deadline expiring on a 2.57-second transaction
costs nothing. The recovery fake models the property the gate turns on: its
core completes only if the patch MM2S was armed since it was started, so R5's
wedge is produced by the same mechanism there as on silicon.

The five pre-existing suites were re-run unchanged and still pass:
`test_board_preflight.py` 47 checks, `test_board_gate_full_dma.py` 13/13,
`test_board_gate_extract.py` 14/14, `test_board_gate_protocol.py` 6/6,
`test_gate_signals.py` 6/6.

## Board state left behind

    uptime 12 min, fclk0 100.0, fclk1 125.0, CmaFree 82,372 kB
    fabric in its power-on state (gate 7's closing reprogram)
    payload at /home/xilinx/jupyter_notebooks/b2prod_stage1, 32/32 verified

## What Stage 1 does not establish

No PDF has been through this overlay. Every page in this stage is synthetic
and pinned — 24×20, 96×64, and the matcher's own manifest geometries. Nothing
here says anything about detector parity, candidate ordering on a real page,
per-page wall time, or the 36-page corpus. Stage 2 (`doc_002`) is next,
then the stress page, then the corpus.

## Changed and added files

    sw/board_gate_clock.py           NEW  gate 6 — counted clock, two probes,
                                          absolute + differential + rung
    sw/test_board_gate_clock.py      NEW  12 tests, 7 injected defects,
                                          cycle-law transcription check
    sw/board_gate_recovery.py        NEW  gate 7 — R0..R5 recovery
    sw/test_board_gate_recovery.py   NEW  10 tests, 9 injected defects
    sw/stage_board_payload.py        NEW  payload assembler + PAYLOAD.sha256
    sw/BOARD_RUNBOOK.md              MOD  gates 6 and 7: acceptance rows,
                                          payload list, two sections
    logs/b2prod_20260821/stage1/     NEW  transcripts, as_run/, this document
