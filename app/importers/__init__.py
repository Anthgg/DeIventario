"""Pipeline de importacion Odoo (F002): parsers y mappings sin persistencia."""

from app.importers.base import (
    ImportError,
    ImportSummary,
    InvalidFileError,
    MissingHeaderError,
    ParsedContact,
    ParsedData,
    ParsedMovement,
    ParsedProduct,
    RowError,
    UnknownImportTypeError,
)

__all__ = [
    "ImportError",
    "ImportSummary",
    "InvalidFileError",
    "MissingHeaderError",
    "ParsedContact",
    "ParsedData",
    "ParsedMovement",
    "ParsedProduct",
    "RowError",
    "UnknownImportTypeError",
]
