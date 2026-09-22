"""Construcción SEGURA de consultas de tabla dinámica (pivote).

Mismas invariantes que query_builder (defensa A03 - Injection):
  1. Los nombres de columna se validan contra el esquema real (whitelist).
  2. Las agregaciones provienen de un enum CERRADO (schemas.analysis.Aggregation).
  3. Los VALORES del cross-tab NUNCA se concatenan: van como parámetros ('?').
  4. Los alias de salida se generan en servidor y se citan/escapan como identificador.

Devuelve el SELECT del pivote, la lista de columnas de agrupación (para GROUP BY /
ORDER BY / conteo) y los parámetros posicionales del SELECT, en orden.
"""
from app.core.query_builder import InvalidFilterError, quote_column
from app.schemas.analysis import Aggregation, Measure, PivotSpec

# Tope de columnas generadas por el cross-tab: evita reportes patológicos (A04).
MAX_PIVOT_COLUMNS = 50

# Agregaciones escalares -> función SQL directa. COUNT y COUNT_DISTINCT se tratan aparte.
_SCALAR_AGG: dict[Aggregation, str] = {
    Aggregation.SUM: "sum",
    Aggregation.AVG: "avg",
    Aggregation.MIN: "min",
    Aggregation.MAX: "max",
}

# Prefijo legible del alias por agregación (nombre de la columna del reporte).
_ALIAS_PREFIX: dict[Aggregation, str] = {
    Aggregation.COUNT: "conteo",
    Aggregation.COUNT_DISTINCT: "unicos",
    Aggregation.SUM: "suma",
    Aggregation.AVG: "promedio",
    Aggregation.MIN: "minimo",
    Aggregation.MAX: "maximo",
}


def _quote_alias(alias: str) -> str:
    """Cita un alias de salida como identificador SQL seguro (escapa comillas dobles)."""
    return '"' + alias.replace('"', '""') + '"'


def _measure_alias(measure: Measure) -> str:
    prefix = _ALIAS_PREFIX[measure.aggregation]
    return f"{prefix}_{measure.column}" if measure.column else prefix


def _agg_expr(aggregation: Aggregation, inner_sql: str, has_column: bool) -> str:
    """Envuelve `inner_sql` (una columna citada o un CASE WHEN) con la agregación."""
    if aggregation is Aggregation.COUNT:
        return f"count({inner_sql})" if has_column else "count(*)"
    if aggregation is Aggregation.COUNT_DISTINCT:
        return f"count(DISTINCT {inner_sql})"
    return f"{_SCALAR_AGG[aggregation]}({inner_sql})"


def _measure_column_sql(measure: Measure, valid_columns: set[str]) -> str | None:
    """Columna citada de la métrica. Solo COUNT admite ausencia de columna."""
    if measure.column is None:
        if measure.aggregation is not Aggregation.COUNT:
            raise InvalidFilterError(
                f"La agregación '{measure.aggregation.value}' requiere una columna."
            )
        return None
    return quote_column(measure.column, valid_columns)


def _simple_measures_sql(measures: list[Measure], valid_columns: set[str]) -> list[str]:
    """Métricas sin cross-tab: una columna de salida por métrica."""
    pieces: list[str] = []
    for measure in measures:
        col_sql = _measure_column_sql(measure, valid_columns)
        expr = _agg_expr(measure.aggregation, col_sql or "", has_column=col_sql is not None)
        pieces.append(f"{expr} AS {_quote_alias(_measure_alias(measure))}")
    return pieces


def _crosstab_measures_sql(
    measures: list[Measure], valid_columns: set[str], pivot_col_sql: str, pivot_values: list,
) -> tuple[list[str], list]:
    """Métricas con cross-tab: una columna por (valor distinto × métrica).

    El valor del pivote va como parámetro ('?'); el alias de salida (visible) usa su
    representación textual, citada. Con una sola métrica el alias es solo el valor;
    con varias se añade el nombre de la métrica para desambiguar.
    """
    pieces: list[str] = []
    params: list = []
    single = len(measures) == 1
    for value in pivot_values:
        for measure in measures:
            col_sql = _measure_column_sql(measure, valid_columns)
            inner = f"CASE WHEN {pivot_col_sql} = ? THEN {col_sql or '1'} END"
            expr = _agg_expr(measure.aggregation, inner, has_column=True)
            label = str(value) if single else f"{value} · {_measure_alias(measure)}"
            pieces.append(f"{expr} AS {_quote_alias(label)}")
            params.append(value)
    return pieces, params


def build_pivot(
    request: PivotSpec, valid_columns: set[str], pivot_values: list | None,
) -> tuple[str, str, list]:
    """Arma el pivote. Devuelve (select_sql, group_cols_sql, select_params).

    `pivot_values` son los valores distintos de la columna de cross-tab (obtenidos por
    el servicio con un DISTINCT), o None si no hay cross-tab.
    """
    group_cols = [quote_column(name, valid_columns) for name in request.group_by]
    group_cols_sql = ", ".join(group_cols)

    if request.pivot_column is not None:
        if request.pivot_column in request.group_by:
            raise InvalidFilterError(
                "La columna de cross-tab no puede ser también una columna de agrupación."
            )
        pivot_col_sql = quote_column(request.pivot_column, valid_columns)
        values = pivot_values or []
        if len(values) > MAX_PIVOT_COLUMNS:
            raise InvalidFilterError(
                f"El cross-tab generaría demasiadas columnas (máximo {MAX_PIVOT_COLUMNS})."
            )
        measure_pieces, select_params = _crosstab_measures_sql(
            request.measures, valid_columns, pivot_col_sql, values
        )
    else:
        measure_pieces = _simple_measures_sql(request.measures, valid_columns)
        select_params = []

    select_sql = ", ".join(group_cols + measure_pieces)
    return select_sql, group_cols_sql, select_params
