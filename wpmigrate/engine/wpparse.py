"""Reading WordPress pages: forms, nonces, login results and challenges.

Everything here is pure (text in, data out) so it can be unit tested with
saved pages.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable


@dataclass
class Form:
    """One ``<form>`` with its fields as a browser would submit them."""

    action: str
    method: str
    attrs: dict[str, str]
    fields: list[tuple[str, str]] = field(default_factory=list)
    textareas: list[str] = field(default_factory=list)
    field_types: dict[str, str] = field(default_factory=dict)

    def value(self, name: str) -> str | None:
        for field_name, value in self.fields:
            if field_name == name:
                return value
        return None

    def has(self, name: str) -> bool:
        return any(field_name == name for field_name, _ in self.fields) or name in self.textareas

    def names(self) -> set[str]:
        return {name for name, _ in self.fields} | set(self.textareas)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._current: Form | None = None
        self._textarea: str | None = None
        self._textarea_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: (value or "") for key, value in attrs}
        if tag == "form":
            self._current = Form(action=values.get("action", ""), method=values.get("method", "get").lower(), attrs=values)
            self.forms.append(self._current)
            return
        if self._current is None:
            return
        name = values.get("name")
        if tag == "input" and name:
            input_type = values.get("type", "text").lower()
            self._current.field_types[name] = input_type
            if input_type in ("checkbox", "radio") and "checked" not in values:
                return
            if input_type in ("submit", "button", "image", "reset", "file"):
                return
            self._current.fields.append((name, values.get("value", "")))
        elif tag == "textarea" and name:
            self._textarea = name
            self._textarea_text = []
            self._current.textareas.append(name)
        elif tag in ("select", "button") and name:
            self._current.field_types[name] = tag

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._textarea is not None:
            self._textarea = None
        elif tag == "form":
            self._current = None


def parse_forms(page: str) -> list[Form]:
    """Every form on ``page``."""
    parser = _FormParser()
    parser.feed(page)
    parser.close()
    return parser.forms


def find_form(page: str, *, with_field: str | None = None, with_value: tuple[str, str] | None = None, id_: str | None = None) -> Form | None:
    """The first form matching all given conditions."""
    for form in parse_forms(page):
        if id_ is not None and form.attrs.get("id") != id_:
            continue
        if with_field is not None and not form.has(with_field):
            continue
        if with_value is not None and form.value(with_value[0]) != with_value[1]:
            continue
        return form
    return None


# -- REST nonce -------------------------------------------------------------

_REST_NONCE_PATTERNS = (
    re.compile(r"createNonceMiddleware\(\s*[\"']([0-9a-f]{6,})[\"']\s*\)"),
    re.compile(r"wpApiSettings\s*=\s*(\{.*?\})\s*;", re.S),
)


def find_rest_nonce(page: str) -> str | None:
    """The ``wp_rest`` nonce that wp-admin pages embed for the REST API."""
    match = _REST_NONCE_PATTERNS[0].search(page)
    if match:
        return match.group(1)
    match = _REST_NONCE_PATTERNS[1].search(page)
    if match:
        try:
            settings = json.loads(match.group(1))
        except json.JSONDecodeError:
            settings = {}
        nonce = settings.get("nonce")
        if isinstance(nonce, str) and nonce:
            return nonce
    return None


def find_link(page: str, pattern: str) -> str | None:
    """First ``href`` whose (unescaped) value matches the regex ``pattern``."""
    regex = re.compile(pattern)
    for match in re.finditer(r"""href\s*=\s*["']([^"']+)["']""", page):
        href = html.unescape(match.group(1))
        if regex.search(href):
            return href
    return None


# -- challenges and blocks ----------------------------------------------------

class PageKind:
    """What a response from the source turned out to be."""

    OK = "ok"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    BOT_CHALLENGE = "bot_challenge"
    IP_BLOCKED = "ip_blocked"
    CAPTCHA = "captcha"
    TWO_FACTOR = "two_factor"
    WRONG_PASSWORD = "wrong_password"
    LOGGED_IN = "logged_in"
    LOGIN_PAGE_MISSING = "login_page_missing"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


_CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "cf-turnstile", "grecaptcha", "hcaptcha.com", "recaptcha/api.js", "challenges.cloudflare.com/turnstile", "frc-captcha", "name=\"captcha", "id=\"captcha", "class=\"captcha", "math-captcha", "really-simple-captcha")
_TWO_FACTOR_FIELDS = ("authcode", "two-factor-totp-authcode", "googleotp", "wfls-token", "wp-2fa-totp-code", "mo2f_otp_token", "otp", "totp", "2fa_code", "two_factor_code", "aiowps-2fa", "itsec_two_factor")
_TWO_FACTOR_MARKERS = ("two-factor", "two_factor", "2fa", "authentication code", "verification code", "wfls-", "wp-2fa", "validate_2fa", "itsec-2fa")
_BOT_MARKERS = (
    ("sucuri", "Sucuri website firewall"),
    ("access denied - sucuri", "Sucuri website firewall"),
    ("_incapsula_resource", "Imperva/Incapsula"),
    ("ddos-guard", "DDoS-Guard"),
    ("aes.js", "a JavaScript browser check"),
    ("document.cookie=\"__test=", "a JavaScript browser check"),
    ("sgcaptcha", "a hosting bot check"),
    ("/.well-known/sgcaptcha", "a hosting bot check"),
    ("please enable javascript and cookies", "a JavaScript browser check"),
    ("checking your browser", "a browser check"),
)
_BLOCK_MARKERS = (
    "your access to this site has been limited",
    "has been blocked",
    "you have been blocked",
    "ip address has been blocked",
    "too many failed login attempts",
    "access from your area has been temporarily limited",
    "403 forbidden",
    "you don't have permission to access",
    "you do not have permission to access",
    "wordfence",
)


@dataclass(frozen=True)
class Classification:
    """Result of classifying one response."""

    kind: str
    detail: str = ""


def classify_challenge(status: int, headers: dict[str, str], body: str) -> Classification | None:
    """Detect a CDN challenge, bot check or IP block in any response.

    Returns ``None`` when the response looks like ordinary site content.
    """
    lowered = body[:200_000].lower()
    server = headers.get("server", "").lower()
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return Classification(PageKind.CLOUDFLARE_CHALLENGE, "Cloudflare browser challenge")
    if ("cloudflare" in server or "cf-ray" in headers) and (
        "cf-chl" in lowered or "challenge-platform" in lowered or "just a moment" in lowered or "attention required" in lowered
    ):
        return Classification(PageKind.CLOUDFLARE_CHALLENGE, "Cloudflare browser challenge")
    if status == 429:
        return Classification(PageKind.RATE_LIMITED, "the site is limiting how many requests it accepts")
    for marker, label in _BOT_MARKERS:
        if marker in lowered and ("<form" not in lowered or "loginform" not in lowered):
            return Classification(PageKind.BOT_CHALLENGE, label)
    if status in (403, 406, 451, 503) and any(marker in lowered for marker in _BLOCK_MARKERS):
        return Classification(PageKind.IP_BLOCKED, f"HTTP {status}")
    if status == 403 and "loginform" not in lowered:
        return Classification(PageKind.IP_BLOCKED, "HTTP 403")
    return None


def login_page_problems(page: str) -> Classification | None:
    """Problems visible on the login page before posting the password."""
    lowered = page.lower()
    form = find_form(page, id_="loginform") or find_form(page, with_field="pwd")
    if form is None:
        return Classification(PageKind.LOGIN_PAGE_MISSING, "no WordPress login form on the page")
    if any(marker in lowered for marker in _CAPTCHA_MARKERS):
        return Classification(PageKind.CAPTCHA, "the login form has a CAPTCHA")
    return None


def classify_login_result(status: int, headers: dict[str, str], body: str, has_auth_cookie: bool) -> Classification:
    """Interpret the response after posting the login form."""
    challenge = classify_challenge(status, headers, body)
    if challenge:
        return challenge
    lowered = body.lower()
    forms = parse_forms(body)
    field_names = {name.lower() for form in forms for name in form.names()}
    if any(name in field_names for name in _TWO_FACTOR_FIELDS) or (
        not has_auth_cookie and any(marker in lowered for marker in _TWO_FACTOR_MARKERS) and "loginform" in lowered and "pwd" not in field_names
    ):
        return Classification(PageKind.TWO_FACTOR, "the account uses two-step login")
    if has_auth_cookie:
        return Classification(PageKind.LOGGED_IN)
    if any(marker in lowered for marker in _CAPTCHA_MARKERS) and ("captcha" in lowered and "login_error" in lowered):
        return Classification(PageKind.CAPTCHA, "the login form requires a CAPTCHA")
    if 'id="login_error"' in lowered or "id='login_error'" in lowered:
        message = re.sub(r"<[^>]+>", " ", body[lowered.index("login_error"):][:600])
        message = html.unescape(" ".join(message.split()))
        if "captcha" in message.lower():
            return Classification(PageKind.CAPTCHA, "the login form requires a CAPTCHA")
        if "locked" in message.lower() or "too many" in message.lower() or "blocked" in message.lower():
            return Classification(PageKind.IP_BLOCKED, message[:200])
        return Classification(PageKind.WRONG_PASSWORD, message[:200])
    if "pwd" in field_names:
        return Classification(PageKind.WRONG_PASSWORD, "the login page came back without logging in")
    return Classification(PageKind.UNKNOWN, f"HTTP {status}")


def is_not_allowed_page(body: str) -> bool:
    """WordPress's "Sorry, you are not allowed to access this page." screen."""
    lowered = body.lower()
    return "you are not allowed" in lowered or "you do not have sufficient permissions" in lowered or "cheatin" in lowered


# -- the Reprint settings page -------------------------------------------------

@dataclass(frozen=True)
class ReprintPage:
    """What the Tools → Reprint Server screen offers."""

    enroll_form: Form | None
    connection_token_form: Form | None
    remove_key_forms: dict[str, Form]
    remove_connection_token_form: Form | None
    managed_by_file: bool
    api_url: str | None


def parse_reprint_page(page: str) -> ReprintPage:
    """Read the forms on ``tools.php?page=reprint-server``."""
    enroll = find_form(page, with_value=("action", "reprint_server_enroll_public_key"))
    token_form = find_form(page, with_field="reprint_server_connection_token")
    remove_forms: dict[str, Form] = {}
    for form in parse_forms(page):
        if form.value("action") == "reprint_server_remove_public_key":
            key_id = form.value("reprint_server_key_id")
            if key_id:
                remove_forms[key_id] = form
    remove_token = find_form(page, with_value=("action", "reprint_server_remove_connection_token"))
    managed = "public-keys.php</code>" in page or "secret.php</code>" in page
    api_url = None
    match = re.search(r'id="reprint-server-api-url"[^>]*value="([^"]+)"', page) or re.search(r'value="([^"]+)"[^>]*id="reprint-server-api-url"', page)
    if match:
        api_url = html.unescape(match.group(1))
    return ReprintPage(enroll, token_form, remove_forms, remove_token, managed, api_url)


def admin_notice_text(page: str) -> str:
    """Text of the WordPress admin notices on a page, for error messages."""
    notices = re.findall(r'<div[^>]+class="[^"]*notice[^"]*"[^>]*>(.*?)</div>', page, re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", notice) for notice in notices)
    return html.unescape(" ".join(text.split()))[:400]


def strip_tags(fragment: str) -> str:
    return html.unescape(" ".join(re.sub(r"<[^>]+>", " ", fragment).split()))


def iter_plugin_rows(plugins: Iterable[dict]) -> Iterable[dict]:
    for plugin in plugins:
        if isinstance(plugin, dict):
            yield plugin
