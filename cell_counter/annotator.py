"""
Annotation rendering: draw cell outlines, numbers, and legend on the original image.
"""

import cv2
import numpy as np


LIVE_COLOR = (0, 200, 0)
DEAD_COLOR = (0, 0, 220)
TOTAL_COLOR = (220, 200, 0)


def annotate_image(image, total_result, dead_mask=None):
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

    dead_labels = set()
    if dead_mask is not None and labels is not None:
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
