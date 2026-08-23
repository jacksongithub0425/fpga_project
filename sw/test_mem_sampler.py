#!/usr/bin/env python3
"""Tests for the checkpoint memory sampler and its hooks in the detector.

    python test_mem_sampler.py        # from sw/, with the HLS venv python
    pytest test_mem_sampler.py

WHAT NEEDS PROVING, AND WHY EACH ONE IS HERE
--------------------------------------------
The sampler is an instrument that will be quoted as evidence about whether a
production page fits in 290 MiB.  Three properties carry that weight:

* **it does not perturb what it measures.**  A sampler that keeps a
  reference to the grey page adds 62 MB to its own reading, and the frozen
  `detect_page(backend=None)` path must produce the same detections with the
  observer attached as without.  `test_sampler_keeps_no_reference` and
  `test_observer_does_not_change_detections`.
* **the record survives the process.**  The run this exists for is the one
  the OOM killer ends: the last record must already be on disk when `mark()`
  returns, and a half-written tail must not take the good records with it.
  `test_record_is_on_disk_before_mark_returns`, `test_truncated_tail`.
* **it counts the allocation, not the window.**  The extractor's patch
  records are small slices of full-bound 251,740 B copies; `patch.nbytes`
  under-reports the page's retention several-fold.
  `test_describe_arrays_counts_backing_not_view`,
  `test_retained_bytes_reports_backing_not_view`.

The verdict tests exist because HOLD is the interesting one: a pipeline that
"fits" by swapping has not fitted, and the board has 511 MiB of swap to hide
in.
"""

from __future__ import annotations

import gc
import json
import mmap
import sys
import tempfile
import weakref
from pathlib import Path

import numpy as np

import corpus_labels as CL
import mem_sampler as MS
import pl_backends as B
import terminal_counter_endpoint_first as det

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent.parent / "sample"

#: Stage 2's page, named by LABEL so no source file carries a drawing
#: filename.  `resolve()` returns None on a machine without the corpus --
#: the normal state of a clone -- and the placeholder keeps this a Path,
#: so the `.exists()` skips below read exactly as they always did.
STAGE2_PDF = CL.resolve("doc_002", SAMPLES) or (SAMPLES / "doc_002.absent")

#: Small enough to run a real page in a unit test.  The MEMORY numbers at
#: this zoom mean nothing -- the gate runs at zoom 4 on the board -- but the
#: wiring, ordering and non-perturbation results are zoom-independent.
TEST_ZOOM = 1.0


class SkipTest(Exception):
    pass


_TEMPLATES = None


def templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        _TEMPLATES = det.build_side_templates(
            det.load_template_bank(str(HERE / "male_ter" / "male_left.png")),
            det.load_template_bank(str(HERE / "male_ter" / "male_right.png")),
            det.load_template_bank(str(HERE / "female_ter" / "female_left.png")),
            det.load_template_bank(str(HERE / "female_ter" / "female_right.png")),
            det.load_template_bank(str(HERE / "ferrule_ter" / "ferrule_left.png")),
            det.load_template_bank(str(HERE / "ferrule_ter" / "ferrule_right.png")),
        )
    return _TEMPLATES


def require_pdf():
    if not STAGE2_PDF.exists():
        raise SkipTest(f"{STAGE2_PDF} not present")


def sampler(tmp, **kw) -> MS.MemorySampler:
    # fsync off in the tests: durability against a PROCESS kill is what the
    # tests check, and that only needs the flush.  fsync is on by default in
    # the real runner, where the threat is a board reset.
    kw.setdefault("fsync", False)
    return MS.MemorySampler(Path(tmp) / "mem.jsonl", **kw)


# ---------------------------------------------------------------------------
# The checkpoint set
# ---------------------------------------------------------------------------


def test_checkpoints_are_specified_plus_added():
    """No checkpoint appears from nowhere, and none of the six went missing.

    The specified six were the gate's definition; the three additions are
    argued for in the module docstring.  Folding them together would make it
    impossible to see later which set a reviewer had agreed to.
    """
    assert set(MS.CHECKPOINTS) == set(MS.SPECIFIED_CHECKPOINTS) | set(
        MS.ADDED_CHECKPOINTS), MS.CHECKPOINTS
    assert not (set(MS.SPECIFIED_CHECKPOINTS) & set(MS.ADDED_CHECKPOINTS))
    assert len(MS.CHECKPOINTS) == len(set(MS.CHECKPOINTS))


def test_specified_checkpoints_keep_their_order():
    """The six still appear in the order they were specified in."""
    pos = [MS.CHECKPOINTS.index(c) for c in MS.SPECIFIED_CHECKPOINTS]
    assert pos == sorted(pos), pos


def test_unknown_checkpoint_raises():
    """A typo must not become a silently missing checkpoint.

    A dropped checkpoint reads as INCOMPLETE downstream, and INCOMPLETE
    reads as an OOM kill -- so a typo would manufacture the exact finding
    this instrument exists to detect.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        try:
            s.mark("render_complet")
        except ValueError as exc:
            assert "unknown checkpoint" in str(exc), exc
            return
        finally:
            s.close()
    raise AssertionError("a misspelled checkpoint was accepted")


# ---------------------------------------------------------------------------
# /proc parsing
# ---------------------------------------------------------------------------


def test_parse_kb_table():
    text = ("MemTotal:         503716 kB\n"
            "MemFree:           12345 kB\n"
            "MemAvailable:     298000 kB\n"
            "SwapTotal:        523260 kB\n"
            "SwapFree:         523260 kB\n"
            "CmaTotal:         229376 kB\n"
            "CmaFree:           20480 kB\n"
            "Hugepagesize:       2048 kB\n")
    got = MS._parse_kb_table(text, MS._MEMINFO_FIELDS)
    assert got["MemTotal_kB"] == 503716, got
    assert got["CmaFree_kB"] == 20480, got
    assert "Hugepagesize_kB" not in got, got


def test_parse_kb_table_skips_non_numeric():
    """`VmFlags:` and friends share the format but carry no number."""
    text = "VmRSS:\t   61440 kB\nVmFlags: rd ex mr mw me\nVmSwap:\t 0 kB\n"
    got = MS._parse_kb_table(text, ("VmRSS", "VmSwap", "VmFlags"))
    assert got == {"VmRSS_kB": 61440, "VmSwap_kB": 0}, got


def test_sample_memory_is_tagged_with_its_source():
    """Every reading says which accounting system produced it.

    Windows working-set numbers are real numbers from a different system;
    the tag is what stops one being quoted as a board result.
    """
    mem = MS.sample_memory()
    assert mem["source"] in ("proc", "psapi", "none"), mem
    if mem["source"] == "proc":
        assert "VmHWM_kB" in mem and "VmSwap_kB" in mem, mem
    elif mem["source"] == "psapi":
        assert "VmHWM_kB" in mem, mem
        # No honest per-process resident-swap counter exists here, so there
        # must not be one pretending to be one.
        assert "VmSwap_kB" not in mem, mem


# ---------------------------------------------------------------------------
# Arrays: aliasing, and backing vs window
# ---------------------------------------------------------------------------


def test_describe_arrays_groups_aliases():
    """A view and its base are one allocation, and are reported as one."""
    base = np.zeros((100, 100), dtype=np.uint8)
    view = base[10:20, :]
    other = np.zeros((50, 50), dtype=np.uint8)
    d = MS.describe_arrays({"base": base, "view": view, "other": other,
                            "absent": None})
    assert d["names"]["absent"] is None, d
    assert d["names"]["base"]["group"] == d["names"]["view"]["group"], d
    assert d["names"]["other"]["group"] != d["names"]["base"]["group"], d
    assert d["alias_groups"] == [["base", "view"]], d["alias_groups"]
    assert d["distinct_bytes"] == base.nbytes + other.nbytes, d
    assert d["view_bytes"] == base.nbytes + view.nbytes + other.nbytes, d


def test_describe_arrays_counts_backing_not_view():
    """The extractor's retention shape: a small slice of a big allocation.

    `rec["patch"]` is `raw_patches[i][:w*h].reshape(h, w)` over a full-bound
    251,740 B copy of the receive buffer.  Reporting `patch.nbytes` would
    say 60 kB where 251,740 B is held.
    """
    full = np.zeros(251740, dtype=np.uint8)          # _MAX_PATCH_BYTES
    patch = full[:622 * 96].reshape(96, 622)
    d = MS.describe_arrays({"patch": patch})
    assert d["view_bytes"] == 622 * 96, d
    assert d["distinct_bytes"] == 251740, d
    assert d["names"]["patch"]["group_bytes"] == 251740, d
    assert d["names"]["patch"]["owns_data"] is False, d


def test_distinct_backing_bytes_counts_each_allocation_once():
    full = np.zeros(1000, dtype=np.uint8)
    a, b = full[:10], full[900:]
    lone = np.zeros(7, dtype=np.uint8)
    assert MS.distinct_backing_bytes([a, b]) == 1000
    assert MS.distinct_backing_bytes([a, b, lone, None]) == 1007
    assert MS.distinct_backing_bytes([]) == 0


def test_describe_arrays_survives_a_non_array():
    """A buffer-backed array whose root is not an ndarray still measures."""
    raw = bytearray(4096)
    arr = np.frombuffer(raw, dtype=np.uint8)
    d = MS.describe_arrays({"arr": arr})
    assert d["distinct_bytes"] == 4096, d
    assert d["distinct_unknown"] == 0, d


def test_sampler_keeps_no_reference():
    """THE instrument-perturbation test.

    If `mark()` retains any of the arrays it is handed, the sampler adds its
    own subject to the measurement -- 62 MB for the grey page, 186 MB for
    BGR.  A weakref that outlives the mark is the only direct way to say it
    does not.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        arr = np.zeros((64, 64), dtype=np.uint8)
        ref = weakref.ref(arr)
        s.mark("render_complete", arrays={"gray": arr})
        s.close()
        del arr
        gc.collect()
        assert ref() is None, ("the sampler is still holding the array it "
                               "was asked to measure")


# ---------------------------------------------------------------------------
# Per-phase peaks
# ---------------------------------------------------------------------------


def test_peak_window_defaults_to_run():
    """Off by default: resetting VmHWM changes what the column MEANS."""
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        s.mark("pipeline_ready")
        s.close()
        recs = MS.load(s.path)
        assert all(r.get("peak_window") == "run" for r in recs
                   if r.get("record") != "trailer"), recs
        assert MS.summarise(s.path)["peak_is_per_phase"] is False


def test_peak_window_is_honest_when_the_reset_is_unavailable():
    """`/proc/self/clear_refs` is gated on CONFIG_PROC_PAGE_MONITOR.

    Asking for per-phase peaks and not getting them must not leave the
    records claiming per-phase.  This is the fail-visible half: on a box
    with no /proc the request is made and the records still say "run".
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp, per_phase_peak=True)
        s.header()
        s.mark("pipeline_ready")
        s.mark("render_complete")
        s.close()
        recs = MS.load(s.path)
        head = recs[0]
        assert head["per_phase_peak_requested"] is True, head
        windows = {r.get("peak_window") for r in recs
                   if r.get("record") == "checkpoint"}
        available = MS.reset_peak_rss()
        if available:
            assert windows <= {"run", "since_previous_checkpoint"}, windows
            assert "since_previous_checkpoint" in windows, windows
        else:
            assert windows == {"run"}, windows
            assert MS.summarise(s.path)["peak_is_per_phase"] is False


def test_reset_peak_rss_reports_rather_than_raises():
    """It returns a bool on every platform; it never takes the run down."""
    assert MS.reset_peak_rss() in (True, False)


def _synthetic(path, windows, source="proc"):
    """A sampler file with chosen `peak_window` values, written by hand.

    The reset needs `/proc/self/clear_refs`, so on a dev box the per-phase
    case cannot be produced by running the sampler.  The summariser's
    reading of the column is a separate question from whether the kernel
    supports the reset, and this is what isolates it.
    """
    mem = {"source": source, "VmRSS_kB": 1000, "VmHWM_kB": 2000,
           "VmSwap_kB": 0}
    sysinfo = {"SwapTotal_kB": 0, "SwapFree_kB": 0}
    rows = [{"record": "header", "seq": 0, "peak_window": windows[0],
             "mem": mem, "sys": sysinfo, "env": {}}]
    for i, (cp, win) in enumerate(zip(MS.CHECKPOINTS, windows[1:])):
        rows.append({"record": "checkpoint", "seq": i + 1, "checkpoint": cp,
                     "page": "one.pdf#p1", "peak_window": win,
                     "mem": mem, "sys": sysinfo,
                     "counts": ({"teardown_status": 0}
                                if cp == "teardown_complete" else None)})
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def test_peak_is_per_phase_ignores_the_header_window():
    """The flag was unreachable, and the report said so on every run.

    `header()` writes its record and THEN takes the first reset, so the
    header's window is "run" even when every reset succeeded.  Including it
    in the set made `peak_is_per_phase` False always -- so `_report`
    printed "running peak since process start" over a column that was
    already per-phase, which is the opposite of what the reader needs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        per_phase = _synthetic(
            Path(tmp) / "phase.jsonl",
            ["run"] + ["since_previous_checkpoint"] * len(MS.CHECKPOINTS))
        got = MS.summarise(per_phase)
        assert got["peak_is_per_phase"] is True, got
        assert got["peak_windows"] == ["run", "since_previous_checkpoint"]
        assert got["checkpoint_peak_windows"] == ["since_previous_checkpoint"]
        assert got["verdict"] == "PASS", got

        # And it must still be False when a reset genuinely failed part way:
        # one "run" among the checkpoints is a phase whose column means the
        # other thing, and the file cannot be read as per-phase.
        mixed = _synthetic(
            Path(tmp) / "mixed.jsonl",
            ["run", "run"] + ["since_previous_checkpoint"]
            * (len(MS.CHECKPOINTS) - 1))
        assert MS.summarise(mixed)["peak_is_per_phase"] is False


# ---------------------------------------------------------------------------
# The backing chain the driver's buffers really present
# ---------------------------------------------------------------------------


class _BufferLike(np.ndarray):
    """`pynq.buffer.PynqBuffer`'s shape: an ndarray over a foreign buffer."""

    def __new__(cls, nbytes, backing):
        return np.ndarray.__new__(cls, (nbytes,), np.uint8, buffer=backing)


def test_root_object_charges_the_buffer_not_the_mapping():
    """The correction the CMA buffers forced.

    `binary_view()` is `np.frombuffer(self._bin_buf, …).reshape(…)[:, :w]`,
    and numpy does NOT stop the `.base` chain at the buffer object: it runs
    all the way down to the mapping the pages were mapped through.  Charging
    the mapping is charging a pool that exists whatever this page does --
    up to 192 MiB against a 290 MiB budget.  The innermost ndarray is the
    refcounted allocation, and that is what must be charged.
    """
    pool = mmap.mmap(-1, 1 << 20)          # a 1 MiB mapping...
    buf = _BufferLike(4096, pool)          # ...with a 4 KiB buffer in it
    view = np.frombuffer(buf, dtype=np.uint8, count=3000).reshape(30, 100)

    chain = MS._base_chain(view)
    assert len(chain) >= 3, [type(o).__name__ for o in chain]
    assert isinstance(chain[-1], mmap.mmap), [type(o).__name__ for o in chain]

    root = MS._root_object(view)
    assert root is buf, type(root).__name__
    assert MS.distinct_backing_bytes([view]) == 4096

    # The mapping under it is not hidden -- it is reported beside the
    # charged size, so "4 KiB charged inside a 1 MiB mapping" is readable
    # from the record without re-running anything.
    d = MS.describe_arrays({"view": view})
    assert d["distinct_bytes"] == 4096, d
    assert d["names"]["view"]["provider"]["bytes"] == 1 << 20, d
    assert d["names"]["view"]["depth"] >= 3, d


class _InstanceOnlySamplesMv:
    """A pixmap shaped like PyMuPDF 1.19.2's: `samples_mv` per INSTANCE.

    1.19.2 assigns it in `Pixmap.__init__`, so it lands in `pix.__dict__`
    and appears on neither the class nor `dir(fitz.Pixmap)`.  Measured on
    the board -- `logs/b2prod_20260823/08_samples_mv.txt`.
    """

    def __init__(self, h, w, n):
        self.height, self.width, self.n = h, w, n
        self._buf = bytes(h * w * n)
        self.samples_mv = memoryview(self._buf)

    @property
    def samples(self):
        raise AssertionError(
            "took the `bytes`-copy path on a build that supports the "
            "zero-copy one; on a production page that is 186,126,336 B")


class _NoSamplesMv:
    """A build genuinely without it: only `samples`, a `bytes` copy."""

    def __init__(self, h, w, n):
        self.height, self.width, self.n = h, w, n
        self.samples = bytes(h * w * n)


def test_the_render_path_is_chosen_on_the_pixmap_not_on_the_class():
    """The 186 MB question, and the reason it is asked of the instance.

    `HAVE_SAMPLES_MV = hasattr(fitz.Pixmap, "samples_mv")` is False on
    PyMuPDF 1.19.2 while every pixmap has the attribute, so the detector
    would have taken the copying path on the one runtime where the copy
    cannot be afforded -- and it would have looked like the pipeline not
    fitting, not like a `hasattr` on the wrong object.
    """
    pix = _InstanceOnlySamplesMv(4, 5, 3)
    assert not hasattr(type(pix), "samples_mv"), "fixture models the wrong case"
    assert hasattr(pix, "samples_mv")

    arr = det._pixmap_view(pix)                  # raises if it reads .samples
    assert arr.shape == (4, 5, 3), arr.shape
    assert det.SAMPLES_MV_PATH == "samples_mv", det.SAMPLES_MV_PATH

    plain = det._pixmap_view(_NoSamplesMv(4, 5, 3))
    assert plain.shape == (4, 5, 3), plain.shape
    assert det.SAMPLES_MV_PATH == "samples", det.SAMPLES_MV_PATH


def test_root_object_is_unchanged_for_plain_arrays():
    """The extractor's patch case must keep charging the full-bound copy."""
    backing = np.zeros(251740, dtype=np.uint8)
    patch = backing[:600].reshape(20, 30)
    assert MS._root_object(patch) is backing
    assert MS.distinct_backing_bytes([patch]) == 251740
    assert MS.describe_arrays({"p": patch})["names"]["p"]["provider"] is None


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def test_record_is_on_disk_before_mark_returns():
    """Read from a SEPARATE handle, so the process buffer cannot answer.

    This is the whole durability claim: the run that gets SIGKILLed leaves
    no traceback and no summary, and the last record on disk is the only
    thing that says which phase it died in.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        s.mark("pipeline_ready")
        s.mark("render_complete")
        lines = Path(s.path).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3, lines
        assert json.loads(lines[-1])["checkpoint"] == "render_complete"
        s.close()


def test_truncated_tail_is_dropped_not_repaired():
    """A killed process can leave half a line.  The good records survive."""
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        s.mark("pipeline_ready")
        s.mark("render_complete")
        path = Path(s.path)
        s._fh.close()          # the kill: no trailer, handle gone
        text = path.read_text(encoding="utf-8")
        path.write_text(text + '{"record": "checkpoint", "chec',
                        encoding="utf-8")
        recs = MS.load(path)
        assert len(recs) == 3, recs
        assert recs[-1]["checkpoint"] == "render_complete", recs[-1]


def test_killed_run_reads_incomplete_and_names_the_phase():
    """No trailer, and the missing checkpoints are listed."""
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        s.mark("pipeline_ready")
        s.mark("render_complete")
        # No close(): this is what an OOM kill leaves behind.  The handle
        # goes with the process, which here means closing it by hand.
        s._fh.close()
        got = MS.summarise(s.path)
        assert got["truncated"] is True, got
        assert got["checkpoints_seen"] == ["pipeline_ready",
                                           "render_complete"], got
        assert "preprocess_complete" in got["missing"], got
        assert got["verdict"] in ("INCOMPLETE", "NOT-A-GATE"), got


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def _state(**kw):
    base = {"sources": ["proc"], "seen": list(MS.CHECKPOINTS),
            "max_vmswap_kB": 0, "max_system_swap_growth_kB": 0,
            "swap_growth_tolerance_kB": 0,
            "absent_required_fields": [], "failed": False,
            "failure": None, "teardown_status": 0}
    base.update(kw)
    return base


def test_verdict_pass():
    v = MS.verdict_from_records(_state())
    assert v["verdict"] == "PASS", v
    assert v["missing"] == [], v


def test_verdict_hold_on_process_swap():
    """Fitting by swapping is not fitting."""
    v = MS.verdict_from_records(_state(max_vmswap_kB=4))
    assert v["verdict"] == "HOLD", v
    assert "4 kB resident in swap" in v["reason"], v


def test_verdict_hold_on_system_swap_growth():
    """Pressure we caused in OTHER processes still invalidates the run."""
    v = MS.verdict_from_records(_state(max_system_swap_growth_kB=128))
    assert v["verdict"] == "HOLD", v
    assert "128 kB" in v["reason"], v
    tolerated = MS.verdict_from_records(
        _state(max_system_swap_growth_kB=128,
               swap_growth_tolerance_kB=256))
    assert tolerated["verdict"] == "PASS", tolerated


def test_verdict_swap_beats_incomplete():
    """A run that swapped AND died reports the swap.

    Order matters: INCOMPLETE invites "re-run it", HOLD says the memory
    result itself is not usable.
    """
    v = MS.verdict_from_records(_state(seen=["pipeline_ready"],
                                       max_vmswap_kB=1))
    assert v["verdict"] == "HOLD", v


def test_verdict_not_a_gate_off_proc():
    """An off-board dry run can never reach PASS, however clean it looks."""
    for source in ("psapi", "none", None):
        v = MS.verdict_from_records(_state(sources=None, source=source))
        assert v["verdict"] == "NOT-A-GATE", (source, v)


def test_verdict_incomplete_lists_what_is_missing():
    v = MS.verdict_from_records(_state(seen=["pipeline_ready",
                                             "render_complete"]))
    assert v["verdict"] == "INCOMPLETE", v
    assert "segments_complete" in v["reason"], v


# --- the four fail-open holes, each with its own test ----------------------


def test_verdict_fails_a_run_that_raised():
    """`failed=True` used to reach PASS.

    The runner marks `teardown_complete` from a `finally`, so a page that
    raised after `geometry_flushed` produced the whole checkpoint sequence
    and a perfectly healthy set of numbers -- describing a run that did not
    do the work.
    """
    v = MS.verdict_from_records(
        _state(failed=True, failure="RuntimeError('extractor')"))
    assert v["verdict"] == "FAIL", v
    assert "RuntimeError" in v["reason"], v


def test_verdict_fails_a_nonzero_teardown_status():
    """Ending while still holding fabric state is a different result."""
    v = MS.verdict_from_records(_state(teardown_status=1))
    assert v["verdict"] == "FAIL", v
    assert "status 1" in v["reason"], v
    assert MS.verdict_from_records(_state(teardown_status=0))["verdict"] \
        == "PASS"


def test_verdict_refuses_records_missing_the_fields_the_rules_read():
    """A `/proc` read that parsed to nothing must not read as measured.

    `_parse_kb_table` returns only what it FOUND, so a status file that
    opens and yields no recognised lines gives `{"source": "proc"}` with no
    numbers -- and every rule then read its own default, which for the swap
    rule is 0.  A rule that cannot fire is not a rule that passed.
    """
    v = MS.verdict_from_records(
        _state(absent_required_fields=["VmSwap_kB", "sys.SwapFree_kB"]))
    assert v["verdict"] == "NOT-A-GATE", v
    assert "VmSwap_kB" in v["reason"], v


def test_verdict_refuses_a_source_that_changes_mid_run():
    """A hole in the accounting is not a measured run.

    `self._source` was last-wins, so a `/proc` read that failed in the
    middle and worked again afterwards left the state saying "proc".
    """
    v = MS.verdict_from_records(_state(sources=["proc", "none", "proc"]))
    assert v["verdict"] == "NOT-A-GATE", v
    assert "none" in v["reason"], v


def test_reversed_checkpoints_are_malformed_not_pass():
    """Membership was the whole test, and membership does not order."""
    v = MS.verdict_from_records(_state(seen=list(reversed(MS.CHECKPOINTS))))
    assert v["missing"] == [], v          # every name IS present
    assert v["verdict"] == "MALFORMED", v


def test_duplicated_checkpoints_are_malformed_not_pass():
    seen = list(MS.CHECKPOINTS)
    seen.insert(seen.index("page_complete"), "page_complete")
    v = MS.verdict_from_records(_state(seen=seen))
    assert v["missing"] == [], v
    assert v["verdict"] == "MALFORMED", v


def test_a_run_of_two_pages_is_a_valid_sequence():
    """The per-page block repeats; a flat list cannot express that."""
    seen = (list(MS.PRE_PAGE_CHECKPOINTS)
            + list(MS.PER_PAGE_CHECKPOINTS) * 2
            + list(MS.POST_PAGE_CHECKPOINTS))
    v = MS.verdict_from_records(_state(seen=seen))
    assert v["verdict"] == "PASS", v
    assert v["sequence_pages"] == 2, v


def test_a_short_run_is_incomplete_and_a_wrong_one_is_malformed():
    """The distinction INCOMPLETE exists to make.

    A killed run leaves a PREFIX of the real sequence, which is what
    INCOMPLETE means.  A sequence that is not a prefix is a different
    failure and must not borrow the OOM signature.
    """
    short = MS.check_sequence(["pipeline_ready", "render_complete"])
    assert short["status"] == "prefix", short
    assert short["expected_next"] == "preprocess_complete", short

    wrong = MS.check_sequence(["render_complete", "pipeline_ready"])
    assert wrong["status"] == "malformed", wrong

    assert MS.check_sequence([])["status"] == "prefix"
    assert MS.check_sequence(list(MS.CHECKPOINTS))["status"] == "ok"
    trailing = list(MS.CHECKPOINTS) + ["render_complete"]
    assert MS.check_sequence(trailing)["status"] == "malformed"


def test_the_reader_understands_the_old_name_and_the_writer_cannot_emit_it():
    """A rename must not turn last week's evidence into MALFORMED.

    `06_sampler_offboard_*.jsonl` were written before `match_complete`
    became `initial_match_complete`.  Under a strict reader they stop being
    readable as sequences, which is a rename quietly invalidating honest
    records.  So the READER accepts the old name and the WRITER refuses it:
    the alias cannot become a second live name.
    """
    legacy = [c if c != "initial_match_complete" else "match_complete"
              for c in MS.CHECKPOINTS]
    seq = MS.check_sequence(legacy)
    assert seq["status"] == "ok", seq
    v = MS.verdict_from_records(_state(seen=legacy))
    assert v["verdict"] == "PASS", v
    assert v["missing"] == [], v          # not "initial_match_complete"

    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        try:
            s.mark("match_complete")
        except ValueError as exc:
            assert "unknown checkpoint" in str(exc), exc
        else:
            raise AssertionError("the writer accepted the retired name")
        finally:
            s.close()


def test_geometry_without_a_page_block_is_malformed():
    """A memory gate over zero pages measured nothing."""
    seen = list(MS.PRE_PAGE_CHECKPOINTS) + list(MS.POST_PAGE_CHECKPOINTS)
    v = MS.verdict_from_records(_state(seen=seen))
    assert v["verdict"] == "MALFORMED", v


def test_summarise_reads_the_outcome_from_the_teardown_checkpoint():
    """Not only from the trailer, which a killed run does not have.

    The runner writes the outcome into the `teardown_complete` MARK as well
    as into `close()`, and this is the path that survives.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp, page_label="one.pdf#p1")
        s.header()
        s.mark("teardown_complete", counts={"teardown_status": 2},
               flags={"failed": False})
        # No close(): the trailer is exactly what a killed run lacks.  The
        # handle would go with the process; here it is closed by hand.
        s._fh.close()
        got = MS.summarise(s.path)
        assert got["teardown_status"] == 2, got
        assert got["truncated"] is True, got
        # Off /proc this reads NOT-A-GATE first; the fact still has to be
        # carried, because on the board it is what turns PASS into FAIL.
        state = _state(seen=got["checkpoints_seen"],
                       teardown_status=got["teardown_status"])
        assert MS.verdict_from_records(state)["verdict"] == "FAIL"


def test_summarise_refuses_peak_attribution_across_pages():
    """VmHWM is per PROCESS and never falls.

    Two pages in one process means the peak belongs to whichever ran first,
    so peak attribution needs one page per process -- which is why the
    small-page re-invocation is a separate run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        s.page_label = "big.pdf#p1"
        s.mark("render_complete")
        s.page_label = "small.pdf#p1"
        s.mark("render_complete")
        s.close()
        got = MS.summarise(s.path)
        assert got["pages"] == ["big.pdf#p1", "small.pdf#p1"], got
        assert got["peak_attributable_to_one_page"] is False, got
        assert got["peak_VmHWM_kB"] is None, got
        assert got["peak_VmHWM_kB_process"] >= 0, got


def test_summarise_attributes_a_single_page_peak():
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp, page_label="one.pdf#p1")
        s.header()
        s.mark("render_complete")
        s.close()
        got = MS.summarise(s.path)
        assert got["pages"] == ["one.pdf#p1"], got
        assert got["peak_attributable_to_one_page"] is True, got


def test_header_records_the_rasteriser_versions():
    """A page rendered by a different MuPDF is a different page.

    The rebase this sampler was written for turns on that fact, so a record
    that cannot say which PyMuPDF produced it cannot be compared to
    anything.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header(backend="cpu")
        s.close()
        head = MS.load(s.path)[0]
        env = head["env"]
        assert head["facts"]["backend"] == "cpu", head
        for key in ("numpy", "cv2", "fitz"):
            assert key in env, env
        assert "VersionBind" in env["fitz"], env["fitz"]
        assert "VersionFitz" in env["fitz"], env["fitz"]
        assert env["samples_mv"] in (True, False), env
        assert env["machine"], env


# ---------------------------------------------------------------------------
# The retention the extractor holds
# ---------------------------------------------------------------------------


def test_retained_bytes_reports_backing_not_view():
    """`PlSideBankMatcher` holds full-bound copies, not just the patches."""
    m = B.PlSideBankMatcher(pl=None)
    fulls = [np.zeros(251740, dtype=np.uint8) for _ in range(3)]
    m._records = {i: {"patch": f[:600 * 90].reshape(90, 600)}
                  for i, f in enumerate(fulls)}
    m._batches = 1
    got = m.retained_bytes()
    assert got["records"] == 3, got
    assert got["patch_view_bytes"] == 3 * 600 * 90, got
    assert got["patch_backing_bytes"] == 3 * 251740, got
    assert got["batches"] == 1, got
    # The gap is the finding, and it is not small.
    assert got["patch_backing_bytes"] > 4 * got["patch_view_bytes"], got


def test_retained_bytes_is_zero_after_end_page():
    m = B.PlSideBankMatcher(pl=None)
    m._records = {0: {"patch": np.zeros((10, 10), dtype=np.uint8)}}
    m._batches = 2
    m.end_page()
    got = m.retained_bytes()
    assert got == {"records": 0, "patch_view_bytes": 0,
                   "patch_backing_bytes": 0, "batches": 0}, got


def test_backend_retained_bytes_is_zero_not_none_for_cpu():
    """"Retains nothing" and "nobody asked" are different answers."""
    b = B.make_backend("cpu")
    got = b.retained_bytes()
    assert got == {"records": 0, "patch_view_bytes": 0,
                   "patch_backing_bytes": 0, "batches": 0}, got


# ---------------------------------------------------------------------------
# The hooks in the detector
# ---------------------------------------------------------------------------


def _run_page_with_observer(tmp, page_index: int = 0):
    import fitz
    require_pdf()
    s = sampler(tmp, page_label="unit#p1")
    s.header()
    doc = fitz.open(str(STAGE2_PDF))
    try:
        out = det.detect_page(doc[page_index], side_templates=templates(),
                              zoom=TEST_ZOOM, score_thresh=0.33,
                              ferrule_score_thresh=0.24, score_margin=0.03,
                              backend=None, observer=s)
    finally:
        doc.close()
    s.close()
    return out, MS.load(s.path)


def test_detect_page_marks_every_page_phase_in_order():
    with tempfile.TemporaryDirectory() as tmp:
        _out, recs = _run_page_with_observer(tmp)
        seen = [r["checkpoint"] for r in recs if r.get("record") ==
                "checkpoint"]
        expected = ["render_complete", "preprocess_complete",
                    "segments_complete", "extraction_complete",
                    "initial_match_complete", "page_complete"]
        assert seen == expected, seen


def test_observer_does_not_change_detections():
    """The frozen CPU path, with the instrument attached and without.

    `backend is None` is byte-for-byte frozen; an observer that changed one
    detection would invalidate every comparison the ladder is built on.
    """
    import fitz
    require_pdf()
    with tempfile.TemporaryDirectory() as tmp:
        doc = fitz.open(str(STAGE2_PDF))
        try:
            kw = dict(side_templates=templates(), zoom=TEST_ZOOM,
                      score_thresh=0.33, ferrule_score_thresh=0.24,
                      score_margin=0.03, backend=None)
            _b0, c0, d0 = det.detect_page(doc[0], observer=None, **kw)
            s = sampler(tmp)
            s.header()
            _b1, c1, d1 = det.detect_page(doc[0], observer=s, **kw)
            s.close()
        finally:
            doc.close()
    assert len(c0) == len(c1), (len(c0), len(c1))
    key = lambda ds: [(d["kind"], d["x"], d["y"], d["w"], d["h"],
                       round(float(d["score"]), 9), d["id"]) for d in ds]
    assert key(d0) == key(d1), "the observer moved a detection"


def test_segments_source_is_recorded_per_page():
    """Which segment source ran, on every page.

    `extract_horizontal_segments_vector` swallows every `get_drawings()`
    exception and returns [], and the caller then falls to the raster path
    below 10 segments.  Under a MuPDF rebase that fallback is the failure
    that looks like success -- different segments, different candidates,
    filed downstream as a silicon disagreement.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _out, recs = _run_page_with_observer(tmp)
        seg = next(r for r in recs if r.get("checkpoint") ==
                   "segments_complete")
        assert seg["flags"]["segments_source"] in ("vector", "raster"), seg
        assert seg["flags"]["vector_fallback"] == (
            seg["flags"]["segments_source"] == "raster"), seg
        assert seg["counts"]["vector_segments"] >= 0, seg
        assert seg["counts"]["segments"] >= 0, seg


def test_preprocess_records_whether_the_binaries_alias():
    """62 MB once or 62 MB twice, said outright rather than inferred.

    On a CPU backend `build_text_suppressed_binary` returns a COPY; on
    `pl-extract`/`pl-all` the suppression goes through the driver and hands
    back a view of the DDR buffer.  Which one happened is a memory fact, and
    the backend name is not a reliable proxy for it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _out, recs = _run_page_with_observer(tmp)
        pre = next(r for r in recs if r.get("checkpoint") ==
                   "preprocess_complete")
        names = pre["arrays"]["names"]
        assert names["gray"]["bytes"] > 0, pre
        assert names["page_bin"]["bytes"] == names["clean_bin"]["bytes"], pre
        # The frozen CPU path takes the copy, so they must NOT alias.
        assert names["page_bin"]["group"] != names["clean_bin"]["group"], pre
        assert pre["arrays"]["distinct_bytes"] >= (
            names["page_bin"]["bytes"] + names["clean_bin"]["bytes"]), pre


def test_render_records_bgr_absence_not_a_zero():
    """`keep_bgr=False` must read as absent, not as an array of size 0."""
    import fitz
    require_pdf()
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        doc = fitz.open(str(STAGE2_PDF))
        try:
            det.detect_page(doc[0], side_templates=templates(),
                            zoom=TEST_ZOOM, score_thresh=0.33,
                            ferrule_score_thresh=0.24, score_margin=0.03,
                            backend=None, keep_bgr=False, observer=s)
        finally:
            doc.close()
        s.close()
        rec = next(r for r in MS.load(s.path)
                   if r.get("checkpoint") == "render_complete")
        assert rec["arrays"]["names"]["bgr"] is None, rec
        assert rec["flags"]["keep_bgr"] is False, rec
        # The path the render ACTUALLY took, not what the class-level
        # question implies.  They disagree on PyMuPDF 1.19.2, where
        # `samples_mv` is an instance attribute: believing the class there
        # costs a 186 MB `bytes` copy per production page.
        assert rec["flags"]["samples_mv_path"] in ("samples_mv", "samples")
        assert rec["flags"]["samples_mv"] == (
            rec["flags"]["samples_mv_path"] == "samples_mv"), rec
        assert rec["flags"]["samples_mv_on_class"] ==             det.HAVE_SAMPLES_MV_ON_CLASS, rec


def test_process_pdf_marks_pipeline_and_geometry():
    """The two document-level checkpoints, and the geometry byte count."""
    require_pdf()
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        gpath = Path(tmp) / "geom.json"
        det.process_pdf(
            input_pdf=str(STAGE2_PDF),
            output_pdf=str(Path(tmp) / "out.pdf"),
            male_left_template_path=str(HERE / "male_ter" / "male_left.png"),
            male_right_template_path=str(HERE / "male_ter" / "male_right.png"),
            female_left_template_path=str(HERE / "female_ter" / "female_left.png"),
            female_right_template_path=str(HERE / "female_ter" / "female_right.png"),
            ferrule_left_template_path=str(HERE / "ferrule_ter" / "ferrule_left.png"),
            ferrule_right_template_path=str(HERE / "ferrule_ter" / "ferrule_right.png"),
            zoom=TEST_ZOOM,
            debug_dir=str(Path(tmp) / "debug"),
            score_thresh=0.33, ferrule_score_thresh=0.24, score_margin=0.03,
            backend=None, debug_images=False,
            geometry_json=str(gpath), annotate=False, observer=s)
        s.close()
        recs = MS.load(s.path)
        seen = [r["checkpoint"] for r in recs
                if r.get("record") == "checkpoint"]
        assert seen[0] == "pipeline_ready", seen
        assert seen[-1] == "geometry_flushed", seen
        ready = recs[1]
        assert ready["counts"]["pages_in_document"] >= 1, ready
        geo = next(r for r in recs
                   if r.get("checkpoint") == "geometry_flushed")
        assert geo["counts"]["geometry_bytes"] == len(
            gpath.read_bytes()), (geo, gpath.stat().st_size)
        assert geo["counts"]["geometry_pages"] >= 1, geo


def test_page_label_is_set_per_page():
    """Records carry the page, so `summarise` can refuse a bad attribution."""
    require_pdf()
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        det.process_pdf(
            input_pdf=str(STAGE2_PDF),
            output_pdf=str(Path(tmp) / "out.pdf"),
            male_left_template_path=str(HERE / "male_ter" / "male_left.png"),
            male_right_template_path=str(HERE / "male_ter" / "male_right.png"),
            female_left_template_path=str(HERE / "female_ter" / "female_left.png"),
            female_right_template_path=str(HERE / "female_ter" / "female_right.png"),
            ferrule_left_template_path=str(HERE / "ferrule_ter" / "ferrule_left.png"),
            ferrule_right_template_path=str(HERE / "ferrule_ter" / "ferrule_right.png"),
            zoom=TEST_ZOOM,
            debug_dir=str(Path(tmp) / "debug"),
            score_thresh=0.33, ferrule_score_thresh=0.24, score_margin=0.03,
            backend=None, debug_images=False,
            geometry_json=str(Path(tmp) / "geom.json"),
            annotate=False, observer=s)
        s.close()
        pages = {r.get("page") for r in MS.load(s.path)
                 if r.get("record") == "checkpoint" and r.get("page")}
        # Derived from STAGE2_PDF rather than written out: the sampler
        # keys pages by real filename, and this assertion has to stay
        # exact without the source naming the drawing.
        assert pages == {f"{STAGE2_PDF.name}#p1"}, pages


def test_pl_all_records_retention_and_the_ddr_alias():
    """The `pl-all` path, against the fake fabric, through the real hooks.

    Two things only this test reaches.  The extractor's retention is
    non-zero and counted by BACKING allocation, and the PL suppression hands
    back a VIEW of the DDR buffer rather than a copy -- so `page_bin` and
    `clean_bin` must land in the SAME alias group here, where the frozen CPU
    path puts them in different ones.  That is 62 MB of difference between
    two backends, and the backend name is not evidence for it.
    """
    import fitz
    require_pdf()
    try:
        from test_pl_backends import FakePL
    except Exception as exc:                                 # noqa: BLE001
        raise SkipTest(f"FakePL unavailable: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp, page_label="fake-pl#p1")
        s.header()
        backend = B.make_backend("pl-all", pl=FakePL())
        doc = fitz.open(str(STAGE2_PDF))
        try:
            det.detect_page(doc[0], side_templates=templates(),
                            zoom=TEST_ZOOM, score_thresh=0.33,
                            ferrule_score_thresh=0.24, score_margin=0.03,
                            backend=backend, keep_bgr=False, observer=s)
        finally:
            doc.close()
        s.close()
        recs = MS.load(s.path)

        pre = next(r for r in recs
                   if r.get("checkpoint") == "preprocess_complete")
        names = pre["arrays"]["names"]
        assert names["page_bin"]["group"] == names["clean_bin"]["group"], (
            "the PL suppression handed back a COPY; the fabric would be "
            "matching a page that still has its text on it")

        ext = next(r for r in recs
                   if r.get("checkpoint") == "extraction_complete")
        counts = ext["counts"]
        assert counts["candidates"] > 0, counts
        assert counts["records"] == counts["candidates"], counts
        assert counts["batches"] >= 1, counts
        assert counts["patch_backing_bytes"] >= counts["patch_view_bytes"] > 0

        done = next(r for r in recs if r.get("checkpoint") == "page_complete")
        assert done["counts"]["records"] == 0, done["counts"]
        assert done["counts"]["patch_backing_bytes"] == 0, done["counts"]


def test_the_cma_buffers_are_in_the_explicit_accounting():
    """The full-page CMA GREY buffer was missing from the byte totals.

    A `pl-*` process holds three page-sized things, not the two the host
    arrays name: the host grey page, the CMA grey buffer the MM2S reads,
    and the CMA binary buffer the S2MM writes.  The binary one reached the
    record already -- `page_bin`/`clean_bin` are views of it -- but the grey
    one is referenced by nothing the detector holds, so `distinct_bytes`
    under-reported the page by a whole 62 MB at production size while
    `VmRSS` and `CmaFree` saw it perfectly well.  The instrument's
    EXPLANATION of the number has to agree with the number.
    """
    import fitz
    require_pdf()
    try:
        from test_pl_backends import FakePL
    except Exception as exc:                                 # noqa: BLE001
        raise SkipTest(f"FakePL unavailable: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp, page_label="fake-pl#p1")
        s.header()
        backend = B.make_backend("pl-all", pl=FakePL())
        doc = fitz.open(str(STAGE2_PDF))
        try:
            det.detect_page(doc[0], side_templates=templates(),
                            zoom=TEST_ZOOM, score_thresh=0.33,
                            ferrule_score_thresh=0.24, score_margin=0.03,
                            backend=backend, keep_bgr=False, observer=s)
        finally:
            doc.close()
        s.close()
        recs = MS.load(s.path)

        # Before anything is binarised the buffers do not exist, and the
        # record says `null` rather than omitting them -- "not allocated
        # yet" and "this backend has none" are different answers.
        render = next(r for r in recs
                      if r.get("checkpoint") == "render_complete")
        assert render["arrays"]["names"]["cma_gray"] is None, render["arrays"]

        pre = next(r for r in recs
                   if r.get("checkpoint") == "preprocess_complete")
        names = pre["arrays"]["names"]
        for key in ("cma_gray", "cma_binary"):
            assert names[key] is not None, names

        # The binary buffer is ONE allocation with the two host views of it.
        assert names["cma_binary"]["group"] == names["page_bin"]["group"]
        assert names["cma_binary"]["group"] == names["clean_bin"]["group"]

        # The grey buffer is its own allocation, and it is counted.
        assert names["cma_gray"]["group"] not in (
            names["cma_binary"]["group"], names["gray"]["group"]), names
        host_only = MS.describe_arrays(
            {"gray": None})          # shape check only; see below
        assert host_only["distinct_bytes"] == 0, host_only
        assert pre["arrays"]["distinct_bytes"] >= (
            names["gray"]["group_bytes"] + names["cma_gray"]["group_bytes"]
            + names["cma_binary"]["group_bytes"]), pre["arrays"]

        # And they are still named at the end of the page, where the
        # question is what is still held.
        done = next(r for r in recs if r.get("checkpoint") == "page_complete")
        assert done["arrays"]["names"]["cma_gray"] is not None


def test_the_cpu_path_has_no_cma_buffers_to_report():
    """Empty, not zeros: the CPU backend has no such allocation at all."""
    backend = B.make_backend("cpu")
    assert backend.sampler_arrays() == {}
    assert det._backend_arrays(None) == {}
    assert det._backend_arrays(backend) == {}


# ---------------------------------------------------------------------------
# The summariser CLI
# ---------------------------------------------------------------------------


def test_cli_reports_and_exits_non_zero_off_proc():
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp, page_label="one#p1")
        s.header()
        for c in MS.CHECKPOINTS:
            s.mark(c)
        s.close()
        rc = MS.main([s.path, "--require-pass"])
        expected = 0 if MS.sample_memory()["source"] == "proc" else 1
        assert rc == expected, (rc, expected)


def test_cli_json_mode_is_parseable():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as tmp:
        s = sampler(tmp)
        s.header()
        s.mark("pipeline_ready")
        s.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            MS.main([s.path, "--json"])
        got = json.loads(buf.getvalue())
        assert isinstance(got, list) and len(got) == 1, got
        assert got[0]["checkpoints_seen"] == ["pipeline_ready"], got


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for t in tests:
        try:
            t()
        except SkipTest as e:
            print(f"skip {t.__name__}: {e}")
            skipped += 1
        except Exception as e:                               # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
        else:
            print(f"ok   {t.__name__}")
    print(f"\n{len(tests) - failed - skipped}/{len(tests)} passed"
          + (f", {skipped} skipped" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
