"""Stage 6: download the files and the database into the private directory."""

from __future__ import annotations

from wpmigrate import constants
from wpmigrate.engine.context import Context
from wpmigrate.engine.destination import human_size
from wpmigrate.engine.errors import AuthorizationNeeded, TransferFailed
from wpmigrate.engine.reprint import ReprintResult, ReprintRunner
from wpmigrate.engine.stages_source import reprint_runner
from wpmigrate.engine.system import LocalSystem

_BLOCKED_HINTS = {
    "CLOUDFLARE_CHALLENGE": "A Cloudflare browser check on the source blocks the transfer. Ask the site's administrator to turn it off for requests to the transfer address (the site address followed by /?reprint-api) or to allow this server's IP address, then run the tool again.",
    "WORDFENCE_BLOCKED": "Wordfence or another firewall on the source blocked the transfer. Wait for the block to expire, or ask the administrator to allow this server's IP address, then run the tool again.",
    "RATE_LIMITED": "The source site is limiting requests. Wait a few minutes and run the tool again; the download continues where it stopped.",
}


def _fail(ctx: Context, result: ReprintResult, what: str) -> None:
    """Turn a Reprint failure into a plain-language error."""
    code = result.error_code or ""
    if result.exit_code == constants.REPRINT_EXIT_AUTH_NEEDED or code in ("AUTH_FAILED", "ENROLLMENT_NEEDED") or result.status == "enrollment_needed":
        raise AuthorizationNeeded(
            "The source site no longer accepts this server's key.",
            "The tool will ask the source to authorize it again.",
            code=code or "AUTH",
        )
    if code in _BLOCKED_HINTS:
        raise TransferFailed(f"{what} was blocked by the source site.", _BLOCKED_HINTS[code], code=code)
    if result.exit_code == constants.REPRINT_EXIT_TEMPORARY:
        raise TransferFailed(
            f"{what} stopped because the source site kept failing temporarily ({result.error}).",
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


def stage_download(ctx: Context) -> None:
    """Preflight, then download the files and the database (no database needed yet)."""
    runner = reprint_runner(ctx)
    credentials = runner.credential_args()
    state = ctx.st
    progress = state.get("download", {}) or {}

    if not progress.get("preflight"):
        ctx.info("Checking the source site...")
        result = runner.run_to_completion(runner.base_args("preflight"), stage="download", secret_args=credentials)
        if result.exit_code != 0:
            _fail(ctx, result, "Checking the source site")
        data = result.data_record(lambda record: isinstance(record.get("data"), dict) and record["data"].get("ok") is not None)
        php_info = ((data or {}).get("data") or {}).get("php") or {}
        assertion = runner.run(runner.base_args("preflight-assert"), stage="download")
        if assertion.exit_code != 0:
            checks = assertion.data_record(lambda record: record.get("type") == "preflight_assertion")
            failed = [f"{check.get('label')}: {check.get('detail')}" for check in (checks or {}).get("checks", []) if not check.get("pass")]
            raise TransferFailed(
                "The source site cannot be copied as it is: " + ("; ".join(failed) or (assertion.error or "preflight checks failed")) + ".",
                "Fix the problem on the source site (or ask its host), then run the tool again.",
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
        ctx.info(f"The source runs WordPress {source.get('wordpressVersion')} on PHP {php_info.get('version')}.")

    rewrite = []
    source_home = state.get("source_home_url") or state.get("source_url")
    new_url = state.get("new_site_url")
    if new_url and source_home and new_url.rstrip("/") != source_home.rstrip("/"):
        rewrite = [f"--new-site-url={new_url}"]

    if not progress.get("files"):
        ctx.info("Downloading the site's files...")
        runner.bytes_total_check = _space_check(ctx, runner)
        result = runner.run_to_completion([*runner.base_args("files-pull"), *rewrite], stage="download", secret_args=credentials, progress_label="Downloading files")
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
        ctx.info("All files are downloaded.", after_progress=True)

    if not progress.get("database"):
        ctx.info("Downloading the database...")
        result = runner.run_to_completion(runner.base_args("db-pull"), stage="download", secret_args=credentials, progress_label="Downloading the database")
        if result.exit_code != 0:
            _fail(ctx, result, "Downloading the database")
        progress["database"] = True
        state.set("download", progress)
        size = ctx.p.sql_dump.stat().st_size if ctx.p.sql_dump.exists() else 0
        ctx.info(f"The database is downloaded ({human_size(size)}).", after_progress=True)
