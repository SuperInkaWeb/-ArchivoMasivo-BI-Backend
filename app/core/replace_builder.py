"""Construcción SEGURA del buscar-y-reemplazar por columna.

Genera un SELECT que reescribe, en su sitio, solo las columnas con reglas y deja
el resto intactas. Mismas invariantes que el resto de builders (defensa A03):
  1. Los nombres de columna se validan contra el esquema real (whitelist).
  2. El modo de coincidencia proviene de un enum CERRADO (schemas.analysis.MatchMode).
  3. Los textos de búsqueda/reemplazo del usuario van SIEMPRE como parámetros ('?').

Las columnas corregidas se emiten como texto (CAST ... AS VARCHAR): un reemplazo
es una operación de texto y así el CASE/replace nunca choca de tipos.

Semántica cuando una columna tiene varias reglas: las coincidencias EXACT se
evalúan sobre el valor original (un único CASE, primera que encaje gana) y las de
CONTAINS se aplican después. Se evita anidar CASE (que duplicaría subexpresiones y
sus parámetros); todas las construcciones dejan el nº de '?' lineal en el nº de reglas.
"""
from app.core.query_builder import InvalidFilterError, quote_column
from app.schemas.analysis import MatchMode, ReplacementRule


def _quote_alias(alias: str) -> str:
    """Cita un alias de salida como identificador SQL seguro (escapa comillas dobles)."""
    return '"' + alias.replace('"', '""') + '"'


def _column_expr(quoted: str, rules: list[ReplacementRule]) -> tuple[str, list]:
    """Expresión de una columna con reglas. Valores del usuario SIEMPRE como parámetros."""
    base = f"CAST({quoted} AS VARCHAR)"
    params: list = []
    expr = base

    exact = [rule for rule in rules if rule.mode is MatchMode.EXACT]
    if exact:
        whens: list[str] = []
        for rule in exact:
            compared = base if rule.case_sensitive else f"lower({base})"
            operand = "?" if rule.case_sensitive else "lower(?)"
            whens.append(f"WHEN {compared} = {operand} THEN ?")
            params.extend([rule.search, rule.replace])
        expr = f"CASE {' '.join(whens)} ELSE {base} END"

    for rule in rules:
        if rule.mode is MatchMode.CONTAINS:
            expr = f"replace({expr}, ?, ?)"  # replace() referencia su entrada una sola vez
            params.extend([rule.search, rule.replace])
    return expr, params


def build_replace_select(
    replacements: list[ReplacementRule], column_order: list[str], valid_columns: set[str],
) -> tuple[str, list]:
    """Arma 'SELECT col1, <correcciones> AS col2, ...' preservando el orden y los nombres."""
    by_column: dict[str, list[ReplacementRule]] = {}
    for rule in replacements:
        if rule.column not in valid_columns:
            raise InvalidFilterError(f"La columna '{rule.column}' no existe en el archivo.")
        by_column.setdefault(rule.column, []).append(rule)

    pieces: list[str] = []
    params: list = []
    for name in column_order:
        quoted = quote_column(name, valid_columns)
        rules = by_column.get(name)
        if not rules:
            pieces.append(quoted)  # sin reglas: se emite tal cual (conserva nombre y tipo)
            continue
        expr, col_params = _column_expr(quoted, rules)
        params.extend(col_params)
        pieces.append(f"{expr} AS {_quote_alias(name)}")
    return ", ".join(pieces), params
