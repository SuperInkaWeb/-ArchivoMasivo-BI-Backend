"""Ingesta: convertir un archivo subido a Parquet y registrar su esquema.

Se ejecuta de forma asíncrona (background task) porque convertir millones de
filas puede tardar y no debe bloquear la respuesta del upload.
"""
import logging

from app.core.storage import parquet_path, upload_path
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine
from app.repositories.duckdb_engine import EXCEL_EXTENSIONS
from app.schemas.dataset import IngestStatus

logger = logging.getLogger(__name__)


def run_ingest(dataset_id: str, sheet: str | None = None) -> None:
    """Procesa un dataset ya subido: archivo original -> Parquet + esquema.

    Para Excel, detecta las hojas y usa la indicada (o la primera). Actualiza el
    estado en el repositorio. Nunca propaga el stack al cliente: ante un fallo
    guarda un mensaje corto y marca el dataset como FAILED.
    """
    extension = repo.get_extension(dataset_id)
    if extension is None:
        logger.warning("Ingesta abortada: dataset %s inexistente", dataset_id)
        return

    source = upload_path(dataset_id, extension)
    dest = parquet_path(dataset_id)

    repo.set_status(dataset_id, IngestStatus.PROCESSING)
    try:
        chosen_sheet = _resolve_sheet(dataset_id, source, extension, sheet)
        columns, row_count = duckdb_engine.convert_to_parquet(source, dest, sheet=chosen_sheet)
        repo.set_ready(dataset_id, columns, row_count)
        logger.info("Ingesta OK: dataset %s (%d filas, hoja=%s)", dataset_id, row_count, chosen_sheet)
    except Exception as exc:  # noqa: BLE001 - se registra completo, al cliente va mensaje corto
        logger.exception("Fallo de ingesta para dataset %s", dataset_id)
        repo.set_status(dataset_id, IngestStatus.FAILED, error=_short_error(exc))


def _resolve_sheet(dataset_id: str, source, extension: str, requested: str | None) -> str | None:
    """Para Excel: registra las hojas disponibles y decide cuál ingerir."""
    if extension.lower() not in EXCEL_EXTENSIONS:
        return None
    sheets = duckdb_engine.list_xlsx_sheets(source)
    chosen = requested if requested in sheets else (sheets[0] if sheets else None)
    repo.set_sheets(dataset_id, sheets, chosen)
    return chosen


def _short_error(exc: Exception) -> str:
    """Mensaje corto y sin datos sensibles para exponer al cliente."""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message[:300]
