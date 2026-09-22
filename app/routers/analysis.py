"""Router de análisis: tablas dinámicas (pivote).

Solo recibe la petición y delega en el service. Endpoints SÍNCRONOS a propósito:
la consulta DuckDB es bloqueante y FastAPI la corre en su threadpool (no bloquea
el event loop). Todos requieren autenticación y operan sobre datos del usuario.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, Request, status

from app.core.rate_limit import download_limit, limiter, upload_limit
from app.core.security import CurrentUser, get_current_user
from app.schemas.analysis import (
    ComputeRequest,
    ComputeSaveRequest,
    PivotRequest,
    PivotResponse,
    PivotSaveRequest,
)
from app.schemas.dataset import DatasetSummary
from app.schemas.filter import PreviewResponse
from app.services import analysis_service

router = APIRouter(prefix="/datasets", tags=["analysis"])


@router.post("/{dataset_id}/pivot", response_model=PivotResponse)
@limiter.limit(download_limit)
def pivot_view(
    request: Request,
    dataset_id: str,
    body: PivotRequest,
    user: CurrentUser = Depends(get_current_user),
) -> PivotResponse:
    """Ejecuta el pivote y devuelve una página del reporte (sin persistir)."""
    return analysis_service.run_pivot(dataset_id, body, user.sub)


@router.post("/{dataset_id}/pivot/save", response_model=DatasetSummary,
             status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(upload_limit)
def pivot_save(
    request: Request,
    dataset_id: str,
    body: PivotSaveRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> DatasetSummary:
    """Guarda el reporte como un dataset nuevo (se materializa en segundo plano)."""
    summary = analysis_service.start_pivot_save(dataset_id, body, user.sub)
    background_tasks.add_task(analysis_service.run_pivot_save, summary.id, dataset_id, body, user.sub)
    return summary


@router.post("/{dataset_id}/compute", response_model=PreviewResponse)
@limiter.limit(download_limit)
def compute_view(
    request: Request,
    dataset_id: str,
    body: ComputeRequest,
    user: CurrentUser = Depends(get_current_user),
) -> PreviewResponse:
    """Aplica columnas calculadas y devuelve una página (sin persistir)."""
    return analysis_service.run_compute(dataset_id, body, user.sub)


@router.post("/{dataset_id}/compute/save", response_model=DatasetSummary,
             status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(upload_limit)
def compute_save(
    request: Request,
    dataset_id: str,
    body: ComputeSaveRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> DatasetSummary:
    """Guarda las columnas calculadas como un dataset nuevo (se materializa en segundo plano)."""
    summary = analysis_service.start_compute_save(dataset_id, body, user.sub)
    background_tasks.add_task(analysis_service.run_compute_save, summary.id, dataset_id, body, user.sub)
    return summary
