#!/usr/bin/env python3
"""Batch wrapper for terminal_counter_endpoint_first.py.

Features:
- Processes either one PDF or all PDFs in a directory.
- Calls terminal_counter_endpoint_first.py as a subprocess.
- Saves output PDFs with the naming convention <PN>_modified.pdf.
- Saves per-file debug folders, stdout/stderr logs, and a summary CSV.
- Optionally compares results against expected_result.xlsx and adds truth-aware
  columns plus a MATCH_CLEAN summary row.
- Optionally filters input PDFs by part-number pattern such as 225-XXXXXX-XXX.

Status values in the summary CSV are operational only:
- CLEAN_NO_UNKNOWN: script completed and no unknown detections were reported.
- HAS_UNKNOWN: script completed but at least one page had unknown detections.
- NO_DETECTIONS: script completed but total detections were zero.
- FAIL: subprocess failed.
- FAIL_PARSE: subprocess returned 0 but the totals could not be parsed.

Important:
These statuses do NOT prove the counts are correct. They only show whether the
run completed cleanly and whether the detector reported obvious uncertainty.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None


PAGE_RE = re.compile(
    r"Page\s+(?P<page>\d+):\s+male=(?P<male>\d+),\s+female=(?P<female>\d+),\s+"
    r"ferrule=(?P<ferrule>\d+),\s+unknown=(?P<unknown>\d+),\s+candidates=(?P<candidates>\d+)"
)
DOC_TOTAL_RE = re.compile(
    r"Document total:\s+male=(?P<male>\d+),\s+female=(?P<female>\d+),\s+"
    r"ferrule=(?P<ferrule>\d+),\s+unknown=(?P<unknown>\d+),\s+total=(?P<total>\d+)"
)
GENERAL_PN_RE = re.compile(r"^[A-Z0-9]{3}-[A-Z0-9]{6}-[A-Z0-9]{3}$", re.IGNORECASE)
PART_NUMBER_IN_STEM_RE = re.compile(r"([A-Z0-9]{3}-[A-Z0-9]{6}-[A-Z0-9]{3})", re.IGNORECASE)


@dataclass
class RunResult:
    row_type: str
    input_pdf: str
    part_number: str
    output_pdf: str
    debug_dir: str
    stdout_log: str
    stderr_log: str
    status: str
    return_code: int
    page_count: int
    pages_processed: int
    pages_with_unknown: int
    max_unknown_on_page: int
    max_candidates_on_page: int
    male_total: Optional[int]
    female_total: Optional[int]
    ferrule_total: Optional[int]
    unknown_total: Optional[int]
    total_detected: Optional[int]
    needs_review: int
    review_reason: str
    truth_available: int
    male_true: Optional[int]
    female_true: Optional[int]
    ferrule_true: Optional[int]
    male_err: Optional[int]
    female_err: Optional[int]
    ferrule_err: Optional[int]
    count_abs_error: Optional[int]
    match_status: str
    message: str


@dataclass
class ParsedStdout:
    male_total: Optional[int]
    female_total: Optional[int]
    ferrule_total: Optional[int]
    unknown_total: Optional[int]
    total_detected: Optional[int]
    pages_processed: int
    pages_with_unknown: int
    max_unknown_on_page: int
    max_candidates_on_page: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run terminal_counter_endpoint_first.py on one PDF or a whole directory."
    )
    parser.add_argument(
        "input_path",
        help="Input PDF file or directory containing PDF files.",
    )
    parser.add_argument("male_left_template", help="Base template path or directory, e.g. male_left.png")
    parser.add_argument("male_right_template", help="Base template path or directory, e.g. male_right.png")
    parser.add_argument("female_left_template", help="Base template path or directory, e.g. female_left.png")
    parser.add_argument("female_right_template", help="Base template path or directory, e.g. female_right.png")
    parser.add_argument("ferrule_left_template", help="Base template path or directory, e.g. ferrule_left.png")
    parser.add_argument("ferrule_right_template", help="Base template path or directory, e.g. ferrule_right.png")
    parser.add_argument(
        "--detector-script",
        default="terminal_counter_endpoint_first.py",
        help="Path to terminal_counter_endpoint_first.py",
    )
    parser.add_argument(
        "--output-dir",
        default="batch_terminal_counter_output",
        help="Root directory for output PDFs, debug images, logs, and summary CSV.",
    )
    parser.add_argument(
        "--summary-csv",
        default="summary.csv",
        help="Summary CSV filename inside --output-dir.",
    )
    parser.add_argument(
        "--expected-xlsx",
        default="",
        help=(
            "Optional workbook path such as expected_result.xlsx. "
            "When provided, summary.csv includes truth-aware columns and a MATCH_CLEAN summary row. "
            "If omitted, the script will auto-use expected_result.xlsx when it can find it."
        ),
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search for PDFs recursively when input_path is a directory.",
    )
    parser.add_argument(
        "--zoom",
        type=float,
        default=4.0,
        help="Render zoom passed to terminal_counter_endpoint_first.py",
    )
    parser.add_argument(
        "--score-thresh",
        type=float,
        default=0.33,
        help="Male/female threshold passed to terminal_counter_endpoint_first.py",
    )
    parser.add_argument(
        "--ferrule-score-thresh",
        type=float,
        default=0.24,
        help="Ferrule threshold passed to terminal_counter_endpoint_first.py",
    )
    parser.add_argument(
        "--score-margin",
        type=float,
        default=0.03,
        help="Winner margin passed to terminal_counter_endpoint_first.py",
    )
    parser.add_argument(
        "--pn-prefix",
        default="225",
        help=(
            "Only process PDFs whose stem matches <prefix>-XXXXXX-XXX. "
            "Use empty string to disable the prefix filter while still keeping the general PN pattern check."
        ),
    )
    parser.add_argument(
        "--allow-nonmatching",
        action="store_true",
        help="Process PDFs even if their stem does not match the expected PN pattern.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Reserved for future parallel runs. Right now only 1 is supported.",
    )
    return parser.parse_args()


def normalize_stem(stem: str) -> str:
    return stem.strip().upper()


def extract_part_number(stem: str) -> Optional[str]:
    norm = normalize_stem(stem)
    match = PART_NUMBER_IN_STEM_RE.search(norm)
    if match is None:
        return None
    return match.group(1).upper()


def stem_matches_expected_pattern(stem: str, pn_prefix: str) -> bool:
    part_number = extract_part_number(stem)
    if part_number is None:
        return False
    if pn_prefix:
        return part_number.startswith(f"{pn_prefix.upper()}-")
    return True


def collect_pdfs(input_path: Path, recursive: bool) -> List[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            raise ValueError(f"Input file is not a PDF: {input_path}")
        return [input_path]

    if not input_path.is_dir():
        raise ValueError(f"Input path does not exist or is not a file/directory: {input_path}")

    walker = input_path.rglob("*") if recursive else input_path.iterdir()
    return sorted(
        p for p in walker
        if p.is_file() and p.suffix.lower() == ".pdf"
    )


def count_pages(pdf_path: Path) -> int:
    if fitz is None:
        return -1
    try:
        doc = fitz.open(str(pdf_path))
        count = len(doc)
        doc.close()
        return count
    except Exception:
        return -1


def load_truth_map(expected_xlsx: Path) -> dict[str, object]:
    from evaluate_expected_results import load_workbook_truth

    truth_rows = load_workbook_truth(expected_xlsx)
    return {row.part_number: row for row in truth_rows}


def discover_expected_xlsx(input_path: Path, script_dir: Path) -> Optional[Path]:
    candidates = [
        Path.cwd() / "expected_result.xlsx",
        script_dir / "expected_result.xlsx",
    ]

    if input_path.is_file():
        candidates.append(input_path.parent / "expected_result.xlsx")
    elif input_path.parent != input_path:
        candidates.append(input_path.parent / "expected_result.xlsx")

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            return candidate
    return None


def parse_stdout(stdout_text: str) -> ParsedStdout:
    pages_processed = 0
    pages_with_unknown = 0
    max_unknown_on_page = 0
    max_candidates_on_page = 0

    for match in PAGE_RE.finditer(stdout_text):
        pages_processed += 1
        unknown = int(match.group("unknown"))
        candidates = int(match.group("candidates"))
        if unknown > 0:
            pages_with_unknown += 1
        max_unknown_on_page = max(max_unknown_on_page, unknown)
        max_candidates_on_page = max(max_candidates_on_page, candidates)

    total_match = DOC_TOTAL_RE.search(stdout_text)
    if total_match is None:
        return ParsedStdout(
            male_total=None,
            female_total=None,
            ferrule_total=None,
            unknown_total=None,
            total_detected=None,
            pages_processed=pages_processed,
            pages_with_unknown=pages_with_unknown,
            max_unknown_on_page=max_unknown_on_page,
            max_candidates_on_page=max_candidates_on_page,
        )

    male_total = int(total_match.group("male"))
    female_total = int(total_match.group("female"))
    ferrule_total = int(total_match.group("ferrule"))
    unknown_total = int(total_match.group("unknown"))
    total_detected = int(total_match.group("total"))

    return ParsedStdout(
        male_total=male_total,
        female_total=female_total,
        ferrule_total=ferrule_total,
        unknown_total=unknown_total,
        total_detected=total_detected,
        pages_processed=pages_processed,
        pages_with_unknown=pages_with_unknown,
        max_unknown_on_page=max_unknown_on_page,
        max_candidates_on_page=max_candidates_on_page,
    )


def compute_match_fields(
    parsed: ParsedStdout,
    truth_row: Optional[object],
) -> tuple[int, Optional[int], Optional[int], Optional[int], Optional[int], Optional[int], Optional[int], Optional[int], str]:
    if truth_row is None:
        return 0, None, None, None, None, None, None, None, "NO_TRUTH"

    male_true = int(getattr(truth_row, "male_true"))
    female_true = int(getattr(truth_row, "female_true"))
    ferrule_true = int(getattr(truth_row, "ferrule_true"))

    if parsed.total_detected is None:
        return 1, male_true, female_true, ferrule_true, None, None, None, None, "FAIL_PARSE"

    male_err = (parsed.male_total or 0) - male_true
    female_err = (parsed.female_total or 0) - female_true
    ferrule_err = (parsed.ferrule_total or 0) - ferrule_true
    count_abs_error = abs(male_err) + abs(female_err) + abs(ferrule_err)

    if parsed.total_detected == 0:
        match_status = "NO_DETECTIONS"
    elif count_abs_error == 0 and (parsed.unknown_total or 0) == 0:
        match_status = "MATCH_CLEAN"
    elif count_abs_error == 0:
        match_status = "MATCH_WITH_UNKNOWN"
    elif (parsed.unknown_total or 0) > 0:
        match_status = "MISMATCH_WITH_UNKNOWN"
    else:
        match_status = "MISMATCH_CLEAN"

    return 1, male_true, female_true, ferrule_true, male_err, female_err, ferrule_err, count_abs_error, match_status


def build_detector_command(
    python_exe: str,
    detector_script: Path,
    input_pdf: Path,
    output_pdf: Path,
    debug_dir: Path,
    args: argparse.Namespace,
) -> List[str]:
    return [
        python_exe,
        str(detector_script),
        str(input_pdf),
        str(args.male_left_template),
        str(args.male_right_template),
        str(args.female_left_template),
        str(args.female_right_template),
        str(args.ferrule_left_template),
        str(args.ferrule_right_template),
        "-o",
        str(output_pdf),
        "--debug-dir",
        str(debug_dir),
        "--zoom",
        str(args.zoom),
        "--score-thresh",
        str(args.score_thresh),
        "--ferrule-score-thresh",
        str(args.ferrule_score_thresh),
        "--score-margin",
        str(args.score_margin),
    ]


def ensure_clean_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def run_one_pdf(
    pdf_path: Path,
    args: argparse.Namespace,
    output_root: Path,
    detector_script: Path,
    truth_map: Optional[dict[str, object]] = None,
) -> RunResult:
    stem = pdf_path.stem
    part_number = extract_part_number(stem) or normalize_stem(stem)

    pdf_output_dir = output_root / "modified_pdfs"
    debug_output_dir = output_root / "debug"
    log_output_dir = output_root / "logs"
    pdf_output_dir.mkdir(parents=True, exist_ok=True)
    debug_output_dir.mkdir(parents=True, exist_ok=True)
    log_output_dir.mkdir(parents=True, exist_ok=True)

    output_pdf = pdf_output_dir / f"{stem}_modified.pdf"
    debug_dir = debug_output_dir / f"{stem}_modified"
    stdout_log = log_output_dir / f"{stem}.stdout.txt"
    stderr_log = log_output_dir / f"{stem}.stderr.txt"

    ensure_clean_path(output_pdf)
    ensure_clean_path(debug_dir)
    ensure_clean_path(stdout_log)
    ensure_clean_path(stderr_log)

    cmd = build_detector_command(
        python_exe=sys.executable,
        detector_script=detector_script,
        input_pdf=pdf_path,
        output_pdf=output_pdf,
        debug_dir=debug_dir,
        args=args,
    )

    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout_log.write_text(completed.stdout, encoding="utf-8")
    stderr_log.write_text(completed.stderr, encoding="utf-8")

    parsed = parse_stdout(completed.stdout)
    page_count = count_pages(pdf_path)
    truth_row = (truth_map or {}).get(part_number)
    (
        truth_available,
        male_true,
        female_true,
        ferrule_true,
        male_err,
        female_err,
        ferrule_err,
        count_abs_error,
        match_status,
    ) = compute_match_fields(parsed, truth_row)

    if completed.returncode != 0:
        status = "FAIL"
        needs_review = 1
        review_reason = "subprocess_failed"
        message = completed.stderr.strip() or "Detector subprocess failed."
    elif parsed.total_detected is None:
        status = "FAIL_PARSE"
        needs_review = 1
        review_reason = "totals_not_parsed"
        message = "Detector finished but document totals could not be parsed from stdout."
    elif parsed.total_detected == 0:
        status = "NO_DETECTIONS"
        needs_review = 1
        review_reason = "no_detections"
        message = "Detector finished but total detections were zero."
    elif (parsed.unknown_total or 0) > 0:
        status = "HAS_UNKNOWN"
        needs_review = 1
        review_reason = "unknown_detections"
        message = "Detector finished with unknown detections."
    else:
        status = "CLEAN_NO_UNKNOWN"
        needs_review = 0
        review_reason = "clean_no_unknown"
        message = "Detector finished cleanly with no unknown detections."

    return RunResult(
        row_type="FILE",
        input_pdf=str(pdf_path),
        part_number=part_number,
        output_pdf=str(output_pdf),
        debug_dir=str(debug_dir),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        status=status,
        return_code=completed.returncode,
        page_count=page_count,
        pages_processed=parsed.pages_processed,
        pages_with_unknown=parsed.pages_with_unknown,
        max_unknown_on_page=parsed.max_unknown_on_page,
        max_candidates_on_page=parsed.max_candidates_on_page,
        male_total=parsed.male_total,
        female_total=parsed.female_total,
        ferrule_total=parsed.ferrule_total,
        unknown_total=parsed.unknown_total,
        total_detected=parsed.total_detected,
        needs_review=needs_review,
        review_reason=review_reason,
        truth_available=truth_available,
        male_true=male_true,
        female_true=female_true,
        ferrule_true=ferrule_true,
        male_err=male_err,
        female_err=female_err,
        ferrule_err=ferrule_err,
        count_abs_error=count_abs_error,
        match_status=match_status,
        message=message,
    )


def write_summary_csv(
    results: Iterable[RunResult],
    summary_csv_path: Path,
    match_clean_count: Optional[int] = None,
) -> None:
    rows = [asdict(result) for result in results]
    if not rows:
        return

    if match_clean_count is not None:
        summary_row = {key: "" for key in rows[0].keys()}
        summary_row["row_type"] = "SUMMARY"
        summary_row["part_number"] = "MATCH_CLEAN_COUNT"
        summary_row["truth_available"] = 1
        summary_row["total_detected"] = match_clean_count
        summary_row["match_status"] = "MATCH_CLEAN"
        summary_row["message"] = (
            f"{match_clean_count} drawing(s) are MATCH_CLEAN based on the expected workbook."
        )
        rows.append(summary_row)

    summary_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if args.workers != 1:
        raise SystemExit("Only --workers 1 is supported right now.")

    script_dir = Path(__file__).resolve().parent
    input_path = Path(args.input_path)
    detector_script = Path(args.detector_script)
    output_root = Path(args.output_dir)
    summary_csv_path = output_root / args.summary_csv
    expected_xlsx = Path(args.expected_xlsx) if args.expected_xlsx else None

    if not detector_script.is_absolute():
        detector_script = script_dir / detector_script

    if not detector_script.is_file():
        raise SystemExit(f"Detector script not found: {detector_script}")

    truth_map: Optional[dict[str, object]] = None
    if expected_xlsx:
        if not expected_xlsx.is_absolute():
            expected_xlsx = Path.cwd() / expected_xlsx
        if not expected_xlsx.is_file():
            raise SystemExit(f"Expected workbook not found: {expected_xlsx}")
    else:
        expected_xlsx = discover_expected_xlsx(input_path, script_dir)

    if expected_xlsx:
        truth_map = load_truth_map(expected_xlsx)

    try:
        all_pdfs = collect_pdfs(input_path, recursive=args.recursive)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if not all_pdfs:
        raise SystemExit(f"No PDF files found under: {input_path}")

    matching_pdfs: List[Path] = []
    skipped_pdfs: List[Path] = []
    for pdf_path in all_pdfs:
        if args.allow_nonmatching or stem_matches_expected_pattern(pdf_path.stem, args.pn_prefix):
            matching_pdfs.append(pdf_path)
        else:
            skipped_pdfs.append(pdf_path)

    if not matching_pdfs:
        raise SystemExit(
            "No PDF files matched the requested PN rule. "
            "Use --allow-nonmatching to process everything or adjust --pn-prefix."
        )

    print(f"Found {len(all_pdfs)} PDF(s). Processing {len(matching_pdfs)} file(s).")
    if skipped_pdfs:
        print(f"Skipped {len(skipped_pdfs)} file(s) due to PN filter.")
    if expected_xlsx:
        print(f"Using expected workbook: {expected_xlsx}")
    else:
        print("Using expected workbook: not found")

    results: List[RunResult] = []
    for index, pdf_path in enumerate(matching_pdfs, start=1):
        print(f"[{index}/{len(matching_pdfs)}] Processing {pdf_path.name} ...")
        result = run_one_pdf(
            pdf_path=pdf_path,
            args=args,
            output_root=output_root,
            detector_script=detector_script,
            truth_map=truth_map,
        )
        results.append(result)
        print(
            f"    status={result.status}, male={result.male_total}, female={result.female_total}, "
            f"ferrule={result.ferrule_total}, unknown={result.unknown_total}, output={Path(result.output_pdf).name}"
        )

    match_clean_count = None
    if truth_map is not None:
        match_clean_count = sum(1 for row in results if row.match_status == "MATCH_CLEAN")

    write_summary_csv(results, summary_csv_path, match_clean_count=match_clean_count)

    clean_count = sum(1 for row in results if row.status == "CLEAN_NO_UNKNOWN")
    review_count = sum(1 for row in results if row.needs_review and not row.status.startswith("FAIL"))
    fail_count = sum(1 for row in results if row.status.startswith("FAIL"))

    print("\nBatch summary")
    print(f"  Total processed: {len(results)}")
    print(f"  Clean no unknown: {clean_count}")
    if match_clean_count is not None:
        print(f"  Match clean: {match_clean_count}")
    else:
        print("  Match clean: n/a (no expected workbook)")
    print(f"  Review: {review_count}")
    print(f"  Fail: {fail_count}")
    print(f"  Summary CSV: {summary_csv_path}")


if __name__ == "__main__":
    main()
