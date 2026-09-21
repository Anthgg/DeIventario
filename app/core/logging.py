"""Configuracion de logging seguro (nunca expone secretos)."""

from __future__ import annotations

import logging
from logging.config import dictConfig

from app.core.config import get_settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_REDACTED = "***"
_MIN_SECRET_LENGTH = 8


class SecretRedactionFilter(logging.Filter):
    """Reemplaza por ``***`` cualquier valor secreto conocido antes de emitir."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(name="secret-redaction")
        self._secrets = tuple(secret for secret in secrets if len(secret) >= _MIN_SECRET_LENGTH)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True

        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, _REDACTED)

        if redacted != message:
            record.msg = redacted
            record.args = ()

        return True


def configure_logging(level: str | None = None) -> None:
    """Configura el logging raiz con formato consistente y redaccion de secretos."""
    settings = get_settings()
    resolved_level = (level or settings.LOG_LEVEL or "INFO").upper()

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"format": LOG_FORMAT}},
            "filters": {
                "secret-redaction": {
                    "()": SecretRedactionFilter,
                    "secrets": settings.secret_values,
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["secret-redaction"],
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["console"], "level": resolved_level},
        }
    )
