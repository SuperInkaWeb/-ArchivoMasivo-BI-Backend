"""Ingesta: convertir un archivo subido a Parquet y registrar su esquema.

Se ejecuta de forma asíncrona (background task) porque convertir millones de
filas puede tardar y no debe bloquear la respuesta del upload.
"""
import logging

from app.core.storage import parquet_path, upload_path
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine
from app.schemas.dataset import IngestStatus

logger = logging.getLogger(__name__)


def run_ingest(dataset_id: str) -> None:
    """Procesa un dataset ya subido: archivo original -> Parquet + esquema.

    Actualiza el estado en el repositorio. Nunca propaga el stack al cliente:
    ante un fallo guarda un mensaje corto y marca el dataset como FAILED.
    """
    extension = repo.get_extension(dataset_id)
    if extension is None:
        logger.warning("Ingesta abortada: dataset %s inexistente", dataset_id)
        return

    source = upload_path(dataset_id, extension)
    dest = parquet_path(dataset_id)

    repo.set_status(dataset_id, IngestStatus.PROCESSING)
    try:
        columns, row_count = duckdb_engine.convert_to_parquet(source, dest)
        repo.set_ready(dataset_id, columns, row_count)
        logger.info("Ingesta OK: dataset %s (%d filas)", dataset_id, row_count)
    except Exception as exc:  # noqa: BLE001 - se registra completo, al cliente va mensaje corto
        logger.exception("Fallo de ingesta para dataset %s", dataset_id)
        repo.set_status(dataset_id, IngestStatus.FAILED, error=_short_error(exc))


def _short_error(exc: Exception) -> str:
    """Mensaje corto y sin datos sensibles para exponer al cliente."""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message[:300]
