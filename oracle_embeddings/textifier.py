import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 8000  # approximate safety limit for embedding models


def rows_to_texts(columns: list[str], rows: list[tuple], processing_config: dict) -> list[str]:
    """Convert rows of data into text strings suitable for embedding."""
    template = processing_config.get("text_template", "{column_name}: {value}")
    separator = processing_config.get("row_separator", " | ")

    texts = []
    for row in rows:
        parts = []
        for col_name, value in zip(columns, row):
            formatted = format_value(col_name, value, template)
            if formatted:
                parts.append(formatted)
        text = separator.join(parts)
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH]
        texts.append(text)

    logger.info("Converted %d rows to text", len(texts))
    return texts


def format_value(column_name: str, value, template: str) -> str | None:
    """Format a single column value using the template."""
    if value is None:
        return None

    if isinstance(value, datetime):
        str_value = value.isoformat()
    elif isinstance(value, date):
        str_value = value.isoformat()
    elif isinstance(value, bytes):
        return None
    else:
        str_value = str(value)

    return template.format(column_name=column_name, value=str_value)
