"""Ingesta: convertir el archivo original a Parquet + registrar su esquema.

Funciona con disco local o con R2 (según configuración). Se ejecuta de forma
asíncrona (background task) porque convertir millones de filas puede tardar.
"""
import logging
import os

from app.core import storage
from app.core.config import get_settings
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine
from app.repositories import r2_client
from app.repositories.duckdb_engine import EXCEL_EXTENSIONS
from app.schemas.dataset import IngestStatus

logger = logging.getLogger(__name__)


def run_ingest(dataset_id: str, sheet: str | None = None) -> None:
    """Procesa un dataset ya subido: original -> Parquet + esquema.

    Para Excel detecta las hojas (descargando una copia local si está en R2) y usa
    la indicada o la primera. Nunca propaga el stack al cliente: ante un fallo guarda
    un mensaje corto y marca el dataset como FAILED.
    """
    extension = repo.get_extension(dataset_id)
    if extension is None:
        logger.warning("Ingesta abortada: dataset %s inexistente", dataset_id)
        return

    settings = get_settings()
    dest_locator = storage.parquet_locator(dataset_id)
    is_excel = extension.lower() in EXCEL_EXTENSIONS
    temp_local: str | None = None

    repo.set_status(dataset_id, IngestStatus.PROCESSING)
    try:
        if is_excel:
            local_raw, temp_local = _local_excel_copy(dataset_id, extension, settings)
            sheets = duckdb_engine.list_xlsx_sheets(local_raw)
            chosen = sheet if sheet in sheets else (sheets[0] if sheets else None)
            repo.set_sheets(dataset_id, sheets, chosen)
            columns, row_count = duckdb_engine.convert_to_parquet(
                local_raw, dest_locator, extension, chosen
            )
        else:
            source_locator = storage.upload_locator(dataset_id, extension)
            columns, row_count = duckdb_engine.convert_to_parquet(
                source_locator, dest_locator, extension
            )
            _delete_raw(dataset_id, extension, settings)  # CSV/TXT: el crudo ya no se usa

        repo.set_ready(dataset_id, columns, row_count)
        logger.info("Ingesta OK: dataset %s (%d filas, hoja=%s)", dataset_id, row_count,
                    sheet if is_excel else "-")
    except Exception as exc:  # noqa: BLE001 - se registra completo, al cliente va mensaje corto
        logger.exception("Fallo de ingesta para dataset %s", dataset_id)
        repo.set_status(dataset_id, IngestStatus.FAILED, error=_short_error(exc))
    finally:
        if temp_local and os.path.exists(temp_local):
            os.unlink(temp_local)


def _local_excel_copy(dataset_id: str, extension: str, settings) -> tuple[str, str | None]:
    """Devuelve (ruta_local, ruta_temporal_a_borrar). Para R2 descarga a un temporal."""
    if settings.use_r2:
        temp = r2_client.download_to_temp(storage.upload_key(dataset_id, extension), suffix=extension.lower())
        return temp, temp
    return str(storage.upload_path(dataset_id, extension)), None


def _delete_raw(dataset_id: str, extension: str, settings) -> None:
    """Borra el archivo original tras convertir (solo aplica a no-Excel)."""
    if settings.use_r2:
        try:
            r2_client.delete_object(storage.upload_key(dataset_id, extension))
        except Exception:  # noqa: BLE001 - limpieza best-effort
            logger.warning("No se pudo borrar el crudo en R2 del dataset %s", dataset_id)
    else:
        storage.upload_path(dataset_id, extension).unlink(missing_ok=True)


def _short_error(exc: Exception) -> str:
    """Mensaje corto y sin datos sensibles para exponer al cliente."""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message[:300]
