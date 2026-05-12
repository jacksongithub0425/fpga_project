#!/usr/bin/env python3
"""Render a PDF page and extract clean terminal template crops.

Typical workflow:
1. Save a page preview to pick crop coordinates.
2. Re-run with six crop definitions to write:
   male_left.png, male_right.png, female_left.png, female_right.png,
   ferrule_left.png, ferrule_right.png

The saved template images are grayscale crops. This matches the detector,
which applies its own binarization when loading templates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, Tuple

import cv2
import fitz  # PyMuPDF
import numpy as np


STANDARD_TEMPLATE_NAMES = (
    "male_left",
    "male_right",
    "female_left",
    "female_right",
    "ferrule_left",
    "ferrule_right",
)


def render_gray_page(page: fitz.Page, zoom: float) -> np.ndarray:
    """Render using the same path as the main detector, then convert to gray."""
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

    if pix.n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def to_binary_inv(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def clamp_rect(rect: Tuple[int, int, int, int], width: int, height: int) -> Tuple[int, int, int, int]:
    x0, y0, x1, y1 = rect
    x0 = max(0, min(x0, width - 1))
    y0 = max(0, min(y0, height - 1))
    x1 = max(x0 + 1, min(x1, width))
    y1 = max(y0 + 1, min(y1, height))
    return x0, y0, x1, y1


def parse_crop_spec(raw: str) -> Tuple[str, Tuple[int, int, int, int]]:
    try:
        name, coords = raw.split("=", 1)
    except ValueError as exc:
        raise ValueError(f"Invalid crop spec '{raw}'. Expected name=x0,y0,x1,y1.") from exc

    name = name.strip()
    if not name:
        raise ValueError(f"Invalid crop spec '{raw}'. Name cannot be empty.")

    parts = [part.strip() for part in coords.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Invalid crop spec '{raw}'. Expected four comma-separated integers.")

    try:
        x0, y0, x1, y1 = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"Invalid crop spec '{raw}'. Coordinates must be integers.") from exc

    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"Invalid crop spec '{raw}'. Require x1>x0 and y1>y0.")

    return name, (x0, y0, x1, y1)


def load_crop_manifest(path: Path) -> Dict[str, Tuple[int, int, int, int]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Crop manifest must be a JSON object: {path}")

    crops: Dict[str, Tuple[int, int, int, int]] = {}
    for name, value in data.items():
        if not isinstance(name, str):
            raise ValueError(f"Crop manifest keys must be strings: {path}")
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError(f"Crop '{name}' must be [x0, y0, x1, y1] in {path}")
        try:
            rect = tuple(int(v) for v in value)
        except ValueError as exc:
            raise ValueError(f"Crop '{name}' must contain integers in {path}") from exc
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise ValueError(f"Crop '{name}' must satisfy x1>x0 and y1>y0 in {path}")
        crops[name] = rect  # type: ignore[assignment]
    return crops


def trim_crop(gray_crop: np.ndarray, padding: int) -> np.ndarray:
    binary = to_binary_inv(gray_crop)
    ys, xs = np.where(binary > 0)
    if len(xs) == 0 or len(ys) == 0:
        return gray_crop

    x0 = max(0, int(xs.min()) - padding)
    y0 = max(0, int(ys.min()) - padding)
    x1 = min(gray_crop.shape[1], int(xs.max()) + 1 + padding)
    y1 = min(gray_crop.shape[0], int(ys.max()) + 1 + padding)
    return gray_crop[y0:y1, x0:x1]


def save_page_previews(gray: np.ndarray, output_dir: Path, page_index: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    gray_path = output_dir / f"page_{page_index + 1:03d}_gray.png"
    binary_path = output_dir / f"page_{page_index + 1:03d}_binary_preview.png"
    cv2.imwrite(str(gray_path), gray)
    cv2.imwrite(str(binary_path), to_binary_inv(gray))
    print(f"Saved page preview: {gray_path}")
    print(f"Saved binary preview: {binary_path}")


def merge_crop_sources(manifest_crops: Dict[str, Tuple[int, int, int, int]],
                       cli_crops: Iterable[str]) -> Dict[str, Tuple[int, int, int, int]]:
    crops = dict(manifest_crops)
    for raw in cli_crops:
        name, rect = parse_crop_spec(raw)
        crops[name] = rect
    return crops


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate clean grayscale template crops for male/female/ferrule terminal matching."
    )
    parser.add_argument("pdf_path", help="Source PDF used to extract template crops.")
    parser.add_argument(
        "--page-index",
        type=int,
        default=0,
        help="Zero-based page index to render. Default: 0",
    )
    parser.add_argument(
        "--zoom",
        type=float,
        default=4.0,
        help="Render zoom. Keep this aligned with the detector. Default: 4.0",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory where preview images and template files are written.",
    )
    parser.add_argument(
        "--save-page-preview",
        action="store_true",
        help="Save full-page grayscale and binary preview images for coordinate picking.",
    )
    parser.add_argument(
        "--crop-manifest",
        help="Optional JSON file mapping template names to [x0, y0, x1, y1].",
    )
    parser.add_argument(
        "--crop",
        action="append",
        default=[],
        help="Template crop in rendered-image pixels: name=x0,y0,x1,y1. Can be repeated.",
    )
    parser.add_argument(
        "--trim-padding",
        type=int,
        default=2,
        help="Padding kept around detected foreground when auto-trimming each crop. Default: 2",
    )
    parser.add_argument(
        "--require-standard-six",
        action="store_true",
        help="Fail unless all six standard template names are present.",
    )
    parser.add_argument(
        "--write-binary-copies",
        action="store_true",
        help="Also save *_binary.png previews next to each grayscale template for inspection.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if args.page_index < 0:
        raise ValueError("--page-index must be >= 0")
    if args.trim_padding < 0:
        raise ValueError("--trim-padding must be >= 0")

    manifest_crops: Dict[str, Tuple[int, int, int, int]] = {}
    if args.crop_manifest:
        manifest_crops = load_crop_manifest(Path(args.crop_manifest).resolve())

    crops = merge_crop_sources(manifest_crops, args.crop)

    doc = fitz.open(str(pdf_path))
    try:
        if args.page_index >= len(doc):
            raise IndexError(
                f"Page index {args.page_index} out of range for {pdf_path.name} with {len(doc)} pages."
            )
        page = doc[args.page_index]
        gray = render_gray_page(page, zoom=args.zoom)
    finally:
        doc.close()

    if args.save_page_preview:
        save_page_previews(gray, output_dir, args.page_index)

    if args.require_standard_six:
        missing = [name for name in STANDARD_TEMPLATE_NAMES if name not in crops]
        if missing:
            raise ValueError(f"Missing required standard templates: {', '.join(missing)}")

    if not crops:
        print("No crop definitions provided. Preview generation complete.")
        print("Add --crop name=x0,y0,x1,y1 or --crop-manifest to write template files.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    img_h, img_w = gray.shape[:2]

    for name, rect in sorted(crops.items()):
        x0, y0, x1, y1 = clamp_rect(rect, img_w, img_h)
        gray_crop = gray[y0:y1, x0:x1]
        trimmed = trim_crop(gray_crop, padding=args.trim_padding)

        gray_path = output_dir / f"{name}.png"
        if not cv2.imwrite(str(gray_path), trimmed):
            raise RuntimeError(f"Failed to save template: {gray_path}")

        print(
            f"Saved {gray_path.name}: "
            f"input_rect=({x0},{y0},{x1},{y1}) output_shape={trimmed.shape[1]}x{trimmed.shape[0]}"
        )

        if args.write_binary_copies:
            binary_path = output_dir / f"{name}_binary.png"
            if not cv2.imwrite(str(binary_path), to_binary_inv(trimmed)):
                raise RuntimeError(f"Failed to save binary preview: {binary_path}")
            print(f"Saved {binary_path.name}")


if __name__ == "__main__":
    main()
