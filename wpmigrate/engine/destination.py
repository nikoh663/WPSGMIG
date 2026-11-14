"""Inspecting and preparing the destination folder and database."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from wpmigrate.engine.system import System

# Entries that make up a WordPress install in its root folder.
_WORDPRESS_NAMES = {"wp-admin", "wp-includes", "wp-content", "index.php", "license.txt", "readme.html", "xmlrpc.php", ".htaccess", "licens.html", "liesmich.html"}
_WORDPRESS_PATTERN = re.compile(r"^wp-[a-z0-9_.-]+\.php$")


def is_wordpress_entry(name: str) -> bool:
    """Whether a top-level name belongs to a WordPress install."""
    return name in _WORDPRESS_NAMES or bool(_WORDPRESS_PATTERN.match(name))


def has_wordpress(folder: Path) -> bool:
    return (folder / "wp-config.php").is_file() and (folder / "wp-includes").is_dir() or (
        (folder / "wp-includes").is_dir() and (folder / "wp-admin").is_dir()
    )


def list_entries(folder: Path, *, exclude: Sequence[Path] = ()) -> list[str]:
    """Top-level names in ``folder`` (sorted), skipping excluded paths."""
    if not folder.is_dir():
        return []
    excluded = {str(path.resolve()) for path in exclude}
    names = []
    for name in sorted(os.listdir(folder)):
        if str((folder / name).resolve()) in excluded:
            continue
        names.append(name)
    return names


def split_entries(folder: Path, *, exclude: Sequence[Path] = ()) -> tuple[list[str], list[str]]:
    """(WordPress entries, other entries) in ``folder``."""
    wordpress, other = [], []
    for name in list_entries(folder, exclude=exclude):
        (wordpress if is_wordpress_entry(name) else other).append(name)
    return wordpress, other


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def site_domain(site_url: str) -> str:
    """``https://www.example.com/blog`` -> ``example.com``."""
    host = (urllib.parse.urlsplit(site_url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def old_site_folder_guess(chosen: Path, domain: str) -> Path:
    """``<chosen>/../../old.<domain>/public_html``, resolved."""
    return Path(os.path.normpath(chosen / ".." / ".." / f"old.{domain}" / "public_html"))


def human_size(byte_count: int) -> str:
    value = float(byte_count)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{byte_count} bytes"


# -- MySQL checks via PHP (no Python MySQL driver in the standard library) -----

_DB_PROBE = r"""
$c = json_decode(stream_get_contents(STDIN), true);
mysqli_report(MYSQLI_REPORT_OFF);
$host = $c['host']; $port = null; $socket = null;
if (preg_match('/^(.*):(\/.*)$/', $host, $m)) { $host = $m[1]; $socket = $m[2]; }
elseif (preg_match('/^(.*):(\d+)$/', $host, $m)) { $host = $m[1]; $port = (int) $m[2]; }
$link = @mysqli_connect($host, $c['user'], $c['password'], $c['name'], $port, $socket);
if (!$link) { echo json_encode(array('ok' => false, 'errno' => mysqli_connect_errno(), 'error' => mysqli_connect_error())); exit(0); }
$like = mysqli_real_escape_string($link, addcslashes($c['prefix'], '\\_%')) . '%';
$tables = array();
$result = mysqli_query($link, "SHOW TABLES LIKE '" . $like . "'");
if ($result) { while ($row = mysqli_fetch_row($result)) { $tables[] = $row[0]; } }
$all = 0;
$result = mysqli_query($link, 'SHOW TABLES');
if ($result) { $all = mysqli_num_rows($result); }
echo json_encode(array('ok' => true, 'version' => mysqli_get_server_info($link), 'prefix_tables' => $tables, 'table_count' => $all));
"""


@dataclass(frozen=True)
class DatabaseCheck:
    ok: bool
    error: str = ""
    errno: int = 0
    version: str = ""
    prefix_tables: tuple[str, ...] = ()
    table_count: int = 0


def check_database(system: System, php: str, *, host: str, name: str, user: str, password: str, prefix: str) -> DatabaseCheck:
    """Connect with the given credentials and list tables that would be replaced."""
    payload = json.dumps({"host": host, "name": name, "user": user, "password": password, "prefix": prefix})
    result = system.run([php, "-d", "display_errors=stderr", "-r", _DB_PROBE], stdin_text=payload, timeout=60)
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        return DatabaseCheck(False, error=(result.stderr.strip() or "no answer from the database check")[:300])
    data = json.loads(lines[-1])
    if not data.get("ok"):
        return DatabaseCheck(False, error=str(data.get("error", "")), errno=int(data.get("errno") or 0))
    return DatabaseCheck(True, version=str(data.get("version", "")), prefix_tables=tuple(data.get("prefix_tables", [])), table_count=int(data.get("table_count", 0)))


def explain_database_error(check: DatabaseCheck) -> str:
    """Plain-language reason for a failed connection."""
    if check.errno == 1045:
        return "The database user name or password was not accepted."
    if check.errno == 1044 or check.errno == 1049:
        return "The database does not exist, or this user has no access to it."
    if check.errno in (2002, 2003, 2005):
        return "The database server could not be reached at that host name."
    return f"The database refused the connection ({check.error})."


def normalize_db_host(host: str) -> str:
    host = host.strip().lower()
    if host in ("localhost", "127.0.0.1", "::1", "localhost:3306", "127.0.0.1:3306"):
        return "localhost"
    return host.removesuffix(":3306")


def split_host_port(host: str) -> tuple[str, str | None]:
    """``db:3307`` -> ``("db", "3307")``; sockets stay in the host."""
    match = re.match(r"^(.*):(\d+)$", host)
    if match and not host.startswith("["):
        return match.group(1), match.group(2)
    return host, None


_WP_BLOCK = re.compile(r"(# BEGIN WordPress\b.*?# END WordPress)", re.S)


def set_rewrite_base(htaccess: str, url_path: str) -> str:
    """Point the standard WordPress rewrite rules at url_path ("/" or "/blog/").

    Only the "# BEGIN WordPress" ... "# END WordPress" block is changed, and
    only its RewriteBase line and its final rule to index.php.
    """
    base = "/" + url_path.strip("/") + "/" if url_path.strip("/") else "/"

    def fix(match: re.Match) -> str:
        block = re.sub(r"(?m)^(\s*RewriteBase\s+)\S+", lambda m: m.group(1) + base, match.group(1))
        return re.sub(r"(?m)^(\s*RewriteRule\s+\.\s+)\S*index\.php(\s+\[L\])", lambda m: m.group(1) + base + "index.php" + m.group(2), block)

    return _WP_BLOCK.sub(fix, htaccess)
