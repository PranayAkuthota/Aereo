import asyncio
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import POINT, kml
from fastapi.testclient import TestClient
from pyproj import Geod
from shapely.geometry import LineString
from test_measurements import result

from app.config import Settings
from app.errors import APIError
from app.main import create_app
from app.repository import Repository
from app.schemas import FeatureMeasurement, FileInfo
from app.service import process_upload


def test_result_budget_exact_boundary_and_atomic_rejection(tmp_path):
    repo = Repository(tmp_path / "budget.db")
    repo.initialize()
    info = process_upload(kml(POINT), "point.kml", Settings(), repo)
    feature = repo.measurements(str(info.id), 0, 1)[0]
    size = len(feature.model_dump_json().encode("utf-8"))
    accepted = process_upload(kml(POINT), "point.kml", Settings(max_result_bytes=size), repo)
    assert repo.get(str(accepted.id)).feature_count == 1
    with TestClient(create_app(Settings(database_path=repo.path, max_result_bytes=size - 1))) as c:
        response = c.post("/api/files/", files={"file": ("point.kml", kml(POINT))})
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "RESULT_TOO_LARGE"
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM features").fetchone()[0] == 2


def test_result_budget_counts_utf8_and_all_features(tmp_path):
    repo = Repository(tmp_path / "aggregate.db")
    repo.initialize()
    data = kml(POINT).replace(b"synthetic", "測量".encode())
    info = process_upload(data, "point.kml", Settings(), repo)
    feature = repo.measurements(str(info.id), 0, 1)[0]
    size = len(feature.model_dump_json().encode("utf-8"))
    assert size > len(feature.model_dump_json())
    two = data.replace(b"</Document>", f"<Placemark>{POINT}</Placemark></Document>".encode())
    with pytest.raises(APIError) as failure:
        process_upload(two, "points.kml", Settings(max_result_bytes=size), repo)
    assert failure.value.code == "RESULT_TOO_LARGE"
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1


def test_interrupt_mid_transaction_rolls_back_metadata_and_features(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "interruption.db")
    repo.initialize()
    now = datetime.now(UTC)
    info = FileInfo(
        id=uuid4(),
        filename="interrupt.kml",
        file_type="kml",
        feature_count=2,
        crs="EPSG:4326",
        status="COMPLETED",
        measured_count=0,
        skipped_count=2,
        failed_count=0,
        created_at=now,
        completed_at=now,
    )
    features = [
        FeatureMeasurement(
            index=i,
            feature_id=str(i),
            geometry_type="Point",
            geometry={"type": "Point", "coordinates": [78, 17]},
            properties={},
            source_crs="EPSG:4326",
            status="NOT_APPLICABLE",
        )
        for i in range(2)
    ]
    original = FeatureMeasurement.model_dump_json

    def interrupted(item, *args, **kwargs):
        if item.index == 1:
            raise asyncio.CancelledError
        return original(item, *args, **kwargs)

    monkeypatch.setattr(FeatureMeasurement, "model_dump_json", interrupted)
    with pytest.raises(asyncio.CancelledError):
        repo.save(info, features)
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM features").fetchone()[0] == 0


def test_worker_persistence_failure_returns_safe_error_and_no_partial_file(tmp_path, monkeypatch):
    original = Repository.save

    def fail_second_insert(repo, info, features):
        # Fail after metadata and one real result have been inserted in the transaction.
        original(repo, info, [features[0], features[0]])

    monkeypatch.setattr(Repository, "save", fail_second_insert)
    settings = Settings(database_path=tmp_path / "failed.db")
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        response = c.post("/api/files/", files={"file": ("point.kml", kml(POINT))})
        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "INTERNAL_ERROR", "message": "An unexpected server error occurred."}
        }
        assert c.get("/health/").status_code == 200
    with sqlite3.connect(settings.database_path) as conn:
        assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM features").fetchone()[0] == 0


@pytest.mark.parametrize("limit,expected", [(1, 200), (1000, 200), (1001, 422)])
def test_page_size_boundary(client, limit, expected):
    response = client.post("/api/files/", files={"file": ("point.kml", kml(POINT))})
    page = client.get(response.headers["Location"] + f"measurements/?limit={limit}")
    assert page.status_code == expected
    if expected == 200:
        assert page.json()["limit"] == limit and len(page.json()["features"]) == 1
    else:
        assert page.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "coordinates,target,tolerance",
    [
        ([(18, -34), (18.001, -34)], "EPSG:32734", 0.002),
        ([(0, -85), (0.01, -85)], "EPSG:32761", 0.007),
        ([(0, 89), (0.01, 89)], "EPSG:32661", 0.007),
        ([(5.999, 10), (6.001, 10)], "EPSG:32632", 0.002),
    ],
)
def test_crs_boundaries_have_independent_numeric_references(coordinates, target, tolerance):
    geometry = LineString(coordinates)
    value = result(geometry)
    assert value.status == "MEASURED"
    assert value.measurement_crs == target
    assert value.length == pytest.approx(
        Geod(ellps="WGS84").geometry_length(geometry), rel=tolerance
    )


@pytest.mark.parametrize("coordinates", [[(0, 83.99), (0, 84.01)], [(0, -80.01), (0, -79.99)]])
def test_features_crossing_projection_family_boundary_are_errors(coordinates):
    value = result(LineString(coordinates))
    assert value.status == "ERROR" and "UTM/UPS" in value.error
    assert value.length is None and value.measurement_crs is None


@pytest.mark.parametrize(
    "name,kind,status,expected",
    [
        ("polygon.kml", "Polygon", "MEASURED", 11804.993839917557),
        ("lines.kml", "LineString", "MEASURED", 106.57758259132162),
        ("points.kml", "Point", "NOT_APPLICABLE", None),
        ("sample_shapefile.zip", "Polygon", "MEASURED", 10000),
    ],
)
def test_checked_in_demo_samples_match_documented_results(client, name, kind, status, expected):
    from pathlib import Path

    data = (Path(__file__).resolve().parents[1] / "examples" / name).read_bytes()
    response = client.post("/api/files/", files={"file": (name, data)})
    assert response.status_code == 201, response.text
    feature = client.get(response.headers["Location"] + "measurements/").json()["features"][0]
    assert feature["geometry_type"] == kind and feature["status"] == status
    if expected is None:
        assert feature["area"] is None and feature["length"] is None
    else:
        measured = feature["area"] if kind == "Polygon" else feature["length"]
        assert measured == pytest.approx(expected, rel=1e-8)


def test_long_filename_does_not_lose_valid_format_extension(client):
    response = client.post("/api/files/", files={"file": ("a" * 300 + ".KML", kml(POINT))})
    assert response.status_code == 201
    assert response.json()["filename"] == "a" * 251 + ".kml"
    assert response.json()["file_type"] == "kml"
