"""Tests for the HTTP client, against a local plain-HTTP server."""

import gzip
import http.server
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wpmigrate.engine.errors import MigrationError
from wpmigrate.engine.http import HttpClient, InsecureUrlRefused, decode_body, parse_retry_after


class Handler(http.server.BaseHTTPRequestHandler):
    calls: list = []
    script: dict = {}

    def log_message(self, *args):  # silence
        pass

    def _reply(self):
        Handler.calls.append((self.command, self.path, dict(self.headers)))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/landing")
            self.send_header("Set-Cookie", "wordpress_logged_in_abc=secretvalue; Path=/")
            self.end_headers()
            return
        if self.path == "/flaky":
            count = sum(1 for call in Handler.calls if call[1] == "/flaky")
            if count < 3:
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.end_headers()
                return
        if self.path == "/gzip":
            payload = gzip.compress(b"compressed hello")
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = b"ok " + body
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _reply


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.calls = []
        self.sleeps = []
        self.tmp = TemporaryDirectory()
        self.client = HttpClient(cookie_file=Path(self.tmp.name) / "cookies.txt", allow_http=True, admin_delay=0, sleep=self.sleeps.append)

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_plain_http_by_default(self):
        client = HttpClient(cookie_file=None)
        with self.assertRaises(InsecureUrlRefused):
            client.get(self.base + "/")

    def test_browser_headers(self):
        self.client.get(self.base + "/page")
        headers = Handler.calls[0][2]
        self.assertIn("Chrome/", headers["User-Agent"])
        self.assertIn("text/html", headers["Accept"])
        self.assertEqual(headers["Accept-Encoding"], "gzip, deflate")
        self.assertIn("Accept-Language", headers)

    def test_form_post_sends_referer_and_origin(self):
        response = self.client.post(self.base + "/form", form={"a": "1"}, referer=self.base + "/page")
        headers = Handler.calls[0][2]
        self.assertEqual(headers["Referer"], self.base + "/page")
        self.assertEqual(headers["Origin"], self.base)
        self.assertEqual(response.text, "ok a=1")

    def test_redirect_keeps_cookies_and_switches_to_get(self):
        response = self.client.post(self.base + "/redirect", form={"x": "y"})
        self.assertEqual(response.url, self.base + "/landing")
        self.assertEqual(Handler.calls[1][0], "GET")
        self.assertTrue(self.client.has_cookie("wordpress_logged_in_"))
        cookie_file = Path(self.tmp.name) / "cookies.txt"
        self.assertEqual(cookie_file.stat().st_mode & 0o777, 0o600)

    def test_backoff_honours_retry_after(self):
        response = self.client.get(self.base + "/flaky")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.sleeps, [1.0, 1.0])

    def test_no_retry_for_login_posts(self):
        response = self.client.post(self.base + "/flaky", form={}, retry=False)
        self.assertEqual(response.status, 503)
        self.assertEqual(len(Handler.calls), 1)

    def test_gzip(self):
        self.assertEqual(self.client.get(self.base + "/gzip").text, "compressed hello")

    def test_unreachable(self):
        client = HttpClient(cookie_file=None, allow_http=True, admin_delay=0, max_attempts=2, sleep=lambda s: None)
        with self.assertRaises(MigrationError):
            client.get("http://127.0.0.1:1/")


class HelperTests(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertEqual(parse_retry_after("7"), 7.0)

    def test_retry_after_date(self):
        self.assertAlmostEqual(parse_retry_after("Thu, 01 Jan 1970 00:01:40 GMT", now=40.0), 60.0)

    def test_retry_after_garbage(self):
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(""))

    def test_deflate_raw_and_zlib(self):
        import zlib
        self.assertEqual(decode_body(zlib.compress(b"hi"), "deflate"), b"hi")
        raw = zlib.compressobj(wbits=-15)
        data = raw.compress(b"hi") + raw.flush()
        self.assertEqual(decode_body(data, "deflate"), b"hi")


if __name__ == "__main__":
    unittest.main()
