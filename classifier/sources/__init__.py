"""Supplemental imagery sources for the vision classifier.

NAIP (60 cm) is too coarse to see rooftop antennas and Nearmap is paid. These
cheaper sources add extra labelled views:

* ``mapillary`` - street-level photos (``MAPILLARY_ACCESS_TOKEN``).

Enable with ``SUPPLEMENTAL_IMAGERY=mapillary`` (comma list, order = priority)
and call ``fetch_supplemental_views`` inside ``source_meter()`` per site.
"""

from classifier.sources.base import (
    SUPPORTED_SOURCES,
    SourceMeter,
    SupplementalView,
    source_meter,
)
from classifier.sources.registry import enabled_sources, fetch_supplemental_views

__all__ = [
    "SUPPORTED_SOURCES",
    "SourceMeter",
    "SupplementalView",
    "enabled_sources",
    "fetch_supplemental_views",
    "source_meter",
]
