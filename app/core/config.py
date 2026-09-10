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

    # Metadatos: si DATABASE_URL está definida se usa Postgres/Neon; si no, SQLite local.
    database_url: str = ""

    # Almacenamiento de archivos. Si las 4 variables R2 están definidas se usa
    # Cloudflare R2 (subida directa + Parquet en R2); si no, disco local.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    # Minutos de validez de la URL prefirmada de subida.
    r2_presign_expiry_minutes: int = 30

    # Límites de recursos (defensa contra abuso / OWASP A04)
    max_upload_mb: int = 2048
    max_download_rows: int = 5_000_000
    preview_max_rows: int = 200
    query_timeout_seconds: int = 120

    # Autenticación (Auth0). auth_enabled=false solo para desarrollo/pruebas locales.
    auth_enabled: bool = True
    auth0_domain: str = ""
    auth0_audience: str = ""

    # Seguridad de transporte
    cors_origins: str = "http://localhost:5173"

    # Rate limiting
    rate_limit_upload: str = "10/minute"
    rate_limit_download: str = "20/minute"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def auth0_issuer(self) -> str:
        return f"https://{self.auth0_domain}/"

    @property
    def auth0_jwks_url(self) -> str:
        return f"https://{self.auth0_domain}/.well-known/jwks.json"

    @property
    def use_r2(self) -> bool:
        return bool(
            self.r2_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_bucket
        )

    @property
    def r2_endpoint(self) -> str:
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Instancia única de configuración (cacheada)."""
    return Settings()
