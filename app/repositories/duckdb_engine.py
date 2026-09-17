"""Acceso a datos vía DuckDB sobre archivos Parquet (en disco local o en R2/S3).

Responsabilidad única: ejecutar contra DuckDB. No conoce reglas de negocio.

Un "locator" es una cadena que apunta al archivo: o una ruta local, o una URI
`s3://bucket/clave` (Cloudflare R2). DuckDB lee/escribe ambos de forma transparente
gracias a la extensión httpfs, que se configura en `_connect()` cuando R2 está activo.

Notas de seguridad:
  - Los locators los genera el servidor (UUID/claves fijas), nunca datos del usuario;
    se escapan/normalizan antes de inyectarse como literal SQL.
  - Los VALORES de filtro se pasan SIEMPRE como parámetros ('?') enlazados.
"""
import logging
import zipfile
from xml.etree import ElementTree

import duckdb

from app.core.config import get_settings
from app.schemas.dataset import ColumnInfo

logger = logging.getLogger(__name__)

CSV_EXTENSIONS = {".csv", ".txt"}
EXCEL_EXTENSIONS = {".xlsx", ".xls"}

_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class EncodingNotSupported(Exception):
    """El archivo no es UTF-8 ni latin-1 legible por DuckDB (típico Windows-1252).

    La señala la capa de datos para que el servicio lo transcodifique a UTF-8; DuckDB
    nativo solo soporta utf-8/utf-16/latin-1 y su latin-1 rechaza el rango de control C1.
    """


def _sql_path(locator: str) -> str:
    """Literal SQL seguro para un locator controlado por el servidor (ruta o s3://)."""
    normalized = str(locator).replace("\\", "/").replace("'", "''")
    return f"'{normalized}'"


def _sql_str(value: str) -> str:
    """Literal SQL seguro para una cadena (escapa comillas simples)."""
    return "'" + value.replace("'", "''") + "'"


def _reader_expr(
    source: str, extension: str, sheet: str | None = None,
    encoding: str | None = None, null_padding: bool = False,
) -> str:
    """Expresión de tabla DuckDB para leer el archivo según su extensión.

    - CSV/TXT: auto-detecta delimitador, tipos y cabecera. `encoding` fuerza la
      codificación (p. ej. 'latin-1' para archivos SUNAT no-UTF-8). `null_padding`
      rellena con NULL las filas con menos columnas de lo detectado (sin perder filas).
    - Excel: lee la hoja indicada (o la primera) asumiendo cabecera.
    """
    ext = extension.lower()
    literal = _sql_path(source)
    if ext in CSV_EXTENSIONS:
        options = ""
        if encoding:
            options += f", encoding = {_sql_str(encoding)}"
        if null_padding:
            options += ", null_padding = true"
        return f"read_csv_auto({literal}{options})"
    if ext in EXCEL_EXTENSIONS:
        sheet_clause = f", sheet = {_sql_str(sheet)}" if sheet else ""
        return f"read_xlsx({literal}, header = true{sheet_clause})"
    raise ValueError(f"Extensión no soportada: {ext}")


def _is_encoding_error(exc: Exception) -> bool:
    """True si el fallo de DuckDB se debe a la codificación del archivo.

    Cubre tanto "not utf-8 encoded" como "File is not latin-1 encoded" (bytes del
    rango de control C1 presentes en archivos Windows-1252).
    """
    message = str(exc).lower()
    hints = ("utf-8", "unicode", "byte sequence", "encoding", "encoded")
    return any(hint in message for hint in hints)


def list_xlsx_sheets(local_path: str) -> list[str]:
    """Nombres de hoja de un .xlsx LOCAL leyendo su workbook.xml (sin dependencias).

    Recibe una ruta local (para R2, el llamador descarga el archivo primero). Si no
    es un ZIP válido (p. ej. .xls antiguo), devuelve lista vacía.
    """
    if not zipfile.is_zipfile(local_path):
        return []
    try:
        with zipfile.ZipFile(local_path) as archive:
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

    settings = get_settings()
    if settings.use_r2:
        # httpfs permite leer/escribir s3://... (R2 es compatible con S3).
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        con.execute(f"SET s3_endpoint={_sql_str(f'{settings.r2_account_id}.r2.cloudflarestorage.com')}")
        con.execute(f"SET s3_access_key_id={_sql_str(settings.r2_access_key_id)}")
        con.execute(f"SET s3_secret_access_key={_sql_str(settings.r2_secret_access_key)}")
        con.execute("SET s3_region='auto'")
        con.execute("SET s3_url_style='path'")
        con.execute("SET s3_use_ssl=true")
    return con


def convert_to_parquet(
    source: str, dest: str, extension: str, sheet: str | None = None
) -> tuple[list[ColumnInfo], int]:
    """Convierte el archivo original a Parquet columnar. Devuelve (columnas, filas).

    La conversión se hace vía COPY en streaming: DuckDB no carga todo en RAM.
    `sheet` solo aplica a Excel; para CSV/TXT se ignora.
    """
    with _connect() as con:
        _copy_with_fallbacks(con, source, dest, extension, sheet)
        columns = _describe(con, dest)
        row_count = con.execute(f"SELECT count(*) FROM read_parquet({_sql_path(dest)})").fetchone()[0]
    return columns, int(row_count)


def _copy_with_fallbacks(
    con: duckdb.DuckDBPyConnection, source: str, dest: str,
    extension: str, sheet: str | None,
) -> None:
    """Convierte a Parquet tolerando dos problemas frecuentes, en dos ejes:

    1. Codificación: UTF-8 → latin-1; si ninguna sirve, EncodingNotSupported (el servicio
       transcodifica Windows-1252/UTF-16 a UTF-8).
    2. Filas irregulares: si un CSV falla por estructura, se reintenta con null_padding
       (rellena con NULL sin perder filas). El intento estricto va primero para no
       enmascarar problemas en archivos bien formados; no se usa ignore_errors para no
       descartar filas en silencio.
    """
    try:
        _copy_trying_encodings(con, source, dest, extension, sheet, null_padding=False)
        return
    except duckdb.Error:
        # EncodingNotSupported no es duckdb.Error: propaga al servicio (transcodifica).
        # Un duckdb.Error aquí ya no es de codificación -> estructura de CSV.
        if extension.lower() not in CSV_EXTENSIONS:
            raise
    logger.warning("CSV con estructura irregular en %s: reintentando con null_padding", dest)
    _copy_trying_encodings(con, source, dest, extension, sheet, null_padding=True)


def _copy_trying_encodings(
    con: duckdb.DuckDBPyConnection, source: str, dest: str, extension: str,
    sheet: str | None, null_padding: bool,
) -> None:
    """Copia a Parquet probando UTF-8 y, si falla por codificación, latin-1."""
    try:
        _copy_to_parquet(con, source, dest, extension, sheet, encoding=None, null_padding=null_padding)
        return
    except duckdb.Error as exc:
        if not (extension.lower() in CSV_EXTENSIONS and _is_encoding_error(exc)):
            raise
    try:
        _copy_to_parquet(con, source, dest, extension, sheet, encoding="latin-1", null_padding=null_padding)
    except duckdb.Error as exc:
        if _is_encoding_error(exc):
            raise EncodingNotSupported(str(exc)) from exc
        raise


def _copy_to_parquet(
    con: duckdb.DuckDBPyConnection, source: str, dest: str, extension: str,
    sheet: str | None, encoding: str | None, null_padding: bool = False,
) -> None:
    reader = _reader_expr(source, extension, sheet, encoding, null_padding)
    con.execute(f"COPY (SELECT * FROM {reader}) TO {_sql_path(dest)} (FORMAT PARQUET)")


def _describe(con: duckdb.DuckDBPyConnection, parquet: str) -> list[ColumnInfo]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet({_sql_path(parquet)})").fetchall()
    # DESCRIBE => (column_name, column_type, null, key, default, extra)
    return [ColumnInfo(name=row[0], type=row[1]) for row in rows]


def get_schema(parquet: str) -> list[ColumnInfo]:
    with _connect() as con:
        return _describe(con, parquet)


def count_matches(parquet: str, where_sql: str, params: list) -> int:
    where_clause = f"WHERE {where_sql}" if where_sql else ""
    sql = f"SELECT count(*) FROM read_parquet({_sql_path(parquet)}) {where_clause}"
    with _connect() as con:
        return int(con.execute(sql, params).fetchone()[0])


def preview(
    parquet: str,
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
    parquet: str,
    select_sql: str,
    where_sql: str,
    order_sql: str,
    params: list,
    dest_path: str,
    fmt: str,
) -> None:
    """Exporta el resultado filtrado a un archivo LOCAL (dest_path) vía COPY streaming."""
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
    sql = f"COPY ({inner}) TO {_sql_path(dest_path)} {copy_opts}"
    with _connect() as con:
        con.execute(sql, params)


def distinct_values(
    parquet: str,
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
