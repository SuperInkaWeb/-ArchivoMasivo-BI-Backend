"""Persistencia de metadatos de datasets.

Backend dual, elegido por configuración:
  - Postgres/Neon  cuando `DATABASE_URL` está definida (producción).
  - SQLite local   cuando no lo está (desarrollo, sin configuración).

Guarda solo metadatos ligeros (id, dueño, nombre, estado, esquema, hojas, conteo);
los datos masivos viven en Parquet. Siempre usa sentencias parametrizadas.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.storage import metadata_db_path
from app.schemas.dataset import ColumnInfo, DatasetDetail, DatasetSummary, IngestStatus

_DATABASE_URL = get_settings().database_url
IS_POSTGRES = bool(_DATABASE_URL)
_PH = "%s" if IS_POSTGRES else "?"  # marcador de parámetro según el driver

if IS_POSTGRES:  # pragma: no cover - depende del entorno
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    _pool: "ConnectionPool | None" = None

    def _get_pool() -> "ConnectionPool":
        global _pool
        if _pool is None:
            _pool = ConnectionPool(
                _DATABASE_URL,
                min_size=1,
                max_size=5,
                open=True,
                kwargs={"autocommit": True},
                # Neon cierra conexiones ociosas (autosuspende el compute). Validar y
                # reconectar antes de entregar la conexión evita el error
                # "SSL connection has been closed unexpectedly".
                check=ConnectionPool.check_connection,
                max_idle=120,
            )
        return _pool

# Tipos portables entre SQLite y Postgres (BIGINT tiene afinidad INTEGER en SQLite).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id                TEXT PRIMARY KEY,
    owner_id          TEXT,
    original_filename TEXT NOT NULL,
    extension         TEXT NOT NULL,
    status            TEXT NOT NULL,
    row_count         BIGINT,
    size_bytes        BIGINT NOT NULL,
    columns_json      TEXT NOT NULL DEFAULT '[]',
    sheets_json       TEXT NOT NULL DEFAULT '[]',
    active_sheet      TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
"""


@contextmanager
def _cursor():
    """Cursor unificado con filas accesibles por nombre (dict-like) en ambos drivers."""
    if IS_POSTGRES:  # pragma: no cover
        with _get_pool().connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                yield cursor
    else:
        connection = sqlite3.connect(metadata_db_path(), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection.cursor()
            connection.commit()
        finally:
            connection.close()


def init_db() -> None:
    with _cursor() as cursor:
        cursor.execute(_SCHEMA)
        _migrate(cursor)


def _migrate(cursor) -> None:
    """Añade columnas nuevas a bases existentes (idempotente, según dialecto)."""
    if IS_POSTGRES:  # pragma: no cover
        cursor.execute("ALTER TABLE datasets ADD COLUMN IF NOT EXISTS sheets_json TEXT NOT NULL DEFAULT '[]'")
        cursor.execute("ALTER TABLE datasets ADD COLUMN IF NOT EXISTS active_sheet TEXT")
        cursor.execute("ALTER TABLE datasets ADD COLUMN IF NOT EXISTS owner_id TEXT")
        return
    existing = {row["name"] for row in cursor.execute("PRAGMA table_info(datasets)").fetchall()}
    if "sheets_json" not in existing:
        cursor.execute("ALTER TABLE datasets ADD COLUMN sheets_json TEXT NOT NULL DEFAULT '[]'")
    if "active_sheet" not in existing:
        cursor.execute("ALTER TABLE datasets ADD COLUMN active_sheet TEXT")
    if "owner_id" not in existing:
        cursor.execute("ALTER TABLE datasets ADD COLUMN owner_id TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(
    dataset_id: str, owner_id: str, original_filename: str, extension: str, size_bytes: int
) -> None:
    now = _now()
    with _cursor() as cursor:
        cursor.execute(
            f"""INSERT INTO datasets
                (id, owner_id, original_filename, extension, status, size_bytes, columns_json, created_at, updated_at)
                VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, '[]', {_PH}, {_PH})""",
            (dataset_id, owner_id, original_filename, extension, IngestStatus.PENDING.value, size_bytes, now, now),
        )


def set_sheets(dataset_id: str, sheets: list[str], active_sheet: str | None) -> None:
    with _cursor() as cursor:
        cursor.execute(
            f"UPDATE datasets SET sheets_json = {_PH}, active_sheet = {_PH}, updated_at = {_PH} WHERE id = {_PH}",
            (json.dumps(sheets), active_sheet, _now(), dataset_id),
        )


def set_size(dataset_id: str, size_bytes: int) -> None:
    with _cursor() as cursor:
        cursor.execute(
            f"UPDATE datasets SET size_bytes = {_PH}, updated_at = {_PH} WHERE id = {_PH}",
            (size_bytes, _now(), dataset_id),
        )


def set_status(dataset_id: str, status: IngestStatus, error: str | None = None) -> None:
    with _cursor() as cursor:
        cursor.execute(
            f"UPDATE datasets SET status = {_PH}, error = {_PH}, updated_at = {_PH} WHERE id = {_PH}",
            (status.value, error, _now(), dataset_id),
        )


def set_ready(dataset_id: str, columns: list[ColumnInfo], row_count: int) -> None:
    columns_json = json.dumps([col.model_dump() for col in columns])
    with _cursor() as cursor:
        cursor.execute(
            f"""UPDATE datasets
                SET status = {_PH}, columns_json = {_PH}, row_count = {_PH}, error = NULL, updated_at = {_PH}
                WHERE id = {_PH}""",
            (IngestStatus.READY.value, columns_json, row_count, _now(), dataset_id),
        )


def _row_to_summary(row) -> DatasetSummary:
    return DatasetSummary(
        id=row["id"],
        original_filename=row["original_filename"],
        status=IngestStatus(row["status"]),
        row_count=row["row_count"],
        size_bytes=row["size_bytes"],
        created_at=datetime.fromisoformat(row["created_at"]),
        error=row["error"],
    )


def list_all(owner_id: str) -> list[DatasetSummary]:
    with _cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM datasets WHERE owner_id = {_PH} ORDER BY created_at DESC", (owner_id,)
        )
        rows = cursor.fetchall()
    return [_row_to_summary(row) for row in rows]


def get_detail(dataset_id: str, owner_id: str) -> DatasetDetail | None:
    with _cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM datasets WHERE id = {_PH} AND owner_id = {_PH}", (dataset_id, owner_id)
        )
        row = cursor.fetchone()
    if row is None:
        return None
    columns = [ColumnInfo(**col) for col in json.loads(row["columns_json"])]
    summary = _row_to_summary(row)
    return DatasetDetail(
        **summary.model_dump(),
        columns=columns,
        sheets=json.loads(row["sheets_json"]),
        active_sheet=row["active_sheet"],
    )


def get_extension(dataset_id: str) -> str | None:
    with _cursor() as cursor:
        cursor.execute(f"SELECT extension FROM datasets WHERE id = {_PH}", (dataset_id,))
        row = cursor.fetchone()
    return row["extension"] if row else None


_STALE_INGEST_MESSAGE = (
    "La conversión se interrumpió (posible reinicio del servidor). Vuelve a subir el archivo."
)


def reap_stale_processing(owner_id: str, older_than_seconds: int) -> int:
    """Marca como FAILED los datasets atascados en PROCESSING y devuelve cuántos reparó.

    Detecta ingestas cuyo proceso murió (crash / redeploy a mitad): quedarían en PROCESSING
    para siempre. Es seguro ante falsos positivos: una ingesta aún viva llamará a set_ready
    al terminar y volverá a READY. Compara `updated_at` en ISO-8601 UTC (orden lexicográfico
    == orden cronológico por formato uniforme).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
    with _cursor() as cursor:
        cursor.execute(
            f"""UPDATE datasets SET status = {_PH}, error = {_PH}, updated_at = {_PH}
                WHERE owner_id = {_PH} AND status = {_PH} AND updated_at < {_PH}""",
            (IngestStatus.FAILED.value, _STALE_INGEST_MESSAGE, _now(),
             owner_id, IngestStatus.PROCESSING.value, cutoff),
        )
        return cursor.rowcount


def delete(dataset_id: str, owner_id: str) -> bool:
    with _cursor() as cursor:
        cursor.execute(
            f"DELETE FROM datasets WHERE id = {_PH} AND owner_id = {_PH}", (dataset_id, owner_id)
        )
        return cursor.rowcount > 0
