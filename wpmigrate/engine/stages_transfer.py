"""Stage 6: download the files and the database into the private directory."""

from __future__ import annotations

from wpmigrate import constants
from wpmigrate.engine import sizes
from wpmigrate.engine.context import Context
from wpmigrate.engine.destination import human_size
from wpmigrate.engine.errors import AuthorizationNeeded, TransferFailed
from wpmigrate.engine.prompts import ConfirmQuestion
from wpmigrate.engine.reprint import ReprintResult, ReprintRunner
from wpmigrate.engine.stages_source import reprint_runner
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
    lines.append("Backups made by plugins such as All-in-One WP Migration, UpdraftPlus and Duplicator are left out automatically, and so are cache and log files.")
    ctx.note("\n".join(lines))

    extras = sizes.find_extras(files, root)
    if not extras:
        state.update({"excluded_paths": [], "excluded_bytes": 0, "excluded_count": 0})
        return
    total = sum(extra.size for extra in extras)
    count = sum(extra.files for extra in extras)
    listing = [f"I also found {'something' if len(extras) == 1 else 'a few things'} that {'looks like a leftover backup' if len(extras) == 1 else 'look like leftover backups'}:"]
    listing += [f"  {human_size(extra.size):>10}  {extra.shown}  ({extra.reason})" for extra in extras[:8]]
    if len(extras) > 8:
        listing.append(f"  ... and {len(extras) - 8} more")
    ctx.note("\n".join(listing))
    skip = ctx.prompter.confirm(ConfirmQuestion(
        "skip_extra_files",
        f"Shall I leave {'it' if count == 1 else 'them'} out of the copy? That saves {human_size(total)}.",
        default=True,
    ))
    if skip:
        # Keep the command line a sensible length: the biggest 200 are plenty.
        chosen = extras[:200]
        state.update({"excluded_paths": [extra.path for extra in chosen], "excluded_bytes": sum(extra.size for extra in chosen), "excluded_count": sum(extra.files for extra in chosen)})
        ctx.info(f"Leaving out {human_size(sum(extra.size for extra in chosen))} of leftover backups. Nothing is deleted on your original site.")
    else:
        state.update({"excluded_paths": [], "excluded_bytes": 0, "excluded_count": 0})
        ctx.info("Okay, everything will be copied.")


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
        result = runner.run_to_completion(runner.base_args("db-pull"), stage="download", secret_args=credentials, progress_label="Copying the database")
        if result.exit_code != 0:
            _fail(ctx, result, "Downloading the database")
        progress["database"] = True
        state.set("download", progress)
        size = ctx.p.sql_dump.stat().st_size if ctx.p.sql_dump.exists() else 0
        ctx.info(f"The database is copied over ({human_size(size)}).", after_progress=True)
