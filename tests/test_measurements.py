import pytest
from pyproj import CRS, Geod, Transformer
from shapely.geometry import GeometryCollection, LineString, MultiLineString, Point, Polygon
from shapely.ops import transform

from app.geospatial.measurements import measure
from app.geospatial.parser import Feature


def result(geometry, epsg=4326):
    return measure(Feature(0, "test", geometry=geometry), CRS.from_epsg(epsg))


def test_geographic_line_compared_with_geodesic_reference():
    line = LineString([(78, 17), (78.001, 17)])
    measurement = result(line)
    reference = Geod(ellps="WGS84").geometry_length(line)
    assert measurement.status == "MEASURED"
    assert measurement.length == pytest.approx(reference, rel=0.002)
    assert measurement.measurement_crs == "EPSG:32644"
    assert measurement.length_unit == "m"


def test_geographic_area_compared_with_geodesic_reference():
    polygon = Polygon([(78, 17), (78.001, 17), (78.001, 17.001), (78, 17.001)])
    measurement = result(polygon)
    reference, _ = Geod(ellps="WGS84").geometry_area_perimeter(polygon)
    assert measurement.area == pytest.approx(abs(reference), rel=0.003)
    assert measurement.area_unit == "m²"


def test_projected_square_with_hole():
    polygon = Polygon(
        [(500000, 1900000), (500100, 1900000), (500100, 1900100), (500000, 1900100)],
        [[(500010, 1900010), (500030, 1900010), (500030, 1900030), (500010, 1900030)]],
    )
    measurement = result(polygon, 32643)
    assert measurement.area == pytest.approx(9600)
    assert measurement.measurement_crs == "EPSG:32643"


def test_web_mercator_is_not_used_for_ground_length():
    geographic = LineString([(78, 60), (78.001, 60)])
    mercator = transform(Transformer.from_crs(4326, 3857, always_xy=True).transform, geographic)
    measurement = result(mercator, 3857)
    reference = Geod(ellps="WGS84").geometry_length(geographic)
    assert measurement.length == pytest.approx(reference, rel=0.002)
    assert measurement.length < mercator.length * 0.6


def test_non_wgs84_geographic_crs():
    line = LineString([(-75, 40), (-74.999, 40)])
    measurement = result(line, 4269)
    assert measurement.status == "MEASURED"
    assert measurement.length == pytest.approx(Geod(ellps="GRS80").geometry_length(line), rel=0.002)
    assert measurement.source_crs == "EPSG:4269"


def test_southern_hemisphere_and_polar_projection():
    south = result(LineString([(18, -34), (18.001, -34)]))
    assert south.measurement_crs == "EPSG:32734"
    polar = result(LineString([(0, 85), (0.01, 85)]))
    assert polar.measurement_crs == "EPSG:32661"
    assert polar.length == pytest.approx(Geod(ellps="WGS84").inv(0, 85, 0.01, 85)[2], rel=0.01)


@pytest.mark.parametrize(
    "geometry,status",
    [
        (Point(78, 17), "NOT_APPLICABLE"),
        (Point(), "ERROR"),
        (Point(float("nan"), 17), "ERROR"),
        (Point(78, 100), "ERROR"),
        (Polygon([(78, 17), (78.001, 17.001), (78.001, 17), (78, 17.001)]), "ERROR"),
        (GeometryCollection([Point(78, 17), LineString([(78, 17), (78.001, 17)])]), "UNSUPPORTED"),
        (LineString([(10, 10), (30, 10)]), "ERROR"),
        (LineString([(179, 10), (-179, 10)]), "ERROR"),
    ],
)
def test_feature_edge_cases(geometry, status):
    measurement = result(geometry)
    assert measurement.status == status
    assert measurement.area is None
    assert measurement.length is None


def test_multiline_measures_all_parts():
    geometry = MultiLineString(
        [[(500000, 1900000), (500030, 1900040)], [(500010, 1900000), (500010, 1900020)]]
    )
    assert result(geometry, 32643).length == pytest.approx(70)


def test_projected_feet_are_converted_to_metric():
    geographic = LineString([(-73.99, 40.75), (-73.989, 40.75)])
    feet = transform(Transformer.from_crs(4326, 2263, always_xy=True).transform, geographic)
    measurement = result(feet, 2263)
    reference = Geod(ellps="WGS84").geometry_length(geographic)
    assert measurement.status == "MEASURED"
    assert measurement.length == pytest.approx(reference, rel=0.002)
    assert measurement.length_unit == "m"
    assert measurement.length < feet.length * 0.4


def test_geocentric_crs_is_reported_as_feature_error():
    measurement = result(Point(1, 2), 4978)
    assert measurement.status == "ERROR"
    assert "horizontal" in measurement.error


def test_widely_separated_multipoints_do_not_require_a_measurement_projection():
    from shapely.geometry import MultiPoint

    value = result(MultiPoint([(10, 10), (30, 10)]))
    assert value.status == "NOT_APPLICABLE"
    assert value.measurement_crs is None
    assert value.area is None and value.length is None


def test_mixed_xy_xyz_geometry_is_normalized_to_finite_xy():
    from shapely.geometry import MultiPoint

    value = result(MultiPoint([Point(78, 17, 100), Point(78.001, 17)]))
    assert value.status == "NOT_APPLICABLE"
    assert value.geometry["coordinates"] == ((78.0, 17.0), (78.001, 17.0))


def test_xyz_line_is_measured_horizontally_and_returned_as_xy():
    value = result(LineString([(500000, 1900000, 10), (500030, 1900040, 1000)]), 32643)
    assert value.length == pytest.approx(50)
    assert value.geometry["coordinates"] == ((500000.0, 1900000.0), (500030.0, 1900040.0))
