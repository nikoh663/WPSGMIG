"""The one HTTP client used toward your original site.

It behaves like a careful browser session: browser headers, a persistent
cookie jar, Referer and Origin on form posts, one request at a time with a
short pause between admin requests, and polite backoff on 429 and 5xx.
TLS certificates are always verified; plain HTTP needs an explicit opt-in.
"""

from __future__ import annotations

import email.utils
import gzip
import http.cookiejar
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from wpmigrate import constants
from wpmigrate.engine.errors import MigrationError

Logger = Callable[[str], None]


class InsecureUrlRefused(MigrationError):
    """Raised for an http:// address when plain HTTP was not allowed."""


@dataclass
class Response:
    """A fully read HTTP response."""

    status: int
    url: str
    headers: dict[str, str]
    body: bytes
    history: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


class _KeepErrors(urllib.request.HTTPErrorProcessor):
    def http_response(self, request: Any, response: Any) -> Any:
        return response

    https_response = http_response


def parse_retry_after(value: str, now: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (seconds or HTTP date)."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    seconds = parsed.timestamp() - (now if now is not None else time.time())
    return max(0.0, seconds)


def decode_body(body: bytes, content_encoding: str) -> bytes:
    """Undo gzip or deflate content encoding."""
    encoding = (content_encoding or "").strip().lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


class HttpClient:
    """Browser-like session toward one site."""

    def __init__(
        self,
        *,
        cookie_file: Path | None,
        user_agent: str = constants.USER_AGENT,
        allow_http: bool = False,
        admin_delay: float = constants.ADMIN_REQUEST_DELAY_SECONDS,
        max_attempts: int = constants.MAX_HTTP_ATTEMPTS,
        log: Logger | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ) -> None:
        self.user_agent = user_agent
        self.allow_http = allow_http
        self.admin_delay = admin_delay
        self.max_attempts = max_attempts
        self.timeout = timeout
        self._log = log or (lambda message: None)
        self._sleep = sleep
        self._last_request_at = 0.0
        self.cookie_file = cookie_file
        self.jar = http.cookiejar.MozillaCookieJar(str(cookie_file) if cookie_file else None)
        if cookie_file and cookie_file.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=False)
            except (OSError, http.cookiejar.LoadError):
                self.jar.clear()
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
            _KeepErrors(),
        )

    # -- cookies ------------------------------------------------------------

    def save_cookies(self) -> None:
        """Write the cookie jar to disk with mode 0600."""
        if not self.cookie_file:
            return
        self.jar.save(ignore_discard=True, ignore_expires=False)
        os.chmod(self.cookie_file, 0o600)

    def clear_cookies(self) -> None:
        self.jar.clear()
        if self.cookie_file and self.cookie_file.exists():
            self.cookie_file.unlink()

    def has_cookie(self, prefix: str) -> bool:
        return any(cookie.name.startswith(prefix) for cookie in self.jar)

    def set_cookie(self, domain: str, name: str, value: str, path: str = "/") -> None:
        cookie = http.cookiejar.Cookie(
            0, name, value, None, False, domain, False, domain.startswith("."), path, True,
            False, None, True, None, None, {}, False,
        )
        self.jar.set_cookie(cookie)

    # -- requests -----------------------------------------------------------

    def _check_url(self, url: str) -> None:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme == "https":
            return
        if scheme == "http" and self.allow_http:
            return
        if scheme == "http":
            raise InsecureUrlRefused(
                f"The address {url} does not use HTTPS, so passwords would travel unprotected.",
                "Use the site's https:// address. If the site really has no HTTPS, run the tool again with --allow-http.",
            )
        raise MigrationError(f"{url} is not a web address.", "Enter an address that starts with https://.")

    def _pace(self) -> None:
        wait = self._last_request_at + self.admin_delay - time.monotonic()
        if wait > 0:
            self._sleep(wait)

    def _send_once(self, method: str, url: str, body: bytes | None, headers: Mapping[str, str]) -> Response:
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        self._pace()
        self._last_request_at = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout) as raw:
                raw_headers = {key.lower(): value for key, value in raw.headers.items()}
                payload = raw.read()
                status = raw.status
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise MigrationError(
                    f"The secure certificate of {urllib.parse.urlsplit(url).hostname} could not be verified ({reason.verify_message}).",
                    "Check that the address is right and that the site's HTTPS certificate is valid, then try again.",
                    code="TLS_VERIFY",
                ) from error
            raise ConnectionError(str(reason)) from error
        payload = decode_body(payload, raw_headers.get("content-encoding", ""))
        return Response(status=status, url=url, headers=raw_headers, body=payload)

    def request(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | list[tuple[str, str]] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        referer: str | None = None,
        follow_redirects: bool = True,
        retry: bool = True,
    ) -> Response:
        """Send one request (following redirects) with browser headers."""
        self._check_url(url)
        all_headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": constants.BROWSER_ACCEPT,
            "Accept-Language": constants.BROWSER_ACCEPT_LANGUAGE,
            "Accept-Encoding": constants.BROWSER_ACCEPT_ENCODING,
        }
        body: bytes | None = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            all_headers["Content-Type"] = "application/json"
            all_headers["Accept"] = "application/json, */*;q=0.1"
        if referer:
            all_headers["Referer"] = referer
        if method.upper() != "GET":
            parts = urllib.parse.urlsplit(referer or url)
            all_headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
        if headers:
            all_headers.update(headers)

        history: list[str] = []
        current_url, current_method, current_body = url, method.upper(), body
        for _ in range(10):
            response = self._send_with_backoff(current_method, current_url, current_body, all_headers, retry=retry)
            self.save_cookies()
            self._log(f"HTTP {current_method} {current_url} -> {response.status}")
            location = response.header("location")
            if follow_redirects and response.status in (301, 302, 303, 307, 308) and location:
                history.append(current_url)
                next_url = urllib.parse.urljoin(current_url, location)
                self._check_url(next_url)
                if response.status in (301, 302, 303):
                    current_method, current_body = "GET", None
                    all_headers.pop("Content-Type", None)
                    all_headers.pop("Origin", None)
                all_headers["Referer"] = current_url
                current_url = next_url
                continue
            response.history = history
            response.url = current_url
            return response
        raise MigrationError(f"The site at {url} redirected too many times.", "Open the address in a browser to check it, then try again.")

    def _send_with_backoff(self, method: str, url: str, body: bytes | None, headers: Mapping[str, str], *, retry: bool) -> Response:
        attempts = self.max_attempts if retry else 1
        delay = 5.0
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._send_once(method, url, body, headers)
            except ConnectionError as error:
                last_error = error
                self._log(f"HTTP {method} {url} failed: {error}")
                if attempt == attempts:
                    break
                self._sleep(delay)
                delay *= 2
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == attempts:
                    return response
                wait = parse_retry_after(response.header("retry-after"))
                if wait is None:
                    wait = delay
                    delay *= 2
                wait = min(wait, constants.MAX_RETRY_AFTER_SECONDS)
                self._log(f"HTTP {method} {url} -> {response.status}; waiting {wait:.0f}s before trying again")
                self._sleep(wait)
                continue
            return response
        raise MigrationError(
            f"Could not reach {urllib.parse.urlsplit(url).netloc} ({last_error}).",
            "Check the address and your internet connection, then run the tool again.",
            code="NETWORK",
        )

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)
