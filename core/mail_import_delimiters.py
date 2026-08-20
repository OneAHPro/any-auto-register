from __future__ import annotations

import re


MAIL_IMPORT_DASH_DELIMITER_PATTERN = r"(?<!-)-{3,4}(?!-)"
MAIL_IMPORT_DASH_DELIMITER_RE = re.compile(MAIL_IMPORT_DASH_DELIMITER_PATTERN)


def normalize_mail_import_field(value: str) -> str:
    """Turn a copied Markdown link field back into its underlying HTTP URL."""
    text = str(value or "").strip()
    match = re.fullmatch(
        r"\[[^\]]*\]\(\s*(?P<url>https?://.+)\s*\)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return text
    url = match.group("url").strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip()
    return url


def has_mail_import_dash_delimiter(value: str) -> bool:
    return bool(MAIL_IMPORT_DASH_DELIMITER_RE.search(str(value or "")))


def split_mail_import_dash_fields(value: str) -> list[str]:
    return [
        normalize_mail_import_field(part)
        for part in MAIL_IMPORT_DASH_DELIMITER_RE.split(str(value or ""))
    ]


def split_mail_import_fields(value: str) -> list[str]:
    text = str(value or "")
    if has_mail_import_dash_delimiter(text):
        return split_mail_import_dash_fields(text)
    if "\t" in text:
        return [normalize_mail_import_field(part) for part in text.split("\t")]
    return [normalize_mail_import_field(part) for part in text.split()]


def split_mail_import_first_field(value: str) -> str:
    text = str(value or "")
    if has_mail_import_dash_delimiter(text):
        return MAIL_IMPORT_DASH_DELIMITER_RE.split(text, maxsplit=1)[0].strip()
    if "\t" in text:
        return text.split("\t", maxsplit=1)[0].strip()
    return text.split(maxsplit=1)[0].strip() if text.strip() else ""


def mail_import_row_pattern(email_pattern: str) -> str:
    return rf"{email_pattern}(?:{MAIL_IMPORT_DASH_DELIMITER_PATTERN}|\t|\s)"
