import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.errors import APIError
from app.schemas import FeatureMeasurement, FileInfo


class Repository:
    """One connection per operation; file and feature inserts share one transaction."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS features (
                    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    feature_index INTEGER NOT NULL CHECK(feature_index >= 0),
                    result TEXT NOT NULL,
                    PRIMARY KEY (file_id, feature_index)
                );
            """)

    def save(self, info: FileInfo, features: list[FeatureMeasurement]) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO files VALUES (?, ?)", (str(info.id), info.model_dump_json()))
            conn.executemany(
                "INSERT INTO features VALUES (?, ?, ?)",
                ((str(info.id), item.index, item.model_dump_json()) for item in features),
            )
            conn.commit()

    def get(self, file_id: str) -> FileInfo:
        with self.connection() as conn:
            row = conn.execute("SELECT metadata FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise APIError("NOT_FOUND", "File not found.", 404)
        return FileInfo.model_validate_json(row[0])

    def measurements(self, file_id: str, offset: int, limit: int) -> list[FeatureMeasurement]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT result FROM features WHERE file_id=? "
                "ORDER BY feature_index LIMIT ? OFFSET ?",
                (file_id, limit, offset),
            ).fetchall()
        return [FeatureMeasurement.model_validate_json(row[0]) for row in rows]

    def check(self) -> None:
        with self.connection() as conn:
            conn.execute("SELECT id FROM files LIMIT 1").fetchone()
