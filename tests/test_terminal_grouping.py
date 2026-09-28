"""Several lines of statements run as one terminal command (terminal_grouping.py)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from openhands.tools.terminal.definition import TerminalAction
from openhands.tools.terminal.terminal import create_terminal_session
from openhands.tools.terminal.terminal.terminal_session import TerminalSession
from openhands.tools.terminal.utils.command import split_bash_commands

from dgx_autonomy.terminal_grouping import group_statements

# The shape of the 21 commands the builder had refused on hugo-dgx1: a heredoc, then a
# command on the next line that uses what it wrote.
HEREDOC_THEN_RUN = """cd {dir} && cat > f.py <<'PY'
x = "a && b; c | d"
print(x)
PY
python3 f.py"""


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cd /x && npm test; echo done",
        "cat > f <<'EOF'\nline && one\nEOF",
        "for f in *; do\n  echo $f\ndone",
        "",
    ],
)
def test_one_statement_is_left_alone(command: str) -> None:
    assert group_statements(command) == command


@pytest.mark.parametrize(
    "command",
    [
        HEREDOC_THEN_RUN.format(dir="/x"),
        "ls\npwd",
        "npm run dev &\nsleep 3\ncurl -s localhost:3000",
        "cat > f <<EOF\n}\nEOF\ncat f",
    ],
)
def test_several_statements_become_one_group(command: str) -> None:
    grouped = group_statements(command)
    assert grouped == "{ " + command.strip() + "\n}"
    assert len(split_bash_commands(grouped)) == 1


def test_input_that_does_not_parse_is_left_alone() -> None:
    command = "echo 'unterminated\npwd"
    assert group_statements(command) == command


@pytest.fixture
def session(tmp_path: Path) -> Iterator[TerminalSession]:
    import dgx_autonomy.terminal_grouping  # noqa: F401  (the Agent Server imports it the same way)

    s = create_terminal_session(work_dir=str(tmp_path))
    assert isinstance(s, TerminalSession)
    s.initialize()
    yield s
    s.close()


def test_the_terminal_writes_a_heredoc_and_runs_the_next_line(
    session: TerminalSession, tmp_path: Path
) -> None:
    obs = session.execute(TerminalAction(command=HEREDOC_THEN_RUN.format(dir=tmp_path)))
    assert "Cannot execute multiple commands" not in obs.text
    assert "a && b; c | d" in obs.text
    assert obs.exit_code == 0


def test_the_lines_share_one_shell_as_if_typed_in_turn(
    session: TerminalSession, tmp_path: Path
) -> None:
    (tmp_path / "sub").mkdir()
    obs = session.execute(TerminalAction(command="cd sub\nX=42\nfalse\necho X=$X"))
    assert "X=42" in obs.text
    obs = session.execute(TerminalAction(command="echo after=$X; pwd"))
    assert "after=42" in obs.text
    assert "/sub" in obs.text
