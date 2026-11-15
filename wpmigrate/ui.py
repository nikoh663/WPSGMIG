"""Terminal presentation: colours, live progress bars and readable text.

Everything here is display only. On a terminal it draws colours, a spinner
and progress bars; when the output is piped or the terminal has gone away it
falls back to plain lines.
"""

from __future__ import annotations

import os
import re
import shutil
import textwrap
import threading
import time
from collections import deque
from typing import Callable, TextIO

from wpmigrate.engine.events import Event, Status

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CODES = {
    "reset": "0", "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "white": "97", "grey": "90",
}
_UNICODE = {"ok": "✓", "phase": "◆", "fail": "✗", "warn": "!", "ask": "?", "prompt": "›", "bullet": "•", "bar": "━", "dot": "·", "arrow": "→",
            "frames": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
_ASCII = {"ok": "+", "phase": "*", "fail": "x", "warn": "!", "ask": "?", "prompt": ">", "bullet": "-", "bar": "#", "dot": "-", "arrow": ">",
          "frames": "|/-\\", "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}


# The engine's ten stages, shown as five plainly named phases.
PHASES = {
    "collect": "First, a few questions",
    "requirements": "Getting connected",
    "login": "Getting connected",
    "plugin": "Getting connected",
    "authorize": "Getting connected",
    "download": "Copying your site",
    "destination": "Setting up its new home",
    "deploy": "Putting everything in place",
    "cleanup_source": "Finishing up",
    "summary": "Finishing up",
}


def visible_length(text: str) -> int:
    return len(_ANSI.sub("", text))


def format_bytes(count: float) -> str:
    megabytes = count / 1_048_576
    if megabytes >= 1024:
        return f"{megabytes / 1024:,.2f} GB"
    if megabytes >= 10:
        return f"{megabytes:,.0f} MB"
    return f"{megabytes:,.1f} MB"


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


class Terminal:
    """Output that survives a dropped SSH session (writes then go nowhere)."""

    def __init__(self, stream: TextIO, *, environ: dict[str, str] | None = None) -> None:
        environ = os.environ if environ is None else environ
        self.stream = stream
        self.detached = False
        self.is_tty = stream.isatty()
        plain_terminal = environ.get("TERM", "") in ("", "dumb")
        self.color = self.is_tty and not plain_terminal and not environ.get("NO_COLOR")
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        self.symbols = _UNICODE if encoding.startswith("utf") else _ASCII
        self._lock = threading.RLock()
        self._live: Callable[[], str] | None = None
        self._live_drawn = ""
        self._ticker: threading.Thread | None = None

    # -- sizes and styles -------------------------------------------------

    @property
    def columns(self) -> int:
        return max(40, shutil.get_terminal_size((80, 24)).columns)

    @property
    def width(self) -> int:
        """Width used for wrapped text."""
        return min(self.columns - 2, 96)

    def style(self, text: str, *names: str) -> str:
        if not self.color or not names:
            return text
        return "\x1b[" + ";".join(_CODES[name] for name in names) + "m" + text + "\x1b[0m"

    def wrap(self, text: str, *, first: str = "", rest: str = "") -> list[str]:
        """Wrap text without ever breaking inside an address or a path."""
        lines: list[str] = []
        for number, paragraph in enumerate(text.splitlines() or [""]):
            wrapped = textwrap.wrap(
                paragraph, self.width, initial_indent=first if number == 0 else rest, subsequent_indent=rest,
                break_long_words=False, break_on_hyphens=False,
            )
            lines.extend(wrapped or [first if number == 0 else rest])
        return lines

    # -- writing ----------------------------------------------------------

    def _raw(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            self.detached = True

    def _erase_live(self) -> None:
        if self._live_drawn:
            self._raw("\r\x1b[2K" if self.color else "\r" + " " * visible_length(self._live_drawn) + "\r")
            self._live_drawn = ""

    def write(self, text: str = "") -> None:
        """Print whole lines above the live line, if there is one."""
        if self.detached:
            return
        with self._lock:
            self._erase_live()
            self._raw(text + "\n")
            self._draw_live()

    def _draw_live(self) -> None:
        if self._live is None or self.detached or not self.is_tty:
            return
        line = self._live()
        limit = self.columns - 1
        if visible_length(line) > limit:
            line = _ANSI.sub("", line)[: limit - 1] + "…"
        if line != self._live_drawn:
            self._erase_live()
            self._raw(line)
            self._live_drawn = line

    def set_live(self, render: Callable[[], str]) -> None:
        """Keep one line at the bottom up to date (spinner or progress bar)."""
        with self._lock:
            self._erase_live()
            self._live = render
            self._draw_live()
        if self.is_tty and (self._ticker is None or not self._ticker.is_alive()):
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()

    def clear_live(self) -> None:
        with self._lock:
            self._erase_live()
            self._live = None

    def _tick(self) -> None:
        while True:
            time.sleep(0.12)
            with self._lock:
                if self._live is None or self.detached:
                    return
                self._draw_live()


class _Bar:
    """State of one progress bar."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.started = time.monotonic()
        self.percent: float | None = None
        self.details: dict = {}
        self.samples: deque[tuple[float, float]] = deque(maxlen=8)

    def update(self, event: Event) -> None:
        self.percent = event.percent
        self.details = event.details
        done = event.details.get("bytes_done")
        if isinstance(done, (int, float)):
            self.samples.append((time.monotonic(), float(done)))

    def speed(self) -> float | None:
        if len(self.samples) < 2:
            return None
        (first_time, first_bytes), (last_time, last_bytes) = self.samples[0], self.samples[-1]
        if last_time - first_time < 1.0 or last_bytes <= first_bytes:
            return None
        return (last_bytes - first_bytes) / (last_time - first_time)


class Renderer:
    """Turns engine events into what the person sees."""

    def __init__(self, terminal: Terminal) -> None:
        self.t = terminal
        self.started = time.monotonic()
        self._spinner: tuple[str, float] | None = None
        self._bar: _Bar | None = None
        self._last_plain_progress = 0.0
        self._phase = ""

    # -- small helpers ----------------------------------------------------

    def _mark(self, kind: str) -> str:
        colour = {"ok": "green", "fail": "red", "warn": "yellow"}[kind]
        return self.t.style(self.t.symbols[kind], colour, "bold")

    def banner(self, title: str, subtitle: str) -> None:
        t, s = self.t, self.t.symbols
        inner = max(len(title), len(subtitle)) + 4
        t.write("")
        t.write("  " + t.style(s["tl"] + s["h"] * inner + s["tr"], "cyan"))
        for text, styles in ((title, ("bold", "white")), (subtitle, ("dim",))):
            t.write("  " + t.style(s["v"], "cyan") + "  " + t.style(text, *styles) + " " * (inner - len(text) - 2) + t.style(s["v"], "cyan"))
        t.write("  " + t.style(s["bl"] + s["h"] * inner + s["br"], "cyan"))

    def hint(self, text: str) -> None:
        indent = " " * (len(text) - len(text.lstrip()) + 2)
        for line in self.t.wrap(text.strip(), first=indent, rest=indent):
            self.t.write(self.t.style(line, "dim"))

    def line(self, kind: str, text: str, *styles: str) -> None:
        """One marked line: a tick, a cross or a warning sign, then the text."""
        if kind == "warn" and not styles:
            styles = ("yellow",)
        lines = self.t.wrap(text, first="    ", rest="    ")
        self.t.write("  " + self._mark(kind) + " " + self.t.style(lines[0][4:], *styles))
        for line in lines[1:]:
            self.t.write(self.t.style(line, *styles))

    # -- live line --------------------------------------------------------

    def _spinner_line(self) -> str:
        assert self._spinner is not None
        text, started = self._spinner
        frames = self.t.symbols["frames"]
        frame = frames[int(time.monotonic() * 10) % len(frames)]
        elapsed = time.monotonic() - started
        tail = self.t.style(f"  {format_duration(elapsed)}", "dim") if elapsed >= 3 else ""
        return "  " + self.t.style(frame, "cyan") + " " + text + tail

    def _bar_line(self) -> str:
        bar = self._bar
        assert bar is not None
        t, s, details = self.t, self.t.symbols, bar.details
        percent = bar.percent
        pieces: list[str] = []
        done, total = details.get("bytes_done"), details.get("bytes_total")
        if isinstance(done, (int, float)):
            pieces.append(f"{format_bytes(done)} of {format_bytes(total)}" if total else format_bytes(done))
        speed = bar.speed()
        if speed:
            pieces.append(f"{format_bytes(speed)}/s")
            if total and done is not None and total > done:
                pieces.append(f"{format_duration((total - done) / speed)} left")
        items_done, items_total = details.get("items_done"), details.get("items_total")
        if isinstance(items_done, (int, float)):
            unit = {"statements": "queries"}.get(details.get("unit"), details.get("unit") or "items")
            pieces.append(f"{items_done:,} of {items_total:,} {unit}" if items_total else f"{items_done:,} {unit}")
        head = "  " + t.style(s["frames"][int(time.monotonic() * 10) % len(s["frames"])], "cyan") + " " + bar.label + "  "
        number = f" {percent:3.0f}%" if percent is not None else ""
        room = t.columns - 1 - visible_length(head) - len(number)
        tail = ""
        for piece in pieces:
            candidate = tail + f"  {s['dot']} {piece}" if tail else f"  {piece}"
            if room - len(candidate) < 20:
                break
            tail = candidate
        size = max(8, min(30, room - len(tail)))
        if percent is None:
            position = int(time.monotonic() * 8) % size
            drawn = t.style(s["bar"] * position, "dim") + t.style(s["bar"] * 3, "cyan") + t.style(s["bar"] * max(0, size - position - 3), "dim")
        else:
            filled = int(size * percent / 100)
            empty = s["bar"] if t.color else s["h"]
            drawn = t.style(s["bar"] * filled, "green") + t.style(empty * (size - filled), "dim")
        return head + drawn + t.style(number, "bold") + t.style(tail, "dim")

    def end_live(self, *, failed: bool = False) -> None:
        """Finish the spinner or bar: a finished bar leaves a one-line result."""
        bar, spinner = self._bar, self._spinner
        self._bar = self._spinner = None
        self.t.clear_live()
        if not self.t.is_tty:
            return
        if failed and (bar or spinner):
            self.line("fail", bar.label if bar else spinner[0])
        elif bar and (bar.percent or 0) >= 99.9:
            details, facts = bar.details, []
            if isinstance(details.get("bytes_done"), (int, float)):
                facts.append(format_bytes(details["bytes_done"]))
            if isinstance(details.get("items_done"), (int, float)):
                unit = {"statements": "queries"}.get(details.get("unit"), details.get("unit") or "items")
                facts.append(f"{details['items_done']:,} {unit}")
            facts.append(format_duration(time.monotonic() - bar.started))
            self.t.write("  " + self._mark("ok") + " " + bar.label + self.t.style("  " + f" {self.t.symbols['dot']} ".join(facts), "dim"))

    # -- events -----------------------------------------------------------

    def emit(self, event: Event) -> None:
        t, status = self.t, event.status
        if status == Status.PROGRESS:
            self._progress(event)
            return
        finished_bar = self._bar is not None and (self._bar.percent or 0) >= 99.9
        self.end_live(failed=status == Status.FAILED)
        if status == Status.STARTED:
            phase = PHASES.get(event.stage, event.message)
            if phase != self._phase:
                self._phase = phase
                t.write("")
                t.write(" " + t.style(t.symbols["phase"], "cyan", "bold") + " " + t.style(phase, "bold"))
        elif status == Status.DONE and event.action_required:
            self._summary(event)
        elif status == Status.INFO:
            if event.details.get("kind") == "note":
                t.write("")
                for paragraph in event.message.splitlines():
                    if paragraph.startswith("  "):
                        # A table row: keep it on one line.
                        t.write("  " + paragraph[: t.columns - 3])
                    else:
                        for line in t.wrap(paragraph, first="  ", rest="  "):
                            t.write(line)
            elif event.message.endswith("..."):
                text = event.message[:-3]
                if t.is_tty:
                    self._spinner = (text, time.monotonic())
                    t.set_live(self._spinner_line)
                else:
                    t.write("  " + event.message)
            elif event.details.get("after_progress") and finished_bar and t.is_tty:
                pass  # the finished bar already said it
            else:
                self.line("ok", event.message)
        elif status == Status.WARNING:
            self.line("warn", event.message)
        elif status == Status.ACTION_REQUIRED:
            self._action(event)
        elif status == Status.FAILED:
            t.write("")
            self.line("fail", event.message, "red", "bold")
            if event.action_required:
                lines = t.wrap(event.action_required, first="    ", rest="    ")
                t.write("  " + t.style(t.symbols["arrow"], "cyan", "bold") + " " + lines[0][4:])
                for line in lines[1:]:
                    t.write(line)

    def _progress(self, event: Event) -> None:
        t = self.t
        label = str(event.details.get("label") or event.message.split(":")[0])
        if not t.is_tty:
            now = time.monotonic()
            if now - self._last_plain_progress >= 15 or (event.percent or 0) >= 100:
                self._last_plain_progress = now
                t.write("  " + (f"{event.percent:5.1f}% " if event.percent is not None else "") + event.message)
            return
        if self._bar is None or self._bar.label != label:
            self.end_live()
            self._bar = _Bar(label)
            t.set_live(self._bar_line)
        self._spinner = None
        self._bar.update(event)

    def _action(self, event: Event) -> None:
        t, s = self.t, self.t.symbols
        rule = t.style(s["h"] * t.width, "yellow")
        t.write("")
        t.write(rule)
        for line in t.wrap(event.message):
            t.write(t.style(line, "bold"))
        t.write("")
        for line in (event.action_required or "").splitlines():
            for wrapped in t.wrap(line, rest="   "):
                t.write(wrapped)
        token = event.details.get("display_token")
        if token:
            t.write("")
            t.write(t.style("Migration Token (copy all of it):", "bold"))
            t.write("")
            t.write(str(token))
        t.write(rule)

    def _summary(self, event: Event) -> None:
        t, s = self.t, self.t.symbols
        t.write("")
        took = format_duration(time.monotonic() - self.started)
        t.write("  " + self._mark("ok") + " " + t.style(event.message, "bold", "green") + t.style(f"  ({took})", "dim"))
        t.write("")
        for item in (event.action_required or "").splitlines():
            item = item[2:] if item.startswith("- ") else item
            lines = t.wrap(item, first="    ", rest="    ")
            careful = item.startswith("Good to know")
            lines[0] = "  " + t.style(s["bullet"], "yellow" if careful else "cyan") + " " + lines[0][4:]
            for line in lines:
                t.write(line)
