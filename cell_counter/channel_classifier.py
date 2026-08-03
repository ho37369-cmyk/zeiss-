"""
Automatic channel type classification.
Identifies brightfield vs fluorescence channels and specific dye types.
"""

import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class ChannelInfo:
    index: int
    name: str
    channel_type: str  # "brightfield" or "fluorescence"
    dye: Optional[str] = None
    role: Optional[str] = None  # "total", "dead", or None


BRIGHTFIELD_KEYWORDS = [
    "brightfield", "bright field", "bf", "phase", "ph",
    "dic", "differential", "contrast", "transmitted",
    "tl", "trans", "transmission", "white light",
]

# Dye names are useful hints, but they are not authoritative.  In different
# experiments the same dye can be used for the total-cell or dead-cell stain.
# Roles are therefore assigned from the observed object counts first; these
# sets are retained only as a tie-breaker when the image evidence is unusable.
TOTAL_DYES = {"dapi"}
DEAD_DYES = {"tritc", "cy5", "pi", "rhodamine"}

DYE_PATTERNS = [
    ("dapi", ["dapi", "hoechst", "dna stain", "nucl"]),
    ("fitc", ["fitc", "gfp", "eyfp", "egfp", "488", "alexa fluor 488", "calcein"]),
    ("tritc", ["tritc", "cy3", "texas red", "568", "alexa fluor 568", "alexa fluor 555", "pi", "propidium"]),
    ("cy5", ["cy5", "647", "alexa fluor 647", "640", "alexa fluor 640", "deep red"]),
]


def classify_channels(channel_metadata, scenes):
    """Classify all channels by type.

    Args:
        channel_metadata: list of {"index": int, "name": str, "dye": str or None}
        scenes: dict of {"SceneX": {"channels": [...]}}

    Returns:
        list of ChannelInfo objects
    """
    info_map = {}
    image_samples = {}

    for s_name, s_data in scenes.items():
        for ch in s_data["channels"]:
            idx = ch["index"]
            if idx not in info_map:
                info_map[idx] = {"name": ch["name"]}
                image_samples[idx] = ch["image"]

    for cm in channel_metadata:
        idx = cm["index"]
        if idx in info_map:
            info_map[idx]["name"] = cm["name"]

    results = []
    for idx in sorted(info_map.keys()):
        name = info_map[idx]["name"]
        img = image_samples.get(idx)
        ch_type, dye = _classify_single_channel(name, img)
        results.append(ChannelInfo(index=idx, name=name, channel_type=ch_type, dye=dye))

    # When two fluorescence channels exist, choose roles from the observed
    # population size.  Dead cells are expected to be a strict subset of all
    # cells, so the channel with more nucleus-like detections is the total-cell
    # reference and the sparse channel is the dead-cell stain.  This is
    # deliberately independent of dye names: a DAPI channel can be the dead
    # stain and rhodamine can label all cells in another experiment.
    brightfield = [r for r in results if r.channel_type == "brightfield"]
    fluorescence = [r for r in results if r.channel_type == "fluorescence"]
    if fluorescence:
        if len(fluorescence) >= 2:
            peak_counts = {
                r.index: _nuclear_peak_count(image_samples.get(r.index))
                for r in fluorescence
            }
            max_count = max(peak_counts.values())
            min_count = min(peak_counts.values())

            # Only use count-based assignment when at least one channel has
            # usable detections and the ordering is informative.  A tie (or
            # two empty/very dim channels) is resolved by the dye hints below.
            if max_count > 0 and max_count > min_count:
                total = max(fluorescence, key=lambda r: peak_counts[r.index])
                dead = min(fluorescence, key=lambda r: peak_counts[r.index])
            else:
                total = next(
                    (r for r in fluorescence if r.dye in TOTAL_DYES), None)
                dead = next(
                    (r for r in fluorescence if r.dye in DEAD_DYES and r is not total),
                    None,
                )
                if total is None:
                    total = max(
                        fluorescence,
                        key=lambda r: _fluorescence_occupancy(
                            image_samples.get(r.index)),
                    )
                if dead is None:
                    candidates = [r for r in fluorescence if r is not total]
                    if candidates:
                        dead = min(
                            candidates,
                            key=lambda r: _fluorescence_occupancy(
                                image_samples.get(r.index)),
                        )

            for r in fluorescence:
                r.role = None
            total.role = "total"
            if dead is not None and dead is not total:
                dead.role = "dead"
        elif len(fluorescence) == 1:
            # A lone fluorescence channel cannot establish a dead-cell subset;
            # treat it as the total-cell reference and leave dead unassigned.
            fluorescence[0].role = "total"

    # Brightfield is a morphology/reference channel only.  It must never be
    # promoted to the total-cell role: the counting workflow is intentionally
    # restricted to black-background, bright-spot fluorescence channels.

    return results


def _classify_single_channel(name, image=None):
    """Classify a single channel as brightfield or fluorescence."""
    name_lower = name.lower()

    for kw in BRIGHTFIELD_KEYWORDS:
        if kw in name_lower:
            return "brightfield", None

    for dye_short, patterns in DYE_PATTERNS:
        for pat in patterns:
            if pat in name_lower:
                return "fluorescence", dye_short

    if image is not None:
        return _classify_by_image_stats(image)

    return "fluorescence", "other"


def _classify_by_image_stats(image):
    """Fallback: classify by image statistics."""
    if image.ndim == 3:
        image = np.mean(image, axis=2).astype(np.uint8)

    mean_val = float(np.mean(image))
    std_val = float(np.std(image))

    hist, _ = np.histogram(image, bins=64, range=(0, 255))
    hist = hist.astype(np.float64)
    hist /= hist.sum() + 1e-8

    low = hist[:20].sum()
    high = hist[40:].sum()

    # Fluorescence images have a predominantly dark background.  Use robust
    # percentiles rather than the maximum, which is easily skewed by hot pixels.
    median_val = float(np.median(image))
    p90 = float(np.percentile(image, 90))
    dark_fraction = float(np.mean(image <= max(8, median_val * 0.35)))

    if median_val < 45 or dark_fraction > 0.45:
        return "fluorescence", "other"
    # Darkly exposed phase/brightfield fields often have a mean just below
    # 80 (the previous cut-off misclassified the supplied 79.7/75 field as
    # fluorescence).  A high median and very small near-black fraction are
    # stronger indicators of transmitted light than an absolute mean.
    if ((mean_val >= 75 and median_val >= 60 and p90 >= 90)
            # Low-exposure phase fields can be fairly flat after display
            # normalization (for example, median ~60 and p90 ~75).  Their
            # small near-black fraction is the useful distinction from a
            # fluorescence image with a dark background.
            or (median_val >= 50 and dark_fraction < 0.08
                and p90 >= median_val + 5)):
        return "brightfield", None
    if low > 0.5 or (std_val > mean_val * 0.7 and median_val < 70):
        return "fluorescence", "other"

    # Keep both tuple elements at the same level.  The old conditional returned
    # ("brightfield", ("fluorescence", "other")) for dark images, causing every
    # unnamed fluorescence channel to be processed as brightfield.
    if mean_val > 80:
        return "brightfield", None
    return "fluorescence", "other"


def _fluorescence_occupancy(image):
    """Return a comparable estimate of how much of a channel contains signal."""
    if image is None or image.size == 0:
        return 0.0
    values = image.astype(np.float64)
    background = float(np.percentile(values, 50))
    noise = float(np.percentile(np.abs(values - background), 75))
    threshold = background + max(5.0, noise * 3.0)
    return float(np.mean(values > threshold))


def _nuclear_peak_count(image):
    """Estimate how many nucleus-like peaks a generic channel contains.

    Coverage alone can confuse a high-death PI field with a dim Hoechst field.
    Counting robust peaks preserves the key distinction: the all-nuclei channel
    contains substantially more nucleus-like objects than the dead subset.
    """
    if image is None or image.size == 0:
        return 0
    import cv2
    from skimage.feature import peak_local_max

    values = image.astype(np.float32)
    background = float(np.median(values))
    mad = max(float(np.median(np.abs(values - background))), 0.5)
    smoothed = cv2.GaussianBlur(values, (0, 0), 4.0)
    threshold = max(5.0, background + 2.5 * mad)
    return len(peak_local_max(
        smoothed, min_distance=6, threshold_abs=threshold,
        exclude_border=False))
