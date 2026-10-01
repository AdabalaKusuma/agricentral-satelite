"""Offline tests for reading farm geometry out of AgriCentral's tables.

The `Aoi` column is PostGIS geometry, so a zone's rings normally come straight from
ST_AsGeoJSON. These cover that parsing and the edges around it. The point-table fallback
("Coordinate") cannot be exercised here because no zone in the dev database has three or
more points - every no-Aoi zone has zero or one.
"""
from __future__ import annotations

from app.db.repo import _bounds_of, _rings_from_geojson

SQUARE = ('{"type":"Polygon","coordinates":[[[76.05,11.996],[76.054,11.996],'
          '[76.054,12.0],[76.05,12.0],[76.05,11.996]]]}')


def test_polygon_rings_are_read_in_lon_lat_order():
    """GeoJSON is [lon, lat] and so is the grid code - no swap anywhere in between."""
    rings = _rings_from_geojson(SQUARE)
    assert rings is not None and len(rings) == 1
    assert rings[0][0] == [76.05, 11.996]
    assert rings[0][0] == rings[0][-1], "the ring stays closed"


def test_polygon_with_a_hole_keeps_both_rings():
    """rings[0] is the outer boundary; the rest are holes the zone test subtracts."""
    raw = ('{"type":"Polygon","coordinates":['
           '[[0,0],[4,0],[4,4],[0,4],[0,0]],'
           '[[1,1],[2,1],[2,2],[1,2],[1,1]]]}')
    rings = _rings_from_geojson(raw)
    assert len(rings) == 2
    assert len(rings[0]) == 5 and len(rings[1]) == 5


def test_multipolygon_takes_the_largest_part():
    """A zone is one field in practice. If it was drawn as several, the biggest is the one
    meant; silently using the first would depend on insertion order."""
    raw = ('{"type":"MultiPolygon","coordinates":['
           '[[[0,0],[1,0],[1,1],[0,1],[0,0]]],'
           '[[[5,5],[6,5],[6,6],[5,6],[5.5,5.5],[5,5]]]]}')
    rings = _rings_from_geojson(raw)
    assert rings[0][0] == [5.0, 5.0], "the 6-vertex part, not the 5-vertex one"


def test_unusable_geometry_returns_none_rather_than_raising():
    """A null Aoi, a point, a line or malformed text must all fall through to the point
    table, not stop the farm."""
    assert _rings_from_geojson(None) is None
    assert _rings_from_geojson("") is None
    assert _rings_from_geojson("not json") is None
    assert _rings_from_geojson('{"type":"Point","coordinates":[76.05,11.99]}') is None
    assert _rings_from_geojson('{"type":"LineString","coordinates":[[0,0],[1,1]]}') is None


def test_a_ring_with_too_few_points_is_rejected():
    """Four coordinates is the minimum for a closed triangle. Below that it is not an area,
    and the dev database is full of zones holding a single stray point."""
    assert _rings_from_geojson('{"type":"Polygon","coordinates":[[[0,0],[1,1],[0,0]]]}') is None


def test_bounds_span_every_zone():
    zones = [
        {"id": "a", "rings": [[[76.05, 11.996], [76.054, 11.996], [76.054, 12.0], [76.05, 12.0], [76.05, 11.996]]]},
        {"id": "b", "rings": [[[76.06, 11.99], [76.07, 11.99], [76.07, 12.01], [76.06, 12.01], [76.06, 11.99]]]},
    ]
    bounds, center = _bounds_of(zones)
    assert bounds == {"west": 76.05, "east": 76.07, "south": 11.99, "north": 12.01}
    assert center["lon"] == 76.06
    assert center["lat"] == 12.0
