from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import FeatureMeasurement, FileInfo

LINE = {
    "index": 0,
    "feature_id": "0",
    "geometry_type": "LineString",
    "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]},
    "properties": {},
    "source_crs": "EPSG:32631",
    "measurement_crs": "EPSG:32631",
    "status": "MEASURED",
    "length": 1,
    "length_unit": "m",
}


@pytest.mark.parametrize(
    "changes",
    [
        {"length": float("nan")},
        {"length": float("inf")},
        {"length": -1},
        {"length_unit": None},
        {"length_unit": "feet"},
        {"measurement_crs": None},
        {"error": "contradiction"},
        {"area": 1, "area_unit": "m²"},
        {"status": "ERROR", "error": "failure"},
        {"geometry_type": "Polygon"},
    ],
)
def test_impossible_measurement_results_are_rejected(changes):
    with pytest.raises(ValidationError):
        FeatureMeasurement.model_validate(LINE | changes)


@pytest.mark.parametrize("status", ["ERROR", "UNSUPPORTED"])
def test_unmeasured_failure_requires_error_and_allows_preserved_crs(status):
    value = LINE | {"status": status, "length": None, "length_unit": None}
    with pytest.raises(ValidationError):
        FeatureMeasurement.model_validate(value)
    assert FeatureMeasurement.model_validate(value | {"error": "reason"}).status == status


def test_file_count_invariant():
    now = datetime.now(UTC)
    value = {
        "id": uuid4(),
        "filename": "x.kml",
        "file_type": "kml",
        "feature_count": 2,
        "crs": "EPSG:4326",
        "status": "COMPLETED",
        "measured_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "created_at": now,
        "completed_at": now,
    }
    with pytest.raises(ValidationError):
        FileInfo.model_validate(value)
    assert FileInfo.model_validate(value | {"failed_count": 1}).feature_count == 2
