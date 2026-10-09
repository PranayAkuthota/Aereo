from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

MetricValue = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class FileInfo(BaseModel):
    id: UUID
    filename: str
    file_type: Literal["kml", "shapefile"]
    feature_count: int = Field(ge=0)
    crs: str
    status: Literal["COMPLETED"]
    measured_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    created_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.measured_count + self.skipped_count + self.failed_count != self.feature_count:
            raise ValueError("Feature outcome counts must sum to feature_count.")
        return self


class FeatureMeasurement(BaseModel):
    index: int = Field(ge=0)
    feature_id: str
    geometry_type: str | None
    source_geometry_type: str | None = None
    geometry: dict[str, Any] | None
    properties: dict[str, Any]
    source_crs: str
    measurement_crs: str | None = None
    status: Literal["MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "ERROR"]
    area: MetricValue | None = None
    area_unit: Literal["m²"] | None = None
    length: MetricValue | None = None
    length_unit: Literal["m"] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if (self.area is None) != (self.area_unit is None):
            raise ValueError("Area and area_unit must be provided together.")
        if (self.length is None) != (self.length_unit is None):
            raise ValueError("Length and length_unit must be provided together.")
        if self.status == "MEASURED":
            if (self.area is None) == (self.length is None):
                raise ValueError("A measured feature needs exactly one measurement.")
            if not self.measurement_crs or self.geometry is None or self.error is not None:
                raise ValueError("A measured feature requires geometry/CRS and no error.")
            if self.area is not None and self.geometry_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError("Area requires a polygon geometry.")
            if self.length is not None and self.geometry_type not in {
                "LineString",
                "MultiLineString",
            }:
                raise ValueError("Length requires a line geometry.")
        else:
            if self.area is not None or self.length is not None:
                raise ValueError("Unmeasured features cannot contain measurements.")
            if self.status == "NOT_APPLICABLE":
                if self.geometry_type not in {"Point", "MultiPoint"} or self.geometry is None:
                    raise ValueError("NOT_APPLICABLE requires a point geometry.")
                if self.measurement_crs is not None or self.error is not None:
                    raise ValueError("Points require neither a measurement CRS nor an error.")
            elif not self.error or not self.error.strip():
                raise ValueError("Failed or unsupported features require an error message.")
        return self


class MeasurementPage(BaseModel):
    file_id: UUID
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=1000)
    features: list[FeatureMeasurement]


class Health(BaseModel):
    status: Literal["ok"] = Field(default="ok")
