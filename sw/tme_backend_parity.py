#!/usr/bin/env python3
"""Walk the backend parity ladder on real pages and report each rung.

    python tme_backend_parity.py "../../sample/<one document>.PDF"
    python tme_backend_parity.py "../../sample/*" --pages 1 --json out.json
    python tme_backend_parity.py <pdf> --backends cpu cpu-sidebank
    python tme_backend_parity.py <pdf> --backends pl-extract pl-all --overlay …

WHY A LADDER AND NOT A PASS/FAIL
--------------------------------
`pl-all` differs from the frozen 36-page CPU oracle in three independent
ways, and a single "does it match the oracle" answer cannot tell them apart
— which means a real matcher fault would be filed under "expected geometry
difference" and never looked at again.  Each rung changes exactly one thing
(see `pl_backends.__doc__` for the arithmetic):

    A  cpu          -> pl-binarize    the binariser        EXPECTED to differ
    B  pl-binarize  -> pl-extract     the organisation     EXPECTED to differ
    C  pl-extract   -> pl-all         the matcher silicon  MUST be identical

Off the board only rung B is walkable, via the `cpu-sidebank` diagnostic:
`cpu` -> `cpu-sidebank` is the SAME organisation change as rung B with the
binariser held fixed, so its size can be measured, reviewed and argued about
before any board time is spent.

WHAT "IDENTICAL" MEANS HERE
---------------------------
The board PASS criterion, unchanged: **exact (x, y)** and
**|score - gold| <= 0.005**.  Never "N/N exact score" — scores are compared
to a tolerance and locations are not.  Per-page class counts are reported
too, but they are a summary: two pages can carry the same counts with
different boxes, so counts alone are never the verdict.

`--assert-rung-c` makes a rung-C difference exit non-zero.  There is
deliberately no `--assert-rung-a/b`: those rungs are measurements, and a
flag that turned a measurement into a pass would invite pinning whatever
number came out first.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import fitz

import corpus_labels as CL
import pl_backends
import terminal_counter_endpoint_first as det

SCORE_TOL = 0.005          # the board PASS criterion, not a display precision

#: The authoritative corpus, pinned so "36/36" cannot be reached by a glob
#: that expanded differently.  35 files and 36 pages, because exactly one PDF
#: (`doc_001`, whose pages are `page_001` and `page_002`) has two pages --
#: checked, not assumed.
CORPUS_PDFS = 35
CORPUS_PAGES = 36

#: Which rung each ordered pair is, when the tool is asked for both ends.
RUNGS = {
    ("cpu", "pl-binarize"):        ("A", "the binariser", False),
    ("cpu", "cpu-sidebank"):       ("B'", "the organisation, binariser held", False),
    ("pl-binarize", "pl-extract"): ("B", "the organisation", False),
    ("cpu-sidebank", "pl-extract"): ("A''", "the binariser, organisation held", False),
    ("pl-extract", "pl-all"):      ("C", "the matcher silicon", True),
    ("cpu-sidebank", "pl-all"):    ("C'", "binariser + silicon, organisation held", False),
    # The rung a B2/100 board run has to pass 36/36.  Both sides are
    # production semantics -- the core's own binariser arithmetic, the PL's
    # patch organisation, the same trial order and tie rule, host refinement
    # -- so the ONLY thing that differs is which chip ran them.  There is no
    # expected arithmetic difference left to file a fault under, which is
    # exactly why `cpu` cannot be the oracle and this can.
    ("cpu-production", "pl-all"):  ("P", "the fabric, production semantics held", True),
    ("cpu-production", "pl-extract"): ("P-", "extractor + binariser silicon", False),
}


def load_templates() -> dict:
    base = Path(__file__).resolve().parent
    return det.build_side_templates(
        det.load_template_bank(str(base / "male_ter" / "male_left.png")),
        det.load_template_bank(str(base / "male_ter" / "male_right.png")),
        det.load_template_bank(str(base / "female_ter" / "female_left.png")),
        det.load_template_bank(str(base / "female_ter" / "female_right.png")),
        det.load_template_bank(str(base / "ferrule_ter" / "ferrule_left.png")),
        det.load_template_bank(str(base / "ferrule_ter" / "ferrule_right.png")),
    )


def build_backend(name: str, args):
    """One backend for the WHOLE run, armed and gated before any transfer.

    Built once rather than once per PDF, for three reasons: a corpus run
    should load the overlay once, the call counters in `describe()` are only
    meaningful when they cover the run being reported, and the teardown has
    exactly one owner instead of 35.
    """
    if name == "cpu":
        return None, None
    pl = None
    if args.fake_pl:
        # The arithmetically-exact stand-in from the off-board suite. It
        # makes the WHOLE ladder walkable with no board, which is how the
        # expected shape of each rung gets agreed BEFORE board time is
        # spent -- but it proves nothing about silicon, and the banner
        # below says so on every run so a transcript cannot be mistaken
        # for a hardware one.
        from test_pl_backends import FakePL
        pl = FakePL()
    backend = pl_backends.make_backend(
        name, overlay=args.overlay, pl=pl, timeout_s=args.pl_timeout,
        rung_c_inline=args.rung_c_inline and name == "pl-all")
    teardown = None
    if backend.pl is not None and not args.fake_pl:
        # Real silicon only.  The fake pipeline has no CMA pages to protect
        # and no overlay to identify, and arming the signal blocks around it
        # would make an off-board run un-interruptible for nothing.
        #
        # Before the first transfer, and before the first page: signals
        # blocked so a SIGTERM cannot free CMA pages mid-DMA, then the build
        # identity and the LIVE clock, so the numbers below are known to
        # describe the matcher and the frequency this run claims.
        import safe_teardown as teardown
        armed = teardown.arm_teardown_protection()
        print(f"  teardown protection: ignoring {', '.join(armed)}")
        import inspect_overlay
        gate = inspect_overlay.gate_identity_and_clock(
            backend.overlay, args.variant)
        if gate:
            status = teardown.teardown(backend.pl, args.overlay, 1)
            raise SystemExit(
                f"build identity/clock gate FAILED (teardown status "
                f"{status}), so no page was processed: " + "; ".join(gate))
    return backend, teardown


def run_backend(backend, pdf: str, side_templates: dict, args,
                pages: Optional[Sequence[int]]) -> List[dict]:
    """Every requested page under ONE backend.  Returns per-page records."""
    out: List[dict] = []
    doc = fitz.open(pdf)
    try:
        wanted = range(len(doc)) if pages is None else [p - 1 for p in pages]
        for i in wanted:
            if i < 0 or i >= len(doc):
                raise SystemExit(f"{pdf}: page {i + 1} is out of range "
                                 f"(1..{len(doc)})")
            t0 = time.perf_counter()
            # `keep_bgr=False`: this tool never draws anything, so the
            # 186 MB BGR array and the 186 MB `samples` copy would be built
            # and thrown away.  On the board that is the difference between
            # fitting in ~290 MiB of userspace and not.
            _bgr, cands, dets = det.detect_page(
                doc[i], side_templates=side_templates, zoom=args.zoom,
                score_thresh=args.score_thresh,
                ferrule_score_thresh=args.ferrule_score_thresh,
                score_margin=args.score_margin, backend=backend,
                keep_bgr=False)
            out.append({
                "page": i + 1,
                "wall_s": time.perf_counter() - t0,
                "candidates": len(cands),
                "detections": [
                    {"id": d["id"], "kind": d["kind"], "score": float(d["score"]),
                     "x": int(d["x"]), "y": int(d["y"]),
                     "w": int(d["w"]), "h": int(d["h"])}
                    for d in dets],
            })
    finally:
        doc.close()
    return out


def counts_of(dets: Sequence[dict]) -> Dict[str, int]:
    c = {"male": 0, "female": 0, "ferrule": 0, "unknown": 0}
    for d in dets:
        c[d["kind"]] = c.get(d["kind"], 0) + 1
    return c


#: What the public record says in place of the detections it drops.
PUBLIC_NOTE = (
    "Pages and documents are anonymous labels. `first_diff` held the first "
    "differing DETECTION on a page -- an id, a class, a score and a pixel box "
    "read off a confidential drawing -- and is withheld: \"REDACTED\" where a "
    "difference exists, null where none does, so \"withheld\" and \"identical\" "
    "stay distinguishable. The inline rung-C mismatch list is replaced by its "
    "count for the same reason. Every aggregate survives verbatim: "
    "loc_mismatch, kind_mismatch, score_over_tol, max_abs_score_delta, the "
    "class counts, the candidate counts and the rung-C trial totals."
)


def public_view(record: dict) -> dict:
    """The committable projection of a run record.

    TWO SERIALISATIONS, ON PURPOSE.  The private record keeps the first
    differing detection and the inline rung-C mismatch list, because that is
    what somebody debugging a real disagreement needs and it stays on this
    machine.  The public one keeps every AGGREGATE and drops every individual
    detection, because a box and a score are read off the drawing and a
    label does not anonymise them.

    Aggregating rather than deleting: the counts already in each result say
    how big the difference is, so dropping the exemplar costs the reader the
    example and not the argument.
    """
    import copy
    pub = copy.deepcopy(record)

    withheld = 0
    for r in pub.get("results", []):
        if r.get("first_diff") is not None:
            r["first_diff"] = "REDACTED"
            withheld += 1

    cc = pub.get("rung_c_inline")
    if isinstance(cc, dict) and "mismatches" in cc:
        cc["mismatch_count"] = len(cc["mismatches"])
        del cc["mismatches"]

    pub["redacted"] = PUBLIC_NOTE
    if withheld:
        pub["first_diff_records_withheld"] = withheld
    return pub


def compare_page(a: dict, b: dict) -> dict:
    """One page under two backends.  Positional, by NMS output order.

    Detections are compared in the order the detector emits them, which is
    sorted by (y, x) and then re-numbered — so a pair that agrees on every
    box agrees positionally too.  A length difference is reported as such
    rather than being aligned away: if the two runs found different numbers
    of terminals, no per-box comparison after that point means anything.
    """
    da, db = a["detections"], b["detections"]
    res = {
        "page": a["page"],
        "count_a": len(da), "count_b": len(db),
        "counts_a": counts_of(da), "counts_b": counts_of(db),
        "candidates_a": a["candidates"], "candidates_b": b["candidates"],
        "wall_a": a["wall_s"], "wall_b": b["wall_s"],
        "same_length": len(da) == len(db),
        "loc_mismatch": 0, "kind_mismatch": 0,
        "score_over_tol": 0, "max_abs_score_delta": 0.0,
        "first_diff": None,
    }
    for i, (x, y) in enumerate(zip(da, db)):
        loc_bad = (x["x"], x["y"], x["w"], x["h"]) != (y["x"], y["y"], y["w"], y["h"])
        kind_bad = x["kind"] != y["kind"]
        d = abs(x["score"] - y["score"])
        res["max_abs_score_delta"] = max(res["max_abs_score_delta"], d)
        if loc_bad:
            res["loc_mismatch"] += 1
        if kind_bad:
            res["kind_mismatch"] += 1
        if d > SCORE_TOL:
            res["score_over_tol"] += 1
        if res["first_diff"] is None and (loc_bad or kind_bad or d > SCORE_TOL):
            res["first_diff"] = {"index": i, "a": x, "b": y,
                                 "score_delta": d}
    res["identical"] = (res["same_length"] and not res["loc_mismatch"]
                        and not res["kind_mismatch"]
                        and not res["score_over_tol"])
    return res


def rung_of(a: str, b: str):
    return RUNGS.get((a, b)) or RUNGS.get((b, a)) or ("?", f"{a} -> {b}", False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+", help="PDF paths or globs")
    ap.add_argument("--backends", nargs="+", default=["cpu", "cpu-sidebank"],
                    help="two or more, compared as consecutive pairs")
    ap.add_argument("--pages", type=int, nargs="*", default=None,
                    help="1-based page numbers; default every page")
    ap.add_argument("--zoom", type=float, default=4.0)
    ap.add_argument("--score-thresh", type=float, default=0.33)
    ap.add_argument("--ferrule-score-thresh", type=float, default=0.24)
    ap.add_argument("--score-margin", type=float, default=0.03)
    ap.add_argument("--overlay", default="three_stage_combined.bit")
    ap.add_argument("--pl-timeout", type=float, default=120.0)
    ap.add_argument("--json", metavar="PATH",
                    help="write the PUBLIC record here: labels only, with "
                         "first_diff and the inline rung-C mismatches "
                         "aggregated. This is the committable one")
    ap.add_argument("--private-json", metavar="PATH",
                    help="write the FULL record here, diagnostic geometry "
                         "included. LOCAL ONLY -- the name must end in "
                         "'.private.json'")
    ap.add_argument("--assert-rung-c", action="store_true",
                    help="exit non-zero if pl-extract and pl-all differ")
    ap.add_argument("--variant", default="baseline",
                    help="which build the board must be running, from "
                         "board_expect.VARIANTS. A pl-* run gates the matcher "
                         "VLNV and the LIVE fclk0/fclk1 against this before "
                         "the first page; a B2/100 run must pass "
                         "--variant combined_b2_100")
    ap.add_argument("--require-corpus", action="store_true",
                    help=f"refuse to report unless the run covers exactly "
                         f"{CORPUS_PDFS} unique PDFs and {CORPUS_PAGES} "
                         f"pages. Without it a glob that expanded to 34 "
                         f"files, or to the same file twice, still prints a "
                         f"clean N/N")
    ap.add_argument("--rung-c-inline", action="store_true",
                    help="pl-all only: also run the CPU reduction over the "
                         "SAME extracted patch, so rung C is proved inside "
                         "one run rather than by comparing two separate "
                         "extractions that are not known to have received "
                         "the same upstream data")
    ap.add_argument("--fake-pl", action="store_true",
                    help="drive the pl-* backends with the off-board fake "
                         "fabric instead of hardware. Proves the WIRING and "
                         "the shape of each rung; proves NOTHING about "
                         "silicon")
    args = ap.parse_args()

    # The suffix IS the guard.  A private record carries per-detection
    # geometry and the anonymisation gate cannot catch it -- the boxes are
    # already label-keyed, so nothing in the file is shaped like a drawing
    # number.  Naming is the only thing that separates it from a committable
    # record, so the name is made a precondition rather than a convention.
    if args.private_json and not args.private_json.endswith(".private.json"):
        raise SystemExit(
            "--private-json must name a file ending in '.private.json'; that "
            "suffix is what keeps a record full of detection geometry out of "
            "the repository. Use --json for the committable projection.")

    if args.fake_pl:
        print("=" * 72)
        print("FAKE FABRIC. Every pl-* number below came from "
              "test_pl_backends.FakePL,")
        print("not from silicon. This run cannot qualify a board and must "
              "not be quoted")
        print("as a board result.")
        print("=" * 72)

    for b in args.backends:
        if b not in pl_backends.ALL_BACKENDS:
            raise SystemExit(f"unknown backend {b!r}; known: "
                             f"{', '.join(pl_backends.ALL_BACKENDS)}")
    if len(args.backends) < 2 and not args.rung_c_inline:
        raise SystemExit("give at least two backends to compare, or run one "
                         "with --rung-c-inline (which compares the fabric "
                         "against the CPU inside a single run)")
    if args.rung_c_inline and "pl-all" not in args.backends:
        raise SystemExit("--rung-c-inline needs pl-all: it compares the PL "
                         "matcher against the CPU on one extracted patch")

    # De-duplicated by resolved path.  On Windows `glob` matches case
    # insensitively, so `sample/*.PDF sample/*.pdf` expands to every file
    # TWICE -- and a corpus counted twice reports 72 pages, or 36 "identical"
    # pages that are 18 files compared with themselves.
    paths: List[str] = []
    seen = set()
    for pat in args.pdfs:
        hits = sorted(glob.glob(pat))
        if not hits:
            raise SystemExit(f"no PDF matched {pat!r}")
        for h in hits:
            key = str(Path(h).resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(h)

    # Labels, not filenames, from here down.  The corpus directory is
    # taken from the inputs themselves rather than configured: those
    # paths are where the documents actually are, and the label order
    # depends on nothing else.
    corpus_dir = Path(paths[0]).resolve().parent
    labelled = [CL.scrub(Path(p).name, corpus_dir) for p in paths]

    side_templates = load_templates()
    record = {"backends": args.backends, "pdfs": labelled,
              "score_tol": SCORE_TOL,
              "fabric": "FAKE (test_pl_backends.FakePL)" if args.fake_pl
                        else "hardware",
              "results": []}
    rung_c_failed = False
    rung_c_pages = 0                 # pages actually COMPARED on a must-match rung
    pages_done = 0

    backends = {}
    teardowns = {}
    for b in args.backends:
        backends[b], teardowns[b] = build_backend(b, args)

    status = 0
    failure = None
    try:
        for pdf, name in zip(paths, labelled):
            # `name` is the LABEL from here on -- it reaches stdout and
            # the per-page record, and neither may carry a filename.
            print(f"\n=== {name} ===")
            runs = {}
            for b in args.backends:
                t0 = time.perf_counter()
                runs[b] = run_backend(backends[b], pdf, side_templates, args,
                                      args.pages)
                total = sum(p["wall_s"] for p in runs[b])
                print(f"  {b:<13} {len(runs[b])} page(s), "
                      f"{total:.2f} s of page time "
                      f"(WALL, PS-side — not hardware cycles)")
            pages_done += max(len(runs[b]) for b in args.backends)

            for a, b in zip(args.backends, args.backends[1:]):
                tag, what, must_match = rung_of(a, b)
                print(f"\n  rung {tag}: {a} -> {b}  ({what})"
                      + ("   [MUST MATCH]" if must_match else "   [measurement]"))
                pages_identical = 0
                for pa, pb in zip(runs[a], runs[b]):
                    cmp = compare_page(pa, pb)
                    cmp.update({"pdf": name, "from": a, "to": b, "rung": tag})
                    record["results"].append(cmp)
                    if cmp["identical"]:
                        pages_identical += 1
                        continue
                    bits = []
                    if not cmp["same_length"]:
                        bits.append(f"{cmp['count_a']} vs {cmp['count_b']} detections")
                    if cmp["kind_mismatch"]:
                        bits.append(f"{cmp['kind_mismatch']} class")
                    if cmp["loc_mismatch"]:
                        bits.append(f"{cmp['loc_mismatch']} box")
                    if cmp["score_over_tol"]:
                        bits.append(f"{cmp['score_over_tol']} score>|{SCORE_TOL}|")
                    print(f"    page {cmp['page']:>3}: DIFFERS — "
                          + ", ".join(bits)
                          + f"; max |dscore| {cmp['max_abs_score_delta']:.6f}")
                    print(f"                 counts {cmp['counts_a']} -> {cmp['counts_b']}")
                    if must_match:
                        rung_c_failed = True
                n = min(len(runs[a]), len(runs[b]))
                if must_match:
                    rung_c_pages += n
                print(f"    {pages_identical}/{n} page(s) identical"
                      + (" — exact (x,y) and |dscore| <= 0.005"
                         if pages_identical == n else ""))
    except BaseException as exc:                             # noqa: BLE001
        failure = exc
        status = 1
    finally:
        for b in args.backends:
            if teardowns[b] is not None:
                # The teardown that reprograms the PL, not a close() whose
                # False turns into a process exit -- exiting is what releases
                # the retained pages while the fabric may still target them.
                status = teardowns[b].teardown(backends[b].pl, args.overlay,
                                               status)
            elif backends[b] is not None and not backends[b].close():
                status = status or 1

    for b in args.backends:
        if backends[b] is not None:
            print(f"\n  {b}: {backends[b].describe()}")
            cc = backends[b].rung_c_report()
            if cc is not None:
                record["rung_c_inline"] = cc
                print(f"    rung C INLINE (same patch, one run): "
                      f"{cc['candidates']} candidate(s), {cc['trials']} CPU "
                      f"trial(s), {len(cc['mismatches'])} mismatch(es), "
                      f"max |dscore| {cc['max_score_delta']:.6f}")
                if cc["mismatches"]:
                    rung_c_failed = True
                    for m in cc["mismatches"][:5]:
                        print(f"      {m}")
    if failure is not None:
        raise failure

    record["pdfs_run"] = len(paths)
    record["pages_run"] = pages_done
    record["rung_c_pages_compared"] = rung_c_pages

    corpus_bad = []
    if args.require_corpus:
        if len(paths) != CORPUS_PDFS:
            corpus_bad.append(f"{len(paths)} unique PDF(s), not {CORPUS_PDFS}")
        if pages_done != CORPUS_PAGES:
            corpus_bad.append(f"{pages_done} page(s), not {CORPUS_PAGES}")

    if args.private_json:
        # Written FIRST, so a run that produced diagnostics keeps them even
        # if the public projection then refuses to write.
        Path(args.private_json).write_text(json.dumps(record, indent=2),
                                           encoding="utf-8")
        print(f"\nwrote {args.private_json}")
        print("  PRIVATE: carries per-detection geometry. Not committable, "
              "and the '.private.json' suffix is what keeps it out.")

    if args.json:
        # The backstop, not the mechanism: the labels above are how the
        # record stays clean, and this is what makes that a property of
        # the tool rather than of whoever ran it.
        CL.write_json_checked(args.json, public_view(record))
        print(f"\nwrote {args.json}")

    if status:
        print(f"\nTEARDOWN did not complete cleanly (status {status}); this "
              f"run FAILS whatever the comparison said.")
        return status

    if corpus_bad:
        print("\nCORPUS REQUIREMENT FAILED: " + "; ".join(corpus_bad)
              + f". The authoritative corpus is {CORPUS_PDFS} unique PDFs / "
                f"{CORPUS_PAGES} pages; a partial run must not be reported "
                f"as corpus parity.")
        return 1

    if args.assert_rung_c:
        # A rung that never ran cannot have passed.  Without this, asking for
        # --assert-rung-c with `--backends cpu cpu-sidebank` exits 0 having
        # compared no silicon at all.
        inline = record.get("rung_c_inline")
        if rung_c_pages == 0 and inline is None:
            print("\nRUNG C DID NOT RUN: --assert-rung-c was given, but no "
                  "must-match rung was compared and --rung-c-inline was not "
                  "used. Compare pl-extract against pl-all, or run pl-all "
                  "with --rung-c-inline.")
            return 1
        if rung_c_failed:
            print("\nMUST-MATCH RUNG FAILED: the host and the fabric "
                  "disagree where nothing but the chip differs; there "
                  "is no expected arithmetic difference left to file "
                  "this under.")
            return 1
        where = []
        if rung_c_pages:
            where.append(f"{rung_c_pages} page(s) across the "
                         f"must-match rung(s)")
        if inline is not None:
            where.append(f"{inline['candidates']} candidate(s) inline on the "
                         f"same extracted patch")
        print("\nMUST-MATCH RUNG(S) PASSED on " + " and ".join(where) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
