"""Acceso a Cloudflare R2 (S3-compatible) vía boto3.

Solo se usa cuando la configuración R2 está completa (`settings.use_r2`). Responsable de:
  - Generar URLs prefirmadas de subida (el navegador sube directo a R2).
  - Borrar objetos (limpieza del crudo tras convertir, y borrado de dataset).
  - Descargar un objeto a un archivo temporal (p. ej. para listar hojas de Excel).

DuckDB lee/escribe el Parquet directamente en R2 (ver duckdb_engine); boto3 no
mueve los datos masivos, solo firma y administra.
"""
import tempfile
from functools import lru_cache

import boto3
from botocore.config import Config

from app.core.config import get_settings


@lru_cache
def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


# Content-Type fijo para que la firma coincida con lo que envía el navegador.
UPLOAD_CONTENT_TYPE = "application/octet-stream"


def presign_put(key: str) -> str:
    """URL prefirmada para que el navegador suba (PUT) el archivo directo a R2."""
    settings = get_settings()
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.r2_bucket, "Key": key, "ContentType": UPLOAD_CONTENT_TYPE},
        ExpiresIn=settings.r2_presign_expiry_minutes * 60,
    )


def object_exists(key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        _client().head_object(Bucket=get_settings().r2_bucket, Key=key)
        return True
    except ClientError:
        return False


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=get_settings().r2_bucket, Key=key)


def object_size(key: str) -> int | None:
    """Tamaño en bytes del objeto, o None si no existe."""
    from botocore.exceptions import ClientError

    try:
        head = _client().head_object(Bucket=get_settings().r2_bucket, Key=key)
        return int(head["ContentLength"])
    except (ClientError, KeyError):
        return None


def read_prefix(key: str, length: int) -> bytes:
    """Primeros `length` bytes del objeto (GET con Range), para detectar el BOM.

    No descarga el archivo completo. Si el objeto no existe o falla, devuelve vacío.
    """
    from botocore.exceptions import ClientError

    try:
        response = _client().get_object(
            Bucket=get_settings().r2_bucket, Key=key, Range=f"bytes=0-{length - 1}"
        )
        return response["Body"].read()
    except ClientError:
        return b""


def download_to_temp(key: str, suffix: str) -> str:
    """Descarga un objeto a un archivo temporal local y devuelve su ruta."""
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.close()
    _client().download_file(get_settings().r2_bucket, key, handle.name)
    return handle.name
