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

The pipeline keeps file handling, PDF rendering, text/vector extraction, and
final post-processing in the processing system (PS). The programmable logic
(PL) accelerates the streaming image kernels and per-candidate classification.

```text
PS: ARM Linux / Python                  PL: FPGA fabric

PDF input
  |
  v
page_render_ps      ---- gray image DMA ----> binarize_core
text/vector logic   <--- binary image DDR ---
  |
  v
candidate_gen_ps    ---- candidates DMA ----> patch_extract_core
                                         --> template_match_core
                                         --> class_score_core
                                         --> result buffer
  |
  v
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
  class_score/      HLS score ranking and classification core

sw/
  terminal_counter_endpoint_first.py   CPU baseline and integration target
  tme_driver.py                        PYNQ/PL driver work
  *_terminal_*.py                      batch, sweep, and evaluation utilities
  *_ter/                               small template assets
  old_code/                            archived Python experiments

rtl/
  Reserved for Phase B SystemVerilog implementations

data/, docs/, tb/
  Reserved project folders
```

## Implemented Accelerator Blocks

| Block | Purpose | Notes |
|---|---|---|
| `binarize_core` | Applies a 3x3 Gaussian blur and threshold to a grayscale page image. | Streams pixels row by row and writes a binary page buffer. |
| `patch_extract_core` | Builds candidate-centered search windows from the binary page image. | Matches the patch boundary logic used by the Python pipeline. |
| `template_match` / `tme_top` | Computes template-match scores over a search patch. | Uses a pipelined MAC-based correlation path. |
| `class_score_core` | Ranks template scores and emits a tentative class result. | Applies threshold and score-margin logic. |
| `tme_driver.py` | Coordinates software/accelerator handoff. | Intended for PYNQ buffer allocation, DMA, and register control. |

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

Each HLS block has its own `run_hls.tcl` script. For example:

```powershell
cd hls\binarize
vitis_hls -f run_hls.tcl
```

```powershell
cd hls\template_match
vitis_hls -f run_hls.tcl
```

Some testbench scripts generate raw `.bin` vectors from private drawing data.
Those vectors are intentionally ignored by Git. To reproduce the tests, provide
your own non-confidential PDFs/templates and regenerate the local test vectors.

## Data Policy

This public repository excludes:

- Confidential source drawings and sample PDFs
- Rendered/debug drawing images
- Raw `.bin` image buffers and template-match test vectors
- Vitis/Vivado generated project outputs
- Local virtual environments and Python caches

The ignore rules are kept in `.gitignore` so future commits do not accidentally
include those files.

## Roadmap

- Complete PS/PL integration through PYNQ buffers and DMA.
- Validate HLS cores against CPU baseline behavior.
- Package HLS cores into a Vivado block design.
- Replace selected HLS modules with optimizied SystemVerilog:
  - `template_match_core.sv`
  - `binarize_core.sv`
  - `patch_extract_core.sv`
  - AXI DMA reader/writer blocks
  - AXI-Lite control register block

## Project Goal

This project implements an FPGA-assisted visual feature detection pipeline for cable-assembly engineering drawings using a Zynq PS/PL co-design flow. The Python/ARM processing system handles flexible control-flow tasks such as PDF rendering, text/vector extraction, endpoint candidate generation, post-processing, and PDF annotation. Regular, compute-heavy image-processing stages, including grayscale binarization, patch extraction, and local template matching, are offloaded to programmable logic using Vitis HLS, where they can be implemented as parallel streaming hardware to improve detection throughput. The project demonstrates hardware/software partitioning, accelerator design, memory-mapped control, and system-level validation for an industrial document-analysis workload.
