"""Turn a QGIS filter expression into the conditions of a GeoParquet query.

Only comparisons, IN, LIKE, IS NULL, AND, OR, and NOT turn into SQL.
"""

from __future__ import annotations

from qgis.core import (
    QgsExpression,
    QgsExpressionNode,
    QgsExpressionNodeBinaryOperator,
    QgsExpressionNodeColumnRef,
    QgsExpressionNodeInOperator,
    QgsExpressionNodeLiteral,
    QgsExpressionNodeUnaryOperator,
    QgsVariantUtils,
)

from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.query import (
    And,
    Compare,
    Condition,
    InList,
    IsNull,
    Like,
    Not,
    Or,
    QueryError,
    Value,
)

_Binary = QgsExpressionNodeBinaryOperator.BinaryOperator
_Unary = QgsExpressionNodeUnaryOperator.UnaryOperator
_COMPARE = {
    _Binary.boEQ: "=",
    _Binary.boNE: "<>",
    _Binary.boLT: "<",
    _Binary.boLE: "<=",
    _Binary.boGT: ">",
    _Binary.boGE: ">=",
}
# Each LIKE operator, and whether it is negated and ignores case.
_LIKE = {
    _Binary.boLike: (False, False),
    _Binary.boNotLike: (True, False),
    _Binary.boILike: (False, True),
    _Binary.boNotILike: (True, True),
}


class FilterError(QueryError):
    """The filter is not an expression that the plugin turns into SQL."""


def _unsupported() -> FilterError:
    return FilterError(
        _(
            "The filter can use comparisons, IN, LIKE, IS NULL, AND, OR, and NOT. "
            "For anything else, check Write my own SQL."
        )
    )


def _column(node: QgsExpressionNode) -> str:
    if not isinstance(node, QgsExpressionNodeColumnRef):
        raise _unsupported()
    return str(node.name())


def _value(node: QgsExpressionNode) -> Value | None:
    """Return the value of a literal, or of a negative number. NULL gives None."""
    if (
        isinstance(node, QgsExpressionNodeUnaryOperator)
        and node.op() == _Unary.uoMinus
        and isinstance(node.operand(), QgsExpressionNodeLiteral)
    ):
        value = _value(node.operand())
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return -value
        raise _unsupported()
    if not isinstance(node, QgsExpressionNodeLiteral):
        raise _unsupported()
    value = node.value()
    if QgsVariantUtils.isNull(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    raise _unsupported()


def _known(value: Value | None) -> Value:
    if value is None:
        raise FilterError(_("Compare with NULL as IS NULL or IS NOT NULL"))
    return value


def _binary(node: QgsExpressionNodeBinaryOperator) -> Condition:
    op = node.op()
    if op in {_Binary.boAnd, _Binary.boOr}:
        kind = And if op == _Binary.boAnd else Or
        return kind(_condition(node.opLeft()), _condition(node.opRight()))
    column = _column(node.opLeft())
    value = _value(node.opRight())
    if op in {_Binary.boIs, _Binary.boIsNot}:
        if value is not None:
            raise _unsupported()
        return IsNull(column, negated=op == _Binary.boIsNot)
    if op in _COMPARE:
        return Compare(column, _COMPARE[op], _known(value))
    if op in _LIKE and isinstance(value, str):
        negated, ignore_case = _LIKE[op]
        return Like(column, value, negated=negated, ignore_case=ignore_case)
    raise _unsupported()


def _condition(node: QgsExpressionNode) -> Condition:
    if isinstance(node, QgsExpressionNodeBinaryOperator):
        return _binary(node)
    if isinstance(node, QgsExpressionNodeUnaryOperator) and node.op() == _Unary.uoNot:
        return Not(_condition(node.operand()))
    if isinstance(node, QgsExpressionNodeInOperator):
        values = tuple(_known(_value(item)) for item in node.list().list())
        return InList(_column(node.node()), values, negated=node.isNotIn())
    raise _unsupported()


def parse_filter(text: str) -> Condition:
    """Return the conditions of the QGIS expression ``text``.

    Raises:
        FilterError: The text does not parse, or uses a form outside the list.
    """
    expression = QgsExpression(text)
    if expression.hasParserError():
        first = expression.parserErrorString().strip().splitlines()[0]
        raise FilterError(_("The filter does not parse: {error}").format(error=first))
    root = expression.rootNode()
    if root is None:
        raise FilterError(_("The filter is empty"))
    return _condition(root)
