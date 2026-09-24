"""Construcción SEGURA de la consulta de 'eliminar duplicados'.

Misma invariante que el resto (defensa A03 - Injection): los nombres de columna se
validan contra el esquema real (whitelist) y se citan; nunca se concatena texto del
usuario. Este builder no recibe valores del usuario, solo nombres de columna.

Dos modos:
  - Sin columnas clave: se comparan TODAS las columnas -> `SELECT DISTINCT *`
    (elimina filas exactamente repetidas).
  - Con columnas clave: se conserva la primera fila de cada combinación de esas
    columnas -> `QUALIFY row_number() OVER (PARTITION BY <claves>) = 1`.
"""
from app.core.query_builder import quote_column


def build_dedupe(key_columns: list[str], valid_columns: set[str]) -> tuple[str, str | None]:
    """Devuelve (select_sql, qualify_sql).

    `select_sql` es la proyección ("DISTINCT *" o "*") y `qualify_sql` la condición
    QUALIFY (o None si no aplica). El motor los compone con el FROM/WHERE.
    """
    if not key_columns:
        return "DISTINCT *", None
    keys = ", ".join(quote_column(name, valid_columns) for name in key_columns)
    return "*", f"row_number() OVER (PARTITION BY {keys}) = 1"
