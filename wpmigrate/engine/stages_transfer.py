"""Stage 6: download the files and the database into the private directory."""

from __future__ import annotations

from wpmigrate import constants
from wpmigrate.engine import sizes
from wpmigrate.engine.context import Context
from wpmigrate.engine.destination import human_size
from wpmigrate.engine.errors import AuthorizationNeeded, TransferFailed
from wpmigrate.engine.prompts import Choice, ChoiceQuestion, TextQuestion
from wpmigrate.engine.reprint import ReprintResult, ReprintRunner
from wpmigrate.engine.stages_source import reprint_runner, source_site
from wpmigrate.engine.system import LocalSystem

_BLOCKED_HINTS = {
    "CLOUDFLARE_CHALLENGE": "A Cloudflare browser check on the source blocks the transfer. Ask the site's administrator to turn it off for requests to the transfer address (the site address followed by /?reprint-api) or to allow this server's IP address, then run the tool again.",
    "WORDFENCE_BLOCKED": "Wordfence or another firewall on the source blocked the transfer. Wait for the block to expire, or ask the administrator to allow this server's IP address, then run the tool again.",
    "RATE_LIMITED": "Your original site is limiting requests. Wait a few minutes and run the tool again; the download continues where it stopped.",
}


def _fail(ctx: Context, result: ReprintResult, what: str) -> None:
    """Turn a Reprint failure into a plain-language error."""
    code = result.error_code or ""
    if result.exit_code == constants.REPRINT_EXIT_AUTH_NEEDED or code in ("AUTH_FAILED", "ENROLLMENT_NEEDED") or result.status == "enrollment_needed":
        raise AuthorizationNeeded(
            "Your original site no longer accepts this server's key.",
            "The tool will ask the source to authorize it again.",
            code=code or "AUTH",
        )
    if code in _BLOCKED_HINTS:
        raise TransferFailed(f"{what} was blocked by your original site.", _BLOCKED_HINTS[code], code=code)
    if result.exit_code == constants.REPRINT_EXIT_TEMPORARY:
        raise TransferFailed(
            f"{what} stopped because your original site kept failing temporarily ({result.error}).",
            "Run the tool again later; it continues where it stopped.",
            code=code or "TEMPORARY",
        )
    if result.exit_code == constants.REPRINT_EXIT_UNFINISHED:
        raise TransferFailed(f"{what} is not finished yet.", "Run the tool again; it continues where it stopped.", code="UNFINISHED")
    detail = result.error or (result.stderr_tail[-1] if result.stderr_tail else "no details")
    raise TransferFailed(
        f"{what} failed: {detail}",
        f"Run the tool again to retry. If it keeps failing, the details are in {ctx.p.log_file}.",
        code=code or "REPRINT_FAILED",
    )


def _space_check(ctx: Context, runner: ReprintRunner):
    def check(total_bytes: int) -> None:
        free = LocalSystem.disk_free(ctx.p.download)
        needed = int(total_bytes * 1.1) + 200 * 1_048_576
        ctx.st.set("download_bytes_total", total_bytes)
        if free < needed:
            ctx.st.set("disk_space_problem", {"free": free, "needed": needed})
            ctx.warn(f"The site's files need about {human_size(needed)} but only {human_size(free)} is free. Stopping the download.")
            ctx.system.stop_children()
    return check


def _measure(ctx: Context, runner: ReprintRunner, credentials: list[str]) -> None:
    """Show how big the site is, and offer to leave leftover backups behind."""
    state = ctx.st
    ctx.info("Measuring your site...")
    result = runner.run_to_completion(runner.base_args("files-index"), stage="download", secret_args=credentials, progress_label="Measuring your site")
    if result.exit_code != 0:
        _fail(ctx, result, "Measuring the files")
    tables_run = runner.run_to_completion(runner.base_args("db-index"), stage="download", secret_args=credentials)
    indexes = sorted(ctx.p.reprint_state.glob("remotes/*/pull/remote-index.next.jsonl"), key=lambda path: path.stat().st_mtime)
    files = sizes.read_file_index(indexes[-1]) if indexes else []
    if not files:
        ctx.log("no remote file list found; skipping the size report")
        return
    root = sizes.find_site_root(files)
    summary = sizes.summarize_files(files, root)
    database = sizes.read_database_tables(ctx.p.reprint_state / "db-tables.jsonl") if tables_run.exit_code == 0 else {"bytes": 0, "count": 0, "largest": []}
    state.update({"source_files_bytes": summary["bytes"], "source_files_count": summary["count"], "source_db_bytes": database["bytes"], "source_db_tables": database["count"]})
    if database["count"]:
        ctx.info(
            f"Your site is about {human_size(summary['bytes'] + database['bytes'])} in total: "
            f"{human_size(summary['bytes'])} of files ({summary['count']:,} files) and {human_size(database['bytes'])} of database ({database['count']:,} tables)."
        )
    else:
        ctx.info(f"Your site has {human_size(summary['bytes'])} of files ({summary['count']:,} files). The database size could not be read.")
    lines = ["Where the space goes:"]
    lines += [f"  {human_size(size):>10}  {name}" for name, size in summary["parts"][:4] if size]
    lines += [f"  {human_size(size):>10}  database table {name}" for name, size in database["largest"][:2] if size >= sizes.MEGABYTE]
    ctx.note("\n".join(lines))
    _report_plugin_backups(ctx, files, root, summary["bytes"])
    _choose_leftovers(ctx, sizes.find_extras(files, root))


def _report_plugin_backups(ctx: Context, files: list[sizes.RemoteFile], root: str | None, listed_bytes: int) -> None:
    """Say what is never copied: plugin-made backups, cache and logs."""
    owners = sizes.plugin_backup_owners(files, root)
    hidden = _uncopied_bytes(ctx, listed_bytes)
    ctx.st.update({"plugin_backup_owners": owners, "uncopied_bytes": hidden})
    seen = f" I can see backup folders from {_join(owners)}." if owners else ""
    if hidden is not None and hidden >= sizes.LARGE_ARCHIVE_BYTES:
        ctx.note(
            f"About {human_size(hidden)} more on your original site is not part of the copy: "
            f"backup archives made by backup plugins, plus cache and log files.{seen} "
            "These cannot be brought along, so if you need one of those backups, download it from the plugin's own screen. They stay untouched on your original site."
        )
    elif owners:
        ctx.note(
            f"Your original site has backup folders from {_join(owners)}. The backup archives those plugins made are not part of the copy and cannot be brought along, "
            "and I cannot see how big they are. If you need one, download it from the plugin's own screen. They stay untouched on your original site."
        )
    else:
        ctx.note("Backup archives made by plugins such as All-in-One WP Migration, UpdraftPlus and Duplicator are never part of the copy, and neither are cache and log files.")


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _uncopied_bytes(ctx: Context, listed_bytes: int) -> int | None:
    """How much the original site holds beyond the files that will be copied.

    Asks the original site for its own total (a read-only request with the
    login from earlier). Returns ``None`` when there is no login to use or
    the site does not answer; the copy does not depend on it.
    """
    try:
        site = source_site(ctx)
        if not site.client.has_cookie("wordpress_logged_in_"):
            return None
        # WordPress counts every file for this, which can take long on a big
        # site. It is only a nicety, so do not wait long for it.
        site.client.timeout = 20
        site.discover()
        total = site.files_size()
    except Exception as error:  # noqa: BLE001 - only a nicety, never stop the copy for it
        ctx.log(f"could not read the original site's total size: {error}")
        return None
    return None if total is None else max(0, total - listed_bytes)


def _choose_leftovers(ctx: Context, extras: list[sizes.Extra]) -> None:
    """List leftover backups and ask which of them to copy (none by default)."""
    state = ctx.st
    if not extras:
        state.update({"excluded_paths": [], "excluded_bytes": 0, "excluded_count": 0})
        return
    # Keep the command line a sensible length: the biggest 200 are plenty.
    extras = extras[:200]
    shown = extras[:20]
    total = sum(extra.size for extra in extras)
    one = len(extras) == 1
    listing = [f"I also found {'something that looks like a leftover backup' if one else 'a few things that look like leftover backups'}:"]
    for number, extra in enumerate(shown, 1):
        row = f"  {number:>2}. {human_size(extra.size):>9}  {extra.shown}"
        # The reason only when it still fits on a normal terminal line.
        listing.append(row + f"  ({extra.reason})" if len(row) + len(extra.reason) + 4 <= 78 else row)
    if len(extras) > len(shown):
        listing.append(f"  ... and {len(extras) - len(shown)} smaller ones")
    ctx.note("\n".join(listing))
    choices = [
        Choice("none", f"No, leave {'it' if one else 'them all'} out. That saves {human_size(total)}."),
        Choice("all", f"Yes, copy {'it' if one else 'all of them'}."),
    ]
    if not one:
        choices.append(Choice("some", "Let me pick which ones to copy."))
    answer = ctx.prompter.ask_choice(ChoiceQuestion("copy_extra_files", f"Would you like {'this' if one else 'any of these'} copied too?", choices, default="none"))
    keep: set[int] = set()
    if answer == "all":
        keep = set(range(len(extras)))
    elif answer == "some":
        for _ in range(3):
            text = ctx.prompter.ask_text(TextQuestion(
                "copy_extra_numbers",
                "Which ones should I copy?",
                help=f"Type their numbers, separated by commas, for example 1,3. Press Enter for none. The numbers go from 1 to {len(shown)}.",
            ))
            picked = sizes.parse_selection(text, len(shown))
            if picked is not None:
                keep = set(picked)
                break
            ctx.warn(f"I did not understand that. Please use numbers from 1 to {len(shown)}, for example 1,3.")
    left_out = [extra for position, extra in enumerate(extras) if position not in keep]
    state.update({
        "excluded_paths": [extra.path for extra in left_out],
        "excluded_bytes": sum(extra.size for extra in left_out),
        "excluded_count": sum(extra.files for extra in left_out),
    })
    copied = [extra for position, extra in enumerate(extras) if position in keep]
    if not left_out:
        ctx.info("Okay, the leftover backups will be copied too.")
    elif copied:
        ctx.info(f"Okay, I will copy {_join([extra.shown for extra in copied])} and leave out the other {human_size(state.get('excluded_bytes'))}. Nothing is deleted on your original site.")
    else:
        ctx.info(f"Leaving out {human_size(state.get('excluded_bytes'))} of leftover backups. Nothing is deleted on your original site.")


def stage_download(ctx: Context) -> None:
    """Preflight, then download the files and the database (no database needed yet)."""
    runner = reprint_runner(ctx)
    credentials = runner.credential_args()
    state = ctx.st
    progress = state.get("download", {}) or {}

    if not progress.get("preflight"):
        ctx.info("Taking a look at your original site...")
        result = runner.run_to_completion(runner.base_args("preflight"), stage="download", secret_args=credentials)
        if result.exit_code != 0:
            _fail(ctx, result, "Checking your original site")
        data = result.data_record(lambda record: isinstance(record.get("data"), dict) and record["data"].get("ok") is not None)
        php_info = ((data or {}).get("data") or {}).get("php") or {}
        assertion = runner.run(runner.base_args("preflight-assert"), stage="download")
        if assertion.exit_code != 0:
            checks = assertion.data_record(lambda record: record.get("type") == "preflight_assertion")
            failed = [f"{check.get('label')}: {check.get('detail')}" for check in (checks or {}).get("checks", []) if not check.get("pass")]
            raise TransferFailed(
                "Your original site cannot be copied as it is: " + ("; ".join(failed) or (assertion.error or "preflight checks failed")) + ".",
                "Fix the problem on your original site (or ask its host), then run the tool again.",
                code="PREFLIGHT_ASSERT",
            )
        metadata = runner.run(runner.base_args("pull-metadata", fs_root=False), stage="download")
        meta = metadata.data_record(lambda record: "sourceSite" in record) or {}
        source = meta.get("sourceSite") or {}
        state.update({
            "source_php_version": php_info.get("version"),
            "source_home_url": source.get("homeUrl"),
            "source_site_url": source.get("siteUrl"),
            "source_table_prefix": source.get("tablePrefix") or "wp_",
            "source_wordpress_version": source.get("wordpressVersion"),
        })
        progress["preflight"] = True
        state.set("download", progress)
        ctx.info(f"Found WordPress {source.get('wordpressVersion')} running on PHP {php_info.get('version')}.")

    if not progress.get("measured"):
        with ctx.timed("measuring the site"):
            _measure(ctx, runner, credentials)
        progress["measured"] = True
        state.set("download", progress)
    excludes = [f"--exclude={path}" for path in state.get("excluded_paths") or []]

    rewrite = []
    source_home = state.get("source_home_url") or state.get("source_url")
    new_url = state.get("new_site_url")
    if new_url and source_home and new_url.rstrip("/") != source_home.rstrip("/"):
        rewrite = [f"--new-site-url={new_url}"]

    if not progress.get("files"):
        ctx.info("Copying the files...")
        runner.bytes_total_check = _space_check(ctx, runner)
        with ctx.timed("copying the files"):
            result = runner.run_to_completion([*runner.base_args("files-pull"), *rewrite, *excludes], stage="download", secret_args=credentials, progress_label="Copying files")
        runner.bytes_total_check = None
        problem = state.get("disk_space_problem")
        if problem:
            state.set("disk_space_problem", None)
            raise TransferFailed(
                f"Not enough disk space: about {human_size(problem['needed'])} is needed and {human_size(problem['free'])} is free.",
                "Free up space in your hosting account, then run the tool again; the download continues where it stopped.",
                code="DISK_SPACE",
            )
        if result.exit_code != 0 or result.status not in ("complete", None):
            _fail(ctx, result, "Downloading the files")
        progress["files"] = True
        state.set("download", progress)
        ctx.info("All files are copied over.", after_progress=True)

    if not progress.get("database"):
        ctx.info("Copying the database...")
        with ctx.timed("copying the database"):
            result = runner.run_to_completion(runner.base_args("db-pull"), stage="download", secret_args=credentials, progress_label="Copying the database")
        if result.exit_code != 0:
            _fail(ctx, result, "Downloading the database")
        progress["database"] = True
        state.set("download", progress)
        size = ctx.p.sql_dump.stat().st_size if ctx.p.sql_dump.exists() else 0
        ctx.info(f"The database is copied over ({human_size(size)}).", after_progress=True)
