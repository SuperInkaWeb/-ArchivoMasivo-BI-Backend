"""Ingesta: convertir el archivo original a Parquet + registrar su esquema.

Funciona con disco local o con R2 (según configuración). Se ejecuta de forma
asíncrona (background task) porque convertir millones de filas puede tardar.
"""
import logging
import os
import tempfile

from app.core import storage
from app.core.config import get_settings
from app.repositories import dataset_repository as repo
from app.repositories import duckdb_engine
from app.repositories import r2_client
from app.repositories.duckdb_engine import EXCEL_EXTENSIONS, EncodingNotSupported
from app.schemas.dataset import IngestStatus

logger = logging.getLogger(__name__)

# Codificación de reserva para transcodificar archivos que DuckDB no lee nativamente.
# SUNAT exporta desde Windows: cp1252 mapea correctamente la puntuación del rango C1.
_FALLBACK_ENCODING = "cp1252"
_TRANSCODE_CHUNK = 1024 * 1024  # 1 MiB de caracteres por iteración (memoria plana)


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
    temp_files: list[str] = []  # temporales a limpiar (descargas de R2, transcodificados)

    repo.set_status(dataset_id, IngestStatus.PROCESSING)
    try:
        if is_excel:
            local_raw, temp = _local_raw_copy(dataset_id, extension, settings)
            _track(temp_files, temp)
            sheets = duckdb_engine.list_xlsx_sheets(local_raw)
            chosen = sheet if sheet in sheets else (sheets[0] if sheets else None)
            repo.set_sheets(dataset_id, sheets, chosen)
            columns, row_count = duckdb_engine.convert_to_parquet(
                local_raw, dest_locator, extension, chosen
            )
        else:
            columns, row_count = _ingest_csv(
                dataset_id, extension, dest_locator, settings, temp_files
            )
            _delete_raw(dataset_id, extension, settings)  # CSV/TXT: el crudo ya no se usa

        repo.set_ready(dataset_id, columns, row_count)
        logger.info("Ingesta OK: dataset %s (%d filas, hoja=%s)", dataset_id, row_count,
                    sheet if is_excel else "-")
    except Exception as exc:  # noqa: BLE001 - se registra completo, al cliente va mensaje corto
        logger.exception("Fallo de ingesta para dataset %s", dataset_id)
        repo.set_status(dataset_id, IngestStatus.FAILED, error=_short_error(exc))
    finally:
        for path in temp_files:
            if os.path.exists(path):
                os.unlink(path)


def _ingest_csv(
    dataset_id: str, extension: str, dest_locator: str, settings, temp_files: list[str]
) -> tuple[list, int]:
    """Convierte un CSV/TXT a Parquet, transcodificando a UTF-8 si DuckDB no lo lee.

    El primer intento lee el crudo tal cual (directo desde R2, sin descargar). Solo si
    la codificación no es soportada se descarga, se transcodifica a UTF-8 y se reintenta.
    """
    source_locator = storage.upload_locator(dataset_id, extension)
    try:
        return duckdb_engine.convert_to_parquet(source_locator, dest_locator, extension)
    except EncodingNotSupported:
        logger.info("Codificación no-UTF-8 en dataset %s: transcodificando a UTF-8", dataset_id)
        local_raw, temp = _local_raw_copy(dataset_id, extension, settings)
        _track(temp_files, temp)
        utf8_path = _transcode_to_utf8(local_raw)
        temp_files.append(utf8_path)
        return duckdb_engine.convert_to_parquet(utf8_path, dest_locator, extension)


def _transcode_to_utf8(source_path: str) -> str:
    """Reescribe un CSV/TXT Windows-1252 a UTF-8 en un temporal y devuelve su ruta.

    Lectura/escritura en streaming (cp1252 es de un solo byte, sin cortes de carácter).
    Los pocos bytes no definidos en cp1252 se reemplazan para no abortar la ingesta.
    """
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    handle.close()
    with open(source_path, "r", encoding=_FALLBACK_ENCODING, errors="replace", newline="") as reader, \
            open(handle.name, "w", encoding="utf-8", newline="") as writer:
        for chunk in iter(lambda: reader.read(_TRANSCODE_CHUNK), ""):
            writer.write(chunk)
    return handle.name


def _local_raw_copy(dataset_id: str, extension: str, settings) -> tuple[str, str | None]:
    """Devuelve (ruta_local, ruta_temporal_a_borrar). Para R2 descarga a un temporal."""
    if settings.use_r2:
        temp = r2_client.download_to_temp(storage.upload_key(dataset_id, extension), suffix=extension.lower())
        return temp, temp
    return str(storage.upload_path(dataset_id, extension)), None


def _track(temp_files: list[str], path: str | None) -> None:
    """Registra un temporal para limpiarlo al final (ignora rutas no temporales)."""
    if path:
        temp_files.append(path)


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
