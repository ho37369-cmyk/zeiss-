"""
Annotation rendering: draw cell outlines, numbers, and legend on the original image.
"""

import cv2
import numpy as np


LIVE_COLOR = (0, 200, 0)
DEAD_COLOR = (0, 0, 220)
TOTAL_COLOR = (220, 200, 0)


def annotate_image(image, total_result, dead_mask=None, dead_labels=None):
    """Draw cell annotations on the original image.

    Args:
        image: np.ndarray (H, W) or (H, W, 3)
        total_result: dict from count_cells with 'labels', 'props'
        dead_mask: optional boolean mask of dead cell locations

    Returns:
        np.ndarray (H, W, 3) BGR annotated image
    """
    if image.ndim == 2:
        display = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    else:
        display = image.copy()

    labels = total_result.get('labels')
    props = total_result.get('props', [])

    dead_labels = set(dead_labels or [])
    if not dead_labels and dead_mask is not None and labels is not None:
        unique = np.unique(labels[dead_mask > 0])
        for lbl in unique:
            if lbl > 0:
                dead_labels.add(lbl)

    for prop in props:
        label = prop['label']
        centroid = prop['centroid']
        bbox = prop['bbox']
        is_dead = label in dead_labels
        color = DEAD_COLOR if is_dead else LIVE_COLOR

        mask = (labels == label).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            cv2.drawContours(display, [max(cnts, key=cv2.contourArea)], -1, color, 2)
        else:
            y1, x1, y2, x2 = bbox
            cv2.rectangle(display, (x1, y1), (x2, y2), color, 1)

        cv2.putText(display, str(label), (centroid[1] - 5, centroid[0] - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)

    _draw_legend(display, len(props), len(props) - len(dead_labels), len(dead_labels))
    return display


def _draw_legend(display, n_total, n_live, n_dead):
    """Draw legend in top-right corner."""
    h, w = display.shape[:2]
    texts = [f'Total: {n_total}', f'Live: {n_live}', f'Dead: {n_dead}']
    colors = [TOTAL_COLOR, LIVE_COLOR, DEAD_COLOR]

    overlay = display.copy()
    x0, y0 = w - 200, 10
    y1 = y0 + 15 + len(texts) * 22
    cv2.rectangle(overlay, (x0, y0), (w - 10, y1), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.6, display, 0.4, 0, display)

    for i, (text, color) in enumerate(zip(texts, colors)):
        y_pos = y0 + 22 + i * 22
        cv2.putText(display, text, (x0 + 10, y_pos),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
        cv2.rectangle(display, (x0 + 155, y_pos - 11), (x0 + 170, y_pos + 2), color, -1)


def create_dead_mask(dead_result):
    """Create a boolean mask from dead cell counting result."""
    if dead_result is None:
        return None
    labels = dead_result.get('labels')
    if labels is None:
        return None
    return labels > 0


def match_dead_cells(total_result, dead_result, min_overlap_fraction=0.10,
                     max_gap_pixels=8, max_peak_distance=12,
                     min_direct_peak_mad=30):
    """Match dead-stain detections to accepted total-cell objects.

    A dead detection is counted only when a meaningful portion overlaps an
    accepted total cell.  A small spatial tolerance accounts for channel
    registration and for brightfield masks stopping at the inner edge of a
    phase halo.  This rejects isolated background spots and prevents multiple
    dead blobs on one cell from increasing the count twice.
    """
    if not total_result or not dead_result:
        return set()
    total_labels = total_result.get("labels")
    dead_labels = dead_result.get("labels")
    if total_labels is None or dead_labels is None or total_labels.shape != dead_labels.shape:
        return set()

    valid_total = {int(p["label"]) for p in total_result.get("props", [])}
    total_props = total_result.get("props", [])
    dead_props = dead_result.get("props", [])

    # The calibrated Hoechst/PI pipeline records the detection centre for each
    # object.  A very strong PI peak located inside an accepted nucleus region
    # is assigned to that region first.  This handles elongated/asymmetric
    # nuclei whose LoG seed can be far from the PI-positive portion.  We only
    # allow this for peaks at least 30 background MAD above baseline, so dim
    # foreground texture is not promoted to a dead cell.  Remaining candidates
    # use the conservative centre-distance rule.  Multiple PI peaks mapping to
    # one Hoechst nucleus are deliberately counted once.
    if (total_props and dead_props
            and all("peak" in p for p in total_props)
            and all("peak" in p for p in dead_props)):
        total_points = np.asarray([p["peak"] for p in total_props], dtype=float)
        matches = set()
        max_distance_sq = float(max_peak_distance) ** 2
        for dead_prop in dead_props:
            point = np.asarray(dead_prop["peak"], dtype=float)
            y, x = np.rint(point).astype(int)
            if 0 <= y < total_labels.shape[0] and 0 <= x < total_labels.shape[1]:
                region_label = int(total_labels[y, x])
                peak_mad = dead_prop.get("peak_background_mad")
                if (region_label in valid_total and peak_mad is not None
                        and float(peak_mad) >= min_direct_peak_mad):
                    matches.add(region_label)
                    continue
            distances_sq = np.sum((total_points - point) ** 2, axis=1)
            nearest = int(np.argmin(distances_sq))
            if distances_sq[nearest] <= max_distance_sq:
                matches.add(int(total_props[nearest]["label"]))
        return matches

    matches = set()
    for dead_prop in dead_props:
        region = dead_labels == dead_prop["label"]
        region_area = int(np.count_nonzero(region))
        if region_area == 0:
            continue
        candidates, counts = np.unique(total_labels[region], return_counts=True)
        best_label = 0
        best_count = 0
        for label, count in zip(candidates, counts):
            label = int(label)
            if label in valid_total and int(count) > best_count:
                best_label, best_count = label, int(count)
        required = max(3, int(np.ceil(region_area * min_overlap_fraction)))
        if not best_label or best_count < required:
            # Fluorescence and transmitted-light acquisitions can differ by a
            # few pixels.  Expand only already-qualified dead-stain objects;
            # the intensity/shape filtering happens before this matcher.
            kernel_size = max_gap_pixels * 2 + 1
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
            nearby = cv2.dilate(region.astype(np.uint8), kernel) > 0
            candidates, counts = np.unique(
                total_labels[nearby], return_counts=True)
            best_label = 0
            best_count = 0
            for label, count in zip(candidates, counts):
                label = int(label)
                if label in valid_total and int(count) > best_count:
                    best_label, best_count = label, int(count)
            required = 3
        if best_label and best_count >= required:
            matches.add(best_label)
    return matches
