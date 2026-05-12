import argparse
import re
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import numpy as np


PIN_TOKEN_RE = re.compile(r"^(?=.*\d)[A-Z0-9]+(?:-[A-Z0-9]+)*$", re.IGNORECASE)
NUMERIC_PIN_RE = re.compile(r"^\d+$")
EDGE_ENDPOINT_LABEL_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$", re.IGNORECASE)


def parse_pin_value_set(raw):
    if not raw:
        return set()
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def pin_is_dummy(pin_text, kind, dummy_male_pins=None, dummy_female_pins=None):
    pin = (pin_text or "").strip().upper()
    if not pin:
        return False
    if kind == "male":
        return pin in (dummy_male_pins or set())
    if kind == "female":
        return pin in (dummy_female_pins or set())
    return False


# ---------------------------
# Basic image / PDF helpers
# ---------------------------

def render_page(page, zoom=4.0):
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img, gray, zoom


def to_binary_inv(gray):
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def load_template(path):
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Cannot read template: {path}")
    return to_binary_inv(img)


def clamp_box(x, y, w, h, img_w, img_h):
    x = max(0, min(int(round(x)), img_w - 1))
    y = max(0, min(int(round(y)), img_h - 1))
    w = max(1, min(int(round(w)), img_w - x))
    h = max(1, min(int(round(h)), img_h - y))
    return x, y, w, h


# ---------------------------
# Text extraction / pin labels
# ---------------------------

def extract_words(page, zoom):
    words = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text, *_ = w
        text = text.strip()
        if not text:
            continue
        words.append({
            "text": text,
            "x0": x0 * zoom,
            "y0": y0 * zoom,
            "x1": x1 * zoom,
            "y1": y1 * zoom,
            "cx": ((x0 + x1) * 0.5) * zoom,
            "cy": ((y0 + y1) * 0.5) * zoom,
        })
    return words


def is_pin_token(text):
    text = text.strip().upper()
    if not text:
        return False
    return bool(PIN_TOKEN_RE.fullmatch(text))


def extract_candidate_pin_numbers_from_words(words):
    nums = []

    for i, w in enumerate(words):
        txt = w["text"].strip().upper()
        if not is_pin_token(txt):
            continue
        if any(ch.isalpha() for ch in txt) and any(ch.isdigit() for ch in txt) and len(txt) > 4:
            continue

        # Reject row text near wire specs like "22 AWG" so we do not treat
        # wire gauge / color tokens as terminal labels.
        reject = False
        for j, other in enumerate(words):
            if i == j:
                continue
            same_row = abs(other["cy"] - w["cy"]) <= 20
            close_x = abs(other["cx"] - w["cx"]) <= 160
            if same_row and close_x and other["text"].strip().upper() == "AWG":
                reject = True
                break

        if not reject:
            ww = dict(w)
            ww["text"] = txt
            nums.append(ww)

    return nums


def extract_numeric_pin_numbers_from_words(words):
    nums = []

    for i, w in enumerate(words):
        txt = w["text"].strip().upper()
        if not NUMERIC_PIN_RE.fullmatch(txt):
            continue

        reject = False
        for j, other in enumerate(words):
            if i == j:
                continue
            same_row = abs(other["cy"] - w["cy"]) <= 20
            close_x = abs(other["cx"] - w["cx"]) <= 160
            if same_row and close_x and other["text"].strip().upper() == "AWG":
                reject = True
                break

        if not reject:
            ww = dict(w)
            ww["text"] = txt
            nums.append(ww)

    return nums


def extract_candidate_pin_numbers(page, zoom):
    words = extract_words(page, zoom)
    return extract_candidate_pin_numbers_from_words(words)


# ---------------------------
# Split into left/right columns
# ---------------------------

def cluster_numbers_by_x(numbers, x_tol=70):
    if not numbers:
        return []

    nums = sorted(numbers, key=lambda n: n["cx"])
    clusters = []
    current = [nums[0]]

    for n in nums[1:]:
        mean_x = sum(x["cx"] for x in current) / len(current)
        if abs(n["cx"] - mean_x) <= x_tol:
            current.append(n)
        else:
            clusters.append(current)
            current = [n]

    clusters.append(current)
    return [c for c in clusters if len(c) >= 2]


def split_pin_number_columns(numbers):
    clusters = cluster_numbers_by_x(numbers, x_tol=70)
    if len(clusters) < 2:
        return [], []

    clusters = sorted(
        clusters,
        key=lambda c: (-len(c), sum(n["cx"] for n in c) / len(c))
    )[:2]
    clusters = sorted(clusters, key=lambda c: sum(n["cx"] for n in c) / len(c))

    left_cluster = sorted(clusters[0], key=lambda n: (n["cy"], n["cx"]))
    right_cluster = sorted(clusters[1], key=lambda n: (n["cy"], n["cx"]))
    return left_cluster, right_cluster


def build_directional_roi(num, direction, template_bin, page_bin, gap=2, y_offset=3, width_scale=5.0, height_scale=3.5):
    img_h, img_w = page_bin.shape[:2]
    th, tw = template_bin.shape[:2]

    roi_w = max(28, int(tw * width_scale))
    roi_h = max(20, int(th * height_scale))
    cy = num["cy"] + y_offset

    if direction == "left":
        x1 = num["x0"] - gap + 3
        x0 = x1 - roi_w
    else:
        x0 = num["x1"] + gap - 3
        x1 = x0 + roi_w

    y0 = cy - roi_h / 2
    y1 = y0 + roi_h

    x0, y0, _, _ = clamp_box(x0, y0, 1, 1, img_w, img_h)
    x1 = max(x0 + 1, min(int(round(x1)), img_w))
    y1 = max(y0 + 1, min(int(round(y1)), img_h))
    return x0, y0, x1, y1


def best_template_match_in_roi(page_bin, template_bin, roi, scales=(0.80, 0.90, 1.00, 1.10, 1.20),
                               target_cy=None, vertical_tol=None):
    x0, y0, x1, y1 = roi
    x0 = max(0, int(x0))
    y0 = max(0, int(y0))
    x1 = min(page_bin.shape[1], int(x1))
    y1 = min(page_bin.shape[0], int(y1))
    if x1 <= x0 or y1 <= y0:
        return None

    search = page_bin[y0:y1, x0:x1]
    best_score = -1.0
    best_box = None

    for scale in scales:
        tw = max(4, int(template_bin.shape[1] * scale))
        th = max(4, int(template_bin.shape[0] * scale))
        resized = cv2.resize(template_bin, (tw, th), interpolation=cv2.INTER_NEAREST)
        if resized.shape[0] >= search.shape[0] or resized.shape[1] >= search.shape[1]:
            continue
        result = cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED)

        if target_cy is not None:
            tol = max(10, int(th * 0.35)) if vertical_tol is None else int(vertical_tol)
            row_centers = y0 + np.arange(result.shape[0], dtype=np.float32) + (th * 0.5)
            valid_rows = np.where(np.abs(row_centers - target_cy) <= tol)[0]

            if valid_rows.size > 0:
                constrained = result[valid_rows, :]
                _, max_val, _, max_loc = cv2.minMaxLoc(constrained)
                max_loc = (max_loc[0], int(valid_rows[max_loc[1]]))
            else:
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
        else:
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > best_score:
            best_score = float(max_val)
            best_box = (x0 + max_loc[0], y0 + max_loc[1], tw, th)

    if best_box is None:
        return None
    return {"score": best_score, "box": best_box}


def non_max_suppression(boxes, scores, iou_thresh=0.15):
    if not boxes:
        return []

    boxes = np.array(boxes, dtype=np.float32)
    scores = np.array(scores, dtype=np.float32)

    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 0] + boxes[:, 2]
    y2 = boxes[:, 1] + boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)

    order = scores.argsort()[::-1]
    keep = []

    while order.size > 0:
        i = order[0]
        keep.append(i)

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0, xx2 - xx1)
        h = np.maximum(0, yy2 - yy1)
        inter = w * h
        union = areas[i] + areas[order[1:]] - inter
        iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)

        inds = np.where(iou <= iou_thresh)[0]
        order = order[inds + 1]

    return keep


def collapse_boxes_by_row(boxes, scores, y_tol):
    if not boxes:
        return []

    items = sorted(zip(boxes, scores), key=lambda item: (item[0][1], -item[1]))
    kept = []

    for box, score in items:
        y = box[1]
        matched = False

        for idx, (kept_box, kept_score) in enumerate(kept):
            if abs(y - kept_box[1]) <= y_tol:
                matched = True
                if score > kept_score:
                    kept[idx] = (box, score)
                break

        if not matched:
            kept.append((box, score))

    return [box for box, _ in kept]


def detect_template_multiscale(gray, template_bin, threshold, roi, scales=(0.85, 0.95, 1.00, 1.10, 1.20)):
    page_bin = to_binary_inv(gray)
    x0, y0, x1, y1 = roi
    x0 = max(0, int(x0))
    y0 = max(0, int(y0))
    x1 = min(page_bin.shape[1], int(x1))
    y1 = min(page_bin.shape[0], int(y1))
    if x1 <= x0 or y1 <= y0:
        return []

    search = page_bin[y0:y1, x0:x1]
    boxes = []
    scores = []

    for scale in scales:
        tw = max(4, int(template_bin.shape[1] * scale))
        th = max(4, int(template_bin.shape[0] * scale))
        resized = cv2.resize(template_bin, (tw, th), interpolation=cv2.INTER_NEAREST)

        if resized.shape[0] >= search.shape[0] or resized.shape[1] >= search.shape[1]:
            continue

        result = cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= threshold)

        for x, y in zip(xs, ys):
            boxes.append((int(x + x0), int(y + y0), tw, th))
            scores.append(float(result[y, x]))

    keep = non_max_suppression(boxes, scores, iou_thresh=0.15)
    boxes = [boxes[i] for i in keep]
    scores = [scores[i] for i in keep]

    row_tol = max(10, int(template_bin.shape[0] * 0.9))
    return collapse_boxes_by_row(boxes, scores, y_tol=row_tol)


def label_for_detection_id(kind, counters):
    if kind == "male":
        det_id = f"M{counters['male']}"
        counters["male"] += 1
        return det_id
    if kind == "female":
        det_id = f"F{counters['female']}"
        counters["female"] += 1
        return det_id
    det_id = f"FE{counters['ferrule']}"
    counters["ferrule"] += 1
    return det_id


def extract_global_ferrule_anchor_words(page, zoom, img_w, img_h):
    words = extract_words(page, zoom)
    anchors = []

    for w in words:
        text = w["text"].strip()

        if re.fullmatch(r"\d+:", text) and (img_w * 0.52) < w["cx"] < (img_w * 0.60):
            anchors.append(w)
            continue

        if (
            re.fullmatch(r"[A-Z0-9]{1,3}[+-]", text, re.IGNORECASE)
            and (img_w * 0.52) < w["cx"] < (img_w * 0.60)
            and (img_h * 0.45) < w["cy"] < (img_h * 0.75)
        ):
            anchors.append(w)

    anchors.sort(key=lambda item: (item["cy"], item["cx"]))
    return anchors


def build_global_symbol_detections(page, gray, zoom, male_left_template, female_right_template, ferrule_right_template):
    img_h, img_w = gray.shape[:2]
    ferrule_gap = int(max(50, ferrule_right_template.shape[1] * 1.10))

    male_boxes = detect_template_multiscale(
        gray,
        male_left_template,
        threshold=0.55,
        roi=(0, 0, int(img_w * 0.40), img_h),
    )
    female_boxes = detect_template_multiscale(
        gray,
        female_right_template,
        threshold=0.55,
        roi=(int(img_w * 0.45), 0, img_w, img_h),
    )

    ferrule_boxes = []
    for anchor in extract_global_ferrule_anchor_words(page, zoom, img_w, img_h):
        ferrule_boxes.append(
            make_anchor_box(
                anchor,
                ferrule_right_template,
                img_w,
                img_h,
                direction="left",
                scale=1.0,
                gap=ferrule_gap,
                y_offset=0,
            )
        )

    detections = []
    counters = {"male": 1, "female": 1, "ferrule": 1}

    for kind, boxes, side in (
        ("male", male_boxes, "left"),
        ("female", female_boxes, "right"),
        ("ferrule", ferrule_boxes, "right"),
    ):
        for x, y, w, h in sorted(boxes, key=lambda box: (box[1], box[0])):
            detections.append({
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "kind": kind,
                "pin": "",
                "id": label_for_detection_id(kind, counters),
                "side": side,
                "male_score": -1.0,
                "female_score": -1.0,
                "ferrule_score": -1.0,
            })

    return detections


def best_symbol_match_for_token(num, page_bin, male_template, female_template, ferrule_template,
                                gap=2, y_offset=3):
    templates = {
        "male": male_template,
        "female": female_template,
        "ferrule": ferrule_template,
    }
    target_cy = num["cy"] + y_offset
    vertical_tol = max(12, int(min(t.shape[0] for t in templates.values()) * 0.45))

    best = {
        "kind": None,
        "direction": None,
        "score": -1.0,
        "box": None,
    }

    for kind, template in templates.items():
        for direction in ("left", "right"):
            roi = build_directional_roi(
                num=num,
                direction=direction,
                template_bin=template,
                page_bin=page_bin,
                gap=gap,
                y_offset=y_offset,
            )
            hit = best_template_match_in_roi(
                page_bin,
                template,
                roi,
                target_cy=target_cy,
                vertical_tol=vertical_tol,
            )
            if hit is not None and hit["score"] > best["score"]:
                best = {
                    "kind": kind,
                    "direction": direction,
                    "score": float(hit["score"]),
                    "box": hit["box"],
                }

    return best


def choose_best_cluster(clusters, side, page_bin,
                        male_template, female_template, ferrule_template,
                        gap=2, y_offset=3, raw_hit_thresh=0.12):
    if not clusters:
        return []

    ranked = []
    img_w = page_bin.shape[1]

    for cluster in clusters:
        mean_x = sum(n["cx"] for n in cluster) / len(cluster)
        side_ok = mean_x < (img_w * 0.5) if side == "left" else mean_x > (img_w * 0.5)
        if not side_ok:
            continue

        hits = 0
        score_sum = 0.0
        for num in cluster:
            best = best_symbol_match_for_token(
                num,
                page_bin,
                male_template,
                female_template,
                ferrule_template,
                gap=gap,
                y_offset=y_offset,
            )
            if best["score"] >= raw_hit_thresh:
                hits += 1
                score_sum += best["score"]

        edge_bias = -mean_x if side == "left" else mean_x
        ranked.append(((hits, score_sum, len(cluster), edge_bias), cluster))

    if not ranked:
        return []

    ranked.sort(key=lambda item: item[0], reverse=True)
    return sorted(ranked[0][1], key=lambda n: (n["cy"], n["cx"]))


def choose_cluster_by_pin_overlap(clusters, pin_values):
    if not clusters or not pin_values:
        return []

    ranked = []
    for cluster in clusters:
        overlap = sum(1 for n in cluster if n["text"].upper() in pin_values)
        if overlap == 0:
            continue
        mean_x = sum(n["cx"] for n in cluster) / len(cluster)
        ranked.append(((overlap, len(cluster), mean_x), cluster))

    if not ranked:
        return []

    ranked.sort(key=lambda item: item[0], reverse=True)
    return sorted(ranked[0][1], key=lambda n: (n["cy"], n["cx"]))


# ---------------------------
# Optional dummy-row filter
# ---------------------------

def group_pin_rows(left_numbers, right_numbers, y_tol=14):
    combined = []
    for n in left_numbers:
        combined.append({"side": "left", "num": n})
    for n in right_numbers:
        combined.append({"side": "right", "num": n})

    combined.sort(key=lambda item: item["num"]["cy"])

    rows = []
    for item in combined:
        cy = item["num"]["cy"]
        if rows and abs(cy - rows[-1]["cy"]) <= y_tol:
            rows[-1]["items"].append(item)
            rows[-1]["cy"] = sum(x["num"]["cy"] for x in rows[-1]["items"]) / len(rows[-1]["items"])
        else:
            rows.append({"cy": cy, "items": [item]})

    return rows


def row_has_interior_text(words, row_cy, x0, x1, y_tol=18):
    for w in words:
        txt = w["text"].strip().upper()
        if x0 <= w["cx"] <= x1 and abs(w["cy"] - row_cy) <= y_tol:
            if txt == "AWG":
                return True
            if "DRAIN_WIRE" in txt:
                return True
            if re.fullmatch(r"[A-Z]{2,}|[A-Z]{3,}_[A-Z]+", txt):
                return True
    return False


def row_has_extra_graphics(page_bin, row_cy, x0, x1, band=26, min_pixels=35):
    x0 = max(0, int(x0))
    x1 = min(page_bin.shape[1], int(x1))
    y0 = max(0, int(row_cy - band))
    y1 = min(page_bin.shape[0], int(row_cy + band))

    if x1 <= x0 or y1 <= y0:
        return False

    roi = page_bin[y0:y1, x0:x1]
    if roi.size == 0:
        return False

    kernel_w = max(25, min(120, max(1, roi.shape[1] // 4)))
    horizontal_kernel = np.ones((1, kernel_w), np.uint8)
    horizontal = cv2.morphologyEx(roi, cv2.MORPH_OPEN, horizontal_kernel)

    residual = cv2.subtract(roi, horizontal)
    residual = cv2.morphologyEx(residual, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    return cv2.countNonZero(residual) >= min_pixels


def filter_dummy_pin_rows(left_numbers, right_numbers, words, page_bin, y_tol=14):
    if not left_numbers and not right_numbers:
        return [], []

    if not left_numbers or not right_numbers:
        return left_numbers, right_numbers

    inner_x0 = max(n["x1"] for n in left_numbers) + 12
    inner_x1 = min(n["x0"] for n in right_numbers) - 12

    if inner_x1 <= inner_x0:
        return left_numbers, right_numbers

    kept_left = []
    kept_right = []

    for row in group_pin_rows(left_numbers, right_numbers, y_tol=y_tol):
        row_cy = row["cy"]
        active = (
            row_has_interior_text(words, row_cy, inner_x0, inner_x1, y_tol=18)
            or row_has_extra_graphics(page_bin, row_cy, inner_x0, inner_x1, band=26, min_pixels=35)
        )

        if not active:
            continue

        for item in row["items"]:
            if item["side"] == "left":
                kept_left.append(item["num"])
            else:
                kept_right.append(item["num"])

    kept_left = sorted(kept_left, key=lambda n: (n["cy"], n["cx"]))
    kept_right = sorted(kept_right, key=lambda n: (n["cy"], n["cx"]))
    return kept_left, kept_right


# ---------------------------
# Template matching helpers
# ---------------------------

def make_anchor_box(num, template_bin, img_w, img_h,
                    direction="left", scale=0.85, gap=2, y_offset=3):
    th, tw = template_bin.shape[:2]
    bw = max(6, int(tw * scale))
    bh = max(8, int(th * scale))
    cy = num["cy"]

    if direction == "left":
        x = num["x0"] - gap - bw
    else:
        x = num["x1"] + gap

    y = cy - bh / 2 + y_offset
    return clamp_box(x, y, bw, bh, img_w, img_h)


def row_is_left_ferrule_label(text):
    upper = text.strip().upper()
    if not upper:
        return False
    if "AWG" in upper or "WIRE" in upper or "SOLDER" in upper or "JOINT" in upper:
        return False
    return (
        bool(re.search(r"\b[A-Z0-9]{2,}\.[A-Z0-9]{2,}\b", upper))
        or "GND" in upper
        or "CHASIS" in upper
    )


def build_left_label_ferrule_detections(page, gray, zoom, ferrule_left_template, start_index=1):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)
    left_words = [w for w in words if w["x0"] < img_w * 0.25]

    rows = []
    for w in sorted(left_words, key=lambda item: (item["cy"], item["cx"])):
        text = w["text"].strip()
        if not re.search(r"[A-Za-z]", text):
            continue
        upper = text.upper()
        if upper in {"AWG", "BLK", "WHT", "GRN", "BLU", "BRN", "DRAIN_WIRE", "DRAIN", "WIRE"}:
            continue

        if rows and abs(w["cy"] - rows[-1]["cy"]) <= 20:
            rows[-1]["words"].append(w)
            rows[-1]["cy"] = sum(item["cy"] for item in rows[-1]["words"]) / len(rows[-1]["words"])
        else:
            rows.append({"cy": w["cy"], "words": [w]})

    detections = []
    ferrule_idx = start_index

    for row in rows:
        row_text = " ".join(w["text"] for w in row["words"])
        if not row_is_left_ferrule_label(row_text):
            continue

        row_x1 = max(w["x1"] for w in row["words"])
        roi = (row_x1 + 5, row["cy"] - 60, row_x1 + 260, row["cy"] + 60)
        hit = best_template_match_in_roi(
            page_bin,
            ferrule_left_template,
            roi,
            target_cy=row["cy"],
            vertical_tol=30,
        )
        if hit is None or hit["score"] < 0.38:
            continue

        x, y, w, h = hit["box"]
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "ferrule",
            "pin": row_text,
            "id": f"FE{ferrule_idx}",
            "side": "left",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": float(hit["score"]),
        })
        ferrule_idx += 1

    return detections


def nearby_left_endpoint_words(row, words, max_dx=380, y_tol=45):
    return [
        w for w in words
        if abs(w["cy"] - row["cy"]) <= y_tol
        and w["x1"] < (row["x0"] - 5)
        and w["x1"] > (row["x0"] - max_dx)
    ]


def row_left_endpoint_label_text(row, words):
    nearby = nearby_left_endpoint_words(row, words)
    if not nearby:
        return ""
    nearby = sorted(nearby, key=lambda w: (w["cy"], w["cx"]))
    return " ".join(w["text"].strip() for w in nearby if w["text"].strip())


def nearby_right_endpoint_words(row, words, max_dx=380, y_tol=45):
    return [
        w for w in words
        if abs(w["cy"] - row["cy"]) <= y_tol
        and w["x0"] > (row["x1"] + 5)
        and w["x0"] < (row["x1"] + max_dx)
    ]


def row_right_endpoint_label_text(row, words):
    nearby = nearby_right_endpoint_words(row, words)
    if not nearby:
        return ""
    nearby = sorted(nearby, key=lambda w: (w["cy"], w["cx"]))
    return " ".join(w["text"].strip() for w in nearby if w["text"].strip())


def row_is_left_endpoint_ferrule_label(text):
    upper = text.strip().upper()
    if not upper:
        return False
    if upper in {"N/C", "GND", "A01", "AO1"}:
        return False
    return (
        bool(re.search(r"\d+V(?:DC)?", upper))
        or "COM" in upper
    )


def filter_left_labeled_female_rows(rows, words):
    kept = []
    for row in rows:
        label_text = row_left_endpoint_label_text(row, words)
        if label_text and label_text.strip().upper() != "N/C":
            kept.append(row)
    return kept


def filter_right_labeled_female_rows(rows, words):
    kept = []
    for row in rows:
        label_text = row_right_endpoint_label_text(row, words)
        if label_text and label_text.strip().upper() != "N/C":
            kept.append(row)
    return kept


def build_left_row_ferrule_detections(rows, words, page_bin, ferrule_left_template, img_w, img_h, start_index=1):
    detections = []
    ferrule_idx = start_index

    for row in rows:
        nearby = nearby_left_endpoint_words(row, words)
        if not nearby:
            continue

        row_text = " ".join(w["text"].strip() for w in nearby if w["text"].strip())
        if not row_is_left_endpoint_ferrule_label(row_text):
            continue

        label_cy = sum(w["cy"] for w in nearby) / len(nearby)
        label_x1 = max(w["x1"] for w in nearby)
        roi_x1 = min(img_w, int(max(label_x1 + 40, row["x0"] - 5)))
        roi = (int(label_x1 + 5), int(label_cy - 60), roi_x1, int(label_cy + 60))
        hit = best_template_match_in_roi(
            page_bin,
            ferrule_left_template,
            roi,
            target_cy=label_cy,
            vertical_tol=35,
        )

        if hit is None or hit["score"] < 0.30:
            pseudo = {"x0": label_x1, "x1": label_x1, "cy": label_cy}
            box = make_anchor_box(
                pseudo,
                ferrule_left_template,
                img_w,
                img_h,
                direction="right",
                scale=0.9,
                gap=8,
                y_offset=0,
            )
            ferrule_score = -1.0
        else:
            box = hit["box"]
            ferrule_score = float(hit["score"])

        x, y, w, h = box
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "ferrule",
            "pin": row_text,
            "id": f"FE{ferrule_idx}",
            "side": "left",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": ferrule_score,
        })
        ferrule_idx += 1

    return detections


def build_right_power_label_ferrule_detections(words, page_bin, ferrule_right_template, img_w, img_h, start_index=1):
    labels = []
    for word in words:
        text = word["text"].strip().upper()
        if not re.fullmatch(r"\d+VPS\d+[+-]", text):
            continue
        labels.append({
            "text": text,
            "x0": word["x0"],
            "x1": word["x1"],
            "cx": word["cx"],
            "cy": word["cy"],
        })

    labels = sorted(labels, key=lambda item: (item["cy"], item["cx"]))
    detections = []
    ferrule_idx = start_index

    for label in labels:
        roi = (
            max(0, int(label["x0"] - 180)),
            max(0, int(label["cy"] - 60)),
            max(0, int(label["x0"] - 4)),
            min(img_h, int(label["cy"] + 60)),
        )
        hit = best_template_match_in_roi(
            page_bin,
            ferrule_right_template,
            roi,
            target_cy=label["cy"],
            vertical_tol=35,
        )

        if hit is None or hit["score"] < 0.30:
            box = make_anchor_box(
                label,
                ferrule_right_template,
                img_w,
                img_h,
                direction="left",
                scale=0.9,
                gap=8,
                y_offset=0,
            )
            ferrule_score = -1.0
        else:
            box = hit["box"]
            ferrule_score = float(hit["score"])

        x, y, w, h = box
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "ferrule",
            "pin": label["text"],
            "id": f"FE{ferrule_idx}",
            "side": "right",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": ferrule_score,
        })
        ferrule_idx += 1

    return detections


def build_right_edge_label_ferrule_detections(words, page_bin, ferrule_right_template, img_w, img_h, start_index=1):
    labels = []
    for word in words:
        text = word["text"].strip().upper()
        if len(text) > 12:
            continue
        colon_style = bool(re.fullmatch(r"\d+:[A-Z0-9_+\-]+", text))
        edge_style = bool(EDGE_ENDPOINT_LABEL_RE.fullmatch(text))
        if not (edge_style or colon_style):
            continue
        if word["cx"] < (img_w * 0.50):
            continue
        if not ((img_h * 0.05) <= word["cy"] <= (img_h * 0.80)):
            continue
        labels.append({
            "text": text,
            "x0": word["x0"],
            "x1": word["x1"],
            "cx": word["cx"],
            "cy": word["cy"],
            "colon_style": colon_style,
        })

    labels = sorted(labels, key=lambda item: (item["cy"], item["cx"]))
    detections = []
    ferrule_idx = start_index

    for label in labels:
        if label["colon_style"]:
            roi = (
                max(0, int(label["x0"] - 180)),
                max(0, int(label["cy"] - 60)),
                max(0, int(label["x0"] - 4)),
                min(img_h, int(label["cy"] + 60)),
            )
            hit = best_template_match_in_roi(
                page_bin,
                ferrule_right_template,
                roi,
                target_cy=label["cy"],
                vertical_tol=35,
            )
            if hit is not None and hit["score"] >= 0.28:
                box = hit["box"]
                ferrule_score = float(hit["score"])
            else:
                box = make_anchor_box(
                    label,
                    ferrule_right_template,
                    img_w,
                    img_h,
                    direction="left",
                    scale=0.9,
                    gap=8,
                    y_offset=0,
                )
                ferrule_score = -1.0
        else:
            # These ferrule circles sit in the small gap immediately after the
            # endpoint label and before any trailing parenthetical number.
            box = make_anchor_box(
                label,
                ferrule_right_template,
                img_w,
                img_h,
                direction="right",
                scale=0.72,
                gap=24,
                y_offset=28,
            )
            ferrule_score = -1.0

        x, y, w, h = box
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "ferrule",
            "pin": label["text"],
            "id": f"FE{ferrule_idx}",
            "side": "right",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": ferrule_score,
        })
        ferrule_idx += 1

    return detections


def row_residual_pixel_count(page_bin, row_cy, x0, x1, band=26):
    x0 = max(0, int(x0))
    x1 = min(page_bin.shape[1], int(x1))
    y0 = max(0, int(row_cy - band))
    y1 = min(page_bin.shape[0], int(row_cy + band))

    if x1 <= x0 or y1 <= y0:
        return 0

    roi = page_bin[y0:y1, x0:x1]
    if roi.size == 0:
        return 0

    kernel_w = max(25, min(120, max(1, roi.shape[1] // 4)))
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 1))
    horizontal = cv2.morphologyEx(roi, cv2.MORPH_OPEN, horizontal_kernel)

    residual = cv2.subtract(roi, horizontal)
    residual = cv2.morphologyEx(
        residual,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)),
    )
    return int(cv2.countNonZero(residual))


def row_wire_tokens(words, row, x0, x1, y_tol=22):
    tokens = []
    for word in words:
        text = word["text"].strip().upper()
        if not text:
            continue
        if abs(word["cy"] - row["cy"]) <= y_tol and x0 <= word["cx"] <= x1:
            tokens.append(text)
    return tokens


def row_has_any_wire_tokens(tokens):
    return "AWG" in tokens


def row_has_drain_wire_tokens(tokens):
    return "DRAIN_WIRE" in tokens or ("DRAIN" in tokens and "WIRE" in tokens)


def row_has_signal_wire_tokens(tokens):
    return row_has_any_wire_tokens(tokens) and not row_has_drain_wire_tokens(tokens)


def split_rows_into_bundles(rows, gap_threshold=120):
    if not rows:
        return []

    bundles = []
    current = [rows[0]]

    for row in rows[1:]:
        if (row["cy"] - current[-1]["cy"]) > gap_threshold:
            bundles.append(current)
            current = [row]
        else:
            current.append(row)

    bundles.append(current)
    return bundles


def trim_singleton_edge_bundles(rows, gap_threshold=160):
    if len(rows) < 3:
        return rows

    bundles = split_rows_into_bundles(rows, gap_threshold=gap_threshold)
    if len(bundles) <= 1:
        return rows

    if len(bundles[0]) == 1 and len(bundles) >= 2 and len(bundles[1]) >= 2:
        bundles = bundles[1:]

    if len(bundles) >= 2 and len(bundles[-1]) == 1 and len(bundles[-2]) >= 2:
        bundles = bundles[:-1]

    trimmed = []
    for bundle in bundles:
        trimmed.extend(bundle)
    return trimmed


def has_row_counterpart(num, counterpart_rows, y_tol=14):
    return any(abs(other["cy"] - num["cy"]) <= y_tol for other in counterpart_rows)


def select_active_rows_from_cluster(cluster_rows, counterpart_rows, words, page_bin, inner_x0, inner_x1):
    kept = []

    for bundle in split_rows_into_bundles(cluster_rows, gap_threshold=120):
        cues = []
        counterparts = []

        for row in bundle:
            cue = (
                row_has_interior_text(words, row["cy"], inner_x0, inner_x1, y_tol=18)
                or row_residual_pixel_count(page_bin, row["cy"], inner_x0, inner_x1) >= 1000
            )
            cues.append(cue)
            counterparts.append(has_row_counterpart(row, counterpart_rows))

        for idx, row in enumerate(bundle):
            keep = cues[idx]

            # Keep a single unlabeled row that sits between two clearly active rows.
            if not keep and 0 < idx < (len(bundle) - 1) and cues[idx - 1] and cues[idx + 1]:
                keep = True

            # Keep the leading row of a bundle when the next two rows are clearly active.
            if not keep and idx == 0 and len(bundle) >= 3 and cues[1] and cues[2]:
                keep = True

            # Keep the tail row of a real span if it lines up with the opposite-side bundle.
            if (
                not keep
                and counterparts[idx]
                and idx >= 2
                and cues[idx - 1]
                and cues[idx - 2]
                and (idx == len(bundle) - 1 or not cues[idx + 1])
            ):
                keep = True

            if keep:
                kept.append(row)

    return kept


def select_left_activity_fallback_rows(cluster_rows, words, inner_x0, inner_x1):
    kept = []
    mid_x = inner_x0 + ((inner_x1 - inner_x0) * 0.5)

    for bundle in split_rows_into_bundles(cluster_rows, gap_threshold=120):
        for row in bundle:
            # Only inspect the left half of the harness span for left-side rows so
            # a dummy pin does not inherit wire text from a branch on the right.
            tokens = row_wire_tokens(words, row, inner_x0, mid_x)
            if row_has_any_wire_tokens(tokens):
                kept.append(row)

    return kept


def select_right_activity_fallback_rows(cluster_rows, words, inner_x0, inner_x1):
    kept = []

    for bundle in split_rows_into_bundles(cluster_rows, gap_threshold=120):
        kept_in_bundle = []
        drain_present = False

        for idx, row in enumerate(bundle):
            tokens = row_wire_tokens(words, row, inner_x0, inner_x1)
            if row_has_drain_wire_tokens(tokens):
                drain_present = True
                continue
            if row_has_signal_wire_tokens(tokens):
                kept_in_bundle.append((idx, row))

        kept.extend(row for _, row in kept_in_bundle)

        if drain_present:
            continue

        if not kept_in_bundle:
            continue

        last_idx = kept_in_bundle[-1][0]
        tail = bundle[last_idx + 1:]
        if tail and all(not row_has_any_wire_tokens(row_wire_tokens(words, row, inner_x0, inner_x1)) for row in tail):
            kept.append(tail[0])

    return kept


def dominant_cluster_terminal_kind(rows, page_bin, male_template, female_template, gap=2, y_offset=3):
    male_total = 0.0
    female_total = 0.0
    vertical_tol = max(12, int(min(male_template.shape[0], female_template.shape[0]) * 0.45))

    for row in rows:
        male_hit = best_template_match_for_kind(
            num=row,
            page_bin=page_bin,
            template_bin=male_template,
            gap=gap,
            y_offset=y_offset,
            vertical_tol=vertical_tol,
        )
        female_hit = best_template_match_for_kind(
            num=row,
            page_bin=page_bin,
            template_bin=female_template,
            gap=gap,
            y_offset=y_offset,
            vertical_tol=vertical_tol,
        )
        male_total += 0.0 if male_hit is None else male_hit["score"]
        female_total += 0.0 if female_hit is None else female_hit["score"]

    return "male" if male_total >= female_total else "female"


def build_cluster_side_detections(rows, kind, side, page_bin, img_w, img_h,
                                  template, counters, scale=0.85, gap=2, y_offset=3,
                                  force_anchor=False):
    detections = []
    vertical_tol = max(12, int(template.shape[0] * 0.45))

    for row in rows:
        if force_anchor:
            box = make_anchor_box(
                row,
                template,
                img_w,
                img_h,
                direction=side,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
            )
        else:
            hit = best_template_match_for_kind(
                num=row,
                page_bin=page_bin,
                template_bin=template,
                gap=gap,
                y_offset=y_offset,
                vertical_tol=vertical_tol,
            )
            if hit is None:
                box = make_anchor_box(
                    row,
                    template,
                    img_w,
                    img_h,
                    direction=side,
                    scale=scale,
                    gap=gap,
                    y_offset=y_offset,
                )
            else:
                box = hit["box"]

        x, y, w, h = box
        det_id = next_detection_id(kind, counters)
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": kind,
            "pin": row["text"],
            "id": det_id,
            "side": side,
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": -1.0,
        })

    return detections


def build_edge_label_ferrule_fallback_detections(page, gray, zoom,
                                                 ferrule_left_template, ferrule_right_template):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)

    candidates = []
    for word in words:
        text = word["text"].strip().upper()
        if len(text) > 10:
            continue
        if not EDGE_ENDPOINT_LABEL_RE.fullmatch(text):
            continue
        if not ((img_h * 0.05) <= word["cy"] <= (img_h * 0.80)):
            continue
        ww = dict(word)
        ww["text"] = text
        candidates.append(ww)

    label_clusters = cluster_numbers_by_x(candidates, x_tol=220)
    label_clusters = [sorted(cluster, key=lambda item: (item["cy"], item["cx"])) for cluster in label_clusters if len(cluster) >= 2]
    if len(label_clusters) < 2:
        return None

    label_clusters.sort(key=lambda cluster: sum(item["cx"] for item in cluster) / len(cluster))
    left_labels = label_clusters[0]
    right_labels = label_clusters[-1]

    detections = []
    counters = {"male": 1, "female": 1, "ferrule": 1, "unknown": 1}

    for label in left_labels:
        roi = (label["x1"] + 4, label["cy"] - 60, label["x1"] + 140, label["cy"] + 60)
        hit = best_template_match_in_roi(
            page_bin,
            ferrule_left_template,
            roi,
            target_cy=label["cy"],
            vertical_tol=35,
        )
        if hit is None:
            box = make_anchor_box(
                label,
                ferrule_left_template,
                img_w,
                img_h,
                direction="right",
                scale=0.9,
                gap=8,
                y_offset=0,
            )
        else:
            box = hit["box"]

        x, y, w, h = box
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "ferrule",
            "pin": label["text"],
            "id": next_detection_id("ferrule", counters),
            "side": "left",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": -1.0,
        })

    for label in right_labels:
        roi = (label["x0"] - 140, label["cy"] - 60, label["x0"] - 4, label["cy"] + 60)
        hit = best_template_match_in_roi(
            page_bin,
            ferrule_right_template,
            roi,
            target_cy=label["cy"],
            vertical_tol=35,
        )
        if hit is None:
            box = make_anchor_box(
                label,
                ferrule_right_template,
                img_w,
                img_h,
                direction="left",
                scale=0.9,
                gap=8,
                y_offset=0,
            )
        else:
            box = hit["box"]

        x, y, w, h = box
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "ferrule",
            "pin": label["text"],
            "id": next_detection_id("ferrule", counters),
            "side": "right",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": -1.0,
        })

    return {
        "detections": detections,
        "left_numbers": [],
        "right_numbers": [],
    }


def build_alpha_left_tb_fallback_detections(page, gray, zoom,
                                            female_left_template, ferrule_right_template):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)

    left_rows = []
    for word in words:
        text = word["text"].strip().upper()
        if not re.fullmatch(r"[A-Z]", text):
            continue
        if not ((img_w * 0.18) <= word["cx"] <= (img_w * 0.30)):
            continue
        if not ((img_h * 0.20) <= word["cy"] <= (img_h * 0.55)):
            continue

        left_rows.append(word)

    right_labels = []
    for word in words:
        text = word["text"].strip().upper()
        if ".TB" not in text:
            continue
        if not re.fullmatch(r"[A-Z0-9.]+\.TB\d+-\d+", text):
            continue
        if word["cx"] < (img_w * 0.58):
            continue
        if not ((img_h * 0.20) <= word["cy"] <= (img_h * 0.60)):
            continue
        right_labels.append(word)

    left_rows = sorted(left_rows, key=lambda item: (item["cy"], item["cx"]))
    right_labels = sorted(right_labels, key=lambda item: (item["cy"], item["cx"]))

    if len(left_rows) < 2 or len(right_labels) < 3:
        return None

    detections = []
    counters = {"male": 1, "female": 1, "ferrule": 1, "unknown": 1}

    for row in left_rows:
        roi = (
            max(0, int(row["x0"] - 120)),
            max(0, int(row["cy"] - 60)),
            min(img_w, int(row["x1"] + 40)),
            min(img_h, int(row["cy"] + 60)),
        )
        hit = best_template_match_in_roi(
            page_bin,
            female_left_template,
            roi,
            target_cy=row["cy"],
            vertical_tol=35,
        )
        if hit is not None and hit["score"] >= 0.18:
            box = hit["box"]
        else:
            box = make_anchor_box(
                row,
                female_left_template,
                img_w,
                img_h,
                direction="left",
                scale=0.85,
                gap=2,
                y_offset=0,
            )

        x, y, w, h = box
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "female",
            "pin": row["text"],
            "id": next_detection_id("female", counters),
            "side": "left",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": -1.0,
        })

    for label in right_labels:
        roi = (
            max(0, int(label["x1"] + 2)),
            max(0, int(label["cy"] - 60)),
            min(img_w, int(label["x1"] + 120)),
            min(img_h, int(label["cy"] + 60)),
        )
        hit = best_template_match_in_roi(
            page_bin,
            ferrule_right_template,
            roi,
            target_cy=label["cy"],
            vertical_tol=35,
        )
        if hit is not None and hit["score"] >= 0.28:
            box = hit["box"]
            ferrule_score = float(hit["score"])
        else:
            box = make_anchor_box(
                label,
                ferrule_right_template,
                img_w,
                img_h,
                direction="right",
                scale=0.72,
                gap=24,
                y_offset=28,
            )
            ferrule_score = -1.0

        x, y, w, h = box
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "ferrule",
            "pin": label["text"].strip().upper(),
            "id": next_detection_id("ferrule", counters),
            "side": "right",
            "male_score": -1.0,
            "female_score": -1.0,
            "ferrule_score": ferrule_score,
        })

    return {
        "detections": detections,
        "left_numbers": [],
        "right_numbers": [],
        "female_count": len(left_rows),
        "ferrule_count": len(right_labels),
    }


def cluster_side_from_x(mean_x, img_w):
    return "left" if mean_x < (img_w * 0.5) else "right"


def dominant_kind_for_cluster(cluster_rows, side, page_bin,
                              male_left_template, male_right_template,
                              female_left_template, female_right_template,
                              ferrule_left_template, ferrule_right_template,
                              score_thresh=0.34, ferrule_score_thresh=0.16, score_margin=0.02,
                              gap=2, y_offset=3):
    kind_counts = {"male": 0, "female": 0, "ferrule": 0, "unknown": 0}

    male_template = male_left_template if side == "left" else male_right_template
    female_template = female_left_template if side == "left" else female_right_template
    ferrule_template = ferrule_left_template if side == "left" else ferrule_right_template

    for row in cluster_rows:
        result = classify_side_endpoint(
            num=row,
            side=side,
            page_bin=page_bin,
            male_template=male_template,
            female_template=female_template,
            ferrule_template=ferrule_template,
            score_thresh=score_thresh,
            ferrule_score_thresh=ferrule_score_thresh,
            score_margin=score_margin,
            gap=gap,
            y_offset=y_offset,
        )
        kind_counts[result["kind"]] += 1

    dominant_kind = max(("male", "female", "ferrule"), key=lambda kind: kind_counts[kind])
    return dominant_kind, kind_counts


def cue_span_for_cluster(cluster_rows, side, img_w):
    if side == "left":
        x0 = max(row["x1"] for row in cluster_rows) + 12
        x1 = img_w * 0.72
    else:
        x0 = img_w * 0.18
        x1 = min(row["x0"] for row in cluster_rows) - 12
    return x0, x1


def filter_female_cluster_rows(cluster_rows, side, words, page_bin, img_w):
    cluster_rows = trim_singleton_edge_bundles(cluster_rows, gap_threshold=160)
    x0, x1 = cue_span_for_cluster(cluster_rows, side, img_w)
    if x1 <= x0:
        return cluster_rows

    bundles = split_rows_into_bundles(cluster_rows, gap_threshold=160)
    kept = []

    for bundle_idx, bundle in enumerate(bundles):
        if len(bundle) == 1:
            row = bundle[0]
            has_text = row_has_interior_text(words, row["cy"], x0, x1, y_tol=18)
            has_gfx = row_has_extra_graphics(page_bin, row["cy"], x0, x1, band=26, min_pixels=35)
            if bundle_idx in {0, len(bundles) - 1} and not has_text:
                continue
            if has_text or has_gfx:
                kept.append(row)
            continue

        bundle_has_text = any(
            row_has_interior_text(words, row["cy"], x0, x1, y_tol=18)
            for row in bundle
        )
        if not bundle_has_text:
            continue

        for row in bundle:
            has_text = row_has_interior_text(words, row["cy"], x0, x1, y_tol=18)
            has_gfx = row_has_extra_graphics(page_bin, row["cy"], x0, x1, band=26, min_pixels=35)
            if has_text or has_gfx:
                kept.append(row)

    return kept


def filter_terminal_bundle_rows(bundle_rows, side, words, page_bin, img_w, kind):
    if not bundle_rows:
        return []

    x0, x1 = cue_span_for_cluster(bundle_rows, side, img_w)
    if x1 <= x0:
        return list(bundle_rows)

    signal_cues = []
    text_cues = []
    drain_cues = []

    for row in bundle_rows:
        tokens = row_wire_tokens(words, row, x0, x1)
        signal_cues.append(row_has_signal_wire_tokens(tokens))
        text_cues.append(row_has_interior_text(words, row["cy"], x0, x1, y_tol=18))
        drain_cues.append(row_has_drain_wire_tokens(tokens))

    if kind == "male":
        first_drain_idx = next((idx for idx, has_drain in enumerate(drain_cues) if has_drain), None)
        if first_drain_idx is not None:
            kept = [row for idx, row in enumerate(bundle_rows[:first_drain_idx]) if signal_cues[idx]]
            return kept
    else:
        last_drain_idx = next((idx for idx in range(len(drain_cues) - 1, -1, -1) if drain_cues[idx]), None)

    kept = []
    for idx, row in enumerate(bundle_rows):
        dummy_circle = kind == "female" and row_has_dummy_terminal_circle(row, side, page_bin)

        if kind == "female":
            keep = (text_cues[idx] or signal_cues[idx]) and not dummy_circle
        else:
            keep = signal_cues[idx]

        if not keep and len(bundle_rows) == 1 and text_cues[idx] and kind == "female" and not dummy_circle:
            keep = True

        if (
            not keep
            and idx == 0
            and len(bundle_rows) >= 3
            and not dummy_circle
            and ((text_cues[1] or signal_cues[1]) if kind == "female" else signal_cues[1])
            and ((text_cues[2] or signal_cues[2]) if kind == "female" else signal_cues[2])
        ):
            keep = True

        if (
            not keep
            and 0 < idx < (len(bundle_rows) - 1)
            and not dummy_circle
            and ((text_cues[idx - 1] or signal_cues[idx - 1]) if kind == "female" else signal_cues[idx - 1])
            and ((text_cues[idx + 1] or signal_cues[idx + 1]) if kind == "female" else signal_cues[idx + 1])
        ):
            keep = True

        if (
            not keep
            and kind == "female"
            and not dummy_circle
            and idx == (len(bundle_rows) - 1)
            and len(bundle_rows) >= 3
            and (text_cues[idx - 1] or signal_cues[idx - 1])
            and (text_cues[idx - 2] or signal_cues[idx - 2])
        ):
            keep = True

        if (
            not keep
            and kind == "female"
            and last_drain_idx is not None
            and idx > last_drain_idx
            and not dummy_circle
        ):
            keep = True

        if keep:
            kept.append(row)

    return kept


def row_has_dummy_terminal_circle(row, side, page_bin):
    if side == "right":
        x0 = max(0, int(row["x0"] - 85))
        x1 = min(page_bin.shape[1], int(row["x0"] - 5))
    else:
        x0 = max(0, int(row["x1"] + 5))
        x1 = min(page_bin.shape[1], int(row["x1"] + 85))

    y0 = max(0, int(row["cy"] - 28))
    y1 = min(page_bin.shape[0], int(row["cy"] + 28))
    if x1 <= x0 or y1 <= y0:
        return False

    roi = page_bin[y0:y1, x0:x1]
    if roi.size == 0:
        return False

    comp_count, _, stats, _ = cv2.connectedComponentsWithStats(roi, 8)
    for comp_idx in range(1, comp_count):
        sx, sy, w, h, area = [int(v) for v in stats[comp_idx]]
        if area < 35 or area > 120:
            continue
        if w < 8 or w > 18 or h < 8 or h > 18:
            continue
        return True

    return False


def build_mixed_gender_cluster_fallback_detections(page, gray, zoom,
                                                   male_left_template, male_right_template,
                                                   female_left_template, female_right_template,
                                                   ferrule_left_template, ferrule_right_template,
                                                   scale=0.85, gap=2, y_offset=3,
                                                   score_thresh=0.34, ferrule_score_thresh=0.16,
                                                   score_margin=0.02):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)
    numeric_pins = extract_numeric_pin_numbers_from_words(words)
    clusters = [
        sorted(cluster, key=lambda row: (row["cy"], row["cx"]))
        for cluster in cluster_numbers_by_x(numeric_pins, x_tol=70)
        if len(cluster) >= 3
    ]
    if len(clusters) < 2:
        return None

    candidates = []
    for cluster in clusters:
        mean_x = sum(row["cx"] for row in cluster) / len(cluster)
        side = cluster_side_from_x(mean_x, img_w)
        for bundle in split_rows_into_bundles(cluster, gap_threshold=160):
            dominant_kind, kind_counts = dominant_kind_for_cluster(
                cluster_rows=bundle,
                side=side,
                page_bin=page_bin,
                male_left_template=male_left_template,
                male_right_template=male_right_template,
                female_left_template=female_left_template,
                female_right_template=female_right_template,
                ferrule_left_template=ferrule_left_template,
                ferrule_right_template=ferrule_right_template,
                score_thresh=score_thresh,
                ferrule_score_thresh=ferrule_score_thresh,
                score_margin=score_margin,
                gap=gap,
                y_offset=y_offset,
            )
            filtered_rows = filter_terminal_bundle_rows(
                bundle_rows=bundle,
                side=side,
                words=words,
                page_bin=page_bin,
                img_w=img_w,
                kind=dominant_kind,
            )
            if not filtered_rows:
                continue
            candidates.append({
                "rows": filtered_rows,
                "side": side,
                "dominant_kind": dominant_kind,
                "kind_counts": kind_counts,
            })

    male_candidates = [
        candidate for candidate in candidates
        if candidate["dominant_kind"] == "male"
    ]
    female_candidates = [
        candidate for candidate in candidates
        if candidate["dominant_kind"] == "female"
    ]

    if not male_candidates or not female_candidates:
        return None

    detections = []
    counters = {"male": 1, "female": 1, "ferrule": 1, "unknown": 1}

    for candidate in male_candidates:
        male_template = male_left_template if candidate["side"] == "left" else male_right_template
        detections.extend(
            build_cluster_side_detections(
                rows=candidate["rows"],
                kind="male",
                side=candidate["side"],
                page_bin=page_bin,
                img_w=img_w,
                img_h=img_h,
                template=male_template,
                counters=counters,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
                force_anchor=True,
            )
        )
 
    for candidate in female_candidates:
        female_template = female_left_template if candidate["side"] == "left" else female_right_template
        detections.extend(
            build_cluster_side_detections(
                rows=candidate["rows"],
                kind="female",
                side=candidate["side"],
                page_bin=page_bin,
                img_w=img_w,
                img_h=img_h,
                template=female_template,
                counters=counters,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
                force_anchor=True,
            )
        )

    male_count = sum(1 for det in detections if det["kind"] == "male")
    female_count = sum(1 for det in detections if det["kind"] == "female")
    if male_count < 1 or female_count < 1:
        return None

    return {
        "detections": detections,
        "left_numbers": [],
        "right_numbers": [],
        "male_count": male_count,
        "female_count": female_count,
    }


def build_female_ferrule_fallback_detections(page, gray, zoom,
                                             male_left_template, male_right_template,
                                             female_left_template, female_right_template,
                                             ferrule_left_template, ferrule_right_template,
                                             scale=0.85, gap=2, y_offset=3,
                                             score_thresh=0.34, ferrule_score_thresh=0.16,
                                             score_margin=0.02):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)
    numeric_pins = extract_numeric_pin_numbers_from_words(words)
    clusters = [
        sorted(cluster, key=lambda row: (row["cy"], row["cx"]))
        for cluster in cluster_numbers_by_x(numeric_pins, x_tol=70)
        if len(cluster) >= 3
    ]
    if len(clusters) < 2:
        return None

    female_candidates = []
    for cluster in clusters:
        mean_x = sum(row["cx"] for row in cluster) / len(cluster)
        side = cluster_side_from_x(mean_x, img_w)
        for bundle in split_rows_into_bundles(cluster, gap_threshold=160):
            dominant_kind, kind_counts = dominant_kind_for_cluster(
                cluster_rows=bundle,
                side=side,
                page_bin=page_bin,
                male_left_template=male_left_template,
                male_right_template=male_right_template,
                female_left_template=female_left_template,
                female_right_template=female_right_template,
                ferrule_left_template=ferrule_left_template,
                ferrule_right_template=ferrule_right_template,
                score_thresh=score_thresh,
                ferrule_score_thresh=ferrule_score_thresh,
                score_margin=score_margin,
                gap=gap,
                y_offset=y_offset,
            )
            if dominant_kind != "female":
                continue
            female_candidates.append({
                "rows": bundle,
                "side": side,
                "kind_counts": kind_counts,
            })

    left_candidates = [candidate for candidate in female_candidates if candidate["side"] == "left"]
    right_candidates = [candidate for candidate in female_candidates if candidate["side"] == "right"]
    if not left_candidates or not right_candidates:
        return None

    left_candidate = max(left_candidates, key=lambda item: len(item["rows"]))
    right_candidate = max(right_candidates, key=lambda item: len(item["rows"]))

    left_rows = filter_left_labeled_female_rows(left_candidate["rows"], words)
    right_rows = filter_right_labeled_female_rows(right_candidate["rows"], words)

    if not left_rows or not right_rows:
        return None

    detections = []
    counters = {"male": 1, "female": 1, "ferrule": 1, "unknown": 1}
    detections.extend(
        build_cluster_side_detections(
            rows=left_rows,
            kind="female",
            side="left",
            page_bin=page_bin,
            img_w=img_w,
            img_h=img_h,
            template=female_left_template,
            counters=counters,
            scale=scale,
            gap=gap,
            y_offset=y_offset,
            force_anchor=True,
        )
    )
    detections.extend(
        build_cluster_side_detections(
            rows=right_rows,
            kind="female",
            side="right",
            page_bin=page_bin,
            img_w=img_w,
            img_h=img_h,
            template=female_right_template,
            counters=counters,
            scale=scale,
            gap=gap,
            y_offset=y_offset,
            force_anchor=True,
        )
    )

    ferrule_detections = build_right_power_label_ferrule_detections(
        words=words,
        page_bin=page_bin,
        ferrule_right_template=ferrule_right_template,
        img_w=img_w,
        img_h=img_h,
        start_index=counters["ferrule"],
    )
    detections.extend(ferrule_detections)

    female_count = sum(1 for det in detections if det["kind"] == "female")
    ferrule_count = sum(1 for det in detections if det["kind"] == "ferrule")
    if female_count < 4 or ferrule_count < 2:
        return None

    return {
        "detections": detections,
        "left_numbers": [],
        "right_numbers": [],
        "female_count": female_count,
        "ferrule_count": ferrule_count,
    }


def build_activity_filtered_fallback_detections(page, gray, zoom,
                                                male_left_template, female_left_template,
                                                male_right_template, female_right_template,
                                                ferrule_left_template,
                                                scale=0.85, gap=2, y_offset=3):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)
    numeric_pins = extract_numeric_pin_numbers_from_words(words)
    clusters = cluster_numbers_by_x(numeric_pins, x_tol=70)

    substantial = []
    for cluster in clusters:
        mean_x = sum(n["cx"] for n in cluster) / len(cluster)
        substantial.append((mean_x, sorted(cluster, key=lambda n: (n["cy"], n["cx"]))))

    substantial = [item for item in substantial if len(item[1]) >= 4]
    if len(substantial) < 2:
        return None

    substantial.sort(key=lambda item: item[0])
    left_rows = substantial[0][1]
    right_rows = substantial[-1][1]

    inner_x0 = max(n["x1"] for n in left_rows) + 12
    inner_x1 = min(n["x0"] for n in right_rows) - 12
    if inner_x1 <= inner_x0:
        return None

    active_left_rows = select_left_activity_fallback_rows(
        cluster_rows=left_rows,
        words=words,
        inner_x0=inner_x0,
        inner_x1=inner_x1,
    )
    active_right_rows = select_right_activity_fallback_rows(
        cluster_rows=right_rows,
        words=words,
        inner_x0=inner_x0,
        inner_x1=inner_x1,
    )

    if (len(active_left_rows) + len(active_right_rows)) < 6:
        return None

    left_kind = dominant_cluster_terminal_kind(
        rows=active_left_rows,
        page_bin=page_bin,
        male_template=male_left_template,
        female_template=female_left_template,
        gap=gap,
        y_offset=y_offset,
    )
    right_kind = dominant_cluster_terminal_kind(
        rows=active_right_rows,
        page_bin=page_bin,
        male_template=male_right_template,
        female_template=female_right_template,
        gap=gap,
        y_offset=y_offset,
    )
    left_template = male_left_template if left_kind == "male" else female_left_template
    right_template = male_right_template if right_kind == "male" else female_right_template

    detections = []
    counters = {
        "male": 1,
        "female": 1,
        "ferrule": 1,
        "unknown": 1,
    }
    detections.extend(
        build_cluster_side_detections(
            rows=active_left_rows,
            kind=left_kind,
            side="left",
            page_bin=page_bin,
            img_w=img_w,
            img_h=img_h,
            template=left_template,
            counters=counters,
            scale=scale,
            gap=gap,
            y_offset=y_offset,
        )
    )
    detections.extend(
        build_cluster_side_detections(
            rows=active_right_rows,
            kind=right_kind,
            side="right",
            page_bin=page_bin,
            img_w=img_w,
            img_h=img_h,
            template=right_template,
            counters=counters,
            scale=scale,
            gap=gap,
            y_offset=y_offset,
        )
    )

    ferrule_detections = build_left_label_ferrule_detections(
        page=page,
        gray=gray,
        zoom=zoom,
        ferrule_left_template=ferrule_left_template,
        start_index=counters["ferrule"],
    )
    detections.extend(ferrule_detections)

    return {
        "detections": detections,
        "left_numbers": active_left_rows,
        "right_numbers": active_right_rows,
        "kind": f"{left_kind}/{right_kind}",
    }


def build_male_ferrule_layout_fallback_detections(page, gray, zoom,
                                                  male_left_template, female_left_template,
                                                  ferrule_right_template,
                                                  scale=0.85, gap=2, y_offset=3):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]
    words = extract_words(page, zoom)
    numeric_pins = extract_numeric_pin_numbers_from_words(words)
    clusters = [
        sorted(cluster, key=lambda row: (row["cy"], row["cx"]))
        for cluster in cluster_numbers_by_x(numeric_pins, x_tol=70)
        if len(cluster) >= 3
    ]

    left_clusters = []
    for cluster in clusters:
        mean_x = sum(row["cx"] for row in cluster) / len(cluster)
        if mean_x < (img_w * 0.40):
            left_clusters.append(cluster)

    if not left_clusters:
        return None

    left_rows = trim_singleton_edge_bundles(max(left_clusters, key=len), gap_threshold=160)
    active_left = []
    for bundle in split_rows_into_bundles(left_rows, gap_threshold=160):
        active_left.extend(
            filter_terminal_bundle_rows(
                bundle,
                "left",
                words,
                page_bin,
                img_w,
                "male",
            )
        )
    if not active_left:
        active_left = left_rows

    left_shape_kind = dominant_cluster_terminal_kind(
        active_left,
        page_bin,
        male_left_template,
        female_left_template,
        gap=gap,
        y_offset=y_offset,
    )
    left_template = male_left_template if left_shape_kind == "male" else female_left_template

    counters = {"male": 1, "female": 1, "ferrule": 1, "unknown": 1}
    detections = build_cluster_side_detections(
        rows=active_left,
        kind="male",
        side="left",
        page_bin=page_bin,
        img_w=img_w,
        img_h=img_h,
        template=left_template,
        counters=counters,
        scale=scale,
        gap=gap,
        y_offset=y_offset,
        force_anchor=True,
    )

    ferrule_detections = build_right_edge_label_ferrule_detections(
        words=words,
        page_bin=page_bin,
        ferrule_right_template=ferrule_right_template,
        img_w=img_w,
        img_h=img_h,
        start_index=counters["ferrule"],
    )
    detections.extend(ferrule_detections)

    male_count = sum(1 for det in detections if det["kind"] == "male")
    ferrule_count = sum(1 for det in detections if det["kind"] == "ferrule")
    if male_count < 3 or ferrule_count < 2:
        return None

    return {
        "detections": detections,
        "left_numbers": active_left,
        "right_numbers": [],
        "male_count": male_count,
        "ferrule_count": ferrule_count,
    }


def build_local_roi(num, template_bin, page_bin,
                    direction="left", gap=2, y_offset=3):
    return build_directional_roi(
        num=num,
        direction=direction,
        template_bin=template_bin,
        page_bin=page_bin,
        gap=gap,
        y_offset=y_offset,
    )


def best_template_match_for_kind(num, page_bin, template_bin, gap=2, y_offset=3, vertical_tol=None):
    target_cy = num["cy"] + y_offset
    best = None
    for direction in ("left", "right"):
        roi = build_local_roi(
            num=num,
            template_bin=template_bin,
            page_bin=page_bin,
            direction=direction,
            gap=gap,
            y_offset=y_offset,
        )
        hit = best_template_match_in_roi(
            page_bin,
            template_bin,
            roi,
            target_cy=target_cy,
            vertical_tol=vertical_tol,
        )
        if hit is not None and (best is None or hit["score"] > best["score"]):
            best = {
                "score": float(hit["score"]),
                "box": hit["box"],
                "direction": direction,
            }
    return best


def classify_side_endpoint(num, side, page_bin,
                           male_template, female_template, ferrule_template,
                           score_thresh=0.34, ferrule_score_thresh=0.16,
                           score_margin=0.02, gap=2, y_offset=3):
    specs = {
        "male": male_template,
        "female": female_template,
        "ferrule": ferrule_template,
    }

    vertical_tol = max(
        12,
        int(min(t.shape[0] for t in [male_template, female_template, ferrule_template]) * 0.45)
    )

    matches = {}
    scores = {}
    directions = {}

    for kind, template in specs.items():
        hit = best_template_match_for_kind(
            num=num,
            page_bin=page_bin,
            template_bin=template,
            gap=gap,
            y_offset=y_offset,
            vertical_tol=vertical_tol,
        )
        matches[kind] = hit
        scores[kind] = -1.0 if hit is None else hit["score"]
        directions[kind] = "left" if hit is None else hit["direction"]

    valid = [(kind, scores[kind]) for kind in specs if matches[kind] is not None]
    if not valid:
        return {
            "kind": "unknown",
            "box": None,
            "direction": "left",
            "male_score": scores["male"],
            "female_score": scores["female"],
            "ferrule_score": scores["ferrule"],
        }

    valid.sort(key=lambda x: x[1], reverse=True)
    best_kind, best_score = valid[0]
    best_box = matches[best_kind]["box"]
    best_direction = directions[best_kind]
    second_score = valid[1][1] if len(valid) >= 2 else -1.0

    effective_thresh = ferrule_score_thresh if best_kind == "ferrule" else score_thresh

    if best_score < effective_thresh or (best_score - second_score) < score_margin:
        return {
            "kind": "unknown",
            "box": best_box,
            "direction": best_direction,
            "male_score": scores["male"],
            "female_score": scores["female"],
            "ferrule_score": scores["ferrule"],
        }

    return {
        "kind": best_kind,
        "box": best_box,
        "direction": best_direction,
        "male_score": scores["male"],
        "female_score": scores["female"],
        "ferrule_score": scores["ferrule"],
    }


def template_for_kind(kind, male_template, female_template, ferrule_template):
    if kind == "male":
        return male_template
    if kind == "female":
        return female_template
    if kind == "ferrule":
        return ferrule_template
    return ferrule_template


def next_detection_id(kind, counters):
    if kind == "male":
        det_id = f"M{counters['male']}"
        counters["male"] += 1
        return det_id
    if kind == "female":
        det_id = f"F{counters['female']}"
        counters["female"] += 1
        return det_id
    if kind == "ferrule":
        det_id = f"FE{counters['ferrule']}"
        counters["ferrule"] += 1
        return det_id

    det_id = f"U{counters['unknown']}"
    counters["unknown"] += 1
    return det_id


# ---------------------------
# Main detection pass
# ---------------------------

def detect_from_pin_numbers_auto(page, gray, zoom,
                                 male_left_template, male_right_template,
                                 female_left_template, female_right_template,
                                 ferrule_left_template, ferrule_right_template,
                                 score_thresh=0.34, ferrule_score_thresh=0.16,
                                 score_margin=0.02, scale=0.85, gap=2, y_offset=3,
                                 use_dummy_filter=False, dummy_male_pins=None,
                                 dummy_female_pins=None):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]

    words = extract_words(page, zoom)
    all_pin_numbers = extract_candidate_pin_numbers_from_words(words)
    clusters = cluster_numbers_by_x(all_pin_numbers, x_tol=70)

    if dummy_male_pins:
        target_cluster = choose_cluster_by_pin_overlap(clusters, dummy_male_pins)
        if target_cluster:
            right_numbers = sorted(target_cluster, key=lambda n: (n["cy"], n["cx"]))
            detections = []
            counters = {
                "male": 1,
                "female": 1,
                "ferrule": 1,
                "unknown": 1,
            }
            vertical_tol = max(12, int(male_right_template.shape[0] * 0.45))

            for num in right_numbers:
                if pin_is_dummy(
                    num["text"],
                    "male",
                    dummy_male_pins=dummy_male_pins,
                    dummy_female_pins=dummy_female_pins,
                ):
                    continue

                hit = best_template_match_for_kind(
                    num=num,
                    page_bin=page_bin,
                    template_bin=male_right_template,
                    gap=gap,
                    y_offset=y_offset,
                    vertical_tol=vertical_tol,
                )
                if hit is None:
                    box = make_anchor_box(
                        num,
                        male_right_template,
                        img_w,
                        img_h,
                        direction="right",
                        scale=scale,
                        gap=gap,
                        y_offset=y_offset,
                    )
                else:
                    box = hit["box"]

                x, y, w, h = box
                det_id = next_detection_id("male", counters)
                detections.append({
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "kind": "male",
                    "pin": num["text"],
                    "id": det_id,
                    "side": "right",
                    "male_score": -1.0,
                    "female_score": -1.0,
                    "ferrule_score": -1.0,
                })

            ferrule_detections = build_left_label_ferrule_detections(
                page=page,
                gray=gray,
                zoom=zoom,
                ferrule_left_template=ferrule_left_template,
                start_index=counters["ferrule"],
            )
            detections.extend(ferrule_detections)
            return detections, [], right_numbers

    left_numbers = choose_best_cluster(
        clusters, "left", page_bin,
        male_left_template, female_left_template, ferrule_left_template,
        gap=gap, y_offset=y_offset,
    )
    right_numbers = choose_best_cluster(
        clusters, "right", page_bin,
        male_right_template, female_right_template, ferrule_right_template,
        gap=gap, y_offset=y_offset,
    )

    left_numbers = trim_singleton_edge_bundles(left_numbers, gap_threshold=160)
    right_numbers = trim_singleton_edge_bundles(right_numbers, gap_threshold=160)

    if use_dummy_filter:
        left_numbers, right_numbers = filter_dummy_pin_rows(
            left_numbers=left_numbers,
            right_numbers=right_numbers,
            words=words,
            page_bin=page_bin,
            y_tol=14,
        )

    left_numbers = sorted(left_numbers, key=lambda n: (n["cy"], n["cx"]))
    right_numbers = sorted(right_numbers, key=lambda n: (n["cy"], n["cx"]))

    detections = []
    counters = {
        "male": 1,
        "female": 1,
        "ferrule": 1,
        "unknown": 1,
    }

    for num in left_numbers:
        result = classify_side_endpoint(
            num=num,
            side="left",
            page_bin=page_bin,
            male_template=male_left_template,
            female_template=female_left_template,
            ferrule_template=ferrule_left_template,
            score_thresh=score_thresh,
            ferrule_score_thresh=ferrule_score_thresh,
            score_margin=score_margin,
            gap=gap,
            y_offset=y_offset,
        )

        kind = result["kind"]
        box = result["box"]
        direction = result["direction"]

        if pin_is_dummy(
            num["text"],
            kind,
            dummy_male_pins=dummy_male_pins,
            dummy_female_pins=dummy_female_pins,
        ):
            continue

        if box is None:
            fallback = template_for_kind(
                kind, male_left_template, female_left_template, ferrule_left_template
            )
            box = make_anchor_box(
                num, fallback, img_w, img_h,
                direction=direction, scale=scale, gap=gap, y_offset=y_offset
            )

        x, y, w, h = box
        det_id = next_detection_id(kind, counters)

        detections.append({
            "x": x, "y": y, "w": w, "h": h,
            "kind": kind, "pin": num["text"], "id": det_id,
            "side": "left",
            "male_score": result["male_score"],
            "female_score": result["female_score"],
            "ferrule_score": result["ferrule_score"],
        })

    for num in right_numbers:
        result = classify_side_endpoint(
            num=num,
            side="right",
            page_bin=page_bin,
            male_template=male_right_template,
            female_template=female_right_template,
            ferrule_template=ferrule_right_template,
            score_thresh=score_thresh,
            ferrule_score_thresh=ferrule_score_thresh,
            score_margin=score_margin,
            gap=gap,
            y_offset=y_offset,
        )

        kind = result["kind"]
        box = result["box"]
        direction = result["direction"]

        if pin_is_dummy(
            num["text"],
            kind,
            dummy_male_pins=dummy_male_pins,
            dummy_female_pins=dummy_female_pins,
        ):
            continue

        if box is None:
            fallback = template_for_kind(
                kind, male_right_template, female_right_template, ferrule_right_template
            )
            box = make_anchor_box(
                num, fallback, img_w, img_h,
                direction=direction, scale=scale, gap=gap, y_offset=y_offset
            )

        x, y, w, h = box
        det_id = next_detection_id(kind, counters)

        detections.append({
            "x": x, "y": y, "w": w, "h": h,
            "kind": kind, "pin": num["text"], "id": det_id,
            "side": "right",
            "male_score": result["male_score"],
            "female_score": result["female_score"],
            "ferrule_score": result["ferrule_score"],
        })

    return detections, left_numbers, right_numbers


# ---------------------------
# Drawing / annotation helpers
# ---------------------------

def image_rect_to_pdf_rect(x, y, w, h, page_rect, img_w, img_h):
    sx = page_rect.width / img_w
    sy = page_rect.height / img_h
    return fitz.Rect(x * sx, y * sy, (x + w) * sx, (y + h) * sy)


def annotate_page(page, detections, img_shape):
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


def draw_debug_image(bgr, detections, out_path):
    dbg = bgr.copy()

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
        cv2.putText(
            dbg,
            d["id"] if not d["pin"] else f'{d["id"]}:{d["pin"]}',
            (x, max(12, y - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            color,
            1,
            cv2.LINE_AA,
        )

    cv2.imwrite(str(out_path), dbg)


# ---------------------------
# PDF processing
# ---------------------------

def process_pdf(input_pdf, output_pdf,
                male_left_template_path, male_right_template_path,
                female_left_template_path, female_right_template_path,
                ferrule_left_template_path, ferrule_right_template_path,
                zoom=4.0, debug_dir="debug_out",
                score_thresh=0.34, ferrule_score_thresh=0.16,
                score_margin=0.02, scale=0.85, gap=2, y_offset=3,
                use_dummy_filter=False, dummy_male_pins=None,
                dummy_female_pins=None):
    input_pdf = Path(input_pdf)
    output_pdf = Path(output_pdf)
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(input_pdf))

    male_left_template = load_template(male_left_template_path)
    male_right_template = load_template(male_right_template_path)
    female_left_template = load_template(female_left_template_path)
    female_right_template = load_template(female_right_template_path)
    ferrule_left_template = load_template(ferrule_left_template_path)
    ferrule_right_template = load_template(ferrule_right_template_path)

    total_male = 0
    total_female = 0
    total_ferrule = 0
    total_unknown = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        bgr, gray, zoom_used = render_page(page, zoom=zoom)

        detections, left_numbers, right_numbers = detect_from_pin_numbers_auto(
            page=page,
            gray=gray,
            zoom=zoom_used,
            male_left_template=male_left_template,
            male_right_template=male_right_template,
            female_left_template=female_left_template,
            female_right_template=female_right_template,
            ferrule_left_template=ferrule_left_template,
            ferrule_right_template=ferrule_right_template,
            score_thresh=score_thresh,
            ferrule_score_thresh=ferrule_score_thresh,
            score_margin=score_margin,
            scale=scale,
            gap=gap,
            y_offset=y_offset,
            use_dummy_filter=use_dummy_filter,
            dummy_male_pins=dummy_male_pins,
            dummy_female_pins=dummy_female_pins,
        )

        token_male = sum(1 for d in detections if d["kind"] == "male")
        token_female = sum(1 for d in detections if d["kind"] == "female")
        token_ferrule = sum(1 for d in detections if d["kind"] == "ferrule")
        token_unknown = sum(1 for d in detections if d["kind"] == "unknown")

        if (
            not use_dummy_filter
            and token_unknown == 0
            and token_ferrule == 0
            and left_numbers
            and right_numbers
            and abs(len(left_numbers) - len(right_numbers)) <= 1
        ):
            filtered_detections, filtered_left_numbers, filtered_right_numbers = detect_from_pin_numbers_auto(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                male_right_template=male_right_template,
                female_left_template=female_left_template,
                female_right_template=female_right_template,
                ferrule_left_template=ferrule_left_template,
                ferrule_right_template=ferrule_right_template,
                score_thresh=score_thresh,
                ferrule_score_thresh=ferrule_score_thresh,
                score_margin=score_margin,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
                use_dummy_filter=True,
                dummy_male_pins=dummy_male_pins,
                dummy_female_pins=dummy_female_pins,
            )

            filtered_male = sum(1 for d in filtered_detections if d["kind"] == "male")
            filtered_female = sum(1 for d in filtered_detections if d["kind"] == "female")
            filtered_ferrule = sum(1 for d in filtered_detections if d["kind"] == "ferrule")
            filtered_unknown = sum(1 for d in filtered_detections if d["kind"] == "unknown")

            current_known_total = token_male + token_female + token_ferrule
            filtered_known_total = filtered_male + filtered_female + filtered_ferrule

            if (
                filtered_unknown == 0
                and filtered_ferrule == 0
                and filtered_known_total < current_known_total
                and len(filtered_left_numbers) == len(filtered_right_numbers)
                and len(filtered_left_numbers) >= 2
            ):
                detections = filtered_detections
                left_numbers = filtered_left_numbers
                right_numbers = filtered_right_numbers
                token_male = filtered_male
                token_female = filtered_female
                token_ferrule = filtered_ferrule
                token_unknown = filtered_unknown
                print(f"Page {page_idx + 1}: switched to symmetric dummy-row filter")

        mixed_gender_fallback = None
        if (
            token_unknown > 0
            or token_ferrule > 0
            or (
                token_unknown == 0
                and token_ferrule == 0
                and (len(left_numbers) >= 10 or len(right_numbers) >= 10)
            )
        ):
            mixed_gender_fallback = build_mixed_gender_cluster_fallback_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                male_right_template=male_right_template,
                female_left_template=female_left_template,
                female_right_template=female_right_template,
                ferrule_left_template=ferrule_left_template,
                ferrule_right_template=ferrule_right_template,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
                score_thresh=score_thresh,
                ferrule_score_thresh=ferrule_score_thresh,
                score_margin=score_margin,
            )

        if mixed_gender_fallback is not None:
            fallback_detections = mixed_gender_fallback["detections"]
            fallback_known_total = sum(
                1 for d in fallback_detections if d["kind"] in {"male", "female", "ferrule"}
            )
            current_known_total = token_male + token_female + token_ferrule
            fallback_male = mixed_gender_fallback["male_count"]
            fallback_female = mixed_gender_fallback["female_count"]

            should_use_mixed_fallback = False
            if token_unknown > 0 or token_ferrule > 0:
                should_use_mixed_fallback = fallback_known_total >= current_known_total
                if (
                    not should_use_mixed_fallback
                    and fallback_male > 0
                    and fallback_female > 0
                    and fallback_known_total <= (current_known_total - 8)
                    and fallback_male <= token_male
                    and fallback_female <= token_female
                ):
                    should_use_mixed_fallback = True
            elif (
                fallback_male > 0
                and fallback_female > 0
                and fallback_known_total <= (current_known_total - 4)
            ):
                should_use_mixed_fallback = True

            if should_use_mixed_fallback:
                detections = fallback_detections
                left_numbers = mixed_gender_fallback["left_numbers"]
                right_numbers = mixed_gender_fallback["right_numbers"]
                token_male = fallback_male
                token_female = fallback_female
                token_ferrule = 0
                token_unknown = 0
                print(f"Page {page_idx + 1}: switched to mixed gender fallback")

        global_mixed_fallback = None
        if token_unknown > 0 or token_ferrule > 0:
            global_mixed_fallback = build_global_symbol_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                female_right_template=female_right_template,
                ferrule_right_template=ferrule_right_template,
            )

        if global_mixed_fallback is not None:
            fallback_male = sum(1 for d in global_mixed_fallback if d["kind"] == "male")
            fallback_female = sum(1 for d in global_mixed_fallback if d["kind"] == "female")
            fallback_ferrule = sum(1 for d in global_mixed_fallback if d["kind"] == "ferrule")
            fallback_known_total = fallback_male + fallback_female + fallback_ferrule
            current_known_total = token_male + token_female + token_ferrule

            if (
                fallback_male > 0
                and fallback_female > 0
                and fallback_ferrule == 0
                and fallback_known_total >= current_known_total
            ):
                detections = global_mixed_fallback
                left_numbers = []
                right_numbers = []
                token_male = fallback_male
                token_female = fallback_female
                token_ferrule = 0
                token_unknown = 0
                print(f"Page {page_idx + 1}: switched to global mixed symbol fallback")

        female_ferrule_fallback = None
        if token_male == 0 and token_female >= 8 and token_ferrule == 0:
            female_ferrule_fallback = build_female_ferrule_fallback_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                male_right_template=male_right_template,
                female_left_template=female_left_template,
                female_right_template=female_right_template,
                ferrule_left_template=ferrule_left_template,
                ferrule_right_template=ferrule_right_template,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
                score_thresh=score_thresh,
                ferrule_score_thresh=ferrule_score_thresh,
                score_margin=score_margin,
            )

        if female_ferrule_fallback is not None:
            fallback_detections = female_ferrule_fallback["detections"]
            fallback_female = female_ferrule_fallback["female_count"]
            fallback_ferrule = female_ferrule_fallback["ferrule_count"]
            fallback_known_total = fallback_female + fallback_ferrule
            current_known_total = token_male + token_female + token_ferrule

            if (
                fallback_ferrule > 0
                and fallback_female > 0
                and fallback_known_total <= current_known_total
            ):
                detections = fallback_detections
                left_numbers = female_ferrule_fallback["left_numbers"]
                right_numbers = female_ferrule_fallback["right_numbers"]
                token_male = 0
                token_female = fallback_female
                token_ferrule = fallback_ferrule
                token_unknown = 0
                print(f"Page {page_idx + 1}: switched to female/ferrule fallback")

        male_ferrule_fallback = None
        if token_male > 0 and token_female == 0 and token_ferrule == 0:
            male_ferrule_fallback = build_male_ferrule_layout_fallback_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                female_left_template=female_left_template,
                ferrule_right_template=ferrule_right_template,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
            )

        if male_ferrule_fallback is not None:
            fallback_detections = male_ferrule_fallback["detections"]
            fallback_male = male_ferrule_fallback["male_count"]
            fallback_ferrule = male_ferrule_fallback["ferrule_count"]
            fallback_known_total = fallback_male + fallback_ferrule
            current_known_total = token_male + token_female + token_ferrule

            if (
                fallback_male > 0
                and fallback_ferrule > 0
                and fallback_male <= token_male
                and fallback_known_total <= current_known_total
            ):
                detections = fallback_detections
                left_numbers = male_ferrule_fallback["left_numbers"]
                right_numbers = male_ferrule_fallback["right_numbers"]
                token_male = fallback_male
                token_female = 0
                token_ferrule = fallback_ferrule
                token_unknown = 0
                print(f"Page {page_idx + 1}: switched to male/ferrule fallback")

        activity_fallback = None
        if token_unknown >= 8:
            activity_fallback = build_activity_filtered_fallback_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                female_left_template=female_left_template,
                male_right_template=male_right_template,
                female_right_template=female_right_template,
                ferrule_left_template=ferrule_left_template,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
            )

        if activity_fallback is not None:
            fallback_detections = activity_fallback["detections"]
            fallback_known_total = sum(
                1 for d in fallback_detections if d["kind"] in {"male", "female", "ferrule"}
            )
            current_known_total = token_male + token_female + token_ferrule

            if fallback_known_total > current_known_total:
                detections = fallback_detections
                left_numbers = activity_fallback["left_numbers"]
                right_numbers = activity_fallback["right_numbers"]
                token_unknown = 0
                token_ferrule = sum(1 for d in detections if d["kind"] == "ferrule")
                print(
                    f"Page {page_idx + 1}: switched to activity fallback "
                    f"({activity_fallback['kind']} cluster)"
                )

        if token_ferrule == 0 and token_unknown > 0:
            detections = build_global_symbol_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                female_right_template=female_right_template,
                ferrule_right_template=ferrule_right_template,
            )
            left_numbers = []
            right_numbers = []
            print(f"Page {page_idx + 1}: switched to global symbol fallback")

        token_male = sum(1 for d in detections if d["kind"] == "male")
        token_female = sum(1 for d in detections if d["kind"] == "female")
        token_ferrule = sum(1 for d in detections if d["kind"] == "ferrule")
        token_unknown = sum(1 for d in detections if d["kind"] == "unknown")

        if (token_male + token_female + token_ferrule) == 0:
            male_ferrule_fallback = build_male_ferrule_layout_fallback_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                male_left_template=male_left_template,
                female_left_template=female_left_template,
                ferrule_right_template=ferrule_right_template,
                scale=scale,
                gap=gap,
                y_offset=y_offset,
            )
            if male_ferrule_fallback is not None:
                detections = male_ferrule_fallback["detections"]
                left_numbers = male_ferrule_fallback["left_numbers"]
                right_numbers = male_ferrule_fallback["right_numbers"]
                print(f"Page {page_idx + 1}: switched to male/ferrule fallback")

        token_male = sum(1 for d in detections if d["kind"] == "male")
        token_female = sum(1 for d in detections if d["kind"] == "female")
        token_ferrule = sum(1 for d in detections if d["kind"] == "ferrule")
        token_unknown = sum(1 for d in detections if d["kind"] == "unknown")

        if (token_male + token_female + token_ferrule) == 0:
            alpha_tb_fallback = build_alpha_left_tb_fallback_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                female_left_template=female_left_template,
                ferrule_right_template=ferrule_right_template,
            )
            if alpha_tb_fallback is not None:
                detections = alpha_tb_fallback["detections"]
                left_numbers = alpha_tb_fallback["left_numbers"]
                right_numbers = alpha_tb_fallback["right_numbers"]
                print(f"Page {page_idx + 1}: switched to alpha/tb fallback")

        token_male = sum(1 for d in detections if d["kind"] == "male")
        token_female = sum(1 for d in detections if d["kind"] == "female")
        token_ferrule = sum(1 for d in detections if d["kind"] == "ferrule")
        token_unknown = sum(1 for d in detections if d["kind"] == "unknown")

        if (token_male + token_female + token_ferrule) == 0:
            ferrule_edge_fallback = build_edge_label_ferrule_fallback_detections(
                page=page,
                gray=gray,
                zoom=zoom_used,
                ferrule_left_template=ferrule_left_template,
                ferrule_right_template=ferrule_right_template,
            )
            if ferrule_edge_fallback is not None:
                detections = ferrule_edge_fallback["detections"]
                left_numbers = ferrule_edge_fallback["left_numbers"]
                right_numbers = ferrule_edge_fallback["right_numbers"]
                print(f"Page {page_idx + 1}: switched to edge-label ferrule fallback")

        print(
            f"Page {page_idx + 1} filtered: "
            f"left_real={len(left_numbers)}, "
            f"right_real={len(right_numbers)}, "
            f"detections={len(detections)}"
        )

        male_count, female_count, ferrule_count, unknown_count, total_count = annotate_page(
            page, detections, bgr.shape
        )
        total_male += male_count
        total_female += female_count
        total_ferrule += ferrule_count
        total_unknown += unknown_count

        draw_debug_image(bgr, detections, debug_dir / f"page_{page_idx + 1}_debug.png")

        print(
            f"Page {page_idx + 1}: male={male_count}, female={female_count}, "
            f"ferrule={ferrule_count}, unknown={unknown_count}, total={total_count}"
        )

    doc.save(str(output_pdf))
    doc.close()

    print(f"Saved: {output_pdf}")
    print(
        f"Document total: male={total_male}, female={total_female}, "
        f"ferrule={total_ferrule}, unknown={total_unknown}, "
        f"total={total_male + total_female + total_ferrule}"
    )


# ---------------------------
# CLI
# ---------------------------

def main():
    parser = argparse.ArgumentParser(description="Cable assembly terminal counter")
    parser.add_argument("input_pdf", help="Input PDF")
    parser.add_argument("male_left_template", help="male_left.png")
    parser.add_argument("male_right_template", help="male_right.png")
    parser.add_argument("female_left_template", help="female_left.png")
    parser.add_argument("female_right_template", help="female_right.png")
    parser.add_argument("ferrule_left_template", help="ferrule_left.png")
    parser.add_argument("ferrule_right_template", help="ferrule_right.png")
    parser.add_argument("-o", "--output", default="output.pdf", help="Annotated PDF output path")
    parser.add_argument("--zoom", type=float, default=4.0, help="PDF render zoom")
    parser.add_argument("--debug-dir", default="debug_out", help="Directory for debug images")
    parser.add_argument("--score-thresh", type=float, default=0.34,
                        help="Minimum best-match score for male/female")
    parser.add_argument("--ferrule-score-thresh", type=float, default=0.16,
                        help="Minimum best-match score for ferrule")
    parser.add_argument("--score-margin", type=float, default=0.02,
                        help="Minimum gap between best and second-best template scores")
    parser.add_argument("--scale", type=float, default=0.85, help="Fallback box scale")
    parser.add_argument("--gap", type=int, default=2, help="Gap between label and endpoint symbol")
    parser.add_argument("--y-offset", type=int, default=3, help="Move boxes down by N pixels")
    parser.add_argument("--dummy-filter", action="store_true",
                        help="Enable dummy-row filtering for drawings that contain dummy terminals")
    parser.add_argument("--dummy-male-pins", default="",
                        help="Comma-separated male pin values to ignore as dummy pins")
    parser.add_argument("--dummy-female-pins", default="",
                        help="Comma-separated female pin values to ignore as dummy pins")

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
        scale=args.scale,
        gap=args.gap,
        y_offset=args.y_offset,
        use_dummy_filter=args.dummy_filter,
        dummy_male_pins=parse_pin_value_set(args.dummy_male_pins),
        dummy_female_pins=parse_pin_value_set(args.dummy_female_pins),
    )


if __name__ == "__main__":
    main()
