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

# Nuclear stains label the population used for total-cell counting.  PI/red
# viability stains label the dead subset.  DAPI/Hoechst must not be treated as
# a dead stain: in the calibrated J774 data Hoechst is the manual-counting
# reference for every cell.
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

    # When two fluorescence channels exist, the dense nuclear channel is a
    # substantially more reliable total-cell reference than transmitted light.
    # The sparse channel is the viability/dead stain.  Brightfield remains the
    # fallback for acquisitions without a total-cell fluorescence channel.
    brightfield = [r for r in results if r.channel_type == "brightfield"]
    fluorescence = [r for r in results if r.channel_type == "fluorescence"]
    if fluorescence:
        for r in fluorescence:
            if r.dye in TOTAL_DYES:
                r.role = "total"
            if r.dye in DEAD_DYES:
                r.role = "dead"

        if not any(r.role == "total" for r in fluorescence) and len(fluorescence) >= 2:
            candidates = [r for r in fluorescence if r.role != "dead"]
            if candidates:
                max(
                    candidates,
                    key=lambda r: _nuclear_peak_count(image_samples.get(r.index)),
                ).role = "total"

        if not any(r.role == "dead" for r in fluorescence):
            candidates = [r for r in fluorescence if r.role != "total"]
            if candidates:
                min(
                    candidates,
                    key=lambda r: _fluorescence_occupancy(image_samples.get(r.index)),
                ).role = "dead"

    if not any(r.role == "total" for r in results) and brightfield:
        brightfield[0].role = "total"

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
    if mean_val > 80 and median_val > 65 and p90 > 90:
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
