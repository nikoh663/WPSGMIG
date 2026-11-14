"""Tests for reading WordPress pages."""

import unittest

from wpmigrate.engine import wpparse
from wpmigrate.engine.wpparse import PageKind

LOGIN_PAGE = """
<html><body class="login">
<form name="loginform" id="loginform" action="https://example.com/wp-login.php" method="post">
  <input type="text" name="log" id="user_login" value="">
  <input type="password" name="pwd" id="user_pass" value="">
  <input name="rememberme" type="checkbox" id="rememberme" value="forever">
  <input type="submit" name="wp-submit" id="wp-submit" value="Log In">
  <input type="hidden" name="redirect_to" value="https://example.com/wp-admin/">
  <input type="hidden" name="testcookie" value="1">
</form></body></html>
"""

WRONG_PASSWORD = LOGIN_PAGE.replace(
    '<form name="loginform"',
    '<div id="login_error" class="notice notice-error"><p><strong>Error:</strong> The password you entered for the username <strong>admin</strong> is incorrect.</p></div><form name="loginform"',
)

TWO_FACTOR = """
<html><body class="login">
<form name="validate_2fa_form" id="loginform" action="https://example.com/wp-login.php?action=validate_2fa" method="post">
  <input type="hidden" name="wp-auth-id" value="1">
  <input type="hidden" name="wp-auth-nonce" value="abc123">
  <p>Enter the code from your authenticator app.</p>
  <input type="tel" name="authcode" id="authcode" value="">
</form></body></html>
"""

CAPTCHA_LOGIN = LOGIN_PAGE.replace("</form>", '<div class="g-recaptcha" data-sitekey="x"></div></form>')

CLOUDFLARE = "<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'></div></body></html>"

WORDFENCE_BLOCK = "<html><body><h1>Your access to this site has been limited by the site owner</h1>Wordfence</body></html>"

REPRINT_KEY_PAGE = """
<div class="wrap reprint-server-page"><h1>Reprint Server</h1>
<form method="post" action="https://example.com/wp-admin/admin-post.php">
  <input type="hidden" name="action" value="reprint_server_enroll_public_key" />
  <input type="hidden" id="_wpnonce" name="_wpnonce" value="0a1b2c3d4e" /><input type="hidden" name="_wp_http_referer" value="/wp-admin/tools.php?page=reprint-server" />
  <textarea id="reprint_server_public_key" name="reprint_server_public_key" rows="3" required></textarea>
  <input type="submit" name="submit" id="submit" class="button button-primary" value="Authorize client" />
</form>
<input type="text" class="code" id="reprint-server-api-url" value="https://example.com/?reprint-api" readonly />
<form method="post" action="https://example.com/wp-admin/admin-post.php">
  <input type="hidden" name="action" value="reprint_server_remove_public_key" />
  <input type="hidden" name="reprint_server_key_id" value="cedb74be13fb9cdd" />
  <input type="hidden" id="_wpnonce" name="_wpnonce" value="99aa88bb77" />
  <input type="submit" value="Remove Migration Token" />
</form>
</div>
"""

REPRINT_TOKEN_PAGE = """
<form method="post" action="https://example.com/wp-admin/options.php">
  <input type='hidden' name='option_page' value='reprint_server' /><input type="hidden" name="action" value="update" />
  <input type="hidden" id="_wpnonce" name="_wpnonce" value="5f5f5f5f5f" />
  <input type="password" spellcheck="false" class="regular-text code" id="reprint_server_connection_token" name="reprint_server_connection_token" value="" autocomplete="off" />
  <input type="submit" name="submit" value="Save connection token" />
</form>
"""


class FormTests(unittest.TestCase):
    def test_login_form_fields(self):
        form = wpparse.find_form(LOGIN_PAGE, id_="loginform")
        self.assertIsNotNone(form)
        self.assertEqual(form.value("redirect_to"), "https://example.com/wp-admin/")
        self.assertTrue(form.has("pwd"))
        # Unchecked checkboxes and submit buttons are not submitted.
        self.assertIsNone(form.value("rememberme"))
        self.assertIsNone(form.value("wp-submit"))

    def test_reprint_page_key_host(self):
        page = wpparse.parse_reprint_page(REPRINT_KEY_PAGE)
        self.assertIsNotNone(page.enroll_form)
        self.assertEqual(page.enroll_form.value("_wpnonce"), "0a1b2c3d4e")
        self.assertIn("reprint_server_public_key", page.enroll_form.textareas)
        self.assertIsNone(page.connection_token_form)
        self.assertEqual(list(page.remove_key_forms), ["cedb74be13fb9cdd"])
        self.assertEqual(page.api_url, "https://example.com/?reprint-api")

    def test_reprint_page_token_host(self):
        page = wpparse.parse_reprint_page(REPRINT_TOKEN_PAGE)
        self.assertIsNone(page.enroll_form)
        self.assertIsNotNone(page.connection_token_form)
        self.assertEqual(page.connection_token_form.value("option_page"), "reprint_server")
        self.assertEqual(page.connection_token_form.action, "https://example.com/wp-admin/options.php")

    def test_rest_nonce_from_api_fetch(self):
        page = '<script>wp.apiFetch.use( wp.apiFetch.createNonceMiddleware( "1a2b3c4d5e" ) );</script>'
        self.assertEqual(wpparse.find_rest_nonce(page), "1a2b3c4d5e")

    def test_rest_nonce_from_wp_api_settings(self):
        page = '<script>var wpApiSettings = {"root":"https:\\/\\/example.com\\/wp-json\\/","nonce":"abcdef1234","versionString":"wp\\/v2\\/"};</script>'
        self.assertEqual(wpparse.find_rest_nonce(page), "abcdef1234")

    def test_find_link_unescapes(self):
        page = '<a class="install-now button" href="https://example.com/wp-admin/update.php?action=install-plugin&#038;plugin=reprint&#038;_wpnonce=abc123">Install</a>'
        self.assertEqual(
            wpparse.find_link(page, r"update\.php\?action=install-plugin&plugin=reprint&_wpnonce="),
            "https://example.com/wp-admin/update.php?action=install-plugin&plugin=reprint&_wpnonce=abc123",
        )


class LoginClassificationTests(unittest.TestCase):
    def test_logged_in(self):
        result = wpparse.classify_login_result(200, {}, "<html>Dashboard</html>", has_auth_cookie=True)
        self.assertEqual(result.kind, PageKind.LOGGED_IN)

    def test_wrong_password(self):
        result = wpparse.classify_login_result(200, {}, WRONG_PASSWORD, has_auth_cookie=False)
        self.assertEqual(result.kind, PageKind.WRONG_PASSWORD)
        self.assertIn("incorrect", result.detail)

    def test_two_factor(self):
        result = wpparse.classify_login_result(200, {}, TWO_FACTOR, has_auth_cookie=False)
        self.assertEqual(result.kind, PageKind.TWO_FACTOR)

    def test_captcha_before_login(self):
        problem = wpparse.login_page_problems(CAPTCHA_LOGIN)
        self.assertEqual(problem.kind, PageKind.CAPTCHA)

    def test_captcha_error_after_login(self):
        page = LOGIN_PAGE.replace('<form name="loginform"', '<div id="login_error"><strong>Error</strong>: Please solve the CAPTCHA.</div><form name="loginform"')
        result = wpparse.classify_login_result(200, {}, page, has_auth_cookie=False)
        self.assertEqual(result.kind, PageKind.CAPTCHA)

    def test_lockout_message(self):
        page = LOGIN_PAGE.replace('<form name="loginform"', '<div id="login_error">Too many failed login attempts. Please try again in 20 minutes.</div><form name="loginform"')
        result = wpparse.classify_login_result(200, {}, page, has_auth_cookie=False)
        self.assertEqual(result.kind, PageKind.IP_BLOCKED)

    def test_missing_login_form(self):
        self.assertEqual(wpparse.login_page_problems("<html>Not here</html>").kind, PageKind.LOGIN_PAGE_MISSING)


class ChallengeTests(unittest.TestCase):
    def test_cloudflare_header(self):
        result = wpparse.classify_challenge(403, {"cf-mitigated": "challenge", "server": "cloudflare"}, CLOUDFLARE)
        self.assertEqual(result.kind, PageKind.CLOUDFLARE_CHALLENGE)

    def test_cloudflare_body(self):
        result = wpparse.classify_challenge(503, {"server": "cloudflare", "cf-ray": "1"}, CLOUDFLARE)
        self.assertEqual(result.kind, PageKind.CLOUDFLARE_CHALLENGE)

    def test_wordfence_block(self):
        result = wpparse.classify_challenge(403, {}, WORDFENCE_BLOCK)
        self.assertEqual(result.kind, PageKind.IP_BLOCKED)

    def test_rate_limited(self):
        self.assertEqual(wpparse.classify_challenge(429, {}, "slow down").kind, PageKind.RATE_LIMITED)

    def test_hosting_bot_check(self):
        page = '<html><head><meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=%2Fwp-login.php"></head></html>'
        self.assertEqual(wpparse.classify_challenge(202, {}, page).kind, PageKind.BOT_CHALLENGE)

    def test_ordinary_page(self):
        self.assertIsNone(wpparse.classify_challenge(200, {"server": "Apache"}, LOGIN_PAGE))

    def test_not_allowed(self):
        self.assertTrue(wpparse.is_not_allowed_page("<p>Sorry, you are not allowed to access this page.</p>"))


if __name__ == "__main__":
    unittest.main()
