import io
import lzma
import math
import re
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal
from xml.etree.ElementTree import Element, ParseError

import shapefile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.geometry.base import BaseGeometry

from app.config import Settings
from app.errors import APIError

KML_NS = "http://www.opengis.net/kml/2.2"
GX_NS = "http://www.google.com/kml/ext/2.2"
GEOMETRY_TAGS = {
    "Point",
    "LineString",
    "Polygon",
    "LinearRing",
    "MultiGeometry",
    "Model",
    "Track",
    "MultiTrack",
}
QUALIFIED_GEOMETRIES = {f"{{{KML_NS}}}{name}" for name in GEOMETRY_TAGS - {"Track", "MultiTrack"}}
QUALIFIED_GEOMETRIES |= {f"{{{GX_NS}}}Track", f"{{{GX_NS}}}MultiTrack"}


@dataclass
class Feature:
    index: int
    feature_id: str
    properties: dict[str, Any] = field(default_factory=dict)
    geometry: BaseGeometry | None = None
    geometry_type: str | None = None
    error: str | None = None


@dataclass
class Dataset:
    features: list[Feature]
    crs: CRS
    file_type: Literal["kml", "shapefile"]


def tag(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


class KMLReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.coordinate_count = 0

    def coordinates(self, element: Element | None) -> list[tuple[float, float]]:
        if element is None:
            raise ValueError("Missing coordinates.")
        text = element.findtext(f"{{{KML_NS}}}coordinates", default="")
        coordinates = []
        for match in re.finditer(r"\S+", text):
            if match.end() - match.start() > 128:
                raise ValueError("A coordinate tuple exceeds 128 characters.")
            token = match.group()
            self.coordinate_count += 1
            if self.coordinate_count > self.settings.max_coordinates:
                raise APIError("TOO_MANY_COORDINATES", "The coordinate limit was exceeded.", 413)
            parts = token.split(",")
            if len(parts) not in (2, 3):
                raise ValueError(
                    "KML coordinates must contain longitude, latitude, and optional altitude."
                )
            numbers = [float(part) for part in parts]
            if not all(math.isfinite(n) for n in numbers):
                raise ValueError("Coordinates must be finite.")
            lon, lat = numbers[:2]
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError("KML longitude or latitude is outside its valid range.")
            coordinates.append((lon, lat))
        return coordinates

    def ring(self, boundary: Element | None) -> list[tuple[float, float]]:
        if boundary is None:
            raise ValueError("Polygon is missing an outer boundary.")
        coordinates = self.coordinates(boundary.find(f"{{{KML_NS}}}LinearRing"))
        if len(coordinates) < 4 or coordinates[0] != coordinates[-1]:
            raise ValueError("Polygon rings must have at least four coordinates and be closed.")
        return coordinates

    def geometry(self, element: Element) -> BaseGeometry | None:
        if element.tag not in QUALIFIED_GEOMETRIES:
            raise ValueError("Unsupported geometry namespace.")
        kind = tag(element)
        if kind == "Point":
            coords = self.coordinates(element)
            if len(coords) != 1:
                raise ValueError("A Point must contain exactly one coordinate.")
            return Point(coords[0])
        if kind == "LineString":
            coords = self.coordinates(element)
            if len(coords) < 2:
                raise ValueError("A LineString must contain at least two coordinates.")
            return LineString(coords)
        if kind == "Polygon":
            outer = self.ring(element.find(f"{{{KML_NS}}}outerBoundaryIs"))
            holes = [self.ring(b) for b in element.findall(f"{{{KML_NS}}}innerBoundaryIs")]
            return Polygon(outer, holes)
        if kind == "MultiGeometry":
            children = [self.geometry(child) for child in element if tag(child) in GEOMETRY_TAGS]
            if not children:
                raise ValueError("MultiGeometry is empty.")
            if any(child is None for child in children):
                return None
            # Flatten nested homogeneous Multi* geometries without inventing mixed types.
            parts: list[BaseGeometry] = []
            pending = list(reversed([child for child in children if child is not None]))
            while pending:
                child = pending.pop()
                if child.geom_type.startswith("Multi") or child.geom_type == "GeometryCollection":
                    pending.extend(reversed(list(child.geoms)))
                else:
                    parts.append(child)
            kinds = {child.geom_type for child in parts}
            if kinds == {"Polygon"}:
                return MultiPolygon(parts)
            if kinds == {"LineString"}:
                return MultiLineString(parts)
            if kinds == {"Point"}:
                return MultiPoint(parts)
            return GeometryCollection(parts)
        return None

    def feature(self, placemark: Element, index: int) -> Feature:
        properties: dict[str, Any] = {}
        for name in ("name", "description"):
            value = placemark.findtext(f"{{{KML_NS}}}{name}")
            if value is not None:
                properties[name] = value
        for item in placemark.findall(f".//{{{KML_NS}}}Data"):
            if item.get("name"):
                properties[item.attrib["name"]] = item.findtext(f"{{{KML_NS}}}value")
        for item in placemark.findall(f".//{{{KML_NS}}}SimpleData"):
            if item.get("name"):
                properties[item.attrib["name"]] = item.text
        feature = Feature(index, placemark.get("id") or str(index), properties)
        geometries = [child for child in placemark if tag(child) in GEOMETRY_TAGS]
        if len(geometries) != 1:
            feature.error = "Placemark must contain exactly one geometry element."
        else:
            feature.geometry_type = tag(geometries[0])
            try:
                feature.geometry = self.geometry(geometries[0])
            except (ValueError, TypeError, GEOSException) as exc:
                message = str(exc)
                feature.error = message if len(message) <= 256 else "Malformed geometry values."
        return feature

    def read(self, data: bytes) -> Dataset:
        features: list[Feature] = []
        stack: list[Element] = []
        element_count = 0
        placemark_count = 0
        in_placemark = False
        try:
            events = ElementTree.iterparse(
                io.BytesIO(data), events=("start", "end"), forbid_dtd=True
            )
            for event, element in events:
                if event == "start":
                    if not stack and element.tag != f"{{{KML_NS}}}kml":
                        raise APIError(
                            "INVALID_KML",
                            "Expected a KML 2.2 document with its standard namespace.",
                        )
                    stack.append(element)
                    element_count += 1
                    if element_count > self.settings.max_xml_elements:
                        raise APIError(
                            "TOO_MANY_XML_ELEMENTS", "The XML element limit was exceeded.", 413
                        )
                    if len(stack) > 64:
                        raise APIError("INVALID_KML", "KML nesting exceeds 64 levels.")
                    if element.tag == f"{{{KML_NS}}}Placemark":
                        if in_placemark:
                            raise APIError("INVALID_KML", "Nested Placemarks are not supported.")
                        in_placemark = True
                        placemark_count += 1
                        if placemark_count > self.settings.max_features:
                            raise APIError(
                                "TOO_MANY_FEATURES", "The feature limit was exceeded.", 413
                            )
                    continue
                if element.tag == f"{{{KML_NS}}}Placemark":
                    features.append(self.feature(element, len(features)))
                    in_placemark = False
                if not in_placemark:
                    # Remove processed/irrelevant nodes, including from Document/Folder parents.
                    if len(stack) > 1:
                        stack[-2].remove(element)
                    element.clear()
                stack.pop()
        except (ParseError, DefusedXmlException) as exc:
            raise APIError("INVALID_KML", "The file is not safe, well-formed KML XML.") from exc
        if not features:
            raise APIError("EMPTY_DATASET", "The KML contains no Placemarks.")
        return Dataset(features, CRS.from_epsg(4326), "kml")


def archive_components(data: bytes, settings: Settings) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 100:
                raise APIError("INVALID_ZIP", "An archive must contain between 1 and 100 entries.")
            total = 0
            names: set[str] = set()
            files: dict[str, zipfile.ZipInfo] = {}
            for entry in entries:
                path = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or "\x00" in entry.orig_filename
                    or stat.S_ISLNK(mode)
                    or entry.flag_bits & 1
                ):
                    raise APIError("UNSAFE_ZIP", "Unsafe paths, links, or encrypted ZIP entries.")
                name = str(path).lower()
                if name in names:
                    raise APIError("INVALID_ZIP", "Duplicate archive entry names.")
                names.add(name)
                total += entry.file_size
                if total > settings.max_expanded_bytes:
                    raise APIError(
                        "ARCHIVE_TOO_LARGE", "Expanded archive size exceeds the limit.", 413
                    )
                if entry.file_size > max(entry.compress_size, 1) * 200:
                    raise APIError(
                        "UNSAFE_ZIP", "Archive compression ratio exceeds the safety limit."
                    )
                if not entry.is_dir():
                    files[name] = entry
            candidates = [name for name in files if name.endswith(".shp")]
            if len(candidates) != 1:
                raise APIError(
                    "INVALID_SHAPEFILE", "ZIP must contain exactly one Shapefile dataset."
                )
            stem = candidates[0][:-4]
            required = {ext: files.get(stem + ext) for ext in (".shp", ".shx", ".dbf", ".prj")}
            if required[".prj"] is None:
                raise APIError(
                    "MISSING_CRS", "Shapefile requires a matching .prj; CRS is not guessed."
                )
            if any(value is None for value in required.values()):
                raise APIError(
                    "INVALID_SHAPEFILE", "Matching .shp, .shx, .dbf and .prj are required."
                )
            if stem + ".cpg" in files:
                required[".cpg"] = files[stem + ".cpg"]
            if required[".prj"].file_size > 64 * 1024:
                raise APIError("CRS_TOO_LARGE", "The .prj exceeds 64 KiB.", 413)
            cpg = required.get(".cpg")
            if cpg is not None and cpg.file_size > 128:
                raise APIError("INVALID_SHAPEFILE", "The .cpg encoding label exceeds 128 bytes.")
            # No filesystem extraction: each selected component stays in bounded memory.
            return {
                ext: read_component(archive, entry)
                for ext, entry in required.items()
                if entry is not None
            }
    except APIError:
        raise
    except (
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        raise APIError("INVALID_ZIP", "The file is not a readable ZIP archive.") from exc


def read_component(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    with archive.open(entry) as stream:
        data = stream.read()  # zipfile verifies member headers and CRC.
        # Python 3.12's ZipExtFile can accept a truncated DEFLATE terminator when
        # the decoded bytes still match CRC. Verify the decoder's end marker too.
        needs_end = entry.compress_type in {zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2}
        needs_end |= entry.compress_type == zipfile.ZIP_LZMA and bool(entry.flag_bits & 2)
        decoder = getattr(stream, "_decompressor", None)
        if needs_end and not getattr(decoder, "eof", False):
            raise zipfile.BadZipFile("Compressed stream has no end marker.")
        return data


def attribute_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite DBF attribute.")
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def preflight_shapefile(parts: dict[str, bytes], settings: Settings) -> None:
    """Check actual index/record sizes and counts before PyShp allocates coordinate arrays."""
    shp, shx, dbf = parts[".shp"], parts[".shx"], parts[".dbf"]
    if len(shx) < 100 or (len(shx) - 100) % 8 or len(dbf) < 32:
        raise APIError("INVALID_SHAPEFILE", "Shapefile index or attribute header is truncated.")
    for component in (shp, shx):
        if (
            len(component) < 100
            or struct.unpack_from(">I", component, 0)[0] != 9994
            or struct.unpack_from("<I", component, 28)[0] != 1000
            or struct.unpack_from(">I", component, 24)[0] * 2 != len(component)
        ):
            raise APIError("INVALID_SHAPEFILE", "Invalid Shapefile component header or length.")
    if shp[32:36] != shx[32:36]:
        raise APIError("INVALID_SHAPEFILE", "SHP and SHX geometry types disagree.")
    file_kind = struct.unpack_from("<I", shp, 32)[0]
    expected_offset = 100
    count = (len(shx) - 100) // 8
    records = struct.unpack_from("<I", dbf, 4)[0]
    if max(count, records) > settings.max_features:
        raise APIError("TOO_MANY_FEATURES", "The feature limit was exceeded.", 413)
    if count != records:
        raise APIError("INVALID_SHAPEFILE", "Geometry and attribute record counts differ.")
    coordinate_count = 0
    for i in range(count):
        offset_words, length_words = struct.unpack_from(">II", shx, 100 + i * 8)
        offset, size = offset_words * 2, length_words * 2
        if offset != expected_offset or size < 4 or offset + 8 + size > len(shp):
            raise APIError("INVALID_SHAPEFILE", "A shape record points outside its component.")
        if struct.unpack_from(">I", shp, offset)[0] != i + 1:
            raise APIError("INVALID_SHAPEFILE", "Shape record identity disagrees with its index.")
        expected_offset = offset + 8 + size
        if struct.unpack_from(">I", shp, offset + 4)[0] != length_words:
            raise APIError("INVALID_SHAPEFILE", "SHP and SHX record lengths disagree.")
        kind = struct.unpack_from("<I", shp, offset + 8)[0]
        if kind not in {0, file_kind}:
            raise APIError("INVALID_SHAPEFILE", "Shape record type disagrees with the file header.")
        points = 0
        minimum = 4
        if kind in {1, 11, 21}:
            points, minimum = 1, 20
        elif kind in {8, 18, 28}:
            if size < 40:
                raise APIError("INVALID_SHAPEFILE", "MultiPoint record is truncated.")
            points = struct.unpack_from("<i", shp, offset + 44)[0]
            minimum = 40 + max(points, 0) * 16
        elif kind in {3, 5, 13, 15, 23, 25, 31}:
            if size < 44:
                raise APIError("INVALID_SHAPEFILE", "Multipart shape record is truncated.")
            part_count, points = struct.unpack_from("<ii", shp, offset + 44)
            if part_count < 0 or part_count > points:
                raise APIError("INVALID_SHAPEFILE", "Invalid shape part count.")
            minimum = 44 + part_count * (8 if kind == 31 else 4) + max(points, 0) * 16
        if points < 0 or size < minimum:
            raise APIError("INVALID_SHAPEFILE", "Invalid or truncated shape coordinate array.")
        coordinate_count += points
        if coordinate_count > settings.max_coordinates:
            raise APIError("TOO_MANY_COORDINATES", "The coordinate limit was exceeded.", 413)
    if expected_offset != len(shp):
        raise APIError(
            "INVALID_SHAPEFILE", "SHP records are not fully represented by the SHX index."
        )


def read_shapefile(data: bytes, settings: Settings) -> Dataset:
    parts = archive_components(data, settings)
    try:
        crs = CRS.from_wkt(parts[".prj"].decode("utf-8-sig"))
    except (CRSError, UnicodeError) as exc:
        raise APIError("INVALID_CRS", "The .prj file does not contain a valid CRS.") from exc
    preflight_shapefile(parts, settings)
    features = []
    try:
        encoding = parts.get(".cpg", b"utf-8").decode("ascii").strip()
        if encoding == "65001":
            encoding = "utf-8"
        with shapefile.Reader(
            shp=io.BytesIO(parts[".shp"]),
            shx=io.BytesIO(parts[".shx"]),
            dbf=io.BytesIO(parts[".dbf"]),
            encoding=encoding,
        ) as reader:
            if reader.numRecords > settings.max_features:
                raise APIError("TOO_MANY_FEATURES", "The feature limit was exceeded.", 413)
            if reader.numShapes != reader.numRecords:
                raise APIError("INVALID_SHAPEFILE", "Geometry and attribute record counts differ.")
            coordinate_count = 0
            for index in range(reader.numRecords):
                # iterShapeRecords() zips filtered DBF records with unfiltered shapes.
                # Read both by the same original ordinal; deleted DBF rows are omitted.
                record = reader.record(index)
                if record is None:
                    continue
                source_shape = reader.shape(index)
                coordinate_count += len(source_shape.points)
                if coordinate_count > settings.max_coordinates:
                    raise APIError(
                        "TOO_MANY_COORDINATES", "The coordinate limit was exceeded.", 413
                    )
                properties = record.as_dict()
                # DBF dates and binary fields must become predictable JSON values.
                properties = {key: attribute_value(value) for key, value in properties.items()}
                feature = Feature(
                    index, str(index), properties, geometry_type=source_shape.shapeTypeName
                )
                try:
                    if source_shape.shapeType == shapefile.NULL:
                        feature.error = "Geometry is empty."
                    elif source_shape.shapeType == shapefile.MULTIPATCH:
                        feature.geometry = None  # No planar GeoJSON representation.
                    else:
                        feature.geometry = shape(source_shape.__geo_interface__)
                except (
                    ValueError,
                    TypeError,
                    GEOSException,
                    shapefile.ShapefileException,
                    shapefile.GeoJSON_Error,
                ) as exc:
                    feature.error = f"Invalid Shapefile geometry: {type(exc).__name__}."
                features.append(feature)
    except APIError:
        raise
    except Exception as exc:
        raise APIError(
            "INVALID_SHAPEFILE", "Shapefile components or attributes are malformed."
        ) from exc
    if not features:
        raise APIError("EMPTY_DATASET", "The Shapefile contains no features.")
    return Dataset(features, crs, "shapefile")


def parse(data: bytes, suffix: str, settings: Settings) -> Dataset:
    if suffix == ".kml":
        return KMLReader(settings).read(data)
    if suffix == ".zip":
        return read_shapefile(data, settings)
    raise APIError("UNSUPPORTED_FILE", "Upload a .kml file or a .zip containing a Shapefile.", 415)
