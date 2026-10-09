import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.errors import APIError
from app.repository import Repository
from app.schemas import FeatureMeasurement, FileInfo


def test_failed_batch_insert_rolls_back_file_and_features(tmp_path):
    repository = Repository(tmp_path / "atomic.db")
    repository.initialize()
    now = datetime.now(UTC)
    info = FileInfo(
        id=uuid4(),
        filename="atomic.kml",
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
    feature = FeatureMeasurement(
        index=0,
        feature_id="0",
        geometry_type="Point",
        geometry={"type": "Point", "coordinates": [78, 17]},
        properties={},
        source_crs="EPSG:4326",
        status="NOT_APPLICABLE",
    )
    # Force a constraint failure on the second insert after the file and first row exist.
    with pytest.raises(sqlite3.IntegrityError):
        repository.save(info, [feature, feature])
    with pytest.raises(APIError) as exc:
        repository.get(str(info.id))
    assert exc.value.status_code == 404
    assert repository.measurements(str(info.id), 0, 100) == []
