"""Prueba end-to-end del motor sin levantar HTTP.

Genera un CSV grande, lo convierte a Parquet, aplica filtros vía el query-builder
seguro y exporta el resultado. Sirve para validar rendimiento y correctitud del
enfoque DuckDB antes de invertir en el frontend.

Uso:
    python -m scripts.smoke_test          # 2.000.000 de filas (por defecto)
    python -m scripts.smoke_test 5000000  # N filas
"""
import csv
import random
import sys
import time
from pathlib import Path

from app.core.query_builder import build_order_by, build_select, build_where
from app.repositories import duckdb_engine
from app.schemas.filter import (
    Combinator,
    FilterCondition,
    Operator,
    SortDirection,
    SortSpec,
)

_REGIONS = ["Lima", "Arequipa", "Cusco", "Piura", "Trujillo"]
_TIPOS = ["01", "03", "07", "08"]
_WORK_DIR = Path(__file__).resolve().parent.parent / "data" / "_smoke"


def _log(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


def _generate_csv(path: Path, rows: int) -> None:
    _log(f"Generando CSV de {rows:,} filas en {path} ...")
    start = time.perf_counter()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "fecha", "ruc", "monto", "tipo", "region"])
        for i in range(rows):
            writer.writerow([
                i,
                f"2026-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
                f"20{random.randint(100000000, 999999999)}",
                round(random.uniform(10, 50000), 2),
                random.choice(_TIPOS),
                random.choice(_REGIONS),
            ])
    _log(f"CSV generado en {time.perf_counter() - start:.1f}s ({path.stat().st_size / 1e6:.1f} MB)")


def _timed(label: str, func):
    start = time.perf_counter()
    result = func()
    _log(f"{label}: {time.perf_counter() - start:.3f}s")
    return result


def main() -> None:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = _WORK_DIR / "source.csv"
    parquet_path = _WORK_DIR / "source.parquet"
    export_path = _WORK_DIR / "filtrado.csv"

    _generate_csv(csv_path, rows)

    columns, row_count = _timed(
        "Conversión CSV -> Parquet", lambda: duckdb_engine.convert_to_parquet(csv_path, parquet_path)
    )
    _log(f"Esquema detectado: {[(c.name, c.type) for c in columns]}")
    _log(f"Filas en Parquet: {row_count:,}  |  Parquet: {parquet_path.stat().st_size / 1e6:.1f} MB")
    assert row_count == rows, f"Conteo esperado {rows}, obtenido {row_count}"

    valid_columns = {c.name for c in columns}
    conditions = [
        FilterCondition(column="region", operator=Operator.EQ, value="Lima"),
        FilterCondition(column="monto", operator=Operator.GT, value=1000),
    ]
    where_sql, params = build_where(conditions, Combinator.AND, valid_columns)
    select_sql = build_select(["id", "region", "monto", "tipo"], valid_columns)
    order_sql = build_order_by([SortSpec(column="monto", direction=SortDirection.DESC)], valid_columns)
    _log(f"WHERE seguro: {where_sql}  params={params}")

    total = _timed(
        "Conteo de coincidencias", lambda: duckdb_engine.count_matches(parquet_path, where_sql, params)
    )
    _log(f"Coincidencias (region=Lima AND monto>1000): {total:,}")

    column_names, preview_rows = _timed(
        "Preview (10 filas)",
        lambda: duckdb_engine.preview(parquet_path, select_sql, where_sql, order_sql, 10, 0, params),
    )
    _log(f"Columnas preview: {column_names}")
    for row in preview_rows[:3]:
        _log(f"  fila: {row}")

    _timed(
        "Export CSV filtrado",
        lambda: duckdb_engine.export_to_file(
            parquet_path, select_sql, where_sql, order_sql, params, export_path, "csv"
        ),
    )
    exported_lines = sum(1 for _ in export_path.open(encoding="utf-8")) - 1  # menos cabecera
    _log(f"Export escrito: {export_path} ({exported_lines:,} filas de datos)")
    assert exported_lines == total, f"Export {exported_lines} != coincidencias {total}"

    # Verificación de seguridad: una columna desconocida debe ser rechazada.
    try:
        build_where(
            [FilterCondition(column="'; DROP TABLE x; --", operator=Operator.EQ, value="x")],
            Combinator.AND,
            valid_columns,
        )
        raise SystemExit("[smoke] FALLO: se aceptó una columna inválida (riesgo de inyección)")
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ != "InvalidFilterError":
            raise
        _log("Seguridad OK: columna inválida rechazada por el query-builder.")

    _log("TODO OK")


if __name__ == "__main__":
    main()
