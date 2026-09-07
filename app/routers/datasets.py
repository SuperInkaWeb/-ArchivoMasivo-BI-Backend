"""Router de datasets: subir (multi-archivo), listar, detalle y eliminar.

Solo recibe la petición y delega en el service. Sin lógica de negocio aquí.
"""
from fastapi import APIRouter, BackgroundTasks, Request, UploadFile, status

from app.core.rate_limit import limiter, upload_limit
from app.schemas.dataset import DatasetDetail, DatasetSummary, UploadResult
from app.services import dataset_service
from app.services.ingest_service import run_ingest

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("/upload", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
@limiter.limit(upload_limit)
async def upload_datasets(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile],
) -> UploadResult:
    """Sube uno o varios archivos. Cada uno se ingesta en background (async)."""
    created: list[DatasetSummary] = []
    for file in files:
        summary = await dataset_service.save_upload(file)
        background_tasks.add_task(run_ingest, summary.id)
        created.append(summary)
    return UploadResult(datasets=created)


@router.get("", response_model=list[DatasetSummary])
async def list_datasets() -> list[DatasetSummary]:
    return dataset_service.list_datasets()


@router.get("/{dataset_id}", response_model=DatasetDetail)
async def get_dataset(dataset_id: str) -> DatasetDetail:
    return dataset_service.get_dataset(dataset_id)


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(dataset_id: str) -> None:
    dataset_service.delete_dataset(dataset_id)
