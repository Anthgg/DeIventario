"""Validadores y normalizadores de celdas de los exports."""

from __future__ import annotations

import datetime as dt
import decimal
import zoneinfo

from app.core.config import get_settings


def _project_tz() -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(get_settings().DEFAULT_TIMEZONE)


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_decimal(value: object) -> tuple[decimal.Decimal | None, str | None]:
    """Devuelve (decimal, error_code). Vacio -> (None, None); invalido -> (None, codigo)."""
    text = clean_text(value)
    if text == "":
        return None, None
    try:
        return decimal.Decimal(text), None
    except decimal.InvalidOperation:
        return None, "INVALID_DECIMAL"


def parse_datetime_aware(value: object) -> tuple[dt.datetime | None, str | None]:
    """Parsea fechas y las ancla a la zona horaria del proyecto (America/Lima)."""
    if value is None:
        return None, None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_project_tz()), None
        return value, None
    text = clean_text(value)
    if text == "":
        return None, None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=_project_tz()), None
        except ValueError:
            continue
    return None, "INVALID_DATETIME"


def sanitize_raw_value(value: object, max_length: int = 500) -> str | None:
    text = clean_text(value)
    if text == "":
        return None
    return text[:max_length]
