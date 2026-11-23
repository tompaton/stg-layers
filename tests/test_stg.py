from pathlib import Path

import pytest

from stg_layers.commands import CommandRunner
from stg_layers.errors import RepositoryError
from stg_layers.models import PatchStatus
from stg_layers.stg import StGit, parse_series


def test_parse_series_records_patch_state_and_description() -> None:
    output = "> current # current patch\n+ earlier # applied patch\n- next # queued patch\n! hidden"

    assert parse_series(output) == [
        ("current", PatchStatus.CURRENT, "current patch"),
        ("earlier", PatchStatus.APPLIED, "applied patch"),
        ("next", PatchStatus.UNAPPLIED, "queued patch"),
        ("hidden", PatchStatus.HIDDEN, ""),
    ]


def test_parse_series_rejects_unknown_status() -> None:
    with pytest.raises(RepositoryError, match="Unrecognized output"):
        parse_series("? patch")


@pytest.mark.parametrize("operation,undo_steps", [("push", 1), ("refresh", 2)])
@pytest.mark.parametrize("fails", [False, True])
def test_mutation_notifications_preserve_undo_counts_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, undo_steps: int, fails: bool
) -> None:
    commands = CommandRunner(tmp_path)
    error = RepositoryError("operation failed")
    notifications: list[tuple[int, RepositoryError | None]] = []

    def run(
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        if fails:
            raise error
        return "operation completed"

    monkeypatch.setattr(commands, "run", run)
    stg = StGit(
        commands, after_mutation=lambda steps, failure: notifications.append((steps, failure))
    )
    action = (lambda: stg.push("patch")) if operation == "push" else stg.refresh
    if fails:
        with pytest.raises(RepositoryError) as failure:
            action()
        assert failure.value is error
    else:
        assert action() == "operation completed"
    assert notifications == [(undo_steps, error if fails else None)]
