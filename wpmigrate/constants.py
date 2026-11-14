"""Fixed values shared by the engine and the front ends."""

from __future__ import annotations

# A current stable desktop Chrome user agent. Override with --user-agent.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

# Headers a browser sends with a top-level page load. Accept-Encoding lists
# only the encodings the Python standard library can decode.
BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8"
)
BROWSER_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
BROWSER_ACCEPT_ENCODING = "gzip, deflate"

REPRINT_PHAR_URL = "https://plugins.svn.wordpress.org/reprint/downloads/reprint.phar"
WP_CLI_PHAR_URL = "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar"

# Plugin directory slug and the admin page the plugin registers under Tools.
REPRINT_PLUGIN_SLUG = "reprint"
REPRINT_ADMIN_PAGE = "tools.php?page=reprint-server"

# PHP versions for running Reprint.
PHP_PREFERRED = (8, 2)
PHP_MINIMUM = (7, 4)
PHP_REQUIRED_EXTENSIONS = ("curl", "hash", "json", "mbstring", "openssl", "PDO", "zlib", "mysqli", "Phar")

# Disk space we want free before downloading anything (bytes).
MIN_FREE_BYTES = 512 * 1024 * 1024

# Behaviour toward the source site.
ADMIN_REQUEST_DELAY_SECONDS = 1.0
MAX_HTTP_ATTEMPTS = 4
MAX_RETRY_AFTER_SECONDS = 60
MAX_LOGIN_ATTEMPTS = 3

# Reprint exit codes, from `reprint.phar --help` and the progress reference.
REPRINT_EXIT_OK = 0
REPRINT_EXIT_FAILED = 1
REPRINT_EXIT_UNFINISHED = 2
REPRINT_EXIT_TEMPORARY = 3
REPRINT_EXIT_AUTH_NEEDED = 4

# How often we restart an unfinished Reprint command, and how long we wait
# after a temporary failure, before giving up for this run.
MAX_UNFINISHED_RESTARTS = 200
TEMPORARY_FAILURE_WAITS = (30, 60, 120)


class ExitCode:
    """Process exit codes of wpmigrate itself."""

    SUCCESS = 0
    GENERAL_ERROR = 1
    LOGIN_FAILED = 10
    AUTHORIZATION_NEEDED = 11
    TRANSFER_FAILED = 12
    DESTINATION_FAILED = 13
    CANCELLED = 14
    REQUIREMENTS_FAILED = 15
    ALREADY_RUNNING = 16
