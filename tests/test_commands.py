import sys
from pathlib import Path

import pytest

from stg_layers.commands import CommandRunner
from stg_layers.errors import CommandError, RepositoryError
from stg_layers.git_commands import GitCommands
from stg_layers.stg_commands import StgCommands


@pytest.fixture(params=["command-runner", "git-commands", "stg-commands"])
def runner(
    request: pytest.FixtureRequest, tmp_path: Path
) -> CommandRunner | GitCommands | StgCommands:
    commands = CommandRunner(tmp_path)
    if request.param == "git-commands":
        return GitCommands(commands)
    if request.param == "stg-commands":
        return StgCommands(commands)
    return commands


def test_command_runner_preserves_stdin_and_working_directory(
    tmp_path: Path, runner: CommandRunner | GitCommands | StgCommands
) -> None:
    assert runner.root == tmp_path
    output = runner.run(
        sys.executable,
        "-c",
        "import os, sys; print(os.getcwd()); print(sys.stdin.read(), end='')",
        input_text="input with spaces and ünicode\n",
    )
    assert output == f"{tmp_path}\ninput with spaces and ünicode\n"


def test_command_runner_keeps_failure_details_and_allows_expected_exit_codes(
    runner: CommandRunner | GitCommands | StgCommands,
) -> None:
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


def test_command_runner_reports_a_missing_executable(
    tmp_path: Path, runner: CommandRunner | GitCommands | StgCommands
) -> None:
    with pytest.raises(RepositoryError, match="was not found on PATH"):
        runner.run(str(tmp_path / "missing-executable"))
