"""Reading and rewriting the settings in a wp-config.php file.

Only the value expressions of ``define('NAME', ...)`` calls and the
``$table_prefix = ...;`` assignment are replaced; every other line stays as
the site owner wrote it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """Character range of a value expression inside the file."""

    start: int
    end: int


def _skip_string(text: str, index: int) -> int:
    """Return the index just after the PHP string literal starting at ``index``."""
    quote = text[index]
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return index


def _skip_comment(text: str, index: int) -> int | None:
    if text.startswith("//", index) or text.startswith("#", index):
        end = text.find("\n", index)
        return len(text) if end == -1 else end
    if text.startswith("/*", index):
        end = text.find("*/", index + 2)
        return len(text) if end == -1 else end + 2
    return None


def _expression_end(text: str, index: int, terminators: str) -> int:
    """Scan a PHP expression until a terminator at nesting depth zero."""
    depth = 0
    while index < len(text):
        char = text[index]
        if char in "'\"":
            index = _skip_string(text, index)
            continue
        skipped = _skip_comment(text, index)
        if skipped is not None:
            index = skipped
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0 and char in terminators:
                return index
            depth -= 1
        elif depth == 0 and char in terminators:
            return index
        index += 1
    return index


def _code_regions(text: str) -> list[tuple[int, int]]:
    """Regions of ``text`` that are PHP code (not comments or strings)."""
    regions: list[tuple[int, int]] = []
    index = 0
    start = 0
    while index < len(text):
        char = text[index]
        if char in "'\"":
            regions.append((start, index))
            index = _skip_string(text, index)
            start = index
            continue
        skipped = _skip_comment(text, index)
        if skipped is not None:
            regions.append((start, index))
            index = skipped
            start = index
            continue
        index += 1
    regions.append((start, len(text)))
    return regions


def _in_code(position: int, regions: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in regions)


_DEFINE = re.compile(r"\bdefine\s*\(\s*(['\"])([A-Z0-9_]+)\1\s*,\s*", re.I)
_PREFIX = re.compile(r"\$table_prefix\s*=\s*")


def find_define(text: str, name: str) -> Span | None:
    """Location of the value of ``define('NAME', value)`` (outside comments)."""
    regions = _code_regions(text)
    for match in _DEFINE.finditer(text):
        if match.group(2) != name or not _in_code(match.start(), regions):
            continue
        start = match.end()
        end = _expression_end(text, start, ",)")
        return Span(start, end)
    return None


def find_table_prefix(text: str) -> Span | None:
    regions = _code_regions(text)
    for match in _PREFIX.finditer(text):
        if not _in_code(match.start(), regions):
            continue
        start = match.end()
        return Span(start, _expression_end(text, start, ";"))
    return None


def php_string(value: str) -> str:
    """A single-quoted PHP string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def literal_value(expression: str) -> str | None:
    """The value of a plain string literal expression, or ``None``."""
    expression = expression.strip()
    if len(expression) >= 2 and expression[0] == expression[-1] and expression[0] in "'\"":
        inner = expression[1:-1]
        if expression[0] == "'":
            return inner.replace("\\'", "'").replace("\\\\", "\\")
        if "$" in inner:
            return None
        return bytes(inner, "utf-8").decode("unicode_escape")
    return None


def read_define(text: str, name: str) -> str | None:
    span = find_define(text, name)
    if span is None:
        return None
    return literal_value(text[span.start:span.end])


def read_table_prefix(text: str) -> str | None:
    span = find_table_prefix(text)
    if span is None:
        return None
    return literal_value(text[span.start:span.end])


def set_define(text: str, name: str, value: str | bool, *, add_if_missing: bool = True) -> str:
    """Return ``text`` with ``NAME`` defined as ``value``."""
    literal = ("true" if value else "false") if isinstance(value, bool) else php_string(value)
    span = find_define(text, name)
    if span is not None:
        return text[: span.start] + literal + text[span.end :]
    if not add_if_missing:
        return text
    line = f"define( '{name}', {literal} );\n"
    marker = re.search(r"/\*\s*That's all, stop editing!.*?\*/", text)
    if marker:
        return text[: marker.start()] + line + "\n" + text[marker.start() :]
    settings = text.find("require_once ABSPATH . 'wp-settings.php'")
    if settings == -1:
        settings = text.find('require_once ABSPATH . "wp-settings.php"')
    if settings != -1:
        line_start = text.rfind("\n", 0, settings) + 1
        return text[:line_start] + line + text[line_start:]
    opening = text.find("<?php")
    insert_at = opening + len("<?php") if opening != -1 else 0
    return text[:insert_at] + "\n" + line + text[insert_at:]


def set_table_prefix(text: str, prefix: str) -> str:
    span = find_table_prefix(text)
    literal = php_string(prefix)
    if span is not None:
        return text[: span.start] + literal + text[span.end :]
    return text.replace("<?php", f"<?php\n$table_prefix = {literal};", 1)


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    name: str
    user: str
    password: str
    table_prefix: str


def read_database_settings(text: str) -> DatabaseSettings | None:
    """Database settings if they are plain literals, else ``None``."""
    values = [read_define(text, key) for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")]
    prefix = read_table_prefix(text)
    if any(value is None for value in values) or prefix is None:
        return None
    host, name, user, password = values  # type: ignore[misc]
    return DatabaseSettings(host, name, user, password, prefix)


def apply_destination(text: str, *, db: DatabaseSettings, site_url: str | None) -> str:
    """Point a wp-config.php at the destination database (and address)."""
    text = set_define(text, "DB_NAME", db.name)
    text = set_define(text, "DB_USER", db.user)
    text = set_define(text, "DB_PASSWORD", db.password)
    text = set_define(text, "DB_HOST", db.host)
    text = set_table_prefix(text, db.table_prefix)
    if site_url:
        for constant in ("WP_HOME", "WP_SITEURL"):
            if find_define(text, constant) is not None:
                text = set_define(text, constant, site_url)
    return text
