"""Dependency-free Office Open XML writer for backend dataset responses."""

from __future__ import annotations

import io
import json
import math
import re
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


DATASET_ROW_FIELDS = [
    "site_id",
    "asset_id",
    "device_id",
    "timestamp",
    "sequence",
    "vibration_rms_raw",
    "vibration_rms_mm_s",
    "vibration_peak_hz",
    "acoustic_rms_raw",
    "acoustic_db",
    "acoustic_peak_hz",
    "rpm",
    "scenario_label",
    "known_vibration_label",
    "known_acoustic_label",
    "telemetry_source",
    "is_synthetic",
    "vibration_unit_note",
    "acoustic_unit_note",
    "event_id",
    "event_label",
    "label_taxonomy_version",
    "ground_truth_label",
    "ground_truth_source",
    "target_label",
    "target_label_taxonomy_version",
    "label_status",
    "training_eligible",
    "event_reviewed",
    "dataset_split",
]


def _column_name(index: int) -> str:
    value = index
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _ooxml_text(value: str) -> str:
    # ST_Xstring encodes XML-forbidden UTF-16 units as _xHHHH_. Protect
    # literal escape-shaped strings first so a note containing "_x0001_"
    # is not decoded as a control character by spreadsheet readers.
    value = re.sub(r"_x[0-9a-fA-F]{4}_", lambda match: "_x005F_" + match[0][1:], value)
    encoded = []
    for char in value:
        code = ord(char)
        if (
            code < 0x20 and char not in "\t\n"
            or 0xD800 <= code <= 0xDFFF
            or code in {0xFFFE, 0xFFFF}
        ):
            encoded.append(f"_x{code:04X}_")
        else:
            encoded.append(char)
    return escape("".join(encoded), {'"': "&quot;"})


def _cell(reference: str, value: Any) -> str:
    if value is None:
        return f'<c r="{reference}"/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if math.isfinite(numeric):
            rendered = str(value).lower()
            return f'<c r="{reference}" t="n"><v>{rendered}</v></c>'
    if isinstance(value, (dict, list)):
        value = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    text = _ooxml_text(str(value))
    return (
        f'<c r="{reference}" t="inlineStr"><is>'
        f'<t xml:space="preserve">{text}</t></is></c>'
    )


def _worksheet(rows: list[list[Any]]) -> str:
    rendered_rows = []
    for row_number, row in enumerate(rows, 1):
        cells = "".join(
            _cell(f"{_column_name(column)}{row_number}", value)
            for column, value in enumerate(row, 1)
        )
        rendered_rows.append(f'<row r="{row_number}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rendered_rows)}</sheetData></worksheet>"
    )


def dataset_xlsx_bytes(export: dict[str, Any]) -> bytes:
    """Return a two-sheet workbook containing the exact manifest and rows."""

    manifest = export["manifest"]
    source_rows = export["rows"]
    headers = list(source_rows[0]) if source_rows else list(DATASET_ROW_FIELDS)
    manifest_rows = [["field", "value"], *[[key, value] for key, value in manifest.items()]]
    data_rows = [headers]
    data_rows.extend([[row.get(key) for key in headers] for row in source_rows])

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="manifest" sheetId="1" r:id="rId1"/><sheet name="rows" sheetId="2" r:id="rId2"/></sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="0"/>
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="1"><xf xfId="0"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>"""

    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet(manifest_rows))
        archive.writestr("xl/worksheets/sheet2.xml", _worksheet(data_rows))
    return output.getvalue()
