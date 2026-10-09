import math
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely import get_coordinates
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from app.errors import FeatureError


@lru_cache(maxsize=128)
def transformer(source_wkt: str, destination: str) -> Transformer:
    return Transformer.from_crs(source_wkt, destination, always_xy=True, allow_ballpark=False)


def geographic_geometry(geometry: BaseGeometry, source: CRS) -> BaseGeometry:
    """Validate horizontal coordinates without imposing measurement extent limits."""
    if not (source.is_geographic or source.is_projected):
        raise FeatureError("Only geographic or projected horizontal CRSs are supported.")
    try:
        to_wgs84 = transformer(source.to_wkt(), "EPSG:4326")
        geographic = transform(
            lambda x, y, z=None: to_wgs84.transform(x, y, errcheck=True), geometry
        )
        if not finite_geometry(geographic):
            raise FeatureError("CRS transformation produced non-finite coordinates.")
        west, south, east, north = geographic.bounds
        if west < -180 or east > 180 or south < -90 or north > 90:
            raise FeatureError("Coordinates fall outside the valid geographic range.")
        return geographic
    except ProjError as exc:
        raise FeatureError(
            "Cannot transform this CRS using available datum transformations."
        ) from exc


def project_for_measurement(geometry: BaseGeometry, source: CRS) -> tuple[BaseGeometry, str]:
    """Choose a local metric UTM/UPS CRS for each measurable feature."""
    try:
        geographic = geographic_geometry(geometry, source)
        west, south, east, north = geographic.bounds
        if east - west > 6 or north - south > 6:
            raise FeatureError(
                "Feature exceeds the local projection limit (6° longitude or latitude); "
                "split it before measuring. Antimeridian crossings are not supported."
            )
        lon, lat = geographic.centroid.x, geographic.centroid.y
        if south >= 84:
            target = "EPSG:32661"
        elif north <= -80:
            target = "EPSG:32761"
        elif south < -80 or north > 84:
            raise FeatureError("Feature crosses the UTM/UPS boundary; split it before measuring.")
        else:
            zone = min(60, max(1, int((lon + 180) // 6) + 1))
            target = f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"
        # Original -> target avoids chaining datum transformations and preserves projected inputs.
        to_metric = transformer(source.to_wkt(), target)
        projected = transform(
            lambda x, y, z=None: to_metric.transform(x, y, errcheck=True), geometry
        )
        if not all(math.isfinite(float(n)) for pair in get_coordinates(projected) for n in pair):
            raise FeatureError("Measurement projection produced non-finite coordinates.")
        return projected, target
    except ProjError as exc:
        raise FeatureError(
            "Cannot transform this CRS using available datum transformations."
        ) from exc


def finite_geometry(geometry: BaseGeometry) -> bool:
    return all(math.isfinite(float(n)) for pair in get_coordinates(geometry) for n in pair)
