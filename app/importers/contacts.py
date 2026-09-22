"""Parser del export de contactos (res.partner)."""

from __future__ import annotations

import openpyxl

from app.importers import mappings
from app.importers.base import ParsedContact, RowError
from app.importers.excel_reader import detect_header, first_sheet, iter_data_rows
from app.importers.validators import clean_text


def parse_contacts(
    workbook: openpyxl.Workbook,
) -> tuple[list[ParsedContact], list[RowError], int]:
    """Parsea el export de contactos. Devuelve (contactos, errores, filas totales)."""
    sheet = first_sheet(workbook)
    header_row, headers = detect_header(sheet)
    columns = mappings.build_column_map(
        headers,
        required=(mappings.CONTACT_HEADER_NAME,),
        optional=(mappings.CONTACT_HEADER_EXTERNAL_REF,),
    )
    name_col = columns[mappings.CONTACT_HEADER_NAME]
    ref_col = columns.get(mappings.CONTACT_HEADER_EXTERNAL_REF)

    contacts: list[ParsedContact] = []
    errors: list[RowError] = []
    total = 0
    for row_number, values in iter_data_rows(sheet, header_row):
        total += 1
        name = clean_text(values[name_col])
        if not name:
            errors.append(
                RowError(
                    row_number,
                    mappings.CONTACT_HEADER_NAME,
                    "MISSING_NAME",
                    "Fila de contacto sin nombre.",
                )
            )
            continue
        external_ref = clean_text(values[ref_col]) if ref_col is not None else ""
        contacts.append(
            ParsedContact(
                external_ref=external_ref or None,
                name=name,
                raw_external_id=external_ref or None,
            )
        )
    return contacts, errors, total
