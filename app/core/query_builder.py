"""Construcción SEGURA de consultas de filtrado (defensa central contra A03 - Injection).

Reglas invariantes:
  1. Los nombres de columna se validan contra el esquema real del dataset (whitelist).
     Una columna desconocida => error, jamás se interpola texto arbitrario como identificador.
  2. Los operadores provienen de un enum cerrado (schemas.filter.Operator).
  3. Los VALORES del usuario NUNCA se concatenan: van siempre como parámetros ('?')
     que DuckDB enlaza (bind) de forma parametrizada.

El módulo devuelve fragmentos SQL + la lista de parámetros posicionales, en orden.
"""
from app.schemas.filter import (
    Combinator,
    FilterCondition,
    Operator,
    SortSpec,
)


class InvalidFilterError(ValueError):
    """La petición de filtro referencia columnas/operadores inválidos."""


def _quote_ident(name: str, valid_columns: set[str]) -> str:
    """Valida contra whitelist y devuelve el identificador citado de forma segura."""
    if name not in valid_columns:
        raise InvalidFilterError(f"Columna desconocida: {name!r}")
    # Cita estilo DuckDB; escapa comillas dobles internas por robustez.
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _escape_like(value: str) -> str:
    """Escapa comodines LIKE para búsquedas literales de subcadena."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _condition_sql(cond: FilterCondition, valid_columns: set[str]) -> tuple[str, list]:
    """Traduce una condición a (fragmento_sql, params). Los valores van como '?'."""
    col = _quote_ident(cond.column, valid_columns)
    op = cond.operator

    simple_ops = {
        Operator.EQ: "=",
        Operator.NE: "!=",
        Operator.GT: ">",
        Operator.GTE: ">=",
        Operator.LT: "<",
        Operator.LTE: "<=",
    }
    if op in simple_ops:
        _require_scalar(cond)
        return f"{col} {simple_ops[op]} ?", [cond.value]

    if op is Operator.IS_NULL:
        return f"{col} IS NULL", []
    if op is Operator.IS_NOT_NULL:
        return f"{col} IS NOT NULL", []

    if op in (Operator.CONTAINS, Operator.NOT_CONTAINS, Operator.STARTS_WITH, Operator.ENDS_WITH):
        _require_scalar(cond)
        literal = _escape_like(str(cond.value))
        if op is Operator.STARTS_WITH:
            pattern = f"{literal}%"
        elif op is Operator.ENDS_WITH:
            pattern = f"%{literal}"
        else:
            pattern = f"%{literal}%"
        negate = "NOT " if op is Operator.NOT_CONTAINS else ""
        return f"CAST({col} AS VARCHAR) {negate}LIKE ? ESCAPE '\\'", [pattern]

    if op in (Operator.IN, Operator.NOT_IN):
        values = _require_list(cond)
        negate = "NOT " if op is Operator.NOT_IN else ""
        if not values:
            # IN vacío => nada; NOT IN vacío => todo.
            return ("FALSE" if op is Operator.IN else "TRUE"), []
        placeholders = ", ".join("?" for _ in values)
        return f"{col} {negate}IN ({placeholders})", list(values)

    if op is Operator.BETWEEN:
        values = _require_list(cond)
        if len(values) != 2:
            raise InvalidFilterError("BETWEEN requiere exactamente 2 valores.")
        return f"{col} BETWEEN ? AND ?", [values[0], values[1]]

    raise InvalidFilterError(f"Operador no soportado: {op!r}")


def _require_scalar(cond: FilterCondition) -> None:
    if cond.value is None or isinstance(cond.value, list):
        raise InvalidFilterError(f"El operador {cond.operator.value!r} requiere un valor escalar.")


def _require_list(cond: FilterCondition) -> list:
    if not isinstance(cond.value, list):
        raise InvalidFilterError(f"El operador {cond.operator.value!r} requiere una lista de valores.")
    return cond.value


def build_where(
    conditions: list[FilterCondition],
    combinator: Combinator,
    valid_columns: set[str],
) -> tuple[str, list]:
    """Devuelve (clausula_where_sin_keyword, params). Cadena vacía si no hay condiciones."""
    if not conditions:
        return "", []
    fragments: list[str] = []
    params: list = []
    for cond in conditions:
        fragment, cond_params = _condition_sql(cond, valid_columns)
        fragments.append(f"({fragment})")
        params.extend(cond_params)
    joiner = " AND " if combinator is Combinator.AND else " OR "
    return joiner.join(fragments), params


def build_select(select: list[str], valid_columns: set[str]) -> str:
    """Lista de columnas a proyectar. Vacío => '*'. Cada columna validada."""
    if not select:
        return "*"
    return ", ".join(_quote_ident(name, valid_columns) for name in select)


def build_order_by(sort: list[SortSpec], valid_columns: set[str]) -> str:
    """Cláusula ORDER BY validada. Cadena vacía si no hay orden."""
    if not sort:
        return ""
    parts = [f"{_quote_ident(spec.column, valid_columns)} {spec.direction.value.upper()}" for spec in sort]
    return "ORDER BY " + ", ".join(parts)
