"""CSV import parsing, mapping and validation helpers.

The parser intentionally uses Python's standard csv module instead of splitting
on commas so quoted delimiters, escaped quotes and embedded newlines are handled
correctly.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple


MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 10_000
MAX_CSV_COLUMNS = 200
PREVIEW_ROWS = 10
MAX_RETURNED_ERRORS = 100

SUPPORTED_IMPORT_FIELD_TYPES = {
    "text",
    "number",
    "select",
    "multiSelect",
    "date",
    "progress",
    "url",
    "rating",
    "email",
    "phone",
}


class CsvImportError(ValueError):
    pass


def _resolve_delimiter(csv_text: str, delimiter: Optional[str]) -> str:
    raw_requested = delimiter if delimiter is not None else "auto"
    # A literal tab is a valid delimiter. Do not strip it into an empty string.
    requested = (
        "\t"
        if raw_requested == "\t"
        else str(raw_requested).strip().lower()
    )
    aliases = {
        "comma": ",",
        "tab": "\t",
        "semicolon": ";",
        "pipe": "|",
        ",": ",",
        "\t": "\t",
        ";": ";",
        "|": "|",
    }
    if requested != "auto":
        resolved = aliases.get(requested)
        if not resolved:
            raise CsvImportError("Unsupported CSV delimiter")
        return resolved

    sample = csv_text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def _normalize_header(value: str, index: int) -> str:
    cleaned = value.strip()
    return cleaned or f"列{index + 1}"


def parse_csv_text(
    csv_text: str,
    *,
    has_header: bool = True,
    delimiter: Optional[str] = "auto",
) -> Dict[str, Any]:
    if not isinstance(csv_text, str):
        raise CsvImportError("CSV content must be text")

    text = csv_text.lstrip("\ufeff")
    if not text.strip():
        raise CsvImportError("CSV file is empty")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise CsvImportError("CSV file exceeds the 5 MB limit")

    resolved_delimiter = _resolve_delimiter(text, delimiter)
    try:
        reader = csv.reader(
            io.StringIO(text, newline=""),
            delimiter=resolved_delimiter,
            quotechar='"',
            doublequote=True,
            strict=True,
        )
        raw_rows = [
            list(row)
            for row in reader
            if any(str(cell).strip() for cell in row)
        ]
    except csv.Error as exc:
        raise CsvImportError(f"Invalid CSV syntax: {exc}") from exc

    if not raw_rows:
        raise CsvImportError("CSV file does not contain any rows")

    header_row: List[str] = raw_rows[0] if has_header else []
    data_rows = raw_rows[1:] if has_header else raw_rows

    if len(data_rows) > MAX_CSV_ROWS:
        raise CsvImportError(
            f"CSV contains more than {MAX_CSV_ROWS} data rows"
        )

    max_width = max(
        [len(header_row), *(len(row) for row in data_rows)],
        default=0,
    )
    if max_width == 0:
        raise CsvImportError("CSV file does not contain any columns")
    if max_width > MAX_CSV_COLUMNS:
        raise CsvImportError(
            f"CSV contains more than {MAX_CSV_COLUMNS} columns"
        )

    if has_header:
        headers = [
            _normalize_header(
                header_row[index] if index < len(header_row) else "",
                index,
            )
            for index in range(max_width)
        ]
    else:
        headers = [f"列{index + 1}" for index in range(max_width)]

    warnings: List[str] = []
    normalized_rows: List[List[str]] = []
    expected_width = len(header_row) if has_header else max_width
    for row_index, row in enumerate(data_rows):
        if len(row) != expected_width and len(warnings) < 20:
            human_row = row_index + (2 if has_header else 1)
            warnings.append(
                f"第 {human_row} 行包含 {len(row)} 列，"
                f"预期 {expected_width} 列；缺失值将留空，多出的列仍可映射。"
            )
        normalized_rows.append(
            [row[index] if index < len(row) else "" for index in range(max_width)]
        )

    return {
        "columns": [
            {"index": index, "name": header}
            for index, header in enumerate(headers)
        ],
        "rows": normalized_rows,
        "rowCount": len(normalized_rows),
        "sampleRows": normalized_rows[:PREVIEW_ROWS],
        "detectedDelimiter": resolved_delimiter,
        "warnings": warnings,
        "hasHeader": bool(has_header),
    }


def _field_map(fields: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(field.get("id")): dict(field)
        for field in fields
        if field.get("id") is not None
    }


def normalize_csv_mapping(
    mapping: Optional[Sequence[Dict[str, Any]]],
    fields: Sequence[Dict[str, Any]],
    column_count: int,
) -> List[Tuple[int, Dict[str, Any]]]:
    if not mapping:
        return []

    fields_by_id = _field_map(fields)
    normalized: List[Tuple[int, Dict[str, Any]]] = []
    used_sources: set[int] = set()
    used_targets: set[str] = set()

    for item in mapping:
        if not isinstance(item, dict):
            raise CsvImportError("CSV field mapping is invalid")
        try:
            source_index = int(item.get("sourceIndex"))
        except (TypeError, ValueError) as exc:
            raise CsvImportError("CSV source column index is invalid") from exc

        field_id = str(item.get("fieldId") or "").strip()
        if source_index < 0 or source_index >= column_count:
            raise CsvImportError("CSV source column index is out of range")
        if source_index in used_sources:
            raise CsvImportError("A CSV column can only be mapped once")
        if not field_id or field_id not in fields_by_id:
            raise CsvImportError("Mapped target field does not exist")
        if field_id in used_targets:
            raise CsvImportError("A table field can only be mapped once")

        field = fields_by_id[field_id]
        field_type = str(field.get("type") or "")
        if field_type not in SUPPORTED_IMPORT_FIELD_TYPES:
            raise CsvImportError(
                f"Field '{field.get('name') or field_id}' "
                f"({field_type or 'unknown'}) does not support CSV import"
            )

        used_sources.add(source_index)
        used_targets.add(field_id)
        normalized.append((source_index, field))

    if not normalized:
        raise CsvImportError("Map at least one CSV column to a table field")
    return normalized


def _parse_number(value: str) -> int | float:
    cleaned = value.strip().replace(",", "").replace("_", "").replace(" ", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    if not cleaned:
        raise ValueError("empty number")
    number = float(cleaned)
    if not (number == number and abs(number) != float("inf")):
        raise ValueError("non-finite number")
    return int(number) if number.is_integer() else number


def _parse_date(value: str) -> str | int:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("empty date")

    if re.fullmatch(r"\d{13}", cleaned):
        return int(cleaned)
    if re.fullmatch(r"\d{10}", cleaned):
        return int(cleaned) * 1000

    formats = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y%m%d",
        "%Y年%m月%d日",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
    )
    for date_format in formats:
        try:
            parsed = datetime.strptime(cleaned, date_format)
        except ValueError:
            continue
        if "%H" in date_format:
            return parsed.isoformat(timespec="seconds")
        return parsed.strftime("%Y-%m-%d")
    raise ValueError("unsupported date format")


def _match_option(
    token: str,
    field: Dict[str, Any],
) -> str:
    options = list(field.get("options") or [])
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("empty option")

    for option in options:
        if str(option.get("id") or "") == cleaned:
            return str(option.get("id"))

    folded = cleaned.casefold()
    matches = [
        option
        for option in options
        if str(option.get("label") or "").strip().casefold() == folded
    ]
    if len(matches) == 1:
        return str(matches[0].get("id"))
    if len(matches) > 1:
        raise ValueError(f"option label '{cleaned}' is ambiguous")
    raise ValueError(f"option '{cleaned}' does not exist")


def convert_csv_value(raw_value: Any, field: Dict[str, Any]) -> Any:
    value = "" if raw_value is None else str(raw_value)
    cleaned = value.strip()
    if cleaned == "":
        return None

    field_type = str(field.get("type") or "")
    if field_type in {"text", "url", "email", "phone"}:
        return cleaned
    if field_type in {"number", "progress", "rating"}:
        try:
            return _parse_number(cleaned)
        except ValueError as exc:
            raise ValueError("不是有效数字") from exc
    if field_type == "date":
        try:
            return _parse_date(cleaned)
        except ValueError as exc:
            raise ValueError(
                "日期格式无法识别，请使用 YYYY-MM-DD 等常见格式"
            ) from exc
    if field_type == "select":
        return _match_option(cleaned, field)
    if field_type == "multiSelect":
        tokens = [
            token.strip()
            for token in re.split(r"[,;|、]", cleaned)
            if token.strip()
        ]
        resolved: List[str] = []
        for token in tokens:
            option_id = _match_option(token, field)
            if option_id not in resolved:
                resolved.append(option_id)
        return resolved

    raise ValueError(f"字段类型 {field_type or 'unknown'} 不支持 CSV 导入")


def validate_csv_rows(
    parsed: Dict[str, Any],
    mapping: Sequence[Dict[str, Any]],
    fields: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    columns = list(parsed.get("columns") or [])
    normalized_mapping = normalize_csv_mapping(
        mapping,
        fields,
        len(columns),
    )

    valid_records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    invalid_row_indexes: set[int] = set()
    error_count = 0

    rows = list(parsed.get("rows") or [])
    has_header = bool(parsed.get("hasHeader"))
    for row_index, row in enumerate(rows):
        record: Dict[str, Any] = {}
        row_has_error = False
        for source_index, field in normalized_mapping:
            raw_value = row[source_index] if source_index < len(row) else ""
            try:
                record[str(field["id"])] = convert_csv_value(raw_value, field)
            except ValueError as exc:
                error_count += 1
                row_has_error = True
                invalid_row_indexes.add(row_index)
                if len(errors) < MAX_RETURNED_ERRORS:
                    errors.append(
                        {
                            "rowNumber": row_index + (2 if has_header else 1),
                            "columnIndex": source_index,
                            "columnName": columns[source_index]["name"],
                            "fieldId": str(field["id"]),
                            "fieldName": str(field.get("name") or field["id"]),
                            "value": raw_value,
                            "message": str(exc),
                        }
                    )
        if not row_has_error:
            valid_records.append(record)

    return {
        "records": valid_records,
        "errors": errors,
        "errorCount": error_count,
        "invalidRowCount": len(invalid_row_indexes),
        "validRowCount": len(valid_records),
    }


def build_csv_preview(
    csv_text: str,
    fields: Sequence[Dict[str, Any]],
    *,
    mapping: Optional[Sequence[Dict[str, Any]]] = None,
    has_header: bool = True,
    delimiter: Optional[str] = "auto",
) -> Dict[str, Any]:
    parsed = parse_csv_text(
        csv_text,
        has_header=has_header,
        delimiter=delimiter,
    )
    result = {
        "columns": parsed["columns"],
        "rowCount": parsed["rowCount"],
        "sampleRows": parsed["sampleRows"],
        "detectedDelimiter": parsed["detectedDelimiter"],
        "warnings": parsed["warnings"],
        "hasHeader": parsed["hasHeader"],
        "validRowCount": parsed["rowCount"],
        "invalidRowCount": 0,
        "errorCount": 0,
        "errors": [],
    }
    if mapping:
        validation = validate_csv_rows(parsed, mapping, fields)
        result.update(
            {
                "validRowCount": validation["validRowCount"],
                "invalidRowCount": validation["invalidRowCount"],
                "errorCount": validation["errorCount"],
                "errors": validation["errors"],
            }
        )
    return result


def prepare_csv_import(
    csv_text: str,
    fields: Sequence[Dict[str, Any]],
    mapping: Sequence[Dict[str, Any]],
    *,
    has_header: bool = True,
    delimiter: Optional[str] = "auto",
) -> Dict[str, Any]:
    parsed = parse_csv_text(
        csv_text,
        has_header=has_header,
        delimiter=delimiter,
    )
    validation = validate_csv_rows(parsed, mapping, fields)
    return {
        **validation,
        "rowCount": parsed["rowCount"],
        "detectedDelimiter": parsed["detectedDelimiter"],
        "warnings": parsed["warnings"],
    }
