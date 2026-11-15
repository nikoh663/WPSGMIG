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


# Things a hosting account puts in a web folder by itself. They do not get in
# WordPress's way (and .well-known is needed for SSL certificates), so they
# are left where they are without asking.
_HOST_NAMES = {".well-known", "cgi-bin", ".ftpquota", "default.html", "default.htm"}


def is_host_entry(name: str) -> bool:
    return name.lower() in _HOST_NAMES


def shadows_wordpress(name: str) -> bool:
    """A page that web servers usually show instead of WordPress's index.php."""
    return name.lower() in ("index.html", "index.htm")


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
    """Folder of the subdomain old.<domain>, next to the domain's own folder.

    With the layout ``<base>/<domain>/<web root>[/sub/folder]`` this is
    ``<base>/old.<domain>/<web root>``, also when ``chosen`` is a subfolder.
    Without a recognisable layout it falls back to two levels up.
    """
    parts = Path(os.path.normpath(chosen)).parts
    for index in range(len(parts) - 1, 1, -1):
        if parts[index] in WEB_ROOT_NAMES and _DOMAIN_NAME.match(parts[index - 1]):
            return Path(*parts[: index - 1]) / f"old.{domain}" / parts[index]
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


# Folder names that web servers commonly use as a site's web root.
WEB_ROOT_NAMES = ("public_html", "htdocs", "httpdocs")
_DOMAIN_NAME = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$", re.I)


def address_for_folder(folder: Path) -> str | None:
    """Guess the web address a folder is served at, from a <domain>/<web root>/... path.

    ``/home/u/www/example.com/public_html/new`` gives ``https://example.com/new``.
    Returns None when the path does not follow that layout.
    """
    parts = Path(os.path.normpath(folder)).parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index] in WEB_ROOT_NAMES and _DOMAIN_NAME.match(parts[index - 1]):
            below = "/".join(urllib.parse.quote(part) for part in parts[index + 1:])
            return "https://" + parts[index - 1].lower() + ("/" + below if below else "")
    return None


# A stand-in address used while moving a site to a subfolder of its own domain.
# The ".invalid" top-level domain is reserved, so it can never be a real site.
TEMPORARY_ADDRESS = "https://wpmigrate-new-address.invalid"


def address_rewrite_steps(source_api_url: str, new_url: str) -> list[list[str]]:
    """The URL replacements for the database, as lists of --rewrite-url arguments.

    Normally this is one step from the old origin (http and https) to the new
    address. When the new address still contains the old one, for example
    https://example.com to https://example.com/new, the transfer engine
    rewrites some links twice inside post content and gives
    https://example.com/new/new/page/. Going through a temporary address
    first avoids that, because neither step's target then contains its source.
    """
    parts = urllib.parse.urlsplit(source_api_url)
    origins = [f"https://{parts.netloc}", f"http://{parts.netloc}"]
    new_url = new_url.rstrip("/")
    if not any(origin.lower() in new_url.lower() for origin in origins) and f"//{parts.netloc.lower()}" not in new_url.lower():
        return [[arg for origin in origins for arg in ("--rewrite-url", origin, new_url)]]
    return [
        [arg for origin in origins for arg in ("--rewrite-url", origin, TEMPORARY_ADDRESS)],
        ["--rewrite-url", TEMPORARY_ADDRESS, new_url],
    ]


def temporary_address_replacements(new_url: str) -> list[tuple[str, str]]:
    """Plain search and replace pairs that turn the temporary address into the new one.

    The order matters: full addresses first, then the JSON-escaped form, then
    the bare host name for anything else (protocol-relative or encoded links).
    """
    new_url = new_url.rstrip("/")
    parts = urllib.parse.urlsplit(new_url)
    temporary_host = urllib.parse.urlsplit(TEMPORARY_ADDRESS).netloc
    return [
        (TEMPORARY_ADDRESS, new_url),
        (TEMPORARY_ADDRESS.replace("/", "\\/"), new_url.replace("/", "\\/")),
        (temporary_host, parts.netloc + parts.path),
    ]


# WP-CLI's search-replace leaves tables without a primary key alone. This
# fixed snippet (run with "wp eval-file" from the private folder, never
# built from downloaded content)
# does the same replacement there: each distinct value that contains the
# temporary address is rewritten, keeping serialized data valid, and written
# back with an exact-match UPDATE. The pairs arrive as a JSON argument.
NO_PRIMARY_KEY_REPLACE = r"""
global $wpdb;
$pairs = json_decode($args[0], true);
$needle = $args[1];
$fix = function ($value) use (&$fix, $pairs) {
    if (is_string($value)) {
        $data = @unserialize($value, array('allowed_classes' => false));
        if ($data !== false || $value === 'b:0;') {
            return serialize($fix($data));
        }
        foreach ($pairs as $pair) { $value = str_replace($pair[0], $pair[1], $value); }
        return $value;
    }
    if (is_array($value)) {
        $out = array();
        foreach ($value as $key => $item) { $out[$key] = $fix($item); }
        return $out;
    }
    if (is_object($value)) {
        foreach (get_object_vars($value) as $key => $item) { $value->$key = $fix($item); }
    }
    return $value;
};
$changed = 0;
foreach ($wpdb->get_col('SHOW TABLES') as $table) {
    $columns = $wpdb->get_results("SHOW COLUMNS FROM `$table`");
    $has_key = false;
    foreach ($columns as $column) { if ($column->Key === 'PRI') { $has_key = true; } }
    if ($has_key) { continue; }
    foreach ($columns as $column) {
        if (!preg_match('/char|text|blob|json/i', $column->Type)) { continue; }
        $name = $column->Field;
        $values = $wpdb->get_col($wpdb->prepare("SELECT DISTINCT `$name` FROM `$table` WHERE `$name` LIKE %s", '%' . $wpdb->esc_like($needle) . '%'));
        foreach ($values as $old) {
            $new = $fix($old);
            if ($new !== $old) {
                $changed += (int) $wpdb->query($wpdb->prepare("UPDATE `$table` SET `$name` = %s WHERE BINARY `$name` = %s", $new, $old));
            }
        }
    }
}
echo "changed $changed\n";
"""


def human_duration(seconds: float) -> str:
    """A short duration such as ``45s``, ``2m 28s`` or ``1h 5m``."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def timing_summary(timings: dict[str, float]) -> str | None:
    """The steps that took a noticeable time, longest first."""
    steps = sorted(((spent, label) for label, spent in timings.items() if spent >= 5), reverse=True)
    if not steps:
        return None
    return "Where the time went: " + ", ".join(f"{label} {human_duration(spent)}" for spent, label in steps) + "."
