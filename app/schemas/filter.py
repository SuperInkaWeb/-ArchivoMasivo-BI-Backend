"""DTOs de filtrado (árbol recursivo con grupos anidados).

Un filtro es un ÁRBOL de nodos:
  - FilterLeaf  = una condición sobre una columna (hoja).
  - FilterGroup = un conector (AND/OR) con hijos, que pueden ser hojas u otros grupos.

Esto permite lógica anidada como "(region = Lima Y monto > 1000) O tipo = 08".
El conjunto de operadores es un enum CERRADO: cualquier operador fuera de la lista
es rechazado por validación antes de tocar la BD (defensa contra inyección, A03).
"""
from enum import Enum
from typing import Annotated, Literal, Union

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


FilterValue = str | int | float | bool | list[str | int | float] | None


class FilterLeaf(BaseModel):
    """Hoja: una condición sobre una columna."""
    type: Literal["condition"] = "condition"
    column: str
    operator: Operator
    # value: escalar para la mayoría; lista para IN/NOT_IN/BETWEEN; ausente para IS_NULL.
    value: FilterValue = None


class FilterGroup(BaseModel):
    """Grupo: un conector (AND/OR) con hijos (hojas u otros grupos)."""
    type: Literal["group"] = "group"
    combinator: Combinator = Combinator.AND
    children: list["FilterNode"] = Field(default_factory=list)


# Unión discriminada por el campo "type"; recursiva vía FilterGroup.children.
FilterNode = Annotated[Union[FilterLeaf, FilterGroup], Field(discriminator="type")]
FilterGroup.model_rebuild()


class SortSpec(BaseModel):
    column: str
    direction: SortDirection = SortDirection.ASC


class FilterRequest(BaseModel):
    # Árbol raíz de filtros; None (o grupo vacío) = sin filtro (todas las filas).
    filter: FilterGroup | None = None
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


class DistinctValuesResponse(BaseModel):
    """Valores únicos de una columna para el filtro tipo Excel.

    Los valores se devuelven como texto para un contrato simple; el cliente los
    coacciona al tipo real de la columna al construir el filtro.
    """
    column: str
    values: list[str]
    truncated: bool  # true si hay más valores de los devueltos
