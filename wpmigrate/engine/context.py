"""Everything a stage needs, passed explicitly (no global state)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from wpmigrate import constants
from wpmigrate.engine.events import Event, EventSink, Status
from wpmigrate.engine.prompts import Prompter
from wpmigrate.engine.state import SitePaths, StateStore
from wpmigrate.engine.system import LocalSystem
from wpmigrate.redact import Redactor


class Secrets:
    """Secrets held in memory only. ``repr`` never shows them."""

    def __init__(self, admin_password: str | None = None, db_password: str | None = None) -> None:
        self.admin_password = admin_password
        self.db_password = db_password

    def __repr__(self) -> str:
        return "Secrets(<hidden>)"


@dataclass
class Options:
    """Choices given on the command line or in environment variables.

    Each one, when set, answers the matching question so that an unattended
    run never has to ask. ``None`` means "ask the person".
    """

    source_url: str | None = None
    admin_user: str | None = None
    new_site_url: str | None = None
    php_binary: str | None = None
    user_agent: str = constants.USER_AGENT
    allow_http: bool = False
    reprint_phar: str | None = None
    manual_source_steps: bool = False
    start_dir: Path = field(default_factory=Path.cwd)
    keep_downloads: bool = False
    file_layout: str = "move"
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Context:
    """Per-run objects shared by the stages."""

    system: LocalSystem
    prompter: Prompter
    sink: EventSink
    options: Options
    secrets: Secrets
    redactor: Redactor
    paths: SitePaths | None = None
    state: StateStore | None = None
    log_lines: list[str] = field(default_factory=list)
    current_stage: str = ""
    lock: object | None = None

    def emit(self, status: str, message: str, *, stage: str | None = None, percent: float | None = None, action_required: str | None = None, **details: object) -> None:
        event = Event(stage or self.current_stage, status, self.redactor(message), percent, self.redactor(action_required) if action_required else None, dict(details))
        self.log(f"[{event.stage}] {event.status}: {message}" + (f" | {action_required}" if action_required else ""))
        self.sink.emit(event)

    def info(self, message: str, **details: object) -> None:
        self.emit(Status.INFO, message, **details)

    def warn(self, message: str, **details: object) -> None:
        self.emit(Status.WARNING, message, **details)

    def log(self, message: str) -> None:
        """Append a redacted line to the private log file."""
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + self.redactor(message)
        if self.paths is None:
            self.log_lines.append(line)
            return
        try:
            with open(self.paths.log_file, "a", encoding="utf-8") as handle:
                for pending in self.log_lines:
                    handle.write(pending + "\n")
                self.log_lines.clear()
                handle.write(line + "\n")
        except OSError:
            self.log_lines.append(line)

    def emit_event(self, event: Event) -> None:
        """Forward an event built elsewhere (for example by the Reprint runner)."""
        self.log(f"[{event.stage}] {event.status}: {event.message}")
        self.sink.emit(Event(event.stage, event.status, self.redactor(event.message), event.percent, event.action_required, event.details))

    @property
    def st(self) -> StateStore:
        assert self.state is not None, "state is opened in the collect stage"
        return self.state

    @property
    def p(self) -> SitePaths:
        assert self.paths is not None, "paths are set in the collect stage"
        return self.paths
