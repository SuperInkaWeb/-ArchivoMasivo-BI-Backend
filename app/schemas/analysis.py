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
from app.schemas.filter import Delimiter, DownloadFormat, FilterGroup


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


class PivotSpec(BaseModel):
    """Definición de un pivote (sin paginación ni destino).

    Base compartida por las tres operaciones: ver, guardar y descargar. Así los
    campos de configuración se declaran una sola vez (DRY).
    """
    # Filtro previo (mismo árbol del filtrado normal); None = todas las filas.
    filter: FilterGroup | None = None
    group_by: list[str] = Field(min_length=1, max_length=6)
    measures: list[Measure] = Field(min_length=1, max_length=10)
    # Cross-tab opcional: sus valores distintos se convierten en columnas.
    pivot_column: str | None = None


class PivotRequest(PivotSpec):
    """Pivote para VER (una página del reporte)."""
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class PivotResponse(BaseModel):
    columns: list[str]
    rows: list[dict]
    total_matched: int  # nº de filas agrupadas del reporte completo
    limit: int
    offset: int


class PivotSaveRequest(PivotSpec):
    """Pivote que se persiste como un dataset nuevo (reporte reutilizable)."""
    name: str = Field(min_length=1, max_length=120)


class PivotDownloadRequest(PivotSpec):
    """Pivote que se descarga como archivo (CSV/XLSX/TXT), sin persistirlo."""
    format: DownloadFormat = DownloadFormat.CSV
    delimiter: Delimiter = Delimiter.TAB  # solo aplica a TXT


class ComputeSpec(BaseModel):
    """Definición de columnas calculadas (sin paginación ni destino)."""
    filter: FilterGroup | None = None
    columns: list[ComputedColumn] = Field(min_length=1, max_length=20)


class ComputeRequest(ComputeSpec):
    """Columnas calculadas para VER (una página)."""
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class ComputeSaveRequest(ComputeSpec):
    """Columnas calculadas que se persisten como un dataset nuevo."""
    name: str = Field(min_length=1, max_length=120)


class ComputeDownloadRequest(ComputeSpec):
    """Columnas calculadas que se descargan como archivo (CSV/XLSX/TXT)."""
    format: DownloadFormat = DownloadFormat.CSV
    delimiter: Delimiter = Delimiter.TAB  # solo aplica a TXT
