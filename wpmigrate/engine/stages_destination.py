"""Stages 7 and 8: prepare the destination, then deploy with Reprint."""

from __future__ import annotations

import os
import re
import secrets as random_tokens
import time
import urllib.parse
from pathlib import Path

from wpmigrate.engine import destination as dest
from wpmigrate.engine import wpconfig
from wpmigrate.engine.context import Context
from wpmigrate.engine.errors import Cancelled, DestinationFailed
from wpmigrate.engine.http import HttpClient
from wpmigrate.engine.php import parse_version
from wpmigrate.engine.prompts import Choice, ChoiceQuestion, ConfirmQuestion, SecretQuestion, TextQuestion, TypeToConfirmQuestion, WaitQuestion
from wpmigrate.engine.reprint import ReprintRunner
from wpmigrate.engine.stages_source import reprint_runner
from wpmigrate.engine.system import LocalSystem
from wpmigrate.engine.wpcli import WpCli


def tool_paths(ctx: Context) -> list[Path]:
    """Files of this tool itself, which are never listed or moved."""
    paths = [ctx.p.root]
    own = ctx.st.get("tool_location")
    if own:
        paths.append(Path(own))
    return paths


def wp_cli(ctx: Context) -> WpCli:
    return WpCli.locate(ctx.system, ctx.st.get("php_path"), ctx.p.tools, ctx.options.user_agent)


def db_password(ctx: Context) -> str:
    if ctx.secrets.db_password is None:
        ctx.secrets.db_password = ctx.prompter.ask_secret(SecretQuestion("db_password", "Database password"))
        ctx.redactor.add(ctx.secrets.db_password)
    return ctx.secrets.db_password


# -- 7a: folder -----------------------------------------------------------------

def choose_folder(ctx: Context) -> Path:
    state = ctx.st
    if state.get("dest_folder"):
        return Path(state.get("dest_folder"))
    start = Path(state.get("start_dir") or ctx.options.start_dir)
    home = ctx.system.home().resolve()
    while True:
        answer = ctx.prompter.ask_text(
            TextQuestion(
                "dest_folder",
                "Which folder should the copy go in?",
                default=str(start),
                help="You can also type a subfolder name such as blog, or a full path.",
            )
        ).strip()
        folder = Path(os.path.expanduser(answer or str(start)))
        if not folder.is_absolute():
            folder = start / folder
        folder = Path(os.path.normpath(folder))
        if not dest.is_inside(folder, home):
            ctx.warn(f"{folder} is not inside your home folder ({home}). Choose a folder inside it.")
            continue
        if dest.is_inside(folder, ctx.p.root):
            ctx.warn("That folder is the tool's private storage. Choose the website's folder instead.")
            continue
        if not folder.exists():
            create = ctx.prompter.confirm(ConfirmQuestion("create_dest_folder", f"{folder} does not exist yet. Create it?", default=True))
            if not create:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o755)
        if not LocalSystem.is_writable_dir(folder):
            ctx.warn(f"{folder} cannot be written to. Choose another folder or fix its permissions in the hosting panel.")
            continue
        break
    state.set("dest_folder", str(folder))
    # The address should match where the folder is served. Ask when the folder
    # picked here points somewhere else than the address chosen in step 1.
    new_url = state.get("new_site_url")
    suggested = dest.address_for_folder(folder)
    if suggested is None and folder != start and dest.is_inside(folder, start):
        relative = folder.relative_to(start).as_posix()
        if not new_url.rstrip("/").endswith("/" + relative):
            suggested = new_url.rstrip("/") + "/" + relative
    if suggested and suggested.rstrip("/") != new_url.rstrip("/") and (folder != start or state.get("new_site_url") == state.get("source_url")):
        use = ctx.prompter.confirm(ConfirmQuestion("use_subfolder_address", f"In that folder the site will be at {suggested}, not {new_url}. Use that address for the copy?", default=True))
        if use:
            state.set("new_site_url", suggested)
            ctx.info(f"The copied site will use the address {suggested}.")
    return folder


# -- 7b/7c: an existing site -----------------------------------------------------

def existing_site_address(ctx: Context, folder: Path) -> str | None:
    config_path = folder / "wp-config.php"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8", errors="replace")
        for constant in ("WP_HOME", "WP_SITEURL"):
            value = wpconfig.read_define(text, constant)
            if value:
                return value
    try:
        cli = wp_cli(ctx)
    except DestinationFailed:
        return None
    return cli.option_get(folder, "home")


def old_site_plan(ctx: Context, folder: Path) -> tuple[str, Path]:
    """Where a kept site would go: its new address and its new folder.

    It always moves to the subdomain old.<domain>, whose folder sits next to
    the domain's own folder, also when the site itself is in a subfolder.
    """
    domain = dest.site_domain(ctx.st.get("new_site_url"))
    return f"https://old.{domain}", dest.old_site_folder_guess(folder, domain)


def decide_existing_site(ctx: Context, folder: Path) -> None:
    state = ctx.st
    if state.get("existing_action"):
        return
    if not dest.has_wordpress(folder):
        state.set("existing_action", "none")
        return
    address = existing_site_address(ctx, folder) or "(address unknown)"
    config_text = (folder / "wp-config.php").read_text(encoding="utf-8", errors="replace") if (folder / "wp-config.php").exists() else ""
    database = wpconfig.read_database_settings(config_text) if config_text else None
    domain = dest.site_domain(state.get("new_site_url"))
    keep_label = f"Keep it: move it to the subdomain old.{domain} and put the copy here."
    ctx.note(f"There is already a WordPress site in {folder}. Its address is {address}.")
    action = ctx.prompter.ask_choice(
        ChoiceQuestion(
            "existing_site",
            "What should happen to the site that is there now?",
            [
                Choice("remove", "Remove it and put the copy in its place."),
                Choice("keep", keep_label),
                Choice("cancel", "Cancel."),
            ],
            default="cancel",
        )
    )
    if action == "cancel":
        raise Cancelled("Stopped without changing anything in the destination folder.", "Run the tool again when you have decided; the download is kept.")
    record = {"existing_action": action, "existing_site_url": address}
    if database:
        record["existing_db"] = {"host": database.host, "name": database.name, "user": database.user, "table_prefix": database.table_prefix}
    if action == "remove":
        confirmed = ctx.prompter.type_to_confirm(
            TypeToConfirmQuestion("confirm_remove_domain", f"This permanently deletes the site in {folder}. Type the domain {domain} to confirm", domain)
        )
        if not confirmed:
            raise Cancelled("The domain was not typed correctly, so nothing was removed.", "Run the tool again to choose again.")
    state.update(record)


def _old_site_folder(ctx: Context, folder: Path) -> Path:
    state = ctx.st
    if state.get("old_site_folder"):
        return Path(state.get("old_site_folder"))
    old_url, guess = old_site_plan(ctx, folder)
    answer = ctx.prompter.ask_text(
        TextQuestion("old_site_folder", f"The current site will be moved to {guess} so it opens at {old_url}. Press Enter to use that folder, or type another full path", default=str(guess))
    ).strip()
    target = Path(os.path.normpath(os.path.expanduser(answer or str(guess))))
    return target


def keep_existing_site(ctx: Context, folder: Path) -> None:
    """Move the existing site to old.<domain> and repoint it there (7c)."""
    state = ctx.st
    if state.destructive_done("old_site_configured"):
        return
    domain = dest.site_domain(state.get("new_site_url"))
    old_url, _ = old_site_plan(ctx, folder)
    home = ctx.system.home().resolve()
    target = _old_site_folder(ctx, folder)
    while not state.destructive_done("old_site_moved") and not state.destructive_started("old_site_moved"):
        if not dest.is_inside(target, home) or dest.is_inside(target, folder) or dest.is_inside(folder, target):
            ctx.warn(f"{target} cannot be used: it must be inside your home folder and separate from {folder}.")
            target = Path(ctx.prompter.ask_text(TextQuestion("old_site_folder_other", "Full path of the folder for the old site")).strip())
            continue
        if not target.is_dir():
            ctx.emit(
                "action_required",
                f"Before the old site can be moved, please create the subdomain old.{domain} in your hosting panel. This tool cannot create it for you.",
                action_required=f"When it is created, the folder {target} should appear. Press Enter when you have done this.",
            )
            ctx.prompter.wait_for_person(WaitQuestion("subdomain_created", "Press Enter when the subdomain has been created."))
            while not target.is_dir():
                ctx.warn(f"The folder {target} does not exist yet.")
                choice = ctx.prompter.ask_choice(
                    ChoiceQuestion(
                        "subdomain_missing",
                        "What would you like to do?",
                        [
                            Choice("check", "Check again (I have created the subdomain now)."),
                            Choice("path", "Enter a different folder path."),
                            Choice("create", "Continue without the subdomain: create the folder anyway."),
                            Choice("cancel", "Cancel."),
                        ],
                        default="check",
                    )
                )
                if choice == "cancel":
                    raise Cancelled("Stopped before moving anything.", "Create the subdomain old." + domain + " in your hosting panel, then run the tool again.")
                if choice == "path":
                    target = Path(os.path.normpath(os.path.expanduser(ctx.prompter.ask_text(TextQuestion("old_site_folder_other", "Full path of the folder for the old site")).strip())))
                    break
                if choice == "create":
                    ctx.warn(f"The old site will not be reachable at {old_url} until the subdomain old.{domain} points at {target}.")
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, 0o755)
            if not target.is_dir():
                continue
        if dest.has_wordpress(target):
            ctx.warn(f"{target} already contains a WordPress site. The old site cannot be moved there.")
            choice = ctx.prompter.ask_choice(ChoiceQuestion("old_folder_has_wordpress", "What would you like to do?", [Choice("path", "Enter a different folder path."), Choice("cancel", "Cancel.")], default="cancel"))
            if choice == "cancel":
                raise Cancelled("Stopped before moving anything.", "Choose an empty folder for the old site and run the tool again.")
            target = Path(ctx.prompter.ask_text(TextQuestion("old_site_folder_other", "Full path of the folder for the old site")).strip())
            continue
        # Files already in that folder only matter when they clash with the
        # old site's own files, or would be shown instead of it (index.html).
        leftovers = dest.list_entries(target)
        moving, _ = dest.split_entries(folder, exclude=tool_paths(ctx))
        clashing = [name for name in leftovers if name in moving]
        shadowing = [name for name in leftovers if dest.shadows_wordpress(name) and name not in clashing]
        in_the_way = clashing + shadowing
        if in_the_way:
            listing = ", ".join(in_the_way[:20]) + (" ..." if len(in_the_way) > 20 else "")
            choices = [Choice("remove", "Remove them so the old site can go there.")]
            if not clashing:
                choices.append(Choice("leave", "Leave them. (A page named index.html may then show instead of the old site.)"))
            choices += [Choice("path", "Use a different folder for the old site."), Choice("cancel", "Cancel.")]
            what = "has files with the same names as the old site's" if clashing else "has a placeholder page"
            choice = ctx.prompter.ask_choice(ChoiceQuestion("old_folder_files", f"{target} already {what}: {listing}. What would you like to do?", choices))
            if choice == "cancel":
                raise Cancelled("Stopped before moving anything.", "Run the tool again when you have decided; the download is kept.")
            if choice == "path":
                target = Path(os.path.normpath(os.path.expanduser(ctx.prompter.ask_text(TextQuestion("old_site_folder_other", "Full path of the folder for the old site")).strip())))
                continue
            if choice == "remove":
                for name in in_the_way:
                    LocalSystem.remove(target / name)
                ctx.info(f"Removed {listing} from {target}.")
        staying = [name for name in dest.list_entries(target)]
        if staying:
            ctx.info(f"Left {', '.join(staying[:8])}{' ...' if len(staying) > 8 else ''} in {target} as {'it is' if len(staying) == 1 else 'they are'}.")
        state.set("old_site_folder", str(target))
        break

    target = Path(state.get("old_site_folder"))
    wordpress_entries, _ = dest.split_entries(folder, exclude=tool_paths(ctx))
    if not state.destructive_done("old_site_moved"):
        state.record_destructive_started("old_site_moved", source=str(folder), target=str(target))
        moved = state.get("old_site_moved_entries", []) or []
        for name in wordpress_entries:
            if (target / name).exists() and not (folder / name).exists():
                continue
            LocalSystem.move(folder / name, target / name)
            moved.append(name)
            state.set("old_site_moved_entries", moved)
        state.record_destructive("old_site_moved", source=str(folder), target=str(target), entries=moved)
        ctx.info(f"Moved the existing site to {target}.")

    # The old site now sits at the top of its subdomain, wherever it was before.
    old_htaccess = target / ".htaccess"
    if old_htaccess.is_file():
        text = old_htaccess.read_text(encoding="utf-8", errors="replace")
        fixed = dest.set_rewrite_base(text, "/")
        if fixed != text:
            LocalSystem.write_file(old_htaccess, fixed, 0o644)

    cli = wp_cli(ctx)
    old_address = state.get("existing_site_url") or ""
    steps = [
        ["option", "update", "home", old_url],
        ["option", "update", "siteurl", old_url],
    ]
    for args in steps:
        result = cli.run(target, args)
        if result.returncode != 0:
            raise DestinationFailed(f"Changing the old site's address failed: {result.stderr.strip() or result.stdout.strip()}", f"The old site's files are in {target}. Check its wp-config.php, then run the tool again.")
    if old_address.startswith("http"):
        variants = {old_address.rstrip("/")}
        parts = urllib.parse.urlsplit(old_address)
        variants.add(urllib.parse.urlunsplit(("http" if parts.scheme == "https" else "https", parts.netloc, parts.path, "", "")).rstrip("/"))
        for variant in sorted(variants):
            result = cli.run(target, ["search-replace", variant, old_url, "--all-tables-with-prefix", "--skip-columns=guid", "--report-changed-only"])
            if result.returncode != 0:
                raise DestinationFailed(f"Updating addresses in the old site's database failed: {result.stderr.strip()}", f"The old site's files are in {target}; run the tool again to retry.")
    cli.run(target, ["option", "update", "blog_public", "0"])
    cli.run(target, ["cache", "flush"])
    cli.run(target, ["rewrite", "flush"])
    check = cli.run(target, ["option", "get", "home"], skip_plugins=False)
    if check.returncode != 0 or check.stdout.strip().rstrip("/") != old_url:
        raise DestinationFailed(
            f"The old site in {target} does not start correctly after the move: {(check.stderr or check.stdout).strip()[:300]}",
            "Nothing else was changed. Look at the old site's files and database, then run the tool again.",
        )
    ctx.info(f"The old site starts correctly with the address {old_url}.")
    answered, detail = check_address(ctx, old_url)
    state.update({"old_site_url": old_url, "old_site_answered": answered, "old_site_answer_detail": detail})
    if answered:
        ctx.info(f"{old_url} answers ({detail}).")
    else:
        ctx.warn(f"{old_url} does not answer yet ({detail})." + " That is normal until the subdomain exists and its DNS has updated.")
    state.record_destructive("old_site_configured", folder=str(target), url=old_url)


def check_address(ctx: Context, url: str) -> tuple[bool, str]:
    client = HttpClient(cookie_file=None, user_agent=ctx.options.user_agent, allow_http=ctx.options.allow_http, admin_delay=0, max_attempts=1, log=ctx.log, timeout=20)
    try:
        response = client.get(url + "/")
    except Exception as error:  # noqa: BLE001 - any failure means "did not answer"
        return False, str(getattr(error, "what", error))
    return True, f"HTTP {response.status}"


# -- 7d: database -----------------------------------------------------------------

def choose_database(ctx: Context) -> None:
    state = ctx.st
    if state.get("dest_db_confirmed"):
        return
    php = state.get("php_path")
    prefix = state.get("source_table_prefix") or "wp_"
    action = state.get("existing_action")
    existing = state.get("existing_db")
    defaults = {"host": "localhost", "name": "", "user": ""}
    if action == "remove" and existing:
        reuse = ctx.prompter.confirm(ConfirmQuestion("reuse_existing_db", f"Use the database of the site being removed ({existing['name']} on {existing['host']})?", default=True))
        if reuse:
            defaults = {"host": existing["host"], "name": existing["name"], "user": existing["user"]}
            old_config = Path(state.get("dest_folder")) / "wp-config.php"
            values = wpconfig.read_database_settings(old_config.read_text(encoding="utf-8", errors="replace")) if old_config.exists() else None
            if values:
                ctx.secrets.db_password = values.password
                ctx.redactor.add(values.password)
    if action == "keep" and existing:
        ctx.note(
            f"Your old site keeps its own database ({existing['name']}); I only updated its address in there, so nothing else needs to change for it. "
            "The copy needs a database of its own, so the two sites never overwrite each other. "
            "If you do not have one yet, create an empty database and a user for it in your hosting panel first."
        )
    else:
        ctx.note("Next, the database for the copy. If you do not have one yet, create an empty database and a user for it in your hosting panel first.")
    while True:
        host = ctx.prompter.ask_text(TextQuestion("db_host", "Database host", default=defaults["host"] or "localhost")).strip()
        name = ctx.prompter.ask_text(TextQuestion("db_name", "Database name", default=defaults["name"] or None)).strip()
        user = ctx.prompter.ask_text(TextQuestion("db_user", "Database user", default=defaults["user"] or None)).strip()
        if not (host and name and user):
            ctx.warn("Host, database name and user are all needed.")
            continue
        if action == "keep" and existing and dest.normalize_db_host(existing["host"]) == dest.normalize_db_host(host) and existing["name"] == name and existing["table_prefix"] == prefix:
            ctx.warn(
                f"The old site keeps using the database {name} with the table prefix {prefix}, the same prefix the copied site uses. "
                "Sharing them would overwrite the old site. Create a separate database for the copied site in your hosting panel and enter it here."
            )
            ctx.secrets.db_password = None
            defaults = {"host": host, "name": "", "user": ""}
            continue
        password = db_password(ctx)
        check = dest.check_database(ctx.system, php, host=host, name=name, user=user, password=password, prefix=prefix)
        if not check.ok:
            ctx.warn(dest.explain_database_error(check) + " Please enter the details again.")
            ctx.secrets.db_password = None
            defaults = {"host": host, "name": name, "user": user}
            continue
        break
    if check.prefix_tables:
        ctx.warn(f"The database {name} already has {len(check.prefix_tables)} table(s) starting with {prefix} ({', '.join(check.prefix_tables[:6])}{' ...' if len(check.prefix_tables) > 6 else ''}). They will be replaced by the copied site's tables.")
    else:
        ctx.info(f"Connected to the database {name} (MySQL {check.version}).")
    agree = ctx.prompter.confirm(ConfirmQuestion("confirm_replace_tables", f"Ready to import into the database {name}? Any tables that start with {prefix} will be replaced.", default=False))
    if not agree:
        raise Cancelled("Stopped before changing the database.", "Run the tool again to choose a different database.")
    state.update({"dest_db": {"host": host, "name": name, "user": user, "table_prefix": prefix}, "dest_db_confirmed": True})


# -- 7e: other files ----------------------------------------------------------------

def decide_other_files(ctx: Context, folder: Path) -> None:
    state = ctx.st
    if state.get("other_files_action"):
        return
    wordpress_entries, other = dest.split_entries(folder, exclude=tool_paths(ctx))
    if state.get("existing_action") == "none" and wordpress_entries:
        other = wordpress_entries + other
    # What the hosting account put there itself stays, without asking.
    other = [name for name in other if not dest.is_host_entry(name)]
    if not other:
        state.update({"other_files_action": "none", "other_files": []})
        return
    listing = "\n".join(f"  {name}" for name in other[:40]) + ("\n  ..." if len(other) > 40 else "")
    ctx.note(f"The folder {folder} also contains files that are not part of a WordPress site:\n{listing}")
    action = ctx.prompter.ask_choice(
        ChoiceQuestion(
            "other_files",
            "What should happen to them?",
            [
                Choice("backup", f"Move them to a dated backup folder in {ctx.p.backups}."),
                Choice("remove", "Remove them."),
                Choice("leave", "Leave them where they are."),
            ],
            default="backup",
        )
    )
    state.update({"other_files_action": action, "other_files": other})


def stage_destination(ctx: Context) -> None:
    """Only now set up the destination: folder, existing site, database, other files."""
    folder = choose_folder(ctx)
    decide_existing_site(ctx, folder)
    if ctx.st.get("existing_action") == "keep":
        try:
            keep_existing_site(ctx, folder)
        except Cancelled:
            if not ctx.st.destructive_started("old_site_moved") and not ctx.st.destructive_done("old_site_moved"):
                # Nothing was moved: let the next run ask again what to do.
                ctx.st.update({"existing_action": None, "old_site_folder": None})
            raise
    choose_database(ctx)
    decide_other_files(ctx, folder)


# -- stage 8 ---------------------------------------------------------------------------

def _clear_folder(ctx: Context, folder: Path) -> None:
    state = ctx.st
    action = state.get("other_files_action")
    other = state.get("other_files") or []
    if other and action != "leave" and not state.destructive_done("other_files"):
        state.record_destructive_started("other_files", action=action)
        if action == "backup":
            backup = ctx.p.backups / time.strftime("%Y%m%d-%H%M%S")
            LocalSystem.make_private_dir(backup)
            for name in other:
                if (folder / name).exists() or (folder / name).is_symlink():
                    LocalSystem.move(folder / name, backup / name)
            state.record_destructive("other_files", action=action, backup=str(backup), entries=other)
            ctx.info(f"Moved {len(other)} other item(s) to {backup}.")
        else:
            for name in other:
                if (folder / name).exists() or (folder / name).is_symlink():
                    LocalSystem.remove(folder / name)
            state.record_destructive("other_files", action=action, entries=other)
            ctx.info(f"Removed {len(other)} other item(s) from {folder}.")
    if state.get("existing_action") == "remove" and not state.destructive_done("existing_site_removed"):
        state.record_destructive_started("existing_site_removed", folder=str(folder))
        wordpress_entries, _ = dest.split_entries(folder, exclude=tool_paths(ctx))
        for name in wordpress_entries:
            LocalSystem.remove(folder / name)
        state.record_destructive("existing_site_removed", folder=str(folder), entries=wordpress_entries)
        ctx.info(f"Removed the previous site from {folder}.")


def _apply_database(ctx: Context, runner: ReprintRunner) -> None:
    state = ctx.st
    database = state.get("dest_db")
    host, port = dest.split_host_port(database["host"])
    target = [
        "--target-engine=mysql",
        f"--target-host={host}",
        f"--target-user={database['user']}",
        f"--target-db={database['name']}",
    ]
    if port:
        target.append(f"--target-port={port}")
    source_home = state.get("source_home_url") or state.get("source_url")
    new_url = state.get("new_site_url")
    steps = dest.address_rewrite_steps(runner.api_url, new_url) if new_url.rstrip("/") != (source_home or "").rstrip("/") else [[]]
    secret = [f"--target-pass={db_password(ctx)}"]
    if not state.get("db_applied"):
        ctx.info("Importing the database" + (f" and changing the address to {new_url}" if steps[0] else "") + "...")
        result = runner.run_to_completion([*runner.base_args("db-apply"), *target, *steps[0]], stage="deploy", secret_args=secret, progress_label="Importing the database")
        if result.exit_code != 0:
            raise DestinationFailed(
                f"Importing the database failed: {result.error or 'no details'}",
                f"Check that the database user may create tables, then run the tool again. Details are in {ctx.p.log_file}.",
                code=result.error_code,
            )
        state.set("db_applied", True)
        state.record_destructive("tables_replaced", database=database["name"], prefix=database["table_prefix"])
    if len(steps) > 1 and not state.get("db_address_finished"):
        # Second half of a move into a subfolder of the same domain (see address_rewrite_steps).
        result = runner.run_to_completion([*runner.base_args("db-rewrite-urls", fs_root=False), *target, *steps[1]], stage="deploy", secret_args=secret, progress_label="Updating the address")
        if result.exit_code != 0:
            raise DestinationFailed(
                f"Updating the address in the database failed: {result.error or 'no details'}",
                f"Run the tool again to retry. Details are in {ctx.p.log_file}.",
                code=result.error_code,
            )
        state.set("db_address_finished", True)


def _merge_folder(source: Path, target: Path, *, copy: bool) -> None:
    """Put everything from source into target; files already in target win."""
    import shutil
    for name in sorted(os.listdir(source)):
        item, existing = source / name, target / name
        if item.is_dir() and not item.is_symlink() and existing.is_dir() and not existing.is_symlink():
            _merge_folder(item, existing, copy=copy)
        elif not existing.exists() and not existing.is_symlink():
            if copy:
                shutil.copytree(item, existing, symlinks=True) if item.is_dir() and not item.is_symlink() else shutil.copy2(item, existing, follow_symlinks=False)
            else:
                LocalSystem.move(item, existing)


def _place_files(ctx: Context, runner: ReprintRunner, folder: Path) -> None:
    """Assemble the standard layout with Reprint, then move the real files in."""
    state = ctx.st
    if state.get("files_placed"):
        return
    layout = ctx.options.file_layout if not state.get("file_layout") else state.get("file_layout")
    state.set("file_layout", layout)
    # A "link" layout (symlinks from the site folder into the download) was
    # tried and rejected: the download lives in the 0700 private directory, so
    # the web server cannot follow the links and every page answers 403.
    assemble = ctx.p.assemble
    if not state.get("assembled"):
        if assemble.exists():
            LocalSystem.remove(assemble)
        args = [*runner.base_args("flat-docroot"), f"--flatten-to={assemble}"]
        if layout == "copy":
            args.append("--core-files=copy")
        result = runner.run(args, stage="deploy")
        if result.exit_code != 0:
            raise DestinationFailed(f"Assembling the site from the download failed: {result.error}", "Run the tool again; nothing was lost.")
        state.set("assembled", True)
    download_root = ctx.p.download.resolve()
    placed = 0

    # Files that were in the folder before and stay there (host files, or "leave them").
    kept_names = {name for name in dest.list_entries(folder, exclude=tool_paths(ctx))}

    def place(link_dir: Path, target_dir: Path) -> None:
        nonlocal placed
        for name in sorted(os.listdir(link_dir)):
            entry = link_dir / name
            target = target_dir / name
            if entry.is_symlink():
                source = Path(os.path.realpath(entry))
                if not source.exists() and target.exists():
                    continue  # moved by an earlier, interrupted run
                if not dest.is_inside(source, download_root):
                    raise DestinationFailed(f"The downloaded file {name} points outside the download folder.", "Run the tool again with a fresh download.")
                if target.is_dir() and not target.is_symlink() and source.is_dir() and not source.is_symlink():
                    # A folder that was left in place (such as .well-known): add the copy's files to it.
                    _merge_folder(source, target, copy=layout == "copy")
                    continue
                if target.is_file() and target_dir == folder and name in kept_names:
                    ctx.log(f"kept the existing {name}; the copy's {name} was not placed")
                    continue
                if target.exists() or target.is_symlink():
                    raise DestinationFailed(f"{target} already exists, so the copied {name} cannot be put there.", "Move that file away and run the tool again.")
                if layout == "copy":
                    import shutil
                    if source.is_dir():
                        shutil.copytree(source, target, symlinks=True)
                    else:
                        shutil.copy2(source, target)
                else:
                    LocalSystem.move(source, target)
                placed += 1
            elif entry.is_dir():
                target.mkdir(exist_ok=True)
                place(entry, target)
            elif entry.is_file():
                if not target.exists():
                    LocalSystem.move(entry, target)
                    placed += 1

    place(assemble, folder)
    LocalSystem.make_web_readable(folder)
    state.update({"files_placed": True})
    ctx.info(f"The files are in place in {folder}.")


def _write_config(ctx: Context, folder: Path) -> None:
    state = ctx.st
    config = folder / "wp-config.php"
    database = state.get("dest_db")
    settings = wpconfig.DatabaseSettings(database["host"], database["name"], database["user"], db_password(ctx), database["table_prefix"])
    if config.exists():
        text = config.read_text(encoding="utf-8", errors="replace")
    else:
        sample = folder / "wp-config-sample.php"
        if not sample.exists():
            raise DestinationFailed("The copied site has no wp-config.php and no wp-config-sample.php.", "Check your original site's files, then run the tool again.")
        text = sample.read_text(encoding="utf-8", errors="replace")
        for key in ("AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY", "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT"):
            text = wpconfig.set_define(text, key, random_tokens.token_urlsafe(48))
        ctx.warn("Your original site keeps wp-config.php outside its web folder, so a new one was created from the sample. Logged-in users will have to log in again.")
    text = wpconfig.apply_destination(text, db=settings, site_url=state.get("new_site_url"))
    mode = 0o644
    LocalSystem.write_file(config, text, mode)
    ctx.info("Connected the copy to its database (wp-config.php).")


def _destination_php_version(ctx: Context, folder: Path) -> tuple[str | None, str]:
    """PHP version that serves the site here, and how it was found."""
    for directory in [folder, *folder.parents]:
        htaccess = directory / ".htaccess"
        if htaccess.is_file():
            match = re.search(r"(?im)^\s*(?:AddHandler|SetHandler|AddType)\s+[^\n]*php(\d)\.?(\d)", htaccess.read_text(encoding="utf-8", errors="replace"))
            if match:
                return f"{match.group(1)}.{match.group(2)}", f"the PHP handler in {htaccess}"
        if directory == ctx.system.home():
            break
    state = ctx.st
    new_url = state.get("new_site_url")
    if new_url.rstrip("/") != (state.get("source_url") or "").rstrip("/"):
        client = HttpClient(cookie_file=None, user_agent=ctx.options.user_agent, allow_http=ctx.options.allow_http, admin_delay=0, max_attempts=1, log=ctx.log, timeout=20)
        try:
            response = client.get(new_url + "/")
            powered = response.header("x-powered-by")
            match = re.search(r"PHP/(\d+\.\d+(?:\.\d+)?)", powered)
            if match:
                return match.group(1), f"the web server's answer at {new_url}"
        except Exception:  # noqa: BLE001
            pass
    return state.get("php_version"), "the command-line PHP on this server (the website may use a different version; check your hosting panel)"


def _fix_rewrite_rules(ctx: Context, folder: Path) -> None:
    """Make the copied .htaccess rules match the new address's path (for example /blog/)."""
    htaccess = folder / ".htaccess"
    if not htaccess.is_file():
        return
    path = urllib.parse.urlsplit(ctx.st.get("new_site_url")).path or "/"
    text = htaccess.read_text(encoding="utf-8", errors="replace")
    fixed = dest.set_rewrite_base(text, path)
    if fixed != text:
        LocalSystem.write_file(htaccess, fixed, 0o644)
        ctx.info(f"Adjusted .htaccess so links work under {path}.")


def stage_deploy(ctx: Context) -> None:
    """Import the database, put the files in place, configure and check WordPress."""
    state = ctx.st
    folder = Path(state.get("dest_folder"))
    runner = reprint_runner(ctx)
    _clear_folder(ctx, folder)
    _apply_database(ctx, runner)
    _place_files(ctx, runner, folder)
    _write_config(ctx, folder)
    _fix_rewrite_rules(ctx, folder)
    if not state.get("runtime_applied"):
        # apply-runtime only generates configuration for php-builtin, nginx-fpm
        # and playground-cli ("none" is rejected despite its help text). The
        # site here is served by the host's own web server, so the generated
        # router stays in the private directory unused and is deleted (it holds
        # the database password); what matters is the source-host analysis and
        # the host-plugin cleanup it performs on the site folder.
        result = runner.run(
            [*runner.base_args("apply-runtime", fs_root=False), f"--flat-document-root={folder}", f"--output-dir={ctx.p.runtime}", "--runtime=php-builtin"],
            stage="deploy",
        )
        if result.exit_code != 0:
            raise DestinationFailed(f"Preparing the site for this server failed: {result.error}", "Run the tool again to retry.")
        summary = result.data_record(lambda record: record.get("command") == "apply-runtime" and "webhost" in record) or {}
        webhost = summary.get("webhost") or "other"
        state.set("source_webhost", webhost)
        if webhost != "other":
            ctx.warn(f"Your original site was hosted on a managed platform ({webhost}). Its platform-only plugins were removed from the copy; check the site carefully.")
        for generated in ("runtime.php", "start.sh"):
            if (ctx.p.runtime / generated).exists():
                (ctx.p.runtime / generated).unlink()
        result = runner.run(["post-process", runner.api_url, f"--fs-root={folder}", f"--state-dir={ctx.p.reprint_state}", "--tasks=disable-hosting-plugins"], stage="deploy")
        if result.exit_code != 0:
            raise DestinationFailed(f"Removing the old host's own plugins failed: {result.error or (result.stderr_tail[-1:] or [''])[0]}", "Run the tool again to retry.")
        state.set("runtime_applied", True)
    result = runner.run(["recover", f"--fs-root={folder}"], stage="deploy")
    report = result.data_record(lambda record: "disabled_plugins" in record) or {}
    disabled = report.get("disabled_plugins") or []
    if disabled:
        names = ", ".join(str(item.get("plugin", item)) if isinstance(item, dict) else str(item) for item in disabled)
        ctx.warn(f"These plugins stopped WordPress from starting and were switched off: {names}. Their settings are kept.")
    if result.exit_code != 0:
        raise DestinationFailed(
            f"WordPress does not start in {folder}: {report.get('error') or result.error or (result.stderr_tail[-1:] or ['unknown error'])[0]}",
            "Check the database details and the site's PHP version in your hosting panel, then run the tool again.",
        )
    state.set("disabled_plugins", disabled)
    ctx.info("WordPress starts up fine in its new home.")
    new_url = state.get("new_site_url")
    if new_url.rstrip("/") != (state.get("source_home_url") or state.get("source_url") or "").rstrip("/"):
        # A different address than the source's: it can be opened right away.
        ctx.info(f"Opening {new_url} to check it...")
        answered, detail = check_address(ctx, new_url)
        good = answered and detail[5:6] in ("2", "3")
        state.update({"new_site_answers": good, "new_site_answer_detail": detail})
        if good:
            ctx.info(f"The copy is live at {new_url}.")
        elif answered:
            ctx.warn(f"{new_url} answered with an error ({detail}). Open it in a browser; if it stays broken, check the site's PHP version and the error log in your hosting panel.")
        else:
            ctx.warn(f"{new_url} does not answer yet ({detail}). That is normal until its domain points to this hosting account.")
    source_php = state.get("source_php_version")
    destination_php, how = _destination_php_version(ctx, folder)
    state.update({"destination_php_version": destination_php, "destination_php_source": how})
    if source_php and destination_php and parse_version(source_php)[:2] != parse_version(destination_php)[:2]:
        ctx.warn(
            f"The old site ran on PHP {'.'.join(map(str, parse_version(source_php)[:2]))}, but here it is PHP {'.'.join(map(str, parse_version(destination_php)[:2]))} (from {how}). "
            "Older plugins and themes can break on a different PHP version. If something looks wrong, choose the same PHP version in your hosting panel."
        )
