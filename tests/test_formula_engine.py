import pytest

from app.services.formula_engine import (
    FORMULA_CYCLE_ERROR,
    FORMULA_DIV_ZERO_ERROR,
    FormulaValidationError,
    formula_dependents,
    materialize_records,
    materialize_store,
    validate_formula_schema,
)


def field(field_id: str, name: str, field_type: str = "number", formula: str | None = None):
    payload = {
        "id": field_id,
        "name": name,
        "type": field_type,
        "options": None,
        "property": None,
    }
    if field_type == "formula":
        payload["property"] = {"formula": formula or "", "resultType": "auto"}
    return payload


def test_materializes_arithmetic_formula_without_mutating_source_record():
    fields = [
        field("f_price", "Price"),
        field("f_qty", "Quantity"),
        field("f_total", "Total", "formula", "{{f_price}} * {{f_qty}}"),
    ]
    source = {"id": "r1", "f_price": 12.5, "f_qty": 4}

    result = materialize_records(fields, [source])

    assert result[0]["f_total"] == 50
    assert "f_total" not in source


def test_formula_references_are_stable_across_field_renames():
    fields = [
        field("f_price", "Renamed price"),
        field("f_qty", "Renamed quantity"),
        field("f_total", "Total", "formula", "{{f_price}} * {{f_qty}}"),
    ]

    validate_formula_schema(fields)
    result = materialize_records(fields, [{"id": "r1", "f_price": 9, "f_qty": 3}])

    assert result[0]["f_total"] == 27


def test_supports_formula_dependencies_and_safe_functions():
    fields = [
        field("f_price", "Price"),
        field("f_qty", "Quantity"),
        field("f_subtotal", "Subtotal", "formula", "{{f_price}} * {{f_qty}}"),
        field("f_rounded", "Rounded", "formula", "ROUND({{f_subtotal}} * 1.1, 2)"),
        field("f_label", "Label", "formula", "CONCAT('Total: ', {{f_rounded}})"),
    ]

    validate_formula_schema(fields)
    result = materialize_records(fields, [{"id": "r1", "f_price": 10, "f_qty": 3}])[0]

    assert result["f_subtotal"] == 30
    assert result["f_rounded"] == 33
    assert result["f_label"] == "Total: 33.0" or result["f_label"] == "Total: 33"


def test_array_values_can_be_passed_to_safe_functions():
    fields = [
        field("f_tags", "Tags", "multiSelect"),
        field("f_count", "Tag count", "formula", "LEN({{f_tags}})"),
    ]

    validate_formula_schema(fields)
    result = materialize_records(fields, [{"id": "r1", "f_tags": ["a", "b", "c"]}])

    assert result[0]["f_count"] == 3


def test_missing_reference_is_rejected_before_persistence():
    fields = [field("f_total", "Total", "formula", "{{f_missing}} + 1")]

    with pytest.raises(FormulaValidationError, match="references missing field"):
        validate_formula_schema(fields)


def test_dependency_cycle_is_rejected_before_persistence():
    fields = [
        field("f_a", "A", "formula", "{{f_b}} + 1"),
        field("f_b", "B", "formula", "{{f_a}} + 1"),
    ]

    with pytest.raises(FormulaValidationError, match="dependency cycle"):
        validate_formula_schema(fields)

    # Runtime reads remain resilient even if legacy/bad data bypassed validation.
    result = materialize_records(fields, [{"id": "r1"}])[0]
    assert result["f_a"] == FORMULA_CYCLE_ERROR
    assert result["f_b"] == FORMULA_CYCLE_ERROR


def test_dangerous_attribute_and_unknown_function_are_rejected():
    fields = [
        field("f_text", "Text", "text"),
        field("f_bad_attr", "BadAttr", "formula", "{{f_text}}.__class__"),
    ]
    with pytest.raises(FormulaValidationError, match="Unsupported formula expression"):
        validate_formula_schema(fields)

    fields[1] = field("f_bad_fn", "BadFn", "formula", "eval('1 + 1')")
    with pytest.raises(FormulaValidationError, match="Formula function is not allowed"):
        validate_formula_schema(fields)


def test_runtime_division_by_zero_returns_displayable_error():
    fields = [
        field("f_a", "A"),
        field("f_b", "B"),
        field("f_div", "Division", "formula", "{{f_a}} / {{f_b}}"),
    ]

    validate_formula_schema(fields)
    result = materialize_records(fields, [{"id": "r1", "f_a": 10, "f_b": 0}])

    assert result[0]["f_div"] == FORMULA_DIV_ZERO_ERROR


def test_deleting_referenced_field_can_be_detected():
    fields = [
        field("f_price", "Price"),
        field("f_total", "Total", "formula", "{{f_price}} * 2"),
    ]

    assert formula_dependents(fields, "f_price") == ["Total"]


def test_materialize_store_keeps_metadata_and_derives_records():
    store = {
        "fields": [
            field("f_a", "A"),
            field("f_double", "Double", "formula", "{{f_a}} * 2"),
        ],
        "records": [{"id": "r1", "f_a": 7}],
        "views": [{"id": "v1", "name": "Grid", "type": "grid"}],
    }

    result = materialize_store(store)

    assert result["records"][0]["f_double"] == 14
    assert result["views"] == store["views"]
    assert "f_double" not in store["records"][0]
