"""Orquestación de análisis: tablas dinámicas (pivote) y columnas calculadas.

Ambas ofrecen "ver" (paginado, sin persistir) y "guardar como dataset nuevo". El
guardado materializa el resultado a un Parquet y registra un dataset derivado
(origin=pivot|computed) en segundo plano, reutilizando la máquina de estados
PROCESSING->READY y el sondeo del frontend (igual que la ingesta normal).

Un dataset derivado es uno más: se puede filtrar, previsualizar y descargar
(CSV/TXT/XLSX) con los endpoints existentes.
"""
import logging
import uuid
from typing import Callable

from app.core.config import get_settings
from app.core.exceptions import DatasetNotReadyError
from app.core.dedupe_builder import build_dedupe
from app.core.expression_builder import build_expression
from app.core.pivot_builder import MAX_PIVOT_COLUMNS, build_pivot, build_pivot_totals
from app.core.query_builder import InvalidFilterError, build_where, quote_column
from app.core.replace_builder import build_replace_select
from app.core.storage import parquet_key, parquet_locator, parquet_path
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine, r2_client
from app.schemas.analysis import (
    ComputeDownloadRequest,
    ComputeRequest,
    ComputeSaveRequest,
    ComputeSpec,
    DedupeDownloadRequest,
    DedupeRequest,
    DedupeResponse,
    DedupeSaveRequest,
    DedupeSpec,
    PivotDownloadRequest,
    PivotRequest,
    PivotResponse,
    PivotSaveRequest,
    PivotSpec,
    ReplaceDownloadRequest,
    ReplaceRequest,
    ReplaceSaveRequest,
    ReplaceSpec,
)
from app.schemas.dataset import DatasetDetail, DatasetOrigin, DatasetSummary, IngestStatus
from app.schemas.filter import DownloadFormat, PreviewResponse
from app.services import dataset_service, export_service

logger = logging.getLogger(__name__)

# Extensión lógica de un dataset derivado: sus datos ya viven en Parquet (sin crudo).
_DERIVED_EXTENSION = ".parquet"


# ---------------------------------------------------------------------------
# Tablas dinámicas (pivote)
# ---------------------------------------------------------------------------

def run_pivot(dataset_id: str, request: PivotRequest, owner_id: str) -> PivotResponse:
    """Ejecuta el pivote y devuelve una página del reporte + total de filas agrupadas.

    Incluye la fila de Total general (métricas sobre todas las filas) para fijarla al pie
    de la vista previa; no se persiste ni se descarga (solo la columna Total por fila sí).
    """
    _, valid_columns, numeric_columns, where_sql, where_params, pivot_values = _pivot_context(
        dataset_id, request, owner_id
    )
    select_sql, group_cols_sql, order_sql, params = build_pivot(
        request, valid_columns, numeric_columns, pivot_values
    )
    parquet = parquet_locator(dataset_id)
    total = duckdb_engine.pivot_count(parquet, group_cols_sql, where_sql, where_params)
    columns, rows = duckdb_engine.pivot_preview(
        parquet, select_sql, group_cols_sql, where_sql, params + where_params,
        request.limit, request.offset, order_sql,
    )
    totals_select, totals_params = build_pivot_totals(
        request, valid_columns, numeric_columns, pivot_values
    )
    totals = duckdb_engine.pivot_totals(
        parquet, totals_select, where_sql, totals_params + where_params
    )
    return PivotResponse(
        columns=columns, rows=rows, total_matched=total,
        limit=request.limit, offset=request.offset, totals=totals,
    )


def start_pivot_save(dataset_id: str, request: PivotSaveRequest, owner_id: str) -> DatasetSummary:
    """Crea el registro del reporte en PROCESSING y lo devuelve. El router lanza la tarea."""
    dataset_service.get_dataset(dataset_id, owner_id)  # valida propiedad/existencia del origen
    return _start_derived(owner_id, request.name.strip(), DatasetOrigin.PIVOT)


def run_pivot_save(new_id: str, source_id: str, request: PivotSaveRequest, owner_id: str) -> None:
    """Tarea en segundo plano: materializa el pivote a Parquet y marca READY/FAILED."""
    def materialize() -> None:
        select_sql, group_cols_sql, order_sql, where_sql, params, where_params = _plan_pivot(
            source_id, request, owner_id
        )
        duckdb_engine.pivot_to_parquet(
            parquet_locator(source_id), select_sql, group_cols_sql, where_sql,
            params + where_params, parquet_locator(new_id), order_sql,
        )
    _finalize_derived(new_id, source_id, materialize)


def export_pivot(dataset_id: str, request: PivotDownloadRequest, owner_id: str) -> export_service.ExportResult:
    """Descarga el reporte pivote COMPLETO como archivo (CSV/XLSX/TXT), sin persistirlo."""
    detail, valid_columns, numeric_columns, where_sql, where_params, pivot_values = _pivot_context(
        dataset_id, request, owner_id
    )
    select_sql, group_cols_sql, order_sql, params = build_pivot(
        request, valid_columns, numeric_columns, pivot_values
    )
    parquet = parquet_locator(dataset_id)
    matched = duckdb_engine.pivot_count(parquet, group_cols_sql, where_sql, where_params)
    delimiter = request.delimiter.char if request.format is DownloadFormat.TXT else None
    return export_service.write_export(
        dataset_id, detail.original_filename, request.format, matched, "reporte",
        lambda dest: duckdb_engine.pivot_export_to_file(
            parquet, select_sql, group_cols_sql, where_sql,
            params + where_params, dest, request.format.value, delimiter, order_sql,
        ),
    )


def _pivot_context(
    dataset_id: str, request: PivotSpec, owner_id: str
) -> tuple[DatasetDetail, set[str], set[str], str, list, list | None]:
    """Piezas comunes del pivote, calculadas una sola vez (DRY entre ver/guardar/descargar).

    Devuelve (detalle, columnas_válidas, columnas_numéricas, where_sql, where_params,
    valores_del_cross_tab).
    """
    detail, valid_columns = _require_ready_columns(dataset_id, owner_id)
    numeric_columns = _numeric_columns(detail)
    where_sql, where_params = build_where(request.filter, valid_columns)
    pivot_values = _distinct_pivot_values(dataset_id, request, valid_columns)
    return detail, valid_columns, numeric_columns, where_sql, where_params, pivot_values


def _plan_pivot(
    dataset_id: str, request: PivotSpec, owner_id: str
) -> tuple[str, str, str | None, str, list, list]:
    """Valida y arma el SQL del pivote.

    Devuelve (select, group_cols, order_sql, where, params, where_params).
    """
    _, valid_columns, numeric_columns, where_sql, where_params, pivot_values = _pivot_context(
        dataset_id, request, owner_id
    )
    select_sql, group_cols_sql, order_sql, params = build_pivot(
        request, valid_columns, numeric_columns, pivot_values
    )
    return select_sql, group_cols_sql, order_sql, where_sql, params, where_params


def _distinct_pivot_values(dataset_id: str, request: PivotSpec, valid_columns: set[str]) -> list | None:
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


# Subcadenas de tipo DuckDB que identifican columnas numéricas (INT cubre TINYINT..HUGEINT).
_NUMERIC_TYPE_HINTS = ("INT", "DECIMAL", "DOUBLE", "FLOAT", "REAL", "NUMERIC")


def _numeric_columns(detail: DatasetDetail) -> set[str]:
    """Nombres de columnas numéricas del dataset (para validar SUMA/PROMEDIO)."""
    return {
        column.name
        for column in detail.columns
        if any(hint in column.type.upper() for hint in _NUMERIC_TYPE_HINTS)
    }


# ---------------------------------------------------------------------------
# Columnas calculadas
# ---------------------------------------------------------------------------

def run_compute(dataset_id: str, request: ComputeRequest, owner_id: str) -> PreviewResponse:
    """Aplica las columnas calculadas y devuelve una página (todas las originales + nuevas)."""
    _, valid_columns = _require_ready_columns(dataset_id, owner_id)
    where_sql, where_params = build_where(request.filter, valid_columns)
    select_sql, compute_params = _compute_select(request, valid_columns)
    parquet = parquet_locator(dataset_id)
    total = duckdb_engine.count_matches(parquet, where_sql, where_params)
    columns, rows = duckdb_engine.preview(
        parquet, select_sql, where_sql, "", request.limit, request.offset,
        compute_params + where_params,
    )
    return PreviewResponse(
        columns=columns, rows=rows, total_matched=total,
        limit=request.limit, offset=request.offset,
    )


def start_compute_save(dataset_id: str, request: ComputeSaveRequest, owner_id: str) -> DatasetSummary:
    """Crea el registro del dataset con columnas calculadas en PROCESSING y lo devuelve."""
    dataset_service.get_dataset(dataset_id, owner_id)
    return _start_derived(owner_id, request.name.strip(), DatasetOrigin.COMPUTED)


def run_compute_save(new_id: str, source_id: str, request: ComputeSaveRequest, owner_id: str) -> None:
    """Tarea en segundo plano: materializa las columnas calculadas y marca READY/FAILED."""
    def materialize() -> None:
        _, valid_columns = _require_ready_columns(source_id, owner_id)
        where_sql, where_params = build_where(request.filter, valid_columns)
        select_sql, compute_params = _compute_select(request, valid_columns)
        duckdb_engine.materialize_to_parquet(
            parquet_locator(source_id), select_sql, where_sql,
            compute_params + where_params, parquet_locator(new_id),
        )
    _finalize_derived(new_id, source_id, materialize)


def export_compute(dataset_id: str, request: ComputeDownloadRequest, owner_id: str) -> export_service.ExportResult:
    """Descarga TODAS las filas con las columnas calculadas como archivo (CSV/XLSX/TXT)."""
    detail, valid_columns = _require_ready_columns(dataset_id, owner_id)
    where_sql, where_params = build_where(request.filter, valid_columns)
    select_sql, compute_params = _compute_select(request, valid_columns)
    parquet = parquet_locator(dataset_id)
    matched = duckdb_engine.count_matches(parquet, where_sql, where_params)
    delimiter = request.delimiter.char if request.format is DownloadFormat.TXT else None
    return export_service.write_export(
        dataset_id, detail.original_filename, request.format, matched, "columnas",
        lambda dest: duckdb_engine.export_to_file(
            parquet, select_sql, where_sql, "", compute_params + where_params,
            dest, request.format.value, delimiter,
        ),
    )


def _compute_select(request: ComputeSpec, valid_columns: set[str]) -> tuple[str, list]:
    """Arma 'SELECT *, expr AS "alias", ...' validando nombres y expresiones."""
    pieces = ["*"]
    params: list = []
    seen: set[str] = set()
    for column in request.columns:
        alias = column.name.strip()
        if not alias:
            raise InvalidFilterError("El nombre de la columna calculada no puede estar vacío.")
        if alias in valid_columns:
            raise InvalidFilterError(f"Ya existe una columna llamada '{alias}'.")
        if alias in seen:
            raise InvalidFilterError(f"Nombre de columna calculada repetido: '{alias}'.")
        seen.add(alias)
        expr_sql, expr_params = build_expression(column.expression, valid_columns)
        pieces.append(f'{expr_sql} AS {_quote_alias(alias)}')
        params.extend(expr_params)
    return ", ".join(pieces), params


def _quote_alias(alias: str) -> str:
    return '"' + alias.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Buscar y reemplazar por columna
# ---------------------------------------------------------------------------

def run_replace(dataset_id: str, request: ReplaceRequest, owner_id: str) -> PreviewResponse:
    """Aplica las correcciones y devuelve una página (todas las columnas, ya corregidas)."""
    select_sql, where_sql, params, where_params = _plan_replace(dataset_id, request, owner_id)
    parquet = parquet_locator(dataset_id)
    total = duckdb_engine.count_matches(parquet, where_sql, where_params)
    columns, rows = duckdb_engine.preview(
        parquet, select_sql, where_sql, "", request.limit, request.offset, params + where_params,
    )
    return PreviewResponse(
        columns=columns, rows=rows, total_matched=total,
        limit=request.limit, offset=request.offset,
    )


def start_replace_save(dataset_id: str, request: ReplaceSaveRequest, owner_id: str) -> DatasetSummary:
    """Crea el registro del dataset corregido en PROCESSING y lo devuelve."""
    dataset_service.get_dataset(dataset_id, owner_id)
    return _start_derived(owner_id, request.name.strip(), DatasetOrigin.REPLACED)


def run_replace_save(new_id: str, source_id: str, request: ReplaceSaveRequest, owner_id: str) -> None:
    """Tarea en segundo plano: materializa las correcciones y marca READY/FAILED."""
    def materialize() -> None:
        select_sql, where_sql, params, where_params = _plan_replace(source_id, request, owner_id)
        duckdb_engine.materialize_to_parquet(
            parquet_locator(source_id), select_sql, where_sql,
            params + where_params, parquet_locator(new_id),
        )
    _finalize_derived(new_id, source_id, materialize)


def export_replace(dataset_id: str, request: ReplaceDownloadRequest, owner_id: str) -> export_service.ExportResult:
    """Descarga TODAS las filas ya corregidas como archivo (CSV/XLSX/TXT)."""
    detail, _ = _require_ready_columns(dataset_id, owner_id)
    select_sql, where_sql, params, where_params = _plan_replace(dataset_id, request, owner_id)
    parquet = parquet_locator(dataset_id)
    matched = duckdb_engine.count_matches(parquet, where_sql, where_params)
    delimiter = request.delimiter.char if request.format is DownloadFormat.TXT else None
    return export_service.write_export(
        dataset_id, detail.original_filename, request.format, matched, "corregido",
        lambda dest: duckdb_engine.export_to_file(
            parquet, select_sql, where_sql, "", params + where_params,
            dest, request.format.value, delimiter,
        ),
    )


def _plan_replace(dataset_id: str, request: ReplaceSpec, owner_id: str) -> tuple[str, str, list, list]:
    """Valida y arma el SQL de corrección. Devuelve (select, where, params, where_params)."""
    detail, valid_columns = _require_ready_columns(dataset_id, owner_id)
    where_sql, where_params = build_where(request.filter, valid_columns)
    column_order = [column.name for column in detail.columns]
    select_sql, params = build_replace_select(request.replacements, column_order, valid_columns)
    return select_sql, where_sql, params, where_params


# ---------------------------------------------------------------------------
# Eliminar duplicados
# ---------------------------------------------------------------------------

def run_dedupe(dataset_id: str, request: DedupeRequest, owner_id: str) -> DedupeResponse:
    """Quita duplicados y devuelve una página + cuántas filas quedaron y cuántas había."""
    select_sql, where_sql, qualify_sql, where_params = _plan_dedupe(dataset_id, request, owner_id)
    parquet = parquet_locator(dataset_id)
    original = duckdb_engine.count_matches(parquet, where_sql, where_params)
    total = duckdb_engine.dedupe_count(parquet, select_sql, where_sql, qualify_sql, where_params)
    columns, rows = duckdb_engine.dedupe_preview(
        parquet, select_sql, where_sql, qualify_sql, where_params,
        request.limit, request.offset,
    )
    return DedupeResponse(
        columns=columns, rows=rows, total_matched=total, total_original=original,
        limit=request.limit, offset=request.offset,
    )


def start_dedupe_save(dataset_id: str, request: DedupeSaveRequest, owner_id: str) -> DatasetSummary:
    """Crea el registro del dataset sin duplicados en PROCESSING y lo devuelve."""
    dataset_service.get_dataset(dataset_id, owner_id)
    return _start_derived(owner_id, request.name.strip(), DatasetOrigin.DEDUPED)


def run_dedupe_save(new_id: str, source_id: str, request: DedupeSaveRequest, owner_id: str) -> None:
    """Tarea en segundo plano: materializa el resultado sin duplicados y marca READY/FAILED."""
    def materialize() -> None:
        select_sql, where_sql, qualify_sql, where_params = _plan_dedupe(source_id, request, owner_id)
        duckdb_engine.dedupe_to_parquet(
            parquet_locator(source_id), select_sql, where_sql, qualify_sql,
            where_params, parquet_locator(new_id),
        )
    _finalize_derived(new_id, source_id, materialize)


def export_dedupe(dataset_id: str, request: DedupeDownloadRequest, owner_id: str) -> export_service.ExportResult:
    """Descarga TODAS las filas sin duplicados como archivo (CSV/XLSX/TXT)."""
    detail, valid_columns = _require_ready_columns(dataset_id, owner_id)
    where_sql, where_params = build_where(request.filter, valid_columns)
    select_sql, qualify_sql = build_dedupe(request.key_columns, valid_columns)
    parquet = parquet_locator(dataset_id)
    matched = duckdb_engine.dedupe_count(parquet, select_sql, where_sql, qualify_sql, where_params)
    delimiter = request.delimiter.char if request.format is DownloadFormat.TXT else None
    return export_service.write_export(
        dataset_id, detail.original_filename, request.format, matched, "sin_duplicados",
        lambda dest: duckdb_engine.dedupe_export_to_file(
            parquet, select_sql, where_sql, qualify_sql, where_params,
            dest, request.format.value, delimiter,
        ),
    )


def _plan_dedupe(dataset_id: str, request: DedupeSpec, owner_id: str) -> tuple[str, str, str | None, list]:
    """Valida y arma el SQL de dedupe. Devuelve (select, where, qualify, where_params)."""
    _, valid_columns = _require_ready_columns(dataset_id, owner_id)
    where_sql, where_params = build_where(request.filter, valid_columns)
    select_sql, qualify_sql = build_dedupe(request.key_columns, valid_columns)
    return select_sql, where_sql, qualify_sql, where_params


# ---------------------------------------------------------------------------
# Infraestructura compartida de datasets derivados
# ---------------------------------------------------------------------------

def _require_ready_columns(dataset_id: str, owner_id: str) -> tuple[DatasetDetail, set[str]]:
    """Devuelve (detalle, columnas_válidas) de un dataset READY del usuario."""
    detail = dataset_service.get_dataset(dataset_id, owner_id)
    if detail.status is not IngestStatus.READY:
        raise DatasetNotReadyError(f"El dataset está en estado '{detail.status.value}'.")
    return detail, {col.name for col in detail.columns}


def _start_derived(owner_id: str, name: str, origin: DatasetOrigin) -> DatasetSummary:
    new_id = uuid.uuid4().hex
    repo.create(new_id, owner_id, name, _DERIVED_EXTENSION, size_bytes=0, origin=origin)
    repo.set_status(new_id, IngestStatus.PROCESSING)
    return repo.get_detail(new_id, owner_id)


def _finalize_derived(new_id: str, source_id: str, materialize: Callable[[], None]) -> None:
    """Ejecuta la materialización y marca READY/FAILED (patrón común pivote/compute)."""
    try:
        materialize()
        dest = parquet_locator(new_id)
        columns = duckdb_engine.get_schema(dest)
        row_count = duckdb_engine.count_matches(dest, "", [])
        if not columns or row_count == 0:
            raise ValueError("No se generó ninguna fila.")
        repo.set_ready(new_id, columns, row_count)
        repo.set_size(new_id, _parquet_size(new_id))
        logger.info("Dataset derivado %s listo (%d filas) desde %s", new_id, row_count, source_id)
    except Exception as exc:  # noqa: BLE001 - stack al log, mensaje corto al cliente
        logger.exception("Fallo al generar el dataset derivado %s desde %s", new_id, source_id)
        repo.set_status(new_id, IngestStatus.FAILED, error=_friendly_error(exc))


def _parquet_size(dataset_id: str) -> int:
    """Tamaño del Parquet generado (best-effort; 0 si no se puede medir)."""
    try:
        if get_settings().use_r2:
            return r2_client.object_size(parquet_key(dataset_id)) or 0
        return parquet_path(dataset_id).stat().st_size
    except Exception:  # noqa: BLE001 - el tamaño es informativo, no crítico
        return 0


def _friendly_error(exc: Exception) -> str:
    """Mensaje corto y sin internals para el cliente ante un fallo."""
    if isinstance(exc, (InvalidFilterError, DatasetNotReadyError)):
        return str(exc)
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:300]
