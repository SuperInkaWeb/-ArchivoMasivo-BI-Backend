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

        # 0 filas = archivo vacío o solo-cabecera: inservible para filtrar (DuckDB inventa
        # una columna fantasma en archivos vacíos, por eso el signo fiable es el nº de filas).
        if not columns or row_count == 0:
            raise ValueError("El archivo no contiene filas de datos. Revisa que no esté vacío.")
        repo.set_ready(dataset_id, columns, row_count)
        logger.info("Ingesta OK: dataset %s (%d filas, hoja=%s)", dataset_id, row_count,
                    sheet if is_excel else "-")
    except Exception as exc:  # noqa: BLE001 - se registra completo, al cliente va mensaje corto
        logger.exception("Fallo de ingesta para dataset %s", dataset_id)
        repo.set_status(dataset_id, IngestStatus.FAILED, error=_friendly_error(exc))
    finally:
        for path in temp_files:
            if os.path.exists(path):
                os.unlink(path)


def _ingest_csv(
    dataset_id: str, extension: str, dest_locator: str, settings, temp_files: list[str]
) -> tuple[list, int]:
    """Convierte un CSV/TXT a Parquet, transcodificando a UTF-8 cuando hace falta.

    - UTF-16 (detectado por BOM): DuckDB lo leería como basura, así que se transcodifica
      siempre desde el origen.
    - Otras codificaciones: se intenta leer directo (rápido, sin descargar) y solo si
      DuckDB no puede (Windows-1252 con rango C1) se descarga y transcodifica.
    """
    wide_encoding = _sniff_wide_encoding(dataset_id, extension, settings)
    if wide_encoding is None:
        source_locator = storage.upload_locator(dataset_id, extension)
        try:
            return duckdb_engine.convert_to_parquet(source_locator, dest_locator, extension)
        except EncodingNotSupported:
            source_encoding = _FALLBACK_ENCODING
            logger.info("Dataset %s: Windows-1252 detectado, transcodificando a UTF-8", dataset_id)
    else:
        source_encoding = wide_encoding
        logger.info("Dataset %s: UTF-16 detectado, transcodificando a UTF-8", dataset_id)

    local_raw, temp = _local_raw_copy(dataset_id, extension, settings)
    _track(temp_files, temp)
    utf8_path = _transcode_to_utf8(local_raw, source_encoding)
    temp_files.append(utf8_path)
    return duckdb_engine.convert_to_parquet(utf8_path, dest_locator, extension)


def _sniff_wide_encoding(dataset_id: str, extension: str, settings) -> str | None:
    """Devuelve 'utf-16' si el archivo trae BOM UTF-16 (que DuckDB leería como basura)."""
    prefix = _read_prefix(dataset_id, extension, settings, length=2)
    if prefix[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"  # el propio BOM indica el endianness; Python lo consume al leer
    return None


def _read_prefix(dataset_id: str, extension: str, settings, length: int) -> bytes:
    """Primeros bytes del crudo (para detectar el BOM sin descargar todo el archivo)."""
    if settings.use_r2:
        return r2_client.read_prefix(storage.upload_key(dataset_id, extension), length)
    with open(storage.upload_path(dataset_id, extension), "rb") as raw:
        return raw.read(length)


def _transcode_to_utf8(source_path: str, source_encoding: str) -> str:
    """Reescribe un CSV/TXT (Windows-1252 o UTF-16) a UTF-8 en un temporal.

    Lectura/escritura en streaming. Los bytes no representables se reemplazan para no
    abortar la ingesta por un carácter aislado. Para UTF-16, Python consume el BOM.
    """
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    handle.close()
    with open(source_path, "r", encoding=source_encoding, errors="replace", newline="") as reader, \
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


def _friendly_error(exc: Exception) -> str:
    """Traduce el fallo técnico a un mensaje accionable en español para el cliente.

    Nunca expone el stack ni rutas internas; para causas conocidas da una pista de qué
    corregir. Para lo demás, la primera línea del error (ya corta y sin datos sensibles).
    """
    text = str(exc).lower()
    if any(hint in text for hint in
           ("utf-8", "utf-16", "latin-1", "encoded", "encoding", "unicode", "byte sequence")):
        return ("No se pudo determinar la codificación del archivo. Vuelve a guardarlo como "
                "UTF-8 (o CSV UTF-8) e inténtalo de nuevo.")
    if any(hint in text for hint in
           ("delimiter", "sniffing", "columns", "expected", "csv error", "unterminated", "quote")):
        return ("El archivo tiene filas con distinto número de columnas o un formato "
                "inesperado. Revisa que todas las filas usen el mismo separador y número de "
                "columnas.")
    if any(hint in text for hint in ("xlsx", "zip", "sheet", "excel")):
        return ("No se pudo leer el archivo Excel. Puede estar dañado, protegido con "
                "contraseña o en un formato no compatible. Guárdalo de nuevo como .xlsx.")
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message[:300]
