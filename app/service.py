import logging
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import uuid4

from app.config import Settings
from app.errors import APIError
from app.geospatial.measurements import measure
from app.geospatial.parser import parse
from app.repository import Repository
from app.schemas import FileInfo

logger = logging.getLogger(__name__)


def process_upload(
    data: bytes, filename: str, settings: Settings, repository: Repository
) -> FileInfo:
    # Keep only a bounded basename for metadata; uploaded names never become filesystem paths.
    basename = PurePosixPath(filename.replace("\\", "/")).name
    suffix = PurePosixPath(basename).suffix.lower()
    if suffix not in {".kml", ".zip"}:
        raise APIError("UNSUPPORTED_FILE", "Upload a .kml file or a zipped Shapefile.", 415)
    # Truncate only metadata, preserving the validated extension for long filenames.
    name = basename if len(basename) <= 255 else basename[: 255 - len(suffix)] + suffix
    if not data:
        raise APIError("EMPTY_FILE", "The uploaded file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise APIError("UPLOAD_TOO_LARGE", "The upload exceeds the size limit.", 413)
    created_at = datetime.now(UTC)
    dataset = parse(data, suffix, settings)
    results = []
    result_bytes = 0
    for feature in dataset.features:
        item = measure(feature, dataset.crs)
        result_bytes += len(item.model_dump_json().encode("utf-8"))
        if result_bytes > settings.max_result_bytes:
            raise APIError("RESULT_TOO_LARGE", "Serialized feature results exceed the limit.", 413)
        results.append(item)
    info = FileInfo(
        id=uuid4(),
        filename=name,
        file_type=dataset.file_type,
        crs=dataset.crs.to_string(),
        feature_count=len(results),
        status="COMPLETED",
        measured_count=sum(r.status == "MEASURED" for r in results),
        skipped_count=sum(r.status in {"NOT_APPLICABLE", "UNSUPPORTED"} for r in results),
        failed_count=sum(r.status == "ERROR" for r in results),
        created_at=created_at,
        completed_at=datetime.now(UTC),
    )
    repository.save(info, results)
    logger.info(
        "file_processed",
        extra={
            "file_id": str(info.id),
            "feature_count": info.feature_count,
            "failed_count": info.failed_count,
        },
    )
    return info
