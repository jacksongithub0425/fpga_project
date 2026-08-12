# FPGA-Assisted Visual Feature Detection Accelerator

This repository contains a Zynq PS/PL co-design for accelerating visual feature
detection in engineering drawing PDFs. The software baseline runs the full
pipeline on the ARM/CPU side, while the FPGA fabric accelerates the repeated
image-processing and template-matching stages.

The project targets Zynq-7020-class boards such as the Arty Z7-020 and PYNQ-Z1.
It is organized as a two-phase implementation:

- Phase A: Vitis HLS cores for fast functional bring-up.
- Phase B: optimizied SystemVerilog for the compute-heavy blocks.

Confidential drawings, generated PDF outputs, raw binary test vectors, and local
build artifacts are intentionally excluded from this public repository.

## System Overview

The pipeline keeps file handling, PDF rendering, text/vector extraction,
classification, and final post-processing in the processing system (PS). The
programmable logic (PL) accelerates the streaming image kernels: binarization,
patch extraction, and template-match scoring. Classification — the
per-candidate score reduction and box construction — is deliberately PS-side
in the MVP (decision 2026-08-11, `docs/pl_interface_contract.md` §10 items
4–5): the PS sequences one matcher invocation per (candidate, template, scale)
trial, keeps a strictly-greater running argmax over a frozen trial order, and
builds the detection box from the patch origin plus the matcher's reported
location.

```text
PS: ARM Linux / Python                  PL: FPGA fabric (three_stage_combined)

PDF input
  |
  v
page_render_ps      ---- gray image MM2S ---> binarize_core
text/vector logic   <--- binary image S2MM --
  |
  v
candidate_gen_ps    ---- candidates MM2S ---> patch_extract_core
                    <--- patch pixels S2MM --
                    <--- metadata S2MM ------
  |
  v
match sequencer     ---- patch MM2S --------> template_match_core (tme_top)
(per trial)         ---- template MM2S ----->
                    <--- score/x/y regs -----
  |
  v
classify_ps: per-kind argmax, box construction
postprocess_ps: heuristics, NMS, annotation, reporting
```

Shared DDR memory is used as the handoff point between Python/PYNQ code and the
FPGA accelerators.

## Current Repository Layout

```text
hls/
  binarize/         HLS grayscale-to-binary image core and testbench
  patch_extract/    HLS patch extraction core
  template_match/   HLS template matching engine
  class_score/      parked — removed from the MVP (2026-08-11)
  integration/      binarize -> extract -> match C/golden harness

sw/
  terminal_counter_endpoint_first.py   CPU baseline and integration target
  tme_driver.py                        PYNQ driver for the combined overlay
  tme_standalone_bringup.py            matcher standalone bring-up (silicon 9/9)
  BOARD_RUNBOOK.md                     ordered board gates
  probe_cma_budget.py                  gate 1: CMA budget (contract 2.2)
  inspect_overlay.py                   gate 2: overlay vs driver cross-check
  board_gate_full_dma.py               gate 3: full-size DMA transfer
  test_*.py                            host-side tests, no board required
  *_ter/                               small template assets
  old_code/                            archived Python experiments

docs/
  pl_interface_contract.md   the binding PS/PL interface contract

vivado/
  three_stage_combined/      overlay reconstruction Tcl + board bundle
```

Phase B SystemVerilog (`rtl/`) has no sources yet; the directory appears when
the first one lands.

## Implemented Accelerator Blocks

| Block | Purpose | Notes |
|---|---|---|
| `binarize_core` | Applies a 3x3 Gaussian blur and threshold to a grayscale page image. | Streams pixels row by row and writes a binary page buffer. |
| `patch_extract_core` | Builds candidate-centered search windows from the binary page image. | Matches the patch boundary logic used by the Python pipeline. |
| `template_match` / `tme_top` | Computes template-match scores over a search patch. | Exact-integer TM_CCOEFF_NORMED; standalone silicon bring-up passed 9/9 (2026-08-07). |
| `class_score_core` | (Parked — removed from the MVP, 2026-08-11.) | Classification and box construction run on the PS; revisit only if benchmarking the completed PS classification shows a bottleneck. |
| `tme_driver.py` | Coordinates software/accelerator handoff. | Per-core register windows against the `three_stage_combined` overlay; explicit CPU/PL backend selection, no silent fallback. |

## Toolchain

Recommended tools:

- Python 3.10+
- NumPy
- OpenCV
- PyMuPDF
- Vitis HLS 2025.2
- Vivado/PYNQ for board integration

Example Python setup:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install numpy opencv-python pymupdf
```

The local virtual environment should not be committed.

## Running HLS Builds

Each HLS block has its own `run_hls.tcl` script. Vitis HLS 2025.2 ships no
standalone `vitis_hls` launcher — runs go through `vitis-run --mode hls`:

```powershell
cd hls\binarize
vitis-run --mode hls -f run_hls.tcl
```

```powershell
cd hls\template_match
vitis-run --mode hls -f run_hls.tcl
```

Some testbench scripts generate raw `.bin` vectors from private drawing data.
Those vectors are intentionally ignored by Git. To reproduce the tests, provide
your own non-confidential PDFs/templates and regenerate the local test vectors.

## Data Policy

This public repository excludes:

- Confidential source drawings and sample PDFs
- Rendered/debug drawing images
- Raw `.bin` image buffers and template-match test vectors
- Vitis/Vivado generated project outputs — with one deliberate exception:
  `vivado/three_stage_combined/board_bundle/` carries the deployable
  `.bit`/`.hwh` pair (SHA-256 recorded in its `BUILD_INFO.txt`) so the
  board results stay reproducible from the repository alone
- Local virtual environments and Python caches

The ignore rules are kept in `.gitignore` so future commits do not accidentally
include those files.

## Roadmap

- ~~Package HLS cores into a Vivado block design~~ — done: the
  `three_stage_combined` overlay (binarize, patch extract, template match,
  five AXI DMAs) is routed with a matching `.bit`/`.hwh` (2026-08-11).
- Board gates, in order (each PASS is a precondition of the next — see
  [sw/BOARD_RUNBOOK.md](sw/BOARD_RUNBOOK.md), which is authoritative):
  1. CMA budget probe (contract §2.2);
  2. overlay introspection (`ip_dict`, `register_map`) against the driver;
  3. the full-size 63,078,400-byte DMA transfer, verified bit-exactly.

  Then per-stage driver bring-up (`binarize_page`, `extract_candidates`,
  `match_template`) with explicit CPU-parity checks.
- Integrate `detect_page()` one PL stage at a time behind explicit detector
  backends (CPU / PL-binarize / PL-extract / PL-all) — no silent
  FPGA-to-CPU fallback during validation.
- Benchmark the completed PS classification; reconsider `class_score_core`
  only if that measurement identifies classification as a bottleneck.
- Replace selected HLS modules with optimized SystemVerilog (Phase B):
  - `template_match_core.sv`
  - `binarize_core.sv`
  - `patch_extract_core.sv`

## Project Goal

This project implements an FPGA-assisted visual feature detection pipeline for cable-assembly engineering drawings using a Zynq PS/PL co-design flow. The Python/ARM processing system handles flexible control-flow tasks such as PDF rendering, text/vector extraction, endpoint candidate generation, post-processing, and PDF annotation. Regular, compute-heavy image-processing stages, including grayscale binarization, patch extraction, and local template matching, are offloaded to programmable logic using Vitis HLS, where they can be implemented as parallel streaming hardware to improve detection throughput. The project demonstrates hardware/software partitioning, accelerator design, memory-mapped control, and system-level validation for an industrial document-analysis workload.
