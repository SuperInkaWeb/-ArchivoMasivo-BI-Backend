"""DTOs de filtrado.

El conjunto de operadores es un enum CERRADO: cualquier operador fuera de
esta lista es rechazado por validación de pydantic antes de tocar la BD.
Esto es parte central de la defensa contra inyección (A03).
"""
from enum import Enum

from pydantic import BaseModel, Field


class Operator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    BETWEEN = "between"


class Combinator(str, Enum):
    AND = "and"
    OR = "or"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class FilterCondition(BaseModel):
    column: str
    operator: Operator
    # value: escalar para la mayoría; lista para IN/NOT_IN/BETWEEN; ausente para IS_NULL.
    value: str | int | float | bool | list[str | int | float] | None = None


class SortSpec(BaseModel):
    column: str
    direction: SortDirection = SortDirection.ASC


class FilterRequest(BaseModel):
    conditions: list[FilterCondition] = Field(default_factory=list)
    combinator: Combinator = Combinator.AND
    # Columnas a devolver; vacío = todas.
    select: list[str] = Field(default_factory=list)
    sort: list[SortSpec] = Field(default_factory=list)


class PreviewRequest(FilterRequest):
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class PreviewResponse(BaseModel):
    columns: list[str]
    rows: list[dict]
    total_matched: int
    limit: int
    offset: int


class DownloadFormat(str, Enum):
    CSV = "csv"
    XLSX = "xlsx"


class DownloadRequest(FilterRequest):
    format: DownloadFormat = DownloadFormat.CSV
