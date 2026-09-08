"""Excepciones de dominio.

Se lanzan desde services/repositories y los routers las mapean a códigos HTTP.
Mantiene la capa de negocio ignorante de HTTP (Separation of Concerns).
"""


class DatasetNotFoundError(Exception):
    """El dataset solicitado no existe."""


class DatasetNotReadyError(Exception):
    """El dataset existe pero aún no terminó la ingesta (o falló)."""


class DownloadTooLargeError(Exception):
    """El resultado filtrado excede el límite permitido de descarga."""


class InvalidSheetError(Exception):
    """La hoja solicitada no existe en el archivo Excel."""


class AuthError(Exception):
    """Falta autenticación o el token es inválido/expirado."""
