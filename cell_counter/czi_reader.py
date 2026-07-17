"""
CZI file reader using aicspylibczi.
Extracts channel images and metadata from Zeiss .czi files.
"""

import numpy as np
from pathlib import Path


def read_czi(filepath):
    """Read a CZI file and return scene-channel data.

    Returns:
        dict: {
            "filepath": str,
            "filename": str,
            "scenes": {
                scene_label: {
                    "channels": [
                        {"index": int, "name": str, "image": np.ndarray (H, W)},
                    ]
                }
            },
            "channel_metadata": [{index: int, name: str, dye: str or None}]
        }
    """
    import aicspylibczi

    filepath = Path(filepath)
    czi = aicspylibczi.CziFile(str(filepath))

    dims_shape_list = czi.get_dims_shape()

    # Extract channel info from metadata XML
    channel_metadata = _parse_channel_metadata(czi.meta)

    # Determine total channels
    total_channels = _get_channel_count(dims_shape_list)
    if total_channels == 0:
        total_channels = max(len(channel_metadata), 1)

    # Check for multiple scenes
    has_scenes, scene_count = _has_multiple_scenes(dims_shape_list)

    scenes = {}
    if has_scenes:
        for s_idx in range(scene_count):
            scene_label = f"Scene{s_idx}"
            scene_channels = _read_scene_channels(czi, s_idx, total_channels)
            scenes[scene_label] = {"channels": scene_channels}
    else:
        scene_channels = _read_scene_channels(czi, None, total_channels)
        scenes["Scene0"] = {"channels": scene_channels}

    scenes = _assign_channel_names(scenes, channel_metadata)

    return {
        "filepath": str(filepath),
        "filename": filepath.name,
        "scenes": scenes,
        "channel_metadata": channel_metadata,
    }


def _parse_channel_metadata(meta_root):
    """Extract channel names/dyes from CZI metadata XML."""
    channels = []

    # Search with namespace-agnostic approach
    for elem in meta_root.iter():
        # Look for elements with local name 'Channel' that contain Id/Name
        local_tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if local_tag != 'Channel':
            continue

        name = None
        for child in elem:
            child_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if child_local in ('Name', 'DyeName') and child.text:
                name = child.text.strip()
                break
            if child_local == 'Id' and child.text and name is None:
                name = child.text.strip()

        # Try to find index from Id like "Channel:2"
        idx = len(channels)
        for child in elem:
            child_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if child_local == 'Id' and child.text and ':' in child.text:
                try:
                    idx = int(child.text.split(':')[-1]) - 1
                except ValueError:
                    pass
                break

        if name:
            channels.append({"index": idx, "name": name, "dye": name})
        else:
            channels.append({"index": idx, "name": f"Channel {idx}", "dye": None})

    return channels


def _get_channel_count(dims_shape_list):
    """Get the maximum channel count from dims_shape."""
    max_c = 0
    if not dims_shape_list:
        return 0
    for ds in dims_shape_list:
        if "C" in ds:
            c_start, c_end = ds["C"]
            max_c = max(max_c, c_end - c_start)
    return max_c


def _has_multiple_scenes(dims_shape_list):
    """Check if file has multiple scenes and return count."""
    if not dims_shape_list:
        return False, 1

    max_s = 0
    seen = len(dims_shape_list) > 1
    for ds in dims_shape_list:
        if "S" in ds:
            _, s_end = ds["S"]
            max_s = max(max_s, s_end)

    scene_count = max(1, max_s)
    has_scenes = seen or scene_count > 1

    return has_scenes, scene_count


def _read_scene_channels(czi, scene_idx, total_channels):
    """Read all channels for a given scene."""
    channels = []

    for c_idx in range(total_channels):
        kwargs = {"C": c_idx}
        if scene_idx is not None:
            kwargs["S"] = scene_idx

        try:
            img, dims = czi.read_image(**kwargs)
        except Exception:
            continue

        if img is None or img.size == 0:
            continue

        img_2d = _squeeze_to_2d(img, dims)
        if img_2d.dtype != np.uint8:
            img_2d = _normalize_image(img_2d)

        channels.append({
            "index": c_idx,
            "name": f"Channel {c_idx}",
            "image": img_2d,
        })

    return channels


def _squeeze_to_2d(img, dims):
    """Convert multi-dimensional array to 2D (max projection over Z, squeeze rest)."""
    if img.ndim <= 2:
        return img

    # Find Z axis index
    z_axis = None
    if dims:
        for i, (d, _) in enumerate(dims):
            if d == "Z":
                z_axis = i
                break

    if z_axis is not None and img.shape[z_axis] > 1:
        img = np.max(img, axis=z_axis)

    # Squeeze remaining singleton dims
    while img.ndim > 2:
        if img.shape[0] == 1:
            img = img[0]
        elif img.shape[-1] <= 4:  # RGB-like
            img = np.mean(img, axis=-1).astype(img.dtype)
        else:
            img = img[0]

    return img


def _normalize_image(img):
    """Normalize to 0-255 uint8."""
    img = img.astype(np.float64)
    mn, mx = img.min(), img.max()
    if mx - mn < 1e-6:
        return np.zeros_like(img, dtype=np.uint8)
    img = (img - mn) / (mx - mn) * 255.0
    return img.astype(np.uint8)


def _assign_channel_names(scenes, channel_metadata):
    """Fill channel names from metadata or fallback."""
    for s_name, s_data in scenes.items():
        for ch in s_data["channels"]:
            idx = ch["index"]
            matching = [cm for cm in channel_metadata if cm["index"] == idx]
            if matching:
                ch["name"] = matching[0]["name"]
            else:
                ch["name"] = _fallback_name(ch["image"])
    return scenes


def _fallback_name(image):
    """Generate fallback channel name from image stats."""
    mean_val = np.mean(image)
    std_val = np.std(image)
    if mean_val > 80 and std_val > 25:
        return "Brightfield"
    elif mean_val < 10:
        return "Dark"
    elif std_val > 40:
        return "Fluorescence"
    else:
        return "Unknown"
