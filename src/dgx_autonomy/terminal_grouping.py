"""The terminal tool runs several lines of statements as one command.

OpenHands 1.49.4 refuses terminal input with more than one statement on separate
lines ("Cannot execute multiple commands at once") and advises chaining with && or ;.
That advice cannot follow a heredoc: its terminator must end the line. On hugo-dgx1
the builder wrote a file with a heredoc and ran it on the next line 21 times in six
hours of one run. Each attempt was refused and cost a turn.

This module wraps such input in a brace group, `{ ...\\n}`. Bash runs a group in the
current shell, one statement after another, as if the lines were typed in turn: `cd`
and variables persist, and a failing line does not stop the next. The terminal sees
one statement and one prompt. Input that does not parse is left alone, as the SDK
leaves it.

Like demo_tool.py, this module runs inside the Agent Server in the sandbox and
imports only the OpenHands SDK. The Agent Server loads it with `--import-modules`.
"""

from __future__ import annotations

from collections.abc import Callable

from openhands.sdk.security.shell_parser import parse
from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
from openhands.tools.terminal.terminal.terminal_session import TerminalSession
from openhands.tools.terminal.utils.command import split_bash_commands

Execute = Callable[[TerminalSession, TerminalAction], TerminalObservation]


def group_statements(command: str) -> str:
    """`command` as one brace group if it has several statements, else unchanged."""
    stripped = command.strip()
    if len(split_bash_commands(stripped)) <= 1:
        return command
    grouped = "{ " + stripped + "\n}"
    if parse(grouped).has_error or len(split_bash_commands(grouped)) != 1:
        return command
    return grouped


def _grouping(execute: Execute) -> Execute:
    def grouped_execute(self: TerminalSession, action: TerminalAction) -> TerminalObservation:
        if not action.is_input and not self.terminal.is_powershell():
            command = group_statements(action.command)
            if command != action.command:
                action = action.model_copy(update={"command": command})
        return execute(self, action)

    grouped_execute.__wrapped__ = execute  # type: ignore[attr-defined]
    return grouped_execute


if not hasattr(TerminalSession.execute, "__wrapped__"):
    TerminalSession.execute = _grouping(TerminalSession.execute)  # type: ignore[method-assign,assignment]
