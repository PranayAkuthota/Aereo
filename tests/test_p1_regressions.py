"""End-to-end malformed-input regressions from the final adversarial review."""

import io
import sqlite3
import struct

import pytest
import shapefile
from conftest import LINE, kml, zip_parts, zip_shapefile
from pyproj import CRS


def assert_rejected_without_persistence(client, settings, filename, data, code):
    response = client.post("/api/files/", files={"file": (filename, data)})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert "Traceback" not in response.text
    assert "Location" not in response.headers
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 0


def multipart_archive(starts=None, name="multipart"):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf) as writer:
        writer.field("label", "C")
        writer.line(
            [
                [(500000, 1900000), (500010, 1900000), (500020, 1900000)],
                [(500100, 1900000), (500110, 1900000), (500120, 1900000)],
            ]
        )
        writer.record("two parts")
    geometry = bytearray(shp.getvalue())
    if starts is not None:
        struct.pack_into("<ii", geometry, 152, *starts)
    return zip_parts(
        {
            name + ".shp": bytes(geometry),
            name + ".shx": shx.getvalue(),
            name + ".dbf": dbf.getvalue(),
            name + ".prj": CRS.from_epsg(32643).to_wkt().encode(),
        }
    )


@pytest.mark.parametrize("starts", [(1, 3), (-1, 3), (0, -1), (0, 0), (0, 6), (0, 7), (3, 0)])
def test_invalid_multipart_starts_are_rejected_atomically(client, settings, starts):
    assert_rejected_without_persistence(
        client, settings, "multipart.zip", multipart_archive(starts), "INVALID_SHAPEFILE"
    )


@pytest.mark.parametrize("name", ["multipart", "café"])
def test_valid_multipart_preserves_all_vertices_and_length(client, name):
    response = client.post(
        "/api/files/", files={"file": ("multipart.zip", multipart_archive(name=name))}
    )
    assert response.status_code == 201, response.text
    location = response.headers["Location"]
    assert client.get(location).json()["measured_count"] == 1
    feature = client.get(location + "measurements/").json()["features"][0]
    assert feature["status"] == "MEASURED"
    assert feature["error"] is None
    assert feature["length"] == pytest.approx(40.0, rel=1e-8)
    assert [len(part) for part in feature["geometry"]["coordinates"]] == [3, 3]


@pytest.mark.parametrize("encoding", ["NOT-A-CODEC", "UTF-32"])
def test_unsupported_xml_encoding_is_controlled(client, settings, encoding):
    data = f'<?xml version="1.0" encoding="{encoding}"?>'.encode() + kml(LINE)
    assert_rejected_without_persistence(client, settings, "bad.kml", data, "INVALID_KML")


def test_malformed_xml_is_controlled(client, settings):
    assert_rejected_without_persistence(client, settings, "bad.kml", kml(LINE)[:-6], "INVALID_KML")


def test_valid_utf8_xml_remains_measured(client):
    data = b'<?xml version="1.0" encoding="UTF-8"?>' + kml(LINE)
    response = client.post("/api/files/", files={"file": ("line.kml", data)})
    assert response.status_code == 201, response.text
    location = response.headers["Location"]
    assert client.get(location).json()["measured_count"] == 1
    feature = client.get(location + "measurements/").json()["features"][0]
    assert feature["status"] == "MEASURED"
    assert feature["length"] > 0


def test_invalid_zip_filename_encoding_is_controlled(client, settings):
    data = bytearray(zip_shapefile())
    central = data.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", data, central + 8)[0]
    struct.pack_into("<H", data, central + 8, flags | 0x800)
    data[central + 46] = 0xFF
    assert_rejected_without_persistence(client, settings, "bad.zip", bytes(data), "INVALID_ZIP")


def test_malformed_zip_is_controlled(client, settings):
    assert_rejected_without_persistence(client, settings, "bad.zip", b"not a zip", "INVALID_ZIP")
