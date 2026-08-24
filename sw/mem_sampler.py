#!/usr/bin/env python3
"""Checkpoint memory sampler for a production `detect_page()` run.

WHAT THIS IS FOR.  The board is 512 MiB with `cma=224M` reserved out of it,
leaving roughly 290 MiB of userspace, and a production page is 9792x6336.
Two peaks have already been found and fixed by reasoning about array
lifetimes — the renderer's 620 MB and the blur's 558 MB — but neither was
ever *measured on the board*, because every off-board run has 32 GB.  This
module measures them, per phase, and writes each record to disk before the
next phase starts.

**Flushed per record, deliberately.**  The run this instrument exists for is
the run that gets OOM-killed: SIGKILL, exit 137, no traceback, no verdict.
A summary written at the end would be written by exactly the runs that did
not need it.  Every `mark()` does write + flush + fsync, so the last record
on disk names the phase the kernel killed.

**It must not change what it measures.**  Two rules follow, and both are
load-bearing:

* the sampler extracts scalars from arrays and **never keeps a reference**.
  An instrument that holds the grey page alive adds 62 MB to the number it
  is reporting;
* `backend is None` stays the frozen CPU path.  The observer only reads.

## Checkpoints

Six are the ones the gate was specified with.  Three more were added, and
they are marked as additions rather than folded in, so the specified set
stays legible:

    pipeline_ready       templates loaded, backend built, overlay
                         programmed, document open — nothing page-sized
                         allocated yet.  Every later delta is against this.
    render_complete      render_page() returned: pixmap released, grey held
    preprocess_complete  binarised + text-suppressed
    segments_complete    ADDED.  Segment extraction sits BETWEEN binarise
                         and candidate collection, and `get_drawings()` is
                         the one allocation in the page that is not bounded
                         by the page dimensions: it materialises a dict plus
                         Point/Rect objects per path item, so a dense
                         drawing can be hundreds of thousands of objects in
                         a 32-bit process.  Without this checkpoint that
                         excursion is attributed to candidate collection.
    extraction_complete  candidates collected and the batch dispatched;
                         this is where the extractor's patches land
    initial_match_complete
                         ADDED.  The classification loop, and NOTHING
                         after it.  The name is deliberate: this mark sits
                         between the `build_detection` loop and
                         `refine_misaligned_terminal_boxes`, so what it
                         closes is the INITIAL match — on `pl-*` the pass
                         that runs on the fabric — and what follows it
                         (refinement and dedupe) is ARM work in every
                         backend.  Calling it `match_complete` invited the
                         reading that matching as a whole was done here,
                         which is the opposite of the split it records.
                         Without the checkpoint at all, matching,
                         refinement and dedupe — the longest phase of the
                         page — all report under `geometry_flushed`.
    page_complete        ADDED.  detect_page() returned and end_page() ran.
                         This is the checkpoint that answers "did the
                         per-page retention actually get released".
    geometry_flushed     the geometry JSON is on disk
    teardown_complete    the PL is torn down and the process is about to end

## Reading the numbers

`VmHWM` is a **per-process** high-water mark and never falls.  A second page
in the same process inherits the first page's peak, so peak attribution
needs **one page per process** — which is why the small-page re-invocation
is a separate run, not a second iteration.  `summarise()` refuses to
attribute a peak when it sees more than one page in one file.

Within one page the same monotonicity hides phases: measured off-board, the
renderer takes the process 247.5 MiB above baseline, so every later
checkpoint reports 247.5 and the only thing the column proves about them is
that they did not exceed it — a 65 MiB blind spot over segment extraction,
matching and refinement.  `per_phase_peak=True` resets `VmHWM` after each
record (`reset_peak_rss`), which turns the column into a per-phase peak
without losing the run peak, since that is then the maximum over the
records.  Every record carries `peak_window` saying which of the two it is,
so a run where the reset was unavailable cannot be read as the other.

`VmRSS` falling is not proof memory was returned to the OS: glibc may keep
freed arena pages.  A drop is evidence; a non-drop is not counter-evidence.

`CmaFree` predicts nothing about whether the next CMA allocation succeeds —
the kernel migrates page-cache pages out of the region.  It is recorded
because it is cheap and occasionally diagnostic, not as a capacity gate.

## Verdicts

    PASS         the whole sequence, in order, from /proc, with every
                 field the rules read present, no swap, and a run that
                 ended clean
    NOT-A-GATE   the records are not a usable /proc reading.  Either the
                 source was not /proc at all — an off-board dry run on
                 Windows produces real numbers from a different accounting
                 system — or a record claims /proc and does not carry the
                 fields the rules read.  Neither can close the gate
    FAIL         the run itself did not end clean: `detect_page` raised, or
                 the PL teardown returned non-zero.  The numbers may look
                 perfectly healthy, and they describe a run that did not do
                 the work
    HOLD         swap was used.  The pipeline "fitting" by swapping is not
                 fitting: the board has 511 MiB of swap and would happily
                 hide a 700 MB peak behind it, at a wall-time cost that
                 would then be filed against the fabric
    MALFORMED    the checkpoints are present but not in a shape a page can
                 have passed through — out of order, repeated, or with a
                 phase block interleaved.  Distinct from INCOMPLETE because
                 a short run is a PREFIX of the real sequence and this is
                 not one
    INCOMPLETE   the checkpoint sequence stops early — the usual signature
                 of the OOM kill this instrument was written for

Precedence is that list's order, and it is not arbitrary.  NOT-A-GATE first
because a reading that is not a measurement cannot be given any other
verdict.  FAIL next because "the run broke" has to be said before anything
about its memory, or a failed run reads as a memory problem.  HOLD before
the two sequence verdicts for the reason it always had: INCOMPLETE invites
"re-run it", HOLD says the memory result itself is not usable.

**Why so many of these block PASS.**  Every one of them was reachable
alongside `verdict="PASS"` before this was tightened: a run that raised, a
teardown that returned non-zero, a `/proc` read that came back with no
`VmSwap_kB` (so the swap rule could not fire), and a checkpoint list that
was reversed or duplicated (only membership was tested).  A gate that says
PASS in those states is not a gate.

`summarise()` recomputes the verdict from the checkpoint records alone and
is the authority.  The trailer record is a convenience, and the runs that
matter most do not have one — which is why the failure and teardown facts
are written into the `teardown_complete` CHECKPOINT as well as the trailer,
and `summarise()` reads whichever it finds.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import sys
import time
from typing import Dict, List, Optional, Sequence


#: The checkpoints the gate was specified with, in order.
SPECIFIED_CHECKPOINTS = (
    "pipeline_ready",
    "render_complete",
    "preprocess_complete",
    "extraction_complete",
    "geometry_flushed",
    "teardown_complete",
)

#: Added on review — see the module docstring for why each one earns a
#: /proc read.  Listed separately so the specified set above stays legible.
ADDED_CHECKPOINTS = (
    "segments_complete",
    "initial_match_complete",
    "page_complete",
)

# --- the shape a run has -----------------------------------------------
#
# Not one flat list, because the middle block REPEATS: `process_pdf` marks
# `pipeline_ready` once, then runs `detect_page` per page, then flushes the
# geometry once and tears down once.  Splitting it here is what lets the
# verdict check ORDER instead of membership — a set of names cannot tell a
# two-page run from a run that emitted `page_complete` twice in a row.

#: Marked once, before the page loop.
PRE_PAGE_CHECKPOINTS = ("pipeline_ready",)

#: Marked once per page, in this order, inside `detect_page`.
PER_PAGE_CHECKPOINTS = (
    "render_complete",
    "preprocess_complete",
    "segments_complete",
    "extraction_complete",
    "initial_match_complete",
    "page_complete",
)

#: Marked once, after the page loop.  `teardown_complete` comes from the
#: runner's `finally`, so it is present on a run that raised.
POST_PAGE_CHECKPOINTS = ("geometry_flushed", "teardown_complete")

#: The full sequence for a ONE-PAGE run, in the order it passes through
#: them.  Multi-page runs repeat `PER_PAGE_CHECKPOINTS`; see
#: `check_sequence()`, which is what the verdict actually uses.
CHECKPOINTS = (PRE_PAGE_CHECKPOINTS + PER_PAGE_CHECKPOINTS
               + POST_PAGE_CHECKPOINTS)

#: Names that existed in earlier records, and what they are now.
#:
#: READ-ONLY.  `check_sequence()` accepts these so that files written before
#: a rename can still be summarised -- a renamed checkpoint should not turn
#: honest evidence into `MALFORMED` months later.  `mark()` deliberately
#: does NOT accept them: the reader understands the old vocabulary, the
#: writer cannot emit it, and that asymmetry is what stops the alias
#: becoming a second live name.
LEGACY_CHECKPOINT_NAMES = {
    # Renamed 2026-08-23.  The mark sits between the classification loop and
    # refinement, so it closes the INITIAL match and not matching as a
    # whole; see the module docstring.
    "match_complete": "initial_match_complete",
}

#: Fields lifted from /proc/self/status.
_STATUS_FIELDS = ("VmSize", "VmRSS", "VmHWM", "VmData", "VmSwap", "VmPeak")

#: Fields lifted from /proc/meminfo.
_MEMINFO_FIELDS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached",
                   "SwapTotal", "SwapFree", "CmaTotal", "CmaFree")

# --- what a record must carry before it can be part of a PASS ----------
#
# `_parse_kb_table` returns only the keys it FOUND.  A `/proc/self/status`
# that opens and parses to `{}` therefore produced `{"source": "proc"}` with
# no numbers in it, and every rule below then read its default: `max_swap`
# fell back to 0, which is the swap rule silently not firing.  A record that
# cannot be checked is not a record that passed.
#
# The split is deliberate.  The first group is what the VERDICT RULES read,
# so their absence disables a rule.  The second is the MEASUREMENT itself —
# no rule reads `VmHWM`, and a memory gate that closes without a
# high-water mark has not measured anything.  Everything else /proc offers
# (`MemAvailable`, `CmaFree`, `VmData`, `Buffers`) is diagnostic and is NOT
# required, because a kernel that omits one of those should not fail a run
# that is otherwise fully evidenced.

#: Read by the swap rules.  Absent ⇒ the rule cannot fire.
REQUIRED_RULE_FIELDS = ("VmSwap_kB",)
REQUIRED_RULE_SYS_FIELDS = ("SwapTotal_kB", "SwapFree_kB")

#: The measurement.  Absent ⇒ there is nothing to report.
REQUIRED_MEASUREMENT_FIELDS = ("VmRSS_kB", "VmHWM_kB")

#: Every per-process field a PASS record must carry.
REQUIRED_MEM_FIELDS = REQUIRED_RULE_FIELDS + REQUIRED_MEASUREMENT_FIELDS

#: Every /proc/meminfo field a PASS record must carry.
REQUIRED_SYS_FIELDS = REQUIRED_RULE_SYS_FIELDS


# ---------------------------------------------------------------------------
# Where the numbers come from
# ---------------------------------------------------------------------------


def _parse_kb_table(text: str, wanted: Sequence[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    want = set(wanted)
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key not in want:
            continue
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key + "_kB"] = int(parts[0])
    return out


def read_proc_status(pid: str = "self") -> Optional[Dict[str, int]]:
    """`/proc/<pid>/status`, in kB, or None where there is no /proc."""
    try:
        with open("/proc/%s/status" % pid, "r", encoding="ascii",
                  errors="replace") as fh:
            return _parse_kb_table(fh.read(), _STATUS_FIELDS)
    except OSError:
        return None


def read_meminfo() -> Optional[Dict[str, int]]:
    """`/proc/meminfo`, in kB, or None where there is no /proc."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii",
                  errors="replace") as fh:
            return _parse_kb_table(fh.read(), _MEMINFO_FIELDS)
    except OSError:
        return None


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32),
                ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def read_psapi() -> Optional[Dict[str, int]]:
    """Windows working set, under the /proc field names' nearest analogues.

    For the OFF-BOARD dry run only.  `WorkingSetSize` is not `VmRSS` and
    `PagefileUsage` is not `VmSwap`; the mapping is close enough to check
    that the instrument is wired up and shaped right, and a record carrying
    `source="psapi"` can never reach PASS.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb)
        if not ok:
            return None
    except (AttributeError, OSError):
        return None
    return {
        "VmRSS_kB": counters.WorkingSetSize // 1024,
        "VmHWM_kB": counters.PeakWorkingSetSize // 1024,
        "VmSize_kB": counters.PagefileUsage // 1024,
        "VmPeak_kB": counters.PeakPagefileUsage // 1024,
        # Deliberately absent: Windows has no per-process resident-swap
        # counter, so there is nothing honest to put in VmSwap_kB.  The
        # verdict never reads it, because `source` is not "proc".
    }


def reset_peak_rss() -> bool:
    """Reset `VmHWM` to the current `VmRSS` (Linux 4.0+), or say it cannot.

    WHY THIS EXISTS.  `VmHWM` is monotonic, so once the renderer has taken
    the process to its peak, every later phase reports that same number and
    the only thing the column can prove about them is "did not exceed it".
    On a production page the render peak is ~247 MiB above baseline, which
    leaves a 65 MiB blind spot over every phase after it — big enough to
    hide the whole segment-extraction transient this sampler was extended
    to look for.

    Resetting at each checkpoint turns the column into a PER-PHASE peak,
    and the run's true peak is then the maximum over the records — nothing
    is lost, because the reset happens immediately after the read.

    `/proc/self/clear_refs` is gated on `CONFIG_PROC_PAGE_MONITOR` and may
    simply not exist.  Returning False rather than raising is deliberate,
    and it is not fail-open: the records carry `peak_window`, so a run where
    the reset did not work says "run" and cannot be read as per-phase.
    """
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as fh:
            fh.write("5\n")
        return True
    except OSError:
        return False


def sample_memory() -> Dict[str, object]:
    """One reading, tagged with which accounting system produced it."""
    status = read_proc_status()
    if status is not None:
        out: Dict[str, object] = {"source": "proc"}
        out.update(status)
        return out
    psapi = read_psapi()
    if psapi is not None:
        out = {"source": "psapi"}
        out.update(psapi)
        return out
    return {"source": "none"}


# ---------------------------------------------------------------------------
# Arrays: how many bytes are really held, and how many times
# ---------------------------------------------------------------------------


def _is_array(obj) -> bool:
    """Duck-typed "this is an ndarray-like allocation boundary".

    `isinstance(obj, np.ndarray)` would be plainer and is not used, because
    this module deliberately imports nothing outside the standard library:
    `python mem_sampler.py FILE` is the summariser, and it has to read a
    board run's evidence on a machine that has no numpy.  An ndarray always
    carries `.base` (even when it is `None`) and an integer `.nbytes`; the
    raw providers this needs to tell it apart from — `bytes`, `bytearray`,
    `memoryview`, `mmap`, cffi buffers — carry no `.base` at all.
    """
    return (hasattr(obj, "base")
            and isinstance(getattr(obj, "nbytes", None), int))


def _base_chain(arr) -> List[object]:
    """`arr`, then everything its `.base` chain reaches, outermost first."""
    chain: List[object] = []
    seen = set()
    obj = arr
    while True:
        chain.append(obj)
        base = getattr(obj, "base", None)
        if base is None or id(base) in seen:
            return chain
        seen.add(id(base))
        obj = base


def _root_object(arr):
    """The allocation that keeps the memory `arr` looks at alive.

    A view's `nbytes` is the size of the WINDOW, not of the allocation
    keeping it alive.  The extractor's patch records are exactly this case:
    each `rec["patch"]` is a small reshaped slice of a full-bound 251,740 B
    copy of the receive buffer, and the whole copy stays alive as long as
    the slice does.  Summing `patch.nbytes` there under-reports the page's
    retention several-fold.

    **This stops at the innermost ndarray, not at the end of the chain,**
    and that is the correction the driver's buffers forced.  `binary_view()`
    is `np.frombuffer(self._bin_buf, …).reshape(h, stride)[:, :w]`, and
    numpy does not stop the `.base` chain at the `PynqBuffer` — measured,
    the chain runs

        view -> reshaped -> frombuffer -> PynqBuffer -> <mmap / cffi buffer>

    all the way down to the raw object the CMA pages were mapped through.
    Following it to the end asks that object for its size, and the size of
    a mapping is not the size of the allocation: it is page-rounded at
    best, and if a PYNQ release ever carves buffers out of one pool
    mapping, it is the size of the POOL.  The CMA pool is reserved at boot
    whatever this page does, so charging it to the page would be a
    ~192 MiB error in a ~290 MiB budget.

    An ndarray in the chain is a refcounted allocation boundary — dropping
    the last reference to a `PynqBuffer` is what releases its CMA pages —
    so the innermost one is the thing to charge.  Where the chain bottoms
    out with no ndarray between the view and a plain buffer (a `bytes` from
    `Pixmap.samples`, say), that buffer IS the allocation and is returned.

    KNOWN LIMIT, deliberately left: `np.frombuffer(b, count=k)` over a
    plain `bytes` with `k` smaller than `b` reports `k`, not `len(b)`.
    Nothing in this pipeline has that shape, and `describe_arrays` records
    the terminal provider beside the charged size so the case is visible
    rather than silent.
    """
    chain = _base_chain(arr)
    if not _is_array(chain[-1]):
        for obj in reversed(chain):
            if _is_array(obj):
                return obj
    return chain[-1]


def _provider(arr) -> Optional[Dict[str, object]]:
    """The non-ndarray object under the charged allocation, if any.

    Reported so that "62 MB of CMA" can be told apart from "62 MB of CMA
    inside a 192 MiB mapping" without re-running anything.
    """
    chain = _base_chain(arr)
    tail = chain[-1]
    if _is_array(tail):
        return None
    return {"type": type(tail).__name__, "bytes": _byte_size(tail)}


def _byte_size(obj) -> Optional[int]:
    n = getattr(obj, "nbytes", None)
    if isinstance(n, int):
        return n
    try:
        return memoryview(obj).nbytes
    except TypeError:
        return None


def distinct_backing_bytes(arrays) -> int:
    """Bytes of the distinct allocations behind an iterable of arrays.

    Counted once per backing object, so N views of one buffer cost that
    buffer once — and one small view of a large buffer costs the LARGE one,
    which is the point.  Keeps no reference to anything it is handed.
    """
    sizes: Dict[int, int] = {}
    for arr in arrays:
        if arr is None:
            continue
        root = _root_object(arr)
        n = _byte_size(root)
        if n is not None:
            sizes[id(root)] = n
    return int(sum(sizes.values()))


def describe_arrays(arrays: Dict[str, object]) -> Dict[str, object]:
    """Sizes and aliasing for a set of named arrays.

    Returns plain data and keeps **no reference** to anything passed in —
    see the module docstring.  `alias_groups` are computed within this one
    record: object ids are reused after a free, so they are reported as
    small group indices rather than as addresses, and cannot be compared
    across records.

    `distinct_bytes` is the number that predicts RSS: the backing
    allocations counted once each.  `view_bytes` is the naive sum, kept
    beside it so the gap between them is visible rather than inferred.
    """
    names: Dict[str, object] = {}
    roots: Dict[int, int] = {}          # id(root) -> group index
    root_bytes: Dict[int, Optional[int]] = {}
    groups: List[List[str]] = []

    for name, arr in arrays.items():
        if arr is None:
            names[name] = None
            continue
        root = _root_object(arr)
        rid = id(root)
        if rid not in roots:
            roots[rid] = len(groups)
            root_bytes[rid] = _byte_size(root)
            groups.append([])
        group = roots[rid]
        groups[group].append(name)
        names[name] = {
            "bytes": _byte_size(arr),
            "shape": [int(d) for d in (getattr(arr, "shape", ()) or ())],
            "dtype": str(getattr(arr, "dtype", "")),
            "owns_data": getattr(arr, "base", None) is None,
            "contiguous": bool(getattr(getattr(arr, "flags", None),
                                       "c_contiguous", False)),
            "group": group,
            "group_bytes": root_bytes[rid],
            # The chain BELOW the allocation that was charged, when there is
            # one: a CMA buffer sits on an mmap, and "62 MB charged, mmap of
            # 62 MB underneath" reads differently from "62 MB charged, mmap
            # of 192 MiB underneath".  `_root_object` explains why the
            # mapping is not what gets charged.
            "depth": len(_base_chain(arr)),
            "provider": _provider(arr),
        }
        # `arr` and `root` fall out of scope with this iteration; nothing
        # below this line may keep either.

    view_bytes = sum(v["bytes"] for v in names.values()
                     if isinstance(v, dict) and v["bytes"] is not None)
    distinct = [b for b in root_bytes.values() if b is not None]
    return {
        "names": names,
        "alias_groups": [g for g in groups if len(g) > 1],
        "view_bytes": int(view_bytes),
        "distinct_bytes": int(sum(distinct)),
        "distinct_unknown": len(root_bytes) - len(distinct),
    }


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------


class MemorySampler:
    """Writes one flushed JSON record per checkpoint.

    Constructed by the runner and threaded down as `observer=`; every hook
    site tolerates `None`, so an un-instrumented run is unchanged and costs
    nothing.
    """

    def __init__(self, path, *, page_label: str = "", note: str = "",
                 fsync: bool = True, swap_growth_tolerance_kB: int = 0,
                 per_phase_peak: bool = False):
        self.path = str(path)
        self.page_label = str(page_label)
        self.note = str(note)
        self._fsync = bool(fsync)
        self._swap_tol = int(swap_growth_tolerance_kB)
        self._per_phase_peak = bool(per_phase_peak)
        self._peak_window = "run"
        self._seq = 0
        self._t0 = time.monotonic()
        self._seen: List[str] = []
        self._max_swap_kB = 0
        self._baseline_swap_used_kB: Optional[int] = None
        self._max_system_swap_growth_kB = 0
        # EVERY source seen, not the last one.  A `/proc` read that fails
        # mid-run and then works again used to leave `self._source ==
        # "proc"`, so a run with a hole in its accounting read as fully
        # measured.  The verdict requires the set to be exactly {"proc"}.
        self._sources: List[str] = []
        self._absent_required: List[str] = []
        # Written by the runner into the `teardown_complete` mark and into
        # `close()`; either is enough, because the trailer is missing from
        # exactly the runs that matter most.
        self._failed = False
        self._failure: Optional[str] = None
        self._teardown_status: Optional[int] = None
        self._closed = False
        # Opened before anything else, so a run that cannot write its
        # evidence fails now rather than six hours in.
        self._fh = open(self.path, "w", encoding="utf-8", newline="\n")

    # -- plumbing ----------------------------------------------------------

    def _emit(self, record: Dict[str, object]) -> None:
        self._fh.write(json.dumps(record, sort_keys=True) + "\n")
        self._fh.flush()
        if self._fsync:
            os.fsync(self._fh.fileno())

    def _now(self) -> Dict[str, object]:
        mem = sample_memory()
        source = str(mem.get("source", "none"))
        if source not in self._sources:
            self._sources.append(source)
        sysinfo = read_meminfo() or {}
        for field in missing_required_fields(mem, sysinfo):
            if field not in self._absent_required:
                self._absent_required.append(field)

        swap = mem.get("VmSwap_kB")
        if isinstance(swap, int):
            self._max_swap_kB = max(self._max_swap_kB, swap)

        total = sysinfo.get("SwapTotal_kB")
        free = sysinfo.get("SwapFree_kB")
        if isinstance(total, int) and isinstance(free, int):
            used = total - free
            if self._baseline_swap_used_kB is None:
                self._baseline_swap_used_kB = used
            self._max_system_swap_growth_kB = max(
                self._max_system_swap_growth_kB,
                used - self._baseline_swap_used_kB)
        return {"mem": mem, "sys": sysinfo,
                "peak_window": self._peak_window}

    def _reset_peak(self) -> None:
        """Called AFTER the record is written, never before."""
        if not self._per_phase_peak:
            return
        self._peak_window = ("since_previous_checkpoint"
                             if reset_peak_rss() else "run")

    def _absorb_outcome(self, *sources: Optional[Dict]) -> None:
        """Pick the run's outcome out of whatever the runner passed in.

        The runner reports it twice on purpose — as `flags`/`counts` on the
        `teardown_complete` mark, and as `close()` facts — because a killed
        run has no trailer.  Both land here, and `failed` is sticky: once
        anything says the run raised, nothing later unsays it.
        """
        for src in sources:
            if not src:
                continue
            if src.get("failed"):
                self._failed = True
            if src.get("failure") is not None:
                self._failure = str(src["failure"])
            status = src.get("teardown_status")
            if isinstance(status, int):
                self._teardown_status = (
                    status if self._teardown_status in (None, 0)
                    else self._teardown_status)

    # -- records -----------------------------------------------------------

    def header(self, **facts) -> None:
        """Environment, written first.

        The version fields are not decoration.  This whole exercise exists
        because a page rendered by a different MuPDF is a different page,
        and a record that does not say which one produced it cannot be
        compared with anything later.
        """
        env: Dict[str, object] = {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "pid": os.getpid(),
            "argv": list(sys.argv),
            "cwd": os.getcwd(),
        }
        for mod, fields in (("numpy", ("__version__",)),
                            ("cv2", ("__version__",)),
                            ("fitz", ("VersionBind", "VersionFitz",
                                      "__file__"))):
            try:
                m = __import__(mod)
            except Exception as exc:                         # noqa: BLE001
                env[mod] = "import failed: %s" % exc.__class__.__name__
                continue
            env[mod] = {f: str(getattr(m, f, "")) for f in fields}
        # `samples_mv` decides whether a rendered page costs one buffer or
        # two, so the header has to get it right rather than nearly right.
        # Asking the CLASS is nearly right and wrong where it matters: on
        # PyMuPDF 1.19.2 -- the board's candidate runtime -- `samples_mv` is
        # assigned in `Pixmap.__init__`, so it is in every instance's
        # `__dict__` and on neither the class nor `dir()`.  A header that
        # recorded `samples_mv=False` there would describe a zero-copy run
        # as a copying one, and 186 MB of a ~290 MiB budget would be
        # unaccounted for in the very record written to account for it.
        #
        # A pixmap is made to settle it.  One pixel, so the instrument
        # still costs nothing, and any failure records `None` rather than
        # taking the run down.
        env["samples_mv_on_class"] = None
        env["samples_mv"] = None
        try:
            import fitz
            env["samples_mv_on_class"] = bool(
                hasattr(fitz.Pixmap, "samples_mv"))
            doc = fitz.open()
            try:
                page = doc.new_page(width=1, height=1)
                pix = page.get_pixmap(alpha=False)
                env["samples_mv"] = bool(hasattr(pix, "samples_mv"))
                del pix
            finally:
                doc.close()
        except Exception:                                    # noqa: BLE001
            pass

        snapshot = self._now()
        record: Dict[str, object] = {
            "record": "header",
            "seq": self._seq,
            "checkpoints_specified": list(SPECIFIED_CHECKPOINTS),
            "checkpoints_added": list(ADDED_CHECKPOINTS),
            "page": self.page_label,
            "note": self.note,
            "t_wall_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "env": env,
            "facts": facts,
        }
        record["per_phase_peak_requested"] = self._per_phase_peak
        record.update(snapshot)
        self._emit(record)
        self._seq += 1
        self._reset_peak()

    def mark(self, checkpoint: str, *, arrays: Optional[Dict] = None,
             counts: Optional[Dict] = None,
             flags: Optional[Dict] = None) -> None:
        """One checkpoint record, on disk before this returns.

        An unknown name raises: a typo would silently drop a checkpoint, and
        a dropped checkpoint reads as INCOMPLETE, which reads as an OOM
        kill.  A /proc read that fails does NOT raise — it records null,
        which is the honest answer.
        """
        if checkpoint not in CHECKPOINTS:
            raise ValueError(
                "unknown checkpoint %r; known: %s"
                % (checkpoint, ", ".join(CHECKPOINTS)))
        snapshot = self._now()
        record: Dict[str, object] = {
            "record": "checkpoint",
            "seq": self._seq,
            "checkpoint": checkpoint,
            "page": self.page_label,
            "t_mono_s": round(time.monotonic() - self._t0, 6),
            "arrays": describe_arrays(arrays) if arrays else None,
            "counts": dict(counts) if counts else None,
            "flags": dict(flags) if flags else None,
        }
        record.update(snapshot)
        self._emit(record)
        self._seq += 1
        self._seen.append(checkpoint)
        self._absorb_outcome(flags, counts)
        self._reset_peak()

    def verdict(self) -> Dict[str, object]:
        """This process's own view.  `summarise()` is the authority."""
        return verdict_from_records(self._verdict_state())

    def _verdict_state(self) -> Dict[str, object]:
        return {
            "sources": list(self._sources),
            "seen": list(self._seen),
            "max_vmswap_kB": self._max_swap_kB,
            "max_system_swap_growth_kB": self._max_system_swap_growth_kB,
            "swap_growth_tolerance_kB": self._swap_tol,
            "absent_required_fields": list(self._absent_required),
            "failed": self._failed,
            "failure": self._failure,
            "teardown_status": self._teardown_status,
        }

    def close(self, **facts) -> Dict[str, object]:
        """Trailer with the in-process verdict.  Absent on a killed run."""
        if self._closed:
            return self.verdict()
        self._absorb_outcome(facts)
        v = dict(self.verdict())
        v["swap_growth_tolerance_kB"] = self._swap_tol
        self._emit({"record": "trailer", "seq": self._seq,
                    "page": self.page_label, "verdict": v, "facts": facts})
        self._seq += 1
        self._fh.close()
        self._closed = True
        return v

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "MemorySampler":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._closed:
            self.close(exception=None if exc is None else repr(exc))
        return False


# ---------------------------------------------------------------------------
# Reading a sampler file back
# ---------------------------------------------------------------------------


def check_sequence(seen: Sequence[str]) -> Dict[str, object]:
    """Is `seen` a run's checkpoint sequence, a prefix of one, or neither?

    Membership is not enough, and that was the hole: `[c for c in
    CHECKPOINTS if c not in seen]` is empty for the correct sequence, for
    the REVERSED sequence, and for one with `page_complete` emitted three
    times — all of which then read PASS.  Order is the only thing that says
    a page went through these phases rather than that these names appeared.

    The grammar is the one `process_pdf` actually emits:

        pipeline_ready  (per-page block)+  geometry_flushed
        teardown_complete

    Returns `status` in:

      ``ok``         a complete run of one or more pages
      ``prefix``     a correct sequence that stops early — what an OOM kill
                     leaves.  `expected_next` names the phase it died in
      ``malformed``  anything else: reordered, repeated, interleaved, or
                     carrying a name after `teardown_complete`

    `pages` counts completed per-page blocks, which is a stronger statement
    than the page LABELS: a label is metadata the runner sets, and this is
    the phase sequence itself.
    """
    seen = [LEGACY_CHECKPOINT_NAMES.get(c, c) for c in seen]
    n = len(seen)
    per = list(PER_PAGE_CHECKPOINTS)
    i = 0
    pages = 0

    def prefix(expected):
        return {"status": "prefix", "pages": pages, "expected_next": expected,
                "detail": ""}

    def bad(detail):
        return {"status": "malformed", "pages": pages, "expected_next": None,
                "detail": detail}

    for want in PRE_PAGE_CHECKPOINTS:
        if i == n:
            return prefix(want)
        if seen[i] != want:
            return bad("record %d is %r; a run opens with %r"
                       % (i, seen[i], want))
        i += 1

    while True:
        if i == n:
            return prefix(POST_PAGE_CHECKPOINTS[0] if pages
                          else per[0])
        if seen[i] == POST_PAGE_CHECKPOINTS[0]:
            break
        block = seen[i:i + len(per)]
        for k, got in enumerate(block):
            if got != per[k]:
                return bad("record %d is %r; page %d's phase %d is %r"
                           % (i + k, got, pages + 1, k, per[k]))
        if len(block) < len(per):
            return prefix(per[len(block)])
        i += len(per)
        pages += 1

    if pages == 0:
        return bad("the geometry was flushed with no page block before it; "
                   "a memory gate over zero pages measures nothing")

    for want in POST_PAGE_CHECKPOINTS:
        if i == n:
            return prefix(want)
        if seen[i] != want:
            return bad("record %d is %r; the run closes with %r"
                       % (i, seen[i], want))
        i += 1

    if i != n:
        return bad("%d record(s) after %r: %s"
                   % (n - i, POST_PAGE_CHECKPOINTS[-1],
                      ", ".join(seen[i:])))
    return {"status": "ok", "pages": pages, "expected_next": None,
            "detail": ""}


def missing_required_fields(mem: Dict[str, object],
                            sysinfo: Dict[str, object]) -> List[str]:
    """Required fields this ONE record does not carry, as `Vm…`/`sys.…`."""
    out = [f for f in REQUIRED_MEM_FIELDS
           if not isinstance(mem.get(f), int)]
    out += ["sys." + f for f in REQUIRED_SYS_FIELDS
            if not isinstance(sysinfo.get(f), int)]
    return out


def verdict_from_records(state: Dict[str, object]) -> Dict[str, object]:
    """The verdict and the rule that fired.  See the module docstring.

    Precedence: NOT-A-GATE, FAIL, HOLD, MALFORMED, INCOMPLETE, PASS.
    """
    sources = sorted({str(s) for s in (state.get("sources") or [])}
                     or ({str(state["source"])}
                         if state.get("source") is not None else set()))
    seen = list(state.get("seen") or [])
    max_swap = int(state.get("max_vmswap_kB") or 0)
    growth = int(state.get("max_system_swap_growth_kB") or 0)
    tol = int(state.get("swap_growth_tolerance_kB") or 0)
    absent = list(state.get("absent_required_fields") or [])
    failed = bool(state.get("failed"))
    failure = state.get("failure")
    teardown_status = state.get("teardown_status")

    seq = check_sequence(seen)
    canonical = {LEGACY_CHECKPOINT_NAMES.get(c, c) for c in seen}
    missing = [c for c in CHECKPOINTS if c not in canonical]
    common = {"missing": missing, "max_vmswap_kB": max_swap,
              "max_system_swap_growth_kB": growth,
              "sequence_status": seq["status"],
              "sequence_pages": seq["pages"],
              "absent_required_fields": absent,
              "failed": failed,
              "teardown_status": teardown_status}

    # -- 1. is this a measurement at all -----------------------------------
    if sources != ["proc"]:
        return dict(common, verdict="NOT-A-GATE", reason=(
            "memory accounting source is %s, not /proc throughout; an "
            "off-board reading -- or a run whose /proc reads stopped working "
            "part way -- cannot close a board memory gate"
            % (", ".join(repr(s) for s in sources) or "unrecorded",)))
    if absent:
        return dict(common, verdict="NOT-A-GATE", reason=(
            "records claim /proc but do not carry %s; the rules that read "
            "those fields cannot fire, and a rule that cannot fire is not a "
            "rule that passed" % ", ".join(absent)))

    # -- 2. did the run do its work ----------------------------------------
    if failed:
        return dict(common, verdict="FAIL", reason=(
            "the run raised before it finished (%s); these numbers describe "
            "a run that did not do the work"
            % (failure or "exception not recorded",)))
    if teardown_status not in (None, 0):
        return dict(common, verdict="FAIL", reason=(
            "the PL teardown returned status %s; the process ended holding "
            "fabric state, which is a different memory result from one that "
            "ended clean" % (teardown_status,)))

    # -- 3. is the memory result usable ------------------------------------
    if max_swap > 0:
        return dict(common, verdict="HOLD", reason=(
            "the sampled process had %d kB resident in swap; a peak hidden "
            "behind swap is not a peak that fits" % max_swap))
    if growth > tol:
        return dict(common, verdict="HOLD", reason=(
            "system swap in use grew by %d kB during the run (tolerance "
            "%d kB); the process's own VmSwap stayed 0, so this is pressure "
            "it caused in something else, not in itself" % (growth, tol)))

    # -- 4. is it the sequence a page passes through -----------------------
    if seq["status"] == "malformed":
        return dict(common, verdict="MALFORMED", reason=(
            "the checkpoints are not a sequence a page can have passed "
            "through: %s" % seq["detail"]))
    if seq["status"] == "prefix":
        return dict(common, verdict="INCOMPLETE", reason=(
            "the checkpoint sequence stops early, at %r; missing: %s"
            % (seq["expected_next"], ", ".join(missing) or "none by name, "
               "but a per-page block is short")))

    # The reason says what was CHECKED, including the one thing that may be
    # absent rather than clean: a `teardown_complete` mark carries the
    # status as a count, and a file that has the mark without the count is
    # not evidence that the teardown was clean, only that it ran.
    return dict(common, verdict="PASS", reason=(
        "%d page block(s) in order, every required field present, no swap, "
        "teardown status %s" % (seq["pages"],
                                "not recorded" if teardown_status is None
                                else teardown_status)))


def load(path) -> List[Dict[str, object]]:
    """Every complete JSON line.  A truncated tail is DROPPED, not repaired.

    A killed process can leave a partial line; the records before it are
    still evidence, and the partial one is not.
    """
    out: List[Dict[str, object]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                break
    return out


def summarise(path) -> Dict[str, object]:
    """Recompute the verdict and the peaks from the records themselves."""
    records = load(path)
    header = next((r for r in records if r.get("record") == "header"), None)
    checks = [r for r in records if r.get("record") == "checkpoint"]
    trailer = next((r for r in records if r.get("record") == "trailer"), None)

    sources: List[str] = []
    absent: List[str] = []
    seen: List[str] = []
    max_swap = 0
    baseline_used: Optional[int] = None
    growth = 0
    peak_hwm = 0
    min_avail: Optional[int] = None
    min_cma: Optional[int] = None
    pages = set()
    windows = set()
    check_windows = set()

    ordered = ([header] if header else []) + checks
    for r in ordered:
        mem = r.get("mem") or {}
        sysinfo = r.get("sys") or {}
        window = str(r.get("peak_window", "run"))
        windows.add(window)
        if r.get("record") == "checkpoint":
            check_windows.add(window)
        src = str(mem.get("source", "none"))
        if src not in sources:
            sources.append(src)
        for field in missing_required_fields(mem, sysinfo):
            if field not in absent:
                absent.append(field)
        if isinstance(mem.get("VmSwap_kB"), int):
            max_swap = max(max_swap, mem["VmSwap_kB"])
        if isinstance(mem.get("VmHWM_kB"), int):
            peak_hwm = max(peak_hwm, mem["VmHWM_kB"])
        if isinstance(sysinfo.get("MemAvailable_kB"), int):
            v = sysinfo["MemAvailable_kB"]
            min_avail = v if min_avail is None else min(min_avail, v)
        if isinstance(sysinfo.get("CmaFree_kB"), int):
            v = sysinfo["CmaFree_kB"]
            min_cma = v if min_cma is None else min(min_cma, v)
        if isinstance(sysinfo.get("SwapTotal_kB"), int) and \
                isinstance(sysinfo.get("SwapFree_kB"), int):
            used = sysinfo["SwapTotal_kB"] - sysinfo["SwapFree_kB"]
            if baseline_used is None:
                baseline_used = used
            growth = max(growth, used - baseline_used)

    failed = False
    failure = None
    teardown_status: Optional[int] = None
    for r in checks:
        seen.append(str(r.get("checkpoint")))
        if r.get("page"):
            pages.add(str(r["page"]))
        # The runner writes the outcome into the `teardown_complete` mark
        # as well as into the trailer, and this reads whichever exists.
        flags = r.get("flags") or {}
        counts = r.get("counts") or {}
        if flags.get("failed"):
            failed = True
        if flags.get("failure") is not None:
            failure = str(flags["failure"])
        if isinstance(counts.get("teardown_status"), int):
            if teardown_status in (None, 0):
                teardown_status = int(counts["teardown_status"])

    tol = 0
    if trailer:
        tol = int((trailer.get("verdict") or {}).get(
            "swap_growth_tolerance_kB", 0) or 0)
        tfacts = trailer.get("facts") or {}
        if tfacts.get("failed"):
            failed = True
        if isinstance(tfacts.get("teardown_status"), int):
            if teardown_status in (None, 0):
                teardown_status = int(tfacts["teardown_status"])

    v = verdict_from_records({"sources": sources, "seen": seen,
                              "max_vmswap_kB": max_swap,
                              "max_system_swap_growth_kB": growth,
                              "swap_growth_tolerance_kB": tol,
                              "absent_required_fields": absent,
                              "failed": failed, "failure": failure,
                              "teardown_status": teardown_status})

    # VmHWM is per PROCESS.  With two pages in one file the peak belongs to
    # whichever ran first, and reporting it against either is wrong.
    attributable = len(pages) <= 1
    out = {
        "path": str(path),
        "pages": sorted(pages),
        "peak_attributable_to_one_page": attributable,
        "peak_VmHWM_kB": peak_hwm if attributable else None,
        "peak_VmHWM_kB_process": peak_hwm,
        # "run" means every row after the first big allocation reports THAT
        # allocation's peak, so a later phase's own transient is bounded
        # only from above.  "since_previous_checkpoint" means each row is
        # its own phase, and this maximum is still the run's peak.
        #
        # `peak_is_per_phase` is over the CHECKPOINT rows only, and that is
        # not a convenience.  `header()` writes its record and THEN takes
        # the first reset, so the header's window is necessarily "run" — on
        # every run, including one where every reset succeeded.  Including
        # it made the flag unreachable, and `_report` therefore printed
        # "running peak since process start" over a column that was already
        # per-phase.  `peak_windows` keeps the union over all records so the
        # header's row is still visible rather than quietly dropped.
        "peak_windows": sorted(windows),
        "checkpoint_peak_windows": sorted(check_windows),
        "peak_is_per_phase": check_windows == {"since_previous_checkpoint"},
        "min_MemAvailable_kB": min_avail,
        "min_CmaFree_kB": min_cma,
        "checkpoints_seen": seen,
        "sources": sources,
        "records": len(records),
        "truncated": trailer is None,
        "env": (header or {}).get("env"),
    }
    out.update(v)
    return out


def _fmt_mib(kb) -> str:
    return "      -" if kb is None else "%7.1f" % (kb / 1024.0)


def _report(path) -> str:
    s = summarise(path)
    records = load(path)
    print("== %s" % s["path"])
    env = s.get("env") or {}
    fitz_env = env.get("fitz") if isinstance(env.get("fitz"), dict) else {}
    numpy_env = env.get("numpy") if isinstance(env.get("numpy"), dict) else {}
    cv2_env = env.get("cv2") if isinstance(env.get("cv2"), dict) else {}
    print("   %s  python %s  machine %s"
          % (env.get("platform", "?"), env.get("python", "?"),
             env.get("machine", "?")))
    print("   numpy %s  cv2 %s  pymupdf %s / mupdf %s  samples_mv=%s"
          % (numpy_env.get("__version__", "?"),
             cv2_env.get("__version__", "?"),
             fitz_env.get("VersionBind", "?"),
             fitz_env.get("VersionFitz", "?"),
             env.get("samples_mv")))
    print()
    per_phase = s["peak_is_per_phase"]
    print("   HWM column: %s"
          % ("PER-PHASE peak (VmHWM reset after each record)" if per_phase
             else "running peak since process start -- a phase after the "
                  "render is bounded only from above"))
    print("   %-20s %8s %8s %8s %10s %11s %11s %9s"
          % ("checkpoint", "RSS MiB", "HWM MiB", "swap kB", "avail MiB",
             "cmafree MiB", "arrays MiB", "t s"))
    for r in records:
        if r.get("record") != "checkpoint":
            continue
        mem = r.get("mem") or {}
        sysinfo = r.get("sys") or {}
        arrays = r.get("arrays") or {}
        dist = arrays.get("distinct_bytes")
        print("   %-20s %8s %8s %8s %10s %11s %11s %9.3f"
              % (r.get("checkpoint", "?"),
                 _fmt_mib(mem.get("VmRSS_kB")),
                 _fmt_mib(mem.get("VmHWM_kB")),
                 mem.get("VmSwap_kB", "-"),
                 _fmt_mib(sysinfo.get("MemAvailable_kB")),
                 _fmt_mib(sysinfo.get("CmaFree_kB")),
                 "-" if dist is None else "%7.1f" % (dist / 1048576.0),
                 r.get("t_mono_s", 0.0)))
    print()
    if not s["peak_attributable_to_one_page"]:
        print("   pages in this file: %s -- VmHWM is a PER-PROCESS "
              "high-water mark, so the peak below belongs to whichever page "
              "ran first and is NOT attributable"
              % ", ".join(s["pages"]))
    print("   peak VmHWM       %s MiB" % _fmt_mib(s["peak_VmHWM_kB_process"]))
    print("   min MemAvailable %s MiB" % _fmt_mib(s["min_MemAvailable_kB"]))
    print("   min CmaFree      %s MiB" % _fmt_mib(s["min_CmaFree_kB"]))
    print("   sequence         %s over %d page block(s)"
          % (s["sequence_status"], s["sequence_pages"]))
    print("   accounting       source(s) %s%s"
          % (", ".join(s["sources"]) or "none",
             "" if not s["absent_required_fields"]
             else "; MISSING REQUIRED " + ", ".join(
                 s["absent_required_fields"])))
    print("   run outcome      failed=%s teardown_status=%s"
          % (s["failed"],
             "not recorded" if s["teardown_status"] is None
             else s["teardown_status"]))
    if s["truncated"]:
        print("   NO TRAILER: this file ends without a close() record -- the "
              "process did not finish (an OOM kill leaves exactly this)")
    print("   VERDICT %s: %s" % (s["verdict"], s["reason"]))
    return str(s["verdict"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Summarise checkpoint memory sampler files.")
    ap.add_argument("jsonl", nargs="+", help="sampler output files")
    ap.add_argument("--json", action="store_true",
                    help="emit the summaries as JSON instead of a table")
    ap.add_argument("--require-pass", action="store_true",
                    help="exit non-zero unless every file reads PASS")
    args = ap.parse_args(argv)

    if args.json:
        summaries = [summarise(p) for p in args.jsonl]
        print(json.dumps(summaries, indent=1))
        verdicts = [str(s["verdict"]) for s in summaries]
    else:
        verdicts = []
        for p in args.jsonl:
            verdicts.append(_report(p))
            print()
    if args.require_pass and any(v != "PASS" for v in verdicts):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
