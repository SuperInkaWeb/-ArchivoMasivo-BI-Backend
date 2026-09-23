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
from app.schemas.filter import SortDirection

# Tope de columnas generadas por el cross-tab: evita reportes patológicos (A04).
MAX_PIVOT_COLUMNS = 50

# Decimales a los que se redondean las agregaciones de coma flotante (SUM/AVG).
# Elimina el ruido de flotante (p. ej. 683696.2599999998 -> 683696.26) en la vista,
# el guardado y la descarga por igual, sin recortar precisión razonable.
_ROUND_DECIMALS = 6

# Etiqueta de la columna de total por fila en un cross-tab.
_ROW_TOTAL_LABEL = "Total"

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

# Agregaciones que exigen una columna NUMÉRICA (y que además producen ruido de flotante,
# por lo que se redondean). SUM/AVG sobre texto no tiene sentido y DuckDB fallaría.
_NUMERIC_AGG = {Aggregation.SUM, Aggregation.AVG}

# Agregaciones donde una celda de cross-tab sin datos significa 0 (no NULL).
_ZERO_ON_EMPTY = {Aggregation.SUM, Aggregation.COUNT, Aggregation.COUNT_DISTINCT}

# Agregaciones aditivas para las que el "% del total" tiene sentido (el total es la suma
# de las partes). En promedio/mín/máx/únicos un porcentaje del total no es interpretable.
_PERCENT_AGG = {Aggregation.SUM, Aggregation.COUNT}


def _quote_alias(alias: str) -> str:
    """Cita un alias de salida como identificador SQL seguro (escapa comillas dobles)."""
    return '"' + alias.replace('"', '""') + '"'


def _measure_alias(measure: Measure) -> str:
    prefix = _ALIAS_PREFIX[measure.aggregation]
    return f"{prefix}_{measure.column}" if measure.column else prefix


def _raw_agg(aggregation: Aggregation, inner_sql: str, has_column: bool) -> str:
    """Agregación SQL cruda (sin redondear) sobre `inner_sql` (columna citada o CASE WHEN)."""
    if aggregation is Aggregation.COUNT:
        return f"count({inner_sql})" if has_column else "count(*)"
    if aggregation is Aggregation.COUNT_DISTINCT:
        return f"count(DISTINCT {inner_sql})"
    return f"{_SCALAR_AGG[aggregation]}({inner_sql})"


def _agg_expr(aggregation: Aggregation, inner_sql: str, has_column: bool) -> str:
    """Agregación con redondeo de SUM/AVG (elimina el ruido de coma flotante)."""
    expr = _raw_agg(aggregation, inner_sql, has_column)
    if aggregation in _NUMERIC_AGG:
        expr = f"round({expr}, {_ROUND_DECIMALS})"
    return expr


def _percent_expr(
    aggregation: Aggregation, num_inner: str, has_num: bool,
    full_inner: str, has_full: bool, windowed: bool,
) -> str:
    """Valor de la celda como % del total de la métrica.

    Numerador = la agregación de la celda; denominador = el total general de la métrica.
    En el reporte agrupado el total se obtiene con una ventana (`sum(...) OVER ()`); en
    la fila de Total general (sin GROUP BY) el total es la agregación sobre todas las
    filas. `nullif(...,0)` evita la división por cero.
    """
    numerator = _raw_agg(aggregation, num_inner, has_num)
    full = _raw_agg(aggregation, full_inner, has_full)
    denominator = f"sum({full}) OVER ()" if windowed else full
    return f"round(100.0 * {numerator} / nullif({denominator}, 0), 2)"


def _value_expr(
    measure: Measure, num_inner: str, has_num: bool, col_sql: str | None,
    percent: bool, windowed: bool,
) -> str:
    """Expresión de una celda: valor absoluto (agregación redondeada) o % del total."""
    if percent:
        return _percent_expr(
            measure.aggregation, num_inner, has_num, col_sql or "", col_sql is not None, windowed
        )
    return _agg_expr(measure.aggregation, num_inner, has_num)


def _measure_column_sql(
    measure: Measure, valid_columns: set[str], numeric_columns: set[str]
) -> str | None:
    """Columna citada de la métrica. Solo COUNT admite ausencia de columna.

    SUM/AVG exigen una columna numérica: se rechaza antes de tocar la BD con un mensaje
    claro (en vez de dejar que DuckDB falle con un error técnico).
    """
    if measure.column is None:
        if measure.aggregation is not Aggregation.COUNT:
            raise InvalidFilterError(
                f"La agregación '{measure.aggregation.value}' requiere una columna."
            )
        return None
    if measure.aggregation in _NUMERIC_AGG and measure.column not in numeric_columns:
        raise InvalidFilterError(
            f"La agregación '{measure.aggregation.value}' requiere una columna numérica; "
            f"'{measure.column}' no lo es."
        )
    return quote_column(measure.column, valid_columns)


def _simple_measures_sql(
    measures: list[Measure], valid_columns: set[str], numeric_columns: set[str],
    percent: bool, windowed: bool,
) -> list[str]:
    """Métricas sin cross-tab: una columna de salida por métrica."""
    pieces: list[str] = []
    for measure in measures:
        col_sql = _measure_column_sql(measure, valid_columns, numeric_columns)
        expr = _value_expr(measure, col_sql or "", col_sql is not None, col_sql, percent, windowed)
        pieces.append(f"{expr} AS {_quote_alias(_measure_alias(measure))}")
    return pieces


def _crosstab_measures_sql(
    measures: list[Measure], valid_columns: set[str], numeric_columns: set[str],
    pivot_col_sql: str, pivot_values: list, percent: bool, windowed: bool,
) -> tuple[list[str], list]:
    """Métricas con cross-tab: una columna por (valor distinto × métrica).

    El valor del pivote va como parámetro ('?'); el alias de salida (visible) usa su
    representación textual, citada. Con una sola métrica el alias es solo el valor;
    con varias se añade el nombre de la métrica para desambiguar. En agregaciones
    aditivas (SUMA/Conteo/Únicos) una celda sin datos se muestra como 0, no NULL.
    """
    pieces: list[str] = []
    params: list = []
    single = len(measures) == 1
    for value in pivot_values:
        for measure in measures:
            col_sql = _measure_column_sql(measure, valid_columns, numeric_columns)
            inner = f"CASE WHEN {pivot_col_sql} = ? THEN {col_sql or '1'} END"
            expr = _value_expr(measure, inner, True, col_sql, percent, windowed)
            if measure.aggregation in _ZERO_ON_EMPTY:
                expr = f"coalesce({expr}, 0)"
            label = str(value) if single else f"{value} · {_measure_alias(measure)}"
            pieces.append(f"{expr} AS {_quote_alias(label)}")
            params.append(value)
    return pieces, params


def _row_total_pieces(
    measures: list[Measure], valid_columns: set[str], numeric_columns: set[str],
    percent: bool, windowed: bool,
) -> list[str]:
    """Columna(s) de total por fila del cross-tab: la métrica sobre TODOS los valores.

    Es la misma agregación sin el filtro por valor del pivote, así el total refleja la
    fila completa (p. ej. suma total = suma PEN + suma USD). En modo %, cada fila suma 100.
    """
    single = len(measures) == 1
    pieces: list[str] = []
    for measure in measures:
        col_sql = _measure_column_sql(measure, valid_columns, numeric_columns)
        expr = _value_expr(measure, col_sql or "", col_sql is not None, col_sql, percent, windowed)
        label = _ROW_TOTAL_LABEL if single else f"{_ROW_TOTAL_LABEL} · {_measure_alias(measure)}"
        pieces.append(f"{expr} AS {_quote_alias(label)}")
    return pieces


def _measure_pieces(
    request: PivotSpec, valid_columns: set[str], numeric_columns: set[str],
    pivot_values: list | None, windowed: bool,
) -> tuple[list[str], list]:
    """Piezas de agregación del SELECT (sin las columnas de agrupación).

    Sin cross-tab: una columna por métrica. Con cross-tab: una columna por
    (valor × métrica) más una columna Total por métrica. Compartido por el reporte
    paginado y la fila de Total general, de modo que sus alias coinciden (DRY).

    `windowed` distingue el reporte agrupado (True: el total del % se toma con ventana)
    de la fila de Total general (False: sin GROUP BY, el total es la agregación global).
    """
    percent = request.percent_of_total
    if pivot_values is None:
        return _simple_measures_sql(
            request.measures, valid_columns, numeric_columns, percent, windowed
        ), []
    pivot_col_sql = quote_column(request.pivot_column, valid_columns)
    pieces, params = _crosstab_measures_sql(
        request.measures, valid_columns, numeric_columns, pivot_col_sql,
        pivot_values, percent, windowed,
    )
    pieces += _row_total_pieces(
        request.measures, valid_columns, numeric_columns, percent, windowed
    )
    return pieces, params


def _validate_percent(measures: list[Measure]) -> None:
    """El '% del total' solo aplica a métricas aditivas (Suma/Conteo)."""
    for measure in measures:
        if measure.aggregation not in _PERCENT_AGG:
            raise InvalidFilterError(
                "El '% del total' solo aplica a métricas de Suma o Conteo."
            )


def _order_sql(request: PivotSpec, valid_columns: set[str], group_cols_sql: str) -> str | None:
    """Cláusula ORDER BY del reporte, o None para el orden por defecto (agrupación).

    Ordenar por una métrica usa su alias de salida (generado en servidor); en un
    cross-tab, ordena por la columna Total de esa métrica. Se añaden las columnas de
    agrupación como desempate para un orden estable.
    """
    sort = request.sort
    if sort is None or (sort.column is None and sort.measure_index is None):
        return None
    direction = "DESC" if sort.direction is SortDirection.DESC else "ASC"
    if sort.column is not None:
        if sort.column not in request.group_by:
            raise InvalidFilterError(
                "Solo se puede ordenar por una columna de agrupación o una métrica."
            )
        return f"{quote_column(sort.column, valid_columns)} {direction}, {group_cols_sql}"
    if sort.measure_index >= len(request.measures):
        raise InvalidFilterError("La métrica por la que se quiere ordenar no existe.")
    measure = request.measures[sort.measure_index]
    if request.pivot_column is not None:
        single = len(request.measures) == 1
        label = _ROW_TOTAL_LABEL if single else f"{_ROW_TOTAL_LABEL} · {_measure_alias(measure)}"
    else:
        label = _measure_alias(measure)
    return f"{_quote_alias(label)} {direction}, {group_cols_sql}"


def build_pivot(
    request: PivotSpec, valid_columns: set[str], numeric_columns: set[str],
    pivot_values: list | None,
) -> tuple[str, str, str | None, list]:
    """Arma el pivote. Devuelve (select_sql, group_cols_sql, order_sql, select_params).

    `pivot_values` son los valores distintos de la columna de cross-tab (obtenidos por
    el servicio con un DISTINCT), o None si no hay cross-tab. `order_sql` es None cuando
    se usa el orden por defecto (por columnas de agrupación).
    """
    if request.percent_of_total:
        _validate_percent(request.measures)
    group_cols = [quote_column(name, valid_columns) for name in request.group_by]
    group_cols_sql = ", ".join(group_cols)

    if request.pivot_column is not None:
        if request.pivot_column in request.group_by:
            raise InvalidFilterError(
                "La columna de cross-tab no puede ser también una columna de agrupación."
            )
        if len(pivot_values or []) > MAX_PIVOT_COLUMNS:
            raise InvalidFilterError(
                f"El cross-tab generaría demasiadas columnas (máximo {MAX_PIVOT_COLUMNS})."
            )

    measure_pieces, select_params = _measure_pieces(
        request, valid_columns, numeric_columns, pivot_values, windowed=True
    )
    select_sql = ", ".join(group_cols + measure_pieces)
    order_sql = _order_sql(request, valid_columns, group_cols_sql)
    return select_sql, group_cols_sql, order_sql, select_params


def build_pivot_totals(
    request: PivotSpec, valid_columns: set[str], numeric_columns: set[str],
    pivot_values: list | None,
) -> tuple[str, list]:
    """Arma el SELECT de la fila de Total general (sin GROUP BY). Devuelve (select, params).

    Usa las mismas piezas de agregación que `build_pivot`, así los alias (y por tanto las
    claves del dict resultante) coinciden con las columnas del reporte. Sin GROUP BY, el
    total del % se calcula sobre todas las filas (windowed=False).
    """
    if request.percent_of_total:
        _validate_percent(request.measures)
    pieces, params = _measure_pieces(
        request, valid_columns, numeric_columns, pivot_values, windowed=False
    )
    return ", ".join(pieces), params
