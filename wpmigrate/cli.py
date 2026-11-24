"""Terminal front end: asks the questions and shows what the engine reports."""

from __future__ import annotations

import argparse
import getpass
import os
import signal
import sys
from pathlib import Path
from typing import Any

from wpmigrate.engine import destination
from wpmigrate import __version__, constants
from wpmigrate.engine.context import Context, Options, Secrets
from wpmigrate.engine.prompts import (
    ChoiceQuestion,
    ConfirmQuestion,
    NoAnswerAvailable,
    SecretQuestion,
    TextQuestion,
    TypeToConfirmQuestion,
    WaitQuestion,
)
from wpmigrate.engine.runner import Engine
from wpmigrate.engine.system import LocalSystem
from wpmigrate.redact import Redactor
from wpmigrate.ui import Renderer, Terminal


class TerminalPrompter:
    """Asks in the terminal, unless an option already gave the answer."""

    def __init__(self, terminal: Terminal, presets: dict[str, Any], *, interactive: bool, renderer: Renderer | None = None) -> None:
        self.t = terminal
        self.presets = presets
        self.interactive = interactive
        self.renderer = renderer

    def _preset(self, key: str) -> Any:
        return self.presets.get(key)

    def has_answer(self, key: str) -> bool:
        return self.presets.get(key) is not None

    def _quiet(self) -> None:
        """Stop any spinner or bar before asking."""
        if self.renderer is not None:
            self.renderer.end_live()

    def _question(self, text: str, help_text: str | None = None, *, tail: str = "") -> None:
        """Print the question: a marker, the text in bold, then a dim hint."""
        t = self.t
        self._quiet()
        t.write("")
        lines = t.wrap(text + tail, first="    ", rest="    ")
        t.write("  " + t.style(t.symbols["ask"], "cyan", "bold") + " " + t.style(lines[0][4:], "bold"))
        for line in lines[1:]:
            t.write(t.style(line, "bold"))
        if help_text:
            for line in t.wrap(help_text, first="    ", rest="    "):
                t.write(t.style(line, "dim"))

    def _arrow(self) -> str:
        return "  " + self.t.style(self.t.symbols["prompt"], "cyan", "bold") + " "

    def _input(self, prompt: str, *, what: str = "") -> str:
        if not self.interactive or self.t.detached:
            raise NoAnswerAvailable(what or prompt.strip())
        try:
            return input(prompt)
        except EOFError as error:
            raise NoAnswerAvailable(what or prompt.strip()) from error

    def ask_text(self, question: TextQuestion) -> str:
        preset = self._preset(question.key)
        if preset is not None:
            return str(preset)
        if not self.interactive and question.default is not None:
            return question.default
        if not self.interactive or self.t.detached:
            raise NoAnswerAvailable(question.text)
        self._question(question.text, question.help or None)
        if question.default:
            t = self.t
            for line in t.wrap(f"Press Enter for {question.default}", first="    ", rest="    "):
                t.write(t.style(line[:20], "dim") + t.style(line[20:], "cyan") if line.startswith("    Press Enter for ") else t.style(line, "cyan"))
        answer = self._input(self._arrow(), what=question.text).strip()
        return answer or (question.default or "")

    def ask_secret(self, question: SecretQuestion) -> str:
        preset = self._preset(question.key)
        if preset is not None:
            return str(preset)
        if not self.interactive or self.t.detached:
            raise NoAnswerAvailable(question.text)
        self._question(question.text, None if "not shown" in question.text else "It is not shown while you type.")
        try:
            return getpass.getpass(self._arrow())
        except EOFError as error:
            raise NoAnswerAvailable(question.text) from error

    def ask_choice(self, question: ChoiceQuestion) -> str:
        preset = self._preset(question.key)
        values = [choice.value for choice in question.choices]
        if preset is not None:
            if str(preset) in values:
                return str(preset)
            raise NoAnswerAvailable(f"{question.key}={preset} is not one of {', '.join(values)}")
        if not self.interactive and question.default is not None and question.key not in ("existing_site",):
            return question.default
        if not self.interactive or self.t.detached:
            raise NoAnswerAvailable(question.text)
        t = self.t
        self._question(question.text)
        default_number = values.index(question.default) + 1 if question.default in values else None
        for number, choice in enumerate(question.choices, start=1):
            lines = t.wrap(choice.label, first="       ", rest="       ")
            t.write("    " + t.style(f"{number}.", "cyan", "bold") + " " + lines[0][7:] + (t.style("  (default)", "dim") if number == default_number else ""))
            for line in lines[1:]:
                t.write(line)
        while True:
            hint = f"Type a number{f' [{default_number}]' if default_number else ''}: "
            answer = self._input(self._arrow() + hint, what=question.text).strip()
            if not answer and default_number:
                return values[default_number - 1]
            if answer.isdigit() and 1 <= int(answer) <= len(values):
                return values[int(answer) - 1]
            t.write(t.style(f"    Please type a number from 1 to {len(values)}.", "yellow"))

    def confirm(self, question: ConfirmQuestion) -> bool:
        preset = self._preset(question.key)
        if preset is not None:
            return bool(preset) if isinstance(preset, bool) else str(preset).lower() in ("1", "y", "yes", "true")
        if not self.interactive or self.t.detached:
            raise NoAnswerAvailable(question.text)
        self._question(question.text)
        hint = "[Y/n] " if question.default else "[y/N] "
        while True:
            answer = self._input(self._arrow() + self.t.style(hint, "dim"), what=question.text).strip().lower()
            if not answer:
                return question.default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.t.write(self.t.style("    Please answer y or n.", "yellow"))

    def type_to_confirm(self, question: TypeToConfirmQuestion) -> bool:
        preset = self._preset(question.key)
        if preset is not None:
            return str(preset) == question.expected
        self._question(question.text)
        answer = self._input(self._arrow(), what=question.text).strip()
        return answer == question.expected

    def wait_for_person(self, question: WaitQuestion) -> None:
        if self._preset(question.key) is not None:
            return
        self._quiet()
        for line in question.instructions:
            self.t.write("  " + line)
        self._question(question.text)
        self._input(self._arrow(), what=question.text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wpmigrate",
        description="Copy a WordPress site onto this server. Start it in the website's folder and answer the questions; no options are needed.",
        epilog=(
            "Every option can also be set as an environment variable, for example WPMIGRATE_SOURCE or WPMIGRATE_DB_NAME. "
            "Passwords are only read from WPMIGRATE_ADMIN_PASSWORD and WPMIGRATE_DB_PASSWORD (or asked for), never from options. "
            "Exit codes: 0 success, 10 login failed, 11 source authorization needed, 12 transfer failed, 13 destination setup failed, "
            "14 cancelled, 15 server requirements missing, 16 already running, 1 other error."
        ),
    )
    add = parser.add_argument
    add("--version", action="version", version=f"wpmigrate {__version__}")
    add("--source", metavar="URL", help="address of the site to copy")
    add("--user", metavar="NAME", help="administrator username on the source site")
    add("--new-url", metavar="URL", help="address of the copied site (default: worked out from the folder, otherwise the same address)")
    add("--folder", metavar="PATH", help="folder to install the copied site in (default: the current folder)")
    add("--existing-site", choices=("remove", "keep", "cancel"), help="what to do with a WordPress site already in that folder")
    add("--confirm-domain", metavar="DOMAIN", help="confirms removing an existing site (must match its domain)")
    add("--old-site-folder", metavar="PATH", help="folder for the kept old site (default: old.<domain>/public_html next to the domain folder, also for a site in a subfolder)")
    add("--subdomain-ready", action="store_true", default=None, help="the old.<domain> subdomain has been created already")
    add("--db-host", metavar="HOST", help="database host for the copied site (default: localhost)")
    add("--db-name", metavar="NAME", help="database name for the copied site")
    add("--db-user", metavar="USER", help="database user for the copied site")
    add("--replace-tables", action="store_true", default=None, help="confirm replacing tables in that database")
    add("--reuse-old-db", choices=("yes", "no"), help="use the removed site's database details")
    add("--other-files", choices=("backup", "with_old", "move", "remove", "leave"), help="what to do with non-WordPress files in the folder (with_old: move them along with a kept old site; move: to --other-files-folder)")
    add("--install-plugins", metavar="LIST", help="plugins to install and activate once the copy works, comma separated (slugs, .zip files or https:// .zip addresses)")
    add("--server-ip", metavar="IP", help="this server's IP address for the hosts-file line (found automatically when not given)")
    add("--other-files-folder", metavar="PATH", help="where --other-files move puts them")
    add("--skip-backups", choices=("yes", "no"), help="leave leftover backups out of the copy (yes, the default) or copy all of them (no)")
    add("--cleanup-source", choices=("yes", "no"), help="remove the plugin and this server's access from the source at the end (default: yes)")
    add("--manual", action="store_true", help="do the source steps (plugin and authorization) yourself in the browser")
    add("--php", metavar="PATH", help="PHP program to use (default: the newest suitable one found)")
    add("--reprint-phar", metavar="PATH", help="use this copy of the transfer engine (reprint.phar) instead of downloading it")
    add("--user-agent", metavar="TEXT", help="browser identification sent to the source site")
    add("--allow-http", action="store_true", help="allow a source or new address without HTTPS (insecure)")
    add("--keep-downloads", action="store_true", help="keep the downloaded database file and login cookies at the end")
    add("--file-layout", choices=("move", "copy"), help="move the downloaded files into the folder (default, fast) or copy them (needs twice the space)")
    add("--non-interactive", action="store_true", help="never ask; stop if an answer is missing")
    add("--resume", choices=("yes", "no"), help="continue an unfinished copy started from this folder")
    return parser


_ENV_KEYS = {
    "source": "WPMIGRATE_SOURCE", "user": "WPMIGRATE_USER", "new_url": "WPMIGRATE_NEW_URL", "folder": "WPMIGRATE_FOLDER",
    "existing_site": "WPMIGRATE_EXISTING_SITE", "confirm_domain": "WPMIGRATE_CONFIRM_DOMAIN", "old_site_folder": "WPMIGRATE_OLD_SITE_FOLDER",
    "db_host": "WPMIGRATE_DB_HOST", "db_name": "WPMIGRATE_DB_NAME", "db_user": "WPMIGRATE_DB_USER", "other_files": "WPMIGRATE_OTHER_FILES",
    "cleanup_source": "WPMIGRATE_CLEANUP_SOURCE", "php": "WPMIGRATE_PHP", "reprint_phar": "WPMIGRATE_REPRINT_PHAR",
    "user_agent": "WPMIGRATE_USER_AGENT", "file_layout": "WPMIGRATE_FILE_LAYOUT", "reuse_old_db": "WPMIGRATE_REUSE_OLD_DB", "skip_backups": "WPMIGRATE_SKIP_BACKUPS", "install_plugins": "WPMIGRATE_INSTALL_PLUGINS", "server_ip": "WPMIGRATE_SERVER_IP", "resume": "WPMIGRATE_RESUME",
}
_ENV_FLAGS = {"allow_http": "WPMIGRATE_ALLOW_HTTP", "keep_downloads": "WPMIGRATE_KEEP_DOWNLOADS", "manual": "WPMIGRATE_MANUAL",
              "non_interactive": "WPMIGRATE_NON_INTERACTIVE", "replace_tables": "WPMIGRATE_REPLACE_TABLES", "subdomain_ready": "WPMIGRATE_SUBDOMAIN_READY"}


def apply_environment(args: argparse.Namespace, environ: dict[str, str]) -> None:
    for attribute, variable in _ENV_KEYS.items():
        if getattr(args, attribute, None) in (None, "") and environ.get(variable):
            setattr(args, attribute, environ[variable])
    for attribute, variable in _ENV_FLAGS.items():
        if not getattr(args, attribute, None) and environ.get(variable, "").lower() in ("1", "yes", "true"):
            setattr(args, attribute, True)


def presets_from(args: argparse.Namespace) -> dict[str, Any]:
    """Question answers given as options."""
    presets: dict[str, Any] = {}
    mapping = {
        "folder": "dest_folder", "existing_site": "existing_site", "confirm_domain": "confirm_remove_domain",
        "old_site_folder": "old_site_folder", "db_host": "db_host", "db_name": "db_name", "db_user": "db_user",
        "other_files": "other_files", "other_files_folder": "other_files_folder",
    }
    for attribute, key in mapping.items():
        value = getattr(args, attribute, None)
        if value not in (None, ""):
            presets[key] = value
    if args.subdomain_ready:
        presets["subdomain_created"] = True
    if args.replace_tables:
        presets["confirm_replace_tables"] = True
    if args.reuse_old_db:
        presets["reuse_existing_db"] = args.reuse_old_db == "yes"
    if args.cleanup_source:
        presets["cleanup_source"] = args.cleanup_source == "yes"
    if args.skip_backups:
        presets["copy_extra_files"] = "none" if args.skip_backups == "yes" else "all"
    if args.install_plugins:
        presets["install_plugins"] = args.install_plugins
    if args.resume:
        presets["resume"] = args.resume == "yes"
    if args.non_interactive:
        presets.setdefault("create_dest_folder", True)
        presets.setdefault("copy_again", True)
        presets.setdefault("copy_extra_files", "none")
        presets.setdefault("use_subfolder_address", True)
        presets.setdefault("remove_tool_files", False)
    return presets


def tool_location() -> str | None:
    """Path of this tool's own files (the .pyz, or the folder holding the package)."""
    launched = Path(sys.argv[0]).resolve()
    if launched.suffix == ".pyz" and launched.is_file():
        return str(launched)
    package_parent = Path(__file__).resolve().parent.parent
    if (package_parent / "wpmigrate").is_dir() and (package_parent / "README.md").exists():
        return str(package_parent)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    apply_environment(args, dict(os.environ))
    if args.server_ip:
        try:
            import ipaddress
            ipaddress.ip_address(args.server_ip)
        except ValueError:
            parser.error(f"--server-ip: {args.server_ip} is not an IP address")
    try:
        install_plugins = destination.parse_plugin_list(args.install_plugins or "")
    except ValueError as error:
        parser.error(f"--install-plugins: {error}")
    terminal = Terminal(sys.stdout)
    renderer = Renderer(terminal)
    interactive = not args.non_interactive and sys.stdin.isatty()
    prompter = TerminalPrompter(terminal, presets_from(args), interactive=interactive, renderer=renderer)
    redactor = Redactor()
    secrets = Secrets(os.environ.get("WPMIGRATE_ADMIN_PASSWORD") or None, os.environ.get("WPMIGRATE_DB_PASSWORD") or None)
    redactor.add(secrets.admin_password)
    redactor.add(secrets.db_password)
    for variable in ("WPMIGRATE_ADMIN_PASSWORD", "WPMIGRATE_DB_PASSWORD"):
        os.environ.pop(variable, None)  # keep them out of child processes
    options = Options(
        source_url=args.source,
        admin_user=args.user,
        new_site_url=args.new_url,
        php_binary=args.php,
        user_agent=args.user_agent or constants.USER_AGENT,
        allow_http=bool(args.allow_http),
        reprint_phar=args.reprint_phar,
        manual_source_steps=bool(args.manual),
        start_dir=Path.cwd(),
        keep_downloads=bool(args.keep_downloads),
        file_layout=args.file_layout or "move",
        install_plugins=tuple(install_plugins),
        server_ip=args.server_ip,
    )
    system = LocalSystem()
    ctx = Context(system=system, prompter=prompter, sink=renderer, options=options, secrets=secrets, redactor=redactor)

    def on_hangup(signum: int, frame: object) -> None:
        # The SSH session dropped: keep the transfer going, stop asking questions.
        terminal.detached = True
        prompter.interactive = False
        ctx.log("terminal disconnected; continuing without questions")

    signal.signal(signal.SIGHUP, on_hangup)
    renderer.banner(f"wpmigrate {__version__}", "Let's move your WordPress site to this server")
    renderer.hint("Just answer a few questions and I'll do the rest. You can stop any time with Ctrl+C; run me again and I pick up where I left off.")
    engine = Engine(ctx, tool_location=tool_location())
    try:
        code = engine.run()
    except KeyboardInterrupt:
        renderer.end_live()
        system.stop_children()
        terminal.write("")
        renderer.line("warn", "Paused. Run me again whenever you like and I will pick up where I left off.")
        ctx.log("stopped with Ctrl+C")
        return constants.ExitCode.CANCELLED
    renderer.end_live()
    if ctx.paths is not None and code != 0:
        renderer.hint(f"  A detailed log is in {ctx.paths.log_file}")
    if terminal.detached:
        # The terminal is gone; make sure the final flush cannot fail.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.dup2(devnull, sys.stderr.fileno())
    return code


if __name__ == "__main__":
    sys.exit(main())
