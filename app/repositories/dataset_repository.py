"""Persistencia de metadatos de datasets en SQLite (stdlib, sin dependencias extra).

Guarda solo metadatos ligeros (id, nombre, estado, esquema, conteo). Los datos
masivos viven en Parquet. Usa siempre sentencias parametrizadas (nunca concatena SQL).
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.core.storage import metadata_db_path
from app.schemas.dataset import ColumnInfo, DatasetDetail, DatasetSummary, IngestStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id                TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    extension         TEXT NOT NULL,
    status            TEXT NOT NULL,
    row_count         INTEGER,
    size_bytes        INTEGER NOT NULL,
    columns_json      TEXT NOT NULL DEFAULT '[]',
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    connection = sqlite3.connect(metadata_db_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    with _conn() as connection:
        connection.execute(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(dataset_id: str, original_filename: str, extension: str, size_bytes: int) -> None:
    now = _now()
    with _conn() as connection:
        connection.execute(
            """INSERT INTO datasets
               (id, original_filename, extension, status, size_bytes, columns_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, '[]', ?, ?)""",
            (dataset_id, original_filename, extension, IngestStatus.PENDING.value, size_bytes, now, now),
        )


def set_status(dataset_id: str, status: IngestStatus, error: str | None = None) -> None:
    with _conn() as connection:
        connection.execute(
            "UPDATE datasets SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status.value, error, _now(), dataset_id),
        )


def set_ready(dataset_id: str, columns: list[ColumnInfo], row_count: int) -> None:
    columns_json = json.dumps([col.model_dump() for col in columns])
    with _conn() as connection:
        connection.execute(
            """UPDATE datasets
               SET status = ?, columns_json = ?, row_count = ?, error = NULL, updated_at = ?
               WHERE id = ?""",
            (IngestStatus.READY.value, columns_json, row_count, _now(), dataset_id),
        )


def _row_to_summary(row: sqlite3.Row) -> DatasetSummary:
    return DatasetSummary(
        id=row["id"],
        original_filename=row["original_filename"],
        status=IngestStatus(row["status"]),
        row_count=row["row_count"],
        size_bytes=row["size_bytes"],
        created_at=datetime.fromisoformat(row["created_at"]),
        error=row["error"],
    )


def list_all() -> list[DatasetSummary]:
    with _conn() as connection:
        rows = connection.execute(
            "SELECT * FROM datasets ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_summary(row) for row in rows]


def get_detail(dataset_id: str) -> DatasetDetail | None:
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
    if row is None:
        return None
    columns = [ColumnInfo(**col) for col in json.loads(row["columns_json"])]
    summary = _row_to_summary(row)
    return DatasetDetail(**summary.model_dump(), columns=columns)


def get_extension(dataset_id: str) -> str | None:
    with _conn() as connection:
        row = connection.execute(
            "SELECT extension FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
    return row["extension"] if row else None


def delete(dataset_id: str) -> bool:
    with _conn() as connection:
        cursor = connection.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    return cursor.rowcount > 0
