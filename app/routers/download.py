"""Router de descarga: genera y transmite el resultado filtrado (CSV/XLSX)."""
import os

from fastapi import APIRouter, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.core.rate_limit import download_limit, limiter
from app.schemas.filter import DownloadRequest
from app.services import export_service

router = APIRouter(prefix="/datasets", tags=["download"])


@router.post("/{dataset_id}/download")
@limiter.limit(download_limit)
async def download_filtered(
    request: Request,
    dataset_id: str,
    body: DownloadRequest,
) -> FileResponse:
    """Aplica los filtros, escribe el resultado en disco y lo transmite.

    El archivo temporal se elimina tras enviarse (BackgroundTask de limpieza).
    """
    result = export_service.build_export(dataset_id, body)
    return FileResponse(
        path=result.path,
        media_type=result.media_type,
        filename=result.download_filename,
        background=BackgroundTask(os.remove, result.path),
    )
