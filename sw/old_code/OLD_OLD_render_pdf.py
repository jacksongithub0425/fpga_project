import argparse
from pathlib import Path

import re
import cv2
import fitz  # PyMuPDF
import numpy as np


PIN_TOKEN_RE = re.compile(r"^(?=.*\d)[A-Z0-9]+(?:-[A-Z0-9]+)*$", re.IGNORECASE)


def is_pin_token(text):
    text = text.strip().upper()
    if not text:
        return False
    return bool(PIN_TOKEN_RE.fullmatch(text))

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


def extract_words(page, zoom):
    words = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text, *_ = w
        text = text.strip()
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


def extract_candidate_pin_numbers_from_words(words):
    nums = []

    for i, w in enumerate(words):
        txt = w["text"].strip().upper()
        if not is_pin_token(txt):
            continue

        reject = False
        for j, other in enumerate(words):
            if i == j:
                continue
            same_row = abs(other["cy"] - w["cy"]) <= 20
            close_x = abs(other["cx"] - w["cx"]) <= 140
            if same_row and close_x and other["text"].upper() == "AWG":
                reject = True
                break

        if not reject:
            ww = dict(w)
            ww["text"] = txt
            nums.append(ww)

    return nums


def extract_candidate_pin_numbers(page, zoom):
    words = extract_words(page, zoom)
    nums = []

    for i, w in enumerate(words):
        txt = w["text"].strip().upper()

        if not is_pin_token(txt):
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
        if x0 <= w["cx"] <= x1 and abs(w["cy"] - row_cy) <= y_tol:
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

    kernel_w = max(25, min(120, roi.shape[1] // 4))
    horizontal_kernel = np.ones((1, kernel_w), np.uint8)

    # Extract long horizontal wire strokes
    horizontal = cv2.morphologyEx(roi, cv2.MORPH_OPEN, horizontal_kernel)

    # Keep only non-horizontal content: text, branches, circles, splices, etc.
    residual = cv2.subtract(roi, horizontal)
    residual = cv2.morphologyEx(residual, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    return cv2.countNonZero(residual) >= min_pixels


def filter_dummy_pin_rows(left_numbers, right_numbers, words, page_bin, y_tol=14):
    if not left_numbers and not right_numbers:
        return [], []

    # Need both columns so we can inspect the middle harness area
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


def align_pin_rows(left_numbers, right_numbers, y_tol=18):
    left_sorted = sorted(left_numbers, key=lambda n: n["cy"])
    right_sorted = sorted(right_numbers, key=lambda n: n["cy"])

    matched_left = []
    matched_right = []
    used_right = set()

    for ln in left_sorted:
        best_j = None
        best_dy = 1e9
        for rj, rn in enumerate(right_sorted):
            if rj in used_right:
                continue
            dy = abs(ln["cy"] - rn["cy"])
            if dy <= y_tol and dy < best_dy:
                best_dy = dy
                best_j = rj
        if best_j is not None:
            matched_left.append(ln)
            matched_right.append(right_sorted[best_j])
            used_right.add(best_j)

    return matched_left, matched_right


def make_anchor_box(num, side, template_bin, img_w, img_h,
                    direction="outward", scale=0.85, gap=2, y_offset=3):
    th, tw = template_bin.shape[:2]
    bw = max(6, int(tw * scale))
    bh = max(8, int(th * scale))
    cy = num["cy"]

    place_left_of_label = (
        (side == "left" and direction == "outward") or
        (side == "right" and direction == "inward")
    )

    if place_left_of_label:
        x = num["x0"] - gap - bw
    else:
        x = num["x1"] + gap

    y = cy - bh / 2 + y_offset
    return clamp_box(x, y, bw, bh, img_w, img_h)


def build_local_roi(num, side, template_bin, page_bin,
                    direction="outward", gap=2, y_offset=3):
    img_h, img_w = page_bin.shape[:2]
    th, tw = template_bin.shape[:2]

    roi_w = max(28, int(tw * 5.0))
    roi_h = max(20, int(th * 3.5))
    cy = num["cy"] + y_offset

    search_left_of_label = (
        (side == "left" and direction == "outward") or
        (side == "right" and direction == "inward")
    )

    if search_left_of_label:
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


def classify_side_endpoint(num, side, page_bin,
                           male_template, female_template, ferrule_template,
                           score_thresh=0.34, ferrule_score_thresh=0.18,
                           score_margin=0.02, gap=2, y_offset=3):
    specs = {
        "male": {
            "template": male_template,
            "direction": "outward",
        },
        "female": {
            "template": female_template,
            "direction": "outward",
        },
        "ferrule": {
            "template": ferrule_template,
            "direction": "inward",
        },
    }

    matches = {}
    scores = {}

    vertical_tol = max(
        12,
        int(min(t.shape[0] for t in [male_template, female_template, ferrule_template]) * 0.45)
    )

    target_cy = num["cy"] + y_offset

    for kind, spec in specs.items():
        roi = build_local_roi(
            num=num,
            side=side,
            template_bin=spec["template"],
            page_bin=page_bin,
            direction=spec["direction"],
            gap=gap,
            y_offset=y_offset,
        )

        hit = best_template_match_in_roi(
            page_bin,
            spec["template"],
            roi,
            target_cy=target_cy,
            vertical_tol=vertical_tol,
        )

        matches[kind] = hit
        scores[kind] = -1.0 if hit is None else hit["score"]

    valid = [(kind, scores[kind]) for kind in specs if matches[kind] is not None]
    if not valid:
        return {
            "kind": "unknown",
            "box": None,
            "direction": "outward",
            "male_score": scores["male"],
            "female_score": scores["female"],
            "ferrule_score": scores["ferrule"],
        }

    valid.sort(key=lambda x: x[1], reverse=True)
    best_kind, best_score = valid[0]
    best_box = matches[best_kind]["box"]
    best_direction = specs[best_kind]["direction"]
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


def detect_from_pin_numbers_auto(page, gray, zoom,
                                 male_left_template, male_right_template,
                                 female_left_template, female_right_template,
                                 ferrule_left_template, ferrule_right_template,
                                 score_thresh=0.34, ferrule_score_thresh=0.18,
                                 score_margin=0.02, scale=0.85, gap=2, y_offset=3):
    page_bin = to_binary_inv(gray)
    img_h, img_w = gray.shape[:2]

    words = extract_words(page, zoom)
    all_pin_numbers = extract_candidate_pin_numbers_from_words(words)

    left_numbers, right_numbers = split_pin_number_columns(all_pin_numbers)

    # Real filtering, not row truncation
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

        if box is None:
            fallback = template_for_kind(
                kind, male_left_template, female_left_template, ferrule_left_template
            )
            box = make_anchor_box(
                num, "left", fallback, img_w, img_h,
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

        if box is None:
            fallback = template_for_kind(
                kind, male_right_template, female_right_template, ferrule_right_template
            )
            box = make_anchor_box(
                num, "right", fallback, img_w, img_h,
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

        label = f'{d["id"]}:{d["pin"]}'
        label_y = rect_pdf.y0 - 2
        if label_y < 8:
            label_y = rect_pdf.y1 + 8
        page.insert_text(fitz.Point(rect_pdf.x0, label_y), label, fontsize=6, color=color)

    summary = fitz.Rect(page.rect.width - 180, 20, page.rect.width - 20, 120)
    page.draw_rect(summary, color=(0, 0, 0), width=1)
    page.insert_textbox(
        summary,
        f"Male: {male_count}\nFemale: {female_count}\nFerrule: {ferrule_count}\nUnknown: {unknown_count}\nTotal: {total_count}",
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
            f'{d["id"]}:{d["pin"]}',
            (x, max(12, y - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            color,
            1,
            cv2.LINE_AA
        )

    cv2.imwrite(str(out_path), dbg)


def process_pdf(input_pdf, output_pdf,
                male_left_template_path, male_right_template_path,
                female_left_template_path, female_right_template_path,
                ferrule_left_template_path, ferrule_right_template_path,
                zoom=4.0, debug_dir="debug_out",
                score_thresh=0.34, ferrule_score_thresh=0.18,
                score_margin=0.02, scale=0.85, gap=2, y_offset=3):
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
        )

        print(
            f"Page {page_idx + 1} filtered: "
            f"left_real={len(left_numbers)}, "
            f"right_real={len(right_numbers)}, "
            f"detections={len(detections)}"
        )

        male_count, female_count, ferrule_count, unknown_count, total_count = annotate_page(page, detections, bgr.shape)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cable assembly terminal counter")
    parser.add_argument("input_pdf", help="Input PDF")
    parser.add_argument("male_left_template", help="male_left.png")
    parser.add_argument("male_right_template", help="male_right.png")
    parser.add_argument("female_left_template", help="female_left.png")
    parser.add_argument("female_right_template", help="female_right.png")
    parser.add_argument("-o", "--output", default="output.pdf", help="Annotated PDF output path")
    parser.add_argument("--zoom", type=float, default=4.0, help="PDF render zoom")
    parser.add_argument("--debug-dir", default="debug_out", help="Directory for debug images")
    parser.add_argument("--score-thresh", type=float, default=0.34, help="Minimum best-match score")
    parser.add_argument("--score-margin", type=float, default=0.02, help="Min difference between male/female scores")
    parser.add_argument("--scale", type=float, default=0.85, help="Fallback box scale")
    parser.add_argument("--gap", type=int, default=2, help="Gap between number and endpoint symbol")
    parser.add_argument("--y-offset", type=int, default=3, help="Move boxes down by N pixels")
    parser.add_argument("ferrule_left_template", help="ferrule_left.png")
    parser.add_argument("ferrule_right_template", help="ferrule_right.png")
    parser.add_argument("--ferrule-score-thresh", type=float, default=0.18,
                    help="Minimum best-match score for ferrule")

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
        score_margin=args.score_margin,
        scale=args.scale,
        gap=args.gap,
        y_offset=args.y_offset,
        ferrule_score_thresh=args.ferrule_score_thresh,
    )
