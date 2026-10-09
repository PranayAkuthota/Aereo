from uuid import uuid4

import pytest
from conftest import LINE, POINT, POLYGON, kml, zip_parts, zip_shapefile
from fastapi.testclient import TestClient
from pyproj import Geod
from shapely.geometry import LineString

from app.config import Settings
from app.main import create_app


def upload(client, content, filename="sample.kml"):
    return client.post("/api/files/", files={"file": (filename, content)})


def test_kml_upload_metadata_measurements_and_pagination(client):
    extra = f"<Placemark>{LINE}</Placemark><Placemark>{POINT}</Placemark>"
    response = upload(client, kml(POLYGON, extra))
    assert response.status_code == 201, response.text
    info = response.json()
    assert info["feature_count"] == 3
    assert info["measured_count"] == 2
    assert info["skipped_count"] == 1
    assert info["failed_count"] == 0
    assert info["crs"] == "EPSG:4326"
    assert client.get(response.headers["Location"]).json() == info
    page = client.get(response.headers["Location"] + "measurements/?offset=1&limit=1").json()
    assert page["total"] == 3
    assert len(page["features"]) == 1
    assert page["features"][0]["index"] == 1
    assert page["features"][0]["length"] == pytest.approx(
        Geod(ellps="WGS84").geometry_length(LineString([(78, 17), (78.001, 17)])), rel=0.002
    )
    first = client.get(response.headers["Location"] + "measurements/").json()["features"][0]
    assert first["feature_id"] == "first"
    assert first["properties"]["name"] == "synthetic"
    assert first["geometry"]["type"] == "Polygon"
    assert first["area_unit"] == "m²"


@pytest.mark.parametrize("kind,expected", [("polygon", 10000), ("line", 50), ("point", None)])
def test_shapefile_upload(client, kind, expected):
    response = upload(client, zip_shapefile(geometry=kind), "sample.zip")
    assert response.status_code == 201, response.text
    results = client.get(response.headers["Location"] + "measurements/").json()["features"]
    feature = results[0]
    assert feature["source_crs"] == "EPSG:32643"
    assert feature["properties"]["label"] == "synthetic"
    if expected is None:
        assert feature["status"] == "NOT_APPLICABLE"
        assert feature["area"] is None and feature["length"] is None
    else:
        value = feature["area"] if kind == "polygon" else feature["length"]
        assert value == pytest.approx(expected, rel=1e-8)


@pytest.mark.parametrize(
    "filename,content,status,code",
    [
        ("sample.txt", b"hello", 415, "UNSUPPORTED_FILE"),
        ("sample.kml", b"", 400, "EMPTY_FILE"),
        ("sample.kml", b"<broken", 400, "INVALID_KML"),
        ("sample.kml", b"<html/>", 400, "INVALID_KML"),
        ("sample.zip", b"not zip", 400, "INVALID_ZIP"),
        ("sample.zip", zip_parts({"readme.txt": b"hi"}), 400, "INVALID_SHAPEFILE"),
        ("sample.zip", zip_shapefile(crs=None), 400, "MISSING_CRS"),
        ("sample.zip", zip_parts({"../evil.shp": b"x"}), 400, "UNSAFE_ZIP"),
        ("sample.kml", b'<!DOCTYPE kml [<!ENTITY x "bad">]><kml>&x;</kml>', 400, "INVALID_KML"),
    ],
)
def test_bad_upload(client, filename, content, status, code):
    response = upload(client, content, filename)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_one_bad_feature_preserves_success(client):
    bad = "<Placemark><LineString><coordinates>broken</coordinates></LineString></Placemark>"
    response = upload(client, kml(POINT, bad))
    assert response.status_code == 201
    assert response.json()["failed_count"] == 1
    features = client.get(response.headers["Location"] + "measurements/").json()["features"]
    assert features[0]["status"] == "NOT_APPLICABLE"
    assert features[1]["status"] == "ERROR"


def test_null_shapefile_geometry(client):
    response = upload(client, zip_shapefile(geometry="null"), "null.zip")
    assert response.status_code == 201, response.text
    assert response.json()["failed_count"] == 1


def test_unknown_file_and_validation(client):
    for suffix in ("", "measurements/"):
        response = client.get(f"/api/files/{uuid4()}/{suffix}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/api/files/not-a-uuid/").status_code == 422
    assert client.post("/api/files/").status_code == 422
    assert client.get(f"/api/files/{uuid4()}/measurements/?limit=0").status_code == 422


def test_health_and_openapi(client):
    assert client.get("/health/").json() == {"status": "ok"}
    schema = client.get("/openapi.json").json()
    assert "/api/files/{file_id}/measurements/" in schema["paths"]
    assert client.get("/docs").status_code == 200


def test_size_limit_and_coordinate_limit(tmp_path):
    settings = Settings(database_path=tmp_path / "small.db", max_upload_bytes=20)
    with TestClient(create_app(settings)) as client:
        assert upload(client, kml(POINT)).status_code == 413
    settings = Settings(database_path=tmp_path / "coords.db", max_coordinates=1)
    with TestClient(create_app(settings)) as client:
        response = upload(client, kml(LINE))
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "TOO_MANY_COORDINATES"


def test_results_survive_restart(settings):
    with TestClient(create_app(settings)) as first:
        response = upload(
            first, kml(POLYGON, f"<Placemark>{LINE}</Placemark><Placemark>{POINT}</Placemark>")
        )
        location = response.headers["Location"]
        metadata = first.get(location).json()
        results = first.get(location + "measurements/").json()
    with TestClient(create_app(settings)) as second:
        assert second.get(location).json() == metadata
        assert second.get(location + "measurements/").json() == results


def test_chunked_body_limit(tmp_path):
    settings = Settings(database_path=tmp_path / "chunked.db", max_upload_bytes=10)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/files/",
            content=iter([b"x" * 40000, b"y" * 40000]),
            headers={"Content-Type": "multipart/form-data; boundary=BOUNDARY"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


def test_filename_is_only_metadata(client):
    response = upload(client, kml(POINT), "../../survey.kml")
    assert response.status_code == 201
    assert response.json()["filename"] == "survey.kml"
