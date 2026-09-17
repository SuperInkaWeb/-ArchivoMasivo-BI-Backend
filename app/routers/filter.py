"""Router de filtrado: previsualización paginada del resultado filtrado."""
from fastapi import APIRouter, Depends

from app.core.security import CurrentUser, get_current_user
from app.schemas.filter import PreviewRequest, PreviewResponse
from app.services import dataset_service

router = APIRouter(prefix="/datasets", tags=["filter"])


# Endpoint SÍNCRONO a propósito: la consulta DuckDB sobre R2 es bloqueante y puede tardar
# segundos en archivos grandes. Con `def`, FastAPI la corre en su threadpool y el servidor
# sigue atendiendo otras peticiones (evita el "Failed to fetch" por bloqueo del event loop).
@router.post("/{dataset_id}/preview", response_model=PreviewResponse)
def preview_filtered(
    dataset_id: str,
    request: PreviewRequest,
    user: CurrentUser = Depends(get_current_user),
) -> PreviewResponse:
    """Devuelve una página de filas que cumplen los filtros + total de coincidencias."""
    return dataset_service.preview(dataset_id, request, user.sub)
