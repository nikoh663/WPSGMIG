"""Tests for wp-config editing, Reprint output parsing, redaction and helpers."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wpmigrate.engine import destination, wpconfig
from wpmigrate.engine.php import parse_version
from wpmigrate.engine.reprint import LAUNCHER, ReprintRunner, describe_progress, is_final_report, parse_jsonl_line, progress_percent
from wpmigrate.engine.source import normalize_site_url, reprint_api_url
from wpmigrate.redact import Redactor

CONFIG = """<?php
// ** Database settings ** //
define( 'DB_NAME', 'old_db' );
/** define('DB_NAME', 'commented'); */
define( 'DB_USER', getenv_docker('WORDPRESS_DB_USER', 'example (user)') );
define( "DB_PASSWORD", 'it\\'s secret' );
define( 'DB_HOST', 'localhost' );
define( 'WP_HOME', 'https://old.example' );
$table_prefix = 'wp_';
/* That's all, stop editing! Happy publishing. */
require_once ABSPATH . 'wp-settings.php';
"""


class WpConfigTests(unittest.TestCase):
    def test_read(self):
        self.assertEqual(wpconfig.read_define(CONFIG, "DB_NAME"), "old_db")
        self.assertEqual(wpconfig.read_define(CONFIG, "DB_PASSWORD"), "it's secret")
        self.assertIsNone(wpconfig.read_define(CONFIG, "DB_USER"))  # not a literal
        self.assertEqual(wpconfig.read_table_prefix(CONFIG), "wp_")

    def test_apply_destination(self):
        settings = wpconfig.DatabaseSettings("db.internal:3307", "new_db", "new_user", "p'w\\d", "abc_")
        text = wpconfig.apply_destination(CONFIG, db=settings, site_url="https://new.example")
        self.assertEqual(wpconfig.read_define(text, "DB_NAME"), "new_db")
        self.assertEqual(wpconfig.read_define(text, "DB_USER"), "new_user")
        self.assertEqual(wpconfig.read_define(text, "DB_PASSWORD"), "p'w\\d")
        self.assertEqual(wpconfig.read_define(text, "DB_HOST"), "db.internal:3307")
        self.assertEqual(wpconfig.read_table_prefix(text), "abc_")
        self.assertEqual(wpconfig.read_define(text, "WP_HOME"), "https://new.example")
        self.assertIn("define('DB_NAME', 'commented')", text)  # comment untouched
        self.assertNotIn("old_db", text)
        self.assertNotIn("getenv_docker", text)

    def test_add_missing_define(self):
        text = wpconfig.set_define(CONFIG, "WP_CACHE", False)
        self.assertLess(text.index("WP_CACHE"), text.index("That's all"))

    def test_read_settings(self):
        plain = CONFIG.replace("getenv_docker('WORDPRESS_DB_USER', 'example (user)')", "'u'")
        settings = wpconfig.read_database_settings(plain)
        self.assertEqual((settings.name, settings.user, settings.password, settings.table_prefix), ("old_db", "u", "it's secret", "wp_"))


class ReprintOutputTests(unittest.TestCase):
    def test_parse_lines(self):
        self.assertIsNone(parse_jsonl_line("Downloading..."))
        self.assertIsNone(parse_jsonl_line("{broken"))
        self.assertIsNone(parse_jsonl_line("[1,2]"))
        report = parse_jsonl_line('{"type":"reprint_report","schema_version":1,"command":"files-pull","status":"complete","exit_code":0}')
        self.assertTrue(is_final_report(report))
        self.assertFalse(is_final_report({"type": "reprint_report", "schema_version": 2}))

    def test_progress(self):
        record = {"progress": {"items": {"unit": "files", "done": 10, "total": 40}, "bytes": {"done": 50, "total": 200}, "current_table": None}}
        self.assertEqual(progress_percent(record), 25.0)
        self.assertIn("10 of 40 files", describe_progress(record))
        self.assertEqual(progress_percent({"progress": {"items": {"done": 3, "total": 4}, "bytes": {"done": 1, "total": None}}}), 75.0)
        self.assertIsNone(progress_percent({"progress": {"items": {"done": 3, "total": None}, "bytes": None}}))

    def test_launcher_keeps_secrets_out_of_argv(self):
        captured = {}

        class FakeSystem:
            def stream(self, argv, *, on_stdout_line, on_stderr_line, stdin_text=None, env=None, cwd=None):
                captured["argv"] = argv
                captured["stdin"] = stdin_text
                on_stdout_line('{"type":"reprint_report","schema_version":1,"command":"db-apply","status":"complete","exit_code":0}')
                return 0

        runner = ReprintRunner(system=FakeSystem(), php="php", phar=Path("/x/reprint.phar"), state_dir=Path("/s"), fs_root=Path("/f"),
                               api_url="https://e.test/?reprint-api", emit=lambda e: None, log=lambda m: None)
        result = runner.run(["db-apply", "https://e.test/?reprint-api"], stage="deploy", secret_args=["--target-pass=hunter2"])
        self.assertEqual(result.status, "complete")
        self.assertNotIn("hunter2", " ".join(captured["argv"]))
        self.assertIn("hunter2", captured["stdin"])
        self.assertEqual(captured["argv"][4], LAUNCHER)


class HelperTests(unittest.TestCase):
    def test_normalize_site_url(self):
        self.assertEqual(normalize_site_url("Example.com/"), "https://example.com")
        self.assertEqual(normalize_site_url("https://example.com/blog/wp-admin/"), "https://example.com/blog")
        self.assertEqual(reprint_api_url("https://example.com/blog"), "https://example.com/blog/?reprint-api")

    def test_parse_version(self):
        self.assertEqual(parse_version("8.2.10-1+ubuntu"), (8, 2, 10))
        self.assertEqual(parse_version("7.4"), (7, 4, 0))

    def test_domain_and_old_folder(self):
        self.assertEqual(destination.site_domain("https://www.example.com/blog"), "example.com")
        self.assertEqual(
            destination.old_site_folder_guess(Path("/home/u/www/example.com/public_html"), "example.com"),
            Path("/home/u/www/old.example.com/public_html"),
        )
        self.assertEqual(
            destination.old_site_folder_guess(Path("/home/u/www/example.com/public_html/new"), "example.com"),
            Path("/home/u/www/old.example.com/public_html"),
        )

    def test_split_entries(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("wp-admin", "wp-content"):
                (folder / name).mkdir()
            for name in ("wp-config.php", "index.php", ".htaccess", "google123.html", "robots.txt"):
                (folder / name).write_text("x")
            wordpress, other = destination.split_entries(folder)
            self.assertEqual(sorted(wordpress), [".htaccess", "index.php", "wp-admin", "wp-config.php", "wp-content"])
            self.assertEqual(other, ["google123.html", "robots.txt"])

    def test_rewrite_base(self):
        text = "Header set X 1\n# BEGIN WordPress\nRewriteEngine On\nRewriteBase /\nRewriteRule ^index\\.php$ - [L]\nRewriteRule . /index.php [L]\n# END WordPress\nRewriteBase /keep\n"
        fixed = destination.set_rewrite_base(text, "/blog")
        self.assertIn("RewriteBase /blog/\n", fixed)
        self.assertIn("RewriteRule . /blog/index.php [L]", fixed)
        self.assertIn("RewriteBase /keep", fixed)  # outside the WordPress block
        self.assertEqual(destination.set_rewrite_base(fixed, "/"), text)

    def test_address_for_folder(self):
        guess = destination.address_for_folder
        self.assertEqual(guess(Path("/home/u/www/example.com/public_html/new")), "https://example.com/new")
        self.assertEqual(guess(Path("/home/u/www/Example.com/public_html")), "https://example.com")
        self.assertEqual(guess(Path("/home/u/sub.example.co.uk/htdocs/a/b")), "https://sub.example.co.uk/a/b")
        self.assertIsNone(guess(Path("/home/u/public_html/new")))
        self.assertIsNone(guess(Path("/var/www/html")))

    def test_renderer_plain_output(self):
        import io
        from wpmigrate.engine.events import Event, Status
        from wpmigrate.ui import Renderer, Terminal, format_bytes, format_duration
        out = io.StringIO()
        renderer = Renderer(Terminal(out))
        renderer.emit(Event("download", Status.STARTED, "Downloading the site", details={"number": 6, "total": 10}))
        renderer.emit(Event("download", Status.INFO, "Checking the source site..."))
        renderer.emit(Event("download", Status.PROGRESS, "Downloading files: 1 of 2 files", percent=100.0, details={"label": "Downloading files", "bytes_done": 5, "bytes_total": 5}))
        renderer.emit(Event("download", Status.WARNING, "Careful with https://very-long-host-name.example.com/path"))
        renderer.emit(Event("download", Status.FAILED, "It broke.", action_required="Run it again."))
        text = out.getvalue()
        self.assertNotIn("\x1b", text)  # no colours when the output is not a terminal
        self.assertIn("Copying your site", text)
        self.assertNotIn("/10", text)
        self.assertIn("100.0% Downloading files", text)
        self.assertIn("https://very-long-host-name.example.com/path", text)
        self.assertIn("Run it again.", text)
        self.assertEqual(format_bytes(480 * 1048576), "480 MB")
        self.assertEqual(format_duration(125), "2m 05s")

    def test_sizes_and_leftover_backups(self):
        import json
        from base64 import b64encode
        from wpmigrate.engine import sizes
        root = "/srv/site"
        listed = {
            "wp-includes/version.php": 900,
            "wp-content/uploads/2026/photo.jpg": 4_000_000,
            "wp-content/uploads/2026/catalogue.zip": 50_000_000,  # a real download: keep
            "wp-content/plugins/shop/shop.php": 2_000,
            "old-site-backup.zip": 25_000_000,
            "small.zip": 1_000,
            "dump.sql": 300,
            "wp-content/mysql.sql.gz": 700,
            "wp-content/ai1wm-backups/renamed.zip": 9_000,
            "wp-content/ai1wm-backups/index.php": 10,
            "wp-content/backups/a/site.tar": 6_000,
            "wp-content/backups/b.txt": 4,
            "wp-content/backupwordpress-abc123-backups/x.zip": 77,
        }
        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.jsonl"
            lines = [json.dumps({"path": b64encode(f"{root}/{name}".encode()).decode(), "size": size, "type": "file"}) for name, size in listed.items()]
            lines += ["not json", json.dumps({"path": "%%%", "size": 1, "type": "file"}), json.dumps({"path": b64encode(b"/srv/site/dir").decode(), "type": "dir"})]
            index.write_text("\n".join(lines))
            files = sizes.read_file_index(index)
            tables = Path(tmp) / "db-tables.jsonl"
            tables.write_text('{"name":"wp_posts","data_bytes":5000}\n{"name":"wp_options","data_bytes":100}\n')
            database = sizes.read_database_tables(tables)
        self.assertEqual(len(files), len(listed))
        self.assertEqual(sizes.find_site_root(files), root)
        summary = sizes.summarize_files(files, root)
        self.assertEqual(summary["bytes"], sum(listed.values()))
        self.assertEqual(summary["parts"][0][0], "uploads (media)")
        self.assertEqual((database["bytes"], database["count"], database["largest"][0][0]), (5100, 2, "wp_posts"))
        extras = {extra.shown: extra for extra in sizes.find_extras(files, root)}
        self.assertEqual(
            sorted(extras),
            ["dump.sql", "old-site-backup.zip", "wp-content/ai1wm-backups/renamed.zip", "wp-content/backups/", "wp-content/backupwordpress-abc123-backups/", "wp-content/mysql.sql.gz"],
        )
        self.assertEqual(extras["wp-content/backups/"].path, "/srv/site/wp-content/backups")
        self.assertEqual((extras["wp-content/backups/"].size, extras["wp-content/backups/"].files), (6_004, 2))

    def test_redactor(self):
        redact = Redactor()
        redact.add("hunter2")
        text = redact("pwd=hunter2&log=admin Cookie: wordpress_logged_in_1=abc _wpnonce=0123abcdef")
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc", text.split("Cookie:")[1])
        self.assertNotIn("0123abcdef", text)
        self.assertEqual(redact("MII" + "A" * 200), "[redacted-token]")


if __name__ == "__main__":
    unittest.main()


class BackupChoiceTests(unittest.TestCase):
    def test_parse_selection(self):
        from wpmigrate.engine import sizes
        self.assertEqual(sizes.parse_selection("1, 3-4", 5), [0, 2, 3])
        self.assertEqual(sizes.parse_selection("", 5), [])
        self.assertEqual(sizes.parse_selection("2 2", 5), [1])
        self.assertIsNone(sizes.parse_selection("6", 5))
        self.assertIsNone(sizes.parse_selection("0", 5))
        self.assertIsNone(sizes.parse_selection("all", 5))

    def test_plugin_backup_owners(self):
        from wpmigrate.engine import sizes
        root = "/var/www/html"
        files = [sizes.RemoteFile(root + p, 10) for p in (
            "/wp-includes/version.php",
            "/wp-content/ai1wm-backups/index.php",
            "/wp-content/updraft/index.html",
            "/wp-content/backups-dup-lite/index.php",
            "/wp-content/uploads/backwpup-abc-backups/.htaccess",
            "/wp-content/plugins/updraftplus/updraftplus.php",
        )]
        self.assertEqual(sizes.plugin_backup_owners(files, root), ["All-in-One WP Migration", "UpdraftPlus", "Duplicator", "BackWPup"])
