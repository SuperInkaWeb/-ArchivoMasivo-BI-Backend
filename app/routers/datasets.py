"""Router de datasets: subir (multi-archivo), listar, detalle y eliminar.

Solo recibe la petición y delega en el service. Sin lógica de negocio aquí.
Todos los endpoints requieren autenticación (Auth0) y operan sobre los datos
del usuario autenticado (aislamiento por owner_id).
"""
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, UploadFile, status

from app.core.rate_limit import limiter, upload_limit
from app.core.security import CurrentUser, get_current_user
from app.schemas.dataset import DatasetDetail, DatasetSummary, SheetSelection, UploadResult
from app.schemas.filter import DistinctValuesResponse
from app.services import dataset_service
from app.services.ingest_service import run_ingest

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("/upload", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
@limiter.limit(upload_limit)
async def upload_datasets(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile],
    user: CurrentUser = Depends(get_current_user),
) -> UploadResult:
    """Sube uno o varios archivos. Cada uno se ingesta en background (async)."""
    created: list[DatasetSummary] = []
    for file in files:
        summary = await dataset_service.save_upload(file, user.sub)
        background_tasks.add_task(run_ingest, summary.id)
        created.append(summary)
    return UploadResult(datasets=created)


@router.get("", response_model=list[DatasetSummary])
async def list_datasets(user: CurrentUser = Depends(get_current_user)) -> list[DatasetSummary]:
    return dataset_service.list_datasets(user.sub)


@router.get("/{dataset_id}", response_model=DatasetDetail)
async def get_dataset(
    dataset_id: str, user: CurrentUser = Depends(get_current_user)
) -> DatasetDetail:
    return dataset_service.get_dataset(dataset_id, user.sub)


@router.get("/{dataset_id}/values", response_model=DistinctValuesResponse)
async def get_column_values(
    dataset_id: str,
    column: str = Query(..., description="Nombre de la columna"),
    search: str | None = Query(None, description="Texto para filtrar los valores"),
    user: CurrentUser = Depends(get_current_user),
) -> DistinctValuesResponse:
    """Valores únicos de una columna (para el desplegable de casillas tipo Excel)."""
    return dataset_service.distinct_values(dataset_id, column, search, user.sub)


@router.post("/{dataset_id}/sheet", response_model=DatasetDetail, status_code=status.HTTP_202_ACCEPTED)
async def change_sheet(
    dataset_id: str,
    selection: SheetSelection,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> DatasetDetail:
    """Re-ingesta el Excel usando otra hoja (procesa en segundo plano)."""
    dataset_service.request_sheet_change(dataset_id, selection.sheet, user.sub)
    background_tasks.add_task(run_ingest, dataset_id, selection.sheet)
    return dataset_service.get_dataset(dataset_id, user.sub)


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: str, user: CurrentUser = Depends(get_current_user)
) -> None:
    dataset_service.delete_dataset(dataset_id, user.sub)
