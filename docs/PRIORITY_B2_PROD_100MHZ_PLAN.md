# Priority B2-PROD — Combined B2 at 100 MHz

## Objective

Integrate the preserved, standalone-validated B2 matcher into the complete
overlay and qualify the exact routed image at 100 MHz.

This task must:

* Use `TermCountB2:hls:tme_top:0.2` from its preserved `impl/ip` directory.
* Exclude the B0b changes and preserve the matcher register map and stream
  topology.
* Produce a matching `.bit` and `.hwh` pair.
* Meet implementation timing at 10.000 ns.
* Pass synthetic, board and end-to-end PDF qualification.

This task does not include Phase S, pruning, B0b, trial reduction or the
five-minute redesign.

## Current status — 2026-08-23

* Gate A passed for the combined current matcher at 100 MHz.
* Gates B and C passed for combined B2/100: WNS `+0.135 ns`, WHS `+0.008 ns`,
  62,813/62,813 nets routed, and 12,347/13,300 slices (92.83%).
* Formal fresh-boot board preflight passed.
* Stage 1 passed on silicon. **The current Gate 7 re-run obligation is
  discharged**: `board_gate_recovery.py` passed on silicon on 2026-08-23 with
  42 checks and 0 failures (`logs/b2prod_20260823/02_gate7_recovery.txt`),
  and the two things that would otherwise have left it unattributable were
  closed on the same boot — the five-module runtime digest closure
  (`03_script_identity.txt`, `gate7 exit code recorded this boot: 0`) and an
  independent counted-clock gate implying **100.0176 MHz** against 100.0 MHz
  over 15 checks (`04_counted_clock.txt`).
* The Stage 2–4 memory path is implemented and qualified off-board. The
  PL-backed `detect_page()` seam exists; streamed stripe/histogram Otsu removed
  the former 558,217,748-byte allocation; `samples_mv` removes the Pixmap
  copy; conversion striping avoids full-page BGR; and both runners pass
  `keep_bgr=False`. Native-gray rendering and clip-striped rendering are
  retained as tested negative results. A maximum-page, whole-pipeline run on
  the board is still required before memory feasibility is closed on silicon.
* The checkpoint memory sampler is built and closed off-board
  (`logs/b2prod_20260823/06_memory_sampler.md`, `10_step1_review_fixes.md`).
  Every off-board record reads `NOT-A-GATE` by construction. Its
  `distinct_bytes` prediction has been checked against the real allocator on
  the board (`07_pynq_alias_chain.txt`).
* **The board cannot render a page.** PyMuPDF is absent and no armv7l wheel
  exists (`05_board_environment.md`). The staged-isolation probe for the
  `python3-fitz 1.19.2` alternative **failed under the agreed
  PYTHONPATH-only protocol and cannot pass under it**: `_fitz` links
  `libgumbo.so.1` and `libmujs.so.1`, neither is on the board, and the
  dynamic loader does not consult `PYTHONPATH`. A labelled supplementary run
  showed it is ~195 KB of further `.deb` away, not impossible
  (`09_step2_fitz_1192.md`). **Formal protocol v2 then passed**: v1 plus the
  two libraries in the same throwaway root, 34 gates, 0 failures, predictions
  registered before the run (`11_protocol_v2_PREREGISTERED.md`,
  `12_protocol_v2_RESULT.md`). It runs THIS PROJECT'S renderer, not raw
  `fitz`, and the module the board loaded is byte-identical to the committed
  one. The binding is 1.19.2 but its rasteriser is **MuPDF 1.19.0**, against
  which no parity result on record was produced.
* Board `cv2` is 4.5.4 against dev 5.0.0. `BGR2GRAY` and Otsu are
  bit-identical; `matchTemplate` differs by one float32 ULP (~1.2e-7),
  reaching host refinement only, where it can flip an argmax on a genuine
  tie.
* B2/100 is not yet a final PASS. The open work is: requalification of
  MuPDF 1.19 across the 36-page corpus, the OpenCV 4.5.4 replay gate, the
  maximum-page whole-pipeline memory gate, then Stage 2, Stage 3 and
  Stage 4. Protocol v2 is discharged.
* Deployment is **decided and not yet executed**: board-native
  `python3-fitz 1.19.2` from apt, installed properly. Per-run staging is
  rejected as the deployment -- it was protocol v2's mechanism for proving
  feasibility without touching the board, and a throwaway root that is
  rebuilt every run makes the runtime an unrecorded variable of each run. A
  persistent unpacked root is rejected for the same reason with less of the
  benefit. The fallback, if the distro package cannot be made to work, is a
  native build on the board behind a temporary 1 GiB SD swapfile -- hours,
  and the 1.24+ rebased bindings are untested on 32-bit -- not a cross
  toolchain and not an off-board wheel, because no supported ARMv7 wheel
  exists. Protocol v2 proves the staging works; the deployment is the apt
  install, and Stage 2 is blocked on doing it.

## Phase 0 — preserved input and clock configuration

### Preserve the B2 release candidate

* [ ] Use the preserved B2 `impl/ip` directory containing `component.xml`, not
  the ZIP and not regenerated HLS output from the dirty live tree.
* [ ] Confirm Vivado resolves `TermCountB2:hls:tme_top:0.2`.
* [ ] Preserve unrelated B0b and worktree changes.
* [ ] Record the ordinary Git revision, IP directory and VLNV.

Compare the baseline and B2 `component.xml` files for AXI-Lite offsets,
AXI-Stream ports and widths, clock/reset ports, directions, control protocol
and maximum dimensions. This is structural ABI evidence only; result
semantics, `TLAST`, ordering and tie behavior still require cosim and board
tests.

### Validate the clock configuration

Before implementation:

* [ ] Confirm the PS7 IO PLL model is 1000 MHz.
* [ ] Confirm FCLK0 divisor product `5 × 2 = 10` and FCLK1 divisor product
  `4 × 2 = 8`.
* [ ] Confirm the generated periods are 10.000 ns for FCLK0 and 8.000 ns for
  FCLK1.
* [ ] Confirm FCLK1 has no sequential cells, launch paths, capture paths or
  other logic loads.
* [ ] Inventory all FCLK0 consumers, including the five DMAs, SmartConnects,
  binarizer, extractor and matcher.

The divisor and `Clocks.fclk0_mhz`/`Clocks.fclk1_mhz` readings are preflight
checks, not independent clock measurements. In particular, power-on FCLK0 is
already 100.0 MHz on this board, so an FCLK0-only read-back is fail-open.
FCLK1 changing from the power-on 142.857143 MHz to 125.0 MHz corroborates that
the overlay's clock divisors were applied.

Final clock acceptance requires the Stage 1 counted-clock differential gate:

* both known-cycle probes must return their golden numerical results;
* the difference must represent 249,549,328 modeled B2 cycles;
* the implied clock must be within 0.5% of 100.0 MHz; and
* 100.0 MHz must be the nearest valid `1000/divisor` clock rung.

The recorded differential was about 2.4957 seconds, implying 99.9929 MHz
(`−0.007%`). This is measured PS-side wall time against a cosim-validated
cycle model, not a hardware cycle counter.

### Parameterize sign-off

Parameterize the matcher VLNV, expected FCLK frequency, expected clock period,
clock inventory and build/output variant. Retain the baseline defaults and the
separate `combined_current_100` and `combined_b2_100` variants. The timing
clock is generated by PS7; do not add or remove a separate 20 ns XDC clock.

## Gate A — combined current matcher at 100 MHz

Before introducing B2, implement the complete combined design with the
current production matcher and a 10.000 ns FCLK0 period. Capture utilization,
congestion, timing, DRC and constraint reports.

Acceptance:

* Setup WNS ≥ 0 and TNS = 0.
* Hold WHS ≥ 0 and THS = 0.
* No unconstrained endpoints.
* Complete routing and no critical DRC or methodology violations.
* FCLK0 is 10.000 ns; enabled FCLK1 is 8.000 ns and has no logic endpoints.

If Gate A fails, diagnose the combined clock/reset/interconnect problem before
introducing B2. Do not attribute a Gate-A failure to B2.

## Gate B — combined B2 at 100 MHz

Replace the baseline matcher with `TermCountB2:hls:tme_top:0.2`, preserving
its instance name, register addresses and stream connections. Regenerate the
block design, wrapper and output products, then run clean synthesis and
implementation.

Resource expectations are diagnostic, not acceptance limits. Only
implemented placement and routing establish feasibility; do not use a naive
slice sum or LUT-per-slice density as a capacity gate.

## Timing-closure procedure

Run one normal timing-driven implementation before adding physical
constraints. If it fails, classify it as placement/resource, routing/
congestion, setup, hold, or constraint/clock-definition failure. Then use
evidence-driven changes such as timing strategies, `phys_opt_design`, exact
critical-path analysis, high-fanout control-net analysis, placement relative
to BRAM/DSP columns, targeted floorplanning, or a limited justified
strategy/seed experiment.

Do not false-path legitimate reset or correlation paths, ignore hold timing,
hide unconstrained endpoints, run blind seed farms, accept a lower-frequency
build as B2/100, or change HLS before identifying the physical cause.

If HLS is regenerated, the result is a new B2 revision. Repeat C simulation,
RTL cosimulation, standalone implementation and standalone board
qualification before returning to the combined design.

## Gate C — implementation sign-off and artifact freeze

The combined B2/100 image passes Gate C only when:

* [ ] FCLK0 period is exactly 10.000 ns.
* [ ] Setup WNS ≥ 0 and TNS = 0.
* [ ] Hold WHS ≥ 0 and THS = 0.
* [ ] No unconstrained endpoints remain.
* [ ] Clock/reset relationships are valid, including zero FCLK1 logic
  endpoints.
* [ ] Routing is complete.
* [ ] DRC and methodology reports contain no critical violations.
* [ ] No unjustified timing exceptions were added.
* [ ] Post-route utilization and congestion are documented.
* [ ] The `.bit` and `.hwh` come from the same routed build.

At 92.83% slice occupancy and 135 ps setup margin, the accepted image is
timing-clean but physically fragile. Treat it as immutable throughout board
qualification. The exact production pair is pinned in the
`combined_b2_100` manifest entry:

```text
bit  C9E6EE67F07531CA187DA84798E422990EC9A5A23FC90011325D94866DD6FDE8
hwh  32AC478E76F72F85F939CAF206F3CDD84BF27EB0B4A4DDD044559EAD459CF5B7
```

The standalone-B2 re-baseline guard protects the historical HLS/IP evidence;
it does not adopt a new combined board bundle. The combined image is protected
by the independent `board_expect.py` pins, its `BUILD_INFO.txt`, and staging/
preflight refusal on any disagreement. Do not edit those pins merely to silence
a mismatch. Any regenerated `.bit` or `.hwh` re-enters qualification at Gate C,
not at board preflight; after Gate C passes, deliberately register the new pair
and repeat fresh-boot preflight, Stage 1 and every later board stage claimed for
that new artifact. A source, IP or block-design change also re-enters Gate B.
Software-only Gate 7 fixes do not reopen Gate C while the pinned PL pair remains
unchanged.

## Board-session discipline

The formal preflight starts from a fresh boot and must confirm:

* [ ] CMA is at least 192 MiB and the driver-order allocation succeeds.
* [ ] The exact pinned B2/100 `.bit`, `.hwh` and build manifest agree.
* [ ] The matcher VLNV, IP/register addresses and offsets agree with the HWH.
* [ ] FCLK0 reads 100.0 MHz and FCLK1 reads 125.0 MHz as divisor-register
  preflight corroboration.
* [ ] Raw-MMIO reset and idle checks pass before DMA traffic.

Resolve and validate the bundle identity from the HWH before constructing a
`PLPipeline` or programming the PL. PYNQ's DMA driver starts channels when its
objects are constructed. Therefore, perform the raw-MMIO idle inspection
before resolving DMA driver objects, and preserve the `PLPipeline` allocation/
driver order. Driver construction itself must never be mistaken for evidence
that the power-on DMA state was running. The current application runners do
not replace this external preflight: they construct `PLPipeline` before their
internal identity/clock gate.

Each Stage 2–4 board session must either:

1. begin from a fresh boot and repeat preflight plus the counted-clock gate; or
2. explicitly repeat the CMA allocation/identity/idle checks and the counted-
   clock gate before processing a page.

`Clocks.fclk0_mhz == 100.0` alone never satisfies that requirement.

## Staged board qualification

### Stage 1 — synthetic gates

The synthetic qualification covers full-page DMA, extractor/matcher
correctness, stream protocol, the counted clock, timeout/reset recovery,
maximum-envelope and post-maximum re-invocation, exact locations, row-major
tie behavior and score error ≤ 0.005.

This obligation is **discharged**. The current `board_gate_recovery.py` — the
one whose fail-stop/reprogram-failure handling changed after the earlier
silicon PASS — was re-run on 2026-08-23 and passed with 42 checks and 0
failures, leaving the board reprogrammed and clean. What makes that
attributable rather than merely recorded is on the same boot: five module
digests pinning the runtime that produced it, and an independent
counted-clock gate. `logs/b2prod_20260823/02_gate7_recovery.txt`,
`03_script_identity.txt`, `04_counted_clock.txt`.

The rule that produced the obligation stands: a recorded PASS that predates a
change to the script's failure handling is not a PASS of the current script,
and must be re-run rather than re-quoted.

### Stage 2 — first real PDF through `pl-all`

Run `doc_002` using the explicit `pl-all` backend and
`--variant combined_b2_100`.

`pl-all` is the criterion for “without PC assistance”: the board-local PL
performs binarization, extraction and matching through the production
`detect_page()` seam, with only the documented board-local host refinement
remaining because this RTL does not return a correlation map. No external-PC
compute and no silent CPU fallback are permitted.

Verify candidate ordering, actual matcher-invocation count, matcher results,
final classes/counts, location and annotation geometry, DMA/core wall time,
inline rung C, and exact parity against the `cpu-production` oracle. Emit the
annotation geometry as JSON on the board. Do not draw onto or PNG-encode a
full-page BGR image there; reproduce the annotated output off-board from the
source PDF plus the recorded geometry.

The following prerequisites are complete:

* `pl_backends.py` supplies the explicit production backend seam, covered by
  `test_pl_backends.py`.
* The stripe/histogram Otsu implementation removes the whole-page `int32` blur
  and is bit-exact with `cv2` on all 36 corpus pages.
* The 8 MiB setting bounds the returned blur stripe, not the total process
  peak; do not describe it as an 8 MiB total working set.
* `samples_mv` is byte-identical to `samples` on all 36 pages and removes the
  186,126,336-byte copy.
* With `keep_bgr=False`, the whole RGB Pixmap is rendered once and OpenCV's
  existing RGB→BGR→gray conversion runs in bounded row bands into one full
  gray destination. All 36 pages agree with the original path for BGR, gray,
  BGR-free gray, Otsu threshold, core binary and every tested band height.
* Both application runners now pass `keep_bgr=False`.
* `--geometry-json` plus `--no-annotate` records board output without a
  full-page annotated raster; `--from-geometry` reproduces it off-board. The
  redraw agrees with direct annotation over 46,531,584 rendered sample bytes.

Two cheaper alternatives were measured and rejected:

* Native MuPDF grayscale is 0/36 byte-identical to OpenCV's conversion, with
  differences up to 23 levels. Equal Otsu thresholds on this corpus do not
  make that arithmetic interchangeable.
* Clip-striped rendering tiles exactly in geometry but not in pixels because
  MuPDF antialiases against clip edges. Margins of 16 and 64 rows still differ;
  a larger empirically sufficient margin has no correctness bound.

Regression tests must continue to require both rejected routes to differ, so
neither can silently return without repeating the corpus proof.

For the 9792×6336 RGB page, the whole-page base inside
`render_page(keep_bgr=False)` is 248,168,448 bytes: one RGB Pixmap plus one
gray destination. At the 8 MiB conversion-band setting, the BGR and gray band
temporaries bring the direct array set to approximately 259,331,328 bytes
(247.3 MiB). These are bounds on this function, not the whole process; only
62,042,112 gray bytes remain after it returns. Run that maximum page within
the board's measured `MemAvailable` before closing memory feasibility on
silicon. This gate must cover the whole page pipeline, not only
`render_page()`: rendering, streamed binarization while `page_bin` and
`clean_bin` coexist, PL extraction/matching, geometry emission and safe
teardown must all complete, followed immediately by a known-small page.
Record process RSS, `MemAvailable` and CMA availability at the relevant phase
boundaries so Python/PYNQ and DMA allocations are included rather than hidden
inside the approximately 259 MB function-level array calculation.

### Stage 3 — representative stress page

Run `doc_003`, page 1, through `pl-all`, then immediately run a
known-small page. Verify the practical candidate/trial workload, no timeout or
stale state, exact `cpu-production` parity, inline rung C, correct re-invocation
after the long transaction, and DMA/core wall time consistent with its initial
matcher model: 148,323,642,023 cycles = 1,483.2364 seconds at 100 MHz
(24 minutes 43.236 seconds). Record the ARM refinement time separately.

The production organization has 68 candidates, two `[64, 4]` batches, 2,040
initial matcher trials, a largest patch of 622×300, and an off-board
expectation of 26 host-refinement calls comprising 208 ARM correlations. The
Cortex-A9 cost of those 208 correlations is unmeasured; Stage 3 must time and
report it separately from DMA/core time.

The older 161,607,184,773-cycle mixed figure belongs to the frozen-trace
organization, not the production organization. It adds 176 frozen refinement
records repriced at `pl_side_bank`/B2 (13,283,542,750 cycles) to the common
2,040-trial initial total. It is a conditional diagnostic only: this RTL
returns a scalar raw-map argmax and cannot execute `prefer_local_alignment`.
Do not divide that 176-record price by the production path's 208 correlations,
and do not compare DMA/core wall time with the mixed total.

### Stage 4 — full production-semantics corpus parity

The detector-parity question is resolved. The frozen `cpu` backend is a
regression diagnostic, not the production oracle: it uses different
binarization and patch organization and agrees with production semantics on
only 7/36 pages.

The authoritative oracle is `cpu-production`:

* core-equivalent truncating binarization and Otsu convention;
* `pl_side_bank` patch organization;
* the same matcher trial ordering and row-major tie rule; and
* board-local host refinement.

Run the exact authoritative 35-PDF / 36-page filename set as rung P,
`cpu-production → pl-all`, with inline rung C enabled on the same extracted
patch. The off-board exact fake-fabric run already establishes that 36/36 is
arithmetically reachable; it is a prediction, not silicon evidence.

Do not construct `cpu-production` and `pl-all` concurrently on the 512 MiB
board. **Generate the `cpu-production` records ON THE BOARD**, one page per
process, sequentially, freezing each page's record to disk; then run `pl-all`
against those records in the identical binary environment, with inline rung C,
comparing each checkpoint. The current parity runner has no reference-record
comparison/resume mode and must gain it before the corpus session.

This supersedes the earlier arrangement, which precomputed the oracle
off-board. That would compare the wrong things. Ubuntu's `python3-fitz` links
the distro `libmupdf` with the distro FreeType/HarfBuzz/OpenJPEG; a PyPI
1.19.2 wheel bundles MuPDF's pinned thirdparty tree, and text rasterisation
depends on the FreeType build. An off-board oracle "on 1.19.2" would make rung
P a measurement of MuPDF packaging rather than of PL correctness. (PyPI 1.19.2
also has no cp313 wheel, so the dev venv cannot run it at all.) The x86 1.28
records are kept as labelled diagnostics, not as the oracle. Measure one page
on the board before budgeting the corpus.

Acceptance:

* [ ] The exact authoritative 35 unique PDFs and 36 pages run.
* [ ] Rung P reaches 36/36 page parity: exact final locations/classes and
  absolute score error ≤ 0.005.
* [ ] Inline rung C reports no matcher mismatch on the exact same extracted
  patches.
* [ ] Candidate IDs remain in input order.
* [ ] Every classification, matcher trial and refinement call is accounted
  from this run. Do not reuse or mix the old 808-call CPU and 968-call
  PL-geometry traces.
* [ ] Average, maximum and representative-page wall times are recorded and
  compared with the validated model.

The corpus is a long-running test. The exact initial PL matcher model at
100 MHz is 13,653.0329 seconds = 3.79 hours total. Its worst initial page is
1,483.2364 seconds = 24 minutes 43.236 seconds. The older 396.0 seconds × 36 =
3.96-hour figure conditionally reprices frozen CPU-triggered refinement under
the fabric law; it is neither a production-organization projection nor an
end-to-end wall-time estimate. Actual elapsed time is the initial PL work plus
measured Cortex-A9 refinement and other PS-side work.

For each page, make the timeout budget an explicit sum of the modeled initial
PL time, a separately named ARM-refinement allowance, measured PS pipeline
overhead and a documented safety margin. Use a conservative ARM allowance for
the first stress run; after Stage 3, replace it with the measured Cortex-A9
value plus margin. Do not hide ARM refinement inside an undifferentiated
approximately-27-minute timeout. Therefore the Stage 4 runner must:

* use a per-page deadline set above that page's modeled time with a documented
  margin;
* flush the transcript after every page; and
* atomically checkpoint each completed page so a later hang resumes at the
  first incomplete page without losing earlier evidence.

These are requirements, not descriptions of current behavior: today
`--pl-timeout` is per PL invocation, output may remain buffered, and the JSON
record is written only after the whole run succeeds.

A resumed run must revalidate the same pinned image, variant, oracle settings
and fresh-boot/CMA/counted-clock requirements before appending evidence.

## Performance reporting rules

Use the frozen figures correctly:

* Production initial matching is 303.401 s/page at 125 MHz and 379.251 s/page
  at 100 MHz.
* The 316.797/396-second conditional-refinement figure is not the production
  initial-only estimate.
* Deleting the full-correlation term still leaves 91.651 s/page at 125 MHz and
  114.563 s/page at 100 MHz.
* The 3.753-second front-end term is not the architecture's hard floor.

The RTL has no page-level hardware cycle counter. Record PS-side DMA/core wall
time, compare it with modeled or inferred cycle totals, and do not label those
totals as directly measured hardware cycles.

## Completion decision

Declare **B2/100 PASS** only when:

1. Gate A isolates and passes the combined 100 MHz clock increase.
2. Gate B implements the B2 replacement.
3. The exact immutable image passes Gate C.
4. Fresh-boot CMA/artifact/idle preflight passes.
5. The counted-clock differential accepts 100 MHz and the current Gate 7
   revision passes on silicon.
6. Stage 2 and the Stage 3 stress/re-invocation sequence pass through
   `pl-all` against `cpu-production`, after the maximum-page whole-pipeline
   memory gate passes.
7. Stage 4 reaches 36/36 rung-P parity with inline rung C and complete workload
   accounting.

A lower-frequency or rebuilt-but-not-requalified image may be retained as
diagnostic evidence, but it does not satisfy B2/100.

## Deliverables

* Parameterized sign-off/build scripts and Gate A/B/C reports.
* The exact pinned B2/100 `.bit`, `.hwh` and manifest.
* Timing, hold, utilization, congestion, clock, constraint and DRC reports.
* Fresh-boot/CMA/identity/idle and counted-clock evidence.
* Current Gate 7 re-run transcript.
* Maximum-page whole-pipeline memory evidence, including the simultaneous
  binary images, PL work and the immediate small-page re-invocation.
* Stage 2 `doc_002` geometry JSON through `pl-all` and the corresponding
  off-board-rendered annotated output.
* Stage 3 stress-page and immediate small-page outputs.
* Restartable 36-page rung-P/inline-rung-C parity and performance summary.
* Concise final B2/100 evidence document, changed-file list and remaining
  blockers.

Do not commit unless explicitly instructed.
