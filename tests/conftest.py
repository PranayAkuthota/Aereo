import io
import zipfile
from pathlib import Path

import pytest
import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS

from app.config import Settings
from app.main import create_app


def kml(geometry: str, extra: str = "") -> bytes:
    return (
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f'<Placemark id="first"><name>synthetic</name>{geometry}</Placemark>{extra}'
        "</Document></kml>"
    ).encode()


POINT = "<Point><coordinates>78,17,0</coordinates></Point>"
LINE = "<LineString><coordinates>78,17 78.001,17</coordinates></LineString>"
POLYGON = (
    "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
    "78,17 78.001,17 78.001,17.001 78,17.001 78,17"
    "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
)


def zip_shapefile(crs: int | None = 32643, geometry: str = "polygon") -> bytes:
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf) as writer:
        writer.field("label", "C")
        if geometry == "polygon":
            # Clockwise exterior ring; a 100m x 100m square in UTM zone 43N.
            writer.poly(
                [
                    [
                        (500000, 1900000),
                        (500000, 1900100),
                        (500100, 1900100),
                        (500100, 1900000),
                        (500000, 1900000),
                    ]
                ]
            )
        elif geometry == "line":
            writer.line([[(500000, 1900000), (500030, 1900040)]])
        elif geometry == "point":
            writer.point(500000, 1900000)
        elif geometry == "null":
            writer.null()
        writer.record("synthetic")
    parts = {
        "sample.shp": shp.getvalue(),
        "sample.shx": shx.getvalue(),
        "sample.dbf": dbf.getvalue(),
    }
    if crs is not None:
        parts["sample.prj"] = CRS.from_epsg(crs).to_wkt().encode()
    return zip_parts(parts)


def zip_parts(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(database_path=tmp_path / "test.sqlite3")


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as value:
        yield value
