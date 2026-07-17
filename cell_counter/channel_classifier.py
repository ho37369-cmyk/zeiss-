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


BRIGHTFIELD_KEYWORDS = [
    "brightfield", "bright field", "bf", "phase", "ph",
    "dic", "differential", "contrast", "transmitted",
    "tl", "trans", "transmission", "white light",
]

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

    if mean_val > 100 and std_val > 25:
        return "brightfield", None
    if low > 0.5 and high > 0.05:
        return "fluorescence", "other"

    return "brightfield", None if mean_val > 80 else ("fluorescence", "other")
