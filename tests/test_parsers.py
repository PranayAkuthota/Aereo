import io
import stat
import zipfile
from dataclasses import replace

import pytest
from conftest import LINE, POINT, POLYGON, kml, zip_parts, zip_shapefile
from pyproj import CRS, Geod
from shapely.geometry import MultiPolygon, Polygon

from app.config import Settings
from app.errors import APIError
from app.geospatial.measurements import measure
from app.geospatial.parser import Feature, parse


def shapefile_parts():
    with zipfile.ZipFile(io.BytesIO(zip_shapefile())) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.mark.parametrize("name", ["../evil.shp", "/evil.shp", "folder\\evil.shp", "C:evil.shp"])
def test_archive_paths_rejected(name):
    with pytest.raises(APIError, match="Unsafe"):
        parse(zip_parts({name: b"x"}), ".zip", Settings())


def test_symlink_rejected():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        entry = zipfile.ZipInfo("sample.shp")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, "elsewhere")
    with pytest.raises(APIError, match="Unsafe"):
        parse(data.getvalue(), ".zip", Settings())


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing_dbf", "INVALID_SHAPEFILE"),
        ("invalid_crs", "INVALID_CRS"),
        ("malformed_components", "INVALID_SHAPEFILE"),
        ("multiple_datasets", "INVALID_SHAPEFILE"),
    ],
)
def test_shapefile_components(change, code):
    parts = shapefile_parts()
    if change == "missing_dbf":
        parts.pop("sample.dbf")
    elif change == "invalid_crs":
        parts["sample.prj"] = b"not a CRS"
    elif change == "malformed_components":
        parts["sample.shp"] = b"bad geometry header"
    else:
        parts["other.shp"] = parts["sample.shp"]
    with pytest.raises(APIError) as exc:
        parse(zip_parts(parts), ".zip", Settings())
    assert exc.value.code == code


def test_case_insensitive_components_in_directory():
    parts = {"nested/" + name.upper(): value for name, value in shapefile_parts().items()}
    assert parse(zip_parts(parts), ".zip", Settings()).crs == CRS.from_epsg(32643)


def test_expanded_size_and_compression_ratio():
    with pytest.raises(APIError) as exc:
        parse(zip_shapefile(), ".zip", Settings(max_expanded_bytes=100))
    assert exc.value.status_code == 413
    with pytest.raises(APIError, match="compression ratio"):
        parse(zip_parts({"zeroes": b"0" * 100000}), ".zip", Settings())


def test_duplicate_archive_names():
    parts = shapefile_parts()
    parts["SAMPLE.SHP"] = parts["sample.shp"]
    with pytest.raises(APIError, match="Duplicate"):
        parse(zip_parts(parts), ".zip", Settings())


def test_empty_kml_and_feature_limit():
    with pytest.raises(APIError) as exc:
        parse(b'<kml xmlns="http://www.opengis.net/kml/2.2"/>', ".kml", Settings())
    assert exc.value.code == "EMPTY_DATASET"
    with pytest.raises(APIError) as exc:
        parse(kml(POINT, f"<Placemark>{POINT}</Placemark>"), ".kml", Settings(max_features=1))
    assert exc.value.status_code == 413


def test_kml_extended_properties_and_altitude():
    data = kml(
        POINT.replace("78,17,0", "78,17,100")
        + (
            '<ExtendedData><Data name="owner"><value>test</value></Data>'
            '<SchemaData><SimpleData name="count">2</SimpleData></SchemaData></ExtendedData>'
        )
    )
    dataset = parse(data, ".kml", Settings())
    feature = dataset.features[0]
    assert list(feature.geometry.coords) == [(78.0, 17.0)]
    assert feature.properties["owner"] == "test"
    assert feature.properties["count"] == "2"


@pytest.mark.parametrize(
    "geometry",
    [
        "<Model/>",
        f"<MultiGeometry>{POINT}{POLYGON}</MultiGeometry>",
    ],
)
def test_unsupported_kml_geometry(geometry):
    dataset = parse(kml(geometry), ".kml", Settings())
    assert measure(dataset.features[0], dataset.crs).status == "UNSUPPORTED"


def test_homogeneous_kml_multi_polygon():
    second = POLYGON.replace("78.001", "78.003").replace("78,", "78.002,")
    dataset = parse(kml(f"<MultiGeometry>{POLYGON}{second}</MultiGeometry>"), ".kml", Settings())
    result = measure(dataset.features[0], dataset.crs)
    assert result.geometry_type == "MultiPolygon"
    assert result.status == "MEASURED"
    reference = 0.0
    for west in (78, 78.002):
        polygon = Polygon([(west, 17), (west + 0.001, 17), (west + 0.001, 17.001), (west, 17.001)])
        area, _ = Geod(ellps="WGS84").geometry_area_perimeter(polygon)
        reference += abs(area)
    assert result.area == pytest.approx(reference, rel=0.003)


def test_projected_multipolygon_sums_parts():
    first = Polygon([(500000, 1900000), (500010, 1900000), (500010, 1900010), (500000, 1900010)])
    second = Polygon([(500020, 1900000), (500030, 1900000), (500030, 1900010), (500020, 1900010)])
    feature = Feature(0, "multi", geometry=MultiPolygon([first, second]))
    assert measure(feature, CRS.from_epsg(32643)).area == pytest.approx(200)


def test_kml_bad_rings_and_out_of_range_feature_errors():
    for geometry in (
        POLYGON.replace(" 78,17</", "</"),
        POINT.replace("78,17", "78,100"),
        POINT.replace("78,17", "nan,17"),
    ):
        feature = parse(kml(geometry), ".kml", Settings()).features[0]
        assert feature.error is not None


def test_kml_nesting_limit():
    content = '<kml xmlns="http://www.opengis.net/kml/2.2">' + "<Folder>" * 65
    content += "</Folder>" * 65 + "</kml>"
    with pytest.raises(APIError, match="nesting"):
        parse(content.encode(), ".kml", Settings())


def test_shapefile_coordinate_limit():
    with pytest.raises(APIError) as exc:
        parse(zip_shapefile(), ".zip", replace(Settings(), max_coordinates=1))
    assert exc.value.code == "TOO_MANY_COORDINATES"


@pytest.mark.parametrize("kind", ["Point", "LineString", "Polygon", "MultiGeometry"])
def test_foreign_geometry_namespaces_are_not_interpreted_as_kml(kind):
    contents = {
        "Point": "<coordinates>78,17</coordinates>",
        "LineString": "<coordinates>78,17 78.001,17</coordinates>",
        "Polygon": POLYGON[len("<Polygon>") : -len("</Polygon>")],
        "MultiGeometry": POINT,
    }
    geometry = (
        f'<foreign:{kind} xmlns:foreign="https://invalid.example">{contents[kind]}</foreign:{kind}>'
    )
    dataset = parse(kml(geometry), ".kml", Settings())
    value = measure(dataset.features[0], dataset.crs)
    assert value.status == "ERROR"
    assert value.error == "Unsupported geometry namespace."
    assert value.area is None and value.length is None


def test_xml_element_limit_is_enforced_during_parsing():
    content = '<kml xmlns="http://www.opengis.net/kml/2.2">' + "<unused/>" * 10000 + "</kml>"
    with pytest.raises(APIError) as error:
        parse(content.encode(), ".kml", Settings(max_xml_elements=20))
    assert error.value.code == "TOO_MANY_XML_ELEMENTS"
    assert error.value.status_code == 413


def test_long_coordinate_token_does_not_echo_input_or_materialize_error_payload():
    geometry = "<Point><coordinates>" + "0" * 10000 + ",17</coordinates></Point>"
    value = measure(parse(kml(geometry), ".kml", Settings()).features[0], CRS.from_epsg(4326))
    assert value.status == "ERROR"
    assert value.error == "A coordinate tuple exceeds 128 characters."


@pytest.mark.parametrize("geometry", [LINE, POLYGON])
def test_nested_homogeneous_multi_geometry_matches_flat_measurements(geometry):
    flat = parse(kml(f"<MultiGeometry>{geometry}</MultiGeometry>"), ".kml", Settings())
    nested = parse(
        kml(f"<MultiGeometry><MultiGeometry>{geometry}</MultiGeometry></MultiGeometry>"),
        ".kml",
        Settings(),
    )
    a, b = measure(flat.features[0], flat.crs), measure(nested.features[0], nested.crs)
    assert b.status == "MEASURED"
    assert a.geometry_type == b.geometry_type
    assert a.geometry == b.geometry
    assert a.area == b.area and a.length == b.length
