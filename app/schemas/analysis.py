"""DTOs de análisis: tablas dinámicas (pivote).

Un pivote agrupa por una o más columnas (dimensiones) y calcula métricas
(agregaciones) sobre otras. Opcionalmente cruza una columna cuyos valores
distintos se vuelven columnas del reporte (cross-tab, estilo Excel).

El conjunto de agregaciones es un enum CERRADO: cualquier función fuera de la
lista se rechaza antes de tocar la BD (misma defensa contra inyección que el
filtrado, A03). Los nombres de columna se validan contra el esquema real.
"""
from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.expression import ComputedColumn
from app.schemas.filter import FilterGroup


class Aggregation(str, Enum):
    COUNT = "count"                    # count(col) o count(*) si no hay columna
    COUNT_DISTINCT = "count_distinct"  # count(DISTINCT col)
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class Measure(BaseModel):
    """Una métrica del reporte: una agregación sobre una columna.

    `column` solo puede omitirse para COUNT (=> count(*), conteo de filas).
    """
    column: str | None = None
    aggregation: Aggregation


class PivotRequest(BaseModel):
    """Configuración de un pivote para VER (paginado)."""
    # Filtro previo (mismo árbol del filtrado normal); None = todas las filas.
    filter: FilterGroup | None = None
    group_by: list[str] = Field(min_length=1, max_length=6)
    measures: list[Measure] = Field(min_length=1, max_length=10)
    # Cross-tab opcional: sus valores distintos se convierten en columnas.
    pivot_column: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class PivotResponse(BaseModel):
    columns: list[str]
    rows: list[dict]
    total_matched: int  # nº de filas agrupadas del reporte completo
    limit: int
    offset: int


class PivotSaveRequest(PivotRequest):
    """Pivote que se persiste como un dataset nuevo (reporte reutilizable)."""
    name: str = Field(min_length=1, max_length=120)


class ComputeRequest(BaseModel):
    """Configuración de columnas calculadas para VER (paginado)."""
    filter: FilterGroup | None = None
    columns: list[ComputedColumn] = Field(min_length=1, max_length=20)
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class ComputeSaveRequest(ComputeRequest):
    """Columnas calculadas que se persisten como un dataset nuevo."""
    name: str = Field(min_length=1, max_length=120)
