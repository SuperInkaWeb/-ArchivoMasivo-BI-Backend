"""Gestión de rutas de almacenamiento en disco.

Responsabilidad única: dar rutas seguras y consistentes por dataset.
Todos los IDs de dataset son UUID generados por el servidor, por lo que
nunca se construye una ruta a partir de datos del usuario (mitiga Path Traversal / A01).
"""
from pathlib import Path

from app.core.config import get_settings


def _base_dir() -> Path:
    base = Path(get_settings().data_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def uploads_dir() -> Path:
    path = _base_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parquet_dir() -> Path:
    path = _base_dir() / "parquet"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    path = _base_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def metadata_db_path() -> Path:
    return _base_dir() / "metadata.sqlite"


def upload_path(dataset_id: str, extension: str) -> Path:
    """Ruta del archivo original subido. El ID es un UUID del servidor."""
    safe_ext = extension.lstrip(".").lower()
    return uploads_dir() / f"{dataset_id}.{safe_ext}"


def parquet_path(dataset_id: str) -> Path:
    return parquet_dir() / f"{dataset_id}.parquet"
