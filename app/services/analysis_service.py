"""Orquestación de tablas dinámicas (pivote): ver y guardar como dataset.

- Ver: ejecuta el pivote y devuelve una página del reporte (paginado).
- Guardar: materializa el reporte a un Parquet nuevo y lo registra como un dataset
  derivado (origin=pivot). Se hace en segundo plano reutilizando la misma máquina de
  estados PROCESSING->READY y el sondeo del frontend (igual que la ingesta normal).

Un reporte guardado es un dataset como cualquiera: se puede filtrar, previsualizar y
descargar (CSV/TXT/XLSX) con los endpoints existentes.
"""
import logging
import uuid

from app.core.config import get_settings
from app.core.exceptions import DatasetNotReadyError
from app.core.pivot_builder import MAX_PIVOT_COLUMNS, build_pivot
from app.core.query_builder import InvalidFilterError, build_where, quote_column
from app.core.storage import parquet_key, parquet_locator, parquet_path
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine, r2_client
from app.schemas.analysis import PivotRequest, PivotResponse, PivotSaveRequest
from app.schemas.dataset import DatasetOrigin, DatasetSummary, IngestStatus
from app.services import dataset_service

logger = logging.getLogger(__name__)

# Extensión lógica de un reporte: sus datos ya viven en Parquet (no hay archivo crudo).
_PIVOT_EXTENSION = ".parquet"


def run_pivot(dataset_id: str, request: PivotRequest, owner_id: str) -> PivotResponse:
    """Ejecuta el pivote y devuelve una página del reporte + total de filas agrupadas."""
    select_sql, group_cols_sql, where_sql, params, where_params = _plan(dataset_id, request, owner_id)
    parquet = parquet_locator(dataset_id)
    total = duckdb_engine.pivot_count(parquet, group_cols_sql, where_sql, where_params)
    columns, rows = duckdb_engine.pivot_preview(
        parquet, select_sql, group_cols_sql, where_sql, params + where_params,
        request.limit, request.offset,
    )
    return PivotResponse(
        columns=columns, rows=rows, total_matched=total,
        limit=request.limit, offset=request.offset,
    )


def start_pivot_save(dataset_id: str, request: PivotSaveRequest, owner_id: str) -> DatasetSummary:
    """Crea el registro del reporte en PROCESSING y lo devuelve. El router lanza la tarea."""
    dataset_service.get_dataset(dataset_id, owner_id)  # valida propiedad/existencia del origen
    new_id = uuid.uuid4().hex
    repo.create(new_id, owner_id, request.name.strip(), _PIVOT_EXTENSION, size_bytes=0,
                origin=DatasetOrigin.PIVOT)
    repo.set_status(new_id, IngestStatus.PROCESSING)
    return repo.get_detail(new_id, owner_id)


def run_pivot_save(new_id: str, source_id: str, request: PivotSaveRequest, owner_id: str) -> None:
    """Tarea en segundo plano: materializa el reporte a Parquet y marca READY/FAILED."""
    try:
        select_sql, group_cols_sql, where_sql, params, where_params = _plan(source_id, request, owner_id)
        dest = parquet_locator(new_id)
        duckdb_engine.pivot_to_parquet(
            parquet_locator(source_id), select_sql, group_cols_sql, where_sql,
            params + where_params, dest,
        )
        columns = duckdb_engine.get_schema(dest)
        row_count = duckdb_engine.count_matches(dest, "", [])
        if not columns or row_count == 0:
            raise ValueError("El pivote no generó ninguna fila.")
        repo.set_ready(new_id, columns, row_count)
        repo.set_size(new_id, _parquet_size(new_id))
        logger.info("Pivote guardado: %s (%d filas) desde %s", new_id, row_count, source_id)
    except Exception as exc:  # noqa: BLE001 - stack al log, mensaje corto al cliente
        logger.exception("Fallo al guardar el pivote %s desde %s", new_id, source_id)
        repo.set_status(new_id, IngestStatus.FAILED, error=_friendly_error(exc))


def _plan(dataset_id: str, request: PivotRequest, owner_id: str) -> tuple[str, str, str, list, list]:
    """Valida y arma el SQL del pivote. Devuelve (select, group_cols, where, params, where_params)."""
    detail = dataset_service.get_dataset(dataset_id, owner_id)
    if detail.status is not IngestStatus.READY:
        raise DatasetNotReadyError(f"El dataset está en estado '{detail.status.value}'.")
    valid_columns = {col.name for col in detail.columns}

    where_sql, where_params = build_where(request.filter, valid_columns)
    pivot_values = _distinct_pivot_values(dataset_id, request, valid_columns)
    select_sql, group_cols_sql, params = build_pivot(request, valid_columns, pivot_values)
    return select_sql, group_cols_sql, where_sql, params, where_params


def _distinct_pivot_values(dataset_id: str, request: PivotRequest, valid_columns: set[str]) -> list | None:
    """Valores distintos de la columna de cross-tab (o None si no hay cross-tab)."""
    if request.pivot_column is None:
        return None
    column_sql = quote_column(request.pivot_column, valid_columns)
    values, truncated = duckdb_engine.distinct_values(
        parquet_locator(dataset_id), column_sql, None, MAX_PIVOT_COLUMNS
    )
    if truncated:
        raise InvalidFilterError(
            f"La columna '{request.pivot_column}' tiene demasiados valores distintos para "
            f"usarla como columnas (máximo {MAX_PIVOT_COLUMNS}). Filtra antes o elige otra."
        )
    return values


def _parquet_size(dataset_id: str) -> int:
    """Tamaño del Parquet generado (best-effort; 0 si no se puede medir)."""
    try:
        if get_settings().use_r2:
            return r2_client.object_size(parquet_key(dataset_id)) or 0
        return parquet_path(dataset_id).stat().st_size
    except Exception:  # noqa: BLE001 - el tamaño es informativo, no crítico
        return 0


def _friendly_error(exc: Exception) -> str:
    """Mensaje corto y sin internals para el cliente ante un fallo del pivote."""
    if isinstance(exc, (InvalidFilterError, DatasetNotReadyError)):
        return str(exc)
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:300]
