"""Finding a command-line PHP that can run Reprint."""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass
from typing import Iterable

from wpmigrate import constants
from wpmigrate.engine.system import System

# Fixed probe code run with `php -r`. It only reports facts about the binary.
_PROBE = (
    'echo json_encode(array("version"=>PHP_VERSION,"sapi"=>PHP_SAPI,'
    '"extensions"=>get_loaded_extensions(),"memory_limit"=>ini_get("memory_limit")));'
)

_NAME_PATTERN = re.compile(r"^php(?:-?\d+(?:\.\d+)?)?(?:-cli)?$")


@dataclass(frozen=True)
class PhpBinary:
    """A probed PHP executable."""

    path: str
    version: tuple[int, int, int]
    sapi: str
    extensions: frozenset[str]

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)

    def missing_extensions(self, required: Iterable[str] = constants.PHP_REQUIRED_EXTENSIONS) -> list[str]:
        have = {name.lower() for name in self.extensions}
        return [name for name in required if name.lower() not in have]


def parse_version(text: str) -> tuple[int, int, int]:
    """``"8.2.10-1ubuntu"`` -> ``(8, 2, 10)``."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", text.strip())
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def candidate_paths(system: System, path_env: str | None = None) -> list[str]:
    """PHP executables on PATH plus common versioned install locations."""
    seen: list[str] = []

    def add(path: str) -> None:
        real = os.path.realpath(path)
        if os.path.isfile(real) and os.access(real, os.X_OK) and real not in [os.path.realpath(p) for p in seen]:
            seen.append(path)

    default = system.which("php")
    if default:
        add(default)
    for directory in (path_env if path_env is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if _NAME_PATTERN.match(name):
                add(os.path.join(directory, name))
    # Versioned layouts used by many servers (for example /usr/local/php82/bin/php).
    for pattern in ("/usr/bin/php*", "/usr/local/bin/php*", "/usr/local/php*/bin/php", "/opt/php*/bin/php", "/opt/*/php*/bin/php"):
        for path in sorted(glob.glob(pattern)):
            if _NAME_PATTERN.match(os.path.basename(path)):
                add(path)
    return seen


def probe(system: System, path: str) -> PhpBinary | None:
    """Run the probe; ``None`` if it is not a working PHP CLI."""
    try:
        result = system.run([path, "-d", "display_errors=stderr", "-r", _PROBE], timeout=30)
    except (OSError, TimeoutError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip().splitlines()
    if not output:
        return None
    try:
        data = json.loads(output[-1])
    except json.JSONDecodeError:
        return None
    if data.get("sapi") != "cli":
        return None
    return PhpBinary(path=path, version=parse_version(data.get("version", "")), sapi=data["sapi"], extensions=frozenset(data.get("extensions", [])))


@dataclass(frozen=True)
class PhpChoice:
    """The selected binary and every other candidate that was considered."""

    chosen: PhpBinary | None
    considered: list[PhpBinary]
    below_preferred: bool


def choose_php(system: System, override: str | None = None) -> PhpChoice:
    """Pick the newest PHP >= 7.4 with the required extensions, preferring >= 8.2."""
    paths = [override] if override else candidate_paths(system)
    probed = [binary for binary in (probe(system, path) for path in paths) if binary]
    usable = [binary for binary in probed if binary.version[:2] >= constants.PHP_MINIMUM and not binary.missing_extensions()]
    usable.sort(key=lambda binary: binary.version, reverse=True)
    if not usable:
        return PhpChoice(None, probed, False)
    best = usable[0]
    return PhpChoice(best, probed, best.version[:2] < constants.PHP_PREFERRED)
