"""Construcción SEGURA de consultas de filtrado (defensa central contra A03 - Injection).

El filtro es un ÁRBOL recursivo (grupos AND/OR con hijos hoja o grupo). Reglas invariantes:
  1. Los nombres de columna se validan contra el esquema real del dataset (whitelist).
     Una columna desconocida => error, jamás se interpola texto arbitrario como identificador.
  2. Los operadores provienen de un enum cerrado (schemas.filter.Operator).
  3. Los VALORES del usuario NUNCA se concatenan: van siempre como parámetros ('?')
     que DuckDB enlaza (bind) de forma parametrizada.
  4. El árbol tiene límites de profundidad y de número de nodos (defensa A04) para
     evitar payloads patológicos.

Devuelve fragmentos SQL + la lista de parámetros posicionales, en orden.
"""
from app.schemas.filter import (
    Combinator,
    FilterGroup,
    FilterLeaf,
    FilterNode,
    Operator,
    SortSpec,
)

_MAX_DEPTH = 6      # niveles de anidamiento permitidos
_MAX_NODES = 200    # nodos totales (hojas + grupos) permitidos


class InvalidFilterError(ValueError):
    """La petición de filtro referencia columnas/operadores inválidos o excede límites."""


def _quote_ident(name: str, valid_columns: set[str]) -> str:
    """Valida contra whitelist y devuelve el identificador citado de forma segura."""
    if name not in valid_columns:
        raise InvalidFilterError(f"Columna desconocida: {name!r}")
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def quote_column(name: str, valid_columns: set[str]) -> str:
    """Valida una columna contra la whitelist y devuelve su identificador citado."""
    return _quote_ident(name, valid_columns)


def _escape_like(value: str) -> str:
    """Escapa comodines LIKE para búsquedas literales de subcadena."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _leaf_sql(leaf: FilterLeaf, valid_columns: set[str]) -> tuple[str, list]:
    """Traduce una condición (hoja) a (fragmento_sql, params). Los valores van como '?'."""
    col = _quote_ident(leaf.column, valid_columns)
    op = leaf.operator

    simple_ops = {
        Operator.EQ: "=",
        Operator.NE: "!=",
        Operator.GT: ">",
        Operator.GTE: ">=",
        Operator.LT: "<",
        Operator.LTE: "<=",
    }
    if op in simple_ops:
        _require_scalar(leaf)
        return f"{col} {simple_ops[op]} ?", [leaf.value]

    if op is Operator.IS_NULL:
        return f"{col} IS NULL", []
    if op is Operator.IS_NOT_NULL:
        return f"{col} IS NOT NULL", []

    if op in (Operator.CONTAINS, Operator.NOT_CONTAINS, Operator.STARTS_WITH, Operator.ENDS_WITH):
        _require_scalar(leaf)
        literal = _escape_like(str(leaf.value))
        if op is Operator.STARTS_WITH:
            pattern = f"{literal}%"
        elif op is Operator.ENDS_WITH:
            pattern = f"%{literal}"
        else:
            pattern = f"%{literal}%"
        negate = "NOT " if op is Operator.NOT_CONTAINS else ""
        return f"CAST({col} AS VARCHAR) {negate}LIKE ? ESCAPE '\\'", [pattern]

    if op in (Operator.IN, Operator.NOT_IN):
        values = _require_list(leaf)
        negate = "NOT " if op is Operator.NOT_IN else ""
        if not values:
            return ("FALSE" if op is Operator.IN else "TRUE"), []
        placeholders = ", ".join("?" for _ in values)
        return f"{col} {negate}IN ({placeholders})", list(values)

    if op is Operator.BETWEEN:
        values = _require_list(leaf)
        if len(values) != 2:
            raise InvalidFilterError("BETWEEN requiere exactamente 2 valores.")
        return f"{col} BETWEEN ? AND ?", [values[0], values[1]]

    raise InvalidFilterError(f"Operador no soportado: {op!r}")


def _require_scalar(leaf: FilterLeaf) -> None:
    if leaf.value is None or isinstance(leaf.value, list):
        raise InvalidFilterError(f"El operador {leaf.operator.value!r} requiere un valor escalar.")


def _require_list(leaf: FilterLeaf) -> list:
    if not isinstance(leaf.value, list):
        raise InvalidFilterError(f"El operador {leaf.operator.value!r} requiere una lista de valores.")
    return leaf.value


def _measure(node: FilterNode, depth: int = 1) -> tuple[int, int]:
    """Devuelve (profundidad_máxima, número_de_nodos) del subárbol."""
    if isinstance(node, FilterLeaf) or not node.children:
        return depth, 1
    max_depth = depth
    total = 1
    for child in node.children:
        child_depth, child_count = _measure(child, depth + 1)
        max_depth = max(max_depth, child_depth)
        total += child_count
    return max_depth, total


def _assert_limits(root: FilterNode) -> None:
    depth, count = _measure(root)
    if depth > _MAX_DEPTH:
        raise InvalidFilterError(f"El filtro está demasiado anidado (máximo {_MAX_DEPTH} niveles).")
    if count > _MAX_NODES:
        raise InvalidFilterError(f"El filtro tiene demasiadas condiciones (máximo {_MAX_NODES}).")


def _build_node(node: FilterNode, valid_columns: set[str]) -> tuple[str, list]:
    """Construye recursivamente el SQL de un nodo (hoja o grupo)."""
    if isinstance(node, FilterLeaf):
        return _leaf_sql(node, valid_columns)

    fragments: list[str] = []
    params: list = []
    for child in node.children:
        fragment, child_params = _build_node(child, valid_columns)
        if fragment:  # ignora grupos vacíos
            fragments.append(f"({fragment})")
            params.extend(child_params)
    if not fragments:
        return "", []
    joiner = " AND " if node.combinator is Combinator.AND else " OR "
    return joiner.join(fragments), params


def build_where(root: FilterGroup | None, valid_columns: set[str]) -> tuple[str, list]:
    """Devuelve (clausula_where_sin_keyword, params). Cadena vacía si no hay filtro."""
    if root is None:
        return "", []
    _assert_limits(root)
    return _build_node(root, valid_columns)


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
