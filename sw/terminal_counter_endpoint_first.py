import argparse
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import fitz  # PyMuPDF
import numpy as np


# ---------------------------
# Basic helpers
# ---------------------------


def render_page(page: fitz.Page, zoom: float = 4.0) -> Tuple[np.ndarray, np.ndarray, float]:
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

    if pix.n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return bgr, gray, zoom



def to_binary_inv(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw



def load_template(path: str) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Cannot read template: {path}")
    return to_binary_inv(img)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

STANDARD_TEMPLATE_DIRS = {
    "male_left": "male_ter",
    "male_right": "male_ter",
    "female_left": "female_ter",
    "female_right": "female_ter",
    "ferrule_left": "ferrule_ter",
    "ferrule_right": "ferrule_ter",
}


def resolve_template_path(path: str) -> Path:
    raw = Path(path).expanduser()
    script_dir = Path(__file__).resolve().parent

    candidates: List[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend(
            [
                raw,
                Path.cwd() / raw,
                script_dir / raw,
            ]
        )

        folder_name = STANDARD_TEMPLATE_DIRS.get(raw.stem)
        if folder_name:
            candidates.append(script_dir / folder_name / raw.name)

    seen: set[Path] = set()
    unique_candidates: List[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_candidates.append(candidate)

    for candidate in unique_candidates:
        if candidate.exists():
            return candidate

    if not raw.is_absolute():
        basename_matches = sorted(script_dir.rglob(raw.name))
        basename_matches = [match for match in basename_matches if match.is_file() or match.is_dir()]
        if len(basename_matches) == 1:
            return basename_matches[0]

    searched = ", ".join(str(candidate) for candidate in unique_candidates)
    raise FileNotFoundError(
        f"Template path does not exist: {path}. Checked: {searched}"
    )


def discover_template_paths(path: str) -> List[Path]:
    base_path = resolve_template_path(path)

    if base_path.is_dir():
        matches = sorted(
            p for p in base_path.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )
        if not matches:
            raise FileNotFoundError(f"No template images found in directory: {path}")
        return matches

    stem = base_path.stem
    suffix = base_path.suffix.lower()
    matches: List[Path] = []
    for candidate in sorted(base_path.parent.iterdir()):
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() != suffix:
            continue
        candidate_stem = candidate.stem
        if candidate_stem == stem or candidate_stem.startswith(f"{stem}_") or candidate_stem.startswith(f"{stem}-"):
            matches.append(candidate)

    if base_path not in matches:
        matches.insert(0, base_path)

    unique_matches: List[Path] = []
    seen: set[Path] = set()
    for candidate in matches:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_matches.append(candidate)
    return unique_matches


def load_template_bank(path: str) -> List[np.ndarray]:
    return [load_template(str(template_path)) for template_path in discover_template_paths(path)]



def clamp_box(x: float, y: float, w: float, h: float, img_w: int, img_h: int) -> Tuple[int, int, int, int]:
    x = max(0, min(int(round(x)), img_w - 1))
    y = max(0, min(int(round(y)), img_h - 1))
    w = max(1, min(int(round(w)), img_w - x))
    h = max(1, min(int(round(h)), img_h - y))
    return x, y, w, h



def iou_xywh(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)

    iw = max(0, ix1 - ix0)
    ih = max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0



def non_max_suppression(detections: List[dict], iou_thresh: float = 0.20) -> List[dict]:
    if not detections:
        return []

    order = sorted(range(len(detections)), key=lambda i: detections[i]["score"], reverse=True)
    keep: List[int] = []

    while order:
        i = order.pop(0)
        keep.append(i)
        survivors = []
        for j in order:
            same_kind = detections[i]["kind"] == detections[j]["kind"]
            overlap = iou_xywh(detections[i]["box"], detections[j]["box"])
            if same_kind and overlap > iou_thresh:
                continue
            survivors.append(j)
        order = survivors

    return [detections[i] for i in keep]


# ---------------------------
# Text helpers
# ---------------------------


def extract_words(page: fitz.Page, zoom: float) -> List[dict]:
    words: List[dict] = []
    for item in page.get_text("words"):
        x0, y0, x1, y1, text, *_ = item
        text = text.strip()
        if not text:
            continue
        words.append(
            {
                "text": text,
                "x0": x0 * zoom,
                "y0": y0 * zoom,
                "x1": x1 * zoom,
                "y1": y1 * zoom,
                "cx": (x0 + x1) * 0.5 * zoom,
                "cy": (y0 + y1) * 0.5 * zoom,
                "h": (y1 - y0) * zoom,
            }
        )
    return words



def build_text_suppressed_binary(page_bin: np.ndarray, words: Sequence[dict], expand: int = 3) -> np.ndarray:
    clean = page_bin.copy()
    h, w = clean.shape[:2]
    for word in words:
        x0 = max(0, int(word["x0"] - expand))
        y0 = max(0, int(word["y0"] - expand))
        x1 = min(w, int(word["x1"] + expand))
        y1 = min(h, int(word["y1"] + expand))
        clean[y0:y1, x0:x1] = 0
    return clean


IGNORE_WORDS = {
    "AWG", "WHT", "BLK", "RED", "GRN", "BRN", "BLU", "ORG", "ORN", "YEL", "VIO", "GRY", "PNK", "TAN",
    "WIRE", "DRAIN", "DRAIN_WIRE", "RTN", "OUT", "IN", "SPARE", "KEY", "PCBA", "EIOC", "VALVE",
}


NON_TERMINAL_MARKERS = {
    "BS",
    "STR",
}


TITLE_BLOCK_WORDS = {
    "REV", "ZONE", "DESCRIPTION", "DRAWN", "CHECK", "DATE", "INITIAL", "RELEASE", "SOURCE",
    "SYSTEM", "DRAWING", "SIZE", "SHEET", "SCALE", "RESEARCH", "DIMENSIONS", "INCHES",
    "DO", "NOT", "UNLESS", "OTHERWISE", "SPECIFIED", "INFORMATION", "SEE", "ME", "EE",
}


def is_probable_label(text: str) -> bool:
    text = (text or "").strip().upper()
    if not text:
        return False
    if text in IGNORE_WORDS or text in TITLE_BLOCK_WORDS:
        return False
    return True


def label_has_alnum(text: str) -> bool:
    return any(ch.isalnum() for ch in (text or ""))


def is_pin_like_label(text: str) -> bool:
    text = (text or "").strip().upper()
    if not is_probable_label(text):
        return False
    if text in NON_TERMINAL_MARKERS:
        return False
    return bool(re.fullmatch(r"[A-Z]?\d+[A-Z]?", text))


def horizontal_box_distance(word: dict, x: float) -> float:
    if word["x0"] <= x <= word["x1"]:
        return 0.0
    if x < word["x0"]:
        return word["x0"] - x
    return x - word["x1"]


def choose_endpoint_pin_label(words: Sequence[dict], x: float, y: float, side: str,
                              max_dx: int = 96, y_tol: int = 34) -> str:
    candidates: List[Tuple[float, str]] = []
    for word in words:
        text = word["text"].strip().upper()
        if not is_pin_like_label(text):
            continue
        if abs(word["cy"] - y) > y_tol:
            continue

        dx = horizontal_box_distance(word, x)
        if dx > max_dx:
            continue

        score = dx + 0.20 * abs(word["cy"] - y)
        candidates.append((score, text))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]



def choose_nearest_label(words: Sequence[dict], x: float, y: float, side: str, max_dx: int = 260, y_tol: int = 26) -> str:
    candidates: List[Tuple[float, str]] = []
    for word in words:
        text = word["text"].strip().upper()
        if not is_probable_label(text):
            continue
        if abs(word["cy"] - y) > y_tol:
            continue
        if side == "left":
            dx = x - word["x1"]
        else:
            dx = word["x0"] - x
        if 0 <= dx <= max_dx:
            score = dx + 0.20 * abs(word["cy"] - y)
            candidates.append((score, text))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def choose_box_aligned_label(words: Sequence[dict],
                             box: Tuple[int, int, int, int],
                             max_dy: int = 82,
                             x_pad: int = 24) -> str:
    x, y, w, _ = box
    box_cx = x + 0.5 * w
    candidates: List[Tuple[float, str]] = []

    for word in words:
        text = word["text"].strip().upper()
        if not is_probable_label(text):
            continue
        if not label_has_alnum(text):
            continue
        if word["y1"] > y + 8:
            continue

        dy = y - word["y1"]
        if dy < -4 or dy > max_dy:
            continue
        if word["cx"] < x - x_pad or word["cx"] > x + w + x_pad:
            continue

        dx = abs(word["cx"] - box_cx)
        score = dy + 0.25 * dx
        candidates.append((score, text))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


# ---------------------------
# Vector / raster segment extraction
# ---------------------------


@dataclass
class Segment:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def length(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def cy(self) -> float:
        return 0.5 * (self.y0 + self.y1)



def _append_if_horizontal(segments: List[Segment], p0: fitz.Point, p1: fitz.Point, zoom: float,
                          y_tol: float, min_len: float) -> None:
    x0, y0 = p0.x * zoom, p0.y * zoom
    x1, y1 = p1.x * zoom, p1.y * zoom
    if abs(y0 - y1) > y_tol:
        return
    if abs(x1 - x0) < min_len:
        return
    if x0 <= x1:
        segments.append(Segment(x0, y0, x1, y1))
    else:
        segments.append(Segment(x1, y1, x0, y0))



def extract_horizontal_segments_vector(page: fitz.Page, zoom: float, img_w: int) -> List[Segment]:
    min_len = max(22.0, img_w * 0.010)
    y_tol = max(1.5, zoom * 0.5)
    segments: List[Segment] = []

    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []

    for drawing in drawings:
        for item in drawing.get("items", []):
            if not item:
                continue
            op = item[0]
            try:
                if op == "l" and len(item) >= 3:
                    _append_if_horizontal(segments, item[1], item[2], zoom, y_tol, min_len)
                elif op == "re" and len(item) >= 2:
                    rect = item[1]
                    p_tl = fitz.Point(rect.x0, rect.y0)
                    p_tr = fitz.Point(rect.x1, rect.y0)
                    p_bl = fitz.Point(rect.x0, rect.y1)
                    p_br = fitz.Point(rect.x1, rect.y1)
                    _append_if_horizontal(segments, p_tl, p_tr, zoom, y_tol, min_len)
                    _append_if_horizontal(segments, p_bl, p_br, zoom, y_tol, min_len)
            except Exception:
                continue

    return merge_horizontal_segments(segments, y_tol=max(3.0, zoom * 0.8), gap_tol=max(10.0, img_w * 0.004))



def extract_horizontal_segments_raster(page_bin: np.ndarray) -> List[Segment]:
    img_h, img_w = page_bin.shape[:2]
    kernel_w = max(20, int(img_w * 0.015))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 1))
    horiz = cv2.morphologyEx(page_bin, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(horiz, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    segments: List[Segment] = []
    min_len = max(22.0, img_w * 0.015)

    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w < min_len:
            continue
        if h > max(6, int(img_h * 0.01)):
            continue
        cy = y + h * 0.5
        segments.append(Segment(x, cy, x + w, cy))

    return merge_horizontal_segments(segments, y_tol=max(3.0, img_h * 0.002), gap_tol=max(8.0, img_w * 0.004))



def merge_horizontal_segments(segments: Sequence[Segment], y_tol: float, gap_tol: float) -> List[Segment]:
    if not segments:
        return []

    ordered = sorted(segments, key=lambda s: (s.cy, s.x0))
    merged: List[Segment] = []

    for seg in ordered:
        if not merged:
            merged.append(seg)
            continue

        prev = merged[-1]
        if (
            abs(prev.cy - seg.cy) <= y_tol and
            seg.x0 <= prev.x1 + gap_tol and
            seg.x1 >= prev.x0 - gap_tol
        ):
            merged[-1] = Segment(prev.x0, 0.5 * (prev.cy + seg.cy), max(prev.x1, seg.x1), 0.5 * (prev.cy + seg.cy))
        else:
            merged.append(seg)

    return merged


# ---------------------------
# Template matching around endpoint anchors
# ---------------------------

MATCH_SCALES = (0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.35, 1.50)


def side_template_anchor(template: np.ndarray, side: str) -> Tuple[float, float]:
    h, w = template.shape[:2]
    if side == "left":
        return float(w - 1), 0.5 * h
    return 0.0, 0.5 * h



def build_endpoint_patch(endpoint_x: float, endpoint_y: float, side: str,
                         img_w: int, img_h: int,
                         max_template_w: int, max_template_h: int) -> Tuple[int, int, int, int]:
    outward_w = int(max_template_w * 2.4)
    inward_w = int(max_template_w * 1.4)
    patch_h = int(max_template_h * 3.2)

    if side == "left":
        x0 = endpoint_x - outward_w
        x1 = endpoint_x + inward_w
    else:
        x0 = endpoint_x - inward_w
        x1 = endpoint_x + outward_w

    y0 = endpoint_y - patch_h / 2
    y1 = endpoint_y + patch_h / 2
    x0, y0, _, _ = clamp_box(x0, y0, 1, 1, img_w, img_h)
    x1 = max(x0 + 2, min(int(round(x1)), img_w))
    y1 = max(y0 + 2, min(int(round(y1)), img_h))
    return x0, y0, x1, y1



def best_template_match_local(page_bin: np.ndarray,
                              template_bin: np.ndarray,
                              endpoint_xy: Tuple[float, float],
                              side: str,
                              scales: Sequence[float],
                              anchor_distance_weight: float = 0.12,
                              prefer_local_alignment: bool = False) -> Optional[dict]:
    img_h, img_w = page_bin.shape[:2]
    max_tw = int(template_bin.shape[1] * max(scales))
    max_th = int(template_bin.shape[0] * max(scales))
    px0, py0, px1, py1 = build_endpoint_patch(endpoint_xy[0], endpoint_xy[1], side, img_w, img_h, max_tw, max_th)
    patch = page_bin[py0:py1, px0:px1]
    if patch.size == 0:
        return None

    best: Optional[dict] = None
    base_anchor_x, base_anchor_y = side_template_anchor(template_bin, side)

    for scale in scales:
        tw = max(4, int(round(template_bin.shape[1] * scale)))
        th = max(4, int(round(template_bin.shape[0] * scale)))
        if tw >= patch.shape[1] or th >= patch.shape[0]:
            continue

        templ = cv2.resize(template_bin, (tw, th), interpolation=cv2.INTER_NEAREST)
        result = cv2.matchTemplate(patch, templ, cv2.TM_CCOEFF_NORMED)
        if result.size == 0:
            continue

        if prefer_local_alignment:
            rows = np.arange(result.shape[0], dtype=np.float32)[:, None]
            cols = np.arange(result.shape[1], dtype=np.float32)[None, :]
            anchor_x = (px0 + cols) + base_anchor_x * scale
            anchor_y = (py0 + rows) + base_anchor_y * scale
            norm_dist = np.hypot(anchor_x - endpoint_xy[0], anchor_y - endpoint_xy[1]) / max(8.0, 0.5 * (tw + th))
            adjusted = result.astype(np.float32) - (anchor_distance_weight * norm_dist.astype(np.float32))

            best_loc = np.unravel_index(int(np.argmax(adjusted)), adjusted.shape)
            max_loc = (int(best_loc[1]), int(best_loc[0]))
            max_val = float(result[best_loc])
            adjusted_score = float(adjusted[best_loc])
        else:
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            x = px0 + max_loc[0]
            y = py0 + max_loc[1]
            anchor_x = x + base_anchor_x * scale
            anchor_y = y + base_anchor_y * scale
            anchor_dist = float(np.hypot(anchor_x - endpoint_xy[0], anchor_y - endpoint_xy[1]))
            norm_dist = anchor_dist / max(8.0, 0.5 * (tw + th))
            adjusted_score = float(max_val) - anchor_distance_weight * norm_dist

        x = px0 + max_loc[0]
        y = py0 + max_loc[1]
        anchor_x = x + base_anchor_x * scale
        anchor_y = y + base_anchor_y * scale
        anchor_dist = float(np.hypot(anchor_x - endpoint_xy[0], anchor_y - endpoint_xy[1]))

        candidate = {
            "score": adjusted_score,
            "raw_score": float(max_val),
            "anchor_dist": anchor_dist,
            "box": (int(x), int(y), int(tw), int(th)),
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    return best



def classify_endpoint(endpoint_xy: Tuple[float, float], side: str, page_bin: np.ndarray,
                      templates: Dict[str, Sequence[np.ndarray]],
                      score_thresh: float, ferrule_score_thresh: float,
                      score_margin: float) -> dict:
    hits: Dict[str, dict] = {}

    for kind, templ_list in templates.items():
        best_hit: Optional[dict] = None
        for templ in templ_list:
            hit = best_template_match_local(
                page_bin,
                templ,
                endpoint_xy,
                side,
                scales=MATCH_SCALES,
            )
            if hit is None:
                continue
            if best_hit is None or hit["score"] > best_hit["score"]:
                best_hit = hit
        if best_hit is not None:
            hits[kind] = best_hit

    if not hits:
        return {
            "kind": "unknown",
            "score": -1.0,
            "box": None,
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": -1.0,
        }

    ranked = sorted(hits.items(), key=lambda item: item[1]["score"], reverse=True)
    best_kind, best_hit = ranked[0]
    second_score = ranked[1][1]["score"] if len(ranked) > 1 else -1.0

    needed = ferrule_score_thresh if best_kind == "ferrule" else score_thresh
    if best_hit["score"] < needed or (best_hit["score"] - second_score) < score_margin:
        final_kind = "unknown"
    else:
        final_kind = best_kind

    return {
        "kind": final_kind,
        "score": best_hit["score"],
        "box": best_hit["box"],
        "male_score": hits.get("male", {}).get("score", -1.0),
        "female_score": hits.get("female", {}).get("score", -1.0),
        "ferrule_score": hits.get("ferrule", {}).get("score", -1.0),
    }


def refine_misaligned_terminal_boxes(page_bin: np.ndarray,
                                     detections: Sequence[dict],
                                     side_templates: Dict[str, Dict[str, Sequence[np.ndarray]]]) -> List[dict]:
    refined: List[dict] = []

    for det in detections:
        updated = dict(det)
        if det["kind"] not in {"male", "female"}:
            refined.append(updated)
            continue

        x, y, w, h = det["box"]
        old_dy = abs((y + (0.5 * h)) - det["endpoint"][1])
        if old_dy <= max(20.0, h * 0.70):
            refined.append(updated)
            continue

        best_hit: Optional[dict] = None
        for templ in side_templates[det["side"]][det["kind"]]:
            hit = best_template_match_local(
                page_bin,
                templ,
                det["endpoint"],
                det["side"],
                scales=MATCH_SCALES,
                prefer_local_alignment=True,
            )
            if hit is None:
                continue
            if best_hit is None or hit["score"] > best_hit["score"]:
                best_hit = hit

        if best_hit is None:
            refined.append(updated)
            continue

        new_x, new_y, new_w, new_h = best_hit["box"]
        new_dy = abs((new_y + (0.5 * new_h)) - det["endpoint"][1])
        if new_dy + 6.0 < old_dy:
            updated["x"] = new_x
            updated["y"] = new_y
            updated["w"] = new_w
            updated["h"] = new_h
            updated["box"] = best_hit["box"]
            updated["score"] = max(det["score"], best_hit["score"])
            if det["kind"] == "male":
                updated["male_score"] = max(det["male_score"], best_hit["score"])
            elif det["kind"] == "female":
                updated["female_score"] = max(det["female_score"], best_hit["score"])

        refined.append(updated)

    return refined


def ferrule_shape_metrics(page_bin: np.ndarray,
                          box: Tuple[int, int, int, int],
                          side: str) -> dict:
    x, y, w, h = box
    roi = page_bin[y:y + h, x:x + w]
    if roi.size == 0:
        return {
            "component_area_ratio": 0.0,
            "component_extent": 0.0,
            "component_aspect": 0.0,
            "component_span": 0.0,
            "hole_area_ratio": 0.0,
            "outer_density": 0.0,
        }

    roi_bin = (roi > 0).astype(np.uint8)
    if side == "right":
        outer = roi_bin[:, max(0, int(round(w * 0.55))):]
    else:
        outer = roi_bin[:, :max(1, int(round(w * 0.45)))]

    if outer.size == 0:
        outer = roi_bin

    kernel = np.ones((3, 3), dtype=np.uint8)
    outer_joined = cv2.dilate(outer, kernel, iterations=1)

    component_area = 0
    component_extent = 0.0
    component_aspect = 0.0
    component_span = 0.0

    comp_count, _, stats, _ = cv2.connectedComponentsWithStats(outer_joined, 8)
    for comp_index in range(1, comp_count):
        _, comp_y, comp_w, comp_h, comp_area = stats[comp_index]
        if comp_area <= component_area:
            continue
        component_area = int(comp_area)
        component_extent = float(comp_area) / float(max(1, comp_w * comp_h))
        component_aspect = float(comp_w) / float(max(1, comp_h))
        component_span = float(comp_h) / float(max(1, roi_bin.shape[0]))

    hole_area = 0.0
    contours, hierarchy = cv2.findContours((outer_joined * 255).astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is not None:
        hierarchy = hierarchy[0]
        for contour_index, contour in enumerate(contours):
            parent_index = hierarchy[contour_index][3]
            if parent_index == -1:
                continue
            area = cv2.contourArea(contour)
            if area > hole_area:
                hole_area = area

    roi_area = float(max(1, roi_bin.size))
    outer_area = float(max(1, outer_joined.size))
    return {
        "component_area_ratio": float(component_area) / roi_area,
        "component_extent": component_extent,
        "component_aspect": component_aspect,
        "component_span": component_span,
        "hole_area_ratio": float(hole_area) / outer_area,
        "outer_density": float(np.count_nonzero(outer_joined)) / outer_area,
    }


def is_possible_ferrule_shape(metrics: dict) -> bool:
    hole_ferrule = (
        metrics["hole_area_ratio"] >= 0.030 and
        metrics["component_area_ratio"] >= 0.090 and
        metrics["component_extent"] >= 0.20 and
        1.30 <= metrics["component_aspect"] <= 2.40
    )
    compact_ferrule = (
        metrics["component_area_ratio"] >= 0.095 and
        metrics["component_extent"] >= 0.28 and
        1.05 <= metrics["component_aspect"] <= 2.10 and
        metrics["component_span"] >= 0.90 and
        metrics["outer_density"] >= 0.20
    )
    return hole_ferrule or compact_ferrule


def is_strong_ferrule_shape(metrics: dict) -> bool:
    hole_ferrule = (
        metrics["hole_area_ratio"] >= 0.050 and
        metrics["component_area_ratio"] >= 0.100 and
        metrics["component_extent"] >= 0.24 and
        1.50 <= metrics["component_aspect"] <= 2.20
    )
    compact_ferrule = (
        metrics["component_area_ratio"] >= 0.100 and
        metrics["component_extent"] >= 0.30 and
        1.20 <= metrics["component_aspect"] <= 1.80 and
        metrics["component_span"] >= 0.95 and
        metrics["outer_density"] >= 0.23
    )
    return hole_ferrule or compact_ferrule


# ---------------------------
# Endpoint candidate generation
# ---------------------------


def endpoint_density(clean_bin: np.ndarray, x: float, y: float, side: str, outward_r: int = 22, inward_r: int = 10, y_band: int = 18) -> float:
    h, w = clean_bin.shape[:2]
    if side == "left":
        x0 = max(0, int(x - outward_r))
        x1 = min(w, int(x + inward_r))
    else:
        x0 = max(0, int(x - inward_r))
        x1 = min(w, int(x + outward_r))
    y0 = max(0, int(y - y_band))
    y1 = min(h, int(y + y_band))
    roi = clean_bin[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    return float(cv2.countNonZero(roi)) / float(roi.size)



def has_nearby_text(words: Sequence[dict], x: float, y: float, side: str,
                    max_dx: int = 320, y_tol: int = 26) -> bool:
    for word in words:
        if not is_probable_label(word["text"]):
            continue
        if abs(word["cy"] - y) > y_tol:
            continue
        if side == "left":
            dx = x - word["x1"]
        else:
            dx = word["x0"] - x
        if 0 <= dx <= max_dx:
            return True
    return False


def has_supporting_pin_label(words: Sequence[dict], x: float, y: float,
                             max_dx: int = 96, y_tol: int = 34) -> bool:
    return bool(choose_endpoint_pin_label(words, x, y, side="left", max_dx=max_dx, y_tol=y_tol))


def collect_endpoint_candidates(segments: Sequence[Segment], clean_bin: np.ndarray,
                                words: Sequence[dict], img_w: int, img_h: int) -> List[dict]:
    candidates: List[dict] = []
    main_min_len = max(25.0, img_w * 0.015)
    short_min_len = max(22.0, img_w * 0.010)
    max_len = img_w * 0.80
    hard_top_band = img_h * 0.07
    soft_top_band = img_h * 0.14
    soft_bottom_band = img_h * 0.86
    hard_bottom_band = img_h * 0.93

    for segment_id, seg in enumerate(segments):
        if seg.length < short_min_len:
            continue
        if seg.length > max_len:
            continue
        if seg.cy < hard_top_band or seg.cy > hard_bottom_band:
            continue

        left_pin = has_supporting_pin_label(words, seg.x0, seg.cy)
        right_pin = has_supporting_pin_label(words, seg.x1, seg.cy)
        left_text = has_nearby_text(words, seg.x0, seg.cy, "left") or left_pin
        right_text = has_nearby_text(words, seg.x1, seg.cy, "right") or right_pin
        segment_has_text = left_text or right_text
        short_segment = seg.length < main_min_len
        if short_segment and not (left_pin or right_pin):
            continue
        if seg.cy < soft_top_band and not segment_has_text:
            continue
        if seg.cy > soft_bottom_band and not segment_has_text:
            continue
        in_top_right_revision_block = (
            seg.cy < soft_top_band and
            seg.x0 > img_w * 0.70 and
            seg.x1 > img_w * 0.82 and
            seg.length < img_w * 0.30
        )
        if in_top_right_revision_block:
            continue
        in_bottom_right_title_block = (
            seg.cy > img_h * 0.78 and
            seg.x0 > img_w * 0.55 and
            seg.x1 > img_w * 0.94 and
            seg.length > img_w * 0.18
        )
        if in_bottom_right_title_block and not segment_has_text:
            continue

        for side, x in (("left", seg.x0), ("right", seg.x1)):
            y = seg.cy
            density = endpoint_density(clean_bin, x, y, side)
            near_text = left_text if side == "left" else right_text
            near_outer_page = x < img_w * 0.18 or x > img_w * 0.82

            if not near_text and density < 0.020:
                continue
            if seg.length > img_w * 0.40 and not near_text:
                continue
            if near_outer_page and not near_text and density < 0.035:
                continue

            candidates.append(
                {
                    "endpoint": (float(x), float(y)),
                    "side": side,
                    "segment_len": seg.length,
                    "density": density,
                    "near_text": near_text,
                    "segment_id": segment_id,
                    "short_segment": short_segment,
                }
            )

    return dedupe_candidates_by_anchor(candidates, dist_thresh=max(12.0, img_w * 0.004))



def dedupe_candidates_by_anchor(candidates: Sequence[dict], dist_thresh: float) -> List[dict]:
    if not candidates:
        return []

    ordered = sorted(
        candidates,
        key=lambda c: (not c.get("near_text", False), -c["segment_len"], c["endpoint"][1], c["endpoint"][0])
    )
    kept: List[dict] = []

    for cand in ordered:
        x, y = cand["endpoint"]
        duplicate = False
        for prev in kept:
            px, py = prev["endpoint"]
            if cand["side"] == prev["side"] and np.hypot(x - px, y - py) <= dist_thresh:
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)

    return kept


def dedupe_detections(detections: Sequence[dict], endpoint_dist_thresh: float, iou_thresh: float = 0.20) -> List[dict]:
    if not detections:
        return []

    ordered = sorted(
        detections,
        key=lambda d: (
            not d.get("has_label", False),
            d["kind"] == "unknown",
            -d["score"],
        ),
    )
    kept: List[dict] = []

    for det in ordered:
        ex, ey = det["endpoint"]
        duplicate = False
        for prev in kept:
            px, py = prev["endpoint"]
            if (
                det.get("short_segment") and prev.get("short_segment") and
                det.get("segment_id") == prev.get("segment_id")
            ):
                duplicate = True
                break
            if np.hypot(ex - px, ey - py) <= endpoint_dist_thresh:
                duplicate = True
                break
            if (
                iou_xywh(det["box"], prev["box"]) > iou_thresh and
                abs(ex - px) <= max(det["w"], prev["w"]) * 0.65 and
                abs(ey - py) <= max(det["h"], prev["h"]) * 0.65
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(det)

    return kept


# ---------------------------
# Drawing / annotation helpers
# ---------------------------


def image_rect_to_pdf_rect(x: int, y: int, w: int, h: int,
                           page_rect: fitz.Rect, img_w: int, img_h: int) -> fitz.Rect:
    sx = page_rect.width / img_w
    sy = page_rect.height / img_h
    return fitz.Rect(x * sx, y * sy, (x + w) * sx, (y + h) * sy)



def annotate_page(page: fitz.Page, detections: Sequence[dict], img_shape: Tuple[int, int, int]) -> Tuple[int, int, int, int, int]:
    img_h, img_w = img_shape[:2]
    male_count = sum(1 for d in detections if d["kind"] == "male")
    female_count = sum(1 for d in detections if d["kind"] == "female")
    ferrule_count = sum(1 for d in detections if d["kind"] == "ferrule")
    unknown_count = sum(1 for d in detections if d["kind"] == "unknown")
    total_count = male_count + female_count + ferrule_count

    for d in detections:
        rect_pdf = image_rect_to_pdf_rect(d["x"], d["y"], d["w"], d["h"], page.rect, img_w, img_h)

        if d["kind"] == "male":
            color = (1, 0, 0)
        elif d["kind"] == "female":
            color = (0, 0, 1)
        elif d["kind"] == "ferrule":
            color = (0, 0.6, 0)
        else:
            color = (1, 0.5, 0)

        page.draw_rect(rect_pdf, color=color, width=0.8)
        label = d["id"] if not d["pin"] else f'{d["id"]}:{d["pin"]}'
        label_y = rect_pdf.y0 - 2
        if label_y < 8:
            label_y = rect_pdf.y1 + 8
        page.insert_text(fitz.Point(rect_pdf.x0, label_y), label, fontsize=6, color=color)

    summary = fitz.Rect(page.rect.width - 180, 20, page.rect.width - 20, 120)
    page.draw_rect(summary, color=(0, 0, 0), width=1)
    page.insert_textbox(
        summary,
        (
            f"Male: {male_count}\n"
            f"Female: {female_count}\n"
            f"Ferrule: {ferrule_count}\n"
            f"Unknown: {unknown_count}\n"
            f"Total: {total_count}"
        ),
        fontsize=10,
        color=(0, 0, 0),
    )
    return male_count, female_count, ferrule_count, unknown_count, total_count



def draw_debug_image(bgr: np.ndarray, detections: Sequence[dict], candidates: Sequence[dict], out_path: Path) -> None:
    dbg = bgr.copy()

    for cand in candidates:
        x, y = cand["endpoint"]
        color = (200, 200, 0) if cand["side"] == "left" else (180, 255, 255)
        cv2.circle(dbg, (int(round(x)), int(round(y))), 4, color, 1)

    for d in detections:
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]
        if d["kind"] == "male":
            color = (0, 0, 255)
        elif d["kind"] == "female":
            color = (255, 0, 0)
        elif d["kind"] == "ferrule":
            color = (0, 180, 0)
        else:
            color = (0, 165, 255)
        cv2.rectangle(dbg, (x, y), (x + w, y + h), color, 1)
        text = d["id"] if not d["pin"] else f'{d["id"]}:{d["pin"]}'
        cv2.putText(dbg, text, (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

    cv2.imwrite(str(out_path), dbg)


# ---------------------------
# Main detection pipeline
# ---------------------------


def _normalize_template_group(group) -> List[np.ndarray]:
    if isinstance(group, np.ndarray):
        return [group]
    return list(group)


def build_side_templates(male_left, male_right,
                         female_left, female_right,
                         ferrule_left, ferrule_right) -> Dict[str, Dict[str, List[np.ndarray]]]:
    return {
        "left": {
            "male": _normalize_template_group(male_left),
            "female": _normalize_template_group(female_left),
            "ferrule": _normalize_template_group(ferrule_left),
        },
        "right": {
            "male": _normalize_template_group(male_right),
            "female": _normalize_template_group(female_right),
            "ferrule": _normalize_template_group(ferrule_right),
        },
    }



def detect_page(page: fitz.Page,
                side_templates: Dict[str, Dict[str, Sequence[np.ndarray]]],
                zoom: float,
                score_thresh: float,
                ferrule_score_thresh: float,
                score_margin: float) -> Tuple[np.ndarray, List[dict], List[dict]]:
    bgr, gray, _ = render_page(page, zoom=zoom)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)
    page_bin = to_binary_inv(gray)
    clean_bin = build_text_suppressed_binary(page_bin, words, expand=max(2, int(round(zoom))))

    segments = extract_horizontal_segments_vector(page, zoom, img_w)
    if len(segments) < 10:
        segments = extract_horizontal_segments_raster(clean_bin)

    candidates = collect_endpoint_candidates(segments, clean_bin, words, img_w, img_h)

    raw_detections: List[dict] = []
    counters = {"male": 1, "female": 1, "ferrule": 1, "unknown": 1}

    def build_detection(cand: dict) -> Optional[dict]:
        side = cand["side"]
        endpoint = cand["endpoint"]
        result = classify_endpoint(
            endpoint_xy=endpoint,
            side=side,
            page_bin=clean_bin,
            templates=side_templates[side],
            score_thresh=score_thresh,
            ferrule_score_thresh=ferrule_score_thresh,
            score_margin=score_margin,
        )

        if result["box"] is None:
            return None

        x, y, w, h = result["box"]
        wire_label = choose_nearest_label(words, endpoint[0], endpoint[1], side=side)
        pin_label = choose_endpoint_pin_label(words, endpoint[0], endpoint[1], side=side)
        strong_female_signal = (
            result["female_score"] >= max(score_thresh + 0.23, 0.56) and
            result["female_score"] >= result["male_score"] + 0.12 and
            result["female_score"] >= result["ferrule_score"] + 0.15
        )
        if not pin_label and (result["kind"] == "female" or strong_female_signal):
            pin_label = choose_box_aligned_label(words, result["box"])
        pin = pin_label or wire_label
        if pin and not label_has_alnum(pin) and not cand.get("near_text", False):
            pin = ""
            pin_label = ""
        if pin in NON_TERMINAL_MARKERS:
            return None

        has_label = is_probable_label(pin)
        numeric_pin_label = bool(pin_label) and any(ch.isdigit() for ch in pin_label)
        ferrule_metrics = ferrule_shape_metrics(clean_bin, result["box"], side)
        ferrule_shape_possible = is_possible_ferrule_shape(ferrule_metrics)
        ferrule_shape_strong = is_strong_ferrule_shape(ferrule_metrics)

        if result["kind"] == "ferrule" and not ferrule_shape_possible:
            result["kind"] = "unknown"
            result["score"] = result["ferrule_score"]

        ferrule_vs_male_gap = result["ferrule_score"] - result["male_score"]
        ferrule_vs_female_gap = result["ferrule_score"] - result["female_score"]
        if (
            result["kind"] == "unknown" and
            ferrule_shape_strong and
            result["ferrule_score"] >= (ferrule_score_thresh - 0.015) and
            ferrule_vs_male_gap >= -0.025 and
            ferrule_vs_female_gap >= 0.05
        ):
            result["kind"] = "ferrule"
            result["score"] = result["ferrule_score"]

        if result["kind"] == "unknown" and strong_female_signal and has_label:
            result["kind"] = "female"
            result["score"] = result["female_score"]

        strong_male_signal = (
            numeric_pin_label and
            result["male_score"] >= max(score_thresh + 0.09, 0.42) and
            result["male_score"] >= result["female_score"] + 0.02 and
            result["male_score"] >= result["ferrule_score"] + 0.08
        )
        if result["kind"] == "unknown" and strong_male_signal:
            result["kind"] = "male"
            result["score"] = result["male_score"]

        if (
            result["kind"] == "unknown" and
            numeric_pin_label and
            result["female_score"] >= 0.45 and
            result["female_score"] >= result["male_score"] - 0.03 and
            result["female_score"] >= result["ferrule_score"] + 0.10
        ):
            result["kind"] = "female"
            result["score"] = result["female_score"]

        if not has_label:
            if cand["segment_len"] > img_w * 0.40:
                result["kind"] = "unknown"
            else:
                unlabeled_thresh = ferrule_score_thresh + 0.06 if result["kind"] == "ferrule" else score_thresh + 0.08
                if not cand.get("near_text", False) and result["kind"] in {"male", "female"}:
                    unlabeled_thresh = max(unlabeled_thresh, score_thresh + 0.18)
                if result["score"] < unlabeled_thresh:
                    result["kind"] = "unknown"

        return {
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "box": (x, y, w, h),
            "score": result["score"],
            "kind": result["kind"],
            "pin": pin,
            "pin_label": pin_label,
            "wire_label": wire_label,
            "id": det_id,
            "side": side,
            "male_score": result["male_score"],
            "female_score": result["female_score"],
            "ferrule_score": result["ferrule_score"],
            "endpoint": endpoint,
            "has_label": has_label,
            "segment_len": cand["segment_len"],
            "near_text": cand.get("near_text", False),
            "segment_id": cand.get("segment_id"),
            "short_segment": cand.get("short_segment", False),
            "ferrule_shape_possible": ferrule_shape_possible,
            "ferrule_shape_strong": ferrule_shape_strong,
            "ferrule_hole_ratio": ferrule_metrics["hole_area_ratio"],
        }

    for cand in candidates:
        det = build_detection(cand)
        if det is not None:
            raw_detections.append(det)

    raw_detections = refine_misaligned_terminal_boxes(clean_bin, raw_detections, side_templates)
    detections = dedupe_detections(raw_detections, endpoint_dist_thresh=max(18.0, img_w * 0.006), iou_thresh=0.20)
    detections = sorted(detections, key=lambda d: (d["y"], d["x"]))

    # Re-number after NMS so IDs stay compact.
    renum = {"male": 1, "female": 1, "ferrule": 1, "unknown": 1}
    for det in detections:
        if det["kind"] == "male":
            det["id"] = f"M{renum['male']}"
            renum["male"] += 1
        elif det["kind"] == "female":
            det["id"] = f"F{renum['female']}"
            renum["female"] += 1
        elif det["kind"] == "ferrule":
            det["id"] = f"FE{renum['ferrule']}"
            renum["ferrule"] += 1
        else:
            det["id"] = f"U{renum['unknown']}"
            renum["unknown"] += 1

    return bgr, candidates, detections



def process_pdf(input_pdf: str,
                output_pdf: str,
                male_left_template_path: str,
                male_right_template_path: str,
                female_left_template_path: str,
                female_right_template_path: str,
                ferrule_left_template_path: str,
                ferrule_right_template_path: str,
                zoom: float,
                debug_dir: str,
                score_thresh: float,
                ferrule_score_thresh: float,
                score_margin: float) -> None:
    input_pdf = str(input_pdf)
    output_pdf = str(output_pdf)
    debug_dir_path = Path(debug_dir)
    debug_dir_path.mkdir(parents=True, exist_ok=True)

    male_left = load_template_bank(male_left_template_path)
    male_right = load_template_bank(male_right_template_path)
    female_left = load_template_bank(female_left_template_path)
    female_right = load_template_bank(female_right_template_path)
    ferrule_left = load_template_bank(ferrule_left_template_path)
    ferrule_right = load_template_bank(ferrule_right_template_path)

    side_templates = build_side_templates(
        male_left, male_right, female_left, female_right, ferrule_left, ferrule_right
    )

    print(
        "Loaded template variants: "
        f"male_left={len(male_left)} male_right={len(male_right)} "
        f"female_left={len(female_left)} female_right={len(female_right)} "
        f"ferrule_left={len(ferrule_left)} ferrule_right={len(ferrule_right)}"
    )

    doc = fitz.open(input_pdf)

    total_male = 0
    total_female = 0
    total_ferrule = 0
    total_unknown = 0

    for page_index in range(len(doc)):
        page = doc[page_index]
        bgr, candidates, detections = detect_page(
            page,
            side_templates=side_templates,
            zoom=zoom,
            score_thresh=score_thresh,
            ferrule_score_thresh=ferrule_score_thresh,
            score_margin=score_margin,
        )

        male_count, female_count, ferrule_count, unknown_count, _ = annotate_page(page, detections, bgr.shape)
        total_male += male_count
        total_female += female_count
        total_ferrule += ferrule_count
        total_unknown += unknown_count

        draw_debug_image(bgr, detections, candidates, debug_dir_path / f"page_{page_index + 1:03d}.png")

        print(
            f"Page {page_index + 1}: male={male_count}, female={female_count}, "
            f"ferrule={ferrule_count}, unknown={unknown_count}, candidates={len(candidates)}"
        )

    doc.save(output_pdf)
    doc.close()

    print(f"Saved: {output_pdf}")
    print(
        f"Document total: male={total_male}, female={total_female}, "
        f"ferrule={total_ferrule}, unknown={total_unknown}, total={total_male + total_female + total_ferrule}"
    )


# ---------------------------
# CLI
# ---------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Endpoint-first cable assembly terminal counter")
    parser.add_argument("input_pdf", help="Input PDF")
    parser.add_argument("male_left_template", help="Base template path or directory, e.g. male_left.png")
    parser.add_argument("male_right_template", help="Base template path or directory, e.g. male_right.png")
    parser.add_argument("female_left_template", help="Base template path or directory, e.g. female_left.png")
    parser.add_argument("female_right_template", help="Base template path or directory, e.g. female_right.png")
    parser.add_argument("ferrule_left_template", help="Base template path or directory, e.g. ferrule_left.png")
    parser.add_argument("ferrule_right_template", help="Base template path or directory, e.g. ferrule_right.png")
    parser.add_argument("-o", "--output", default="output_endpoint_first.pdf", help="Annotated PDF output path")
    parser.add_argument("--zoom", type=float, default=4.0, help="PDF render zoom")
    parser.add_argument("--debug-dir", default="debug_endpoint_first", help="Directory for debug images")
    parser.add_argument("--score-thresh", type=float, default=0.33,
                        help="Minimum adjusted score for male/female")
    parser.add_argument("--ferrule-score-thresh", type=float, default=0.24,
                        help="Minimum adjusted score for ferrule")
    parser.add_argument("--score-margin", type=float, default=0.03,
                        help="Minimum gap between best and second-best class")

    args = parser.parse_args()

    process_pdf(
        input_pdf=args.input_pdf,
        output_pdf=args.output,
        male_left_template_path=args.male_left_template,
        male_right_template_path=args.male_right_template,
        female_left_template_path=args.female_left_template,
        female_right_template_path=args.female_right_template,
        ferrule_left_template_path=args.ferrule_left_template,
        ferrule_right_template_path=args.ferrule_right_template,
        zoom=args.zoom,
        debug_dir=args.debug_dir,
        score_thresh=args.score_thresh,
        ferrule_score_thresh=args.ferrule_score_thresh,
        score_margin=args.score_margin,
    )


if __name__ == "__main__":
    main()
