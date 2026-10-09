"""Pick enabled supplemental sources and fetch their views for one site."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from classifier.sources import base, mapillary, state_ortho
from classifier.sources.base import SUPPORTED_SOURCES, SupplementalView
from envutil import env_str

logger = logging.getLogger(__name__)

_MODULES = {
    "state_ortho": state_ortho,
    "mapillary": mapillary,
}
_DISABLED_VALUES = frozenset({"none", "off", "0", "false", "no"})


def _filter(names: list[str]) -> list[str]:
    """Known, configured, de-duplicated names in the given (priority) order."""
    out: list[str] = []
    for raw in names:
        name = str(raw or "").strip().lower()
        if not name or name in out:
            continue
        if name not in SUPPORTED_SOURCES:
            base.log_once(f"unknown:{name}", logging.WARNING,
                          "Supplemental imagery source %r is unknown (supported: %s); ignored",
                          name, ", ".join(SUPPORTED_SOURCES))
            continue
        ok, reason = _MODULES[name].available()
        if not ok:
            base.log_once(f"unavailable:{name}:{reason}", logging.WARNING,
                          "Supplemental imagery source %r disabled: %s", name, reason)
            continue
        out.append(name)
    return out


def enabled_sources() -> list[str]:
    """Sources from ``SUPPLEMENTAL_IMAGERY`` (comma list, order = priority).

    Unset / ``none`` -> []. Unknown names are ignored and sources missing
    credentials or config are dropped (each logged once per process).
    """
    # Mapillary is on by default (free; dropped when MAPILLARY_ACCESS_TOKEN is unset).
    raw = env_str("SUPPLEMENTAL_IMAGERY", "mapillary")
    if not raw or raw.lower() in _DISABLED_VALUES:
        return []
    return _filter([part for part in raw.split(",")])


def _save_chip(chip_dir: Path, site_id: str, source: str, n: int, view: SupplementalView) -> None:
    stem = base.safe_key(site_id) if site_id else "site"
    path = Path(chip_dir) / f"{stem}_{source}_{n}.jpg"
    try:
        base.atomic_write(path, base.to_jpeg_bytes(view.image))
        view.meta.setdefault("chip_path", str(path))
    except OSError as exc:
        logger.warning("Supplemental chip save failed (%s): %s", path.name, exc)


def fetch_supplemental_views(
    lat: float,
    lon: float,
    *,
    sources: list[str] | None = None,
    site_id: str = "",
    chip_dir: Path | None = None,
    state: str | None = None,
) -> list[SupplementalView]:
    """Views from each enabled source, in priority order. Never raises.

    ``sources`` overrides ``SUPPLEMENTAL_IMAGERY`` (still filtered to known,
    configured sources). Each source fails open: an error is logged (with
    keys redacted) and the remaining sources still run. Each source returns
    at most its own max-views setting. With ``chip_dir`` every view is saved
    as ``{site_id}_{source}_{n}.jpg`` (n from 1).
    """
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return []
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)) or abs(lat_f) > 90 or abs(lon_f) > 180:
        return []
    try:
        names = enabled_sources() if sources is None else _filter(list(sources))
    except Exception as exc:  # defensive: config parsing must not break a run
        logger.warning("Supplemental imagery disabled for %s: %s", site_id or "site", base.redact(exc))
        return []

    out: list[SupplementalView] = []
    for name in names:
        module = _MODULES[name]
        try:
            limit = module.max_views()
            views = module.fetch(lat_f, lon_f, state=state)[: max(0, limit)] if limit > 0 else []
        except Exception as exc:
            logger.warning("Supplemental %s failed for %s: %s: %s", name, site_id or "site",
                           type(exc).__name__, base.redact(exc))
            continue
        for n, view in enumerate(views, start=1):
            if chip_dir is not None:
                _save_chip(chip_dir, site_id, name, n, view)
        base.record_views(name, len(views))
        out.extend(views)
    return out
