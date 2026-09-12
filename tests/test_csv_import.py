from __future__ import annotations

import pytest

from app.services.csv_import import (
    CsvImportError,
    build_csv_preview,
    convert_csv_value,
    parse_csv_text,
    prepare_csv_import,
)


FIELDS = [
    {"id": "name", "name": "名称", "type": "text"},
    {"id": "amount", "name": "金额", "type": "number"},
    {"id": "date", "name": "日期", "type": "date"},
    {
        "id": "status",
        "name": "状态",
        "type": "select",
        "options": [
            {"id": "todo", "label": "待处理"},
            {"id": "done", "label": "已完成"},
        ],
    },
    {
        "id": "tags",
        "name": "标签",
        "type": "multiSelect",
        "options": [
            {"id": "a", "label": "A"},
            {"id": "b", "label": "B"},
        ],
    },
    {"id": "auto", "name": "编号", "type": "autoNumber"},
    {"id": "attachment", "name": "附件", "type": "attachment"},
]


def test_parse_csv_supports_bom_quotes_commas_and_embedded_newlines():
    parsed = parse_csv_text(
        '\ufeff名称,说明\n"项目,A","第一行\n第二行"\n',
        has_header=True,
    )

    assert parsed["detectedDelimiter"] == ","
    assert parsed["columns"] == [
        {"index": 0, "name": "名称"},
        {"index": 1, "name": "说明"},
    ]
    assert parsed["rowCount"] == 1
    assert parsed["rows"][0] == ["项目,A", "第一行\n第二行"]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("名称\t金额\nA\t12\n", "\t"),
        ("名称;金额\nA;12\n", ";"),
        ("名称|金额\nA|12\n", "|"),
    ],
)
def test_parse_csv_auto_detects_common_delimiters(content, expected):
    parsed = parse_csv_text(content, delimiter="auto")
    assert parsed["detectedDelimiter"] == expected
    assert parsed["rowCount"] == 1


def test_parse_csv_accepts_explicit_literal_tab_delimiter():
    parsed = parse_csv_text(
        "名称\t金额\nA\t12\n",
        has_header=True,
        delimiter="\t",
    )
    assert parsed["detectedDelimiter"] == "\t"
    assert parsed["rows"] == [["A", "12"]]


def test_parse_csv_without_header_generates_column_names():
    parsed = parse_csv_text("A,12\nB,13\n", has_header=False)
    assert parsed["columns"] == [
        {"index": 0, "name": "列1"},
        {"index": 1, "name": "列2"},
    ]
    assert parsed["rowCount"] == 2


def test_parse_csv_pads_short_rows_and_returns_warning():
    parsed = parse_csv_text("a,b,c\n1,2\n", has_header=True)
    assert parsed["rows"] == [["1", "2", ""]]
    assert parsed["warnings"]
    assert "第 2 行" in parsed["warnings"][0]


def test_convert_number_and_date_values():
    assert convert_csv_value("1,234.50", FIELDS[1]) == 1234.5
    assert convert_csv_value("80%", {"id": "p", "name": "进度", "type": "progress"}) == 80
    assert convert_csv_value("2026/08/28", FIELDS[2]) == "2026-08-28"
    assert (
        convert_csv_value("2026-08-28 13:45:30", FIELDS[2])
        == "2026-08-28T13:45:30"
    )


def test_convert_select_and_multi_select_labels_to_ids():
    assert convert_csv_value("已完成", FIELDS[3]) == "done"
    assert convert_csv_value("todo", FIELDS[3]) == "todo"
    assert convert_csv_value("A, B、A", FIELDS[4]) == ["a", "b"]


def test_preview_validates_mapping_and_reports_row_level_errors():
    text = "名称,金额,状态\nA,12,已完成\nB,not-a-number,未知\n"
    mapping = [
        {"sourceIndex": 0, "fieldId": "name"},
        {"sourceIndex": 1, "fieldId": "amount"},
        {"sourceIndex": 2, "fieldId": "status"},
    ]

    preview = build_csv_preview(text, FIELDS, mapping=mapping)

    assert preview["rowCount"] == 2
    assert preview["validRowCount"] == 1
    assert preview["invalidRowCount"] == 1
    assert preview["errorCount"] == 2
    assert {error["fieldId"] for error in preview["errors"]} == {
        "amount",
        "status",
    }
    assert all(error["rowNumber"] == 3 for error in preview["errors"])


def test_prepare_csv_import_returns_only_fully_valid_rows():
    text = "名称,金额\n  Alpha  ,1,\nBeta,broken\nGamma,3\n"
    # Extra trailing column in first row is tolerated and ignored because it is
    # not mapped.
    mapping = [
        {"sourceIndex": 0, "fieldId": "name"},
        {"sourceIndex": 1, "fieldId": "amount"},
    ]

    prepared = prepare_csv_import(text, FIELDS, mapping)

    assert prepared["rowCount"] == 3
    assert prepared["validRowCount"] == 2
    assert prepared["invalidRowCount"] == 1
    assert prepared["records"] == [
        {"name": "Alpha", "amount": 1},
        {"name": "Gamma", "amount": 3},
    ]


@pytest.mark.parametrize("field_id", ["auto", "attachment"])
def test_mapping_rejects_readonly_or_unsupported_fields(field_id):
    with pytest.raises(CsvImportError, match="does not support CSV import"):
        prepare_csv_import(
            "列\nvalue\n",
            FIELDS,
            [{"sourceIndex": 0, "fieldId": field_id}],
        )


def test_mapping_rejects_duplicate_target_field():
    with pytest.raises(CsvImportError, match="table field can only be mapped once"):
        prepare_csv_import(
            "a,b\n1,2\n",
            FIELDS,
            [
                {"sourceIndex": 0, "fieldId": "name"},
                {"sourceIndex": 1, "fieldId": "name"},
            ],
        )


def test_empty_csv_and_invalid_syntax_are_rejected():
    with pytest.raises(CsvImportError, match="empty"):
        parse_csv_text(" \n ")

    with pytest.raises(CsvImportError, match="Invalid CSV syntax"):
        parse_csv_text('a,b\n"unterminated,b\n')
