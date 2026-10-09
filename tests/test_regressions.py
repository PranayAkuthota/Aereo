import io
import struct
import zipfile

import pytest
import shapefile
from conftest import zip_parts
from pyproj import CRS


def deleted_record_archive(deleted: set[int]) -> bytes:
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf) as writer:
        writer.field("label", "C")
        for i in range(5):
            x = 500000 + i * 100
            writer.line([[(x, 1900000), (x + (i + 1) * 10, 1900000)]])
            writer.record(f"record-{i}")
    attributes = bytearray(dbf.getvalue())
    header_length, record_length = struct.unpack_from("<HH", attributes, 8)
    for i in deleted:
        attributes[header_length + i * record_length] = ord("*")
    return zip_parts(
        {
            "records.shp": shp.getvalue(),
            "records.shx": shx.getvalue(),
            "records.dbf": bytes(attributes),
            "records.prj": CRS.from_epsg(32643).to_wkt().encode(),
        }
    )


@pytest.mark.parametrize("deleted", [{0}, {2}, {0, 2, 3}, set()])
def test_deleted_dbf_rows_preserve_original_identity_and_association(client, deleted):
    response = client.post(
        "/api/files/", files={"file": ("records.zip", deleted_record_archive(deleted))}
    )
    assert response.status_code == 201, response.text
    info = response.json()
    assert info["feature_count"] == 5 - len(deleted)
    features = client.get(response.headers["Location"] + "measurements/").json()["features"]
    assert [f["index"] for f in features] == [i for i in range(5) if i not in deleted]
    for feature in features:
        i = feature["index"]
        assert feature["feature_id"] == str(i)
        assert feature["properties"] == {"label": f"record-{i}"}
        assert feature["geometry"]["coordinates"] == [
            [500000 + i * 100, 1900000],
            [500000 + i * 100 + (i + 1) * 10, 1900000],
        ]
        assert feature["length"] == pytest.approx((i + 1) * 10, rel=1e-8)
        assert feature["source_geometry_type"] == "POLYLINE"


def test_all_deleted_records_return_empty_dataset(client):
    response = client.post(
        "/api/files/", files={"file": ("deleted.zip", deleted_record_archive(set(range(5))))}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EMPTY_DATASET"


def corrupted_archive(kind: str) -> bytes:
    data = bytearray(deleted_record_archive(set()))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        member = archive.getinfo("records.shp")
        local = member.header_offset
        name_length, extra_length = struct.unpack_from("<HH", data, local + 26)
        start = local + 30 + name_length + extra_length
    central = data.index(b"PK\x01\x02")  # first member is records.shp
    if kind == "deflate":
        data[start] = 0x07  # reserved DEFLATE block type
    elif kind in {"truncated", "missing_end"}:
        shortened = member.compress_size // 2 if kind == "truncated" else member.compress_size - 1
        struct.pack_into("<I", data, local + 18, shortened)
        struct.pack_into("<I", data, central + 20, shortened)
    else:
        struct.pack_into("<I", data, local + 14, member.CRC ^ 1)
        struct.pack_into("<I", data, central + 16, member.CRC ^ 1)
    return bytes(data)


@pytest.mark.parametrize("kind", ["deflate", "truncated", "crc", "missing_end"])
def test_compression_corruption_is_a_controlled_client_error(client, kind):
    response = client.post("/api/files/", files={"file": ("broken.zip", corrupted_archive(kind))})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ZIP"


@pytest.mark.parametrize("offset", [0, 1, 100, 2**63, 10**100])
def test_pagination_never_binds_out_of_range_offsets(client, offset):
    response = client.post(
        "/api/files/", files={"file": ("records.zip", deleted_record_archive(set()))}
    )
    page = client.get(response.headers["Location"] + f"measurements/?offset={offset}&limit=1")
    assert page.status_code == 200, page.text
    result = page.json()
    assert result["offset"] == offset
    assert len(result["features"]) == (1 if offset < 5 else 0)
    if result["features"]:
        assert result["features"][0]["index"] == offset


def test_shapefile_coordinate_header_limit_is_enforced_before_reader_allocation(
    client, monkeypatch
):
    data = deleted_record_archive(set())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parts = {n: archive.read(n) for n in archive.namelist()}
    shp = bytearray(parts["records.shp"])
    struct.pack_into("<i", shp, 100 + 48, 2_000_000)
    parts["records.shp"] = bytes(shp)
    called = False
    original = shapefile.Reader

    def tracked(*args, **kwargs):
        nonlocal called
        called = True
        return original(*args, **kwargs)

    monkeypatch.setattr(shapefile, "Reader", tracked)
    response = client.post("/api/files/", files={"file": ("bad-header.zip", zip_parts(parts))})
    assert response.status_code in {400, 413}
    assert not called


def test_unsupported_shapefile_type_survives_conversion_failure(client):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf) as writer:
        writer.field("name", "C")
        writer.multipatch(
            [[(500000, 1900000, 0), (500010, 1900000, 0), (500000, 1900010, 0)]],
            [shapefile.TRIANGLE_STRIP],
        )
        writer.record("unsupported")
    data = zip_parts(
        {
            "patch.shp": shp.getvalue(),
            "patch.shx": shx.getvalue(),
            "patch.dbf": dbf.getvalue(),
            "patch.prj": CRS.from_epsg(32643).to_wkt().encode(),
        }
    )
    response = client.post("/api/files/", files={"file": ("patch.zip", data)})
    assert response.status_code == 201, response.text
    feature = client.get(response.headers["Location"] + "measurements/").json()["features"][0]
    assert feature["source_geometry_type"] == "MULTIPATCH"
    assert feature["geometry_type"] == "MULTIPATCH"
    assert feature["status"] in {"ERROR", "UNSUPPORTED"}
    assert feature["area"] is None and feature["length"] is None


def test_xyz_shapefile_preserves_source_type_but_normalizes_output_to_xy(client):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf) as writer:
        writer.field("name", "C")
        writer.linez([[(500000, 1900000, 10), (500030, 1900040, 1000)]])
        writer.record("horizontal")
    data = zip_parts(
        {
            "xyz.shp": shp.getvalue(),
            "xyz.shx": shx.getvalue(),
            "xyz.dbf": dbf.getvalue(),
            "xyz.prj": CRS.from_epsg(32643).to_wkt().encode(),
        }
    )
    response = client.post("/api/files/", files={"file": ("xyz.zip", data)})
    assert response.status_code == 201, response.text
    result = client.get(response.headers["Location"] + "measurements/").json()["features"][0]
    assert result["source_geometry_type"] == "POLYLINEZ"
    assert result["geometry"]["coordinates"] == [[500000, 1900000], [500030, 1900040]]
    assert result["length"] == pytest.approx(50)


def test_valid_multipoint_upload_is_not_limited_by_measurement_extent(client):
    content = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><MultiGeometry><Point><coordinates>10,10</coordinates></Point><Point><coordinates>30,10</coordinates></Point></MultiGeometry></Placemark></kml>'
    response = client.post("/api/files/", files={"file": ("points.kml", content)})
    assert response.status_code == 201, response.text
    assert response.json()["skipped_count"] == 1
    result = client.get(response.headers["Location"] + "measurements/").json()["features"][0]
    assert result["status"] == "NOT_APPLICABLE"
    assert result["measurement_crs"] is None
    assert result["geometry"]["coordinates"] == [[10, 10], [30, 10]]


@pytest.mark.parametrize("fault", ["duplicate_index", "wrong_record_identity"])
def test_invalid_shapefile_index_cannot_reassociate_geometry_and_attributes(client, fault):
    with zipfile.ZipFile(io.BytesIO(deleted_record_archive(set()))) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    if fault == "duplicate_index":
        index = bytearray(parts["records.shx"])
        index[108:116] = index[100:108]
        parts["records.shx"] = bytes(index)
    else:
        component = bytearray(parts["records.shp"])
        struct.pack_into(">I", component, 100, 99)
        parts["records.shp"] = bytes(component)
    response = client.post("/api/files/", files={"file": ("bad-index.zip", zip_parts(parts))})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SHAPEFILE"


def test_oversized_crs_definition_is_rejected_before_cache_growth(client):
    with zipfile.ZipFile(io.BytesIO(deleted_record_archive(set()))) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    # Store instead of deflating so the dedicated CRS bound, not ratio protection, is tested.
    parts["records.prj"] = b"x" * (64 * 1024 + 1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    response = client.post("/api/files/", files={"file": ("crs.zip", buffer.getvalue())})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CRS_TOO_LARGE"


@pytest.mark.parametrize(
    "compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]
)
def test_supported_zip_compression_remains_compatible(client, compression):
    with zipfile.ZipFile(io.BytesIO(deleted_record_archive(set()))) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    response = client.post("/api/files/", files={"file": ("normal.zip", buffer.getvalue())})
    assert response.status_code == 201, response.text
    page = client.get(response.headers["Location"] + "measurements/").json()
    assert [f["length"] for f in page["features"]] == pytest.approx([10, 20, 30, 40, 50])
