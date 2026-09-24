"""Labeled image views, normalized boxes, and crops sent to vision models.

A "view" is ``(label, PIL.Image)``. Labels carry the imagery source:
``NAIP top-down``, ``Nearmap top-down``, ``Nearmap oblique (North)``, and
``zoom crop`` / ``cell crop`` variants. Boxes are ``[ymin, xmin, ymax, xmax]``
integers in 0-1000 normalized image space.
"""

from __future__ import annotations

import json
from typing import Any

from PIL import Image

from envutil import env_int

MODEL_IMAGE_MAX_PX = env_int("MODEL_IMAGE_MAX_PX", 768)
MODEL_MAX_OBLIQUES = env_int("MODEL_MAX_OBLIQUES", 2)
# Lite NAIP screen uses a smaller image than Flash confirm.
SCREEN_IMAGE_MAX_PX = env_int("SCREEN_IMAGE_MAX_PX", 768)

# Two-stage zoom: after primary + wide-AOI passes still return other/unclear,
# scout suspicious regions on the best top-down image, magnify them, and
# re-classify. Critical for rural sites where towers are tiny in wide chips.
ZOOM_GRID = 3              # 3x3 grid fallback when scout finds nothing
ZOOM_MAX_CANDIDATES = env_int("ZOOM_MAX_CANDIDATES", 3)
ZOOM_OUTPUT_PX = 1024      # magnified crop size in pixels
ZOOM_MIN_FRAC = 0.10       # minimum crop side as fraction of source image
ZOOM_PAD_FRAC = 0.15       # padding around each candidate box
# Dual-model / cell-recheck crops need more context than zoom scout. 15% pad
# clips panel arrays and Claude then votes false on foliage / tank rim / HVAC.
CELL_CONFIRM_PAD_FRAC = 0.40
# Rooftop gear boxes are intentionally tight (sector panels). Do not reuse
# ZOOM_MIN_FRAC here — that rejects valid antenna boxes as "invalid".
ASSET_BOX_MIN_FRAC = 0.03  # ~30/1000 normalized
ASSET_BOX_MAX_SIDE = 500   # reject whole-roof / whole-scene boxes

NAIP_VIEW_LABEL = "NAIP top-down"
NEARMAP_VERT_LABEL = "Nearmap top-down"
_DIRECTIONS = ("north", "east", "south", "west")

# ------------------------------- view labels --------------------------------


def naip_view_label(chip_m: float | None, base_chip_m: float) -> str:
    """NAIP label; wider-than-primary chips say so for geo matching."""
    if chip_m is not None and chip_m > base_chip_m:
        return f"{NAIP_VIEW_LABEL} (wide {int(chip_m)}m)"
    return NAIP_VIEW_LABEL


def nearmap_view_label(name: str) -> str:
    """Label for a Nearmap view name (``Vert`` / ``North`` / ...)."""
    return NEARMAP_VERT_LABEL if name == "Vert" else f"Nearmap oblique ({name})"


def _is_naip_view(asset_view: str | None) -> bool:
    return bool(asset_view) and str(asset_view).startswith(NAIP_VIEW_LABEL)


def is_oblique_label(label: Any) -> bool:
    """True for a Nearmap oblique label (never NAIP)."""
    view = str(label or "").strip().lower()
    if not view or "naip" in view:
        return False
    return "oblique" in view or any(d in view for d in _DIRECTIONS)


def is_top_down_label(label: Any) -> bool:
    lower = str(label or "").lower()
    return "vert" in lower or "top-down" in lower


def asset_view_is_nearmap_oblique(asset_view: str | None) -> bool:
    return is_oblique_label(asset_view)


def has_obliques(nearmap_views: dict | None) -> bool:
    return any(name != "Vert" for name in (nearmap_views or {}))


def nearmap_tier_for(nearmap_views: dict | None) -> str:
    """``full`` with obliques, ``vert_only`` with only Vert, else ``naip_only``."""
    if has_obliques(nearmap_views):
        return "full"
    return "vert_only" if nearmap_views else "naip_only"


def _oblique_views_only(views: list) -> list:
    """Prefer Nearmap oblique chips for box repair / cell localization."""
    return [(label, img) for label, img in views if is_oblique_label(label)]


def pick_view_for_asset_box(
    res: dict, views: list
) -> tuple[str, Image.Image] | None:
    """Return (label, image) matching asset_view, preferring Nearmap obliques."""
    if not views:
        return None
    target = str(res.get("asset_view") or "").strip().lower()
    if target:
        for label, img in views:
            if target in str(label).strip().lower():
                return label, img
    for label, img in views:
        if is_oblique_label(label):
            return label, img
    for label, img in views:
        if is_top_down_label(label) and "naip" not in str(label).lower():
            return label, img
    return None


def downscale_image(img: Image.Image, max_px: int) -> Image.Image:
    """Return a copy capped at max_px on the long side (original unchanged)."""
    if img is None or max_px <= 0:
        return img
    if max(img.size) <= max_px:
        return img
    copy = img.copy()
    copy.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    return copy


def trim_views_for_model(
    views: list,
    *,
    max_obliques: int | None = None,
    max_px: int | None = None,
) -> list:
    """Keep one top-down + a few obliques (or crops) and downscale for the API."""
    if not views:
        return views
    max_obliques = MODEL_MAX_OBLIQUES if max_obliques is None else max_obliques
    max_px = MODEL_IMAGE_MAX_PX if max_px is None else max_px
    top: list = []
    obliques: list = []
    other: list = []
    for label, img in views:
        lower = str(label).lower()
        if "zoom crop" in lower or "cell crop" in lower:
            other.append((label, img))
        elif is_oblique_label(label):
            obliques.append((label, img))
        elif "naip" in lower or is_top_down_label(label):
            top.append((label, img))
        else:
            other.append((label, img))
    nearmap_vert = [item for item in top if "naip" not in str(item[0]).lower()]
    chosen_top = nearmap_vert[:1] if nearmap_vert else top[:1]
    selected = chosen_top + obliques[: max(0, max_obliques)] + other
    return [(label, downscale_image(img, max_px)) for label, img in selected]


# ---------------------------------- boxes -----------------------------------


def _box_ints(box: Any) -> list[int] | None:
    """Decode (JSON string ok), round, and un-invert a 4-number box."""
    if box is None or box == "":
        return None
    if isinstance(box, str):
        try:
            box = json.loads(box)
        except json.JSONDecodeError:
            return None
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (int(round(float(v))) for v in box[:4])
    except (TypeError, ValueError):
        return None
    if ymin > ymax:
        ymin, ymax = ymax, ymin
    if xmin > xmax:
        xmin, xmax = xmax, xmin
    return [ymin, xmin, ymax, xmax]


def parse_box_2d(box: Any) -> list[int] | None:
    """Strict box parse: in-range, non-empty, any size (geocode / write gates)."""
    parsed = _box_ints(box)
    if parsed is None:
        return None
    ymin, xmin, ymax, xmax = parsed
    if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
        return None
    return parsed


def coerce_asset_box(box) -> list[int] | None:
    """Normalize and validate a rooftop/tower asset_box_2d.

    Fixes inverted corners, clamps to the frame, and rejects empty /
    whole-scene boxes. Uses a smaller minimum than zoom scout so tight
    antenna mounts stay valid.
    """
    parsed = _box_ints(box)
    if parsed is None:
        return None
    ymin, xmin, ymax, xmax = (max(0, min(1000, v)) for v in parsed)
    if ymin >= ymax or xmin >= xmax:
        return None
    min_side = ASSET_BOX_MIN_FRAC * 1000
    if (ymax - ymin) < min_side or (xmax - xmin) < min_side:
        return None
    if (ymax - ymin) > ASSET_BOX_MAX_SIDE or (xmax - xmin) > ASSET_BOX_MAX_SIDE:
        return None
    return [ymin, xmin, ymax, xmax]


def get_valid_asset_box(res: dict) -> list[int] | None:
    return coerce_asset_box(res.get("asset_box_2d"))


def _valid_box(box) -> list[int] | None:
    """Validate a zoom-scout candidate box (larger minimum side)."""
    try:
        ymin, xmin, ymax, xmax = (int(v) for v in box[:4])
    except (TypeError, ValueError):
        return None
    if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
        return None
    if (ymax - ymin) < ZOOM_MIN_FRAC * 1000 or (xmax - xmin) < ZOOM_MIN_FRAC * 1000:
        return None
    return [ymin, xmin, ymax, xmax]


def box_iou(box_a: list[int] | None, box_b: list[int] | None) -> float:
    """Intersection-over-union for [ymin, xmin, ymax, xmax] boxes in 0-1000 space."""
    a = coerce_asset_box(box_a)
    b = coerce_asset_box(box_b)
    if not a or not b:
        return 0.0
    ay0, ax0, ay1, ax1 = a
    by0, bx0, by1, bx1 = b
    iy0, ix0 = max(ay0, by0), max(ax0, bx0)
    iy1, ix1 = min(ay1, by1), min(ax1, bx1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    area_a = max(0, ax1 - ax0) * max(0, ay1 - ay0)
    area_b = max(0, bx1 - bx0) * max(0, by1 - by0)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _grid_boxes(grid: int = ZOOM_GRID) -> list[list[int]]:
    """Return normalized boxes for an NxN grid covering the full image."""
    step = 1000 // grid
    boxes = []
    for row in range(grid):
        for col in range(grid):
            ymin = row * step
            xmin = col * step
            ymax = 1000 if row == grid - 1 else (row + 1) * step
            xmax = 1000 if col == grid - 1 else (col + 1) * step
            boxes.append([ymin, xmin, ymax, xmax])
    return boxes


def _crop_zoom(
    img: Image.Image, box: list[int], *, pad_frac: float | None = None
) -> Image.Image:
    """Magnify a normalized box from a source image to ZOOM_OUTPUT_PX."""
    w, h = img.size
    ymin, xmin, ymax, xmax = box
    pad = ZOOM_PAD_FRAC if pad_frac is None else float(pad_frac)
    pad_y = int((ymax - ymin) * pad)
    pad_x = int((xmax - xmin) * pad)
    ymin = max(0, ymin - pad_y)
    xmin = max(0, xmin - pad_x)
    ymax = min(1000, ymax + pad_y)
    xmax = min(1000, xmax + pad_x)
    left = int(xmin / 1000.0 * w)
    upper = int(ymin / 1000.0 * h)
    right = max(left + 1, int(xmax / 1000.0 * w))
    lower = max(upper + 1, int(ymax / 1000.0 * h))
    crop = img.crop((left, upper, right, lower))
    return crop.resize((ZOOM_OUTPUT_PX, ZOOM_OUTPUT_PX), Image.Resampling.LANCZOS)
