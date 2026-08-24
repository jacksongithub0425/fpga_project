# 09 — Step 2: can PyMuPDF 1.19.2 run on this board, installing nothing?

2026-08-23, same board, same boot family as gates 01–04. Nothing was
installed, nothing was left behind, and the fabric was not touched.

## Result, stated as narrowly as it should be

**The agreed protocol FAILED.** Step 2 was specified as: `apt download`,
`dpkg-deb -x` into a throwaway `/home/xilinx/b2prod_fitz_probe.*` root, and
injection **through PYTHONPATH only**. Under exactly that protocol the
package unpacks and the import fails:

```
GATE ldd_complete: FAIL 2 unresolved
    libgumbo.so.1 => not found
    libmujs.so.1  => not found
GATE fitz_imports_injected: FAIL
    ImportError: libgumbo.so.1: cannot open shared object file
```

`PYTHONPATH` resolves Python modules. It does not resolve **shared
libraries**, and no arrangement of it ever will — so this is not a protocol
that needed retrying with a different path. Transcript:
`09_step2_protocol_probe.txt` (six gates FAIL, thirteen PASS).

A supplementary run — **outside the agreed protocol, and not a Step 2
pass** — establishes what the failure does and does not mean. Transcript:
`09_step2_supplementary.txt`.

## What passed, under the protocol, before it failed

These are real results and they narrow the problem usefully:

| gate | result |
|---|---|
| `unprivileged` / `running_as_xilinx` | PASS, uid 1000 |
| `fitz_absent_before` | PASS — `ModuleNotFoundError` |
| `host_is_armv7l`, `host_is_cpython` | PASS — armv7l, CPython 3.10.4 |
| `deb_arch_usable`, `deb_is_1_19_2` | PASS — `armhf`, `1.19.2+ds1-1ubuntu1` |
| `abi_tag_matches` | PASS — `_fitz.cpython-310-arm-linux-gnueabihf.so` vs interpreter `.cpython-310-arm-linux-gnueabihf.so` |
| `elf_is_arm` | PASS — `e_machine` 40 = `EM_ARM`, read out of the ELF header |
| `ldd_complete` | **FAIL — 2 of 18 unresolved** |
| `numpy` / `cv2` / `pynq` `_resolution_unchanged` | PASS — byte-identical paths and versions with and without the injection |
| `root_removed`, `no_probe_roots_left`, `fitz_absent_after`, `nothing_installed` | PASS |

So: **the architecture and ABI are right.** The blocker is two absent
runtime libraries, not a 32-bit/64-bit or interpreter-version problem.

```
deb      python3-fitz_1.19.2+ds1-1ubuntu1_armhf.deb   22,953,060 B
sha256   988b90cee5d8feadbccc14f66fad078f33cd639340f6549475b718f8d1eecf39
so       _fitz.cpython-310-arm-linux-gnueabihf.so     35,891,796 B
```

`libgumbo1` (HTML5 parser, for `Story`/HTML input) and `libmujs1` (the
JavaScript engine MuPDF embeds) are in the package's `Depends` and are on
neither `ldconfig -p` nor `dpkg -l`.

## The supplementary run, and exactly what it is worth

Adding the two dependency `.deb`s and one more **process-scoped** variable,
`LD_LIBRARY_PATH`, resolves all 18 libraries and 1.19.2 runs:

```
GATE ldd_complete_with_libdir: PASS 0 unresolved
fitz            1.19.2 / mupdf 1.19.0
pixmap          800x600 n=3 stride=2400
samples_mv      1440000 bytes, ndarray (600, 800, 3), zero-copy=True
words           3, first=(20.0, 28.17, 75.62, 43.29, 'TERMINAL')
drawings        3 dicts, 3 items, kinds=l,re
FUNC OK
```

`libgumbo1` is 96,384 B and `libmujs1` is 98,992 B — 195 KB between them.
`numpy`, `cv2` and `pynq` still resolve to exactly the paths and versions
they resolved to clean, and afterwards nothing is installed and no probe
root remains.

**This is NOT a Step 2 pass.** It breaks the stated injection rule. What it
buys is that the FAIL above is no longer ambiguous between "two more debs
away" and "cannot work on this board": it is the first. That distinction
changes the plan and nothing else.

**And it is not a Stage 2 pass or a semantic-rebase pass either.** The page
rendered is a synthetic one this probe built, chosen so that no corpus page
had to be put on the board for a feasibility check. It proves the three APIs
`detect_page()` needs as *inputs* — `get_pixmap`, `get_text("words")`,
`get_drawings()` — function. It proves nothing about whether MuPDF **1.19.0**
renders a corpus page the way **1.29.0** did, and every renderer-parity
result in `logs/b2prod_20260822/` was produced against 1.29.

## A finding that changes a number, not just a plan

The supplementary run reported `samples_mv=False` from the environment probe
while the functional test used `pix.samples_mv` successfully. Chased down in
`08_samples_mv.txt`:

```
hasattr(fitz.Pixmap, 'samples_mv')   False
hasattr(pix,         'samples_mv')   True
'samples_mv' in dir(fitz.Pixmap)     False
'samples_mv' in pix.__dict__         True
```

PyMuPDF 1.19.2 assigns `samples_mv` in `Pixmap.__init__`. It is an
**instance** attribute; the class does not have it. The detector decided the
render path on the class, once, at import:

```python
HAVE_SAMPLES_MV = hasattr(fitz.Pixmap, "samples_mv")     # False on 1.19.2
buf = pix.samples_mv if HAVE_SAMPLES_MV else pix.samples
```

`pix.samples` builds a `bytes` **copy** of the whole pixmap —
**186,126,336 B** on a production page, for a buffer that is read once, on a
board with roughly 290 MiB of userspace. On 1.19.2 that copy would have been
taken on every page, and the resulting OOM would have been filed against the
pipeline rather than against a `hasattr` asked of the wrong object.

Fixed: the guard is now asked of the pixmap, and the record carries the path
actually taken (`samples_mv_path`) beside what the class-level question would
have said (`samples_mv_on_class`). Their disagreement is now itself the
signal that the runtime was rebased. `test_the_render_path_is_chosen_on_the_
pixmap_not_on_the_class` models the 1.19.2 shape and raises if the copying
path is taken on a build that supports the other one.

The same wrong question was in `mem_sampler.header()`, which would have
recorded a zero-copy run as a copying one in the very file written to account
for the memory. It now makes a 1×1 pixmap and asks that.

## What Step 2 leaves open

1. **The protocol as agreed cannot pass**, and the reason is structural.
   Whether to accept the two extra `.deb`s plus `LD_LIBRARY_PATH`, or to
   install the three packages properly, is a decision, not a measurement.
2. **MuPDF 1.19.0, not 1.19.2.** The binding is 1.19.2; the rasteriser it
   carries reports `VersionFitz = 1.19.0`. `05_board_environment.md` named
   `mupdf-tools 1.19.0` and this confirms the binding matches it.
3. **Nothing about parity.** The oracle must be regenerated **on the board**
   under this runtime before any Stage 2 comparison means anything — that is
   the decision already recorded in the MuPDF 1.19 rebase note, and this
   probe does not advance or retire it.

## Reproducing

`09_step2_fitz_probe.sh.as_run` is the protocol probe exactly as run, with
`09_step2_fitz_probe_env.py.as_run` and `09_step2_fitz_probe_func.py.as_run`
as its helpers; `09_step2_fitz_probe2.sh.as_run` is the supplementary one.
All three fail closed and all three remove their root and prove it gone.

The `.as_run` files are the bytes that were on the board, not copies made
afterwards: these digests were read back **from the board** with
`sha256sum` after upload and before any run, and the committed files match
them. (`.gitattributes` marks them `-text` so a checkout does not.)

```
aa248372d210bd8d77833994812f187c7a588f45f00b9f0dce3aa85cd99f92bc  09_step2_fitz_probe.sh.as_run
7815fe79b33077ff2dece220db04887a014e28752dc83148c0f359f0f4d5cce0  09_step2_fitz_probe_env.py.as_run
658cabd87f7cf35715ed513b5507e8487ea08080a5ab57159c33ea85fd688967  09_step2_fitz_probe_func.py.as_run
cf0806cf2b9ca6129707a9908bb191bf6bbb737cb498d522dc9049b6517e2f41  09_step2_fitz_probe2.sh.as_run
522cf788b298a8ea2d1739a0064edd66798a96a633b03bbf982f13c0b404b484  07_pynq_alias_probe.py.as_run
```

**Two are not covered by that**, and saying so is cheaper than implying
otherwise: `08_samples_mv_probe.py.as_run` and `08_samples_mv_run.sh.as_run`
were uploaded and run in a single command without a `sha256sum` read-back,
so what is committed is the local file that `board.py put` sent, not a
board-confirmed digest. The board copies are gone, so this cannot be
repaired after the fact — only re-run.

Afterwards the board was returned to the state it was found in: probe
helpers deleted from `/home/xilinx/jupyter_notebooks`, no
`b2prod_fitz_probe.*` root, `import fitz` failing, and `dpkg -s
python3-fitz` still reporting the package not installed. No overlay was
loaded and no DMA object was constructed at any point.
