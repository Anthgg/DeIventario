"""Formato de numeros, monedas, fechas y anti formula-injection (F010).

Conventions por defecto es-PE: miles '.', decimales ','. El estilo de
moneda y el formato de fecha salen de ``organization_settings`` (nunca
hardcodeados en el renderer).
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

_SYMBOLS: dict[str, str] = {"PEN": "S/", "USD": "$", "EUR": "€"}

FORMULA_PREFIXES = ("=", "+", "-", "@")


def to_decimal(value: Any) -> decimal.Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, decimal.Decimal):
        return value
    try:
        return decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        return None


def format_number(
    value: Any,
    *,
    decimals: int = 0,
    decimal_separator: str = ".",
    thousands_separator: str = ",",
) -> str:
    number = to_decimal(value)
    if number is None:
        return ""
    quantized = number.quantize(decimal.Decimal(1).scaleb(-decimals))
    raw = f"{quantized:f}"
    if "." in raw:
        integer_part, decimal_part = raw.split(".", 1)
    else:
        integer_part, decimal_part = raw, ""
    negative = integer_part.startswith("-")
    if negative:
        integer_part = integer_part[1:]
    if thousands_separator:
        grouped: list[str] = []
        while len(integer_part) > 3:
            grouped.insert(0, integer_part[-3:])
            integer_part = integer_part[:-3]
        grouped.insert(0, integer_part)
        integer_part = thousands_separator.join(grouped)
    sign = "-" if negative else ""
    if decimal_part:
        return f"{sign}{integer_part}{decimal_separator}{decimal_part}"
    return f"{sign}{integer_part}"


def format_quantity(
    value: Any, *, decimal_separator: str = ",", thousands_separator: str = "."
) -> str:
    number = to_decimal(value)
    if number is None:
        return ""
    decimals = 0 if number == number.to_integral_value() else 4
    return format_number(
        number,
        decimals=decimals,
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    )


def format_money(
    value: Any,
    currency: str | None,
    style: str,
    *,
    decimal_separator: str = ",",
    thousands_separator: str = ".",
    decimals: int = 2,
) -> str:
    number = to_decimal(value)
    if number is None:
        return ""
    amount = format_number(
        number,
        decimals=decimals,
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    )
    code = (currency or "").strip()
    symbol = _SYMBOLS.get(code.upper(), code)
    if not symbol:
        return amount
    if style == "SYMBOL_AFTER":
        return f"{amount} {symbol}"
    if style == "CODE_SUFFIX":
        return f"{amount} {code.upper()}"
    return f"{symbol} {amount}"


def format_date(value: Any, date_format: str = "YYYY-MM-DD") -> str:
    if value is None or value == "":
        return ""
    parsed: dt.date | None = None
    if isinstance(value, dt.datetime):
        parsed = value.date()
    elif isinstance(value, dt.date):
        parsed = value
    else:
        text = str(value)
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                parsed = dt.date.fromisoformat(text[:10])
            except ValueError:
                return text
    token_map = {
        "YYYY": f"{parsed.year:04d}",
        "MM": f"{parsed.month:02d}",
        "DD": f"{parsed.day:02d}",
    }
    result = date_format
    for token, replacement in token_map.items():
        result = result.replace(token, replacement)
    return result


def excel_safe(value: Any) -> Any:
    """Neutraliza formula-injection en celdas de texto (F010D/§78)."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value
