"""Orquestación de datasets: subir, listar, obtener esquema y previsualizar filtrado."""
import uuid

import aiofiles
from fastapi import UploadFile

from app.core.config import get_settings
from app.core.exceptions import DatasetNotFoundError, DatasetNotReadyError
from app.core.query_builder import build_order_by, build_select, build_where
from app.core.storage import parquet_path, upload_path
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine
from app.schemas.dataset import DatasetDetail, DatasetSummary, IngestStatus
from app.schemas.filter import PreviewRequest, PreviewResponse

_ALLOWED_EXTENSIONS = {".csv", ".txt", ".xlsx", ".xls"}
_CHUNK_SIZE = 1024 * 1024  # 1 MB por bloque al guardar en disco


class UnsupportedFileTypeError(Exception):
    """Extensión de archivo no permitida."""


class FileTooLargeError(Exception):
    """El archivo supera el tamaño máximo permitido."""


def _extension_of(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


async def save_upload(file: UploadFile) -> DatasetSummary:
    """Guarda un archivo subido en disco (streaming) y crea su registro PENDING.

    Valida extensión y tamaño. El ID es un UUID del servidor: el nombre original
    del usuario nunca se usa para construir rutas (mitiga Path Traversal).
    """
    original_name = file.filename or "sin_nombre"
    extension = _extension_of(original_name)
    if extension not in _ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(f"Tipo no permitido: {extension or 'desconocido'}")

    settings = get_settings()
    dataset_id = uuid.uuid4().hex
    destination = upload_path(dataset_id, extension)

    written = 0
    async with aiofiles.open(destination, "wb") as out:
        while chunk := await file.read(_CHUNK_SIZE):
            written += len(chunk)
            if written > settings.max_upload_bytes:
                await out.close()
                destination.unlink(missing_ok=True)
                raise FileTooLargeError(f"El archivo supera {settings.max_upload_mb} MB")
            await out.write(chunk)

    repo.create(dataset_id, original_name, extension, written)
    return repo.get_detail(dataset_id)  # PENDING recién creado


def list_datasets() -> list[DatasetSummary]:
    return repo.list_all()


def get_dataset(dataset_id: str) -> DatasetDetail:
    detail = repo.get_detail(dataset_id)
    if detail is None:
        raise DatasetNotFoundError(dataset_id)
    return detail


def _require_ready(dataset_id: str) -> DatasetDetail:
    detail = get_dataset(dataset_id)
    if detail.status is not IngestStatus.READY:
        raise DatasetNotReadyError(f"Dataset en estado '{detail.status.value}'")
    return detail


def delete_dataset(dataset_id: str) -> None:
    if repo.get_detail(dataset_id) is None:
        raise DatasetNotFoundError(dataset_id)
    extension = repo.get_extension(dataset_id)
    if extension:
        upload_path(dataset_id, extension).unlink(missing_ok=True)
    parquet_path(dataset_id).unlink(missing_ok=True)
    repo.delete(dataset_id)


def preview(dataset_id: str, request: PreviewRequest) -> PreviewResponse:
    """Aplica filtros y devuelve una página de resultados + total de coincidencias."""
    detail = _require_ready(dataset_id)
    valid_columns = {col.name for col in detail.columns}

    where_sql, params = build_where(request.conditions, request.combinator, valid_columns)
    select_sql = build_select(request.select, valid_columns)
    order_sql = build_order_by(request.sort, valid_columns)
    parquet = parquet_path(dataset_id)

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
