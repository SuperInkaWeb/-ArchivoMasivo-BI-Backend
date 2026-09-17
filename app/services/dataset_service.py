"""Orquestación de datasets: subir (2 pasos), listar, esquema, previsualizar y descargar.

La subida es en dos pasos para soportar archivos grandes:
  1. create_upload  -> devuelve una URL a la que el navegador sube el archivo
     (URL prefirmada de R2, o un endpoint local del backend en modo disco).
  2. confirm_uploaded -> el backend dispara la ingesta (conversión a Parquet).
"""
import uuid

import aiofiles
from fastapi import Request

from app.core.config import get_settings
from app.core.exceptions import DatasetNotFoundError, DatasetNotReadyError, InvalidSheetError
from app.core.query_builder import build_order_by, build_select, build_where, quote_column
from app.core.storage import (
    parquet_key,
    parquet_locator,
    parquet_path,
    upload_key,
    upload_locator,
    upload_path,
)
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine, r2_client
from app.schemas.dataset import (
    DatasetDetail,
    DatasetSummary,
    IngestStatus,
    UploadTicket,
)
from app.schemas.filter import DistinctValuesResponse, PreviewRequest, PreviewResponse

_DISTINCT_VALUES_LIMIT = 500  # tope de valores en el desplegable tipo Excel

_ALLOWED_EXTENSIONS = {".csv", ".txt", ".xlsx"}
# .xls (Excel 97-2003, binario BIFF) no lo lee el motor (read_xlsx solo abre OOXML/.xlsx).
_LEGACY_EXCEL_EXTENSIONS = {".xls"}
_CHUNK_SIZE = 1024 * 1024  # 1 MB por bloque al guardar en disco (modo local)


class UnsupportedFileTypeError(Exception):
    """Extensión de archivo no permitida."""


class FileTooLargeError(Exception):
    """El archivo supera el tamaño máximo permitido."""


class UploadIncompleteError(Exception):
    """El objeto no llegó al almacenamiento (la subida del navegador no se completó)."""


def _extension_of(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


# ---------------------------------------------------------------------------
# Subida en dos pasos
# ---------------------------------------------------------------------------

def create_upload(filename: str, owner_id: str) -> UploadTicket:
    """Paso 1: valida, crea el registro PENDING y devuelve a dónde subir el archivo."""
    extension = _extension_of(filename)
    if extension in _LEGACY_EXCEL_EXTENSIONS:
        raise UnsupportedFileTypeError(
            "El formato .xls (Excel antiguo) no es compatible. Ábrelo en Excel y guárdalo "
            "como .xlsx (o .csv), luego vuelve a subirlo."
        )
    if extension not in _ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(f"Tipo no permitido: {extension or 'desconocido'}")

    dataset_id = uuid.uuid4().hex
    repo.create(dataset_id, owner_id, filename, extension, size_bytes=0)

    if get_settings().use_r2:
        upload_url = r2_client.presign_put(upload_key(dataset_id, extension))
        direct = True
    else:
        upload_url = f"/datasets/{dataset_id}/raw"  # el navegador lo resuelve contra la API
        direct = False

    return UploadTicket(
        dataset=repo.get_detail(dataset_id, owner_id),
        upload_url=upload_url,
        direct_to_storage=direct,
    )


async def save_raw_local(dataset_id: str, owner_id: str, request: Request) -> None:
    """Modo local: recibe el cuerpo del PUT y lo guarda en disco (streaming)."""
    detail = get_dataset(dataset_id, owner_id)  # valida propiedad
    extension = _extension_of(detail.original_filename)
    settings = get_settings()
    destination = upload_path(dataset_id, extension)

    written = 0
    async with aiofiles.open(destination, "wb") as out:
        async for chunk in request.stream():
            if not chunk:
                continue
            written += len(chunk)
            if written > settings.max_upload_bytes:
                await out.close()
                destination.unlink(missing_ok=True)
                raise FileTooLargeError(f"El archivo supera {settings.max_upload_mb} MB")
            await out.write(chunk)
    repo.set_size(dataset_id, written)


def confirm_uploaded(dataset_id: str, owner_id: str) -> DatasetSummary:
    """Paso 2: valida la subida y (para R2) registra el tamaño. El router dispara la ingesta.

    En modo R2 el navegador sube directo al bucket (sin pasar por el backend), así que aquí
    es donde se comprueba que el objeto llegó y que no excede el límite (OWASP A04).
    """
    detail = get_dataset(dataset_id, owner_id)
    settings = get_settings()
    if settings.use_r2:
        key = upload_key(dataset_id, _extension_of(detail.original_filename))
        size = r2_client.object_size(key)
        if size is None:
            raise UploadIncompleteError("La subida no se completó. Vuelve a intentarlo.")
        if size > settings.max_upload_bytes:
            _r2_delete_quiet(key)
            raise FileTooLargeError(f"El archivo supera el límite de {settings.max_upload_mb} MB.")
        repo.set_size(dataset_id, size)
    return repo.get_detail(dataset_id, owner_id)


# ---------------------------------------------------------------------------
# Lectura / gestión
# ---------------------------------------------------------------------------

def list_datasets(owner_id: str) -> list[DatasetSummary]:
    return repo.list_all(owner_id)


def get_dataset(dataset_id: str, owner_id: str) -> DatasetDetail:
    detail = repo.get_detail(dataset_id, owner_id)
    if detail is None:
        raise DatasetNotFoundError(dataset_id)
    return detail


def _require_ready(dataset_id: str, owner_id: str) -> DatasetDetail:
    detail = get_dataset(dataset_id, owner_id)
    if detail.status is not IngestStatus.READY:
        raise DatasetNotReadyError(f"Dataset en estado '{detail.status.value}'")
    return detail


def delete_dataset(dataset_id: str, owner_id: str) -> None:
    detail = repo.get_detail(dataset_id, owner_id)
    if detail is None:
        raise DatasetNotFoundError(dataset_id)
    extension = repo.get_extension(dataset_id)
    if get_settings().use_r2:
        if extension:
            _r2_delete_quiet(upload_key(dataset_id, extension))
        _r2_delete_quiet(parquet_key(dataset_id))
    else:
        if extension:
            upload_path(dataset_id, extension).unlink(missing_ok=True)
        parquet_path(dataset_id).unlink(missing_ok=True)
    repo.delete(dataset_id, owner_id)


def _r2_delete_quiet(key: str) -> None:
    try:
        r2_client.delete_object(key)
    except Exception:  # noqa: BLE001 - limpieza best-effort
        pass


def preview(dataset_id: str, request: PreviewRequest, owner_id: str) -> PreviewResponse:
    """Aplica filtros y devuelve una página de resultados + total de coincidencias."""
    detail = _require_ready(dataset_id, owner_id)
    valid_columns = {col.name for col in detail.columns}

    where_sql, params = build_where(request.filter, valid_columns)
    select_sql = build_select(request.select, valid_columns)
    order_sql = build_order_by(request.sort, valid_columns)
    parquet = parquet_locator(dataset_id)

    column_names, rows = duckdb_engine.preview(
        parquet, select_sql, where_sql, order_sql, request.limit, request.offset, params
    )
    total = duckdb_engine.count_matches(parquet, where_sql, params)
    return PreviewResponse(
        columns=column_names,
        rows=rows,
        total_matched=total,
        limit=request.limit,
        offset=request.offset,
    )


def distinct_values(
    dataset_id: str, column: str, search: str | None, owner_id: str
) -> DistinctValuesResponse:
    """Valores únicos de una columna para el filtro tipo Excel (casillas)."""
    detail = _require_ready(dataset_id, owner_id)
    valid_columns = {col.name for col in detail.columns}
    column_sql = quote_column(column, valid_columns)  # valida contra whitelist
    values, truncated = duckdb_engine.distinct_values(
        parquet_locator(dataset_id), column_sql, search, _DISTINCT_VALUES_LIMIT
    )
    return DistinctValuesResponse(
        column=column,
        values=[str(value) for value in values],
        truncated=truncated,
    )


def request_sheet_change(dataset_id: str, sheet: str, owner_id: str) -> None:
    """Valida el cambio de hoja de Excel y marca el dataset como en proceso.

    La re-ingesta real se dispara en segundo plano desde el router.
    """
    detail = get_dataset(dataset_id, owner_id)
    if sheet not in detail.sheets:
        raise InvalidSheetError(f"La hoja '{sheet}' no existe en el archivo.")
    repo.set_status(dataset_id, IngestStatus.PROCESSING)
