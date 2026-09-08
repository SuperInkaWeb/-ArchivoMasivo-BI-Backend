"""Acceso a datos vía DuckDB sobre archivos Parquet.

Responsabilidad única: ejecutar contra DuckDB. No conoce reglas de negocio.

Notas de seguridad:
  - Las RUTAS de archivo se inyectan al SQL solo cuando son generadas por el
    servidor (UUID bajo el directorio de datos), y se escapan/normalizan.
    Nunca provienen de datos del usuario.
  - Los VALORES de filtro se pasan SIEMPRE como parámetros ('?') enlazados.
"""
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import duckdb

from app.schemas.dataset import ColumnInfo

CSV_EXTENSIONS = {".csv", ".txt"}
EXCEL_EXTENSIONS = {".xlsx", ".xls"}

_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _sql_path(path: Path) -> str:
    """Literal SQL seguro para una ruta controlada por el servidor."""
    posix = str(path).replace("\\", "/").replace("'", "''")
    return f"'{posix}'"


def _sql_str(value: str) -> str:
    """Literal SQL seguro para una cadena (escapa comillas simples)."""
    return "'" + value.replace("'", "''") + "'"


def _reader_expr(source: Path, sheet: str | None = None) -> str:
    """Expresión de tabla DuckDB para leer el archivo según su extensión.

    - CSV/TXT: auto-detecta delimitador, tipos y presencia de cabecera.
    - Excel: lee la hoja indicada (o la primera) asumiendo cabecera.
    """
    ext = source.suffix.lower()
    literal = _sql_path(source)
    if ext in CSV_EXTENSIONS:
        return f"read_csv_auto({literal})"
    if ext in EXCEL_EXTENSIONS:
        sheet_clause = f", sheet = {_sql_str(sheet)}" if sheet else ""
        return f"read_xlsx({literal}, header = true{sheet_clause})"
    raise ValueError(f"Extensión no soportada: {ext}")


def list_xlsx_sheets(source: Path) -> list[str]:
    """Devuelve los nombres de hoja de un .xlsx leyendo su workbook.xml (sin dependencias).

    Un .xlsx es un ZIP; las hojas se declaran en 'xl/workbook.xml'. Si el archivo
    no es un ZIP válido (p. ej. .xls antiguo), devuelve lista vacía.
    """
    if not zipfile.is_zipfile(source):
        return []
    try:
        with zipfile.ZipFile(source) as archive:
            root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return []
    namespace = {"main": _XLSX_MAIN_NS}
    return [
        name
        for sheet in root.findall(".//main:sheets/main:sheet", namespace)
        if (name := sheet.get("name"))
    ]


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    # preserve_insertion_order=false reduce el uso de memoria en cargas grandes.
    con.execute("SET preserve_insertion_order = false")
    # La extensión excel habilita read_xlsx / COPY ... (FORMAT xlsx).
    try:
        con.execute("INSTALL excel")
        con.execute("LOAD excel")
    except duckdb.Error:
        pass  # sin excel: CSV/TXT siguen funcionando
    return con


def convert_to_parquet(
    source: Path, dest: Path, sheet: str | None = None
) -> tuple[list[ColumnInfo], int]:
    """Convierte el archivo subido a Parquet columnar. Devuelve (columnas, filas).

    La conversión se hace vía COPY en streaming: DuckDB no carga todo en RAM.
    `sheet` solo aplica a Excel; para CSV/TXT se ignora.
    """
    reader = _reader_expr(source, sheet)
    with _connect() as con:
        con.execute(f"COPY (SELECT * FROM {reader}) TO {_sql_path(dest)} (FORMAT PARQUET)")
        columns = _describe(con, dest)
        row_count = con.execute(f"SELECT count(*) FROM read_parquet({_sql_path(dest)})").fetchone()[0]
    return columns, int(row_count)


def _describe(con: duckdb.DuckDBPyConnection, parquet: Path) -> list[ColumnInfo]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet({_sql_path(parquet)})").fetchall()
    # DESCRIBE => (column_name, column_type, null, key, default, extra)
    return [ColumnInfo(name=row[0], type=row[1]) for row in rows]


def get_schema(parquet: Path) -> list[ColumnInfo]:
    with _connect() as con:
        return _describe(con, parquet)


def count_matches(parquet: Path, where_sql: str, params: list) -> int:
    where_clause = f"WHERE {where_sql}" if where_sql else ""
    sql = f"SELECT count(*) FROM read_parquet({_sql_path(parquet)}) {where_clause}"
    with _connect() as con:
        return int(con.execute(sql, params).fetchone()[0])


def preview(
    parquet: Path,
    select_sql: str,
    where_sql: str,
    order_sql: str,
    limit: int,
    offset: int,
    params: list,
) -> tuple[list[str], list[dict]]:
    """Devuelve (nombres_columna, filas_como_dict) para una página de resultados."""
    where_clause = f"WHERE {where_sql}" if where_sql else ""
    # limit/offset son enteros validados por pydantic (seguros de interpolar).
    sql = (
        f"SELECT {select_sql} FROM read_parquet({_sql_path(parquet)}) "
        f"{where_clause} {order_sql} LIMIT {int(limit)} OFFSET {int(offset)}"
    )
    with _connect() as con:
        cursor = con.execute(sql, params)
        column_names = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    return column_names, [dict(zip(column_names, row)) for row in rows]


def export_to_file(
    parquet: Path,
    select_sql: str,
    where_sql: str,
    order_sql: str,
    params: list,
    dest: Path,
    fmt: str,
) -> None:
    """Exporta el resultado filtrado a disco vía COPY (streaming, memoria constante)."""
    where_clause = f"WHERE {where_sql}" if where_sql else ""
    inner = (
        f"SELECT {select_sql} FROM read_parquet({_sql_path(parquet)}) "
        f"{where_clause} {order_sql}"
    )
    if fmt == "csv":
        copy_opts = "(FORMAT CSV, HEADER true)"
    elif fmt == "xlsx":
        copy_opts = "(FORMAT xlsx, HEADER true)"
    else:
        raise ValueError(f"Formato no soportado: {fmt}")
    sql = f"COPY ({inner}) TO {_sql_path(dest)} {copy_opts}"
    with _connect() as con:
        con.execute(sql, params)


def distinct_values(
    parquet: Path,
    column_sql: str,
    search: str | None,
    limit: int,
) -> tuple[list, bool]:
    """Valores únicos de una columna (para el filtro tipo Excel).

    `column_sql` debe venir ya validado/citado por el query-builder (whitelist).
    El texto de búsqueda va como parámetro enlazado. Se pide un valor extra para
    saber si la lista quedó truncada.
    """
    filter_clause = ""
    query_params: list = []
    if search:
        filter_clause = f"AND CAST({column_sql} AS VARCHAR) ILIKE ?"
        query_params.append(f"%{search}%")
    sql = (
        f"SELECT DISTINCT {column_sql} AS value "
        f"FROM read_parquet({_sql_path(parquet)}) "
        f"WHERE {column_sql} IS NOT NULL {filter_clause} "
        f"ORDER BY value LIMIT {int(limit) + 1}"
    )
    with _connect() as con:
        rows = con.execute(sql, query_params).fetchall()
    values = [row[0] for row in rows]
    truncated = len(values) > limit
    return values[:limit], truncated
