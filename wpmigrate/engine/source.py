"""Talking to the source WordPress site as its administrator would in a browser."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from wpmigrate import constants
from wpmigrate.engine import wpparse
from wpmigrate.engine.errors import LoginFailed, MigrationError, SourceStepFailed
from wpmigrate.engine.http import HttpClient, Response
from wpmigrate.engine.wpparse import Classification, PageKind


def normalize_site_url(text: str) -> str:
    """``example.com/blog/`` -> ``https://example.com/blog``."""
    text = text.strip()
    if not text:
        raise MigrationError("No site address was entered.", "Enter the address of the site you want to copy, for example https://example.com.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if not parts.hostname:
        raise MigrationError(f"{text} is not a website address.", "Enter an address such as https://example.com.")
    path = re.sub(r"/(wp-admin|wp-login\.php).*$", "", parts.path).rstrip("/")
    netloc = parts.hostname.lower() + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def reprint_api_url(site_url: str) -> str:
    """The Reprint API address shown in Tools → Reprint Server."""
    return site_url.rstrip("/") + "/?reprint-api"


@dataclass
class PluginInfo:
    plugin: str
    status: str


LOGIN_MESSAGES = {
    PageKind.CLOUDFLARE_CHALLENGE: (
        "Your original site is protected by a Cloudflare browser check, which this tool cannot and will not get around.",
        "You can finish the source steps yourself in the browser; the instructions follow.",
    ),
    PageKind.BOT_CHALLENGE: (
        "Your original site shows a bot check ({detail}) before the login page, which this tool will not try to get around.",
        "You can finish the source steps yourself in the browser; the instructions follow.",
    ),
    PageKind.IP_BLOCKED: (
        "Your original site refused this server ({detail}). Its firewall or security plugin may have blocked this server's address.",
        "Ask the site's administrator or host to allow this server, or finish the source steps yourself in the browser.",
    ),
    PageKind.RATE_LIMITED: (
        "Your original site is limiting requests from this server right now.",
        "Wait a few minutes and run the tool again, or finish the source steps yourself in the browser.",
    ),
    PageKind.CAPTCHA: (
        "The login page of your original site asks for a CAPTCHA, which this tool will not try to solve.",
        "You can finish the source steps yourself in the browser; the instructions follow.",
    ),
    PageKind.TWO_FACTOR: (
        "The administrator account uses two-step login (a code from an app, text or e-mail).",
        "You can finish the source steps yourself in the browser, where you can enter the code; the instructions follow.",
    ),
    PageKind.LOGIN_PAGE_MISSING: (
        "Your original site has no standard login page at {url} (a security plugin may have moved it).",
        "You can finish the source steps yourself in the browser; the instructions follow.",
    ),
    PageKind.UNKNOWN: (
        "Logging in to your original site did not work and the reason is unclear ({detail}).",
        "You can finish the source steps yourself in the browser; the instructions follow.",
    ),
}


class WrongPassword(LoginFailed):
    """Username or password was not accepted."""


class NotAdministrator(SourceStepFailed):
    """The account can log in but cannot manage plugins or settings."""


class SourceSite:
    """Session with your original site's admin area."""

    def __init__(self, client: HttpClient, site_url: str, log: Callable[[str], None]) -> None:
        self.client = client
        self.site_url = site_url.rstrip("/")
        self.log = log
        self.admin_url = self.site_url + "/wp-admin/"
        self.login_url = self.site_url + "/wp-login.php"
        self.rest_root: str | None = None
        self.rest_nonce: str | None = None

    # -- helpers ------------------------------------------------------------

    def _challenge_error(self, classification: Classification) -> SourceStepFailed:
        what, next_step = LOGIN_MESSAGES.get(classification.kind, LOGIN_MESSAGES[PageKind.UNKNOWN])
        return SourceStepFailed(
            what.format(detail=classification.detail, url=self.login_url) + " " + next_step,
            code=classification.kind,
        )

    def _check(self, response: Response) -> Response:
        problem = wpparse.classify_challenge(response.status, response.headers, response.text)
        if problem:
            raise self._challenge_error(problem)
        return response

    def rest_url(self, route: str) -> str:
        root = self.rest_root or (self.site_url + "/wp-json/")
        route = route.lstrip("/")
        if "?rest_route=" in root:
            base, _, prefix = root.partition("?rest_route=")
            path, _, query = route.partition("?")
            url = f"{base}?rest_route={prefix.rstrip('/')}/{path}"
            return url + (f"&{query}" if query else "")
        return root.rstrip("/") + "/" + route

    # -- login ----------------------------------------------------------------

    def discover(self) -> None:
        """Load the home page like a visitor and note the REST API root."""
        response = self._check(self.client.get(self.site_url + "/"))
        if response.status >= 400:
            raise SourceStepFailed(f"Your original site answered HTTP {response.status} for its home page.", code="HOME_FAILED")
        link = response.header("link")
        match = re.search(r'<([^>]+)>;\s*rel="https://api\.w\.org/"', link)
        if not match:
            match = re.search(r'<link[^>]+rel=["\']https://api\.w\.org/["\'][^>]+href=["\']([^"\']+)', response.text)
        if match:
            self.rest_root = match.group(1).replace("&#038;", "&").replace("&amp;", "&")

    def login(self, username: str, password: str) -> None:
        """Log in through wp-login.php. Raises on every distinct failure."""
        self.client.set_cookie(urllib.parse.urlsplit(self.site_url).hostname or "", "wordpress_test_cookie", "WP%20Cookie%20check")
        page = self.client.get(self.login_url)
        problem = wpparse.classify_challenge(page.status, page.headers, page.text)
        if problem:
            raise self._challenge_error(problem)
        if page.status == 404:
            raise self._challenge_error(Classification(PageKind.LOGIN_PAGE_MISSING, "HTTP 404"))
        login_problem = wpparse.login_page_problems(page.text)
        if login_problem:
            raise self._challenge_error(login_problem)
        form = wpparse.find_form(page.text, id_="loginform") or wpparse.find_form(page.text, with_field="pwd")
        assert form is not None
        fields = [(name, value) for name, value in form.fields if name not in ("log", "pwd", "redirect_to", "testcookie", "rememberme")]
        fields += [("log", username), ("pwd", password), ("wp-submit", "Log In"), ("redirect_to", self.admin_url), ("testcookie", "1")]
        action = urllib.parse.urljoin(page.url, form.action or self.login_url)
        result = self.client.post(action, form=fields, referer=page.url, retry=False)
        has_auth = self.client.has_cookie("wordpress_logged_in_")
        classification = wpparse.classify_login_result(result.status, result.headers, result.text, has_auth)
        self.log(f"login result: {classification.kind}")
        if classification.kind == PageKind.WRONG_PASSWORD:
            raise WrongPassword(
                "Your original site did not accept that username and password.",
                "Check them (the same ones you use at wp-admin) and try again.",
                code="WRONG_PASSWORD",
            )
        if classification.kind != PageKind.LOGGED_IN:
            raise self._challenge_error(classification)
        if "/wp-admin" in result.url:
            self.admin_url = result.url.split("/wp-admin", 1)[0] + "/wp-admin/"

    def is_logged_in(self) -> bool:
        """Whether the saved cookies still give access to wp-admin."""
        if not self.client.has_cookie("wordpress_logged_in_"):
            return False
        response = self.client.get(self.admin_url, follow_redirects=False)
        return response.status == 200 and "wp-login.php" not in response.header("location")

    def _admin_page(self, path: str) -> Response:
        response = self._check(self.client.get(self.admin_url + path, referer=self.admin_url))
        if "wp-login.php" in response.url:
            raise SourceStepFailed("The login session on your original site ended.", code="SESSION_EXPIRED")
        return response

    def load_rest_nonce(self) -> str:
        """Read the REST API nonce from a wp-admin page, as the browser does."""
        for path in ("", "plugins.php", "index.php"):
            response = self._admin_page(path)
            nonce = wpparse.find_rest_nonce(response.text)
            if nonce:
                self.rest_nonce = nonce
                return nonce
        ajax = self._check(self.client.get(self.admin_url + "admin-ajax.php?action=rest-nonce", referer=self.admin_url))
        if ajax.status == 200 and re.fullmatch(r"[0-9a-f]{6,}", ajax.text.strip()):
            self.rest_nonce = ajax.text.strip()
            return self.rest_nonce
        raise SourceStepFailed("Could not find the REST API security token on the source admin pages.", code="NO_REST_NONCE")

    def rest(self, method: str, route: str, body: Any = None) -> Response:
        if not self.rest_nonce:
            self.load_rest_nonce()
        headers = {"X-WP-Nonce": self.rest_nonce or "", "Accept": "application/json, */*;q=0.1"}
        response = self.client.request(method, self.rest_url(route), json_body=body, headers=headers, referer=self.admin_url, retry=method == "GET")
        if "json" not in response.header("content-type"):
            # A JSON answer comes from WordPress itself (even a 403 such as
            # "plugin installs are disabled"); only HTML may be a block page.
            self._check(response)
        if response.status == 403 and "rest_cookie_invalid_nonce" in response.text:
            self.load_rest_nonce()
            headers["X-WP-Nonce"] = self.rest_nonce or ""
            response = self.client.request(method, self.rest_url(route), json_body=body, headers=headers, referer=self.admin_url, retry=False)
        return response

    def check_administrator(self) -> None:
        """Make sure the account may install plugins and change settings."""
        response = self.rest("GET", "wp/v2/users/me?context=edit")
        if response.status == 200:
            try:
                capabilities = response.json().get("capabilities", {})
            except ValueError:
                capabilities = {}
            if capabilities and not capabilities.get("manage_options"):
                raise NotAdministrator(
                    "This account can log in to your original site but is not an administrator, so it cannot install plugins or change settings.",
                    code="NOT_ADMIN",
                )
            if capabilities:
                return
        settings = self._admin_page("options-general.php")
        if settings.status == 403 or wpparse.is_not_allowed_page(settings.text):
            raise NotAdministrator(
                "This account can log in to your original site but is not an administrator, so it cannot install plugins or change settings.",
                code="NOT_ADMIN",
            )

    # -- plugin -----------------------------------------------------------------

    def find_reprint_plugin(self) -> PluginInfo | None:
        response = self.rest("GET", "wp/v2/plugins?context=edit")
        if response.status != 200:
            return self._find_reprint_plugin_admin()
        try:
            plugins = response.json()
        except ValueError:
            return self._find_reprint_plugin_admin()
        for plugin in wpparse.iter_plugin_rows(plugins):
            name = str(plugin.get("plugin", ""))
            if name.split("/", 1)[0] == constants.REPRINT_PLUGIN_SLUG or plugin.get("textdomain") == "reprint":
                return PluginInfo(name, str(plugin.get("status", "inactive")))
        return None

    def _find_reprint_plugin_admin(self) -> PluginInfo | None:
        page = self._admin_page("plugins.php")
        match = re.search(r'data-plugin="((?:reprint|reprint-[a-z-]+)/[^"]+\.php)"[^>]*class="([^"]*)"', page.text) or re.search(
            r'class="([^"]*)"[^>]*data-plugin="((?:reprint|reprint-[a-z-]+)/[^"]+\.php)"', page.text
        )
        if not match:
            return None
        if match.group(1).endswith(".php"):
            plugin, classes = match.group(1), match.group(2)
        else:
            classes, plugin = match.group(1), match.group(2)
        return PluginInfo(plugin.removesuffix(".php"), "active" if " active" in f" {classes}" else "inactive")

    def install_and_activate(self) -> PluginInfo:
        """Install Reprint from the plugin directory and activate it."""
        response = self.rest("POST", "wp/v2/plugins", {"slug": constants.REPRINT_PLUGIN_SLUG, "status": "active"})
        if response.status in (200, 201):
            data = response.json()
            return PluginInfo(str(data.get("plugin", "reprint/reprint")), str(data.get("status", "active")))
        code = ""
        try:
            code = str(response.json().get("code", ""))
        except ValueError:
            pass
        self.log(f"REST plugin install answered {response.status} {code}")
        if code in ("rest_cannot_install_plugin", "rest_forbidden") and self._file_mods_disallowed():
            raise SourceStepFailed(
                "Your original site does not allow installing plugins from wp-admin (plugin and theme installation is switched off in its configuration).",
                code="FILE_MODS_DISALLOWED",
            )
        # The request may have installed the plugin before failing (or the
        # folder already existed); look before installing again.
        found = self.find_reprint_plugin()
        if found:
            return found if found.status in ("active", "network-active") else self.activate(found)
        return self._install_with_admin_screens()

    def _file_mods_disallowed(self) -> bool:
        page = self.client.get(self.admin_url + "plugin-install.php", referer=self.admin_url)
        return page.status == 403 or wpparse.is_not_allowed_page(page.text) or "plugin-install.php" not in page.url

    def _install_with_admin_screens(self) -> PluginInfo:
        search = self._admin_page("plugin-install.php?s=reprint&tab=search&type=term")
        if wpparse.is_not_allowed_page(search.text) or search.status == 403:
            raise SourceStepFailed("Your original site does not let this account install plugins.", code="CANNOT_INSTALL")
        link = wpparse.find_link(search.text, r"update\.php\?action=install-plugin&plugin=reprint&_wpnonce=")
        if not link:
            raise SourceStepFailed("Could not find the Install button for Reprint on your original site's Add Plugins screen.", code="NO_INSTALL_LINK")
        result = self._check(self.client.get(urllib.parse.urljoin(search.url, link), referer=search.url))
        activate = wpparse.find_link(result.text, r"plugins\.php\?.*action=activate.*plugin=reprint%2F")
        if not activate:
            found = self._find_reprint_plugin_admin()
            if found is None:
                raise SourceStepFailed("Installing Reprint through your original site's admin screens did not work: " + wpparse.strip_tags(result.text)[:300], code="INSTALL_FAILED")
            return self.activate(found)
        self._check(self.client.get(urllib.parse.urljoin(result.url, activate), referer=result.url))
        found = self._find_reprint_plugin_admin()
        if not found or found.status != "active":
            raise SourceStepFailed("Reprint was installed on your original site but could not be activated.", code="ACTIVATE_FAILED")
        return found

    def activate(self, plugin: PluginInfo) -> PluginInfo:
        response = self.rest("POST", f"wp/v2/plugins/{plugin.plugin}", {"status": "active"})
        if response.status == 200:
            return PluginInfo(plugin.plugin, "active")
        page = self._admin_page("plugins.php")
        link = wpparse.find_link(page.text, r"plugins\.php\?.*action=activate.*plugin=" + re.escape(urllib.parse.quote(plugin.plugin + ".php", safe="")))
        if not link:
            raise SourceStepFailed("Reprint is installed on your original site but could not be activated.", code="ACTIVATE_FAILED")
        self._check(self.client.get(urllib.parse.urljoin(page.url, link), referer=page.url))
        return PluginInfo(plugin.plugin, "active")

    def deactivate_and_delete(self, plugin: str) -> None:
        response = self.rest("POST", f"wp/v2/plugins/{plugin}", {"status": "inactive"})
        if response.status != 200:
            page = self._admin_page("plugins.php")
            link = wpparse.find_link(page.text, r"plugins\.php\?.*action=deactivate.*plugin=" + re.escape(urllib.parse.quote(plugin + ".php", safe="")))
            if link:
                self._check(self.client.get(urllib.parse.urljoin(page.url, link), referer=page.url))
        response = self.rest("DELETE", f"wp/v2/plugins/{plugin}")
        if response.status == 200:
            return
        page = self._admin_page("plugins.php")
        link = wpparse.find_link(page.text, r"plugins\.php\?.*action=delete-selected.*" + re.escape(urllib.parse.quote(plugin + ".php", safe="")))
        if not link:
            raise SourceStepFailed("Could not remove the Reprint plugin from your original site.", code="DELETE_FAILED")
        confirm = self._check(self.client.get(urllib.parse.urljoin(page.url, link), referer=page.url))
        form = wpparse.find_form(confirm.text, with_value=("action", "delete-selected"))
        if form is None:
            raise SourceStepFailed("Could not remove the Reprint plugin from your original site.", code="DELETE_FAILED")
        fields = list(form.fields)
        if not form.has("verify-delete"):
            fields.append(("verify-delete", "1"))
        self._check(self.client.post(urllib.parse.urljoin(confirm.url, form.action or "plugins.php"), form=fields, referer=confirm.url, retry=False))

    # -- Reprint settings screen -------------------------------------------------

    def reprint_page(self) -> tuple[Response, wpparse.ReprintPage]:
        response = self._admin_page(constants.REPRINT_ADMIN_PAGE)
        if response.status == 403 or wpparse.is_not_allowed_page(response.text):
            raise SourceStepFailed("The Tools → Reprint Server screen is not available to this account.", code="NO_SETTINGS_PAGE")
        return response, wpparse.parse_reprint_page(response.text)

    def enroll_migration_token(self, public_key: str) -> str | None:
        """Paste the Migration Token into the Authorize form. Returns the key id."""
        response, page = self.reprint_page()
        if page.enroll_form is None:
            raise SourceStepFailed("The Reprint Server screen on the source has no form for a Migration Token.", code="NO_ENROLL_FORM")
        form = page.enroll_form
        fields = [(name, value) for name, value in form.fields if name != "reprint_server_public_key"]
        fields.append(("reprint_server_public_key", public_key))
        result = self._check(
            self.client.post(urllib.parse.urljoin(response.url, form.action), form=fields, referer=response.url, retry=False)
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.url).query)
        notice = (query.get("reprint_server_notice") or [""])[0]
        if notice in ("enrolled", "enroll_duplicate"):
            return (query.get("reprint_server_key_id") or [None])[0]
        raise SourceStepFailed(
            "Your original site did not accept the Migration Token"
            + (f" (it answered: {notice.replace('_', ' ')})." if notice else ".")
            + (" " + wpparse.admin_notice_text(result.text) if wpparse.admin_notice_text(result.text) else ""),
            code="ENROLL_FAILED",
        )

    def save_connection_token(self, token: str) -> None:
        """Save a shared connection token (sources without OpenSSL)."""
        response, page = self.reprint_page()
        form = page.connection_token_form
        if form is None:
            raise SourceStepFailed("The Reprint Server screen on the source has no connection token form.", code="NO_TOKEN_FORM")
        fields = [(name, value) for name, value in form.fields if name != "reprint_server_connection_token"]
        fields.append(("reprint_server_connection_token", token))
        result = self._check(
            self.client.post(urllib.parse.urljoin(response.url, form.action), form=fields, referer=response.url, retry=False)
        )
        if result.status >= 400 or wpparse.is_not_allowed_page(result.text):
            raise SourceStepFailed("Your original site did not save the connection token.", code="TOKEN_SAVE_FAILED")

    def remove_migration_token(self, key_id: str) -> bool:
        """Remove our enrolled key. Returns False if it was already gone."""
        response, page = self.reprint_page()
        form = page.remove_key_forms.get(key_id)
        if form is None:
            return False
        result = self._check(
            self.client.post(urllib.parse.urljoin(response.url, form.action), form=form.fields, referer=response.url, retry=False)
        )
        notice = (urllib.parse.parse_qs(urllib.parse.urlsplit(result.url).query).get("reprint_server_notice") or [""])[0]
        if notice not in ("key_removed", ""):
            raise SourceStepFailed(f"Removing the Migration Token failed ({notice.replace('_', ' ')}).", code="REMOVE_FAILED")
        return True

    def clear_connection_token(self) -> None:
        self.save_connection_token("")
