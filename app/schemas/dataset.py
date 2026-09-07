"""DTOs de dataset: metadatos, columnas y estado de ingesta."""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class IngestStatus(str, Enum):
    PENDING = "pending"      # subido, aún no procesado
    PROCESSING = "processing"  # convirtiéndose a parquet
    READY = "ready"          # listo para filtrar/descargar
    FAILED = "failed"        # error de ingesta


class ColumnInfo(BaseModel):
    name: str
    type: str  # tipo DuckDB (VARCHAR, BIGINT, DOUBLE, DATE, ...)


class DatasetSummary(BaseModel):
    """Vista resumida para listados (no expone rutas internas)."""
    id: str
    original_filename: str
    status: IngestStatus
    row_count: int | None = None
    size_bytes: int
    created_at: datetime
    error: str | None = None


class DatasetDetail(DatasetSummary):
    """Detalle con esquema de columnas."""
    columns: list[ColumnInfo] = Field(default_factory=list)


class UploadResult(BaseModel):
    """Respuesta al subir uno o varios archivos."""
    datasets: list[DatasetSummary]
