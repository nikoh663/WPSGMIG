"""Stages 1-5: questions, requirements, and the source site."""

from __future__ import annotations

import json
import secrets as random_tokens
import sys
from pathlib import Path

from wpmigrate import constants
from wpmigrate.engine import php as php_module
from wpmigrate.engine.context import Context
from wpmigrate.engine.errors import AuthorizationNeeded, LoginFailed, MigrationError, RequirementsFailed, SourceStepFailed
from wpmigrate.engine.events import Status
from wpmigrate.engine.http import HttpClient
from wpmigrate.engine.prompts import ConfirmQuestion, NoAnswerAvailable, SecretQuestion, TextQuestion
from wpmigrate.engine.reprint import ReprintRunner
from wpmigrate.engine.source import SourceSite, WrongPassword, normalize_site_url, reprint_api_url
from wpmigrate.engine.state import RunLock, SitePaths, StateStore, site_slug
from wpmigrate.engine.system import LocalSystem
from wpmigrate.engine.wpcli import download, sha256


def private_root(ctx: Context) -> Path:
    return ctx.system.home() / ".wpmigrate"


# -- shared helpers ------------------------------------------------------------

def http_client(ctx: Context) -> HttpClient:
    return HttpClient(
        cookie_file=ctx.p.cookie_file,
        user_agent=ctx.options.user_agent,
        allow_http=ctx.options.allow_http,
        log=ctx.log,
        sleep=ctx.options.sleep,
    )


def source_site(ctx: Context) -> SourceSite:
    return SourceSite(http_client(ctx), ctx.st.get("source_url"), ctx.log)


def reprint_runner(ctx: Context) -> ReprintRunner:
    runner = ReprintRunner(
        system=ctx.system,
        php=ctx.st.get("php_path"),
        phar=Path(ctx.st.get("reprint_phar")),
        state_dir=ctx.p.reprint_state,
        fs_root=ctx.p.download,
        api_url=reprint_api_url(ctx.st.get("source_url")),
        emit=ctx.emit_event,
        log=ctx.log,
        insecure=ctx.options.allow_http and ctx.st.get("source_url", "").startswith("http:"),
        sleep=ctx.options.sleep,
    )
    token_file = ctx.p.connection_token_file
    if token_file.exists():
        runner.connection_token = token_file.read_text(encoding="utf-8").strip() or None
        ctx.redactor.add(runner.connection_token)
    return runner


def admin_password(ctx: Context) -> str:
    """The source admin password, asked for when needed and kept in memory."""
    if not ctx.secrets.admin_password:
        ctx.secrets.admin_password = ctx.prompter.ask_secret(
            SecretQuestion("admin_password", f"Password for the administrator account \"{ctx.st.get('admin_user')}\" (it is not shown while you type)")
        )
        ctx.redactor.add(ctx.secrets.admin_password)
    return ctx.secrets.admin_password


def open_site_state(ctx: Context, source_url: str) -> None:
    """Create the private directory for this source site and load its state."""
    root = private_root(ctx)
    LocalSystem.make_private_dir(root)
    LocalSystem.make_private_dir(root / "sites")
    ctx.paths = SitePaths(root / "sites" / site_slug(source_url))
    ctx.paths.create()
    lock = RunLock(ctx.paths)
    lock.acquire()
    ctx.lock = lock
    ctx.state = StateStore(ctx.paths)
    ctx.log(f"--- run started in {ctx.options.start_dir} for {source_url}")


def remember_last_run(ctx: Context) -> None:
    """Record which site a run from this folder belongs to, for resuming."""
    pointer = private_root(ctx) / "runs.json"
    try:
        runs = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        runs = {}
    runs[str(ctx.options.start_dir)] = ctx.st.get("source_url")
    LocalSystem.write_private_file(pointer, json.dumps(runs, indent=2))


def unfinished_run_for(ctx: Context, start_dir: Path) -> str | None:
    pointer = private_root(ctx) / "runs.json"
    try:
        runs = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    source_url = runs.get(str(start_dir))
    if not source_url:
        return None
    state_file = private_root(ctx) / "sites" / site_slug(source_url) / "state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if state.get("stages", {}).get("summary", {}).get("status") == "done":
        return None
    return source_url


# -- stage 1 ------------------------------------------------------------------

def stage_collect(ctx: Context) -> None:
    """Ask for the source address, the admin account and the new address."""
    source_url = ctx.options.source_url
    if not source_url:
        previous = unfinished_run_for(ctx, ctx.options.start_dir)
        if previous:
            resume = ctx.prompter.confirm(ConfirmQuestion("resume", f"An unfinished copy of {previous} was started from this folder. Continue it?", default=True))
            if resume:
                source_url = previous
    if not source_url:
        source_url = ctx.prompter.ask_text(TextQuestion("source_url", "What is the address of the WordPress site you want to copy? (for example https://example.com)"))
    source_url = normalize_site_url(source_url)
    open_site_state(ctx, source_url)
    state = ctx.st
    if state.stage_done("collect") and state.get("source_url") == source_url:
        ctx.info(f"Continuing the copy of {source_url} from where it stopped.")
        if ctx.options.admin_user and ctx.options.admin_user != state.get("admin_user"):
            state.set("admin_user", ctx.options.admin_user)
        return
    admin_user = ctx.options.admin_user or ctx.prompter.ask_text(TextQuestion("admin_user", "Administrator username (or e-mail) on that site"))
    admin_user = admin_user.strip()
    if not admin_user:
        raise MigrationError("No administrator username was entered.", "Run the tool again and enter the username you use to log in to wp-admin.")
    state.update({"source_url": source_url, "admin_user": admin_user, "start_dir": str(ctx.options.start_dir)})
    if not ctx.options.manual_source_steps:
        admin_password(ctx)
    new_site_url = ctx.options.new_site_url or ctx.prompter.ask_text(
        TextQuestion(
            "new_site_url",
            "What address will the copied site use? Press Enter to keep the same address (you will switch the domain to this server later).",
            default=source_url,
        )
    )
    new_site_url = normalize_site_url(new_site_url)
    if new_site_url.startswith("http:") and not ctx.options.allow_http:
        raise MigrationError(f"The new address {new_site_url} does not use HTTPS.", "Use an https:// address, or run the tool with --allow-http if the new site really has no HTTPS.")
    state.update({"new_site_url": new_site_url})
    remember_last_run(ctx)


# -- stage 2 ------------------------------------------------------------------

def stage_requirements(ctx: Context) -> None:
    """Check Python, PHP, disk space and folders; get reprint.phar."""
    if sys.version_info < (3, 10):
        raise RequirementsFailed(f"This tool needs Python 3.10 or newer; this server has {sys.version.split()[0]}.", "Ask your host how to run a newer python3, then try again.")
    choice = php_module.choose_php(ctx.system, ctx.options.php_binary)
    if choice.chosen is None:
        if ctx.options.php_binary:
            raise RequirementsFailed(f"The PHP program {ctx.options.php_binary} could not be used.", "Check the path given with --php, or leave it out so the tool can look for PHP itself.")
        if not choice.considered:
            raise RequirementsFailed("No command-line PHP was found on this server.", "Ask your host to enable PHP for SSH sessions, then run the tool again.")
        newest = max(choice.considered, key=lambda binary: binary.version)
        missing = newest.missing_extensions()
        if newest.version[:2] < constants.PHP_MINIMUM:
            raise RequirementsFailed(
                f"The newest command-line PHP found is {newest.version_text} ({newest.path}); Reprint needs PHP 7.4 or newer.",
                "Ask your host how to use a newer PHP in SSH, or pass its path with --php.",
            )
        raise RequirementsFailed(
            f"PHP {newest.version_text} ({newest.path}) is missing these extensions: {', '.join(missing)}.",
            "Enable them for command-line PHP in your hosting panel, or ask your host, then run the tool again.",
        )
    chosen = choice.chosen
    ctx.info(f"Using PHP {chosen.version_text} at {chosen.path}.")
    if choice.below_preferred:
        ctx.warn(f"PHP {chosen.version_text} works, but Reprint runs best on PHP 8.2 or newer. If your host offers a newer PHP for SSH, pass it with --php.")
    free = LocalSystem.disk_free(ctx.p.root)
    if free < constants.MIN_FREE_BYTES:
        raise RequirementsFailed(
            f"Only {free // 1_048_576} MB of disk space is free in your home folder.",
            "Free up space (at least about twice the size of the site) and run the tool again.",
        )
    for directory in (ctx.p.root, ctx.p.reprint_state, ctx.p.download, ctx.p.tools):
        if not LocalSystem.is_writable_dir(directory):
            raise RequirementsFailed(f"The folder {directory} cannot be written to.", "Check the folder's permissions in your hosting panel's file manager.")
    phar = Path(ctx.options.reprint_phar).resolve() if ctx.options.reprint_phar else ctx.p.tools / "reprint.phar"
    if not phar.exists():
        if ctx.options.reprint_phar:
            raise RequirementsFailed(f"{phar} does not exist.", "Check the path given with --reprint-phar.")
        ctx.info("Downloading the Reprint client from WordPress.org...")
        try:
            download(constants.REPRINT_PHAR_URL, phar, user_agent=ctx.options.user_agent)
        except OSError as error:
            raise RequirementsFailed(
                f"Downloading the Reprint client failed ({error}).",
                f"Check this server's internet access, or download {constants.REPRINT_PHAR_URL} yourself and run the tool with --reprint-phar=PATH.",
            ) from error
    version = ctx.system.run([chosen.path, str(phar), "--version"], timeout=60)
    if version.returncode != 0 or not version.stdout.strip():
        raise RequirementsFailed(f"The Reprint client at {phar} does not run with PHP {chosen.version_text}.", "Delete that file so the tool downloads it again, or try another PHP with --php.")
    reprint_version = version.stdout.strip().splitlines()[-1]
    ctx.info(f"Reprint client {reprint_version} is ready.")
    ctx.st.update({
        "php_path": chosen.path,
        "php_version": chosen.version_text,
        "reprint_phar": str(phar),
        "reprint_version": reprint_version,
        "reprint_phar_sha256": sha256(phar),
    })


# -- stages 3-5 with the manual fallback ---------------------------------------

def stage_login(ctx: Context) -> None:
    """Log in to wp-admin of the source and check the account is an administrator."""
    if ctx.options.manual_source_steps or ctx.st.get("manual_source_steps"):
        raise SourceStepFailed("You chose to do the source steps yourself.", code="MANUAL_REQUESTED")
    site = source_site(ctx)
    site.discover()
    if site.is_logged_in():
        ctx.info("Still logged in to the source site from the previous run.")
    else:
        attempts = 0
        while True:
            attempts += 1
            try:
                site.login(ctx.st.get("admin_user"), admin_password(ctx))
                break
            except WrongPassword as error:
                ctx.st.set("failed_logins", int(ctx.st.get("failed_logins", 0)) + 1)
                ctx.secrets.admin_password = None
                if attempts >= constants.MAX_LOGIN_ATTEMPTS:
                    raise LoginFailed(
                        f"The source site did not accept the username and password ({attempts} tries). The tool stopped so the account does not get locked.",
                        "Check the login in a browser, then run the tool again. If you cannot log in automatically, run it with --manual to do the source steps yourself.",
                        code="WRONG_PASSWORD",
                    ) from error
                ctx.warn(f"{error.what} {error.next_step}")
                try:
                    user = ctx.prompter.ask_text(TextQuestion("admin_user_retry", "Administrator username", default=ctx.st.get("admin_user")))
                    ctx.st.set("admin_user", user.strip() or ctx.st.get("admin_user"))
                    admin_password(ctx)
                except NoAnswerAvailable:
                    raise LoginFailed(error.what, "Check the username and password, then run the tool again.", code="WRONG_PASSWORD") from error
    ctx.st.set("failed_logins", 0)
    site.check_administrator()
    ctx.info("Logged in to the source site as an administrator.")


def stage_plugin(ctx: Context) -> None:
    """Install and activate the Reprint plugin on the source, unless it is already active."""
    site = source_site(ctx)
    found = site.find_reprint_plugin()
    if found and found.status in ("active", "network-active"):
        ctx.info("The Reprint plugin is already active on the source site.")
        ctx.st.update({"reprint_plugin": found.plugin, "plugin_installed_by_tool": bool(ctx.st.get("plugin_installed_by_tool", False))})
        return
    if found:
        ctx.info("Activating the Reprint plugin on the source site...")
        site.activate(found)
        ctx.st.update({"reprint_plugin": found.plugin, "plugin_installed_by_tool": bool(ctx.st.get("plugin_installed_by_tool", False))})
        return
    ctx.info("Installing the Reprint plugin on the source site...")
    installed = site.install_and_activate()
    ctx.st.update({"reprint_plugin": installed.plugin, "plugin_installed_by_tool": True})
    ctx.info("Reprint plugin installed and activated on the source site.")


def ensure_key(ctx: Context, runner: ReprintRunner) -> tuple[str, str]:
    """Return (key id, Migration Token); create the key only if none exists."""
    metadata = runner.run(runner.base_args("pull-metadata", fs_root=False), stage=ctx.current_stage)
    record = metadata.data_record(lambda record: "key" in record and "hasCompletedOnce" in record)
    key = (record or {}).get("key")
    if key and key.get("publicKey"):
        return str(key["id"]), str(key["publicKey"])
    if ctx.st.get("key_id"):
        raise MigrationError(
            "The saved private key for this site is missing, but a key was already authorized on the source.",
            f"Restore {ctx.p.reprint_state} from a backup, or remove the old Migration Token in Tools → Reprint Server on the source and delete key_id from {ctx.p.state_file}.",
        )
    result = runner.run(runner.base_args("keygen", fs_root=False), stage=ctx.current_stage)
    if result.exit_code != 0 or not result.report or not result.report.get("public_key"):
        raise MigrationError(f"Reprint could not create a key ({result.error}).", f"See {ctx.p.log_file} for details and run the tool again.")
    key_id = str(result.report.get("key_id"))
    ctx.st.set("key_id", key_id)
    return key_id, str(result.report["public_key"])


def stage_authorize(ctx: Context) -> None:
    """Create the Migration Token and authorize it in Tools → Reprint Server."""
    runner = reprint_runner(ctx)
    if ctx.st.get("authorized") and (ctx.st.get("key_id") or runner.connection_token):
        ctx.info("The source site already trusts this server.")
        return
    site = source_site(ctx)
    _, page = site.reprint_page()
    if page.enroll_form is not None:
        key_id, public_key = ensure_key(ctx, runner)
        enrolled = site.enroll_migration_token(public_key)
        ctx.st.update({"key_id": key_id, "enrolled_key_id": enrolled or key_id, "authorized": True, "auth_scheme": "key"})
        ctx.info("Authorized this server on the source site with a Migration Token.")
        return
    if page.connection_token_form is not None:
        ctx.info("The source server has no OpenSSL, so it uses a shared connection token instead of a Migration Token.")
        token = runner.connection_token or random_tokens.token_hex(32)
        ctx.redactor.add(token)
        site.save_connection_token(token)
        LocalSystem.write_private_file(ctx.p.connection_token_file, token)
        ctx.st.update({"authorized": True, "auth_scheme": "connection-token"})
        ctx.info("Saved a new connection token on the source site.")
        return
    if page.managed_by_file:
        raise SourceStepFailed("The source host manages Reprint access itself (public-keys.php or secret.php), so the settings screen cannot authorize this server.", code="HOST_MANAGED")
    raise SourceStepFailed("The Tools → Reprint Server screen on the source did not show an authorization form.", code="NO_AUTH_FORM")


def verify_authorized(ctx: Context, runner: ReprintRunner) -> tuple[bool, str]:
    """Run preflight to see whether the source now accepts this server."""
    result = runner.run([*runner.base_args("preflight"), *[]], stage=ctx.current_stage, secret_args=runner.credential_args())
    if result.exit_code == 0:
        return True, ""
    return False, result.error or "the source did not answer"


def numbered(steps: list[str]) -> str:
    """Number the steps; lines starting with two spaces are notes to the step above."""
    lines, number = [], 0
    for step in steps:
        if step.startswith("  "):
            lines.append("   " + step.strip())
        else:
            number += 1
            lines.append(f"{number}. {step}")
    return "\n".join(lines)


def manual_fallback(ctx: Context, reason: str) -> None:
    """Show the Migration Token and click-by-click steps, then wait for the person."""
    runner = reprint_runner(ctx)
    ctx.emit(Status.WARNING, reason)
    site_url = ctx.st.get("source_url")
    admin = site_url.rstrip("/") + "/wp-admin/"
    _, public_key = ensure_key(ctx, runner)
    instructions = [
        f"Open {admin} in your browser and log in as an administrator.",
        "In the left menu choose Plugins → Add New Plugin (Add New on older sites).",
        "Type Reprint in the search box. On the Reprint plugin by WordPress Contributors, click Install Now, then Activate.",
        "  If the site does not allow installing plugins, ask your host or developer to install the Reprint plugin, then continue below.",
        "In the left menu choose Tools → Reprint Server.",
        "Under \"Authorize a client to copy this site\", click into the Migration Token box and paste the token shown below.",
        "Click Authorize client. The page should then say \"Client authorized\".",
        "If the screen shows a Connection token box instead: click Generate new token, then Save connection token, then click the eye icon to show it and copy it. You will paste it here.",
    ]
    ctx.emit(
        Status.ACTION_REQUIRED,
        "Please finish these steps on the source site yourself:",
        action_required=numbered(instructions),
        display_token=public_key,
    )
    for attempt in range(1, 6):
        try:
            answer = ctx.prompter.ask_secret(
                SecretQuestion(
                    "manual_done",
                    "Press Enter when the page says \"Client authorized\". If it asked for a connection token, paste that token and press Enter (it is not shown).",
                )
            )
        except NoAnswerAvailable as error:
            raise AuthorizationNeeded(
                "The source site has to authorize this server before the copy can start.",
                "Follow the steps above, then run the tool again; it continues from here.",
            ) from error
        answer = (answer or "").strip()
        if answer:
            ctx.redactor.add(answer)
            LocalSystem.write_private_file(ctx.p.connection_token_file, answer)
            runner.connection_token = answer
        ok, error_text = verify_authorized(ctx, runner)
        if ok:
            ctx.st.update({"authorized": True, "manual_source_steps": True, "auth_scheme": "connection-token" if answer else "key"})
            ctx.info("The source site now accepts this server.")
            return
        ctx.warn(f"The source site does not accept this server yet ({error_text}). Check that the plugin is active and that you clicked Authorize client.")
    raise AuthorizationNeeded(
        "The source site still does not accept this server.",
        "Check the steps above in the browser, then run the tool again; it continues from here.",
    )
