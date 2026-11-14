"""Structured events the engine emits, and the sink front ends implement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class Status:
    """Values of :attr:`Event.status`."""

    STARTED = "started"
    PROGRESS = "progress"
    INFO = "info"
    WARNING = "warning"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    ACTION_REQUIRED = "action_required"


@dataclass(frozen=True)
class Event:
    """One thing that happened inside a stage.

    ``action_required`` is a plain-language instruction for the person when
    they must do something outside the tool (for example in the hosting
    panel); ``details`` carries machine-readable extras.
    """

    stage: str
    status: str
    message: str
    percent: float | None = None
    action_required: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable form."""
        return {
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "percent": self.percent,
            "action_required": self.action_required,
            "details": self.details,
        }


class EventSink(Protocol):
    """Receives events. Implementations must not block for long."""

    def emit(self, event: Event) -> None:  # pragma: no cover - protocol
        ...


class EventList:
    """An :class:`EventSink` that only collects events (used in tests)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
