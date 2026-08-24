# 12 — PyMuPDF protocol v2: **PASS**, 34 gates, 0 failures

The protocol and its ten predictions were committed **before** this ran
(`11_protocol_v2_PREREGISTERED.md`, commit `d3557f2`). Transcript:
`12_protocol_v2_run.txt`. Unprivileged as `xilinx`; nothing installed;
throwaway root removed and its absence proved.

## The ten registered predictions, against what happened

| # | predicted | measured | |
|---|---|---|---|
| 1 | 3 debs, versions and digests as registered | `python3-fitz 1.19.2+ds1-1ubuntu1` 22,953,060 B `988b90ce…`; `libgumbo1 0.10.1+dfsg-2.4` 96,384 B `850192e2…`; `libmujs1 1.1.3-3` 98,992 B `74678dd4…` | ✅ |
| 2 | `ldd` resolves 18/18 | **0 unresolved of 17** | ⚠️ see below |
| 3 | `VersionBind` 1.19.2, `VersionFitz` 1.19.0 | 1.19.2 / 1.19.0 | ✅ |
| 4 | class `samples_mv` False, instance True | False / True | ✅ |
| 5 | the detector imports at all under 1.19 | imports | ✅ |
| 6 | `SAMPLES_MV_PATH == "samples_mv"` | `'samples_mv'` | ✅ |
| 7 | `rendered_shape()` equals the real pixmap | derived `(680, 880, 3)` == actual | ✅ |
| 8 | `extract_words` non-empty | 3 words | ✅ |
| 9 | `extract_horizontal_segments_vector` runs | 5 segments | ✅ |
| 10 | `numpy`/`cv2`/`pynq` resolution unchanged | 1.21.5 / 4.5.4 / 3.1.1, identical paths | ✅ |

**Prediction 2 was stated loosely and is scored as such.** The substance —
zero unresolved — held. "18" counted every line `ldd` prints; the gate counts
lines containing `=>`, and `/lib/ld-linux-armhf.so.3` has none. 17 mapped
libraries plus the interpreter. Nothing about the result changes; the
prediction should have said "zero unresolved" and named its own counting
rule.

The two digests registered as 16-hex prefixes now have their full values,
and both extend the registered prefix.

## What prediction 6 is worth

It is the only gate here that would have **failed before this week's fix**.
`HAVE_SAMPLES_MV_ON_CLASS = False` is printed in the transcript — the
class-level question really does answer False on this runtime — and the
render still took the zero-copy path, because the guard now asks the pixmap.
Under the old import-time class probe the same run would have taken
`pix.samples`: a `bytes` copy of the whole pixmap, **186,126,336 B** on a
production page, against roughly 290 MiB of userspace.

That is a gate rather than a note precisely because the failure mode is
silent. Nothing raises; the page just costs 186 MB more, and the OOM gets
filed against the pipeline.

## Attestation

Every helper's digest was read off the board before anything ran, and the
committed `.as_run` files match:

```
0396f873692f8a8aff600ffbf613029c9f16283278d6282c7b6f66e5c18e6590  12_fitz_v2.sh.as_run
a6b5c4a27520d93826615ecab9f1bb05413052df470d0d8aa49850206585dc8a  12_fitz_v2_detector.py.as_run
7815fe79b33077ff2dece220db04887a014e28752dc83148c0f359f0f4d5cce0  12_fitz_probe_env.py.as_run
658cabd87f7cf35715ed513b5507e8487ea08080a5ab57159c33ea85fd688967  12_fitz_probe_func.py.as_run
```

This closes the asymmetry `README.md` records for the v1 probes, where two
helpers ran without a read-back.

**And the module under test is the committed one.** The board read back

```
36a1d7d8c07cd3828e515fa358d8f8e1c4d5ea4dcde939e0d4145066d2f107d9  terminal_counter_endpoint_first.py
```

which is byte-identical to `git cat-file blob 3702b2b:sw/terminal_counter_endpoint_first.py`.
The result is tied to a commit, not to a working copy.

## What this establishes

The board runs **this project's renderer** under a locally staged PyMuPDF
1.19.2 / MuPDF 1.19.0 — unprivileged, nothing installed, both injection
variables process-scoped — and takes the zero-copy pixmap path. `render_page`,
`extract_words`, `extract_horizontal_segments_vector` and `rendered_shape`
all function, and `get_drawings()` returns real content rather than the empty
list a broken one would also return (the segment extractor swallows its
exceptions, so "ran" and "worked" are checked separately).

## What it does not

Nothing about Stage 2, and **nothing about agreement with MuPDF 1.29**. The
page was synthetic, built by the probe so no corpus page had to be placed on
the board for a feasibility check. Its 5 segments, 3 words and pixel bytes
are not compared to anything and must not be.

Two things a reader should carry forward:

* the rasteriser is **1.19.0**, two minor versions below the 1.29.0 every
  parity result in `logs/b2prod_20260822/` was produced against;
* the injection is a *staging* mechanism, not a deployment. Each run
  downloads 23.1 MB and unpacks ~36 MB. Whether Stage 2 stages per run,
  keeps a persistent root, or installs properly is a decision, and it has
  not been taken here.

Requalifying 1.19 across the 36-page corpus is next, and this does not
advance it.
