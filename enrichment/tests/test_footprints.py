from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from enrichment import footprints as fp

PIN = (40.0, -74.0)  # imprecise (1 decimal) pin
ADDR = (40.001, -74.0)  # ~111 m north
PRECISE_PIN = (40.123456, -74.123456)


def _bld(bid, *, inside, dist, area=800.0, centroid=(40.0002, -74.0002), near=(40.0001, -74.0001)):
    return {"building_id": bid, "centroid_lat": centroid[0], "centroid_lon": centroid[1],
            "area_m2": area, "height_m": 20.0, "num_floors": 5, "building_class": "commercial",
            "inside": inside, "distance_m": dist, "near_lat": near[0], "near_lon": near[1]}


class PinCheckTests(unittest.TestCase):
    def run_check(self, by_point, address=None):
        def lookup(lat, lon):
            return by_point.get((lat, lon))
        with patch.dict(os.environ, {"FOOTPRINT_SNAP_MAX_M": "60", "FOOTPRINT_ADDRESS_MAX_M": "150",
                                     "FOOTPRINT_BIG_M2": "10000", "FOOTPRINT_MAX_SHIFT_M": "30"}):
            return fp.pin_check(*PIN, *(address or (None, None)), lookup=lookup)

    def test_inside_keeps_pin(self):
        out = self.run_check({PIN: _bld("b1", inside=True, dist=0)})
        self.assertEqual((out.status, out.anchor_lat, out.anchor_lon), ("inside", *PIN))

    def test_address_building_wins_over_nearby(self):
        # Address building's centroid is ~122 m from the pin, its nearest edge ~22 m: snap to the edge.
        out = self.run_check({PIN: _bld("near", inside=False, dist=20),
                              ADDR: _bld("addr", inside=True, dist=0, centroid=(40.0011, -74.0),
                                         near=(40.0002, -74.0))}, ADDR)
        self.assertEqual((out.status, out.source, out.building_id), ("snapped", "address", "addr"))
        self.assertEqual((out.anchor_lat, out.anchor_lon), (40.0002, -74.0))

    def test_address_snap_respects_shift_cap_from_pin(self):
        out = self.run_check({PIN: _bld("near", inside=False, dist=20),
                              ADDR: _bld("addr", inside=True, dist=0, centroid=(40.0011, -74.0),
                                         near=(40.0009, -74.0))}, ADDR)
        self.assertEqual((out.source, out.building_id), ("pin", "near"))

    def test_precise_pin_ignores_address(self):
        near = _bld("near", inside=False, dist=20, centroid=(40.1236, -74.1236), near=(40.12350, -74.12350))
        addr = (40.1245, -74.1235)

        def lookup(lat, lon):
            return {PRECISE_PIN: near, addr: _bld("addr", inside=True, dist=0)}.get((lat, lon))
        with patch.dict(os.environ, {"FOOTPRINT_MAX_SHIFT_M": "30"}):
            out = fp.pin_check(*PRECISE_PIN, *addr, lookup=lookup)
        self.assertEqual((out.source, out.building_id), ("pin", "near"))

    def test_snaps_to_nearby_centroid_or_big_building_edge(self):
        small = self.run_check({PIN: _bld("s", inside=False, dist=35)})
        self.assertEqual((small.status, small.anchor_lat), ("snapped", 40.0002))
        big = self.run_check({PIN: _bld("b", inside=False, dist=35, area=50_000)})
        self.assertEqual((big.anchor_lat, big.anchor_lon), (40.0001, -74.0001))

    def test_shift_cap_falls_back_to_edge_then_keeps_pin(self):
        # centroid ~55 m away, edge ~15 m away -> edge
        edge = self.run_check({PIN: _bld("e", inside=False, dist=15, centroid=(40.0005, -74.0),
                                         near=(40.000135, -74.0))})
        self.assertEqual((edge.status, edge.anchor_lat), ("snapped", 40.000135))
        # edge itself ~45 m away -> keep the pin
        far = self.run_check({PIN: _bld("f", inside=False, dist=45, centroid=(40.0008, -74.0),
                                        near=(40.0004, -74.0))})
        self.assertEqual((far.status, far.anchor_lat), ("off_building", PIN[0]))

    def test_off_building_carries_the_building_point(self):
        far = self.run_check({PIN: _bld("f", inside=False, dist=45, centroid=(40.0008, -74.0),
                                        near=(40.0004, -74.0))})
        self.assertEqual(far.status, "off_building")
        self.assertEqual((far.building_lat, far.building_lon), (40.0008, -74.0))

    def test_no_building_within_snap_radius(self):
        out = self.run_check({PIN: _bld("far", inside=False, dist=140)})
        self.assertEqual((out.status, out.anchor_lat), ("no_building", None))
        self.assertEqual(self.run_check({}).status, "no_building")

    def test_sql_unavailable_changes_nothing(self):
        def broken(lat, lon):
            raise LookupError("down")
        out = fp.pin_check(*PIN, lookup=broken)
        self.assertEqual(out.status, "unavailable")
        self.assertEqual(out.as_row()["footprint_status"], "unavailable")


class LoaderHelpersTests(unittest.TestCase):
    def test_box_near_site(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        import load_overture_buildings as lob

        grid = lob.site_grid([PIN], 0.01)
        self.assertTrue(lob.box_near_site(grid, 0.01, -74.0005, 40.0005, -74.0001, 40.0009))
        self.assertFalse(lob.box_near_site(grid, 0.01, -73.99, 40.01, -73.98, 40.02))


if __name__ == "__main__":
    unittest.main()
