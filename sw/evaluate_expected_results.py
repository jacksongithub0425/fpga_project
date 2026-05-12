#!/usr/bin/env python3
"""Evaluate terminal_counter_endpoint_first.py against expected_result.xlsx.

This script is designed for the current project layout:
- truth is stored in an XLSX workbook with document-level counts
- PDFs live in a directory such as sw/test_sample
- detection is provided by terminal_counter_endpoint_first.py

The workbook is parsed directly from XLSX XML so no third-party Excel package
is required.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

import cv2
import fitz  # PyMuPDF


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
PART_NUMBER_RE = re.compile(r"([A-Z0-9]{2,3}-[A-Z0-9]{6}-[A-Z0-9]{2,3})", re.IGNORECASE)


@dataclass(frozen=True)
class WorkbookRow:
    row_index: int
    part_number: str
    male_true: int
    female_true: int
    ferrule_true: int
    male_location: str
    female_location: str
    ferrule_location: str


@dataclass(frozen=True)
class EvalResult:
    pdf_order: int
    part_number: str
    workbook_row: int
    pdf_path: str
    detector_status: str
    status: str
    needs_review: int
    review_reason: str
    male_true: int
    female_true: int
    ferrule_true: int
    male_pred: int
    female_pred: int
    ferrule_pred: int
    unknown_pred: int
    candidates_total: int
    page_count: int
    male_err: int
    female_err: int
    ferrule_err: int
    abs_error_total: int
    exact_match: int


@dataclass(frozen=True)
class ReviewCropRow:
    part_number: str
    workbook_row: int
    page_index: int
    category: str
    kind: str
    detection_id: str
    pin: str
    score: float
    male_score: float
    female_score: float
    ferrule_score: float
    x: int
    y: int
    w: int
    h: int
    crop_path: str


def load_detector_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("terminal_counter_endpoint_first_module", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load detector script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_part_number(text: str) -> str:
    text = (text or "").strip().upper()
    if not text:
        return ""

    match = PART_NUMBER_RE.search(text)
    if match is not None:
        return match.group(1).upper()

    stem = Path(text).stem.upper()
    stem = stem.replace(" (1)", "")
    for suffix in ("_A", "_B"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def parse_numeric_cell(raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        return 0
    return int(float(raw))


def load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []

    shared = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    values: List[str] = []
    for si in shared.findall("a:si", NS):
        values.append("".join(node.text or "" for node in si.iterfind(".//a:t", NS)))
    return values


def load_sheet_rows(xlsx_path: Path) -> List[Dict[str, str]]:
    with zipfile.ZipFile(xlsx_path) as zf:
        shared_strings = load_shared_strings(zf)
        sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))

    rows: List[Dict[str, str]] = []
    for row_index, row in enumerate(sheet.findall(".//a:sheetData/a:row", NS), start=1):
        data: Dict[str, str] = {"_row_index": str(row_index)}
        for cell in row.findall("a:c", NS):
            ref = cell.attrib["r"]
            col = "".join(ch for ch in ref if ch.isalpha())
            cell_type = cell.attrib.get("t")
            value_node = cell.find("a:v", NS)
            value = value_node.text if value_node is not None else ""
            if cell_type == "s" and value:
                value = shared_strings[int(value)]
            data[col] = value
        rows.append(data)
    return rows


def load_workbook_truth(xlsx_path: Path) -> List[WorkbookRow]:
    rows = load_sheet_rows(xlsx_path)
    truth: List[WorkbookRow] = []

    for row in rows[1:]:
        part_number = normalize_part_number(row.get("A", ""))
        if not part_number:
            continue
        truth.append(
            WorkbookRow(
                row_index=int(row["_row_index"]),
                part_number=part_number,
                male_true=parse_numeric_cell(row.get("B", "")),
                female_true=parse_numeric_cell(row.get("D", "")),
                ferrule_true=parse_numeric_cell(row.get("F", "")),
                male_location=(row.get("C", "") or "").strip(),
                female_location=(row.get("E", "") or "").strip(),
                ferrule_location=(row.get("G", "") or "").strip(),
            )
        )

    if not truth:
        raise ValueError(f"No truth rows found in workbook: {xlsx_path}")
    return truth


def collect_pdfs(pdf_dir: Path) -> Dict[str, Path]:
    pdfs: Dict[str, Path] = {}
    for path in sorted(list(pdf_dir.glob("*.pdf")) + list(pdf_dir.glob("*.PDF"))):
        key = normalize_part_number(path.stem)
        if not key:
            continue
        pdfs[key] = path.resolve()
    return pdfs


def count_detections(detections: Sequence[dict]) -> Dict[str, int]:
    male = sum(1 for d in detections if d.get("kind") == "male")
    female = sum(1 for d in detections if d.get("kind") == "female")
    ferrule = sum(1 for d in detections if d.get("kind") == "ferrule")
    unknown = sum(1 for d in detections if d.get("kind") == "unknown")
    return {
        "male": male,
        "female": female,
        "ferrule": ferrule,
        "unknown": unknown,
        "total": male + female + ferrule,
    }


def evaluate_rows(
    detector,
    truth_rows: Sequence[WorkbookRow],
    pdf_map: Dict[str, Path],
    pdf_order_map: Dict[str, int],
    side_templates,
    zoom: float,
    score_thresh: float,
    ferrule_score_thresh: float,
    score_margin: float,
) -> List[EvalResult]:
    results: List[EvalResult] = []

    for truth in truth_rows:
        pdf_path = pdf_map.get(truth.part_number)
        if pdf_path is None:
            continue

        doc = fitz.open(str(pdf_path))
        try:
            page_count = len(doc)
            male_pred = 0
            female_pred = 0
            ferrule_pred = 0
            unknown_pred = 0
            candidates_total = 0

            for page in doc:
                _, candidates, detections = detector.detect_page(
                    page,
                    side_templates=side_templates,
                    zoom=zoom,
                    score_thresh=score_thresh,
                    ferrule_score_thresh=ferrule_score_thresh,
                    score_margin=score_margin,
                )
                counts = count_detections(detections)
                male_pred += counts["male"]
                female_pred += counts["female"]
                ferrule_pred += counts["ferrule"]
                unknown_pred += counts["unknown"]
                candidates_total += len(candidates)
        finally:
            doc.close()

        male_err = male_pred - truth.male_true
        female_err = female_pred - truth.female_true
        ferrule_err = ferrule_pred - truth.ferrule_true
        abs_error_total = abs(male_err) + abs(female_err) + abs(ferrule_err)
        total_detected = male_pred + female_pred + ferrule_pred
        if total_detected == 0:
            detector_status = "NO_DETECTIONS"
        elif unknown_pred > 0:
            detector_status = "HAS_UNKNOWN"
        else:
            detector_status = "CLEAN_NO_UNKNOWN"

        if total_detected == 0:
            status = "NO_DETECTIONS"
            needs_review = 1
            review_reason = "no_detections"
        elif abs_error_total == 0 and unknown_pred == 0:
            status = "MATCH_CLEAN"
            needs_review = 0
            review_reason = "exact_match"
        elif abs_error_total == 0:
            status = "MATCH_WITH_UNKNOWN"
            needs_review = 1
            review_reason = "exact_count_but_unknown_present"
        elif unknown_pred > 0:
            status = "MISMATCH_WITH_UNKNOWN"
            needs_review = 1
            review_reason = "count_mismatch_and_unknown_present"
        else:
            status = "MISMATCH_CLEAN"
            needs_review = 1
            review_reason = "count_mismatch"

        results.append(
            EvalResult(
                pdf_order=pdf_order_map[truth.part_number],
                part_number=truth.part_number,
                workbook_row=truth.row_index,
                pdf_path=str(pdf_path),
                detector_status=detector_status,
                status=status,
                needs_review=needs_review,
                review_reason=review_reason,
                male_true=truth.male_true,
                female_true=truth.female_true,
                ferrule_true=truth.ferrule_true,
                male_pred=male_pred,
                female_pred=female_pred,
                ferrule_pred=ferrule_pred,
                unknown_pred=unknown_pred,
                candidates_total=candidates_total,
                page_count=page_count,
                male_err=male_err,
                female_err=female_err,
                ferrule_err=ferrule_err,
                abs_error_total=abs_error_total,
                exact_match=int(abs_error_total == 0),
            )
        )

    return results


def clamp_crop_box(x: int, y: int, w: int, h: int, img_w: int, img_h: int, pad: int) -> Tuple[int, int, int, int]:
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(img_w, x + w + pad)
    y1 = min(img_h, y + h + pad)
    return x0, y0, x1, y1


def save_crop_image(image, box: Tuple[int, int, int, int], out_path: Path) -> None:
    x0, y0, x1, y1 = box
    crop = image[y0:y1, x0:x1]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), crop)


def choose_review_detections(detections: Sequence[dict], eval_row: EvalResult) -> List[Tuple[str, dict]]:
    picks: List[Tuple[str, dict]] = []
    chosen: set[Tuple[int, str]] = set()

    def maybe_add(category: str, detection: dict) -> None:
        key = (id(detection), category)
        if key in chosen:
            return
        chosen.add(key)
        picks.append((category, detection))

    for detection in detections:
        if detection.get("kind") == "unknown":
            maybe_add("unknown", detection)

    for kind, err in (
        ("male", eval_row.male_err),
        ("female", eval_row.female_err),
        ("ferrule", eval_row.ferrule_err),
    ):
        if err > 0:
            same_kind = [d for d in detections if d.get("kind") == kind]
            same_kind.sort(key=lambda d: (d.get("has_label", False), d.get("score", -1.0)))
            for detection in same_kind[:err]:
                maybe_add(f"likely_false_positive_{kind}", detection)
        elif err < 0:
            unknowns = [d for d in detections if d.get("kind") == "unknown"]
            score_key = f"{kind}_score"
            unknowns.sort(key=lambda d: d.get(score_key, -1.0), reverse=True)
            for detection in unknowns[: abs(err)]:
                maybe_add(f"likely_missed_{kind}", detection)

    return picks


def export_review_crops(
    detector,
    eval_rows: Sequence[EvalResult],
    truth_map: Dict[str, WorkbookRow],
    pdf_map: Dict[str, Path],
    side_templates,
    output_dir: Path,
    zoom: float,
    score_thresh: float,
    ferrule_score_thresh: float,
    score_margin: float,
    top_n: int,
    crop_pad: int,
) -> List[ReviewCropRow]:
    review_rows: List[ReviewCropRow] = []
    selected = [row for row in eval_rows if row.abs_error_total > 0][:top_n]

    for eval_row in selected:
        pdf_path = pdf_map[eval_row.part_number]
        doc = fitz.open(str(pdf_path))
        try:
            file_dir = output_dir / eval_row.part_number
            for page_index, page in enumerate(doc):
                bgr, _, detections = detector.detect_page(
                    page,
                    side_templates=side_templates,
                    zoom=zoom,
                    score_thresh=score_thresh,
                    ferrule_score_thresh=ferrule_score_thresh,
                    score_margin=score_margin,
                )

                page_dir = file_dir / f"page_{page_index + 1:03d}"
                detector.draw_debug_image(bgr, detections, [], page_dir / "page_debug.png")

                for crop_index, (category, detection) in enumerate(choose_review_detections(detections, eval_row), start=1):
                    img_h, img_w = bgr.shape[:2]
                    crop_box = clamp_crop_box(
                        detection["x"],
                        detection["y"],
                        detection["w"],
                        detection["h"],
                        img_w,
                        img_h,
                        crop_pad,
                    )
                    crop_name = (
                        f"{crop_index:03d}_{category}_{detection['kind']}_{detection['id']}.png"
                    )
                    crop_path = page_dir / crop_name
                    save_crop_image(bgr, crop_box, crop_path)

                    review_rows.append(
                        ReviewCropRow(
                            part_number=eval_row.part_number,
                            workbook_row=truth_map[eval_row.part_number].row_index,
                            page_index=page_index,
                            category=category,
                            kind=detection["kind"],
                            detection_id=detection["id"],
                            pin=detection.get("pin", ""),
                            score=float(detection.get("score", -1.0)),
                            male_score=float(detection.get("male_score", -1.0)),
                            female_score=float(detection.get("female_score", -1.0)),
                            ferrule_score=float(detection.get("ferrule_score", -1.0)),
                            x=int(detection["x"]),
                            y=int(detection["y"]),
                            w=int(detection["w"]),
                            h=int(detection["h"]),
                            crop_path=str(crop_path.resolve()),
                        )
                    )
        finally:
            doc.close()

    return review_rows


def write_csv(path: Path, rows: Iterable[dict]) -> None:
    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate terminal_counter_endpoint_first.py against expected_result.xlsx."
    )
    parser.add_argument("expected_xlsx", help="Workbook with PN, male/female/ferrule truth counts")
    parser.add_argument("pdf_dir", help="Directory containing the sample PDFs")
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
    parser.add_argument("--output-dir", default="expected_eval_results", help="Directory for CSV/JSON outputs")
    parser.add_argument("--zoom", type=float, default=4.0, help="Render zoom passed to the detector")
    parser.add_argument("--score-thresh", type=float, default=0.33, help="Male/female threshold")
    parser.add_argument("--ferrule-score-thresh", type=float, default=0.24, help="Ferrule threshold")
    parser.add_argument("--score-margin", type=float, default=0.03, help="Winner margin threshold")
    parser.add_argument(
        "--review-top-n",
        type=int,
        default=10,
        help="Export crops for the top N worst files by absolute count error. Use 0 to disable.",
    )
    parser.add_argument(
        "--crop-pad",
        type=int,
        default=16,
        help="Padding in pixels around each exported crop.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    script_dir = Path(__file__).resolve().parent
    detector_script = Path(args.detector_script)
    if not detector_script.is_absolute():
        detector_script = (script_dir / detector_script).resolve()

    expected_xlsx = Path(args.expected_xlsx).resolve()
    pdf_dir = Path(args.pdf_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    detector = load_detector_module(detector_script)
    truth_rows = load_workbook_truth(expected_xlsx)
    truth_map = {row.part_number: row for row in truth_rows}
    pdf_map = collect_pdfs(pdf_dir)
    pdf_order_map = {part_number: index for index, part_number in enumerate(pdf_map.keys(), start=1)}

    overlap = sorted(set(truth_map) & set(pdf_map))
    missing_from_workbook = sorted(set(pdf_map) - set(truth_map))
    missing_pdf = sorted(set(truth_map) - set(pdf_map))

    male_left = detector.load_template_bank(args.male_left_template)
    male_right = detector.load_template_bank(args.male_right_template)
    female_left = detector.load_template_bank(args.female_left_template)
    female_right = detector.load_template_bank(args.female_right_template)
    ferrule_left = detector.load_template_bank(args.ferrule_left_template)
    ferrule_right = detector.load_template_bank(args.ferrule_right_template)
    side_templates = detector.build_side_templates(
        male_left, male_right, female_left, female_right, ferrule_left, ferrule_right
    )

    results = evaluate_rows(
        detector=detector,
        truth_rows=[truth_map[pn] for pn in overlap],
        pdf_map=pdf_map,
        pdf_order_map=pdf_order_map,
        side_templates=side_templates,
        zoom=args.zoom,
        score_thresh=args.score_thresh,
        ferrule_score_thresh=args.ferrule_score_thresh,
        score_margin=args.score_margin,
    )

    results_by_error = sorted(results, key=lambda row: (-row.abs_error_total, row.part_number))
    results_by_order = sorted(results, key=lambda row: row.pdf_order)

    male_abs_error = sum(abs(row.male_err) for row in results_by_order)
    female_abs_error = sum(abs(row.female_err) for row in results_by_order)
    ferrule_abs_error = sum(abs(row.ferrule_err) for row in results_by_order)
    total_abs_error = male_abs_error + female_abs_error + ferrule_abs_error

    summary = {
        "expected_xlsx": str(expected_xlsx),
        "pdf_dir": str(pdf_dir),
        "detector_script": str(detector_script),
        "overlap_count": len(overlap),
        "workbook_count": len(truth_rows),
        "pdf_count": len(pdf_map),
        "missing_from_workbook": missing_from_workbook,
        "missing_pdf": missing_pdf,
        "male_abs_error": male_abs_error,
        "female_abs_error": female_abs_error,
        "ferrule_abs_error": ferrule_abs_error,
        "total_abs_error": total_abs_error,
        "exact_matches": sum(row.exact_match for row in results_by_order),
        "non_exact_matches": sum(1 for row in results_by_order if not row.exact_match),
        "zoom": args.zoom,
        "score_thresh": args.score_thresh,
        "ferrule_score_thresh": args.ferrule_score_thresh,
        "score_margin": args.score_margin,
        "review_top_n": args.review_top_n,
        "crop_pad": args.crop_pad,
    }

    write_csv(output_dir / "per_file_results.csv", [asdict(row) for row in results_by_order])
    write_csv(output_dir / "per_file_results_by_error.csv", [asdict(row) for row in results_by_error])

    review_rows: List[ReviewCropRow] = []
    if args.review_top_n > 0:
        review_rows = export_review_crops(
            detector=detector,
            eval_rows=results_by_error,
            truth_map=truth_map,
            pdf_map=pdf_map,
            side_templates=side_templates,
            output_dir=output_dir / "review_crops",
            zoom=args.zoom,
            score_thresh=args.score_thresh,
            ferrule_score_thresh=args.ferrule_score_thresh,
            score_margin=args.score_margin,
            top_n=args.review_top_n,
            crop_pad=args.crop_pad,
        )
        write_csv(output_dir / "review_crops_manifest.csv", [asdict(row) for row in review_rows])

    summary["review_crops_exported"] = len(review_rows)
    with (output_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    print("Dataset alignment:")
    print(f"  workbook rows: {len(truth_rows)}")
    print(f"  pdf files: {len(pdf_map)}")
    print(f"  overlap: {len(overlap)}")
    if missing_from_workbook:
        print(f"  PDFs missing from workbook ({len(missing_from_workbook)}): {', '.join(missing_from_workbook)}")
    if missing_pdf:
        print(f"  Workbook rows missing PDFs ({len(missing_pdf)}): {', '.join(missing_pdf)}")

    print("\nEvaluation summary:")
    print(json.dumps(summary, indent=2))

    print("\nWorst files:")
    for row in results_by_error[:10]:
        if row.abs_error_total == 0:
            break
        print(
            f"  {row.part_number}: true=({row.male_true},{row.female_true},{row.ferrule_true}) "
            f"pred=({row.male_pred},{row.female_pred},{row.ferrule_pred}) "
            f"unknown={row.unknown_pred} abs_error={row.abs_error_total}"
        )

    print(f"\nSaved: {output_dir / 'per_file_results.csv'}")
    print(f"Saved: {output_dir / 'per_file_results_by_error.csv'}")
    if review_rows:
        print(f"Saved: {output_dir / 'review_crops_manifest.csv'}")
        print(f"Saved crops under: {output_dir / 'review_crops'}")
    print(f"Saved: {output_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
