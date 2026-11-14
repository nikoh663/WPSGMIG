"""Remove secrets from anything that is logged or shown."""

from __future__ import annotations

import re
import threading

_PATTERNS = (
    # Cookie headers and WordPress auth cookies.
    (re.compile(r"(?i)((?:set-)?cookie:\s*)[^\r\n]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(wordpress_(?:logged_in|sec)?_?[0-9a-f]*=)[^;\s]+"), r"\1[redacted]"),
    # Nonces in query strings, form bodies and JSON.
    (re.compile(r"(?i)(_wpnonce=|_ajax_nonce=|x-wp-nonce:\s*)[0-9a-f]{6,}"), r"\1[redacted]"),
    (re.compile(r'(?i)("nonce"\s*:\s*")[^"]+'), r"\1[redacted]"),
    # Passwords and tokens passed as name=value.
    (re.compile(r"(?i)((?:pwd|password|pass|secret|token|target-pass|mysql-password)=)[^&\s]+"), r"\1[redacted]"),
    # Long base64 blobs such as Migration Tokens.
    (re.compile(r"[A-Za-z0-9+/]{120,}={0,2}"), "[redacted-token]"),
)


class Redactor:
    """Scrubs registered secret values and secret-looking patterns from text."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, secret: str | None) -> None:
        """Register a value that must never appear in output."""
        if secret and len(secret) >= 3:
            with self._lock:
                self._secrets.add(secret)

    def __call__(self, text: str) -> str:
        """Return ``text`` with every secret replaced."""
        if not text:
            return text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, "[redacted]")
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text
