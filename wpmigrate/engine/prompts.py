"""Questions the engine asks, and the interface a front end implements.

The engine never reads from a terminal. It describes a question with one of
the dataclasses below and hands it to a :class:`Prompter`. A terminal front
end answers synchronously; a web front end can run the engine in a worker
and answer from a request handler. When no person is available (unattended
run, dropped session) the prompter raises :class:`NoAnswerAvailable` and the
engine stops cleanly so that the next run continues from the same place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence


class NoAnswerAvailable(Exception):
    """Raised by a prompter when it cannot ask the person right now."""


@dataclass(frozen=True)
class TextQuestion:
    """Free text, optionally with a default used when the answer is empty."""

    key: str
    text: str
    default: str | None = None
    help: str | None = None


@dataclass(frozen=True)
class SecretQuestion:
    """Hidden input such as a password. Never stored by the engine."""

    key: str
    text: str


@dataclass(frozen=True)
class Choice:
    """One option of a :class:`ChoiceQuestion`."""

    value: str
    label: str


@dataclass(frozen=True)
class ChoiceQuestion:
    """Pick one option. ``default`` is a :attr:`Choice.value`."""

    key: str
    text: str
    choices: Sequence[Choice]
    default: str | None = None


@dataclass(frozen=True)
class ConfirmQuestion:
    """Yes or no."""

    key: str
    text: str
    default: bool = False


@dataclass(frozen=True)
class TypeToConfirmQuestion:
    """The person must type ``expected`` exactly to go ahead."""

    key: str
    text: str
    expected: str


@dataclass(frozen=True)
class WaitQuestion:
    """Show instructions and wait until the person says they are done."""

    key: str
    text: str
    instructions: Sequence[str] = field(default_factory=tuple)


class Prompter(Protocol):
    """Implemented by each front end."""

    def ask_text(self, question: TextQuestion) -> str:  # pragma: no cover - protocol
        ...

    def ask_secret(self, question: SecretQuestion) -> str:  # pragma: no cover
        ...

    def ask_choice(self, question: ChoiceQuestion) -> str:  # pragma: no cover
        ...

    def confirm(self, question: ConfirmQuestion) -> bool:  # pragma: no cover
        ...

    def type_to_confirm(self, question: TypeToConfirmQuestion) -> bool:  # pragma: no cover
        ...

    def wait_for_person(self, question: WaitQuestion) -> None:  # pragma: no cover
        ...


class ScriptedPrompter:
    """Answers questions from a dict keyed by question key (unattended runs and tests).

    Missing answers fall back to the question's default; if there is none,
    :class:`NoAnswerAvailable` is raised.
    """

    def __init__(self, answers: dict[str, object] | None = None) -> None:
        self.answers: dict[str, object] = dict(answers or {})
        self.asked: list[str] = []

    def _get(self, key: str) -> object:
        self.asked.append(key)
        if key in self.answers:
            value = self.answers[key]
            if isinstance(value, list):
                if not value:
                    raise NoAnswerAvailable(key)
                return value.pop(0)
            return value
        raise NoAnswerAvailable(key)

    def ask_text(self, question: TextQuestion) -> str:
        try:
            return str(self._get(question.key))
        except NoAnswerAvailable:
            if question.default is not None:
                return question.default
            raise

    def ask_secret(self, question: SecretQuestion) -> str:
        return str(self._get(question.key))

    def ask_choice(self, question: ChoiceQuestion) -> str:
        try:
            value = str(self._get(question.key))
        except NoAnswerAvailable:
            if question.default is not None:
                return question.default
            raise
        if value not in {choice.value for choice in question.choices}:
            raise NoAnswerAvailable(f"{question.key}: {value!r} is not one of the choices")
        return value

    def confirm(self, question: ConfirmQuestion) -> bool:
        try:
            value = self._get(question.key)
        except NoAnswerAvailable:
            raise
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"y", "yes", "1", "true"}

    def type_to_confirm(self, question: TypeToConfirmQuestion) -> bool:
        return str(self._get(question.key)) == question.expected

    def wait_for_person(self, question: WaitQuestion) -> None:
        self._get(question.key)
