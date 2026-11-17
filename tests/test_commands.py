import sys
from pathlib import Path

import pytest

from stg_layers.commands import CommandRunner
from stg_layers.errors import CommandError, RepositoryError


def test_command_runner_preserves_stdin_and_working_directory(tmp_path: Path) -> None:
    output = CommandRunner(tmp_path).run(
        sys.executable,
        "-c",
        "import os, sys; print(os.getcwd()); print(sys.stdin.read(), end='')",
        input_text="input with spaces and ünicode\n",
    )
    assert output == f"{tmp_path}\ninput with spaces and ünicode\n"


def test_command_runner_keeps_failure_details_and_allows_expected_exit_codes(
    tmp_path: Path,
) -> None:
    runner = CommandRunner(tmp_path)
    args = (
        sys.executable,
        "-c",
        "import sys; print('output'); sys.stderr.write('failure'); sys.exit(1)",
    )
    with pytest.raises(CommandError) as failure:
        runner.run(*args)
    assert failure.value.command == args
    assert failure.value.returncode == 1
    assert failure.value.detail == "failure"
    assert runner.run(*args, allowed_returncodes=(1,)) == "output\n"


def test_command_runner_reports_a_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="was not found on PATH"):
        CommandRunner(tmp_path).run(str(tmp_path / "missing-executable"))
