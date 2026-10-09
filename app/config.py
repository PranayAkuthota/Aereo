import os
from dataclasses import dataclass
from pathlib import Path


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path(".data/aereo.sqlite3")
    max_upload_bytes: int = 10 * 1024 * 1024
    max_expanded_bytes: int = 50 * 1024 * 1024
    max_features: int = 10_000
    max_coordinates: int = 100_000
    max_xml_elements: int = 200_000
    max_concurrent_uploads: int = 2
    max_result_bytes: int = 32 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=Path(os.getenv("AEREO_DATABASE_PATH", ".data/aereo.sqlite3")),
            max_upload_bytes=positive_int("AEREO_MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
            max_expanded_bytes=positive_int("AEREO_MAX_EXPANDED_BYTES", 50 * 1024 * 1024),
            max_features=positive_int("AEREO_MAX_FEATURES", 10_000),
            max_coordinates=positive_int("AEREO_MAX_COORDINATES", 100_000),
            max_xml_elements=positive_int("AEREO_MAX_XML_ELEMENTS", 200_000),
            max_concurrent_uploads=positive_int("AEREO_MAX_CONCURRENT_UPLOADS", 2),
            max_result_bytes=positive_int("AEREO_MAX_RESULT_BYTES", 32 * 1024 * 1024),
        )
