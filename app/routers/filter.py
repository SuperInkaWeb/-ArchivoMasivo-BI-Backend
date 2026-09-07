"""Router de filtrado: previsualización paginada del resultado filtrado."""
from fastapi import APIRouter

from app.schemas.filter import PreviewRequest, PreviewResponse
from app.services import dataset_service

router = APIRouter(prefix="/datasets", tags=["filter"])


@router.post("/{dataset_id}/preview", response_model=PreviewResponse)
async def preview_filtered(dataset_id: str, request: PreviewRequest) -> PreviewResponse:
    """Devuelve una página de filas que cumplen los filtros + total de coincidencias."""
    return dataset_service.preview(dataset_id, request)
