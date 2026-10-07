"""Supplemental imagery sources for the vision classifier.

NAIP (60 cm) is too coarse to see rooftop antennas and Nearmap is paid. These
cheaper sources add extra labelled views:

* ``state_ortho`` - state/county orthoimagery services (15-30 cm), config
  file at ``STATE_ORTHO_SOURCES``.
* ``mapillary`` - street-level photos (``MAPILLARY_ACCESS_TOKEN``).
* ``streetview`` - Google Street View Static API (off unless
  ``GOOGLE_STREETVIEW_ENABLED=1``; licensing review pending).

Enable with ``SUPPLEMENTAL_IMAGERY=state_ortho,mapillary`` (order = priority)
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
