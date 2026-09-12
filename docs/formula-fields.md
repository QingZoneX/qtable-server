# QTable Formula Fields

## Data contract

A formula field uses the existing field schema and stores its expression in `property`:

```json
{
  "id": "f_total",
  "name": "Total",
  "type": "formula",
  "property": {
    "formula": "{{f_price}} * {{f_quantity}}",
    "resultType": "auto"
  }
}
```

Formula references use `{{fieldId}}` rather than field names. Field IDs are stable, so renaming a field does not invalidate existing formulas.

Formula values are derived values. They are materialized when the table is read or published through realtime updates and are not treated as authoritative values in `TableRecord.data`.

## Supported operators

- Arithmetic: `+`, `-`, `*`, `/`, `//`, `%`, `**`
- Comparison: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`
- Boolean: `and`, `or`, `not`
- Conditional expression: Python-style `a if condition else b`

## Supported functions

Function names are accepted in uppercase or lowercase.

- `IF(condition, trueValue, falseValue)`
- `CONCAT(value1, value2, ...)`
- `SUM(value1, value2, ...)`
- `AVERAGE(value1, value2, ...)`
- `MIN(value1, value2, ...)`
- `MAX(value1, value2, ...)`
- `ROUND(value, digits)`
- `ABS(value)`
- `LEN(value)`
- `LOWER(value)`
- `UPPER(value)`
- `COALESCE(value1, value2, ...)`
- `AND(value1, value2, ...)`
- `OR(value1, value2, ...)`
- `NOT(value)`

## Error values

Runtime errors are returned as displayable values instead of failing the entire table query:

- `#REF!` — referenced field is missing
- `#CYCLE!` — circular formula dependency
- `#DIV/0!` — division by zero
- `#NUM!` — numeric overflow/non-finite result
- `#ERROR!` — other runtime type/evaluation errors

Invalid formula configuration is rejected before persistence. This includes syntax errors, unknown functions, unsupported expressions, missing references, and circular dependencies.

## Safety limits

- Formula length: maximum 2,000 characters
- Referenced fields: maximum 64 per formula
- String result: maximum 10,000 characters
- Formula evaluation uses `simpleeval` plus an AST allow-list. Attribute access, arbitrary Python calls, lambdas, comprehensions, imports, and other executable Python constructs are rejected.

## Write semantics

Formula fields are read-only at the service layer. Direct single-cell writes and record patches targeting formula fields are rejected. Batch row creation/initialization strips incoming values for formula fields.

Deleting a field that is referenced by one or more formula fields is blocked until those formulas are changed or removed.
