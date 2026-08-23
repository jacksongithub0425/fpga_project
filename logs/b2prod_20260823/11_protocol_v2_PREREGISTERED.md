# 11 — PyMuPDF protocol v2, registered BEFORE the run

Written and committed before the board session it describes. That is the
whole point of the file: a protocol agreed after seeing its result is not a
protocol, and a prediction that can be edited once the answer is in is not a
prediction.

## Why there is a v2

v1 (`09_step2_fitz_1192.md`) failed and **could not have succeeded**. It
specified `apt download` + `dpkg-deb -x` + injection **through PYTHONPATH
only**; `_fitz.cpython-310-arm-linux-gnueabihf.so` links `libgumbo.so.1` and
`libmujs.so.1`, neither is on the board, and the dynamic loader does not
consult `PYTHONPATH`. No arrangement of v1 resolves a shared library.

Everything else about the fit was right — armhf, `EM_ARM`, ABI tag matching
the PYNQ interpreter's own `EXT_SUFFIX`, 16 of 18 libraries resolved
system-wide, and `numpy`/`cv2`/`pynq` resolution unchanged under the
injection.

## v2

v1, plus the two missing libraries unpacked into the same throwaway root and
reached with `LD_LIBRARY_PATH`. Both variables are **process-scoped**;
neither outlives the run, and nothing is installed.

Retained from v1 without change: unprivileged as `xilinx`; `apt download`
and `dpkg-deb -x` only, never `apt install` or `dpkg -i`; a throwaway root
under `/home/xilinx/b2prod_fitz_probe.*`; the root removed at the end and
its absence proved; `dpkg -s` confirming the packages are still not
installed.

### Two things v2 does that the v1 supplementary run did not

**1. The payload is digest-attested from the board.** v1 left two `.as_run`
files uploaded and run in a single command with no `sha256sum` read-back, so
what is committed for them is the local file the uploader sent rather than a
board-confirmed digest (`README.md`). v2 reads every helper's digest back
off the board before running anything, and the run prints them.

**2. It exercises THIS PROJECT'S renderer, not raw `fitz`.** v1 called
`get_pixmap` / `get_text("words")` / `get_drawings()` directly. That answers
"the library works" and not "our code works", and the gap between those two
is where a rebase actually bites. v2 imports
`terminal_counter_endpoint_first` under the injected runtime and calls
`render_page`, `extract_words`, `extract_horizontal_segments_vector` and
`rendered_shape`.

The API surface this puts under test, taken from the module itself:
`fitz.open`, `fitz.Matrix`, `fitz.Point`, `fitz.Rect`, `Page.get_pixmap`,
`Page.get_text`, `Page.get_drawings`, `Page.draw_rect`, `Page.insert_text`,
`Page.insert_textbox`, `Pixmap.samples`, `Pixmap.samples_mv`,
`Pixmap.width/height/n`. (`fitz.csGRAY` appears only in a docstring, so it
is not an import-time dependency.)

## Pass criteria — fail closed, every gate named

A gate that does not pass sets the exit status; the run names the failures.

| gate | criterion |
|---|---|
| `unprivileged` | uid != 0, `whoami` == `xilinx` |
| `three_debs_downloaded` | exactly 3 `.deb`, versions and sha256 recorded |
| `deb_arch_usable` | all three `armhf` |
| `all_extracted` | `dpkg-deb -x` succeeds for all three |
| `ldd_complete` | **0** unresolved of 18 |
| `abi_tag_matches` | `_fitz.cpython-310-arm-linux-gnueabihf.so` vs the interpreter's `EXT_SUFFIX` |
| `fitz_imports` / `bind_is_1_19_2` | import ok, `VersionBind` 1.19.2 |
| `fitz_from_throwaway_root` | `fitz.__file__` under the probe root |
| `samples_mv_on_instance` | `hasattr(pix, "samples_mv")` is **True** |
| `detector_imports` | `import terminal_counter_endpoint_first` succeeds |
| `render_page_works` | returns arrays of the predicted shape |
| `render_took_the_zero_copy_path` | `SAMPLES_MV_PATH == "samples_mv"` |
| `rendered_shape_agrees` | the derivation equals the actual pixmap |
| `words_and_segments_run` | both return without raising |
| `*_resolution_unchanged` | `numpy`, `cv2`, `pynq` identical to the clean baseline |
| `root_removed`, `no_probe_roots_left`, `fitz_absent_after`, `nothing_installed_*` | teardown |

## Predictions, registered now

1. Three debs: `python3-fitz 1.19.2+ds1-1ubuntu1` (sha256
   `988b90cee5d8feadbccc14f66fad078f33cd639340f6549475b718f8d1eecf39`,
   22,953,060 B), `libgumbo1 0.10.1+dfsg-2.4` (96,384 B, sha256 begins
   `850192e2c19dbbf3`), `libmujs1 1.1.3-3` (98,992 B, sha256 begins
   `74678dd4d4a8a8c4`).
2. `ldd` resolves 18/18.
3. `VersionBind` = `1.19.2`, `VersionFitz` = **`1.19.0`**.
4. `hasattr(fitz.Pixmap, "samples_mv")` is **False** and
   `hasattr(pix, "samples_mv")` is **True**. This is now an expectation
   rather than a discovery, and it is registered so that a change in it is
   visible.
5. **`terminal_counter_endpoint_first` imports cleanly.** Genuinely
   uncertain: the module was written against 1.28.0 and nothing has ever
   imported it under 1.19.
6. `render_page` returns and `SAMPLES_MV_PATH == "samples_mv"`. This is the
   186 MB/page fix under test on the runtime that motivated it. Under the
   pre-fix code this gate would have FAILED, which is why it is a gate.
7. `rendered_shape()` equals the real pixmap shape. Geometry, not
   rasterisation, so it should be version-independent — but it is asserted
   rather than assumed.
8. `extract_words` returns a non-empty list on a page with text.
9. `extract_horizontal_segments_vector` returns **without raising**.
10. `numpy` 1.21.5, `cv2` 4.5.4 and `pynq` 3.1.1 resolve to exactly the
    paths and versions they resolve to with no injection.

**Registered as NOT predicted, and deliberately not tested here:** that any
of these produce the SAME numbers as MuPDF 1.29 did. Segment counts, word
boxes and pixel bytes may all differ, and a difference is not a failure of
this gate. That is the requalification, it needs the corpus, and it is a
separate exercise.

## What a v2 PASS will and will not mean

**Will:** the board can run this project's renderer under a locally staged
PyMuPDF 1.19.2 / MuPDF 1.19.0, unprivileged, with nothing installed, and the
zero-copy pixmap path is taken.

**Will not:** anything about Stage 2, and nothing about the semantic rebase.
The page rendered is synthetic, built by the probe so that no corpus page
has to be placed on the board for a feasibility check. Requalifying 1.19
across the 36-page corpus is the next step and this does not advance it.
