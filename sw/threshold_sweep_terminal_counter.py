import argparse
import csv
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import fitz  # PyMuPDF


@dataclass(frozen=True)
class TruthRow:
    pdf_path: Path
    page_index: int
    male_true: int
    female_true: int
    ferrule_true: int


@dataclass(frozen=True)
class SweepParams:
    zoom: float
    score_thresh: float
    ferrule_score_thresh: float
    score_margin: float


PDF_COLS = ("pdf_path", "file_path", "file_name", "input_pdf", "pdf")
PAGE_COLS = ("page", "page_index")


def parse_float_list(raw: str) -> List[float]:
    values: List[float] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        values.append(float(item))
    if not values:
        raise ValueError("Expected at least one numeric value.")
    return values


def load_detector_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("terminal_counter_endpoint_first_module", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load detector script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_first_key(row: Dict[str, str], candidates: Iterable[str]) -> str:
    for key in candidates:
        if key in row:
            return key
    raise KeyError(f"Missing required column. Tried: {', '.join(candidates)}")


def load_truth_rows(csv_path: Path, base_dir: Path, page_base: int) -> List[TruthRow]:
    rows: List[TruthRow] = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("Ground-truth CSV has no header row.")

        pdf_key = find_first_key({name: "" for name in reader.fieldnames}, PDF_COLS)
        page_key = find_first_key({name: "" for name in reader.fieldnames}, PAGE_COLS)

        required = ["male_true", "female_true", "ferrule_true"]
        for req in required:
            if req not in reader.fieldnames:
                raise KeyError(f"Missing required column: {req}")

        for line_no, row in enumerate(reader, start=2):
            pdf_raw = (row.get(pdf_key) or "").strip()
            if not pdf_raw:
                raise ValueError(f"Line {line_no}: empty PDF path/name.")
            pdf_path = Path(pdf_raw)
            if not pdf_path.is_absolute():
                pdf_path = (base_dir / pdf_path).resolve()

            page_value = int((row.get(page_key) or "").strip()) - page_base
            if page_value < 0:
                raise ValueError(f"Line {line_no}: page becomes negative after page-base adjustment.")

            rows.append(
                TruthRow(
                    pdf_path=pdf_path,
                    page_index=page_value,
                    male_true=int((row.get("male_true") or "0").strip()),
                    female_true=int((row.get("female_true") or "0").strip()),
                    ferrule_true=int((row.get("ferrule_true") or "0").strip()),
                )
            )

    if not rows:
        raise ValueError("Ground-truth CSV has no data rows.")
    return rows


def count_detections(detections: List[dict]) -> Dict[str, int]:
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


def evaluate_combo(detector, truth_rows: List[TruthRow], side_templates, params: SweepParams):
    doc_cache: Dict[Path, fitz.Document] = {}
    page_predictions: List[Dict[str, object]] = []

    male_abs_error = 0
    female_abs_error = 0
    ferrule_abs_error = 0
    exact_pages = 0

    male_bias = 0
    female_bias = 0
    ferrule_bias = 0

    try:
        for truth in truth_rows:
            if truth.pdf_path not in doc_cache:
                if not truth.pdf_path.exists():
                    raise FileNotFoundError(f"PDF not found: {truth.pdf_path}")
                doc_cache[truth.pdf_path] = fitz.open(str(truth.pdf_path))

            doc = doc_cache[truth.pdf_path]
            if truth.page_index >= len(doc):
                raise IndexError(
                    f"Page {truth.page_index} out of range for {truth.pdf_path.name} (page count {len(doc)})."
                )

            page = doc[truth.page_index]
            _, _, detections = detector.detect_page(
                page,
                side_templates=side_templates,
                zoom=params.zoom,
                score_thresh=params.score_thresh,
                ferrule_score_thresh=params.ferrule_score_thresh,
                score_margin=params.score_margin,
            )

            pred = count_detections(detections)

            male_err = pred["male"] - truth.male_true
            female_err = pred["female"] - truth.female_true
            ferrule_err = pred["ferrule"] - truth.ferrule_true

            male_abs_error += abs(male_err)
            female_abs_error += abs(female_err)
            ferrule_abs_error += abs(ferrule_err)

            male_bias += male_err
            female_bias += female_err
            ferrule_bias += ferrule_err

            exact = male_err == 0 and female_err == 0 and ferrule_err == 0
            if exact:
                exact_pages += 1

            page_predictions.append(
                {
                    "pdf_path": str(truth.pdf_path),
                    "pdf_name": truth.pdf_path.name,
                    "page": truth.page_index,
                    "male_true": truth.male_true,
                    "female_true": truth.female_true,
                    "ferrule_true": truth.ferrule_true,
                    "male_pred": pred["male"],
                    "female_pred": pred["female"],
                    "ferrule_pred": pred["ferrule"],
                    "unknown_pred": pred["unknown"],
                    "male_err": male_err,
                    "female_err": female_err,
                    "ferrule_err": ferrule_err,
                    "abs_error_total": abs(male_err) + abs(female_err) + abs(ferrule_err),
                    "exact_page": int(exact),
                }
            )
    finally:
        for doc in doc_cache.values():
            doc.close()

    total_abs_error = male_abs_error + female_abs_error + ferrule_abs_error
    summary = {
        "zoom": params.zoom,
        "score_thresh": params.score_thresh,
        "ferrule_score_thresh": params.ferrule_score_thresh,
        "score_margin": params.score_margin,
        "pages": len(truth_rows),
        "exact_pages": exact_pages,
        "male_abs_error": male_abs_error,
        "female_abs_error": female_abs_error,
        "ferrule_abs_error": ferrule_abs_error,
        "total_abs_error": total_abs_error,
        "male_bias": male_bias,
        "female_bias": female_bias,
        "ferrule_bias": ferrule_bias,
    }
    return summary, page_predictions


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"No rows to write: {path}")
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep detector thresholds against page-level truth counts for terminal_counter_endpoint_first.py"
    )
    parser.add_argument("ground_truth_csv", help="CSV with pdf_path/file_name, page, male_true, female_true, ferrule_true")
    parser.add_argument("male_left_template", help="Base template path or directory, e.g. male_left.png")
    parser.add_argument("male_right_template", help="Base template path or directory, e.g. male_right.png")
    parser.add_argument("female_left_template", help="Base template path or directory, e.g. female_left.png")
    parser.add_argument("female_right_template", help="Base template path or directory, e.g. female_right.png")
    parser.add_argument("ferrule_left_template", help="Base template path or directory, e.g. ferrule_left.png")
    parser.add_argument("ferrule_right_template", help="Base template path or directory, e.g. ferrule_right.png")
    parser.add_argument("--detector-script", default="terminal_counter_endpoint_first.py",
                        help="Path to terminal_counter_endpoint_first.py")
    parser.add_argument("--pdf-base-dir", default=".",
                        help="Base directory used for relative PDF paths inside the CSV")
    parser.add_argument("--page-base", type=int, choices=[0, 1], default=0,
                        help="Set to 1 if the CSV page column is 1-based")
    parser.add_argument("--zoom-values", default="4.0",
                        help="Comma-separated zoom values, e.g. 3.5,4.0,4.5")
    parser.add_argument("--score-thresh-values", default="0.27,0.30,0.33,0.36,0.39",
                        help="Comma-separated values for male/female acceptance threshold")
    parser.add_argument("--ferrule-score-thresh-values", default="0.16,0.20,0.24,0.28,0.32",
                        help="Comma-separated values for ferrule acceptance threshold")
    parser.add_argument("--score-margin-values", default="0.00,0.02,0.03,0.05",
                        help="Comma-separated values for best-vs-second-best margin")
    parser.add_argument("--output-dir", default="threshold_sweep_results",
                        help="Directory for leaderboard and best-run predictions")

    args = parser.parse_args()

    ground_truth_csv = Path(args.ground_truth_csv).resolve()
    detector_script = Path(args.detector_script).resolve()
    pdf_base_dir = Path(args.pdf_base_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    detector = load_detector_module(detector_script)

    truth_rows = load_truth_rows(ground_truth_csv, pdf_base_dir, page_base=args.page_base)
    zoom_values = parse_float_list(args.zoom_values)
    score_thresh_values = parse_float_list(args.score_thresh_values)
    ferrule_score_thresh_values = parse_float_list(args.ferrule_score_thresh_values)
    score_margin_values = parse_float_list(args.score_margin_values)

    male_left = detector.load_template_bank(args.male_left_template)
    male_right = detector.load_template_bank(args.male_right_template)
    female_left = detector.load_template_bank(args.female_left_template)
    female_right = detector.load_template_bank(args.female_right_template)
    ferrule_left = detector.load_template_bank(args.ferrule_left_template)
    ferrule_right = detector.load_template_bank(args.ferrule_right_template)
    side_templates = detector.build_side_templates(
        male_left, male_right, female_left, female_right, ferrule_left, ferrule_right
    )

    leaderboard: List[Dict[str, object]] = []
    best_summary = None
    best_predictions = None

    total_combos = (
        len(zoom_values)
        * len(score_thresh_values)
        * len(ferrule_score_thresh_values)
        * len(score_margin_values)
    )
    combo_idx = 0

    for zoom in zoom_values:
        for score_thresh in score_thresh_values:
            for ferrule_score_thresh in ferrule_score_thresh_values:
                for score_margin in score_margin_values:
                    combo_idx += 1
                    params = SweepParams(
                        zoom=zoom,
                        score_thresh=score_thresh,
                        ferrule_score_thresh=ferrule_score_thresh,
                        score_margin=score_margin,
                    )
                    summary, predictions = evaluate_combo(detector, truth_rows, side_templates, params)
                    leaderboard.append(summary)

                    is_better = False
                    if best_summary is None:
                        is_better = True
                    else:
                        current_key = (
                            summary["total_abs_error"],
                            -summary["exact_pages"],
                            abs(summary["male_bias"]) + abs(summary["female_bias"]) + abs(summary["ferrule_bias"]),
                            summary["score_margin"],
                        )
                        best_key = (
                            best_summary["total_abs_error"],
                            -best_summary["exact_pages"],
                            abs(best_summary["male_bias"]) + abs(best_summary["female_bias"]) + abs(best_summary["ferrule_bias"]),
                            best_summary["score_margin"],
                        )
                        is_better = current_key < best_key

                    if is_better:
                        best_summary = summary
                        best_predictions = predictions

                    print(
                        f"[{combo_idx}/{total_combos}] zoom={zoom:.2f} score={score_thresh:.3f} "
                        f"ferrule={ferrule_score_thresh:.3f} margin={score_margin:.3f} -> "
                        f"total_abs_error={summary['total_abs_error']} exact_pages={summary['exact_pages']}/{summary['pages']}"
                    )

    if best_summary is None or best_predictions is None:
        raise RuntimeError("Sweep failed to produce any result.")

    leaderboard.sort(
        key=lambda row: (
            row["total_abs_error"],
            -row["exact_pages"],
            abs(row["male_bias"]) + abs(row["female_bias"]) + abs(row["ferrule_bias"]),
            row["score_margin"],
        )
    )

    leaderboard_path = output_dir / "leaderboard.csv"
    best_predictions_path = output_dir / "best_predictions.csv"
    best_params_path = output_dir / "best_params.json"

    write_csv(leaderboard_path, leaderboard)
    write_csv(best_predictions_path, best_predictions)
    with best_params_path.open("w", encoding="utf-8") as f:
        json.dump(best_summary, f, indent=2)

    print("\nBest parameters:")
    print(json.dumps(best_summary, indent=2))
    print(f"\nSaved leaderboard: {leaderboard_path}")
    print(f"Saved best predictions: {best_predictions_path}")
    print(f"Saved best params JSON: {best_params_path}")


if __name__ == "__main__":
    main()
