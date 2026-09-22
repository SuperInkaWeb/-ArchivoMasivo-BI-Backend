"""DTOs de expresiones para columnas calculadas (árbol cerrado y seguro).

Una expresión es un ÁRBOL de nodos:
  - ColumnExpr   = referencia a una columna del dataset (se valida contra el esquema).
  - LiteralExpr  = un valor constante (se enlaza SIEMPRE como parámetro '?').
  - FunctionExpr = una función de un enum CERRADO aplicada a sub-expresiones.

Mismo principio de seguridad que el filtrado (A03): las funciones vienen de un
conjunto fijo, las columnas se validan por whitelist y los valores del usuario
nunca se concatenan en el SQL. Cualquier función fuera del enum se rechaza.
"""
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class FunctionName(str, Enum):
    # Texto
    CONCAT = "concat"      # unir columnas/valores en uno
    UPPER = "upper"
    LOWER = "lower"
    TRIM = "trim"
    LENGTH = "length"
    SUBSTR = "substr"      # (texto, inicio[, longitud])
    REPLACE = "replace"    # (texto, buscar, reemplazo)
    # Aritmética
    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"            # división segura (NULL si el divisor es 0)
    ROUND = "round"        # (numero[, decimales])
    # Condicional y comparación (para IF)
    IF = "if"              # (condicion, entonces, si_no)
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    AND = "and"
    OR = "or"
    NOT = "not"
    # Fecha
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    DATEDIFF_DAYS = "datediff_days"  # (fecha_a, fecha_b) -> días entre ambas


ExprLiteralValue = str | int | float | bool | None


class ColumnExpr(BaseModel):
    kind: Literal["column"] = "column"
    name: str


class LiteralExpr(BaseModel):
    kind: Literal["literal"] = "literal"
    value: ExprLiteralValue = None


class FunctionExpr(BaseModel):
    kind: Literal["function"] = "function"
    fn: FunctionName
    args: list["Expression"] = Field(default_factory=list)


# Unión discriminada por "kind"; recursiva vía FunctionExpr.args.
Expression = Annotated[
    Union[ColumnExpr, LiteralExpr, FunctionExpr], Field(discriminator="kind")
]
FunctionExpr.model_rebuild()


class ComputedColumn(BaseModel):
    """Una columna nueva: su nombre de salida y la expresión que la calcula."""
    name: str = Field(min_length=1, max_length=100)
    expression: Expression
