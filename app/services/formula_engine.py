"""Safe formula evaluation for SmartTable derived fields.

Formula fields are stored as normal field metadata with::

    {
        "type": "formula",
        "property": {
            "formula": "{{f_price}} * {{f_quantity}}",
            "resultType": "auto"
        }
    }

Field references intentionally use stable field IDs instead of names so field
renames do not break formulas. Formula results are materialized on reads and
are never persisted as authoritative record values.
"""
from __future__ import annotations

import ast
import math
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Set, Tuple
import re

from simpleeval import DEFAULT_OPERATORS, SimpleEval

FORMULA_FIELD_TYPE = "formula"
FORMULA_ERROR = "#ERROR!"
FORMULA_REF_ERROR = "#REF!"
FORMULA_CYCLE_ERROR = "#CYCLE!"
FORMULA_DIV_ZERO_ERROR = "#DIV/0!"
FORMULA_NUM_ERROR = "#NUM!"

MAX_FORMULA_LENGTH = 2000
MAX_FIELD_REFERENCES = 64
MAX_STRING_RESULT_LENGTH = 10000

_FIELD_REF_RE = re.compile(r"\{\{([A-Za-z0-9_.:-]+)\}\}")


class FormulaValidationError(ValueError):
    """Raised when formula metadata is invalid and must not be persisted."""


def _if(condition: Any, value_if_true: Any, value_if_false: Any) -> Any:
    return value_if_true if bool(condition) else value_if_false


def _concat(*values: Any) -> str:
    return "".join("" if value is None else str(value) for value in values)


def _flatten(values: Iterable[Any]) -> List[Any]:
    flattened: List[Any] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            flattened.extend(value)
        else:
            flattened.append(value)
    return flattened


def _numeric_values(values: Iterable[Any]) -> List[Any]:
    return [value for value in _flatten(values) if value is not None and value != ""]


def _sum(*values: Any) -> Any:
    items = _numeric_values(values)
    return sum(items) if items else 0


def _average(*values: Any) -> Any:
    items = _numeric_values(values)
    return sum(items) / len(items) if items else None


def _min(*values: Any) -> Any:
    items = _numeric_values(values)
    return min(items) if items else None


def _max(*values: Any) -> Any:
    items = _numeric_values(values)
    return max(items) if items else None


def _round(value: Any, digits: Any = 0) -> Any:
    return round(value, int(digits))


def _length(value: Any) -> int:
    if value is None:
        return 0
    return len(value)


def _lower(value: Any) -> str:
    return "" if value is None else str(value).lower()


def _upper(value: Any) -> str:
    return "" if value is None else str(value).upper()


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _and(*values: Any) -> bool:
    return all(bool(value) for value in values)


def _or(*values: Any) -> bool:
    return any(bool(value) for value in values)


def _not(value: Any) -> bool:
    return not bool(value)


_BASE_FUNCTIONS = {
    "IF": _if,
    "CONCAT": _concat,
    "SUM": _sum,
    "AVERAGE": _average,
    "MIN": _min,
    "MAX": _max,
    "ROUND": _round,
    "ABS": abs,
    "LEN": _length,
    "LOWER": _lower,
    "UPPER": _upper,
    "COALESCE": _coalesce,
    "AND": _and,
    "OR": _or,
    "NOT": _not,
}
SAFE_FUNCTIONS: Dict[str, Any] = {
    **_BASE_FUNCTIONS,
    **{name.lower(): fn for name, fn in _BASE_FUNCTIONS.items()},
}

_SAFE_OPERATOR_NODES = {
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.And,
    ast.Or,
    ast.Not,
    ast.In,
    ast.NotIn,
}
SAFE_OPERATORS = {
    node: DEFAULT_OPERATORS[node]
    for node in _SAFE_OPERATOR_NODES
    if node in DEFAULT_OPERATORS
}

_ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
)


def is_formula_field(field: Mapping[str, Any]) -> bool:
    return str(field.get("type") or "").lower() == FORMULA_FIELD_TYPE


def get_formula_expression(field: Mapping[str, Any]) -> str:
    prop = field.get("property")
    if not isinstance(prop, Mapping):
        return ""
    formula = prop.get("formula")
    return formula.strip() if isinstance(formula, str) else ""


def get_formula_references(expression: str) -> List[str]:
    return _FIELD_REF_RE.findall(expression or "")


def _compile_formula(expression: str) -> Tuple[str, Dict[str, str]]:
    expression = (expression or "").strip()
    if not expression:
        raise FormulaValidationError("Formula expression is required")
    if len(expression) > MAX_FORMULA_LENGTH:
        raise FormulaValidationError(
            f"Formula expression exceeds {MAX_FORMULA_LENGTH} characters"
        )

    ref_to_name: Dict[str, str] = {}

    def replace_reference(match: re.Match[str]) -> str:
        field_id = match.group(1)
        if field_id not in ref_to_name:
            ref_to_name[field_id] = f"__field_{len(ref_to_name)}"
        return ref_to_name[field_id]

    transformed = _FIELD_REF_RE.sub(replace_reference, expression)
    if len(ref_to_name) > MAX_FIELD_REFERENCES:
        raise FormulaValidationError(
            f"Formula references more than {MAX_FIELD_REFERENCES} fields"
        )

    try:
        parsed = ast.parse(transformed, mode="eval")
    except SyntaxError as exc:
        raise FormulaValidationError(f"Invalid formula syntax: {exc.msg}") from exc

    allowed_variable_names = set(ref_to_name.values())
    for node in ast.walk(parsed):
        if isinstance(node, tuple(_SAFE_OPERATOR_NODES)):
            continue
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise FormulaValidationError(
                f"Unsupported formula expression: {type(node).__name__}"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNCTIONS:
                raise FormulaValidationError("Formula function is not allowed")
            if node.keywords:
                raise FormulaValidationError("Formula functions do not support named arguments")
        if isinstance(node, ast.Name):
            is_function_name = node.id in SAFE_FUNCTIONS
            if not is_function_name and node.id not in allowed_variable_names:
                raise FormulaValidationError(f"Unknown formula name: {node.id}")

    return transformed, ref_to_name


def validate_formula_schema(fields: Sequence[Mapping[str, Any]]) -> None:
    """Validate references, syntax and dependency cycles for all formula fields."""
    field_map: Dict[str, Mapping[str, Any]] = {}
    for field in fields:
        field_id = field.get("id")
        if not isinstance(field_id, str) or not field_id:
            raise FormulaValidationError("Every field must have a stable field id")
        if field_id in field_map:
            raise FormulaValidationError(f"Duplicate field id: {field_id}")
        field_map[field_id] = field

    dependency_map: Dict[str, Set[str]] = {}
    for field_id, field in field_map.items():
        if not is_formula_field(field):
            continue
        expression = get_formula_expression(field)
        _compile_formula(expression)
        references = set(get_formula_references(expression))
        missing = sorted(ref for ref in references if ref not in field_map)
        if missing:
            raise FormulaValidationError(
                f"Formula field '{field.get('name') or field_id}' references missing field: {missing[0]}"
            )
        dependency_map[field_id] = {
            ref for ref in references if is_formula_field(field_map[ref])
        }

    visiting: Set[str] = set()
    visited: Set[str] = set()
    path: List[str] = []

    def visit(field_id: str) -> None:
        if field_id in visited:
            return
        if field_id in visiting:
            try:
                cycle_start = path.index(field_id)
                cycle = path[cycle_start:] + [field_id]
            except ValueError:
                cycle = [field_id, field_id]
            raise FormulaValidationError(
                "Formula dependency cycle detected: " + " -> ".join(cycle)
            )
        visiting.add(field_id)
        path.append(field_id)
        for dependency in dependency_map.get(field_id, set()):
            visit(dependency)
        path.pop()
        visiting.remove(field_id)
        visited.add(field_id)

    for formula_field_id in dependency_map:
        visit(formula_field_id)


def formula_dependents(fields: Sequence[Mapping[str, Any]], field_id: str) -> List[str]:
    dependents: List[str] = []
    for field in fields:
        if not is_formula_field(field):
            continue
        if field_id in set(get_formula_references(get_formula_expression(field))):
            dependents.append(str(field.get("name") or field.get("id") or "formula"))
    return dependents


def _normalize_result(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return FORMULA_NUM_ERROR
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        if isinstance(value, str) and len(value) > MAX_STRING_RESULT_LENGTH:
            return value[:MAX_STRING_RESULT_LENGTH]
        return value
    if isinstance(value, tuple):
        return [_normalize_result(item) for item in value]
    if isinstance(value, list):
        return [_normalize_result(item) for item in value]
    return str(value)


def _evaluate_formula_field(
    field_id: str,
    record: Mapping[str, Any],
    field_map: Mapping[str, Mapping[str, Any]],
    memo: MutableMapping[str, Any],
    visiting: Set[str],
) -> Any:
    if field_id in memo:
        return memo[field_id]

    field = field_map.get(field_id)
    if not field:
        return FORMULA_REF_ERROR
    if not is_formula_field(field):
        return record.get(field_id)
    if field_id in visiting:
        return FORMULA_CYCLE_ERROR

    visiting.add(field_id)
    try:
        expression = get_formula_expression(field)
        transformed, ref_to_name = _compile_formula(expression)
        names: Dict[str, Any] = {}
        for referenced_field_id, variable_name in ref_to_name.items():
            referenced_field = field_map.get(referenced_field_id)
            if not referenced_field:
                result = FORMULA_REF_ERROR
                memo[field_id] = result
                return result
            if is_formula_field(referenced_field):
                referenced_value = _evaluate_formula_field(
                    referenced_field_id, record, field_map, memo, visiting
                )
            else:
                referenced_value = record.get(referenced_field_id)
            if isinstance(referenced_value, str) and referenced_value in {
                FORMULA_ERROR,
                FORMULA_REF_ERROR,
                FORMULA_CYCLE_ERROR,
                FORMULA_DIV_ZERO_ERROR,
                FORMULA_NUM_ERROR,
            }:
                memo[field_id] = referenced_value
                return referenced_value
            names[variable_name] = referenced_value

        evaluator = SimpleEval(
            operators=SAFE_OPERATORS,
            functions=SAFE_FUNCTIONS,
            names=names,
        )
        result = _normalize_result(evaluator.eval(transformed))
    except ZeroDivisionError:
        result = FORMULA_DIV_ZERO_ERROR
    except (OverflowError, ArithmeticError):
        result = FORMULA_NUM_ERROR
    except Exception:
        # Formula configuration is strictly validated before persistence, but
        # runtime data can still make a valid expression fail (e.g. text * date).
        result = FORMULA_ERROR
    finally:
        visiting.discard(field_id)

    memo[field_id] = result
    return result


def materialize_records(
    fields: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Return record copies with formula values calculated from current data."""
    field_map = {
        str(field.get("id")): field
        for field in fields
        if isinstance(field.get("id"), str) and field.get("id")
    }
    formula_ids = [field_id for field_id, field in field_map.items() if is_formula_field(field)]
    if not formula_ids:
        return [dict(record) for record in records]

    materialized: List[Dict[str, Any]] = []
    for record in records:
        next_record = dict(record)
        memo: Dict[str, Any] = {}
        for formula_field_id in formula_ids:
            next_record[formula_field_id] = _evaluate_formula_field(
                formula_field_id,
                record,
                field_map,
                memo,
                set(),
            )
        materialized.append(next_record)
    return materialized


def materialize_store(store: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a store copy whose record collection contains derived formulas."""
    result = dict(store)
    fields = list(store.get("fields") or [])
    records = list(store.get("records") or [])
    result["fields"] = fields
    result["records"] = materialize_records(fields, records)
    return result
