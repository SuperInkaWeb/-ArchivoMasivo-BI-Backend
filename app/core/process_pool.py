"""Pool de procesos compartido para el trabajo pesado de DuckDB.

Las conversiones (ingesta) y materializaciones (análisis) que consumen mucha RAM
corren en procesos hijos efímeros: `spawn` (intérprete limpio, evita deadlocks al
forkear con hilos/async) y `max_tasks_per_child=1` (cada tarea usa un proceso
NUEVO que muere al terminar, garantizando que el SO recupere toda su memoria).

Es un ÚNICO pool para toda la app, de modo que el tope de concurrencia acota el
pico de RAM TOTAL (ingesta + análisis juntos), no por servicio. Lo que excede el
tope queda en cola (backpressure): disparar muchas operaciones nunca desborda la
memoria.
"""
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from app.core.config import get_settings

_MP_CONTEXT = multiprocessing.get_context("spawn")
_pool: ProcessPoolExecutor | None = None


def get_pool() -> ProcessPoolExecutor:
    """Pool compartido (lazy singleton) creado en el proceso web."""
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=max(1, get_settings().ingest_max_workers),
            mp_context=_MP_CONTEXT,
            max_tasks_per_child=1,
        )
    return _pool


def shutdown_pool() -> None:
    """Cierra el pool al apagar la app (invocado desde el lifespan de FastAPI)."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
