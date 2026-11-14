"""Running WP-CLI against a WordPress folder on this server."""

from __future__ import annotations

import hashlib
import os
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from wpmigrate import constants
from wpmigrate.engine.errors import DestinationFailed
from wpmigrate.engine.system import CommandResult, System


def download(url: str, target: Path, *, user_agent: str, timeout: float = 120) -> None:
    """Download a tool over verified HTTPS into ``target`` (mode 0600)."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    context = ssl.create_default_context()
    temporary = target.with_name(target.name + ".part")
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response, open(temporary, "wb") as handle:
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            handle.write(chunk)
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class WpCli:
    """WP-CLI invocations, either a system ``wp`` or ``php wp-cli.phar``."""

    system: System
    command: list[str]

    @classmethod
    def locate(cls, system: System, php: str, tools_dir: Path, user_agent: str) -> "WpCli":
        """Use ``wp`` if installed, else download the official phar once."""
        existing = system.which("wp")
        if existing:
            probe = system.run([existing, "--version"], timeout=60)
            if probe.returncode == 0 and "WP-CLI" in probe.stdout:
                return cls(system, [existing])
        phar = tools_dir / "wp-cli.phar"
        if not phar.exists():
            try:
                download(constants.WP_CLI_PHAR_URL, phar, user_agent=user_agent)
            except OSError as error:
                raise DestinationFailed(
                    f"WP-CLI is not installed here and downloading it failed ({error}).",
                    f"Download {constants.WP_CLI_PHAR_URL} to {phar} by hand, then run the tool again.",
                ) from error
        probe = system.run([php, str(phar), "--version"], timeout=60)
        if probe.returncode != 0 or "WP-CLI" not in probe.stdout:
            raise DestinationFailed(
                "The downloaded WP-CLI does not run with this PHP.",
                f"Delete {phar} and run the tool again.",
            )
        return cls(system, [php, str(phar)])

    def run(self, folder: Path, args: Sequence[str], *, skip_plugins: bool = True, timeout: float = 600) -> CommandResult:
        argv = [*self.command, *args, f"--path={folder}"]
        if skip_plugins:
            argv += ["--skip-plugins", "--skip-themes"]
        return self.system.run(argv, timeout=timeout, cwd=str(folder))

    def option_get(self, folder: Path, name: str, *, skip_plugins: bool = True) -> str | None:
        result = self.run(folder, ["option", "get", name], skip_plugins=skip_plugins)
        return result.stdout.strip() if result.returncode == 0 else None
