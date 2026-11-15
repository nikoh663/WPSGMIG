"""Drive wpmigrate interactively in a pseudo-terminal, as a person would.

Usage: python3 drive.py SCRIPT.json

The script is a JSON object:
  {"cwd": "...", "argv": [...], "env": {...},
   "steps": [{"expect": "regex", "send": "text\n"}, {"expect": "regex", "kill": "INT"}, ...],
   "timeout": 900}

Everything the tool prints is copied to stdout, so the transcript shows the
real interactive session (answers typed into hidden prompts are not echoed).
"""

from __future__ import annotations

import codecs
import json
import os
import pty
import re
import select
import signal
import sys
import time


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Screen:
    """Prints what stays on the screen: colours removed, redrawn lines shown once."""

    def __init__(self) -> None:
        self.line = ""
        self.carriage_return = False

    def feed(self, data: str) -> None:
        for piece in re.split(r"(\x1b\[[0-9;]*[A-Za-z]|\r|\n)", data):
            if not piece:
                continue
            if piece == "\n":
                sys.stdout.write(self.line + "\n")
                self.line, self.carriage_return = "", False
            elif piece == "\r":
                self.carriage_return = True
            elif piece.startswith("\x1b["):
                if piece.endswith("K"):
                    self.line, self.carriage_return = "", False
            else:
                if self.carriage_return:
                    self.line, self.carriage_return = "", False
                self.line += piece
        sys.stdout.flush()

    def flush(self) -> None:
        if self.line:
            sys.stdout.write(self.line + "\n")
            self.line = ""
        sys.stdout.flush()


def main() -> int:
    script = json.load(open(sys.argv[1]))
    steps = list(script.get("steps", []))
    timeout = float(script.get("timeout", 900))
    env = dict(script.get("env", {}))
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(script["cwd"])
        os.execvpe(script["argv"][0], script["argv"], env)
    buffer = ""
    screen = Screen()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    raw_log = open(os.environ["DRIVE_RAW"], "w", encoding="utf-8") if os.environ.get("DRIVE_RAW") else None
    started = time.monotonic()
    killed = False
    captured: dict[str, str] = {}
    while True:
        if time.monotonic() - started > timeout:
            print("\n[drive] TIMEOUT", flush=True)
            os.kill(pid, signal.SIGKILL)
            break
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                data = decoder.decode(os.read(fd, 4096))
            except OSError:
                break
            if not data:
                break
            if raw_log:
                raw_log.write(data)
                raw_log.flush()
            screen.feed(data)
            # Wrapped lines must not hide a phrase: compare with single spaces.
            buffer = re.sub(r"\s+", " ", buffer + ANSI.sub("", data))
        while steps:
            step = steps[0]
            match = re.search(step["expect"], buffer)
            if not match:
                break
            captured.update({key: value for key, value in match.groupdict().items() if value})
            if "kill" in step or step.get("show"):
                screen.flush()
                print(f"\n[drive] on screen at this moment: {match.group(0).strip()}", flush=True)
            buffer = buffer[match.end():]
            steps.pop(0)
            if "run" in step:
                # Test harness only: simulate something the person does elsewhere
                # (for example creating a subdomain in the hosting panel).
                import subprocess
                print(f"\n[drive] meanwhile in the hosting panel: {step.get('why', step['run'])}", flush=True)
                subprocess.run(step["run"].format(**captured), shell=True, check=False)
            if "send" in step:
                time.sleep(0.3)
                os.write(fd, step["send"].format(**captured).encode())
            if "kill" in step:
                time.sleep(float(step.get("delay", 0)))
                signame = step["kill"]
                print(f"\n[drive] sending SIG{signame} to the tool (simulating {step.get('why', 'an interruption')})", flush=True)
                if signame == "HUP":
                    os.close(fd)
                    killed = True
                else:
                    os.kill(pid, getattr(signal, "SIG" + signame))
                    killed = signame == "KILL"
        if killed:
            break
    screen.flush()
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    print(f"\n[drive] exit code: {code}", flush=True)
    if steps:
        print(f"[drive] unmatched steps: {[step['expect'] for step in steps]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
