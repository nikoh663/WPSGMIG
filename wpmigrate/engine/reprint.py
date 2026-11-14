"""Running reprint.phar and turning its JSONL output into engine events."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from wpmigrate import constants
from wpmigrate.engine.events import Event, Status
from wpmigrate.engine.system import System

# Fixed launcher run with `php -r`. It reads extra arguments (secrets such as
# --target-pass or --secret) from stdin as a JSON list, appends them to argv
# and loads reprint.phar. This keeps secrets out of the process list.
LAUNCHER = (
    "$extra = json_decode(stream_get_contents(STDIN), true);"
    " if (!is_array($extra)) { $extra = array(); }"
    " $phar = $argv[1];"
    " $argv = array_merge(array($phar), array_slice($argv, 2), $extra);"
    " $argc = count($argv); $_SERVER['argv'] = $argv; $_SERVER['argc'] = $argc;"
    " require $phar;"
)


@dataclass
class ReprintResult:
    """One invocation's outcome, read from its final command report."""

    exit_code: int
    report: dict[str, Any] | None
    records: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)

    @property
    def status(self) -> str | None:
        return (self.report or {}).get("status")

    @property
    def error(self) -> str | None:
        report = self.report or {}
        return report.get("error") or report.get("message")

    @property
    def error_code(self) -> str | None:
        return (self.report or {}).get("error_code")

    def data_record(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        for record in self.records:
            if predicate(record):
                return record
        return None


def parse_jsonl_line(line: str) -> dict[str, Any] | None:
    """Decode one whole line as a JSON object; ignore anything else."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def is_final_report(record: dict[str, Any]) -> bool:
    return record.get("type") == "reprint_report" and record.get("schema_version") == 1


def progress_percent(record: dict[str, Any]) -> float | None:
    """Percent complete from a record's nested ``progress`` object."""
    progress = record.get("progress")
    if not isinstance(progress, dict):
        return None
    byte_counts = progress.get("bytes") or {}
    done, total = byte_counts.get("done"), byte_counts.get("total")
    if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return max(0.0, min(100.0, 100.0 * done / total))
    items = progress.get("items") or {}
    done, total = items.get("done"), items.get("total")
    if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return max(0.0, min(100.0, 100.0 * done / total))
    return None


def describe_progress(record: dict[str, Any]) -> str:
    """Short plain-language progress text."""
    progress = record.get("progress") or {}
    items = progress.get("items") or {}
    byte_counts = progress.get("bytes") or {}
    parts = []
    if items.get("done") is not None:
        unit = {"files": "files", "tables": "tables", "statements": "database statements", "records": "records"}.get(items.get("unit"), items.get("unit") or "items")
        if items.get("total"):
            parts.append(f"{items['done']:,} of {items['total']:,} {unit}")
        else:
            parts.append(f"{items['done']:,} {unit}")
    if byte_counts.get("done") is not None:
        done_mb = byte_counts["done"] / 1_048_576
        if byte_counts.get("total"):
            parts.append(f"{done_mb:,.1f} of {byte_counts['total'] / 1_048_576:,.1f} MB")
        else:
            parts.append(f"{done_mb:,.1f} MB")
    table = progress.get("current_table") or {}
    if table.get("name"):
        parts.append(f"table {table['name']}")
    return ", ".join(parts)


class ReprintRunner:
    """Builds Reprint command lines and runs them one at a time."""

    def __init__(
        self,
        *,
        system: System,
        php: str,
        phar: Path,
        state_dir: Path,
        fs_root: Path,
        api_url: str,
        emit: Callable[[Event], None],
        log: Callable[[str], None],
        insecure: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        bytes_total_check: Callable[[int], None] | None = None,
    ) -> None:
        self.system = system
        self.php = php
        self.phar = phar
        self.state_dir = state_dir
        self.fs_root = fs_root
        self.api_url = api_url
        self.emit = emit
        self.log = log
        self.insecure = insecure
        self.sleep = sleep
        self.bytes_total_check = bytes_total_check
        self.connection_token: str | None = None

    def base_args(self, command: str, *, remote: bool = True, fs_root: bool = True) -> list[str]:
        args = [command]
        if remote:
            args.append(self.api_url)
        args.append(f"--state-dir={self.state_dir}")
        if fs_root:
            args.append(f"--fs-root={self.fs_root}")
        if self.insecure:
            args.append("--insecure")
        return args

    def credential_args(self) -> list[str]:
        """Secret arguments for commands that contact the source."""
        return [f"--secret={self.connection_token}"] if self.connection_token else []

    def run(
        self,
        args: Sequence[str],
        *,
        stage: str,
        secret_args: Sequence[str] = (),
        progress_label: str | None = None,
    ) -> ReprintResult:
        """Run one Reprint invocation with ``--progress=jsonl``."""
        argv = [self.php, "-d", "display_errors=stderr", "-r", LAUNCHER, "--", str(self.phar), *args]
        if "--progress=jsonl" not in args and args and args[0] not in ("recover", "post-process"):
            argv.append("--progress=jsonl")
        self.log("RUN " + " ".join(argv[5:]) + (" (+ secret arguments on stdin)" if secret_args else ""))
        records: list[dict[str, Any]] = []
        stderr_tail: list[str] = []
        report: dict[str, Any] | None = None
        last_emit = [0.0, -10.0]
        checked_total = [False]

        def handle_record(record: dict[str, Any]) -> None:
            nonlocal report
            if is_final_report(record):
                report = record
                return
            records.append(record)
            record_type = record.get("type")
            if record_type == "warning" or record.get("status") == "warning":
                self.emit(Event(stage, Status.WARNING, str(record.get("message", "Reprint reported a warning."))))
                return
            if record_type == "lifecycle" and record.get("event") == "retry":
                wait = record.get("retry_after_seconds")
                self.emit(Event(stage, Status.INFO, f"The source site asked us to slow down (HTTP {record.get('http_code')}); waiting {wait} seconds before continuing."))
                return
            progress = record.get("progress")
            if isinstance(progress, dict):
                byte_counts = progress.get("bytes") or {}
                if self.bytes_total_check and not checked_total[0] and record.get("phase") == "fetch" and byte_counts.get("total"):
                    checked_total[0] = True
                    self.bytes_total_check(int(byte_counts["total"]))
                percent = progress_percent(record)
                now = time.monotonic()
                if now - last_emit[0] >= 2.0 or (percent is not None and percent >= 100.0 and last_emit[1] < 100.0):
                    last_emit[0] = now
                    last_emit[1] = percent if percent is not None else last_emit[1]
                    text = describe_progress(record)
                    label = progress_label or str(record.get("message") or "Working")
                    self.emit(Event(stage, Status.PROGRESS, f"{label}: {text}" if text else label, percent=percent))

        def on_stdout(line: str) -> None:
            record = parse_jsonl_line(line)
            if record is not None:
                handle_record(record)
            elif line.strip():
                self.log("reprint: " + line[:500])

        def on_stderr(line: str) -> None:
            record = parse_jsonl_line(line)
            if record is not None:
                handle_record(record)
                return
            if line.strip():
                stderr_tail.append(line)
                del stderr_tail[:-20]
                self.log("reprint stderr: " + line[:500])

        stdin_text = json.dumps(list(secret_args)) if secret_args else "[]"
        exit_code = self.system.stream(argv, on_stdout_line=on_stdout, on_stderr_line=on_stderr, stdin_text=stdin_text)
        if report is None:
            self.log(f"Reprint exited {exit_code} without a final report")
        else:
            self.log(f"Reprint report: status={report.get('status')} exit={report.get('exit_code')} error_code={report.get('error_code')} error={report.get('error')}")
        return ReprintResult(exit_code, report, records, stderr_tail)

    def run_to_completion(
        self,
        args: Sequence[str],
        *,
        stage: str,
        secret_args: Sequence[str] = (),
        progress_label: str | None = None,
    ) -> ReprintResult:
        """Run a resumable command again while it reports unfinished work (exit 2),
        and wait and retry a few times after temporary failures (exit 3)."""
        restarts = 0
        temporary_failures = 0
        while True:
            result = self.run(args, stage=stage, secret_args=secret_args, progress_label=progress_label)
            if result.exit_code == constants.REPRINT_EXIT_UNFINISHED:
                restarts += 1
                if restarts > constants.MAX_UNFINISHED_RESTARTS:
                    return result
                continue
            if result.exit_code == constants.REPRINT_EXIT_TEMPORARY:
                if temporary_failures >= len(constants.TEMPORARY_FAILURE_WAITS):
                    return result
                wait = constants.TEMPORARY_FAILURE_WAITS[temporary_failures]
                temporary_failures += 1
                self.emit(Event(stage, Status.INFO, f"The source site is busy or the connection dropped. Waiting {wait} seconds, then continuing where it stopped."))
                self.sleep(wait)
                continue
            return result
