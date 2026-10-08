"""Pin check against Overture building footprints, before any imagery.

``FOOTPRINT_PIN_CHECK=1`` (default off) looks the Salesforce pin up in
dbo.OvertureBuilding (scripts/load_overture_buildings.py):

* ``inside``       the pin is on a building: classify there.
* ``snapped``      the pin is off-building: move the imagery anchor to the
                   building the Census address sits in (only for imprecise
                   pins, <= 3 decimals, within FOOTPRINT_ADDRESS_MAX_M,
                   default 150 m), else to the
                   nearest building within FOOTPRINT_SNAP_MAX_M (default
                   60 m). The anchor is the building centroid, or its nearest
                   edge when the centroid is more than FOOTPRINT_MAX_SHIFT_M
                   (default 30 m) away or the building is very large
                   (> FOOTPRINT_BIG_M2, default 10,000 m2).
* ``off_building`` a building is near but even its edge is more than
                   FOOTPRINT_MAX_SHIFT_M away: keep the pin.
* ``no_building``  nothing within FOOTPRINT_SNAP_MAX_M. Recorded only: towers
                   have no footprint, and on the pool audit most of these
                   pins were real monopoles or stealth sites, so this must
                   not skip Nearmap.
* ``unavailable``  SQL or the table is unavailable: nothing changes.

The shift cap keeps a tower at the pin inside a ~100 m Nearmap chip even
when the pin is snapped to a neighbouring building; the pin stays the
second-pack point. Sites with an FCC/TowerSource match skip the check.
Data: Overture Maps Foundation, ODbL.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from envutil import env_flag, env_float

logger = logging.getLogger(__name__)

TABLE = "dbo.OvertureBuilding"
M_PER_DEG = 111_320.0
WINDOW_DEG = 0.006  # centroid search window (~670 m) so large buildings are found

STATUS_INSIDE = "inside"
STATUS_SNAPPED = "snapped"
STATUS_OFF_BUILDING = "off_building"
STATUS_NO_BUILDING = "no_building"
STATUS_UNAVAILABLE = "unavailable"

_NEAREST_SQL = f"""
WITH p AS (SELECT geometry::Point(?, ?, 4326) AS g)
SELECT TOP 1 b.building_id, b.centroid_lat, b.centroid_lon, b.area_m2, b.height_m,
       b.num_floors, b.building_class,
       b.shape.STContains(p.g) AS inside,
       b.shape.ShortestLineTo(p.g).STStartPoint().STY AS near_lat,
       b.shape.ShortestLineTo(p.g).STStartPoint().STX AS near_lon,
       b.shape.STDistance(p.g) AS dist_deg
FROM {TABLE} b CROSS JOIN p
WHERE b.centroid_lat BETWEEN ? AND ? AND b.centroid_lon BETWEEN ? AND ?
ORDER BY inside DESC, dist_deg ASC
"""


@dataclass
class PinCheck:
    status: str
    anchor_lat: float | None = None
    anchor_lon: float | None = None
    source: str = ""  # pin | address
    building_id: str = ""
    distance_m: float | None = None
    area_m2: float | None = None
    height_m: float | None = None
    num_floors: int | None = None
    building_class: str = ""

    def as_row(self) -> dict[str, Any]:
        def num(value: float | None) -> Any:
            return "" if value is None else round(float(value), 1)

        return {
            "footprint_status": self.status,
            "footprint_source": self.source,
            "footprint_building_id": self.building_id,
            "footprint_distance_m": num(self.distance_m),
            "footprint_area_m2": num(self.area_m2),
            "footprint_height_m": num(self.height_m),
            "footprint_floors": "" if self.num_floors is None else int(self.num_floors),
        }


def enabled() -> bool:
    return env_flag("FOOTPRINT_PIN_CHECK", False)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


_local = threading.local()
_down_until = [0.0]
_down_lock = threading.Lock()


def _cursor():
    """Per-thread SQL cursor; after a failure, stay off for 5 minutes."""
    with _down_lock:
        if time.time() < _down_until[0]:
            return None
    cur = getattr(_local, "cursor", None)
    if cur is not None:
        return cur
    try:
        from enrichment.mssql import connect_mssql

        _local.conn = connect_mssql()
        _local.cursor = _local.conn.cursor()
        return _local.cursor
    except Exception as exc:  # noqa: BLE001
        logger.warning("footprint check unavailable: %s", exc)
        with _down_lock:
            _down_until[0] = time.time() + 300
        return None


def _drop_cursor() -> None:
    _local.cursor = None
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def nearest_building(lat: float, lon: float) -> dict[str, Any] | None:
    """Building containing (lat, lon), else the nearest one in the window.

    Returns None when nothing is in the window; raises LookupError when SQL
    is unavailable.
    """
    cur = _cursor()
    if cur is None:
        raise LookupError("footprints unavailable")
    dlon = WINDOW_DEG / max(0.2, math.cos(math.radians(lat)))
    params = (lon, lat, lat - WINDOW_DEG, lat + WINDOW_DEG, lon - dlon, lon + dlon)
    try:
        row = cur.execute(_NEAREST_SQL, params).fetchone()
    except Exception as exc:  # noqa: BLE001
        _drop_cursor()
        raise LookupError(str(exc)) from exc
    if row is None:
        return None
    # ShortestLineTo is NULL when the point is inside the shape.
    near_lat = lat if row.near_lat is None else float(row.near_lat)
    near_lon = lon if row.near_lon is None else float(row.near_lon)
    return {
        "building_id": str(row.building_id),
        "centroid_lat": float(row.centroid_lat),
        "centroid_lon": float(row.centroid_lon),
        "area_m2": None if row.area_m2 is None else float(row.area_m2),
        "height_m": None if row.height_m is None else float(row.height_m),
        "num_floors": row.num_floors,
        "building_class": row.building_class or "",
        "inside": bool(row.inside),
        "distance_m": 0.0 if row.inside else haversine_m(lat, lon, near_lat, near_lon),
        "near_lat": near_lat,
        "near_lon": near_lon,
    }


def _anchor(building: dict[str, Any], from_lat: float, from_lon: float) -> tuple[float, float] | None:
    """Centroid, else nearest edge, within FOOTPRINT_MAX_SHIFT_M of the point; None if neither."""
    max_shift = env_float("FOOTPRINT_MAX_SHIFT_M", 30.0)
    big = env_float("FOOTPRINT_BIG_M2", 10_000.0)
    centroid = (building["centroid_lat"], building["centroid_lon"])
    if (building.get("area_m2") or 0) <= big and haversine_m(from_lat, from_lon, *centroid) <= max_shift:
        return centroid
    edge = (building["near_lat"], building["near_lon"])
    if haversine_m(from_lat, from_lon, *edge) <= max_shift:
        return edge
    return None


def is_imprecise(lat: float, lon: float) -> bool:
    """True when either coordinate has <= 3 decimals (~100 m precision)."""
    def decimals(value: float) -> int:
        text = repr(float(value))
        return len(text.split(".", 1)[1]) if "." in text and "e" not in text else 0
    return min(decimals(lat), decimals(lon)) <= 3


def _check(building: dict[str, Any], status: str, source: str, anchor: tuple[float, float]) -> PinCheck:
    return PinCheck(
        status=status,
        anchor_lat=anchor[0],
        anchor_lon=anchor[1],
        source=source,
        building_id=building["building_id"],
        distance_m=building["distance_m"],
        area_m2=building.get("area_m2"),
        height_m=building.get("height_m"),
        num_floors=building.get("num_floors"),
        building_class=building.get("building_class") or "",
    )


def pin_check(
    pin_lat: float,
    pin_lon: float,
    address_lat: float | None = None,
    address_lon: float | None = None,
    *,
    lookup=nearest_building,
) -> PinCheck:
    """Classify the pin against footprints (see module docstring). Never raises."""
    snap_max = env_float("FOOTPRINT_SNAP_MAX_M", 60.0)
    address_max = env_float("FOOTPRINT_ADDRESS_MAX_M", 150.0)
    try:
        at_pin = lookup(pin_lat, pin_lon)
        if at_pin is not None and at_pin["inside"]:
            return _check(at_pin, STATUS_INSIDE, "pin", (pin_lat, pin_lon))
        if address_lat is not None and address_lon is not None and is_imprecise(pin_lat, pin_lon):
            if haversine_m(pin_lat, pin_lon, address_lat, address_lon) <= address_max:
                at_address = lookup(address_lat, address_lon)
                if at_address is not None and at_address["inside"]:
                    anchor = _anchor(at_address, address_lat, address_lon) or (address_lat, address_lon)
                    return _check(at_address, STATUS_SNAPPED, "address", anchor)
        if at_pin is not None and at_pin["distance_m"] <= snap_max:
            anchor = _anchor(at_pin, pin_lat, pin_lon)
            if anchor is None:
                return _check(at_pin, STATUS_OFF_BUILDING, "pin", (pin_lat, pin_lon))
            return _check(at_pin, STATUS_SNAPPED, "pin", anchor)
    except LookupError:
        return PinCheck(status=STATUS_UNAVAILABLE)
    except Exception:  # noqa: BLE001 - a footprint bug must never stop a classification
        logger.exception("footprint pin check failed")
        return PinCheck(status=STATUS_UNAVAILABLE)
    check = PinCheck(status=STATUS_NO_BUILDING, source="pin")
    if at_pin is not None:
        check.building_id = at_pin["building_id"]
        check.distance_m = at_pin["distance_m"]
    return check
