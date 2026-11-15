"""Stages 9 and 10: tidy up the source, then summarize."""

from __future__ import annotations

from pathlib import Path

from wpmigrate.engine.destination import site_domain
from wpmigrate.engine.context import Context
from wpmigrate.engine.errors import MigrationError, SourceStepFailed
from wpmigrate.engine.events import Status
from wpmigrate.engine.prompts import ConfirmQuestion, NoAnswerAvailable
from wpmigrate.engine.source import WrongPassword
from wpmigrate.engine.stages_source import admin_password, source_site
from wpmigrate.engine.system import LocalSystem


def _manual_cleanup_steps(ctx: Context, reason: str) -> None:
    state = ctx.st
    admin = state.get("source_url").rstrip("/") + "/wp-admin/"
    steps = [f"Open {admin} and log in."]
    if state.get("auth_scheme") == "connection-token":
        steps.append("Go to Tools → Reprint Server, empty the Connection token box and click Save connection token.")
    else:
        steps.append(f"Go to Tools → Reprint Server, open Manage client access, and click Remove Migration Token next to the ID {state.get('enrolled_key_id') or state.get('key_id')}.")
    if state.get("plugin_installed_by_tool"):
        steps.append("Go to Plugins, click Deactivate under Reprint, then Delete.")
    ctx.emit(Status.ACTION_REQUIRED, f"The source site could not be tidied up automatically ({reason}). Please do this yourself:", action_required="\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)))
    state.set("source_cleanup", "manual")


def stage_cleanup_source(ctx: Context) -> None:
    """Remove this server's access from the source and the plugin if we installed it."""
    state = ctx.st
    what = "remove this server's access" + (" and the transfer plugin" if state.get("plugin_installed_by_tool") else "")
    try:
        agree = ctx.prompter.confirm(ConfirmQuestion("cleanup_source", f"The copy is finished. Tidy up the source site now ({what})?", default=True))
    except NoAnswerAvailable:
        agree = True
    if not agree:
        ctx.note("The source site was left as it is. You can remove this server's access later under Tools → Reprint Server on the source site.")
        state.set("source_cleanup", "skipped")
        return
    site = source_site(ctx)
    try:
        site.discover()
        if not site.is_logged_in():
            try:
                site.login(state.get("admin_user"), admin_password(ctx))
            except WrongPassword:
                ctx.secrets.admin_password = None
                site.login(state.get("admin_user"), admin_password(ctx))
        if state.get("auth_scheme") == "connection-token":
            site.clear_connection_token()
            ctx.info("Cleared the connection token on the source site.")
        else:
            key_id = state.get("enrolled_key_id") or state.get("key_id")
            removed = site.remove_migration_token(key_id) if key_id else False
            ctx.info("Removed this server's access from the source site." if removed else "This server's access was already gone from the source site.")
        if state.get("plugin_installed_by_tool") and state.get("reprint_plugin"):
            site.deactivate_and_delete(state.get("reprint_plugin"))
            ctx.info("Removed the transfer plugin from the source site (this tool had installed it).")
        state.set("source_cleanup", "done")
        site.client.clear_cookies()
    except (SourceStepFailed, MigrationError, NoAnswerAvailable) as error:
        _manual_cleanup_steps(ctx, getattr(error, "reason", None) or getattr(error, "what", None) or str(error))


def stage_summary(ctx: Context) -> None:
    """Delete temporary data and tell the person what to check next."""
    state = ctx.st
    if not ctx.options.keep_downloads:
        if ctx.p.sql_dump.exists():
            ctx.p.sql_dump.unlink()
        if ctx.p.cookie_file.exists():
            ctx.p.cookie_file.unlink()
        if ctx.p.assemble.exists():
            LocalSystem.remove(ctx.p.assemble)
        ctx.info("Deleted the downloaded database file and the login cookies.")
        if state.get("source_cleanup") == "done":
            # Reprint's own saved state keeps the destination database password
            # in plain text. Once the source no longer trusts our key, nothing
            # in it is needed, so keep only its audit log.
            audit = ctx.p.reprint_state / "audit.log"
            if audit.exists():
                LocalSystem.move(audit, ctx.p.root / "reprint-audit.log")
            LocalSystem.remove(ctx.p.reprint_state)
            LocalSystem.remove(ctx.p.download)
            ctx.info("Deleted the temporary working files.")
    folder = state.get("dest_folder")
    new_url = state.get("new_site_url")
    lines = [
        f"The copied site is in {folder} and uses the address {new_url}.",
        f"Its database is {state.get('dest_db', {}).get('name')} on {state.get('dest_db', {}).get('host')}.",
    ]
    if new_url.rstrip("/") == (state.get("source_home_url") or state.get("source_url")).rstrip("/"):
        lines.append("The domain still points to the old server. To make visitors see the copy, switch the domain's DNS (or its nameservers) to this hosting account. Until then you can preview it with your hosting panel's preview feature or a hosts-file entry.")
    elif state.get("new_site_answers"):
        lines.append(f"It is online now. Open it: {new_url}")
    else:
        lines.append(f"It does not answer at {new_url} yet. Make sure the domain {site_domain(new_url)} points to this hosting account.")
    lines.append("Check: the home page, a few posts and pages, images, the admin login, contact forms, and any shop checkout.")
    lines.append("Careful: the copy is a live WordPress site. It may send e-mails (to customers too) and take real payments, and scheduled tasks will run. Switch off what must not run twice.")
    disabled = state.get("disabled_plugins") or []
    if disabled:
        lines.append(f"{len(disabled)} plugin(s) were switched off because WordPress could not start with them; check them in Plugins.")
    if state.get("existing_action") == "keep":
        answered = "answered" if state.get("old_site_answered") else "did not answer yet"
        lines.append(f"The old site is kept at {state.get('old_site_url')}, in the folder {state.get('old_site_folder')}. When checked, {state.get('old_site_url')} {answered} ({state.get('old_site_answer_detail')}). Search engines are asked not to index it.")
    other = state.data["destructive"].get("other_files")
    if other and other.get("backup"):
        lines.append(f"Files that were in the folder before were moved to {other['backup']}.")
    if state.get("source_cleanup") in ("skipped", "manual"):
        lines.append("The source site still has the transfer plugin (Reprint) and this server's access. Remove them when you no longer need them.")
    if ctx.p.reprint_state.exists():
        lines.append(f"The working files in {ctx.p.reprint_state} include the new database password (readable only by your account). Delete that folder when you no longer need to copy the site again.")
    lines.append(f"Logs and saved progress for this copy are in {ctx.p.root}.")
    ctx.emit(Status.DONE, "All done. Your site has been copied.", action_required="\n".join(f"- {line}" for line in lines))
    own = state.get("tool_location")
    if own and folder and Path(own).exists() and str(Path(own).resolve()).startswith(str(Path(folder).resolve()) + "/"):
        try:
            remove = ctx.prompter.confirm(ConfirmQuestion("remove_tool_files", f"This tool's own files are inside the website folder ({own}). Remove them now?", default=True))
        except NoAnswerAvailable:
            remove = False
        if remove:
            LocalSystem.remove(Path(own))
            ctx.info(f"Removed {own}.")
