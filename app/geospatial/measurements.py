import math
from typing import Any

from pyproj import CRS
from shapely import force_2d
from shapely.errors import GEOSException
from shapely.geometry import mapping
from shapely.validation import explain_validity

from app.errors import FeatureError
from app.geospatial.crs import finite_geometry, geographic_geometry, project_for_measurement
from app.geospatial.parser import Feature
from app.schemas import FeatureMeasurement


def measure(feature: Feature, source: CRS) -> FeatureMeasurement:
    geometry = force_2d(feature.geometry) if feature.geometry is not None else None
    crs_name = source.to_string()
    values: dict[str, Any] = dict(
        index=feature.index,
        feature_id=feature.feature_id,
        properties=feature.properties,
        source_crs=crs_name,
        geometry_type=geometry.geom_type if geometry is not None else feature.geometry_type,
        source_geometry_type=feature.geometry_type
        or (geometry.geom_type if geometry is not None else None),
        geometry=None,
        status="ERROR",
    )
    try:
        if feature.error:
            raise FeatureError(feature.error)
        if geometry is None:
            return FeatureMeasurement(
                **(values | {"status": "UNSUPPORTED"}), error="Geometry type is not supported."
            )
        if geometry.is_empty:
            raise FeatureError("Geometry is empty.")
        if not finite_geometry(geometry):
            raise FeatureError("Coordinates must be finite.")
        values["geometry"] = mapping(geometry)
        if not geometry.is_valid:
            raise FeatureError(f"Invalid geometry: {explain_validity(geometry)}")
        if geometry.geom_type not in {
            "Polygon",
            "MultiPolygon",
            "LineString",
            "MultiLineString",
            "Point",
            "MultiPoint",
        }:
            return FeatureMeasurement(
                **(values | {"status": "UNSUPPORTED"}),
                error="Mixed GeometryCollections are not measured.",
            )
        if geometry.geom_type in {"Point", "MultiPoint"}:
            geographic_geometry(geometry, source)
            return FeatureMeasurement(**(values | {"status": "NOT_APPLICABLE"}))
        projected, target = project_for_measurement(geometry, source)
        values["measurement_crs"] = target
        if not projected.is_valid:
            raise FeatureError("Geometry became invalid after projection.")
        is_area = geometry.geom_type in {"Polygon", "MultiPolygon"}
        value = float(projected.area if is_area else projected.length)
        if not math.isfinite(value) or value <= 0:
            raise FeatureError("Measurement must be finite and positive.")
        values["status"] = "MEASURED"
        if is_area:
            return FeatureMeasurement(**values, area=value, area_unit="m²")
        return FeatureMeasurement(**values, length=value, length_unit="m")
    except FeatureError as exc:
        return FeatureMeasurement(**(values | {"status": "ERROR"}), error=str(exc))
    except GEOSException:
        return FeatureMeasurement(
            **(values | {"status": "ERROR"}), error="Geometry operation failed."
        )
