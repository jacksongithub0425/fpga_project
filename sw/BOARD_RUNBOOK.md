# Board runbook — three_stage_combined bring-up gates

Ordered gates for the combined three-core overlay. Run them **in this order**;
each one's PASS is a precondition of the next. All run on the PYNQ board as
root (`sudo`), from one directory containing:

```
three_stage_combined.bit      # from three_stage_combined/postextract_board_bundle_20260811_003143/
three_stage_combined.hwh      # same bundle — MUST sit next to the .bit, same basename
probe_cma_budget.py
inspect_overlay.py
board_gate_full_dma.py
tme_driver.py
tme_standalone_bringup.py
binarize_dma_checks.py
```

## Gate 1 — CMA budget (contract §2.2, §10 item 3)

```
sudo python3 probe_cma_budget.py --overlay three_stage_combined.bit
```

Proves the two **separately contiguous** ~60.2 MiB image buffers can be
allocated with the overlay resident. `--overlay` is not optional in spirit:
probing a pristine pool and then allocating in a different order in
production is how this gate passes on the bench and fails in the field.

- PASS → record the CmaFree numbers it prints and close §10 item 3.
- FAIL (exit 1) → **tiling becomes a platform requirement and §2 changes.**
  Stop here; nothing downstream is worth running.

A PASS is evidence for this boot, not permanent proof — re-run after reboots.

## Gate 2 — overlay introspection (work item 4, board half)

```
sudo python3 inspect_overlay.py --overlay three_stage_combined.bit
```

Prints `ip_dict`, `hierarchy_dict`, the measured PL clock, every core's
`register_map` and every DMA's channel configuration, and checks them against
what `tme_driver.py` resolves by name. Capture the full output into the
repo (`docs/` or the bundle directory) — it is the board-side record of the
HWH inspection.

- PASS → the driver's names are right; proceed.
- FAIL → the overlay and driver disagree; fix `_CORE_NAMES`/`_DMA_NAMES` or
  the block design **before** any driver call. Do not "just try" gate 3.

## Gate 3 — the real 63,078,400-byte DMA transfer

```
sudo python3 board_gate_full_dma.py --overlay three_stage_combined.bit
```

Moves a full 9856 × 6400 procedural page through `binarize_core_0` — one
63,078,400-byte MM2S and one 63,078,400-byte S2MM, each a **single** transfer
(the binarize DMA's 26-bit length register, max 67,108,863 B, is what makes
that legal; this is its first full-size exercise) — and compares the output
**bit-exactly** against the truncating-Gaussian CPU oracle.

This is deliberately a different gate from gate 1: allocation success says
nothing about a transfer completing, and a transfer completing says nothing
about the data being right. A PASS here also validates
`PLPipeline.binarize_page()` — the first of the three per-stage driver
validations.

Expect the CPU-side golden to take a while on the Zynq PS (numpy over 63 M
pixels); the PL round trip itself should be seconds.

## After the gates — per-stage driver validation (work items 6–7)

In order, each with explicit CPU parity checks, no silent fallback:

1. `binarize_page()` — already covered by gate 3.
2. `extract_candidates()` — feed descriptors from
   `hls/patch_extract/tb_patch_extract_cases_csim.txt` (regenerate with
   `patch_extract_generate_golden.py`) against its golden patches; the §6.2
   record is authoritative for patch geometry, the §4.5 formula only a
   cross-check.
3. `match_template()` / `match_candidate()` — re-run the 9-case `hw` manifest
   from `tme_standalone_bringup.py` through the combined overlay's
   `tme_top_0` (`axi_dma_patch`/`axi_dma_templ`), then per-kind argmax parity
   against `classify_endpoint` on real candidates. The argmax is strictly
   greater over the frozen trial order — ties keep the first trial, same as
   the CPU baseline.

Only after all three: connect `detect_page()` one stage at a time behind
explicit backends (CPU / PL-binarize / PL-extract / PL-all), then benchmark
PS classification before reconsidering `class_score_core` (contract §10
items 4–5).
