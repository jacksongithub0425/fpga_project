import argparse
from pathlib import Path

import cv2
import fitz
import numpy as np


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


def extract_candidate_pin_numbers(page, zoom):
    words = extract_words(page, zoom)
    nums = []

    for i, w in enumerate(words):
        txt = w["text"]

        if not (txt.isdigit() and len(txt) <= 3):
            continue

        # Reject numbers that are clearly part of "22 AWG"
        reject = False
        for j, other in enumerate(words):
            if i == j:
                continue

            same_row = abs(other["cy"] - w["cy"]) <= 20
            close_x = abs(other["cx"] - w["cx"]) <= 120

            if same_row and close_x and other["text"].upper() == "AWG":
                reject = True
                break

        if reject:
            continue

        nums.append(w)

    return nums


def to_binary_inv(gray):
    """
    Convert gray image to binary-inverted image:
    black lines/shapes -> white foreground on black background.
    """
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def cluster_numbers_by_x(numbers, x_tol=60):
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

    # keep only useful clusters
    clusters = [c for c in clusters if len(c) >= 2]
    return clusters


def best_local_match(page_bin, template_bin, roi, threshold=0.40,
                     scales=(0.80, 0.90, 1.00, 1.10, 1.20)):
    x0, y0, x1, y1 = roi

    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(page_bin.shape[1], x1)
    y1 = min(page_bin.shape[0], y1)

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
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > best_score:
            best_score = max_val
            best_box = (x0 + max_loc[0], y0 + max_loc[1], tw, th)

    if best_score >= threshold:
        return best_box, best_score

    return None


def split_pin_number_columns(numbers):
    clusters = cluster_numbers_by_x(numbers, x_tol=70)

    if len(clusters) < 2:
        return [], []

    # take the two biggest clusters
    clusters = sorted(clusters, key=lambda c: (-len(c), sum(n["cx"] for n in c) / len(c)))[:2]
    clusters = sorted(clusters, key=lambda c: sum(n["cx"] for n in c) / len(c))

    left_cluster = sorted(clusters[0], key=lambda n: (n["cy"], n["cx"]))
    right_cluster = sorted(clusters[1], key=lambda n: (n["cy"], n["cx"]))

    return left_cluster, right_cluster


def load_template(path):
    t = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if t is None:
        raise FileNotFoundError(f"Cannot read template: {path}")
    return to_binary_inv(t)


def non_max_suppression(boxes, scores, iou_thresh=0.25):
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


def collapse_by_row(boxes, scores, y_tol=18):
    """
    Keep only one detection per horizontal row.
    boxes: list of (x, y, w, h)
    scores: list of float
    """
    if not boxes:
        return []

    items = sorted(zip(boxes, scores), key=lambda t: (t[0][1], -t[1]))
    kept = []

    for box, score in items:
        x, y, w, h = box
        matched_row = False

        for j, (kbox, kscore) in enumerate(kept):
            kx, ky, kw, kh = kbox
            if abs(y - ky) <= y_tol:
                matched_row = True
                if score > kscore:
                    kept[j] = (box, score)
                break

        if not matched_row:
            kept.append((box, score))

    return [b for b, _ in kept]


def detect_template_multiscale(gray, template_bin, threshold=0.55, roi=None,
                               scales=(0.85, 0.95, 1.00, 1.10, 1.20)):
    """
    Multi-scale template matching restricted to a page ROI.
    roi = (x0, y0, x1, y1) in image coordinates
    """
    page_bin = to_binary_inv(gray)

    if roi is None:
        x0, y0, x1, y1 = 0, 0, page_bin.shape[1], page_bin.shape[0]
    else:
        x0, y0, x1, y1 = roi

    search = page_bin[y0:y1, x0:x1]

    boxes = []
    scores = []

    for scale in scales:
        new_w = max(4, int(template_bin.shape[1] * scale))
        new_h = max(4, int(template_bin.shape[0] * scale))

        resized = cv2.resize(template_bin, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

        if resized.shape[0] >= search.shape[0] or resized.shape[1] >= search.shape[1]:
            continue

        result = cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= threshold)

        for (x, y) in zip(xs, ys):
            boxes.append((int(x + x0), int(y + y0), int(new_w), int(new_h)))
            scores.append(float(result[y, x]))

    keep = non_max_suppression(boxes, scores, iou_thresh=0.15)
    nms_boxes = [boxes[i] for i in keep]
    nms_scores = [scores[i] for i in keep]

    # One terminal marker per row
    row_tol = max(10, int(template_bin.shape[0] * 0.9))
    return collapse_by_row(nms_boxes, nms_scores, y_tol=row_tol)


def extract_numeric_words(page, zoom, img_w):
    """
    Extract numeric labels from the PDF text layer and classify them by side.
    Middle-page numbers like '22' from '22 AWG' are rejected by edge filtering.
    """
    words = page.get_text("words")
    all_nums = []

    for w in words:
        x0, y0, x1, y1, text, *_ = w
        text = text.strip()

        if text.isdigit() and len(text) <= 3:
            cx = ((x0 + x1) * 0.5) * zoom
            cy = ((y0 + y1) * 0.5) * zoom

            all_nums.append({
                "text": text,
                "x0": x0 * zoom,
                "y0": y0 * zoom,
                "x1": x1 * zoom,
                "y1": y1 * zoom,
                "cx": cx,
                "cy": cy,
            })

    # Wider than before: helps when labels are not extremely close to page edge
    left_nums = [n for n in all_nums if n["cx"] < img_w * 0.25]
    right_nums = [n for n in all_nums if n["cx"] > img_w * 0.75]

    return left_nums, right_nums


def pair_markers_with_numbers(markers, numbers, kind):
    """
    Pair marker to nearest side-appropriate number if available.
    Marker is kept even if no number is found.
    """
    detections = []
    used_number_ids = set()

    markers_sorted = sorted(markers, key=lambda b: (b[1], b[0]))

    for i, (x, y, w, h) in enumerate(markers_sorted, start=1):
        mx = x + w / 2.0
        my = y + h / 2.0

        best_idx = None
        best_dist = 1e18

        for idx, num in enumerate(numbers):
            if idx in used_number_ids:
                continue

            dx = num["cx"] - mx
            dy = num["cy"] - my

            if kind == "male":
                # left marker, number usually to the right and a bit above
                if not (-10 <= dx <= 180):
                    continue
                if not (-100 <= dy <= 40):
                    continue
            else:
                # right marker, number usually to the left and a bit above
                if not (-180 <= dx <= 10):
                    continue
                if not (-100 <= dy <= 40):
                    continue

            dist = dx * dx + dy * dy
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

        pin = None
        if best_idx is not None:
            used_number_ids.add(best_idx)
            pin = numbers[best_idx]["text"]

        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": kind,
            "pin": pin,
            "id": f'{"M" if kind == "male" else "F"}{i}',
        })

    return detections


def detect_from_pin_numbers(gray, page, zoom, male_template, female_template, threshold):
    page_bin = to_binary_inv(gray)
    all_pin_numbers = extract_candidate_pin_numbers(page, zoom)
    left_numbers, right_numbers = split_pin_number_columns(all_pin_numbers)

    detections = []

    # left side: search for '<' just left of the number
    for i, num in enumerate(left_numbers, start=1):
        cx = int(num["cx"])
        cy = int(num["cy"])

        roi = (
            cx - 140,   # left
            cy - 60,    # top
            cx + 20,    # right
            cy + 40     # bottom
        )

        result = best_local_match(page_bin, male_template, roi, threshold=threshold)
        if result is None:
            continue

        (x, y, w, h), score = result
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "male",
            "pin": num["text"],
            "id": f"M{i}",
            "score": score,
        })

    # right side: search for '>' just right of the number
    for i, num in enumerate(right_numbers, start=1):
        cx = int(num["cx"])
        cy = int(num["cy"])

        roi = (
            cx - 20,
            cy - 60,
            cx + 140,
            cy + 40
        )

        result = best_local_match(page_bin, female_template, roi, threshold=threshold)
        if result is None:
            continue

        (x, y, w, h), score = result
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "female",
            "pin": num["text"],
            "id": f"F{i}",
            "score": score,
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
    total_count = male_count + female_count

    for d in detections:
        rect_pdf = image_rect_to_pdf_rect(d["x"], d["y"], d["w"], d["h"], page.rect, img_w, img_h)

        color = (1, 0, 0) if d["kind"] == "male" else (0, 0, 1)
        page.draw_rect(rect_pdf, color=color, width=0.8)

        label = d["id"] if d["pin"] is None else f'{d["id"]}:{d["pin"]}'
        label_y = rect_pdf.y0 - 2
        if label_y < 8:
            label_y = rect_pdf.y1 + 8

        page.insert_text(
            fitz.Point(rect_pdf.x0, label_y),
            label,
            fontsize=6,
            color=color,
        )

    summary = fitz.Rect(page.rect.width - 180, 20, page.rect.width - 20, 90)
    page.draw_rect(summary, color=(0, 0, 0), width=1)
    page.insert_textbox(
        summary,
        f"Male: {male_count}\nFemale: {female_count}\nTotal: {total_count}",
        fontsize=10,
        color=(0, 0, 0),
    )

    return male_count, female_count, total_count


def draw_debug_image(bgr, detections, out_path):
    dbg = bgr.copy()
    for d in detections:
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]
        color = (0, 0, 255) if d["kind"] == "male" else (255, 0, 0)
        cv2.rectangle(dbg, (x, y), (x + w, y + h), color, 2)
        cv2.putText(
            dbg,
            f'{d["id"]}:{d["pin"]}',
            (x, max(12, y - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
            cv2.LINE_AA
        )
    cv2.imwrite(str(out_path), dbg)


def clamp_box(x, y, w, h, img_w, img_h):
    x = max(0, min(int(round(x)), img_w - 1))
    y = max(0, min(int(round(y)), img_h - 1))
    w = max(1, min(int(round(w)), img_w - x))
    h = max(1, min(int(round(h)), img_h - y))
    return x, y, w, h


def make_anchor_box(num, kind, template_bin, img_w, img_h, scale=0.85, gap=2, y_offset=3):
    th, tw = template_bin.shape[:2]

    bw = max(6, int(tw * scale))
    bh = max(8, int(th * scale))

    cy = num["cy"]

    if kind == "male":
        x = num["x0"] - gap - bw
    else:
        x = num["x1"] + gap

    y = cy - bh / 2 + y_offset

    return clamp_box(x, y, bw, bh, img_w, img_h)


def detect_from_pin_numbers_geometry(page, gray, zoom, male_template, female_template):
    """
    Use PDF pin-number positions as anchors.
    No template matching for v1 detection; template images are only used for box size.
    """
    img_h, img_w = gray.shape[:2]

    all_pin_numbers = extract_candidate_pin_numbers(page, zoom)
    left_numbers, right_numbers = split_pin_number_columns(all_pin_numbers)

    left_numbers = sorted(left_numbers, key=lambda n: (n["cy"], n["cx"]))
    right_numbers = sorted(right_numbers, key=lambda n: (n["cy"], n["cx"]))

    detections = []

    for i, num in enumerate(left_numbers, start=1):
        x, y, w, h = make_anchor_box(num, "male", male_template, img_w, img_h, scale=0.85, gap=2, y_offset=18)
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "male",
            "pin": num["text"],
            "id": f"M{i}",
        })

    for i, num in enumerate(right_numbers, start=1):
        x, y, w, h = make_anchor_box(num, "female", female_template, img_w, img_h, scale=0.85, gap=2, y_offset=18)
        detections.append({
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "kind": "female",
            "pin": num["text"],
            "id": f"F{i}",
        })

    return detections, left_numbers, right_numbers


def process_pdf(input_pdf, output_pdf, male_template_path, female_template_path,
                zoom=4.0, match_threshold=0.48, debug_dir="debug_out"):
    input_pdf = Path(input_pdf)
    output_pdf = Path(output_pdf)
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(input_pdf))

    male_template = load_template(male_template_path)
    female_template = load_template(female_template_path)

    total_male = 0
    total_female = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        bgr, gray, zoom_used = render_page(page, zoom=zoom)

        detections, left_numbers, right_numbers = detect_from_pin_numbers_geometry(
            page=page,
            gray=gray,
            zoom=zoom_used,
            male_template=male_template,
            female_template=female_template,
        )

        print(
            f"Page {page_idx + 1} raw: "
            f"left_pin_numbers={len(left_numbers)}, right_pin_numbers={len(right_numbers)}, "
            f"detections={len(detections)}"
        )

        male_count, female_count, total_count = annotate_page(page, detections, bgr.shape)

        total_male += male_count
        total_female += female_count

        draw_debug_image(bgr, detections, debug_dir / f"page_{page_idx + 1}_debug.png")

        print(
            f"Page {page_idx + 1}: "
            f"male={male_count}, female={female_count}, total={total_count}"
        )

        male_count, female_count, total_count = annotate_page(page, detections, bgr.shape)

        total_male += male_count
        total_female += female_count

        draw_debug_image(bgr, detections, debug_dir / f"page_{page_idx + 1}_debug.png")

        print(
            f"Page {page_idx + 1}: "
            f"male={male_count}, female={female_count}, total={total_count}"
        )

        male_count, female_count, total_count = annotate_page(page, detections, bgr.shape)

        total_male += male_count
        total_female += female_count

        draw_debug_image(bgr, detections, debug_dir / f"page_{page_idx + 1}_debug.png")

        print(
            f"Page {page_idx + 1}: "
            f"male={male_count}, female={female_count}, total={total_count}"
        )

    doc.save(str(output_pdf))
    doc.close()

    print(f"Saved: {output_pdf}")
    print(f"Document total: male={total_male}, female={total_female}, total={total_male + total_female}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cable terminal counter")
    parser.add_argument("input_pdf")
    parser.add_argument("male_template")
    parser.add_argument("female_template")
    parser.add_argument("-o", "--output", default="input_drawing_annotated.pdf")
    parser.add_argument("--zoom", type=float, default=4.0)
    parser.add_argument("--match-threshold", type=float, default=0.48)
    parser.add_argument("--debug-dir", default="debug_out")
    args = parser.parse_args()

    process_pdf(
        input_pdf=args.input_pdf,
        output_pdf=args.output,
        male_template_path=args.male_template,
        female_template_path=args.female_template,
        zoom=args.zoom,
        match_threshold=args.match_threshold,
        debug_dir=args.debug_dir,
    )