"""Exportación del resultado filtrado a un archivo descargable (CSV/XLSX).

Escribe a un archivo temporal vía COPY (streaming en DuckDB, memoria constante),
para luego transmitirlo. Aplica límites de recursos antes de generar nada.
"""
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import DatasetNotReadyError, DownloadTooLargeError
from app.core.query_builder import build_order_by, build_select, build_where
from app.core.storage import exports_dir, parquet_path
from app.repositories import duckdb_engine
from app.schemas.dataset import IngestStatus
from app.schemas.filter import DownloadFormat, DownloadRequest
from app.services.dataset_service import get_dataset

# Tope duro del formato XLSX: 1.048.576 filas por hoja (incluyendo cabecera).
_XLSX_MAX_DATA_ROWS = 1_048_575

_MEDIA_TYPES = {
    DownloadFormat.CSV: "text/csv",
    DownloadFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass
class ExportResult:
    path: Path
    media_type: str
    download_filename: str


def build_export(dataset_id: str, request: DownloadRequest) -> ExportResult:
    """Genera el archivo filtrado en disco y devuelve cómo transmitirlo."""
    detail = get_dataset(dataset_id)
    if detail.status is not IngestStatus.READY:
        raise DatasetNotReadyError(f"Dataset en estado '{detail.status.value}'")
    valid_columns = {col.name for col in detail.columns}

    where_sql, params = build_where(request.conditions, request.combinator, valid_columns)
    select_sql = build_select(request.select, valid_columns)
    order_sql = build_order_by(request.sort, valid_columns)
    parquet = parquet_path(dataset_id)

    matched = duckdb_engine.count_matches(parquet, where_sql, params)
    _guard_limits(matched, request.format)

    dest = exports_dir() / f"{dataset_id}_{uuid.uuid4().hex}.{request.format.value}"
    duckdb_engine.export_to_file(
        parquet, select_sql, where_sql, order_sql, params, dest, request.format.value
    )

    base_name = _sanitize_stem(detail.original_filename)
    return ExportResult(
        path=dest,
        media_type=_MEDIA_TYPES[request.format],
        download_filename=f"{base_name}_filtrado.{request.format.value}",
    )


def _guard_limits(matched: int, fmt: DownloadFormat) -> None:
    settings = get_settings()
    if matched > settings.max_download_rows:
        raise DownloadTooLargeError(
            f"El resultado tiene {matched} filas y supera el máximo de {settings.max_download_rows}."
        )
    if fmt is DownloadFormat.XLSX and matched > _XLSX_MAX_DATA_ROWS:
        raise DownloadTooLargeError(
            f"XLSX admite máximo {_XLSX_MAX_DATA_ROWS} filas; el resultado tiene {matched}. Usa CSV."
        )


def _sanitize_stem(filename: str) -> str:
    """Nombre base seguro para la descarga (sin ruta ni caracteres peligrosos)."""
    stem = Path(filename).stem
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem)
    return safe or "export"
