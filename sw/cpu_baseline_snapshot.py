#!/usr/bin/env python3
"""Capture — and later re-check — the CPU detector's exact output.

This exists to be run BEFORE any PL stage is wired into `detect_page()`, so
that "the FPGA path agrees with the CPU baseline" is a claim against a
recorded artifact rather than against memory.

    # capture (do this before integration)
    python cpu_baseline_snapshot.py capture ../../sample/*.PDF \
        --out ../../baseline_cpu_20260811

    # re-check after wiring in a PL stage; exit 1 on any divergence
    python cpu_baseline_snapshot.py compare ../../sample/*.PDF \
        --manifest ../../baseline_cpu_20260811/baseline_manifest.csv

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCORE_DECIMALS = 6


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_detections(detections) -> list:
    """Order- and float-stable serialization of one page's detections.

    Sorted by geometry rather than trusting the detector's own ordering, so
    a pure reordering is not reported as a divergence — `detect_page`
    already sorts by (y, x), but a PL path has no obligation to preserve
    that and it is not what parity is about.
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
    rows.sort(key=lambda r: (r[1], r[0], r[2], r[3], r[4]))
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


def cmd_capture(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    side_templates = load_templates(args)

    rows = []
    for i, pdf in enumerate(sorted(Path(p) for p in args.pdfs), start=1):
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
    print(f"\nwrote {manifest} ({len(rows)} page rows)")
    print("The manifest is safe to commit; the *_detections.json dumps are "
          "not — they carry labels read off the drawings.")
    return 0


def cmd_compare(args) -> int:
    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"CANNOT RUN: {manifest} not found — capture a baseline first")
        return 2
    with open(manifest, newline="", encoding="utf-8") as fh:
        expected = list(csv.DictReader(fh))

    # Key by (input_sha256, page): the anonymized id is a label, the hash is
    # the identity. A renamed or re-exported PDF must not silently match.
    want = {(r["input_sha256"], int(r["page"])): r for r in expected}
    side_templates = load_templates(args)

    checked = 0
    diffs = []
    for pdf in sorted(Path(p) for p in args.pdfs):
        digest_in = _sha256_file(pdf)
        for page_index, detections, n_cand in run_one(pdf, side_templates,
                                                      args):
            key = (digest_in, page_index)
            if key not in want:
                print(f"  SKIP {pdf.name} p{page_index}: not in the baseline "
                      f"(input sha {digest_in[:12]})")
                continue
            checked += 1
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

    if not checked:
        print("CANNOT RUN: no input matched the baseline manifest by hash")
        return 2
    if diffs:
        print(f"\nPARITY FAILED: {len(diffs)} of {checked} pages diverge from "
              f"the CPU baseline:")
        for d in diffs:
            print(f"  - {d}")
        return 1
    print(f"\nPARITY OK: {checked} pages byte-identical to the CPU baseline.")
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
                                "manifest (keep outside the repo)")
        else:
            p.add_argument("--manifest", required=True)
    args = ap.parse_args()
    return cmd_capture(args) if args.cmd == "capture" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
