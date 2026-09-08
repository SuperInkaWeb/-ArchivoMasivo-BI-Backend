"""Autenticación vía Auth0 (JWT RS256).

Valida el access token que envía el frontend (`Authorization: Bearer …`) contra
Auth0: firma (JWKS), issuer y audience. Expone `get_current_user` como dependencia
de FastAPI. Sin token válido => AuthError (401).

Notas de seguridad (OWASP A07 / A02):
  - Nunca se confía en el payload sin verificar la firma contra el JWKS de Auth0.
  - Se exige audience e issuer exactos.
  - Los errores no exponen detalles internos al cliente.
"""
import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.exceptions import AuthError

# auto_error=False: si falta el header, lo manejamos nosotros (AuthError uniforme).
_bearer_scheme = HTTPBearer(auto_error=False)

# Cliente JWKS cacheado (descarga y cachea las llaves públicas de Auth0).
_jwks_client: PyJWKClient | None = None


class CurrentUser(BaseModel):
    """Usuario autenticado. `sub` es el identificador único de Auth0 (owner)."""
    sub: str
    email: str | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(get_settings().auth0_jwks_url)
    return _jwks_client


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """Dependencia: devuelve el usuario autenticado o lanza AuthError (401)."""
    settings = get_settings()

    # Modo desarrollo sin Auth0: usuario fijo (NUNCA usar en producción).
    if not settings.auth_enabled:
        return CurrentUser(sub="dev-user", email="dev@local")

    if credentials is None or not credentials.credentials:
        raise AuthError("Falta el token de autenticación.")

    try:
        signing_key = _jwks().get_signing_key_from_jwt(credentials.credentials).key
        payload = jwt.decode(
            credentials.credentials,
            signing_key,
            algorithms=["RS256"],
            audience=settings.auth0_audience,
            issuer=settings.auth0_issuer,
        )
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de validación => 401 genérico
        raise AuthError("Token inválido o expirado.") from exc

    subject = payload.get("sub")
    if not subject:
        raise AuthError("Token sin identificador de usuario.")
    return CurrentUser(sub=subject, email=payload.get("email"))
