"""
Cell counting module.
Auto-selects brightfield watershed vs fluorescence thresholding.
"""

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage import measure, morphology


PARAMS_BRIGHTFIELD = {
    "gaussian_sigma": 1.5,
    "adaptive_block_size": 31,
    "adaptive_c": 6,
    "morph_kernel_size": 3,
    "min_area": 80,
    "max_area": 5000,
}

PARAMS_FLUORESCENCE = {
    "gaussian_sigma": 1.0,
    "morph_kernel_size": 3,
    "min_area": 30,
    "max_area": 3000,
    "circularity_threshold": 0.4,
}


def count_cells(image, channel_type, dye=None):
    """Count cells using optimal algorithm for channel type.

    Returns:
        dict with: total, labels (ndarray int32), props (list), mask (bool)
    """
    if channel_type == "brightfield":
        mask, labels, props = _count_brightfield(image)
    else:
        mask, labels, props = _count_fluorescence(image)

    return {"total": len(props), "labels": labels, "props": props, "mask": mask}


def _count_brightfield(image):
    """Brightfield / phase contrast counting: adaptive threshold + watershed."""
    p = PARAMS_BRIGHTFIELD

    blurred = cv2.GaussianBlur(image, (0, 0), p["gaussian_sigma"])

    block_size = p["adaptive_block_size"]
    if block_size % 2 == 0:
        block_size += 1

    binary = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, block_size, p["adaptive_c"],
    )

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                       (p["morph_kernel_size"], p["morph_kernel_size"]))
    cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    cleaned = morphology.remove_small_objects(cleaned.astype(bool), max_size=p["min_area"])
    cleaned = _remove_large_objects(cleaned, max_size=p["max_area"])

    labels = np.zeros(image.shape, dtype=np.int32)

    if cleaned.sum() > 0:
        dist = ndi.distance_transform_edt(cleaned)
        dist_norm = (dist / dist.max() * 255).astype(np.uint8) if dist.max() > 0 else dist

        _, fg = cv2.threshold(dist_norm, 0.3 * 255, 255, cv2.THRESH_BINARY)
        fg = fg.astype(np.uint8)

        kernel_bg = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        bg = cv2.dilate(cleaned.astype(np.uint8), kernel_bg, iterations=2)
        bg = (255 - bg).astype(np.uint8)

        unknown = cv2.subtract(bg, fg)

        _, markers = cv2.connectedComponents(fg)
        markers = markers + 1
        markers[unknown > 0] = 0

        color_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        markers = cv2.watershed(color_img, markers)
        labels = markers.copy()
        labels[labels <= 0] = 0

    props = _extract_props(labels, image, p["min_area"], p["max_area"])
    return cleaned, labels, props


def _count_fluorescence(image):
    """Fluorescence counting: Otsu + connected components."""
    p = PARAMS_FLUORESCENCE

    blurred = cv2.GaussianBlur(image, (0, 0), p["gaussian_sigma"])
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                       (p["morph_kernel_size"], p["morph_kernel_size"]))
    cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    cleaned = morphology.remove_small_objects(cleaned.astype(bool), max_size=p["min_area"])
    cleaned = _remove_large_objects(cleaned, max_size=p["max_area"])

    labels = measure.label(cleaned)
    props = _extract_props(labels, image, p["min_area"], p["max_area"])

    return cleaned, labels, props


def _extract_props(labels, original_image, min_area, max_area):
    """Extract region properties for valid cells."""
    props = measure.regionprops(labels, intensity_image=original_image)
    valid = []
    for prop in props:
        area = prop.area
        if area < min_area or area > max_area:
            continue
        perimeter = prop.perimeter
        circularity = 4 * np.pi * area / (perimeter ** 2) if perimeter > 0 else 0
        valid.append({
            "label": prop.label,
            "area": int(area),
            "centroid": (int(prop.centroid[0]), int(prop.centroid[1])),
            "circularity": float(circularity),
            "mean_intensity": float(prop.intensity_mean),
            "bbox": [int(x) for x in prop.bbox],
        })
    return valid


def _remove_large_objects(mask, max_size):
    """Remove objects larger than max_size from boolean mask."""
    labeled = measure.label(mask)
    props = measure.regionprops(labeled)
    for prop in props:
        if prop.area > max_size:
            labeled[labeled == prop.label] = 0
    return labeled > 0
