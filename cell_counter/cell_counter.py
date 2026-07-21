"""Cell counting module. Auto-selects brightfield watershed vs fluorescence spot detection + watershed."""

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage import measure, morphology, feature, filters, segmentation
from skimage.feature import blob_log


PARAMS_BRIGHTFIELD = {
    # Cell bodies are substantially more textured than the smooth background
    # in transmitted-light CZI images.  A local-standard-deviation image is
    # therefore much more reliable than thresholding the raw grey values.
    "texture_sigma": 4.2,
    "texture_threshold_scale": 1.3,
    "close_kernel_size": 9,
    "close_iterations": 2,
    "min_component_area": 180,
    "min_area": 250,
    "max_area_ratio": 0.02,
    "max_area_abs": 7000,
    "solidity_threshold": 0.55,
    "watershed_min_distance": 16,
    "watershed_min_radius": 7,
}

PARAMS_FLUORESCENCE = {
    # Preprocessing
    "bg_sub_sigma": 25,
    "fg_sigma": 0.8,
    # LoG blob detection
    "log_min_sigma": 2,
    "log_max_sigma": 15,
    "log_num_sigma": 10,
    "log_threshold": 0.01,
    "log_overlap": 0.5,
    # Post-detection filters
    "min_area": 10,
    "max_area_ratio": 0.05,
    "max_area_abs": 5000,
    "circularity_threshold": 0.25,
    "solidity_threshold": 0.65,
    "watershed_min_distance": 6,
    # Reject dim hot pixels and camera noise in sparse dead-stain channels.
    # The threshold is relative to each image's own background/noise level.
    "min_mean_above_background": 8,
    "min_mean_noise_mad": 6,
}

# Calibrated against 63 J774 fields with manual Hoechst/PI counts.  These are
# intentionally separate from the generic fluorescence blob parameters:
# Hoechst needs one peak per nucleus, while PI needs broad smoothing so
# apoptotic fragments do not become separate dead-cell candidates.
PARAMS_NUCLEI = {
    # A 4-pixel blur made adjacent, in-focus nuclei share one broad maximum.
    # At the native acquisition scale the neighbouring optical halos in a
    # doublet are typically 9--12 pixels apart, so use a smaller blur and let
    # the watershed split them at their genuine valley instead of merging
    # them before peak detection.
    "smooth_sigma": 2.5,
    "peak_min_distance": 6,
    "peak_background_mad": 2.5,
    "min_peak_intensity": 5.0,
    "mask_background_mad": 1.5,
}

PARAMS_DEAD_STAIN = {
    "smooth_sigma": 7.0,
    "peak_min_distance": 22,
    "peak_background_mad": 10.0,
    "min_peak_intensity": 5.0,
    # Four-pixel candidate disks plus the matcher's eight-pixel tolerance give
    # the calibrated 12-pixel PI-to-Hoechst registration radius.
    "marker_radius": 4,
}


def count_cells(image, channel_type, dye=None, role=None):
    if role == "total" and channel_type != "brightfield":
        mask, labels, props = _count_nuclei(image)
    elif role == "dead":
        mask, labels, props = _count_dead_stain(image)
    elif channel_type == "brightfield":
        mask, labels, props = _count_brightfield(image)
    else:
        mask, labels, props = _count_fluorescence(image)
    labels, props = _compact_labels(labels, props)
    return {"total": len(props), "labels": labels, "props": props, "mask": mask}


def _robust_background(image):
    values = image.astype(np.float32)
    background = float(np.median(values))
    mad = max(float(np.median(np.abs(values - background))), 0.5)
    return values, background, mad


def _peak_markers(smoothed, threshold, min_distance):
    points = feature.peak_local_max(
        smoothed,
        min_distance=min_distance,
        threshold_abs=threshold,
        exclude_border=False,
    )
    markers = np.zeros(smoothed.shape, dtype=np.int32)
    for label, point in enumerate(points, start=1):
        markers[tuple(point)] = label
    return points, markers


def _count_nuclei(image):
    """Count all cells as one robust intensity peak per Hoechst/DAPI nucleus."""
    p = PARAMS_NUCLEI
    img = _ensure_uint8(image)
    values, background, mad = _robust_background(img)
    smoothed = cv2.GaussianBlur(values, (0, 0), p["smooth_sigma"])
    peak_threshold = max(
        p["min_peak_intensity"],
        background + p["peak_background_mad"] * mad,
    )
    points, markers = _peak_markers(
        smoothed, peak_threshold, p["peak_min_distance"])
    if len(points) == 0:
        empty = np.zeros(img.shape, dtype=bool)
        return empty, np.zeros(img.shape, dtype=np.int32), []

    mask_threshold = max(
        p["min_peak_intensity"],
        background + p["mask_background_mad"] * mad,
    )
    mask = smoothed >= mask_threshold
    mask = morphology.closing(mask, morphology.disk(2))
    mask = morphology.remove_small_objects(mask, max_size=19)
    # A peak exactly on a threshold boundary must remain a watershed seed.
    mask[points[:, 0], points[:, 1]] = True
    labels = segmentation.watershed(-smoothed, markers, mask=mask).astype(np.int32)
    props = _extract_props(labels, img, 1, img.size)
    for prop in props:
        y, x = points[int(prop["label"]) - 1]
        prop["peak"] = (int(y), int(x))
    return mask, labels, props


def _count_dead_stain(image):
    """Find PI-positive candidates while merging nearby apoptotic fragments."""
    p = PARAMS_DEAD_STAIN
    img = _ensure_uint8(image)
    values, background, mad = _robust_background(img)
    smoothed = cv2.GaussianBlur(values, (0, 0), p["smooth_sigma"])
    threshold = max(
        p["min_peak_intensity"],
        background + p["peak_background_mad"] * mad,
    )
    points, _ = _peak_markers(smoothed, threshold, p["peak_min_distance"])
    labels = np.zeros(img.shape, dtype=np.int32)
    radius = p["marker_radius"]
    for label, (y, x) in enumerate(points, start=1):
        cv2.circle(labels, (int(x), int(y)), radius, int(label), -1)
    mask = labels > 0
    props = _extract_props(labels, img, 1, img.size)
    for prop in props:
        y, x = points[int(prop["label"]) - 1]
        prop["peak"] = (int(y), int(x))
    return mask, labels, props


def _resolve_area_params(params, image):
    h, w = image.shape[:2]
    img_area = h * w
    max_area = min(int(img_area * params.get("max_area_ratio", 0.02)),
                   params.get("max_area_abs", 5000))
    return params["min_area"], max_area


def _ensure_uint8(image):
    if image.dtype == np.uint8:
        return image
    if image.dtype == np.uint16:
        return (image.astype(np.float64) / image.max() * 255).astype(np.uint8)
    mn, mx = image.min(), image.max()
    if mx > mn:
        return ((image.astype(np.float64) - mn) / (mx - mn) * 255).astype(np.uint8)
    return np.zeros_like(image, dtype=np.uint8)


# ---- brightfield pipeline ----

def _count_brightfield(image):
    """Segment whole cells from transmitted-light morphology.

    Raw brightfield intensity is not a dependable foreground cue: cell rims
    are dark, halos are bright, and cell interiors contain both.  Local
    texture separates all of those structures from the smooth culture-medium
    background.  Closing and hole filling recover complete cell bodies, then
    distance-transform watershed separates touching cells.
    """
    p = PARAMS_BRIGHTFIELD
    min_area, max_area = _resolve_area_params(p, image)
    image = _ensure_uint8(image)
    values = image.astype(np.float32)

    sigma = p["texture_sigma"]
    local_mean = cv2.GaussianBlur(values, (0, 0), sigma)
    local_sq_mean = cv2.GaussianBlur(values * values, (0, 0), sigma)
    texture = np.sqrt(np.maximum(local_sq_mean - local_mean * local_mean, 0))
    try:
        texture_threshold = filters.threshold_otsu(texture)
    except ValueError:
        return np.zeros(image.shape, dtype=bool), np.zeros(image.shape, dtype=np.int32), []

    cleaned = texture > texture_threshold * p["texture_threshold_scale"]
    ks = p["close_kernel_size"]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks))
    cleaned = cv2.morphologyEx(
        cleaned.astype(np.uint8), cv2.MORPH_CLOSE, kernel,
        iterations=p["close_iterations"],
    ).astype(bool)
    cleaned = ndi.binary_fill_holes(cleaned)
    cleaned = morphology.remove_small_objects(
        cleaned, max_size=p["min_component_area"])
    cleaned = morphology.remove_small_holes(cleaned, max_size=400)
    if cleaned.sum() == 0:
        return cleaned, np.zeros(image.shape, dtype=np.int32), []

    distance = ndi.distance_transform_edt(cleaned)
    peaks = feature.peak_local_max(
        distance,
        min_distance=p["watershed_min_distance"],
        threshold_abs=p["watershed_min_radius"],
        labels=cleaned,
        exclude_border=False,
    )
    markers = np.zeros(image.shape, dtype=np.int32)
    for marker_id, point in enumerate(peaks, start=1):
        markers[tuple(point)] = marker_id
    if markers.max() == 0:
        labels = measure.label(cleaned).astype(np.int32)
    else:
        labels = segmentation.watershed(-distance, markers, mask=cleaned).astype(np.int32)

    props = _extract_props(
        labels, image, min_area, max_area,
        solidity_threshold=p.get("solidity_threshold"),
    )
    return cleaned, labels, props


# ---- fluorescence pipeline (rebuilt) ----

def _count_fluorescence(image):
    """Fluorescence counting via LoG multi-scale blob detection.

    Does NOT use global thresholding. Instead:
    1. DoG background subtraction isolates spots from uneven illumination.
    2. LoG convolution at multiple scales detects circular bright spots.
    3. Scale-space local maxima = cell detections (no threshold cut).
    4. Detections used as watershed seeds for boundary delineation.
    """
    p = PARAMS_FLUORESCENCE
    min_area, max_area = _resolve_area_params(p, image)
    img = _ensure_uint8(image)
    img_f = img.astype(np.float64)

    # 1. Difference of Gaussians background subtraction
    bg = cv2.GaussianBlur(img_f, (0, 0), p["bg_sub_sigma"])
    fg = cv2.GaussianBlur(img_f, (0, 0), p["fg_sigma"])
    dog = cv2.subtract(fg, bg)
    dog[dog < 0] = 0
    if dog.max() <= 0:
        return np.zeros(image.shape, dtype=bool), np.zeros(image.shape, dtype=np.int32), []

    dog_norm = (dog / dog.max()).astype(np.float64)

    # 2. Multi-scale LoG blob detection
    try:
        blobs = blob_log(
            dog_norm,
            min_sigma=p["log_min_sigma"],
            max_sigma=p["log_max_sigma"],
            num_sigma=p["log_num_sigma"],
            threshold=p["log_threshold"],
            overlap=p["log_overlap"],
        )
    except Exception:
        blobs = np.empty((0, 3))

    if len(blobs) == 0:
        return np.zeros(image.shape, dtype=bool), np.zeros(image.shape, dtype=np.int32), []

    # 3. Filter detections
    valid = []
    for y, x, sigma in blobs:
        yi, xi = int(round(y)), int(round(x))
        radius = int(sigma * np.sqrt(2))
        if yi < 0 or yi >= img.shape[0] or xi < 0 or xi >= img.shape[1]:
            continue
        area_est = np.pi * radius ** 2
        if area_est < min_area or area_est > max_area:
            continue
        y0 = max(0, yi - radius)
        y1 = min(img.shape[0], yi + radius + 1)
        x0 = max(0, xi - radius)
        x1 = min(img.shape[1], xi + radius + 1)
        patch = img_f[y0:y1, x0:x1]
        if patch.max() < 8:
            continue
        valid.append((yi, xi, radius, int(area_est)))

    if len(valid) == 0:
        return np.zeros(image.shape, dtype=bool), np.zeros(image.shape, dtype=np.int32), []

    # 4. Create watershed seeds from blob centers
    h, w = image.shape
    seeds = np.zeros((h, w), dtype=np.int32)
    for i, (yi, xi, radius, area) in enumerate(valid):
        rs = 2
        y0s = max(0, yi - rs)
        y1s = min(h, yi + rs + 1)
        x0s = max(0, xi - rs)
        x1s = min(w, xi + rs + 1)
        seeds[y0s:y1s, x0s:x1s] = i + 2

    # 5. Foreground mask via Li threshold on DoG
    dog_u8 = _ensure_uint8(dog * 255 / dog.max())
    binary = _li_threshold(dog_u8, p)

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k)
    cleaned_bool = cleaned.astype(bool)
    cleaned_bool = morphology.remove_small_objects(cleaned_bool, max_size=min_area)

    # Do not discard a large connected foreground component here.  Touching
    # cells commonly form a component larger than max_area; the seeded
    # watershed below separates it and the per-cell area filter is applied
    # afterwards.

    if cleaned_bool.sum() == 0:
        return cleaned_bool, np.zeros(image.shape, dtype=np.int32), []

    # 6. Watershed with blob seeds
    mask_u8 = cleaned_bool.astype(np.uint8)
    k_bg = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    bg_dilated = cv2.dilate(mask_u8, k_bg, iterations=2)
    seeds[bg_dilated == 0] = 1
    unknown = mask_u8.copy()
    unknown[seeds > 0] = 0
    seeds[unknown > 0] = 0

    clr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    markers = cv2.watershed(clr, seeds)
    labels = markers.copy()
    labels[labels <= 1] = 0

    # 7. Extract props
    props = _extract_props(labels.astype(np.int32), img, min_area, max_area,
                           p.get("circularity_threshold"),
                           p.get("solidity_threshold"))

    # Sparse dead-stain images often contain isolated hot pixels that LoG can
    # legitimately describe as blobs.  Require accepted objects to carry a
    # meaningful average signal above the image-specific background.
    background = float(np.median(img))
    noise_mad = float(np.median(np.abs(img.astype(np.float64) - background)))
    min_mean = background + max(
        p["min_mean_above_background"],
        p["min_mean_noise_mad"] * noise_mad,
    )
    props = [prop for prop in props if prop["mean_intensity"] >= min_mean]

    return cleaned_bool, labels, props


def _li_threshold(image, params):
    """Li's minimum-cross-entropy threshold -- robust for sparse fluorescence.

    Falls back to Triangle, then scaled Otsu.
    """
    try:
        t = filters.threshold_li(image.astype(np.float64))
        if np.isnan(t) or t < 3:
            raise ValueError
    except Exception:
        try:
            t, _ = _triangle_threshold(image)
        except Exception:
            t = filters.threshold_otsu(image) * params.get("li_fallback_otsu_scale", 0.6)
    t = max(t, 5)
    _, binary = cv2.threshold(image, int(t), 255, cv2.THRESH_BINARY)
    return binary


def _watershed_hmaxima(image, mask, params):
    """Watershed with h-maxima seeds on the Euclidean distance transform.

    Places seeds at geometric blob centres rather than intensity peaks,
    yielding substantially better separation of touching / clustered cells.
    """
    mask_u8 = mask.astype(np.uint8)
    dist = ndi.distance_transform_edt(mask)
    if dist.max() <= 1:
        return measure.label(mask).astype(np.int32)

    h_val = params.get("watershed_h", 2)
    hmax = morphology.h_maxima(dist, h_val)
    seeds = measure.label(hmax)

    if seeds.max() == 0:
        pts = feature.peak_local_max(
            dist, min_distance=params.get("watershed_min_distance", 8),
            exclude_border=2)
        seeds = np.zeros(image.shape, dtype=np.int32)
        for i, pt in enumerate(pts):
            seeds[pt[0], pt[1]] = i + 2
    else:
        seeds = seeds.astype(np.int32) + 1
        seeds[seeds == 1] = 0
        seeds[seeds > 0] += 1

    k_bg = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    bg = cv2.dilate(mask_u8, k_bg, iterations=2)
    seeds[bg == 0] = 1

    unknown = mask_u8.copy()
    unknown[seeds > 0] = 0
    seeds[unknown > 0] = 0

    clr = cv2.cvtColor(_ensure_uint8(image), cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image
    markers = cv2.watershed(clr, seeds)
    labels = markers.copy()
    labels[labels <= 1] = 0
    return labels.astype(np.int32)


# ---- shared watershed & utilities ----

def _watershed_segment(image, mask, fg_ratio=0.4):
    dist = ndi.distance_transform_edt(mask)
    if dist.max() <= 1:
        labeled = measure.label(mask)
        return labeled.astype(np.int32)
    hist, edges = np.histogram(dist[dist > 0], bins=50)
    peak_idx = np.argmax(hist)
    peak_dist = (edges[peak_idx] + edges[peak_idx + 1]) / 2
    fg_thresh = max(peak_dist * fg_ratio * 2, dist.max() * fg_ratio * 0.5)
    fg = (dist > fg_thresh).astype(np.uint8)
    kernel_bg = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    bg = cv2.dilate(mask.astype(np.uint8), kernel_bg, iterations=2)
    bg = (255 - bg * 255).astype(np.uint8)
    unknown = cv2.subtract(bg, fg * 255)
    _, markers = cv2.connectedComponents(fg)
    markers = markers + 1
    markers[unknown > 0] = 0
    color_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image
    markers = cv2.watershed(color_img, markers)
    labels = markers.copy()
    labels[labels <= 0] = 0
    return labels.astype(np.int32)


def _triangle_threshold(image):
    hist = cv2.calcHist([image], [0], None, [256], [0, 256]).flatten()
    hist = hist.astype(np.float64)
    hist = np.convolve(hist, [1, 2, 1], mode='same')
    hist /= hist.sum()
    peak_bin = np.argmax(hist)
    left = peak_bin
    right = len(hist) - 1
    max_dist = 0
    thresh_bin = left
    for i in range(left, right + 1):
        dx = right - left
        if dx == 0:
            continue
        t = (i - left) / dx
        y_line = hist[left] + t * (hist[right] - hist[left])
        dist = y_line - hist[i]
        if dist > max_dist:
            max_dist = dist
            thresh_bin = i
    return int(thresh_bin), peak_bin


def _extract_props(labels, original_image, min_area, max_area,
                   circularity_threshold=None, solidity_threshold=None):
    props = measure.regionprops(labels, intensity_image=original_image)
    valid = []
    for prop in props:
        area = prop.area
        if area < min_area or area > max_area:
            continue
        perimeter = prop.perimeter
        circularity = 4 * np.pi * area / (perimeter ** 2) if perimeter > 0 else 0
        if circularity_threshold is not None and circularity < circularity_threshold:
            continue
        if solidity_threshold is not None:
            try:
                hull = prop.area_convex
                solidity = float(area) / hull if hull > 0 else 0
                if solidity < solidity_threshold:
                    continue
            except Exception:
                pass
        valid.append({
            "label": prop.label,
            "area": int(area),
            "centroid": (int(prop.centroid[0]), int(prop.centroid[1])),
            "circularity": float(circularity),
            "mean_intensity": float(prop.intensity_mean),
            "bbox": [int(x) for x in prop.bbox],
        })
    return valid


def _compact_labels(labels, props):
    """Keep only accepted objects and renumber them 1..N.

    Watershed labels can contain rejected noise regions and gaps.  Returning
    those raw labels made the dead-cell overlap code count objects that were
    absent from ``total`` and made annotation numbers disagree with the legend.
    """
    compact = np.zeros(labels.shape, dtype=np.int32)
    compact_props = []
    for new_label, prop in enumerate(props, start=1):
        old_label = prop["label"]
        compact[labels == old_label] = new_label
        item = dict(prop)
        item["label"] = new_label
        compact_props.append(item)
    return compact, compact_props


def _remove_large_objects(mask, max_size):
    labeled = measure.label(mask)
    props = measure.regionprops(labeled)
    for prop in props:
        if prop.area > max_size:
            labeled[labeled == prop.label] = 0
    return labeled > 0
