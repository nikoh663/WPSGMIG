"""How big the original site is, and which leftover backup files to skip.

The transfer engine writes a list of the remote files (path and size) and a
list of the database tables before anything is downloaded. This module reads
those two lists. It never touches the files themselves.
"""

from __future__ import annotations

import binascii
import json
import re
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MEGABYTE = 1_048_576

# Leftover folders of backup and migration plugins that the transfer engine
# does not already skip by itself. Everything inside them is a backup.
BACKUP_FOLDERS = (
    "wp-content/backups",
    "wp-content/backup",
    "wp-content/uploads/backups",
    "wp-content/uploads/backup",
    "wp-content/envato-backups",
    "wp-content/backuply",
    "wp-content/managewp/backups",
    "wp-content/webtoffee_migrations",
    "wp-content/uploads/wp-clone",
    "wp-content/uploads/wp-migrate-db",
    "wp-content/uploads/prime-mover-export-files",
    "wp-content/uploads/mainwp/backup",
)
_BACKUP_FOLDER_PATTERNS = (
    re.compile(r"^wp-content/backupwordpress-[^/]+-backups$"),
    re.compile(r"^wp-content/boldgrid_backup[^/]*$"),
    re.compile(r"^wp-content/uploads/jetbackup[^/]*$"),
)
# Folders where backup plugins keep their archives. The engine already skips
# the files each plugin names in its usual way; anything archive-shaped that
# is still listed there is a renamed or imported backup.
PLUGIN_BACKUP_FOLDERS = (
    "wp-content/ai1wm-backups",
    "wp-content/updraft",
    "wp-content/backups-dup-lite",
    "wp-content/backups-dup-pro",
    "wp-content/wpvividbackups",
    "wp-content/backup-db",
    "wp-snapshots",
)
# Whose folder each one is, for telling the person which plugins left backups.
PLUGIN_BACKUP_OWNERS = (
    ("wp-content/ai1wm-backups", "All-in-One WP Migration"),
    ("wp-content/updraft", "UpdraftPlus"),
    ("wp-content/backups-dup-lite", "Duplicator"),
    ("wp-content/backups-dup-pro", "Duplicator"),
    ("wp-snapshots", "Duplicator"),
    ("wp-content/wpvividbackups", "WPvivid"),
    ("wp-content/backup-db", "WP-DBManager"),
    ("wp-content/backup-guard", "Backup Guard"),
    ("wp-content/uploads/backup-guard", "Backup Guard"),
    ("wp-content/uploads/backupbuddy_backups", "BackupBuddy"),
    ("wp-content/uploads/wp-staging", "WP Staging"),
)
_BACKWPUP_FOLDER = re.compile(r"^wp-content/uploads/backwpup[-/]")
_ARCHIVE = re.compile(r"\.(zip|tar|tgz|gz|bz2|xz|7z|rar|wpress|daf|sgbp|wpstg|sql)$", re.I)
_DATABASE_DUMP = re.compile(r"\.sql(\.gz|\.zip|\.bz2)?$", re.I)
# A stray archive next to WordPress itself must be at least this big to count.
LARGE_ARCHIVE_BYTES = 5 * MEGABYTE


@dataclass(frozen=True)
class RemoteFile:
    path: str  # absolute path on the original server
    size: int


@dataclass(frozen=True)
class Extra:
    """Something that looks like a leftover backup."""

    path: str  # absolute remote path to leave out (a file or a whole folder)
    shown: str  # the same path relative to the site, for display
    size: int
    files: int
    reason: str


def read_file_index(index_file: Path) -> list[RemoteFile]:
    """Read the engine's list of remote files.

    Each line is a JSON object. The engine stores the ``path`` value
    base64-encoded so that unusual file names survive; it is only a file
    name, and it is decoded here to plain text and nothing else.
    """
    files: list[RemoteFile] = []
    try:
        handle = open(index_file, encoding="utf-8", errors="replace")
    except OSError:
        return files
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "file":
                continue
            try:
                path = b64decode(str(record.get("path") or ""), validate=True).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                continue
            size = record.get("size")
            if path.startswith("/") and isinstance(size, int) and size >= 0:
                files.append(RemoteFile(path, size))
    return files


def find_site_root(files: Iterable[RemoteFile]) -> str | None:
    """The remote folder WordPress lives in (the one that holds wp-includes)."""
    for entry in files:
        if entry.path.endswith("/wp-includes/version.php"):
            return entry.path[: -len("/wp-includes/version.php")] or "/"
    return None


def _relative(path: str, root: str | None) -> str | None:
    if root is None:
        return None
    prefix = root.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def summarize_files(files: list[RemoteFile], root: str | None) -> dict:
    """Total size and count, plus the biggest parts of the site."""
    parts: dict[str, int] = {}
    for entry in files:
        relative = _relative(entry.path, root)
        if relative is None:
            name = "other folders"
        elif relative.startswith("wp-content/"):
            pieces = relative.split("/")
            name = pieces[1] if len(pieces) > 2 else "other files"
            name = {"uploads": "uploads (media)", "plugins": "plugins", "themes": "themes"}.get(name, "other files")
        else:
            name = "WordPress itself"
        parts[name] = parts.get(name, 0) + entry.size
    return {
        "bytes": sum(entry.size for entry in files),
        "count": len(files),
        "parts": sorted(parts.items(), key=lambda item: -item[1]),
    }


def read_database_tables(tables_file: Path) -> dict:
    """Total data size and the biggest tables, from the engine's table list."""
    tables: list[tuple[str, int]] = []
    try:
        lines = tables_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("name"):
            size = record.get("data_bytes")
            tables.append((str(record["name"]), size if isinstance(size, int) else 0))
    tables.sort(key=lambda item: -item[1])
    return {"bytes": sum(size for _, size in tables), "count": len(tables), "largest": tables[:3]}


def _backup_folder(relative: str) -> str | None:
    """The backup folder a path is inside, if any (as a site-relative path)."""
    for folder in BACKUP_FOLDERS:
        if relative.startswith(folder + "/"):
            return folder
    pieces = relative.split("/")
    for depth in (2, 3):
        candidate = "/".join(pieces[:depth])
        if len(pieces) > depth and any(pattern.match(candidate) for pattern in _BACKUP_FOLDER_PATTERNS):
            return candidate
    return None


def find_extras(files: list[RemoteFile], root: str | None) -> list[Extra]:
    """Leftover backups the engine would still copy, biggest first."""
    if root is None:
        return []
    base = root.rstrip("/") + "/"
    folders: dict[str, list[int]] = {}
    extras: list[Extra] = []
    for entry in files:
        relative = _relative(entry.path, root)
        if relative is None:
            continue
        folder = _backup_folder(relative)
        if folder:
            totals = folders.setdefault(folder, [0, 0])
            totals[0] += entry.size
            totals[1] += 1
            continue
        name = relative.rsplit("/", 1)[-1]
        depth = relative.count("/")
        in_plugin_backup_folder = any(relative.startswith(folder + "/") for folder in PLUGIN_BACKUP_FOLDERS)
        if in_plugin_backup_folder and _ARCHIVE.search(name):
            extras.append(Extra(entry.path, relative, entry.size, 1, "a backup archive"))
        elif depth == 0 or (depth == 1 and relative.startswith("wp-content/")):
            # Next to WordPress itself, or directly inside wp-content.
            if _DATABASE_DUMP.search(name):
                extras.append(Extra(entry.path, relative, entry.size, 1, "a database dump"))
            elif _ARCHIVE.search(name) and entry.size >= LARGE_ARCHIVE_BYTES:
                extras.append(Extra(entry.path, relative, entry.size, 1, "a large archive"))
    for folder, (size, count) in folders.items():
        extras.append(Extra(base + folder, folder + "/", size, count, "a backup folder"))
    return sorted(extras, key=lambda extra: -extra.size)


def plugin_backup_owners(files: list[RemoteFile], root: str | None) -> list[str]:
    """Names of the backup plugins that have a folder on the original site.

    The archives those plugins make are not in the list of remote files, but
    the small files that sit next to them (such as ``index.php``) are, and
    that is enough to tell that the folder exists.
    """
    found: list[str] = []
    for entry in files:
        relative = _relative(entry.path, root)
        if relative is None:
            continue
        owner = next((name for folder, name in PLUGIN_BACKUP_OWNERS if relative.startswith(folder + "/")), None)
        if owner is None and _BACKWPUP_FOLDER.match(relative):
            owner = "BackWPup"
        if owner and owner not in found:
            found.append(owner)
    return found


def parse_selection(text: str, count: int) -> list[int] | None:
    """Turn an answer such as ``1, 3-4`` into zero-based positions.

    Returns ``None`` when the answer cannot be understood. An empty answer
    selects nothing.
    """
    chosen: set[int] = set()
    for piece in re.split(r"[,\s]+", text.strip()):
        if not piece:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", piece)
        if not match:
            return None
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last > count or first > last:
            return None
        chosen.update(range(first - 1, last))
    return sorted(chosen)
