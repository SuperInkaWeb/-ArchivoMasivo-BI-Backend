"""DTOs de dataset: metadatos, columnas y estado de ingesta."""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class IngestStatus(str, Enum):
    PENDING = "pending"      # subido, aún no procesado
    PROCESSING = "processing"  # convirtiéndose a parquet
    READY = "ready"          # listo para filtrar/descargar
    FAILED = "failed"        # error de ingesta


class DatasetOrigin(str, Enum):
    """Cómo se creó el dataset (separa las dos pestañas del frontend)."""
    UPLOADED = "uploaded"  # subido por el usuario
    PIVOT = "pivot"        # generado por una tabla dinámica (reporte)


class ColumnInfo(BaseModel):
    name: str
    type: str  # tipo DuckDB (VARCHAR, BIGINT, DOUBLE, DATE, ...)


class DatasetSummary(BaseModel):
    """Vista resumida para listados (no expone rutas internas)."""
    id: str
    original_filename: str
    status: IngestStatus
    origin: DatasetOrigin = DatasetOrigin.UPLOADED
    row_count: int | None = None
    size_bytes: int
    created_at: datetime
    error: str | None = None


class DatasetDetail(DatasetSummary):
    """Detalle con esquema de columnas y hojas (si es Excel multi-hoja)."""
    columns: list[ColumnInfo] = Field(default_factory=list)
    sheets: list[str] = Field(default_factory=list)
    active_sheet: str | None = None


class UploadUrlRequest(BaseModel):
    """Petición para iniciar una subida: solo el nombre del archivo."""
    filename: str


class UploadTicket(BaseModel):
    """Respuesta con la URL a la que el navegador debe subir (PUT) el archivo.

    - direct_to_storage=true: `upload_url` es una URL prefirmada de R2 (subida directa).
    - direct_to_storage=false: `upload_url` es una ruta relativa del backend (modo local).
    """
    dataset: DatasetSummary
    upload_url: str
    direct_to_storage: bool


class SheetSelection(BaseModel):
    """Petición para re-ingerir un Excel usando una hoja específica."""
    sheet: str
