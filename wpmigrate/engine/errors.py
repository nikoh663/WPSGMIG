"""Errors that carry a plain-language explanation and an exit code."""

from __future__ import annotations

from wpmigrate.constants import ExitCode


class MigrationError(Exception):
    """A failure the person can understand and act on.

    ``what`` says what happened, ``next_step`` says what to do about it.
    """

    exit_code = ExitCode.GENERAL_ERROR

    def __init__(self, what: str, next_step: str, *, code: str | None = None) -> None:
        super().__init__(what)
        self.what = what
        self.next_step = next_step
        self.code = code

    def __str__(self) -> str:
        return f"{self.what} {self.next_step}".strip()


class LoginFailed(MigrationError):
    exit_code = ExitCode.LOGIN_FAILED


class AuthorizationNeeded(MigrationError):
    exit_code = ExitCode.AUTHORIZATION_NEEDED


class TransferFailed(MigrationError):
    exit_code = ExitCode.TRANSFER_FAILED


class DestinationFailed(MigrationError):
    exit_code = ExitCode.DESTINATION_FAILED


class RequirementsFailed(MigrationError):
    exit_code = ExitCode.REQUIREMENTS_FAILED


class Cancelled(MigrationError):
    exit_code = ExitCode.CANCELLED

    def __init__(self, what: str = "Stopped at your request.", next_step: str = "Run the tool again whenever you want to continue; nothing was lost.") -> None:
        super().__init__(what, next_step)


class AlreadyRunning(MigrationError):
    exit_code = ExitCode.ALREADY_RUNNING


class SourceStepFailed(Exception):
    """Internal: an automatic step on the source failed; use the manual fallback.

    Not shown to the person directly; ``reason`` explains why the automatic
    route did not work.
    """

    def __init__(self, reason: str, *, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
