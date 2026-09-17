"""Router de datasets: subir (2 pasos), listar, detalle, valores, hoja y eliminar.

Solo recibe la petición y delega en el service. Todos los endpoints requieren
autenticación (Auth0) y operan sobre los datos del usuario (aislamiento por owner_id).

Subida en dos pasos (para archivos grandes):
  1. POST /datasets/upload-url  -> devuelve la URL a la que subir el archivo.
  2. el navegador sube el archivo (a R2 directo, o a PUT /datasets/{id}/raw en local).
  3. POST /datasets/{id}/uploaded -> dispara la ingesta.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status

from app.core.rate_limit import limiter, upload_limit
from app.core.security import CurrentUser, get_current_user
from app.schemas.dataset import (
    DatasetDetail,
    DatasetSummary,
    SheetSelection,
    UploadTicket,
    UploadUrlRequest,
)
from app.schemas.filter import DistinctValuesResponse
from app.services import dataset_service
from app.services.ingest_service import run_ingest

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("/upload-url", response_model=UploadTicket, status_code=status.HTTP_201_CREATED)
@limiter.limit(upload_limit)
def create_upload_url(
    request: Request,
    body: UploadUrlRequest,
    user: CurrentUser = Depends(get_current_user),
) -> UploadTicket:
    """Paso 1: crea el dataset y devuelve la URL de subida (prefirmada de R2 o local)."""
    return dataset_service.create_upload(body.filename, user.sub)


@router.put("/{dataset_id}/raw", status_code=status.HTTP_204_NO_CONTENT)
async def upload_raw(
    dataset_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> None:
    """Modo local: recibe el archivo (cuerpo del PUT) y lo guarda en disco."""
    await dataset_service.save_raw_local(dataset_id, user.sub, request)


@router.post("/{dataset_id}/uploaded", response_model=DatasetSummary, status_code=status.HTTP_202_ACCEPTED)
def confirm_upload(
    dataset_id: str,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> DatasetSummary:
    """Paso 3: confirma la subida y dispara la ingesta (conversión a Parquet)."""
    summary = dataset_service.confirm_uploaded(dataset_id, user.sub)
    background_tasks.add_task(run_ingest, dataset_id)
    return summary


@router.get("", response_model=list[DatasetSummary])
def list_datasets(user: CurrentUser = Depends(get_current_user)) -> list[DatasetSummary]:
    return dataset_service.list_datasets(user.sub)


@router.get("/{dataset_id}", response_model=DatasetDetail)
def get_dataset(
    dataset_id: str, user: CurrentUser = Depends(get_current_user)
) -> DatasetDetail:
    return dataset_service.get_dataset(dataset_id, user.sub)


@router.get("/{dataset_id}/values", response_model=DistinctValuesResponse)
def get_column_values(
    dataset_id: str,
    column: str = Query(..., description="Nombre de la columna"),
    search: str | None = Query(None, description="Texto para filtrar los valores"),
    user: CurrentUser = Depends(get_current_user),
) -> DistinctValuesResponse:
    """Valores únicos de una columna (para el desplegable de casillas tipo Excel)."""
    return dataset_service.distinct_values(dataset_id, column, search, user.sub)


@router.post("/{dataset_id}/sheet", response_model=DatasetDetail, status_code=status.HTTP_202_ACCEPTED)
def change_sheet(
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
def delete_dataset(
    dataset_id: str, user: CurrentUser = Depends(get_current_user)
) -> None:
    dataset_service.delete_dataset(dataset_id, user.sub)
