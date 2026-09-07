"""Configuración central de la aplicación.

Toda la configuración proviene de variables de entorno (nunca hardcodeada),
cumpliendo el estándar de secretos por entorno. Se valida con pydantic-settings.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "datafilter-backend"
    data_dir: str = "./data"

    # Límites de recursos (defensa contra abuso / OWASP A04)
    max_upload_mb: int = 2048
    max_download_rows: int = 5_000_000
    preview_max_rows: int = 200
    query_timeout_seconds: int = 120

    # Seguridad de transporte
    cors_origins: str = "http://localhost:5173"

    # Rate limiting
    rate_limit_upload: str = "10/minute"
    rate_limit_download: str = "20/minute"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Instancia única de configuración (cacheada)."""
    return Settings()
