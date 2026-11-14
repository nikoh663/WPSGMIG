"""The JSON state file that makes every stage resumable.

Layout of the private directory (one per source site)::

    ~/.wpmigrate/sites/<source-host>/          0700
        state.json        this module            0600
        run.lock          held while a run is active
        wpmigrate.log     redacted log
        cookies.txt       source session cookies   0600
        connection-token  only for sources without OpenSSL   0600
        reprint-state/    Reprint's --state-dir (key, progress, db.sql)
        download/         Reprint's --fs-root (downloaded files)
        assemble/         Reprint's flat-docroot output (links)
        runtime/          apply-runtime output
        backups/          dated backups of files moved out of the way
        tools/            reprint.phar, wp-cli.phar

No password, token, cookie or nonce is ever written to ``state.json``.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wpmigrate.engine.errors import AlreadyRunning, MigrationError
from wpmigrate.engine.system import LocalSystem

STATE_VERSION = 1

STAGE_PENDING = "pending"
STAGE_DONE = "done"

# Keys that must never be persisted. Checked on every save as a safety net.
FORBIDDEN_KEYS = frozenset({"password", "admin_password", "db_password", "secret", "token", "cookie", "nonce"})


def site_slug(source_url: str) -> str:
    """Directory name for a source address: host plus path, filesystem-safe."""
    without_scheme = re.sub(r"^[a-z]+://", "", source_url.strip().lower())
    cleaned = re.sub(r"[^a-z0-9.\-]+", "_", without_scheme).strip("._-")
    return cleaned or "site"


@dataclass(frozen=True)
class SitePaths:
    """All paths inside one site's private directory."""

    root: Path

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "run.lock"

    @property
    def log_file(self) -> Path:
        return self.root / "wpmigrate.log"

    @property
    def cookie_file(self) -> Path:
        return self.root / "cookies.txt"

    @property
    def connection_token_file(self) -> Path:
        return self.root / "connection-token"

    @property
    def reprint_state(self) -> Path:
        return self.root / "reprint-state"

    @property
    def download(self) -> Path:
        return self.root / "download"

    @property
    def assemble(self) -> Path:
        return self.root / "assemble"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    @property
    def sql_dump(self) -> Path:
        return self.reprint_state / "db.sql"

    def create(self) -> None:
        """Create the private directories with owner-only permissions."""
        for directory in (self.root, self.reprint_state, self.download, self.backups, self.tools, self.runtime):
            LocalSystem.make_private_dir(directory)


def _check_no_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ValueError(f"refusing to store secret-looking key {path}{key}")
            _check_no_secrets(inner, f"{path}{key}.")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            _check_no_secrets(inner, f"{path}{index}.")


class StateStore:
    """Reads and writes ``state.json`` atomically."""

    def __init__(self, paths: SitePaths) -> None:
        self.paths = paths
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.paths.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": STATE_VERSION, "stages": {}, "data": {}, "destructive": {}, "created_at": time.time()}
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise MigrationError(
                f"The saved progress file {self.paths.state_file} is damaged ({error.msg}).",
                "Move that file somewhere else and run the tool again to start this site from the beginning.",
            ) from error
        loaded.setdefault("stages", {})
        loaded.setdefault("data", {})
        loaded.setdefault("destructive", {})
        return loaded

    def save(self) -> None:
        """Persist the current state."""
        _check_no_secrets(self.data)
        self.data["updated_at"] = time.time()
        LocalSystem.write_private_file(self.paths.state_file, json.dumps(self.data, indent=2, sort_keys=True))

    # -- stages -------------------------------------------------------------

    def stage_done(self, name: str) -> bool:
        return self.data["stages"].get(name, {}).get("status") == STAGE_DONE

    def mark_stage(self, name: str, status: str) -> None:
        entry = self.data["stages"].setdefault(name, {})
        entry["status"] = status
        entry["at"] = time.time()
        self.save()

    def reset_stage(self, name: str) -> None:
        self.data["stages"].pop(name, None)
        self.save()

    # -- values ---------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self.data["data"].get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.data["data"][key] = value
        self.save()

    def update(self, values: dict[str, Any]) -> None:
        self.data["data"].update(values)
        self.save()

    # -- destructive steps --------------------------------------------------

    def destructive_done(self, name: str) -> bool:
        return bool(self.data["destructive"].get(name, {}).get("done"))

    def record_destructive(self, name: str, **details: Any) -> None:
        """Record that a destructive step finished, so a rerun never repeats it."""
        self.data["destructive"][name] = {"done": True, "at": time.time(), **details}
        self.save()

    def record_destructive_started(self, name: str, **details: Any) -> None:
        """Record that a destructive step began (to detect a half-finished one)."""
        self.data["destructive"][name] = {"done": False, "started_at": time.time(), **details}
        self.save()

    def destructive_started(self, name: str) -> bool:
        entry = self.data["destructive"].get(name)
        return bool(entry) and not entry.get("done")


class RunLock:
    """An exclusive lock on one site's private directory."""

    def __init__(self, paths: SitePaths) -> None:
        self.paths = paths
        self._handle: int | None = None

    def acquire(self) -> None:
        descriptor = os.open(self.paths.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EAGAIN, errno.EACCES):
                holder = ""
                try:
                    holder = self.paths.lock_file.read_text().strip()
                except OSError:
                    pass
                raise AlreadyRunning(
                    "Another copy of this tool is already working on this site" + (f" (process {holder})." if holder else "."),
                    "Wait for it to finish, or close that session, then run the tool again.",
                ) from error
            raise
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode())
        self._handle = descriptor

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            finally:
                os.close(self._handle)
                self._handle = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
