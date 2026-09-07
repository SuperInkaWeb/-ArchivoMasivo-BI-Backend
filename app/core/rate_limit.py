"""Rate limiting compartido (slowapi) para endpoints costosos.

Se define una única instancia de Limiter reutilizada por los routers y
registrada en main.py. Las cuotas concretas se leen de configuración.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

limiter = Limiter(key_func=get_remote_address)


def upload_limit() -> str:
    return get_settings().rate_limit_upload


def download_limit() -> str:
    return get_settings().rate_limit_download
