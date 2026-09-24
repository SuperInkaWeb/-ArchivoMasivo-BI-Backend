"""Punto de entrada de la aplicación FastAPI.

Ensambla: CORS, rate limiting, manejo centralizado de errores de dominio
(nunca expone stack traces al cliente) e inicialización de metadatos.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import get_settings
from app.core.exceptions import (
    AuthError,
    DatasetNotFoundError,
    DatasetNotReadyError,
    DownloadTooLargeError,
    InvalidSheetError,
)
from app.core.query_builder import InvalidFilterError
from app.core.rate_limit import limiter
from app.repositories.dataset_repository import init_db
from app.routers import analysis, datasets, download, filter as filter_router
from app.services.dataset_service import (
    FileTooLargeError,
    UnsupportedFileTypeError,
    UploadIncompleteError,
)

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def _json_error(status_code: int, message: str) -> JSONResponse:
    """Respuesta de error con estructura consistente (sin filtrar internals)."""
    return JSONResponse(status_code=status_code, content={"status": "error", "message": message})


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,  # nunca "*"
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Necesarios para las descargas desde el navegador (origen distinto en prod):
        # el nombre del archivo y el tamaño para la barra de progreso.
        expose_headers=["Content-Disposition", "Content-Length"],
    )

    _register_error_handlers(app)

    app.include_router(datasets.router)
    app.include_router(filter_router.router)
    app.include_router(download.router)
    app.include_router(analysis.router)

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return _json_error(status.HTTP_429_TOO_MANY_REQUESTS, f"Límite de peticiones excedido: {exc.detail}")

    @app.exception_handler(DatasetNotFoundError)
    async def _not_found(_: Request, __: DatasetNotFoundError) -> JSONResponse:
        return _json_error(status.HTTP_404_NOT_FOUND, "Dataset no encontrado.")

    @app.exception_handler(DatasetNotReadyError)
    async def _not_ready(_: Request, exc: DatasetNotReadyError) -> JSONResponse:
        return _json_error(status.HTTP_409_CONFLICT, str(exc) or "El dataset aún no está listo.")

    @app.exception_handler(InvalidFilterError)
    async def _bad_filter(_: Request, exc: InvalidFilterError) -> JSONResponse:
        return _json_error(status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(DownloadTooLargeError)
    async def _too_large(_: Request, exc: DownloadTooLargeError) -> JSONResponse:
        return _json_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc))

    @app.exception_handler(InvalidSheetError)
    async def _bad_sheet(_: Request, exc: InvalidSheetError) -> JSONResponse:
        return _json_error(status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(AuthError)
    async def _auth(_: Request, exc: AuthError) -> JSONResponse:
        response = _json_error(status.HTTP_401_UNAUTHORIZED, str(exc))
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(UnsupportedFileTypeError)
    async def _bad_type(_: Request, exc: UnsupportedFileTypeError) -> JSONResponse:
        return _json_error(status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(FileTooLargeError)
    async def _file_too_large(_: Request, exc: FileTooLargeError) -> JSONResponse:
        return _json_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc))

    @app.exception_handler(UploadIncompleteError)
    async def _upload_incomplete(_: Request, exc: UploadIncompleteError) -> JSONResponse:
        return _json_error(status.HTTP_400_BAD_REQUEST, str(exc))


app = create_app()
