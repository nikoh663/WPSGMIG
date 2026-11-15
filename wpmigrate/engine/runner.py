"""Runs the stages in order, resuming after the last completed one."""

from __future__ import annotations

from typing import Callable

from wpmigrate.constants import ExitCode
from wpmigrate.engine.context import Context
from wpmigrate.engine.errors import AuthorizationNeeded, Cancelled, MigrationError, SourceStepFailed
from wpmigrate.engine.events import Status
from wpmigrate.engine.prompts import NoAnswerAvailable
from wpmigrate.engine.stages_destination import stage_deploy, stage_destination
from wpmigrate.engine.stages_finish import stage_cleanup_source, stage_summary
from wpmigrate.engine.stages_source import manual_fallback, stage_authorize, stage_collect, stage_login, stage_plugin, stage_requirements
from wpmigrate.engine.stages_transfer import stage_download
from wpmigrate.engine.state import RunLock

Stage = Callable[[Context], None]

STAGES: list[tuple[str, str, Stage]] = [
    ("collect", "Site details", stage_collect),
    ("requirements", "Checking this server", stage_requirements),
    ("login", "Logging in to the source site", stage_login),
    ("plugin", "Preparing the source site", stage_plugin),
    ("authorize", "Connecting this server to the source site", stage_authorize),
    ("download", "Downloading the site", stage_download),
    ("destination", "Setting up the destination", stage_destination),
    ("deploy", "Installing the copied site", stage_deploy),
    ("cleanup_source", "Tidying up the source site", stage_cleanup_source),
    ("summary", "Summary", stage_summary),
]
SOURCE_STAGES = ("login", "plugin", "authorize")
STAGE_NAMES = [name for name, _, _ in STAGES]


class Engine:
    """Drives one run. Front ends create it with a :class:`Context` and call :meth:`run`."""

    def __init__(self, ctx: Context, *, tool_location: str | None = None) -> None:
        self.ctx = ctx
        self.tool_location = tool_location

    def _run_stage(self, name: str, title: str, function: Stage) -> None:
        ctx = self.ctx
        ctx.current_stage = name
        ctx.emit(Status.STARTED, title, stage=name, number=STAGE_NAMES.index(name) + 1, total=len(STAGES))
        function(ctx)
        if ctx.state is not None:
            ctx.state.mark_stage(name, "done")
        ctx.emit(Status.DONE, title, stage=name)

    def _source_steps(self) -> None:
        """Stages 3-5, switching to the manual route if any of them fails."""
        ctx = self.ctx
        for name, title, function in STAGES:
            if name not in SOURCE_STAGES or ctx.st.stage_done(name):
                continue
            try:
                self._run_stage(name, title, function)
            except SourceStepFailed as error:
                ctx.current_stage = name
                manual_fallback(ctx, error.reason)
                for later in SOURCE_STAGES:
                    ctx.st.mark_stage(later, "done")
                ctx.emit(Status.DONE, "Source site prepared by hand", stage=name)
                return

    def run(self) -> int:
        """Run every unfinished stage. Returns the process exit code."""
        ctx = self.ctx
        try:
            self._run_stage("collect", STAGES[0][1], stage_collect)
            if self.tool_location:
                ctx.st.set("tool_location", self.tool_location)
            reauthorized = False
            index = 1
            while index < len(STAGES):
                name, title, function = STAGES[index]
                if name in SOURCE_STAGES:
                    self._source_steps()
                    index = STAGE_NAMES.index("authorize") + 1
                    continue
                if ctx.st.stage_done(name):
                    index += 1
                    continue
                try:
                    self._run_stage(name, title, function)
                except AuthorizationNeeded:
                    if reauthorized or name != "download":
                        raise
                    reauthorized = True
                    ctx.warn("The source site does not accept this server's key any more. Authorizing it again (the same key is used).")
                    ctx.st.set("authorized", False)
                    for source_stage in SOURCE_STAGES:
                        ctx.st.reset_stage(source_stage)
                    index = STAGE_NAMES.index("login")
                    continue
                index += 1
            return ExitCode.SUCCESS
        except NoAnswerAvailable as error:
            failure = MigrationError(
                f"The tool needs an answer to continue ({error}), but nobody can be asked right now (the terminal was closed, or the tool runs unattended).",
                "Run the tool again in an open terminal; it continues from this question. Unattended runs can give the answer with the matching option (see --help).",
            )
            ctx.emit(Status.FAILED, failure.what, action_required=failure.next_step, code="NO_ANSWER")
            return ExitCode.GENERAL_ERROR
        except Cancelled as error:
            ctx.emit(Status.FAILED, error.what, action_required=error.next_step, code="CANCELLED")
            return error.exit_code
        except MigrationError as error:
            ctx.emit(Status.FAILED, error.what, action_required=error.next_step, code=error.code)
            return error.exit_code
        finally:
            if isinstance(ctx.lock, RunLock):
                ctx.lock.release()
