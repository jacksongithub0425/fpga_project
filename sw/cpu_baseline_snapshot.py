#!/usr/bin/env python3
"""Capture — and later re-check — the CPU detector's exact output.

This exists to be run BEFORE any PL stage is wired into `detect_page()`, so
that "the FPGA path agrees with the CPU baseline" is a claim against a
recorded artifact rather than against memory.

    # capture (do this before integration)
    python cpu_baseline_snapshot.py capture "../../sample/*" \
        --out ../../baseline_cpu_20260811

    # re-check after wiring in a PL stage; exit 1 on any divergence
    python cpu_baseline_snapshot.py compare "../../sample/*" \
        --manifest cpu_baseline_20260811.csv

Quote the pattern. Globs are expanded by this script, not by the shell —
PowerShell does not expand wildcards for native programs at all, and a bare
`*.PDF` misses lowercase `.pdf` on a case-sensitive filesystem. Matching here
is case-insensitive and a pattern that matches nothing is an error.

`compare` requires FULL coverage: every page in the manifest must be produced
exactly once, and missing, extra or duplicated pages each fail. That is
deliberate — a parity check that quietly narrows its own scope reports green
while proving nothing. `--allow-subset` relaxes it and says so in the output;
it is not a gate.

WHAT IS RETAINED WHERE, AND WHY IT IS SPLIT

The source drawings are confidential and the repository excludes them, along
with anything rendered from them. Detection records carry pin labels lifted
off the page, so they are in that class too. But a digest of them is not:

  - `--out` (LOCAL, not committed): the full per-page detection dump, one
    JSON per page, with every box, score and label. This is the working
    artifact for diagnosing a mismatch.
  - `baseline_manifest.csv` (COMMITTABLE): one row per page carrying an
    anonymized id, the SHA-256 of the input PDF, the per-kind counts, and a
    SHA-256 over the canonical serialization of the whole detection list.

The digest is what makes the manifest a real parity oracle: counts alone
cannot see a box that moved, a score that shifted, or two detections that
swapped kinds while the totals stayed put. Matching digests mean the two
runs produced byte-identical detections, without publishing what they were.
Anonymized ids follow the convention already used by
`ground_truth_template.csv` (example_01.pdf), so the manifest can be read
and diffed by anyone without access to the drawings.

Scores are rounded to 6 decimals before hashing. They come from
`cv2.matchTemplate` on the CPU path, and an unrounded float64 would make the
digest sensitive to numerically irrelevant last-bit differences between
OpenCV builds — which would turn every environment change into a false
parity failure.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCORE_DECIMALS = 6


def expand_inputs(patterns) -> list:
    """Resolve command-line inputs to a de-duplicated, sorted PDF list.

    Globs are expanded HERE rather than left to the shell, because the shell
    cannot be relied on to do it:

    - PowerShell does not expand wildcards for native programs, so Python
      receives the literal string `*.PDF` and would silently process nothing;
    - on a case-sensitive filesystem `*.PDF` misses `foo.pdf`, and this
      corpus genuinely contains both spellings.

    Matching is therefore case-insensitive on the suffix, and a pattern that
    matches nothing is an error rather than an empty run — "PARITY OK over
    zero pages" is the failure mode this whole function exists to prevent.
    """
    out: dict = {}
    for pattern in patterns:
        p = Path(pattern)
        if p.exists() and p.is_file():
            matches = [p]
        else:
            parent = p.parent if str(p.parent) else Path(".")
            if not parent.is_dir():
                raise SystemExit(
                    f"input pattern {pattern!r}: the directory {parent} does "
                    f"not exist. (A POSIX-style path such as /c/Users/... "
                    f"will not resolve under native Windows Python — use "
                    f"C:/Users/... or a relative path.)")
            pat = p.name.lower()
            matches = [c for c in parent.iterdir()
                       if c.is_file() and fnmatch(c.name.lower(), pat)]
        if not matches:
            raise SystemExit(
                f"input pattern {pattern!r} matched no files. (If your shell "
                f"does not expand wildcards — PowerShell does not — the "
                f"pattern arrives here literally; that is handled, so this "
                f"means nothing on disk matches it.)")
        for m in matches:
            out[m.resolve()] = None
    return sorted(out)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_detections(detections) -> list:
    """Order- and float-stable serialization of one page's detections.

    Sorted by the COMPLETE record rather than by geometry alone, so the
    ordering is total: two detections sharing a box and kind but differing
    in pin, side or score would otherwise sort by an equal key, and Python's
    stable sort would then preserve the detector's incoming order — making
    the digest depend on the very ordering this canonicalisation exists to
    remove.  Sorting on every field cannot tie unless the records are
    identical, in which case the order between them is immaterial.

    Sorted rather than trusting the detector's own order because a PL path
    has no obligation to preserve it and that is not what parity is about;
    `detect_page` happens to sort by (y, x).
    """
    rows = []
    for d in detections:
        rows.append([
            int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]),
            str(d["kind"]),
            str(d.get("pin") or ""),
            str(d.get("side") or ""),
            round(float(d["score"]), SCORE_DECIMALS),
        ])
    rows.sort(key=lambda r: (r[1], r[0], r[2], r[3], r[4], r[5], r[6], r[7]))
    return rows


def detections_digest(detections) -> str:
    blob = json.dumps(canonical_detections(detections), separators=(",", ":"),
                      sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_one(pdf_path: Path, side_templates, args):
    """Yield (page_index, detections, candidate_count) for every page."""
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        from terminal_counter_endpoint_first import detect_page
        for page_index in range(doc.page_count):
            page = doc[page_index]
            _bgr, candidates, detections = detect_page(
                page, side_templates,
                zoom=args.zoom,
                score_thresh=args.score_thresh,
                ferrule_score_thresh=args.ferrule_score_thresh,
                score_margin=args.score_margin,
            )
            yield page_index, detections, len(candidates)
    finally:
        doc.close()


def load_templates(args):
    from terminal_counter_endpoint_first import (build_side_templates,
                                                 load_template_bank)
    base = Path(__file__).resolve().parent
    return build_side_templates(
        load_template_bank(str(base / "male_ter" / "male_left.png")),
        load_template_bank(str(base / "male_ter" / "male_right.png")),
        load_template_bank(str(base / "female_ter" / "female_left.png")),
        load_template_bank(str(base / "female_ter" / "female_right.png")),
        load_template_bank(str(base / "ferrule_ter" / "ferrule_left.png")),
        load_template_bank(str(base / "ferrule_ter" / "ferrule_right.png")),
    )


def counts_of(detections) -> dict:
    out = {"male": 0, "female": 0, "ferrule": 0, "unknown": 0}
    for d in detections:
        out[d["kind"]] = out.get(d["kind"], 0) + 1
    return out


FIELDS = ["anon_id", "page", "input_sha256", "male", "female", "ferrule",
          "unknown", "total", "candidates", "detections_sha256"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _reject_in_repo(out_dir: Path) -> None:
    """Refuse to write the per-page dumps anywhere inside the repository.

    The dumps carry pin labels read off confidential drawings.  The repo
    excludes that class of data, and a `--out` pointed at `sw/` (an easy
    slip, since that is where this script lives) would drop 36 JSON files
    straight into the tree, where the next `git add` sweeps them up.  The
    `.gitignore` rule is the backstop; this is the part that fails loudly
    instead of relying on someone reading the diff.
    """
    root = _repo_root().resolve()
    try:
        out_dir.resolve().relative_to(root)
    except ValueError:
        return                                    # outside the repo: fine
    raise SystemExit(
        f"refusing to write detection dumps to {out_dir} — that is inside "
        f"the repository ({root}), and the dumps carry labels read off "
        f"confidential drawings. Point --out somewhere outside the repo; "
        f"only the manifest is meant to be committed.")


def _provenance(args, inputs) -> dict:
    """What the baseline was produced by, so a mismatch can be explained.

    A digest mismatch has two possible causes — the detector changed, or the
    parameters did — and without this the two are indistinguishable after
    the fact.
    """
    # The revision alone would be misleading: this tree is a linked git
    # worktree and can carry uncommitted edits, so a bare hash implies a
    # reproducibility that does not exist. Mark a dirty tree as such, and
    # keep detector_sha256 below as the field that actually identifies the
    # code the digests came from.
    rev = "unknown"
    try:
        root = str(_repo_root())
        rev = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=20).stdout.strip()
        if dirty and rev != "unknown":
            rev += "-dirty"
    except Exception:                                  # noqa: BLE001
        pass
    detector = _repo_root() / "sw" / "terminal_counter_endpoint_first.py"
    return {
        "captured_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_revision": rev,
        "detector_sha256": _sha256_file(detector),
        "snapshot_tool_sha256": _sha256_file(Path(__file__).resolve()),
        "zoom": args.zoom,
        "score_thresh": args.score_thresh,
        "ferrule_score_thresh": args.ferrule_score_thresh,
        "score_margin": args.score_margin,
        "score_decimals": SCORE_DECIMALS,
        "input_count": len(inputs),
        "python": sys.version.split()[0],
        "numpy": __import__("numpy").__version__,
        "opencv": __import__("cv2").__version__,
    }


def cmd_capture(args) -> int:
    out_dir = Path(args.out)
    _reject_in_repo(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    side_templates = load_templates(args)
    inputs = expand_inputs(args.pdfs)

    rows = []
    for i, pdf in enumerate(inputs, start=1):
        anon = f"example_{i:02d}.pdf"
        digest_in = _sha256_file(pdf)
        print(f"{pdf.name}  ->  {anon}")
        for page_index, detections, n_cand in run_one(pdf, side_templates,
                                                      args):
            c = counts_of(detections)
            row = {
                "anon_id": anon,
                "page": page_index,
                "input_sha256": digest_in,
                "male": c["male"], "female": c["female"],
                "ferrule": c["ferrule"], "unknown": c["unknown"],
                "total": sum(c.values()),
                "candidates": n_cand,
                "detections_sha256": detections_digest(detections),
            }
            rows.append(row)
            # Full dump stays local: it carries labels off the drawing.
            dump = out_dir / f"{pdf.stem}_p{page_index}_detections.json"
            dump.write_text(json.dumps(canonical_detections(detections),
                                       indent=1), encoding="utf-8")
            print(f"  page {page_index}: male={c['male']} "
                  f"female={c['female']} ferrule={c['ferrule']} "
                  f"unknown={c['unknown']} cands={n_cand} "
                  f"digest={row['detections_sha256'][:12]}")

    manifest = out_dir / "baseline_manifest.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    prov = _provenance(args, inputs)
    prov["page_count"] = len(rows)
    prov_path = out_dir / "baseline_provenance.json"
    prov_path.write_text(json.dumps(prov, indent=2), encoding="utf-8")

    print(f"\nwrote {manifest} ({len(rows)} page rows)")
    print(f"wrote {prov_path}")
    for k in ("captured_utc", "git_revision", "detector_sha256", "zoom",
              "score_thresh", "ferrule_score_thresh", "score_margin",
              "opencv"):
        v = prov[k]
        print(f"  {k:<22} {v[:16] if k.endswith('sha256') else v}")
    print("\nCommit the manifest AND the provenance file; the "
          "*_detections.json dumps stay local — they carry labels read off "
          "the drawings.")
    return 0


def cmd_compare(args) -> int:
    """Re-check against the baseline, requiring FULL coverage by default.

    Coverage is the point.  An earlier version skipped any page it could not
    match and then reported "PARITY OK" over whatever was left — so a single
    matching page, or a glob the shell never expanded, produced a green
    result that proved almost nothing.  A parity check that silently shrinks
    its own scope is worse than no check, because it reads as evidence.

    So: every page key in the manifest must be visited exactly once.
    Missing, extra and duplicate pages are each a failure with its own
    message.  `--allow-subset` relaxes it to "everything present must match,
    and coverage is reported" — explicitly non-gating, and labelled as such
    in the output so it cannot be mistaken for the real thing.
    """
    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"CANNOT RUN: {manifest} not found — capture a baseline first")
        return 2
    with open(manifest, newline="", encoding="utf-8") as fh:
        expected = list(csv.DictReader(fh))
    if not expected:
        print(f"CANNOT RUN: {manifest} has no rows")
        return 2

    # Key by (input_sha256, page): the anonymized id is a label, the hash is
    # the identity. A renamed or re-exported PDF must not silently match.
    want = {(r["input_sha256"], int(r["page"])): r for r in expected}
    side_templates = load_templates(args)
    inputs = expand_inputs(args.pdfs)

    seen: dict = {}
    diffs, extras, dupes = [], [], []
    for pdf in inputs:
        digest_in = _sha256_file(pdf)
        for page_index, detections, n_cand in run_one(pdf, side_templates,
                                                      args):
            key = (digest_in, page_index)
            if key not in want:
                extras.append(f"{pdf.name} p{page_index} "
                              f"(sha {digest_in[:12]}) is not in the baseline")
                continue
            if key in seen:
                dupes.append(f"{want[key]['anon_id']} p{page_index} was "
                             f"produced twice — by {seen[key]} and {pdf.name}")
                continue
            seen[key] = pdf.name

            exp = want[key]
            got = detections_digest(detections)
            c = counts_of(detections)
            if got != exp["detections_sha256"]:
                diffs.append(
                    f"{exp['anon_id']} p{page_index}: detections digest "
                    f"{got[:12]} != baseline {exp['detections_sha256'][:12]} "
                    f"(counts now male={c['male']} female={c['female']} "
                    f"ferrule={c['ferrule']} unknown={c['unknown']}; baseline "
                    f"male={exp['male']} female={exp['female']} "
                    f"ferrule={exp['ferrule']} unknown={exp['unknown']})")
            else:
                print(f"  ok   {exp['anon_id']} p{page_index}  "
                      f"digest {got[:12]}")

    missing = [f"{r['anon_id']} p{r['page']} (sha {r['input_sha256'][:12]})"
               for r in expected
               if (r["input_sha256"], int(r["page"])) not in seen]

    total = len(want)
    print(f"\ncoverage: {len(seen)}/{total} baseline pages checked "
          f"from {len(inputs)} input file(s)")

    problems = []
    if diffs:
        problems.append(("DIVERGED", diffs))
    if not args.allow_subset:
        if missing:
            problems.append(("MISSING (not produced by these inputs)", missing))
        if extras:
            problems.append(("EXTRA (produced but not in the baseline)", extras))
    if dupes:
        problems.append(("DUPLICATE", dupes))

    if problems:
        print(f"\nPARITY FAILED:")
        for title, items in problems:
            print(f"  {title}: {len(items)}")
            for it in items:
                print(f"    - {it}")
        if missing and not args.allow_subset:
            print("\n  (If a partial check is what you meant, pass "
                  "--allow-subset — it is explicitly NOT a gate.)")
        return 1

    if args.allow_subset and (missing or extras):
        print(f"\nSUBSET OK (NOT A GATE): {len(seen)}/{total} pages matched; "
              f"{len(missing)} baseline page(s) were not exercised"
              + (f" and {len(extras)} input page(s) are not in the baseline"
                 if extras else "") + ".")
        print("This does not establish CPU parity — re-run without "
              "--allow-subset over the full corpus before relying on it.")
        return 0

    print(f"\nPARITY OK: all {total} baseline pages checked and "
          f"byte-identical to the CPU baseline.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("capture", "compare"):
        p = sub.add_parser(name)
        p.add_argument("pdfs", nargs="+")
        p.add_argument("--zoom", type=float, default=4.0)
        p.add_argument("--score-thresh", type=float, default=0.33)
        p.add_argument("--ferrule-score-thresh", type=float, default=0.24)
        p.add_argument("--score-margin", type=float, default=0.03)
        if name == "capture":
            p.add_argument("--out", required=True,
                           help="local directory for the full dumps and the "
                                "manifest (MUST be outside the repo)")
        else:
            p.add_argument("--manifest", required=True)
            p.add_argument("--allow-subset", action="store_true",
                           help="do not require full baseline coverage. "
                                "Explicitly NOT a gate: everything present "
                                "must still match, but missing pages are "
                                "only reported.")
    args = ap.parse_args()
    return cmd_capture(args) if args.cmd == "capture" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
