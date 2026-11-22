import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stg_layers.change_moves import ChangeSelection, MoveTarget
from stg_layers.commands import CommandRunner
from stg_layers.errors import CommandError, RepositoryError
from stg_layers.git import Git
from stg_layers.models import ChangeArea, FileStatus, PatchStatus
from stg_layers.partial_diff import HunkLineSelection
from stg_layers.repository import (
    PatchChangeMoveResult,
    PatchMoveResult,
    StGitRepository,
)


def _git_environment() -> dict[str, str]:
    return os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_EDITOR": "true",
        "GIT_PAGER": "cat",
    }


def _run(root: Path, environment: dict[str, str], *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=root,
        env=environment,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _create_two_patch_stack(root: Path, environment: dict[str, str]) -> None:
    _run(root, environment, "git", "init", "-q")
    _run(root, environment, "git", "branch", "-M", "feature/read-browser")
    _run(root, environment, "git", "config", "user.name", "Test")
    _run(root, environment, "git", "config", "user.email", "test@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    (root / "other.txt").write_text("other\n", encoding="utf-8")
    (root / "third.txt").write_text("third\n", encoding="utf-8")
    (root / "fourth.txt").write_text("fourth\n", encoding="utf-8")
    _run(root, environment, "git", "add", ".")
    _run(root, environment, "git", "commit", "-qm", "base")
    _run(root, environment, "stg", "init")
    _run(root, environment, "stg", "new", "-m", "First patch", "first")
    (root / "base.txt").write_text("base\nfirst layer\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "First patch")
    _run(root, environment, "stg", "new", "-m", "Second patch", "second")
    (root / "other.txt").write_text("other\ncurrent layer\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "Second patch")


def _create_conflicting_two_patch_stack(root: Path, environment: dict[str, str]) -> None:
    _run(root, environment, "git", "init", "-q")
    _run(root, environment, "git", "config", "user.name", "Test")
    _run(root, environment, "git", "config", "user.email", "test@example.invalid")
    (root / "conflict.txt").write_text("base\n", encoding="utf-8")
    _run(root, environment, "git", "add", ".")
    _run(root, environment, "git", "commit", "-qm", "base")
    _run(root, environment, "stg", "init")
    _run(root, environment, "stg", "new", "-m", "First patch", "first")
    (root / "conflict.txt").write_text("first\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "First patch")
    _run(root, environment, "stg", "new", "-m", "Second patch", "second")
    (root / "conflict.txt").write_text("second\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "Second patch")


def _create_three_patch_stack(root: Path, environment: dict[str, str]) -> None:
    _create_two_patch_stack(root, environment)
    _run(root, environment, "stg", "new", "-m", "Third patch", "third")
    (root / "third.txt").write_text("third\nthird layer\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "Third patch")


def _create_four_patch_stack(root: Path, environment: dict[str, str]) -> None:
    _create_three_patch_stack(root, environment)
    _run(root, environment, "stg", "new", "-m", "Fourth patch", "fourth")
    (root / "fourth.txt").write_text("fourth\nfourth layer\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "Fourth patch")


def _create_hunk_transfer_stack(root: Path, environment: dict[str, str]) -> None:
    _run(root, environment, "git", "init", "-q")
    _run(root, environment, "git", "config", "user.name", "Test")
    _run(root, environment, "git", "config", "user.email", "test@example.invalid")
    lines = [f"line {index}" for index in range(60)]
    (root / "file.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(root, environment, "git", "add", "file.txt")
    _run(root, environment, "git", "commit", "-qm", "base")
    _run(root, environment, "stg", "init")
    _run(root, environment, "stg", "new", "-m", "First patch", "first")
    lines[5] = "first patch early"
    lines[45] = "first patch late"
    (root / "file.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "First patch")
    _run(root, environment, "stg", "new", "-m", "Second patch", "second")
    lines[20] = "second patch early"
    lines[35] = "second patch late"
    (root / "file.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "Second patch")


def _create_git_repository(root: Path, environment: dict[str, str]) -> None:
    _run(root, environment, "git", "init", "-q")
    _run(root, environment, "git", "config", "user.name", "Test")
    _run(root, environment, "git", "config", "user.email", "test@example.invalid")
    (root / "file.txt").write_text(
        "".join(f"line {index}\n" for index in range(40)),
        encoding="utf-8",
    )
    _run(root, environment, "git", "add", "--", "file.txt")
    _run(root, environment, "git", "commit", "-qm", "base")


@pytest.mark.parametrize(
    "operation,patch_name,message",
    [
        ("unknown", "patch", "Unknown patch operation"),
        ("push", None, "requires a patch name"),
        ("uncommit", "patch", "requires no patch name"),
    ],
)
def test_patch_operation_validates_arguments_before_running_commands(
    tmp_path: Path, operation: str, patch_name: str | None, message: str
) -> None:
    # This directory is not a repository; invalid requests must fail before Git runs.
    repository = StGitRepository(tmp_path)
    with pytest.raises(RepositoryError, match=message):
        repository.patch_operation(operation, patch_name)


def test_commit_selected_patch_and_uncommit_one_commit(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_three_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    (tmp_path / "local.txt").write_text("local\n", encoding="utf-8")

    repository.patch_operation("commit", "second")

    assert [patch.name for patch in repository.load_stack()] == ["first", "third"]
    assert (
        _run(tmp_path, environment, "git", "log", "-1", "--format=%s", "HEAD~2").strip()
        == "Second patch"
    )
    repository.patch_operation("uncommit")
    patches = repository.load_stack()
    assert len(patches) == 3
    assert "+current layer" in patches[0].diff_text
    assert [patch.name for patch in patches[1:]] == ["first", "third"]
    assert (tmp_path / "local.txt").read_text(encoding="utf-8") == "local\n"
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.parametrize("unapplied", [False, True])
def test_hide_unhide_and_delete_patch_preserve_local_changes(
    tmp_path: Path, unapplied: bool
) -> None:
    environment = _git_environment()
    _create_three_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    if unapplied:
        _run(tmp_path, environment, "stg", "pop", "--all")
    (tmp_path / "fourth.txt").write_text("staged local\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "fourth.txt")
    (tmp_path / "fourth.txt").write_text("unstaged local\n", encoding="utf-8")
    original_status = _run(tmp_path, environment, "git", "status", "--porcelain")

    repository.patch_operation("hide", "second")
    patches = {patch.name: patch for patch in repository.load_stack()}
    assert patches["second"].status == PatchStatus.HIDDEN
    repository.patch_operation("unhide", "second")
    patches = {patch.name: patch for patch in repository.load_stack()}
    assert patches["second"].status == PatchStatus.UNAPPLIED
    repository.patch_operation("delete", "second")
    assert [patch.name for patch in repository.load_stack()] == ["first", "third"]
    assert _run(tmp_path, environment, "git", "status", "--porcelain") == original_status
    assert "+staged local" in _run(tmp_path, environment, "git", "diff", "--cached")
    assert (tmp_path / "fourth.txt").read_text(encoding="utf-8") == "unstaged local\n"
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.parametrize(
    "operation,patch",
    [("commit", "second"), ("delete", "first"), ("hide", "first")],
)
def test_conflicting_patch_operations_can_be_undone(
    tmp_path: Path, operation: str, patch: str
) -> None:
    environment = _git_environment()
    _create_conflicting_two_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    original_series = _run(tmp_path, environment, "stg", "series", "--all")
    original_head = _run(tmp_path, environment, "git", "rev-parse", "HEAD")
    (tmp_path / "local.txt").write_text("local\n", encoding="utf-8")

    with pytest.raises(CommandError):
        repository.patch_operation(operation, patch)

    assert repository.has_conflicts()
    assert repository.stack_operation_pending
    repository.undo_stack_operation()
    assert _run(tmp_path, environment, "stg", "series", "--all") == original_series
    assert _run(tmp_path, environment, "git", "rev-parse", "HEAD") == original_head
    assert (tmp_path / "local.txt").read_text(encoding="utf-8") == "local\n"
    assert not repository.has_conflicts()
    assert not _run(tmp_path, environment, "git", "stash", "list")


def test_uncommit_with_an_empty_patch_stack(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "commit", "--all")
    repository = StGitRepository(tmp_path)
    original_head = _run(tmp_path, environment, "git", "rev-parse", "HEAD")

    repository.patch_operation("uncommit")

    patches = repository.load_stack()
    assert len(patches) == 1
    assert patches[0].status == PatchStatus.CURRENT
    assert "+current layer" in patches[0].diff_text
    assert _run(tmp_path, environment, "git", "rev-parse", "HEAD") == original_head
    assert not _run(tmp_path, environment, "git", "status", "--porcelain")


@pytest.mark.parametrize("status", ["applied", "unapplied", "hidden"])
def test_delete_selected_patch_in_each_state(tmp_path: Path, status: str) -> None:
    environment = _git_environment()
    _create_three_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    if status != "applied":
        _run(tmp_path, environment, "stg", "pop", "--all")
    if status == "hidden":
        _run(tmp_path, environment, "stg", "hide", "second")

    repository.patch_operation("delete", "second")

    assert [patch.name for patch in repository.load_stack()] == ["first", "third"]
    assert not _run(tmp_path, environment, "git", "status", "--porcelain")
    if status == "applied":
        assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "other\n"
        assert (tmp_path / "third.txt").read_text(encoding="utf-8") == "third\nthird layer\n"


def _create_directory_transfer_stack(
    root: Path, environment: dict[str, str], toward_newer: bool
) -> tuple[str, ...]:
    _create_git_repository(root, environment)
    (root / "dir [x]/nested").mkdir(parents=True)
    paths = ("dir [x]/existing.txt", "dir [x]/nested/new.dat", "dir [x]/deleted.txt")
    (root / paths[0]).write_text("old\n", encoding="utf-8")
    (root / paths[2]).write_text("delete me\n", encoding="utf-8")
    _run(root, environment, "git", "add", ".")
    _run(root, environment, "git", "commit", "-qm", "directory base")
    _run(root, environment, "stg", "init")
    _run(root, environment, "stg", "new", "-m", "First", "first")
    if not toward_newer:
        _run(root, environment, "stg", "new", "-m", "Second", "second")
    (root / paths[0]).write_text("new\n", encoding="utf-8")
    (root / paths[1]).write_bytes(b"\x00binary addition\xff\n")
    (root / paths[2]).unlink()
    _run(root, environment, "git", "add", ".")
    _run(root, environment, "stg", "refresh", "--index")
    if toward_newer:
        _run(root, environment, "stg", "new", "-m", "Second", "second")
    return paths


@pytest.mark.parametrize("toward_newer", [False, True])
def test_move_directory_files_between_patches(tmp_path: Path, toward_newer: bool) -> None:
    environment = _git_environment()
    paths = _create_directory_transfer_stack(tmp_path, environment, toward_newer)
    repository = StGitRepository(tmp_path)
    source, target = ("first", "second") if toward_newer else ("second", "first")
    (tmp_path / "file.txt").write_text("staged local\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "file.txt")
    (tmp_path / "file.txt").write_text("unstaged local\n", encoding="utf-8")
    original_status = _run(tmp_path, environment, "git", "status", "--porcelain")

    assert repository.move_patch_changes(source, paths, toward_newer=toward_newer) == (
        PatchChangeMoveResult.MOVED,
        target,
    )

    patches = {patch.name: patch for patch in repository.load_stack()}
    assert not patches[source].files
    assert {file.path for file in patches[target].files} == set(paths)
    assert (tmp_path / paths[0]).read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / paths[1]).read_bytes() == b"\x00binary addition\xff\n"
    assert not (tmp_path / paths[2]).exists()
    assert _run(tmp_path, environment, "git", "status", "--porcelain") == original_status
    assert "+staged local" in _run(tmp_path, environment, "git", "diff", "--cached")
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "unstaged local\n"
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.parametrize("conflict", [False, True])
def test_failed_directory_move_rolls_back_all_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    environment = _git_environment()
    paths = _create_directory_transfer_stack(tmp_path, environment, True)
    repository = StGitRepository(tmp_path)
    original_stack = repository.load_stack()
    original_head = _run(tmp_path, environment, "git", "rev-parse", "HEAD")
    (tmp_path / "local.txt").write_text("preserved\n", encoding="utf-8")
    run = repository.commands.run

    def fail_second_file(
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        if executable == "stg" and args == ("spill", "--", f":(literal){paths[1]}"):
            if conflict:
                run(executable, *args)
                blob = run("git", "rev-parse", "HEAD:file.txt").strip()
                run(
                    "git",
                    "update-index",
                    "--index-info",
                    input_text=(
                        f"100644 {blob} 1\tfile.txt\n100644 {blob} 2\tfile.txt\n"
                        f"100644 {blob} 3\tfile.txt\n"
                    ),
                )
            raise RepositoryError("Injected second file failure")
        return run(
            executable, *args, allowed_returncodes=allowed_returncodes, input_text=input_text
        )

    monkeypatch.setattr(repository.commands, "run", fail_second_file)
    with pytest.raises(RepositoryError, match="Injected second file failure"):
        repository.move_patch_changes("first", paths, toward_newer=True)
    if conflict:
        assert repository.stack_operation_pending
        repository.undo_stack_operation()

    assert repository.load_stack() == original_stack
    assert _run(tmp_path, environment, "git", "rev-parse", "HEAD") == original_head
    assert (tmp_path / "local.txt").read_text(encoding="utf-8") == "preserved\n"
    assert not repository.has_conflicts()
    assert not _run(tmp_path, environment, "git", "stash", "list")


def test_directory_spill_and_refresh_preserve_other_staged_and_unstaged_changes(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    paths = _create_directory_transfer_stack(tmp_path, environment, False)
    repository = StGitRepository(tmp_path)
    original_stack = repository.load_stack()
    (tmp_path / "file.txt").write_text("unrelated staged\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "file.txt")
    (tmp_path / paths[0]).write_text("new\nlocal unstaged\n", encoding="utf-8")

    repository.spill_current_patch_files(paths)

    assert not repository.load_stack()[-1].files
    assert set(
        _run(tmp_path, environment, "git", "diff", "--cached", "--name-only").splitlines()
    ) == {*paths, "file.txt"}
    assert "+local unstaged" in _run(tmp_path, environment, "git", "diff")
    repository.refresh_staged_files_to_current_patch(paths)
    assert repository.load_stack() == original_stack
    assert (
        _run(tmp_path, environment, "git", "diff", "--cached", "--name-only").strip() == "file.txt"
    )
    assert (tmp_path / paths[0]).read_text(encoding="utf-8") == "new\nlocal unstaged\n"


def test_directory_spill_checks_all_paths_before_changing_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    paths = _create_directory_transfer_stack(tmp_path, environment, False)
    repository = StGitRepository(tmp_path)
    original_stack = repository.load_stack()
    (tmp_path / paths[1]).write_bytes(b"\x00staged change\n")
    _run(tmp_path, environment, "git", "add", "--", paths[1])
    original_index = _run(tmp_path, environment, "git", "write-tree")

    with pytest.raises(RepositoryError, match="Unstage those changes first"):
        repository.spill_current_patch_files(paths)

    assert repository.load_stack() == original_stack
    assert _run(tmp_path, environment, "git", "write-tree") == original_index


def test_directory_refresh_checks_all_paths_before_changing_index(tmp_path: Path) -> None:
    environment = _git_environment()
    paths = _create_directory_transfer_stack(tmp_path, environment, False)
    repository = StGitRepository(tmp_path)
    repository.spill_current_patch_files(paths)
    original_stack = repository.load_stack()
    original_index = _run(tmp_path, environment, "git", "write-tree")

    with pytest.raises(RepositoryError, match="No staged changes remain"):
        repository.refresh_staged_files_to_current_patch((*paths, "missing-file"))

    assert repository.load_stack() == original_stack
    assert _run(tmp_path, environment, "git", "write-tree") == original_index


def test_stage_and_unstage_directory_files_use_exact_paths(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_git_repository(tmp_path, environment)
    (tmp_path / "dir/nested").mkdir(parents=True)
    paths = ("dir/old.txt", "dir/nested/[new].txt")
    (tmp_path / paths[0]).write_text("old\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", ".")
    _run(tmp_path, environment, "git", "commit", "-qm", "directory base")
    (tmp_path / paths[0]).unlink()
    (tmp_path / paths[1]).write_text("new\n", encoding="utf-8")
    (tmp_path / "dir/nested/n.txt").write_text("not selected\n", encoding="utf-8")
    (tmp_path / "file.txt").write_text("unrelated staged\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "file.txt")
    repository = Git(CommandRunner(tmp_path))

    repository.stage_files(paths)

    assert set(
        _run(tmp_path, environment, "git", "diff", "--cached", "--name-only").splitlines()
    ) == {*paths, "file.txt"}
    repository.unstage_files(paths)
    assert (
        _run(tmp_path, environment, "git", "diff", "--cached", "--name-only").strip() == "file.txt"
    )
    assert not (tmp_path / paths[0]).exists()
    assert (tmp_path / paths[1]).read_text(encoding="utf-8") == "new\n"


def test_move_patch_one_step_reorders_only_the_selected_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)

    result = repository.move_patch_one_step("second", toward_newer=True)
    assert result == PatchMoveResult.MOVED
    assert tuple(patch.name for patch in repository.load_stack()) == (
        "first",
        "third",
        "second",
        "fourth",
    )
    result = repository.move_patch_one_step("second", toward_newer=False)
    assert result == PatchMoveResult.MOVED
    assert tuple(patch.name for patch in repository.load_stack()) == (
        "first",
        "second",
        "third",
        "fourth",
    )


def test_unapplied_patch_move_preserves_order_and_application_state(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "pop", "-n", "2")
    repository = StGitRepository(tmp_path)

    result = repository.move_patch_one_step("fourth", toward_newer=False)

    assert result == PatchMoveResult.MOVED
    patches = repository.load_stack()
    assert tuple(patch.name for patch in patches) == (
        "first",
        "second",
        "fourth",
        "third",
    )
    assert tuple(patch.status for patch in patches) == (
        PatchStatus.APPLIED,
        PatchStatus.CURRENT,
        PatchStatus.UNAPPLIED,
        PatchStatus.UNAPPLIED,
    )


@pytest.mark.parametrize("applied_count", [0, 1])
@pytest.mark.parametrize("toward_newer", [False, True])
def test_repeated_unapplied_patch_moves_swap_only_adjacent_patches(
    tmp_path: Path, applied_count: int, toward_newer: bool
) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "pop", "-n", str(4 - applied_count))
    repository = StGitRepository(tmp_path)
    expected_names = ["first", "second", "third", "fourth"]
    original_statuses = tuple(patch.status for patch in repository.load_stack())
    selected = expected_names[applied_count] if toward_newer else "fourth"
    step = 1 if toward_newer else -1

    for _ in range(3 - applied_count):
        index = expected_names.index(selected)
        neighbor = index + step
        expected_names[index], expected_names[neighbor] = (
            expected_names[neighbor],
            expected_names[index],
        )

        assert repository.move_patch_one_step(selected, toward_newer=toward_newer) == (
            PatchMoveResult.MOVED
        )
        patches = repository.load_stack()
        assert [patch.name for patch in patches] == expected_names
        assert tuple(patch.status for patch in patches) == original_statuses

    expected_result = (
        PatchMoveResult.APPLICATION_BOUNDARY
        if applied_count and not toward_newer
        else PatchMoveResult.STACK_EDGE
    )
    assert repository.move_patch_one_step(selected, toward_newer=toward_newer) == expected_result
    assert [patch.name for patch in repository.load_stack()] == expected_names


def test_patch_move_stops_at_stack_and_application_boundaries(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "pop", "-n", "2")
    repository = StGitRepository(tmp_path)

    assert repository.move_patch_one_step("first", toward_newer=False) == (
        PatchMoveResult.STACK_EDGE
    )
    assert repository.move_patch_one_step("second", toward_newer=True) == (
        PatchMoveResult.APPLICATION_BOUNDARY
    )
    assert repository.move_patch_one_step("third", toward_newer=False) == (
        PatchMoveResult.APPLICATION_BOUNDARY
    )


def test_conflicting_sink_can_be_undone_to_the_original_stack(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_conflicting_two_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    original_series = _run(tmp_path, environment, "stg", "series", "--all")
    original_content = (tmp_path / "conflict.txt").read_text(encoding="utf-8")
    original_head = _run(tmp_path, environment, "git", "rev-parse", "HEAD")

    with pytest.raises(CommandError, match="failed"):
        repository.move_patch_one_step("second", toward_newer=False)

    assert repository.has_conflicts()
    assert "UU conflict.txt" in _run(tmp_path, environment, "git", "status", "--short")
    _run(tmp_path, environment, "git", "add", "conflict.txt")
    assert not repository.has_conflicts()
    with pytest.raises(RepositoryError, match="awaiting Undo or Refresh"):
        repository.move_patch_one_step("second", toward_newer=True)
    repository.undo_stack_operation()

    assert not repository.has_conflicts()
    assert _run(tmp_path, environment, "stg", "series", "--all") == original_series
    assert (tmp_path / "conflict.txt").read_text(encoding="utf-8") == original_content
    assert _run(tmp_path, environment, "git", "rev-parse", "HEAD") == original_head
    assert not _run(tmp_path, environment, "git", "status", "--porcelain")


def test_stack_reorder_preserves_untracked_changes(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    (tmp_path / "local.txt").write_text("uncommitted\n", encoding="utf-8")

    assert repository.move_patch_one_step("first", toward_newer=True) == (PatchMoveResult.MOVED)

    patches = repository.load_stack()
    assert [patch.name for patch in patches] == ["second", "first"]
    assert (tmp_path / "local.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_push_and_pop_selected_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)

    repository.patch_operation("pop", "second")

    patches = repository.load_stack()
    second = next(patch for patch in patches if patch.name == "second")
    assert second.status == PatchStatus.UNAPPLIED
    assert sum(patch.status == PatchStatus.UNAPPLIED for patch in patches) == 1

    repository.patch_operation("push", "second")

    patches = repository.load_stack()
    assert next(patch for patch in patches if patch.name == "second").status in {
        PatchStatus.APPLIED,
        PatchStatus.CURRENT,
    }
    assert not any(patch.status == PatchStatus.UNAPPLIED for patch in patches)


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_push_can_target_a_later_unapplied_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "pop", "-n", "2")
    repository = StGitRepository(tmp_path)

    repository.patch_operation("push", "fourth")

    patches = repository.load_stack()
    assert next(patch for patch in patches if patch.name == "fourth").status == (
        PatchStatus.CURRENT
    )
    assert next(patch for patch in patches if patch.name == "third").status == (
        PatchStatus.UNAPPLIED
    )


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_push_and_pop_preserve_staged_unstaged_and_untracked_changes(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)

    base_file = tmp_path / "base.txt"
    base_file.write_text(
        base_file.read_text(encoding="utf-8") + "staged change\n",
        encoding="utf-8",
    )
    _run(tmp_path, environment, "git", "add", "--", "base.txt")
    base_file.write_text(
        base_file.read_text(encoding="utf-8") + "unstaged change\n",
        encoding="utf-8",
    )
    (tmp_path / "local-untracked.txt").write_text("untracked change\n", encoding="utf-8")

    repository.patch_operation("pop", "second")
    repository.patch_operation("push", "second")

    staged_diff = _run(tmp_path, environment, "git", "diff", "--cached", "--", "base.txt")
    unstaged_diff = _run(tmp_path, environment, "git", "diff", "--", "base.txt")
    assert "+staged change" in staged_diff
    assert "+unstaged change" not in staged_diff
    assert "+unstaged change" in unstaged_diff
    assert "+staged change" not in unstaged_diff
    assert (tmp_path / "local-untracked.txt").read_text(encoding="utf-8") == ("untracked change\n")
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_undo_conflicted_stack_operation_restores_stashed_changes(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_conflicting_two_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    original_series = _run(tmp_path, environment, "stg", "series", "--all")
    (tmp_path / "local-untracked.txt").write_text("preserved\n", encoding="utf-8")

    with pytest.raises(CommandError, match="failed"):
        repository.move_patch_one_step("second", toward_newer=False)

    stash_commit = repository.stack_operation_stash_reference
    assert stash_commit is not None
    assert repository.stack_operation_pending
    repository.undo_stack_operation()

    assert not repository.stack_operation_pending
    assert (tmp_path / "local-untracked.txt").read_text(encoding="utf-8") == "preserved\n"
    assert not _run(tmp_path, environment, "git", "stash", "list")
    assert _run(tmp_path, environment, "stg", "series", "--all") == original_series
    assert _run(tmp_path, environment, "git", "status", "--porcelain") == (
        "?? local-untracked.txt\n"
    )


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_refresh_conflicted_stack_operation_restores_stash_after_manual_resolution(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_conflicting_two_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    (tmp_path / "local-untracked.txt").write_text("preserved\n", encoding="utf-8")

    with pytest.raises(CommandError, match="failed"):
        repository.move_patch_one_step("second", toward_newer=False)

    stash_commit = repository.stack_operation_stash_reference
    assert stash_commit is not None
    (tmp_path / "conflict.txt").write_text("manually resolved\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "conflict.txt")

    assert repository.refresh_stack_operation() is None

    assert not repository.stack_operation_pending
    assert (tmp_path / "local-untracked.txt").read_text(encoding="utf-8") == "preserved\n"
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_undo_stash_restore_conflict_reverts_stack_and_restores_index(tmp_path: Path) -> None:
    environment = _git_environment()
    _run(tmp_path, environment, "git", "init", "-q")
    _run(tmp_path, environment, "git", "config", "user.name", "Test")
    _run(tmp_path, environment, "git", "config", "user.email", "test@example.invalid")
    (tmp_path / "file.txt").write_text("base\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "file.txt")
    _run(tmp_path, environment, "git", "commit", "-qm", "base")
    _run(tmp_path, environment, "stg", "init")
    _run(tmp_path, environment, "stg", "new", "-m", "Patch", "patch")
    (tmp_path / "file.txt").write_text("base\npatch\n", encoding="utf-8")
    _run(tmp_path, environment, "stg", "refresh", "-m", "Patch")
    _run(tmp_path, environment, "stg", "pop")
    (tmp_path / "file.txt").write_text("base\nuser\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "file.txt")
    repository = StGitRepository(tmp_path)

    with pytest.raises(RepositoryError, match="restoring saved local changes failed"):
        repository.patch_operation("push", "patch")

    assert repository.stash_restore_conflict
    assert repository.stack_operation_pending
    assert repository.stack_operation_stash_reference is not None
    assert not repository.has_conflicts()
    repository.undo_stack_operation()

    assert not repository.stack_operation_pending
    assert next(patch for patch in repository.load_stack() if patch.name == "patch").status == (
        PatchStatus.UNAPPLIED
    )
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "base\nuser\n"
    assert "+user" in _run(tmp_path, environment, "git", "diff", "--cached")
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_staged_changes_with_auto_merge_marker_are_not_conflicts(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = Git(CommandRunner(tmp_path))
    (tmp_path / ".git" / "AUTO_MERGE").write_text("stale merge tree\n", encoding="utf-8")
    (tmp_path / "dummy-file").write_text("dummy change\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "--", "dummy-file")

    assert not repository.has_conflicts()


def test_discover_rejects_non_repository(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="not a git repository"):
        StGitRepository.discover(tmp_path)


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_load_stack_reads_applied_and_unapplied_patches(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "pop")

    repository = StGitRepository.discover(tmp_path)
    patches = repository.load_stack()

    assert repository.branch == "feature/read-browser"
    assert [patch.name for patch in patches] == ["first", "second"]
    assert [patch.status for patch in patches] == [
        PatchStatus.CURRENT,
        PatchStatus.UNAPPLIED,
    ]
    assert patches[0].files[0].path == "base.txt"
    assert patches[0].files[0].hunks
    assert patches[1].files[0].path == "other.txt"
    assert patches[1].files[0].hunks


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_create_patch_uses_edited_saved_template_and_creates_empty_top_patch(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository.discover(tmp_path)

    template = repository.new_patch_template()
    assert "Patch:" in template
    edited_message = template.replace("Patch: ", "Patch: dialog-created", 1)
    repository.create_patch(edited_message)

    patches = repository.load_stack()
    assert [patch.name for patch in patches] == ["first", "second", "dialog-created"]
    assert patches[-1].status == PatchStatus.CURRENT
    assert patches[-1].files == ()


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_edit_older_patch_uses_template_and_preserves_its_diff(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository.discover(tmp_path)

    template = repository.edit_patch_template("first")
    edited_message = template.replace("Patch:  first", "Patch: first-edited", 1)
    repository.edit_patch("first", edited_message)

    patches = repository.load_stack()
    assert [patch.name for patch in patches] == ["first-edited", "second"]
    assert patches[0].status == PatchStatus.APPLIED
    assert patches[0].files[0].path == "base.txt"
    assert "+first layer" in patches[0].diff_text
    assert patches[1].status == PatchStatus.CURRENT


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_load_staged_and_unstaged_changes_separately(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)

    base_file = tmp_path / "base.txt"
    base_file.write_text("base\nfirst layer\nstaged addition\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "--", "base.txt")
    base_file.write_text(
        "base\nfirst layer\nstaged addition\nunstaged addition\n",
        encoding="utf-8",
    )
    (tmp_path / "new file.txt").write_text("untracked content\n", encoding="utf-8")

    repository = StGitRepository.discover(tmp_path)
    staged = repository.load_staged_changes()
    unstaged = repository.load_unstaged_changes()

    assert [file.path for file in staged] == ["base.txt"]
    assert [file.path for file in unstaged] == ["base.txt", "new file.txt"]
    assert "+staged addition" in staged[0].diff_text
    assert "+unstaged addition" not in staged[0].diff_text
    assert "+unstaged addition" in unstaged[0].diff_text
    assert "+staged addition" not in unstaged[0].diff_text
    assert unstaged[1].status == FileStatus.ADDED
    assert "+untracked content" in unstaged[1].diff_text


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_refresh_staged_file_moves_only_selected_file_into_current_patch(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository.discover(tmp_path)

    selected_file = tmp_path / "other.txt"
    selected_file.write_text(
        selected_file.read_text(encoding="utf-8") + "selected staged\n",
        encoding="utf-8",
    )
    (tmp_path / "base.txt").write_text(
        (tmp_path / "base.txt").read_text(encoding="utf-8") + "other staged\n",
        encoding="utf-8",
    )
    _run(tmp_path, environment, "git", "add", "--", "other.txt", "base.txt")
    selected_file.write_text(
        selected_file.read_text(encoding="utf-8") + "selected unstaged\n",
        encoding="utf-8",
    )

    repository.refresh_staged_files_to_current_patch(("other.txt",))

    patches = repository.load_stack()
    current_patch = next(patch for patch in patches if patch.status == PatchStatus.CURRENT)
    assert current_patch.name == "second"
    assert "+selected staged" in current_patch.diff_text
    assert "+selected unstaged" not in current_patch.diff_text
    staged_diff = _run(tmp_path, environment, "git", "diff", "--cached")
    unstaged_diff = _run(tmp_path, environment, "git", "diff", "--")
    assert "+other staged" in staged_diff
    assert "+selected staged" not in staged_diff
    assert "+selected unstaged" in unstaged_diff
    assert "+other staged" not in unstaged_diff


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_refresh_staged_file_into_empty_current_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "new", "-m", "First empty patch", "empty-first")
    _run(tmp_path, environment, "stg", "new", "-m", "Second empty patch", "empty-second")
    repository = StGitRepository.discover(tmp_path)
    (tmp_path / "dummy-file").write_text("dummy change\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "--", "dummy-file")

    repository.refresh_staged_files_to_current_patch(("dummy-file",))

    patches = repository.load_stack()
    assert patches[-2].name == "empty-first"
    assert patches[-2].files == ()
    assert patches[-1].name == "empty-second"
    assert [file.path for file in patches[-1].files] == ["dummy-file"]
    assert not _run(tmp_path, environment, "git", "diff", "--cached")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_refresh_staged_rename_moves_both_paths_into_current_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository.discover(tmp_path)
    _run(tmp_path, environment, "git", "mv", "other.txt", "renamed.txt")

    repository.refresh_staged_files_to_current_patch(("renamed.txt",))

    current_patch = next(
        patch for patch in repository.load_stack() if patch.status == PatchStatus.CURRENT
    )
    assert {(file.path, file.status) for file in current_patch.files} == {
        ("other.txt", FileStatus.DELETED),
        ("renamed.txt", FileStatus.ADDED),
    }
    assert not _run(tmp_path, environment, "git", "diff", "--cached")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_spill_current_patch_file_preserves_other_staged_and_same_file_unstaged_changes(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository.discover(tmp_path)

    selected_file = tmp_path / "other.txt"
    selected_file.write_text(
        selected_file.read_text(encoding="utf-8") + "unstaged selected\n",
        encoding="utf-8",
    )
    (tmp_path / "base.txt").write_text(
        (tmp_path / "base.txt").read_text(encoding="utf-8") + "other staged\n",
        encoding="utf-8",
    )
    _run(tmp_path, environment, "git", "add", "--", "base.txt")

    repository.spill_current_patch_files(("other.txt",))

    patches = repository.load_stack()
    current_patch = next(patch for patch in patches if patch.status == PatchStatus.CURRENT)
    assert current_patch.name == "second"
    assert not current_patch.files
    staged_diff = _run(tmp_path, environment, "git", "diff", "--cached")
    unstaged_diff = _run(tmp_path, environment, "git", "diff", "--")
    assert "+current layer" in staged_diff
    assert "+other staged" in staged_diff
    assert "+current layer" not in unstaged_diff
    assert "+unstaged selected" in unstaged_diff
    assert "+other staged" not in unstaged_diff


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_spill_current_patch_file_rejects_staged_changes_on_selected_path(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository.discover(tmp_path)
    (tmp_path / "other.txt").write_text(
        (tmp_path / "other.txt").read_text(encoding="utf-8") + "staged selected\n",
        encoding="utf-8",
    )
    _run(tmp_path, environment, "git", "add", "--", "other.txt")

    with pytest.raises(RepositoryError, match="Unstage those changes first"):
        repository.spill_current_patch_files(("other.txt",))

    current_patch = next(
        patch for patch in repository.load_stack() if patch.status == PatchStatus.CURRENT
    )
    assert "+current layer" in current_patch.diff_text
    assert "+staged selected" in _run(tmp_path, environment, "git", "diff", "--cached")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_move_file_to_newer_patch_reapplies_the_stack_and_preserves_local_changes(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)

    third_file = tmp_path / "third.txt"
    third_file.write_text(
        third_file.read_text(encoding="utf-8") + "staged local\n",
        encoding="utf-8",
    )
    _run(tmp_path, environment, "git", "add", "third.txt")
    third_file.write_text(
        third_file.read_text(encoding="utf-8") + "unstaged local\n",
        encoding="utf-8",
    )
    (tmp_path / "local-untracked.txt").write_text("untracked local\n", encoding="utf-8")

    result, target = repository.move_patch_change(
        "first",
        "base.txt",
        toward_newer=True,
    )

    assert (result, target) == (PatchChangeMoveResult.MOVED, "second")
    patches = repository.load_stack()
    assert [patch.name for patch in patches] == ["first", "second", "third", "fourth"]
    assert not patches[0].files
    assert "+first layer" in patches[1].diff_text
    assert "+current layer" in patches[1].diff_text
    assert "+third layer" in patches[2].diff_text
    assert "+fourth layer" in patches[3].diff_text
    staged_diff = _run(tmp_path, environment, "git", "diff", "--cached")
    unstaged_diff = _run(tmp_path, environment, "git", "diff")
    assert "+staged local" in staged_diff
    assert "+unstaged local" not in staged_diff
    assert "+unstaged local" in unstaged_diff
    assert "+staged local" not in unstaged_diff
    assert (tmp_path / "local-untracked.txt").read_text(encoding="utf-8") == ("untracked local\n")
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
@pytest.mark.parametrize("toward_newer", [False, True])
@pytest.mark.parametrize("binary", [False, True])
def test_move_added_file_between_patches(tmp_path: Path, toward_newer: bool, binary: bool) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    source = "first" if toward_newer else "second"
    target = "second" if toward_newer else "first"
    if toward_newer:
        _run(tmp_path, environment, "stg", "pop")
    path = "new [file].dat"
    content = b"new file\n" if not binary else b"\x00new file\xff\n"
    (tmp_path / path).write_bytes(content)
    _run(tmp_path, environment, "git", "add", "--", path)
    _run(tmp_path, environment, "stg", "refresh", "--index")
    if toward_newer:
        _run(tmp_path, environment, "stg", "push")
    repository = StGitRepository(tmp_path)

    assert repository.move_patch_change(source, path, toward_newer=toward_newer) == (
        PatchChangeMoveResult.MOVED,
        target,
    )

    patches = {patch.name: patch for patch in repository.load_stack()}
    assert path not in [file.path for file in patches[source].files]
    assert path in [file.path for file in patches[target].files]
    assert (tmp_path / path).read_bytes() == content
    assert not _run(tmp_path, environment, "git", "status", "--porcelain")
    _run(tmp_path, environment, "stg", "pop", "--all")
    _run(tmp_path, environment, "stg", "push", "--all")
    assert (tmp_path / path).read_bytes() == content


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_move_file_from_newer_to_older_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)

    result, target = repository.move_patch_change(
        "second",
        "other.txt",
        toward_newer=False,
    )

    patches = repository.load_stack()
    assert (result, target) == (PatchChangeMoveResult.MOVED, "first")
    assert "+current layer" in patches[0].diff_text
    assert not patches[1].files
    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    assert not _run(tmp_path, environment, "git", "diff")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_move_file_to_older_patch_restores_patches_above_source(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)

    result, target = repository.move_patch_change(
        "third",
        "third.txt",
        toward_newer=False,
    )

    patches = repository.load_stack()
    assert (result, target) == (PatchChangeMoveResult.MOVED, "second")
    assert [patch.name for patch in patches] == ["first", "second", "third", "fourth"]
    assert "+third layer" in patches[1].diff_text
    assert not patches[2].files
    assert "+fourth layer" in patches[3].diff_text
    assert patches[3].status == PatchStatus.CURRENT
    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    assert not _run(tmp_path, environment, "git", "diff")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_move_binary_file_between_adjacent_patches(tmp_path: Path) -> None:
    environment = _git_environment()
    _run(tmp_path, environment, "git", "init", "-q")
    _run(tmp_path, environment, "git", "config", "user.name", "Test")
    _run(tmp_path, environment, "git", "config", "user.email", "test@example.invalid")
    binary_name = "binary [data].dat"
    binary_path = tmp_path / binary_name
    binary_path.write_bytes(b"\x00base\xff\n")
    _run(tmp_path, environment, "git", "add", "--", binary_name)
    _run(tmp_path, environment, "git", "commit", "-qm", "base")
    _run(tmp_path, environment, "stg", "init")
    _run(tmp_path, environment, "stg", "new", "-m", "First patch", "first")
    binary_path.write_bytes(b"\x00base\xff\nfirst patch\x00\n")
    _run(tmp_path, environment, "stg", "refresh", "-m", "First patch")
    _run(tmp_path, environment, "stg", "new", "-m", "Second patch", "second")
    repository = StGitRepository(tmp_path)

    assert repository.load_stack()[0].files[0].status == FileStatus.BINARY
    result, target = repository.move_patch_change(
        "first",
        binary_name,
        toward_newer=True,
    )

    patches = repository.load_stack()
    assert (result, target) == (PatchChangeMoveResult.MOVED, "second")
    assert not patches[0].files
    assert patches[1].files[0].status == FileStatus.BINARY
    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    assert not _run(tmp_path, environment, "git", "diff")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_patch_change_move_respects_applied_boundary_and_unapplied_source(
    tmp_path: Path,
) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    _run(tmp_path, environment, "stg", "pop", "-n", "2")
    repository = StGitRepository(tmp_path)

    assert repository.move_patch_change("second", "other.txt", toward_newer=True) == (
        PatchChangeMoveResult.APPLICATION_BOUNDARY,
        None,
    )
    assert repository.move_patch_change("third", "third.txt", toward_newer=False) == (
        PatchChangeMoveResult.SOURCE_NOT_APPLIED,
        None,
    )


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_move_one_hunk_from_older_to_newer_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_hunk_transfer_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    patches = repository.load_stack()
    first_hunk = patches[0].files[0].hunks[0].diff_text

    result, target = repository.move_patch_change(
        "first",
        "file.txt",
        toward_newer=True,
        hunk_diff=first_hunk,
    )

    patches = repository.load_stack()
    assert (result, target) == (PatchChangeMoveResult.MOVED, "second")
    assert "+first patch early" not in patches[0].diff_text
    assert "+first patch late" in patches[0].diff_text
    assert "+first patch early" in patches[1].diff_text
    assert "+first patch late" not in patches[1].diff_text
    assert "+second patch early" in patches[1].diff_text
    assert "+second patch late" in patches[1].diff_text
    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    assert not _run(tmp_path, environment, "git", "diff")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_move_one_hunk_from_newer_to_older_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_hunk_transfer_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    patches = repository.load_stack()
    selected_hunk = patches[1].files[0].hunks[0].diff_text

    result, target = repository.move_patch_change(
        "second",
        "file.txt",
        toward_newer=False,
        hunk_diff=selected_hunk,
    )

    patches = repository.load_stack()
    assert (result, target) == (PatchChangeMoveResult.MOVED, "first")
    assert "+first patch early" in patches[0].diff_text
    assert "+first patch late" in patches[0].diff_text
    assert "+second patch early" in patches[0].diff_text
    assert "+second patch late" not in patches[0].diff_text
    assert "+second patch early" not in patches[1].diff_text
    assert "+second patch late" in patches[1].diff_text
    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    assert not _run(tmp_path, environment, "git", "diff")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_failed_multi_step_patch_change_move_rolls_back_all_stack_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    original_series = _run(tmp_path, environment, "stg", "series", "--all")
    original_head = _run(tmp_path, environment, "git", "rev-parse", "HEAD")
    run = repository.commands.run
    fail_push = True

    def fail_before_push(
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        nonlocal fail_push
        if executable == "stg" and args[:2] == ("push", "--set-tree") and fail_push:
            fail_push = False
            raise RepositoryError("Injected push failure")
        return run(
            executable,
            *args,
            allowed_returncodes=allowed_returncodes,
            input_text=input_text,
        )

    monkeypatch.setattr(repository.commands, "run", fail_before_push)
    with pytest.raises(RepositoryError, match="Injected push failure"):
        repository.move_patch_change(
            "first",
            "base.txt",
            toward_newer=True,
        )

    assert _run(tmp_path, environment, "stg", "series", "--all") == original_series
    assert _run(tmp_path, environment, "git", "rev-parse", "HEAD") == original_head
    assert not _run(tmp_path, environment, "git", "status", "--porcelain")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_undo_conflicted_multi_step_patch_change_move_restores_stash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _git_environment()
    _create_four_patch_stack(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    original_series = _run(tmp_path, environment, "stg", "series", "--all")
    (tmp_path / "local-untracked.txt").write_text("preserved\n", encoding="utf-8")
    run = repository.commands.run
    injected_conflict = False

    def conflict_after_push(
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        nonlocal injected_conflict
        output = run(
            executable,
            *args,
            allowed_returncodes=allowed_returncodes,
            input_text=input_text,
        )
        if executable == "stg" and args[:2] == ("push", "--set-tree") and not injected_conflict:
            injected_conflict = True
            base_blob = run("git", "rev-parse", "HEAD:base.txt").strip()
            other_blob = run("git", "hash-object", "-w", "--stdin", input_text="conflict\n").strip()
            run(
                "git",
                "update-index",
                "--index-info",
                input_text=(
                    f"100644 {base_blob} 1\tbase.txt\n"
                    f"100644 {other_blob} 2\tbase.txt\n"
                    f"100644 {base_blob} 3\tbase.txt\n"
                ),
            )
            raise CommandError(("stg", *args), 2, "Injected merge conflict")
        return output

    monkeypatch.setattr(repository.commands, "run", conflict_after_push)
    with pytest.raises(CommandError, match="Injected merge conflict"):
        repository.move_patch_change("first", "base.txt", toward_newer=True)

    assert repository.has_conflicts()
    assert repository.stack_operation_pending
    repository.undo_stack_operation()

    assert not repository.has_conflicts()
    assert not repository.stack_operation_pending
    assert _run(tmp_path, environment, "stg", "series", "--all") == original_series
    assert (tmp_path / "local-untracked.txt").read_text(encoding="utf-8") == "preserved\n"
    assert _run(tmp_path, environment, "git", "status", "--porcelain") == (
        "?? local-untracked.txt\n"
    )
    assert not _run(tmp_path, environment, "git", "stash", "list")


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for integration tests")
def test_stage_and_unstage_file_preserves_worktree_changes(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_git_repository(tmp_path, environment)
    repository = Git(CommandRunner(tmp_path))
    path = "file.txt"
    file_path = tmp_path / path
    file_path.write_text(
        file_path.read_text(encoding="utf-8") + "staged change\n",
        encoding="utf-8",
    )

    repository.stage_files((path,))
    file_path.write_text(
        file_path.read_text(encoding="utf-8") + "unstaged change\n",
        encoding="utf-8",
    )
    repository.unstage_files((path,))

    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    worktree_diff = _run(tmp_path, environment, "git", "diff", "--", path)
    assert "+staged change" in worktree_diff
    assert "+unstaged change" in worktree_diff

    untracked_path = "new file.txt"
    (tmp_path / untracked_path).write_text("new content\n", encoding="utf-8")
    repository.stage_files((untracked_path,))
    assert "+new content" in _run(tmp_path, environment, "git", "diff", "--cached")
    repository.unstage_files((untracked_path,))
    status = _run(tmp_path, environment, "git", "status", "--short")
    assert any(line.startswith("?? ") for line in status.splitlines())


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for integration tests")
def test_stage_and_unstage_selected_hunk_preserves_sibling_hunks(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_git_repository(tmp_path, environment)
    repository = Git(CommandRunner(tmp_path))
    lines = (tmp_path / "file.txt").read_text(encoding="utf-8").splitlines()
    lines[15] = "preexisting staged change"
    (tmp_path / "file.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(tmp_path, environment, "git", "add", "--", "file.txt")
    lines[3] = "first changed"
    lines[30] = "second changed"
    (tmp_path / "file.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    repository.stage_hunk("file.txt", 1)

    staged_diff = _run(tmp_path, environment, "git", "diff", "--cached", "--", "file.txt")
    unstaged_diff = _run(tmp_path, environment, "git", "diff", "--", "file.txt")
    assert "+preexisting staged change" in staged_diff
    assert "+second changed" in staged_diff
    assert "+first changed" not in staged_diff
    assert "+first changed" in unstaged_diff
    assert "+second changed" not in unstaged_diff

    repository.unstage_hunk("file.txt", 1)

    remaining_staged_diff = _run(tmp_path, environment, "git", "diff", "--cached", "--", "file.txt")
    remaining_unstaged_diff = _run(tmp_path, environment, "git", "diff", "--", "file.txt")
    assert "+preexisting staged change" in remaining_staged_diff
    assert "+second changed" not in remaining_staged_diff
    assert "+first changed" in remaining_unstaged_diff
    assert "+second changed" in remaining_unstaged_diff
    with pytest.raises(RepositoryError, match="did not stage hunk"):
        repository.stage_hunk("file.txt", 99)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for integration tests")
def test_stage_and_unstage_untracked_file_hunk(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_git_repository(tmp_path, environment)
    repository = Git(CommandRunner(tmp_path))
    untracked_path = "new file.txt"
    (tmp_path / untracked_path).write_text("new content\n", encoding="utf-8")

    repository.stage_hunk(untracked_path, 0)

    assert "+new content" in _run(tmp_path, environment, "git", "diff", "--cached")
    repository.unstage_hunk(untracked_path, 0)

    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    status = _run(tmp_path, environment, "git", "status", "--short")
    assert any(line.startswith("?? ") for line in status.splitlines())
    with pytest.raises(RepositoryError, match="did not stage hunk"):
        repository.stage_hunk(untracked_path, 1)
    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    status = _run(tmp_path, environment, "git", "status", "--short")
    assert any(line.startswith("?? ") for line in status.splitlines())


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("stg") is None,
    reason="Git and StGit are required for integration tests",
)
def test_staged_worktree_hunk_can_refresh_an_earlier_patch(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_two_patch_stack(tmp_path, environment)
    (tmp_path / "base.txt").write_text(
        "base\nfirst layer\nTODO assigned to first\n",
        encoding="utf-8",
    )
    (tmp_path / "other.txt").write_text(
        "other\ncurrent layer\nTODO left in worktree\n",
        encoding="utf-8",
    )
    _run(tmp_path, environment, "git", "add", "-p", "base.txt", input_text="y\nq\n")
    _run(
        tmp_path,
        environment,
        "stg",
        "refresh",
        "--index",
        "--patch",
        "first",
        "--message",
        "Assign selected hunk",
    )

    patches = StGitRepository.discover(tmp_path).load_stack()
    earlier_patch = patches[0]
    current_patch = patches[1]

    assert earlier_patch.name == "first"
    assert "+TODO assigned to first" in earlier_patch.diff_text
    assert "+TODO assigned to first" not in current_patch.diff_text
    assert current_patch.name == "second"
    assert "+TODO left in worktree" in _run(tmp_path, environment, "git", "diff", "--", "other.txt")
    assert not _run(tmp_path, environment, "git", "diff", "--cached")


@pytest.fixture
def hunk_repository(tmp_path: Path) -> StGitRepository:
    environment = _git_environment()
    _run(tmp_path, environment, "git", "init", "-q")
    (tmp_path / "text.txt").write_text("".join(f"line {i}\n" for i in range(100)))
    (tmp_path / "other.txt").write_text("other\n")
    _run(tmp_path, environment, "git", "add", ".")
    _run(tmp_path, environment, "git", "commit", "-qm", "base")
    _run(tmp_path, environment, "stg", "init")
    _run(tmp_path, environment, "stg", "new", "-m", "Current", "current")
    return StGitRepository(tmp_path)


@pytest.mark.parametrize("spill", [False, True])
@pytest.mark.parametrize("hunk_index", [0, 1])
def test_move_current_patch_hunk_preserves_other_changes(
    hunk_repository: StGitRepository, spill: bool, hunk_index: int
) -> None:
    repository = hunk_repository
    git = repository.git
    path = git.root / "text.txt"
    base = path.read_text()
    patched = base.replace("line 10\n", "patch first\nextra patch\n").replace(
        "line 70\n", "patch second\n"
    )
    path.write_text(patched)
    git.stage_files((path.name,))
    repository.stg.refresh()
    staged = patched.replace("line 30\n", "staged first\nextra staged\n").replace(
        "line 90\n", "staged second\n"
    )
    path.write_text(staged)
    (git.root / "other.txt").write_text("staged other\n")
    git.stage_files((path.name, "other.txt"))
    worktree = staged.replace("line 50\n", "unstaged\n")
    path.write_text(worktree)
    (git.root / "untracked.txt").write_text("untracked\n")
    index = git.commands.run("git", "write-tree")
    source = repository.load_stack()[0].files if spill else repository.load_staged_changes()
    file = next(file for file in source if file.path == path.name)
    assert len(file.hunks) == 2
    selected = file.hunks[hunk_index].diff_text
    if spill:
        repository.spill_current_patch_hunk(path.name, selected)
        expected = (
            base.replace("line 70\n", "patch second\n")
            if hunk_index == 0
            else base.replace("line 10\n", "patch first\nextra patch\n")
        )
    else:
        repository.refresh_staged_hunk_to_current_patch(path.name, selected)
        expected = (
            patched.replace("line 30\n", "staged first\nextra staged\n")
            if hunk_index == 0
            else patched.replace("line 90\n", "staged second\n")
        )
    assert git.commands.run("git", "show", "HEAD:text.txt") == expected
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == worktree
    assert (git.root / "other.txt").read_text() == "staged other\n"
    assert (git.root / "untracked.txt").read_text() == "untracked\n"


@pytest.mark.parametrize("addition", [False, True])
def test_current_patch_added_or_deleted_hunk_round_trip(
    hunk_repository: StGitRepository, addition: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    path = git.root / ("added.txt" if addition else "other.txt")
    if addition:
        path.write_text("added\n")
    else:
        path.unlink()
    git.stage_files((path.name,))
    repository.stg.refresh()
    original_head = git.commands.run("git", "rev-parse", "HEAD^{tree}")
    index = git.commands.run("git", "write-tree")
    file = next(file for file in repository.load_stack()[0].files if file.path == path.name)
    repository.spill_current_patch_hunk(path.name, file.hunks[0].diff_text)
    (staged,) = repository.load_staged_changes()
    assert staged.path == path.name
    assert git.commands.run("git", "write-tree") == index
    assert path.exists() == addition
    repository.refresh_staged_hunk_to_current_patch(path.name, staged.hunks[0].diff_text)
    assert git.commands.run("git", "rev-parse", "HEAD^{tree}") == original_head
    assert git.commands.run("git", "write-tree") == index
    assert repository.load_staged_changes() == ()
    assert path.exists() == addition


@pytest.mark.parametrize("spill", [False, True])
def test_hunk_move_failure_restores_index(
    hunk_repository: StGitRepository, monkeypatch: pytest.MonkeyPatch, spill: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    path = git.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "changed\n"))
    git.stage_files((path.name,))
    if spill:
        repository.stg.refresh()
    source = repository.load_stack()[0].files if spill else repository.load_staged_changes()
    file = next(file for file in source if file.path == path.name)
    index = git.commands.run("git", "write-tree")
    head = git.commands.run("git", "rev-parse", "HEAD")
    worktree = path.read_bytes()

    def fail_refresh() -> str:
        raise RepositoryError("refresh failed")

    monkeypatch.setattr(repository.stg, "refresh", fail_refresh)
    operation = (
        repository.spill_current_patch_hunk
        if spill
        else repository.refresh_staged_hunk_to_current_patch
    )
    with pytest.raises(RepositoryError, match="no longer present"):
        operation(path.name, "stale hunk")
    with pytest.raises(RepositoryError, match="refresh failed"):
        operation(path.name, file.hunks[0].diff_text)
    assert git.commands.run("git", "write-tree") == index
    assert git.commands.run("git", "rev-parse", "HEAD") == head
    assert path.read_bytes() == worktree


def test_discover_loads_repository_identity_from_subdirectory(
    hunk_repository: StGitRepository,
) -> None:
    root = hunk_repository.root
    hunk_repository.commands.run("git", "remote", "add", "origin", "git@example.com:repo.git")
    nested = root / "nested"
    nested.mkdir()
    repository = StGitRepository.discover(nested)
    assert repository.root == root
    assert repository.branch == hunk_repository.git.current_branch()
    assert repository.remote_url == "git@example.com:repo.git"


def test_patch_diffstat_handles_empty_text_and_binary_patches(
    hunk_repository: StGitRepository,
) -> None:
    repository = hunk_repository
    assert repository.patch_diffstat("current") == "No changes."
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "changed\n"))
    repository.git.stage_files((path.name,))
    repository.stg.refresh()
    expected = repository.commands.run(
        "git", "diff", "--stat", "--no-color", "HEAD^", "HEAD"
    ).rstrip()
    assert repository.patch_diffstat("current").split() == expected.split()
    (repository.root / "binary.dat").write_bytes(b"\0binary\xff")
    repository.git.stage_files(("binary.dat",))
    repository.stg.refresh()
    stat = repository.patch_diffstat("current")
    assert "binary.dat" in stat and "Bin" in stat


@pytest.mark.parametrize("target", [10, 70])
@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
def test_refresh_selected_staged_lines_preserves_other_changes(
    hunk_repository: StGitRepository, target: int, part: str
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 2\n", "existing patch\n"))
    git.stage_files((path.name,))
    repository.stg.refresh()
    base = path.read_text()
    staged = base.replace("line 10\n", "first 10\nsecond 10\n").replace(
        "line 70\n", "first 70\nsecond 70\n"
    )
    path.write_text(staged)
    (repository.root / "other.txt").write_text("other staged\n")
    git.stage_files((path.name, "other.txt"))
    worktree = staged.replace("line 50\n", "unstaged\n")
    path.write_text(worktree)
    index = git.commands.run("git", "write-tree")
    file = next(file for file in repository.load_staged_changes() if file.path == path.name)
    first = f"+first {target}" if part == "addition" else f"-line {target}"
    last = f"+first {target}" if part == "replacement" else first
    hunk = next(hunk for hunk in file.hunks if first in hunk.diff_text.splitlines())
    lines = hunk.diff_text.splitlines()
    selection = HunkLineSelection(file.path, hunk.diff_text, lines.index(first), lines.index(last))
    repository.refresh_staged_lines_to_current_patch(selection)
    old = f"line {target}\n"
    new = f"first {target}\n"
    replacement = {"addition": old + new, "deletion": "", "replacement": new}[part]
    assert git.commands.run("git", "show", "HEAD:text.txt") == base.replace(old, replacement)
    assert git.commands.run("git", "show", "HEAD:other.txt") == "other\n"
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == worktree
    assert (repository.root / "other.txt").read_text() == "other staged\n"


def test_refresh_selected_lines_in_added_file(hunk_repository: StGitRepository) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "new.txt"
    path.write_text("one\ntwo\nthree\n")
    git.stage_files((path.name,))
    index = git.commands.run("git", "write-tree")
    path.write_text("one\ntwo\nthree\nunstaged\n")
    (file,) = repository.load_staged_changes()
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+two")
    repository.refresh_staged_lines_to_current_patch(
        HunkLineSelection(file.path, hunk.diff_text, row, row)
    )
    assert git.commands.run("git", "show", "HEAD:new.txt") == "two\n"
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == "one\ntwo\nthree\nunstaged\n"


def test_refresh_selected_lines_restores_index_on_failure(
    hunk_repository: StGitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = hunk_repository
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "changed\n"))
    repository.git.stage_files((path.name,))
    (file,) = repository.load_staged_changes()
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+changed")
    selection = HunkLineSelection(file.path, hunk.diff_text, row, row)
    index = repository.commands.run("git", "write-tree")
    head = repository.commands.run("git", "rev-parse", "HEAD")
    worktree = path.read_bytes()

    def fail_refresh() -> str:
        raise RepositoryError("refresh failed")

    monkeypatch.setattr(repository.stg, "refresh", fail_refresh)
    with pytest.raises(RepositoryError, match="refresh failed"):
        repository.refresh_staged_lines_to_current_patch(selection)
    assert repository.commands.run("git", "write-tree") == index
    assert repository.commands.run("git", "rev-parse", "HEAD") == head
    assert path.read_bytes() == worktree


def test_refresh_selected_lines_in_deleted_file(hunk_repository: StGitRepository) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "text.txt"
    base = path.read_text()
    path.unlink()
    git.stage_files((path.name,))
    index = git.commands.run("git", "write-tree")
    (file,) = repository.load_staged_changes()
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("-line 10")
    repository.refresh_staged_lines_to_current_patch(
        HunkLineSelection(file.path, hunk.diff_text, row, row)
    )
    assert git.commands.run("git", "show", "HEAD:text.txt") == base.replace("line 10\n", "")
    assert git.commands.run("git", "write-tree") == index
    assert not path.exists()


def test_refresh_selected_lines_without_final_newline(hunk_repository: StGitRepository) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "other.txt"
    path.write_text("before")
    git.stage_files((path.name,))
    repository.stg.refresh()
    path.write_text("after")
    git.stage_files((path.name,))
    index = git.commands.run("git", "write-tree")
    (file,) = repository.load_staged_changes()
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+after")
    repository.refresh_staged_lines_to_current_patch(
        HunkLineSelection(file.path, hunk.diff_text, row, row)
    )
    assert git.commands.run("git", "show", "HEAD:other.txt") == "before\nafter"
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == "after"


@pytest.mark.parametrize("stale", [False, True])
def test_refresh_selected_lines_rejects_missing_patch_or_stale_selection(
    hunk_repository: StGitRepository, stale: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "text.txt"
    base = path.read_text()
    path.write_text(base.replace("line 10\n", "changed\n"))
    git.stage_files((path.name,))
    (file,) = repository.load_staged_changes()
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+changed")
    selection = HunkLineSelection(file.path, hunk.diff_text, row, row)
    if stale:
        path.write_text(base.replace("line 10\n", "newer\n"))
        git.stage_files((path.name,))
    else:
        git.unstage_files((path.name,))
        path.write_text(base)
        repository.stg.pop("current")
        path.write_text(base.replace("line 10\n", "changed\n"))
        git.stage_files((path.name,))
    index = git.commands.run("git", "write-tree")
    message = "no longer present" if stale else "no current patch"
    with pytest.raises(RepositoryError, match=message):
        repository.refresh_staged_lines_to_current_patch(selection)
    assert git.commands.run("git", "write-tree") == index


def test_refresh_selected_lines_keeps_rename_staged(hunk_repository: StGitRepository) -> None:
    repository = hunk_repository
    git = repository.git
    old = repository.root / "text.txt"
    path = repository.root / "renamed.txt"
    base = old.read_text()
    old.rename(path)
    path.write_text(base.replace("line 10\n", "changed\n"))
    git.stage_files((old.name, path.name))
    index = git.commands.run("git", "write-tree")
    (file,) = repository.load_staged_changes()
    assert file.status == FileStatus.RENAMED
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    selection = HunkLineSelection(
        file.path, hunk.diff_text, rows.index("-line 10"), rows.index("+changed")
    )
    repository.refresh_staged_lines_to_current_patch(selection)
    assert git.commands.run("git", "show", "HEAD:text.txt") == path.read_text()
    assert git.commands.run("git", "write-tree") == index
    assert not old.exists()
    (remaining,) = repository.load_staged_changes()
    assert remaining.status == FileStatus.RENAMED and not remaining.hunks


@pytest.mark.parametrize("target", [10, 70])
@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
def test_move_current_patch_selected_lines_to_staged_preserves_local_changes(
    hunk_repository: StGitRepository, target: int, part: str
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "text.txt"
    base = path.read_text()
    patched = base.replace("line 10\n", "first 10\nsecond 10\n").replace(
        "line 70\n", "first 70\nsecond 70\n"
    )
    path.write_text(patched)
    git.stage_files((path.name,))
    repository.stg.refresh()
    staged = patched.replace("line 30\n", "staged 30\n")
    path.write_text(staged)
    (repository.root / "other.txt").write_text("staged other\n")
    git.stage_files((path.name, "other.txt"))
    worktree = staged.replace("line 50\n", "unstaged 50\n")
    path.write_text(worktree)
    index = git.commands.run("git", "write-tree")
    file = next(file for file in repository.load_stack()[0].files if file.path == path.name)
    first = f"+first {target}" if part == "addition" else f"-line {target}"
    last = f"+first {target}" if part == "replacement" else first
    hunk = next(hunk for hunk in file.hunks if first in hunk.diff_text.splitlines())
    rows = hunk.diff_text.splitlines()
    repository.move_current_patch_lines_to_staged(
        HunkLineSelection(file.path, hunk.diff_text, rows.index(first), rows.index(last))
    )
    new = f"first {target}\n"
    old = f"line {target}\n"
    replacement = {"addition": "", "deletion": old + new, "replacement": old}[part]
    assert git.commands.run("git", "show", "HEAD:text.txt") == patched.replace(new, replacement)
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == worktree
    assert (repository.root / "other.txt").read_text() == "staged other\n"
    (unstaged,) = repository.load_unstaged_changes()
    assert "+unstaged 50" in unstaged.diff_text
    assert f"first {target}" not in unstaged.diff_text


@pytest.mark.parametrize("addition", [False, True])
@pytest.mark.parametrize("all_lines", [False, True])
def test_move_added_or_deleted_patch_lines_to_staged(
    hunk_repository: StGitRepository, addition: bool, all_lines: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / ("new.txt" if addition else "text.txt")
    original = "one\ntwo\nthree\n" if addition else path.read_text()
    if addition:
        path.write_text(original)
    else:
        path.unlink()
    git.stage_files((path.name,))
    repository.stg.refresh()
    index = git.commands.run("git", "write-tree")
    (file,) = repository.load_stack()[0].files
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    first = 1 if all_lines else rows.index("+two" if addition else "-line 10")
    last = len(rows) - 1 if all_lines else first
    repository.move_current_patch_lines_to_staged(
        HunkLineSelection(file.path, hunk.diff_text, first, last)
    )
    if addition and all_lines:
        assert (
            path.name not in git.commands.run("git", "ls-tree", "--name-only", "HEAD").splitlines()
        )
    else:
        expected = (
            original if all_lines else original.replace("two\n", "") if addition else "line 10\n"
        )
        assert git.commands.run("git", "show", f"HEAD:{path.name}") == expected
    assert git.commands.run("git", "write-tree") == index
    assert repository.load_staged_changes()
    assert repository.load_unstaged_changes() == ()
    assert path.exists() == addition


def test_move_patch_lines_to_staged_restores_index_after_refresh_failure(
    hunk_repository: StGitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "patched\n"))
    git.stage_files((path.name,))
    repository.stg.refresh()
    path.write_text(path.read_text().replace("line 30\n", "staged\n"))
    git.stage_files((path.name,))
    index = git.commands.run("git", "write-tree")
    head = git.commands.run("git", "rev-parse", "HEAD")
    worktree = path.read_bytes()
    (file,) = repository.load_stack()[0].files
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+patched")

    def fail_refresh() -> str:
        raise RepositoryError("refresh failed")

    monkeypatch.setattr(repository.stg, "refresh", fail_refresh)
    with pytest.raises(RepositoryError, match="refresh failed"):
        repository.move_current_patch_lines_to_staged(
            HunkLineSelection(file.path, hunk.diff_text, row, row)
        )
    assert git.commands.run("git", "write-tree") == index
    assert git.commands.run("git", "rev-parse", "HEAD") == head
    assert path.read_bytes() == worktree


def test_move_patch_lines_to_staged_preserves_overlapping_staged_edits(
    hunk_repository: StGitRepository,
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "text.txt"
    base = path.read_text()
    path.write_text(base.replace("line 10\n", "patched\n"))
    git.stage_files((path.name,))
    repository.stg.refresh()
    path.write_text(path.read_text().replace("patched\n", "staged edit\n"))
    git.stage_files((path.name,))
    worktree = path.read_text().replace("line 50\n", "unstaged\n")
    path.write_text(worktree)
    index = git.commands.run("git", "write-tree")
    (file,) = repository.load_stack()[0].files
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+patched")
    repository.move_current_patch_lines_to_staged(
        HunkLineSelection(file.path, hunk.diff_text, row, row)
    )
    assert git.commands.run("git", "show", "HEAD:text.txt") == base.replace("line 10\n", "")
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == worktree
    assert "+staged edit" in repository.load_staged_changes()[0].diff_text


def test_move_patch_lines_to_staged_keeps_rename_in_current_patch(
    hunk_repository: StGitRepository,
) -> None:
    repository = hunk_repository
    git = repository.git
    old = repository.root / "text.txt"
    path = repository.root / "renamed.txt"
    base = old.read_text()
    old.rename(path)
    path.write_text(base.replace("line 10\n", "patched\n"))
    git.stage_files((old.name, path.name))
    repository.stg.refresh()
    index = git.commands.run("git", "write-tree")
    (file,) = repository.load_stack()[0].files
    assert file.status == FileStatus.RENAMED
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    repository.move_current_patch_lines_to_staged(
        HunkLineSelection(file.path, hunk.diff_text, rows.index("-line 10"), rows.index("+patched"))
    )
    assert git.commands.run("git", "show", "HEAD:renamed.txt") == base
    assert git.commands.run("git", "write-tree") == index
    assert not old.exists()
    (current_file,) = repository.load_stack()[0].files
    assert current_file.status == FileStatus.RENAMED and not current_file.hunks


@pytest.mark.parametrize("addition", [False, True])
def test_move_patch_lines_to_staged_without_final_newline(
    hunk_repository: StGitRepository, addition: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "other.txt"
    path.write_text("patched")
    git.stage_files((path.name,))
    repository.stg.refresh()
    index = git.commands.run("git", "write-tree")
    (file,) = repository.load_stack()[0].files
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+patched" if addition else "-other")
    repository.move_current_patch_lines_to_staged(
        HunkLineSelection(file.path, hunk.diff_text, row, row)
    )
    assert git.commands.run("git", "show", "HEAD:other.txt") == (
        "" if addition else "other\npatched"
    )
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == "patched"


def test_move_patch_lines_to_staged_rejects_stale_selection(
    hunk_repository: StGitRepository,
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "text.txt"
    base = path.read_text()
    path.write_text(base.replace("line 10\n", "patched\n"))
    git.stage_files((path.name,))
    repository.stg.refresh()
    (file,) = repository.load_stack()[0].files
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+patched")
    selection = HunkLineSelection(file.path, hunk.diff_text, row, row)
    path.write_text(base.replace("line 10\n", "newer patch\n"))
    git.stage_files((path.name,))
    repository.stg.refresh()
    index = git.commands.run("git", "write-tree")
    head = git.commands.run("git", "rev-parse", "HEAD")
    with pytest.raises(RepositoryError, match="no longer present"):
        repository.move_current_patch_lines_to_staged(selection)
    assert git.commands.run("git", "write-tree") == index
    assert git.commands.run("git", "rev-parse", "HEAD") == head


def _prepare_partial_line_stack(
    repository: StGitRepository, toward_newer: bool
) -> tuple[str, str, str, str]:
    git = repository.git
    path = repository.root / "text.txt"
    base = path.read_text()
    full = base.replace("line 10\n", "first 10\nsecond 10\n").replace(
        "line 70\n", "first 70\nsecond 70\n"
    )
    initial = path if toward_newer else repository.root / "other.txt"
    initial.write_text(full if toward_newer else "target\n")
    git.stage_files((initial.name,))
    repository.stg.refresh()
    repository.commands.run("stg", "new", "-m", "Second", "second")
    second = repository.root / "other.txt" if toward_newer else path
    second.write_text("target\n" if toward_newer else full)
    git.stage_files((second.name,))
    repository.stg.refresh()
    for name in ("third", "fourth"):
        repository.commands.run("stg", "new", "-m", name, name)
        (repository.root / f"{name}.txt").write_text(name + "\n")
        git.stage_files((f"{name}.txt",))
        repository.stg.refresh()
    return ("current", "second", base, full) if toward_newer else ("second", "current", base, full)


@pytest.mark.parametrize("toward_newer", [False, True])
@pytest.mark.parametrize("target_line", [10, 70])
@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
def test_move_selected_lines_between_patches_preserves_combined_tree_and_local_changes(
    hunk_repository: StGitRepository, toward_newer: bool, target_line: int, part: str
) -> None:
    repository = hunk_repository
    git = repository.git
    source, target, base, full = _prepare_partial_line_stack(repository, toward_newer)
    path = repository.root / "text.txt"
    path.write_text(full.replace("line 30\n", "local staged\n"))
    (repository.root / "other.txt").write_text("local staged other\n")
    git.stage_files((path.name, "other.txt"))
    index = git.commands.run("git", "write-tree")
    tip = git.commands.run("git", "rev-parse", "HEAD^{tree}")
    worktree = path.read_text().replace("line 50\n", "local unstaged\n")
    path.write_text(worktree)
    (repository.root / "untracked.txt").write_text("untracked\n")
    patch = next(patch for patch in repository.load_stack() if patch.name == source)
    (file,) = patch.files
    first = f"+first {target_line}" if part == "addition" else f"-line {target_line}"
    last = f"+first {target_line}" if part == "replacement" else first
    hunk = next(hunk for hunk in file.hunks if first in hunk.diff_text.splitlines())
    rows = hunk.diff_text.splitlines()
    selection = HunkLineSelection(file.path, hunk.diff_text, rows.index(first), rows.index(last))
    result, destination = repository.move_patch_lines(source, selection, toward_newer=toward_newer)
    assert (result, destination) == (PatchChangeMoveResult.MOVED, target)
    new = f"first {target_line}\n"
    old = f"line {target_line}\n"
    remaining = {"addition": "", "deletion": old + new, "replacement": old}[part]
    added = {"addition": old + new, "deletion": "", "replacement": new}[part]
    source_commit = repository.commands.run("stg", "id", source).strip()
    target_commit = repository.commands.run("stg", "id", target).strip()
    assert git.commands.run("git", "show", f"{source_commit}:text.txt") == (
        full.replace(new, remaining) if toward_newer else full
    )
    assert git.commands.run("git", "show", f"{target_commit}:text.txt") == (
        full if toward_newer else base.replace(old, added)
    )
    patches = repository.load_stack()
    assert [patch.name for patch in patches] == ["current", "second", "third", "fourth"]
    source_patch = next(patch for patch in patches if patch.name == source)
    target_patch = next(patch for patch in patches if patch.name == target)
    for line in {first, last}:
        assert line not in source_patch.diff_text.splitlines()
        assert line in target_patch.diff_text.splitlines()
    assert git.commands.run("git", "rev-parse", "HEAD^{tree}") == tip
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == worktree
    assert (repository.root / "other.txt").read_text() == "local staged other\n"
    assert (repository.root / "untracked.txt").read_text() == "untracked\n"
    assert git.commands.run("git", "stash", "list") == ""


@pytest.mark.parametrize("toward_newer", [False, True])
@pytest.mark.parametrize("addition", [False, True])
def test_move_added_or_deleted_selected_lines_between_patches(
    hunk_repository: StGitRepository, toward_newer: bool, addition: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / ("new.txt" if addition else "text.txt")
    original = "one\ntwo\nthree\n" if addition else path.read_text()
    if not toward_newer:
        repository.commands.run("stg", "new", "-m", "Second", "second")
    if addition:
        path.write_text(original)
        path.chmod(0o755)
    else:
        path.unlink()
    git.stage_files((path.name,))
    repository.stg.refresh()
    if toward_newer:
        repository.commands.run("stg", "new", "-m", "Second", "second")
    source, target = ("current", "second") if toward_newer else ("second", "current")
    patch = next(patch for patch in repository.load_stack() if patch.name == source)
    (file,) = patch.files
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+two" if addition else "-line 10")
    tree = git.commands.run("git", "rev-parse", "HEAD^{tree}")
    assert repository.move_patch_lines(
        source, HunkLineSelection(file.path, hunk.diff_text, row, row), toward_newer=toward_newer
    ) == (PatchChangeMoveResult.MOVED, target)
    expected_source = (
        (original.replace("two\n", "") if addition else "line 10\n")
        if toward_newer
        else (original if addition else None)
    )
    expected_target = (
        (original if addition else None)
        if toward_newer
        else ("two\n" if addition else original.replace("line 10\n", ""))
    )
    for name, expected in ((source, expected_source), (target, expected_target)):
        commit = repository.commands.run("stg", "id", name).strip()
        if expected is None:
            assert (
                path.name
                not in git.commands.run("git", "ls-tree", "--name-only", commit).splitlines()
            )
        else:
            assert git.commands.run("git", "show", f"{commit}:{path.name}") == expected
    assert git.commands.run("git", "rev-parse", "HEAD^{tree}") == tree
    assert path.exists() == addition
    if addition:
        assert path.read_text() == original and path.stat().st_mode & 0o111
    assert repository.load_staged_changes() == repository.load_unstaged_changes() == ()


@pytest.mark.parametrize("toward_newer", [False, True])
def test_move_selected_patch_lines_keeps_rename_in_source(
    hunk_repository: StGitRepository, toward_newer: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    old = repository.root / "text.txt"
    path = repository.root / "renamed.txt"
    base = old.read_text()
    if not toward_newer:
        repository.commands.run("stg", "new", "-m", "Second", "second")
    old.rename(path)
    path.write_text(base.replace("line 10\n", "changed\n"))
    git.stage_files((old.name, path.name))
    repository.stg.refresh()
    if toward_newer:
        repository.commands.run("stg", "new", "-m", "Second", "second")
    source, target = ("current", "second") if toward_newer else ("second", "current")
    patch = next(patch for patch in repository.load_stack() if patch.name == source)
    (file,) = patch.files
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    selection = HunkLineSelection(
        file.path, hunk.diff_text, rows.index("-line 10"), rows.index("+changed")
    )
    tree = git.commands.run("git", "rev-parse", "HEAD^{tree}")
    assert repository.move_patch_lines(source, selection, toward_newer=toward_newer) == (
        PatchChangeMoveResult.MOVED,
        target,
    )
    source_patch = next(patch for patch in repository.load_stack() if patch.name == source)
    assert source_patch.files[0].status == FileStatus.RENAMED
    assert not source_patch.files[0].hunks
    target_patch = next(patch for patch in repository.load_stack() if patch.name == target)
    assert any("+changed" in file.diff_text for file in target_patch.files)
    assert git.commands.run("git", "rev-parse", "HEAD^{tree}") == tree
    assert not old.exists() and path.read_text() == base.replace("line 10\n", "changed\n")


@pytest.mark.parametrize("failed_refresh", [1, 2, 3])
def test_failed_partial_line_transfer_restores_stack_and_local_changes(
    hunk_repository: StGitRepository, monkeypatch: pytest.MonkeyPatch, failed_refresh: int
) -> None:
    repository = hunk_repository
    git = repository.git
    source, _, _, full = _prepare_partial_line_stack(repository, False)
    path = repository.root / "text.txt"
    path.write_text(full.replace("line 30\n", "staged\n"))
    git.stage_files((path.name,))
    worktree = path.read_text().replace("line 50\n", "unstaged\n")
    path.write_text(worktree)
    (repository.root / "untracked.txt").write_text("untracked\n")
    index = git.commands.run("git", "write-tree")
    before = repository.load_stack()
    patch = next(patch for patch in before if patch.name == source)
    (file,) = patch.files
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+first 10")
    original_run = repository.commands.run
    refreshes = 0

    def run(
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        nonlocal refreshes
        if executable == "stg" and args[0] == "refresh":
            refreshes += 1
            if refreshes == failed_refresh:
                raise RepositoryError("injected line-transfer failure")
        return original_run(
            executable, *args, allowed_returncodes=allowed_returncodes, input_text=input_text
        )

    monkeypatch.setattr(repository.commands, "run", run)
    with pytest.raises(RepositoryError, match="injected line-transfer failure"):
        repository.move_patch_lines(
            source, HunkLineSelection(file.path, hunk.diff_text, row, row), toward_newer=False
        )
    assert repository.load_stack() == before
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == worktree
    assert (repository.root / "untracked.txt").read_text() == "untracked\n"
    assert git.commands.run("git", "stash", "list") == ""
    assert not repository.stack_operation_pending


@pytest.mark.parametrize("case", ["edge", "boundary", "unapplied", "hidden"])
def test_patch_line_transfer_respects_stack_boundaries(
    hunk_repository: StGitRepository, case: str
) -> None:
    repository = hunk_repository
    _prepare_partial_line_stack(repository, True)
    if case != "edge":
        repository.patch_operation("pop", "fourth")
    if case == "hidden":
        repository.patch_operation("hide", "fourth")
    before = repository.load_stack()
    index = repository.git.commands.run("git", "write-tree")
    name = "current" if case == "edge" else "third" if case == "boundary" else "fourth"
    expected = {
        "edge": PatchChangeMoveResult.STACK_EDGE,
        "boundary": PatchChangeMoveResult.APPLICATION_BOUNDARY,
        "unapplied": PatchChangeMoveResult.SOURCE_NOT_APPLIED,
        "hidden": PatchChangeMoveResult.SOURCE_NOT_APPLIED,
    }[case]
    assert repository.move_patch_lines(
        name, HunkLineSelection("text.txt", "", 0, 0), toward_newer=case != "edge"
    ) == (expected, None)
    assert repository.load_stack() == before
    assert repository.git.commands.run("git", "write-tree") == index


@pytest.mark.parametrize("toward_newer", [False, True])
@pytest.mark.parametrize("addition", [False, True])
def test_move_patch_lines_between_patches_without_final_newline(
    hunk_repository: StGitRepository, toward_newer: bool, addition: bool
) -> None:
    repository = hunk_repository
    git = repository.git
    path = repository.root / "other.txt"
    path.write_text("before")
    git.stage_files((path.name,))
    repository.stg.refresh()
    repository.commands.run("stg", "new", "-m", "Source", "source")
    path.write_text("after")
    git.stage_files((path.name,))
    repository.stg.refresh()
    if toward_newer:
        repository.commands.run("stg", "new", "-m", "Target", "target")
    target = "target" if toward_newer else "current"
    patch = next(patch for patch in repository.load_stack() if patch.name == "source")
    (file,) = patch.files
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+after" if addition else "-before")
    tree = git.commands.run("git", "rev-parse", "HEAD^{tree}")
    assert repository.move_patch_lines(
        "source", HunkLineSelection(file.path, hunk.diff_text, row, row), toward_newer=toward_newer
    ) == (PatchChangeMoveResult.MOVED, target)
    source_commit = repository.commands.run("stg", "id", "source").strip()
    target_commit = repository.commands.run("stg", "id", target).strip()
    expected_partial = "" if addition else "before\nafter"
    expected_added = "before\nafter" if addition else ""
    assert git.commands.run("git", "show", f"{source_commit}:{path.name}") == (
        expected_partial if toward_newer else "after"
    )
    assert git.commands.run("git", "show", f"{target_commit}:{path.name}") == (
        "after" if toward_newer else expected_added
    )
    assert git.commands.run("git", "rev-parse", "HEAD^{tree}") == tree
    assert path.read_text() == "after"
    assert repository.load_staged_changes() == repository.load_unstaged_changes() == ()


@pytest.mark.parametrize("status", ["current", "applied", "unapplied", "hidden"])
@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
def test_discard_patch_selected_lines(
    hunk_repository: StGitRepository, status: str, part: str
) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    base = path.read_text()
    changed = base.replace("line 10\n", "changed\n").replace("line 70\n", "keep\n")
    path.write_text(changed)
    commands.run("stg", "refresh")
    if status == "applied":
        commands.run("stg", "new", "-m", "Later", "later")
        (repository.root / "other.txt").write_text("later\n")
        commands.run("stg", "refresh")
    elif status in ("unapplied", "hidden"):
        commands.run("stg", "pop")
        if status == "hidden":
            commands.run("stg", "hide", "current")
    series = repository.stg.series()
    patch = next(patch for patch in repository.load_stack() if patch.name == "current")
    file = next(file for file in patch.files if file.path == "text.txt")
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    first = rows.index("+changed" if part == "addition" else "-line 10")
    last = rows.index("-line 10" if part == "deletion" else "+changed")
    selection = HunkLineSelection(file.path, hunk.diff_text, first, last)
    # Independent staged, unstaged and untracked changes survive the stack transaction.
    local = path.read_text().replace("line 40\n", "staged\n")
    path.write_text(local)
    repository.git.stage_files((path.name,))
    path.write_text(local.replace("line 90\n", "unstaged\n"))
    (repository.root / "untracked").write_text("keep me\n")
    assert "text.txt" in repository.discarded_lines_diffstat(selection, patch_name="current")
    repository.discard_selected_lines(selection, patch_name="current")
    replacement = {"addition": "", "deletion": "line 10\nchanged\n", "replacement": "line 10\n"}[
        part
    ]
    expected = changed.replace("changed\n", replacement)
    commit = repository.stg.patch_commit("current")
    assert commands.run("git", "show", f"{commit}:text.txt") == expected
    assert repository.stg.series() == series
    assert "+staged" in commands.run("git", "diff", "--no-ext-diff", "--cached")
    assert "+unstaged" in commands.run("git", "diff", "--no-ext-diff")
    assert (repository.root / "untracked").read_text() == "keep me\n"
    assert not repository.has_conflicts()
    if status in ("current", "applied"):
        assert path.read_text() == expected.replace("line 40\n", "staged\n").replace(
            "line 90\n", "unstaged\n"
        )


@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
def test_discard_staged_selected_lines(hunk_repository: StGitRepository, part: str) -> None:
    repository = hunk_repository
    path = repository.root / "text.txt"
    base = path.read_text()
    staged = base.replace("line 10\n", "changed\n").replace("line 70\n", "keep\n")
    path.write_text(staged)
    repository.git.stage_files((path.name,))
    path.write_text(staged.replace("line 90\n", "unstaged\n"))
    file = repository.load_staged_changes()[0]
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    first = rows.index("+changed" if part == "addition" else "-line 10")
    last = rows.index("-line 10" if part == "deletion" else "+changed")
    selection = HunkLineSelection(file.path, hunk.diff_text, first, last)
    repository.discard_selected_lines(selection, staged=True)
    replacement = {"addition": "", "deletion": "line 10\nchanged\n", "replacement": "line 10\n"}[
        part
    ]
    expected = staged.replace("changed\n", replacement)
    assert repository.commands.run("git", "show", ":text.txt") == expected
    assert path.read_text() == expected.replace("line 90\n", "unstaged\n")


def test_discard_staged_lines_rejects_overlapping_worktree_edits(
    hunk_repository: StGitRepository,
) -> None:
    repository = hunk_repository
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "staged\n"))
    repository.git.stage_files((path.name,))
    file = repository.load_staged_changes()[0]
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+staged")
    selection = HunkLineSelection(file.path, hunk.diff_text, row, row)
    index = repository.git.index_tree()
    path.write_text(path.read_text().replace("staged\n", "unstaged\n"))
    before = path.read_bytes()
    with pytest.raises(RepositoryError):
        repository.discard_selected_lines(selection, staged=True)
    assert repository.git.index_tree() == index
    assert path.read_bytes() == before


def test_discard_patch_lines_rolls_back_on_edit_failure(
    hunk_repository: StGitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = hunk_repository
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "changed\n"))
    repository.commands.run("stg", "refresh")
    file = repository.load_stack()[0].files[0]
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+changed")
    selection = HunkLineSelection(file.path, hunk.diff_text, row, row)
    repository.commands.run("stg", "new", "-m", "Later", "later")
    head = repository.commands.run("git", "rev-parse", "HEAD")
    index = repository.git.index_tree()
    before = path.read_bytes()

    def fail(*args: object) -> str:
        raise RepositoryError("edit failed")

    monkeypatch.setattr("stg_layers.stg.StGit.set_patch_tree", fail)
    with pytest.raises(RepositoryError, match="edit failed"):
        repository.discard_selected_lines(selection, patch_name="current")
    assert repository.commands.run("git", "rev-parse", "HEAD") == head
    assert repository.git.index_tree() == index
    assert path.read_bytes() == before


def test_discard_patch_lines_conflict_can_be_undone(hunk_repository: StGitRepository) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "changed\n"))
    commands.run("stg", "refresh")
    file = repository.load_stack()[0].files[0]
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    selection = HunkLineSelection(
        file.path, hunk.diff_text, rows.index("-line 10"), rows.index("+changed")
    )
    commands.run("stg", "new", "-m", "Later", "later")
    path.write_text(path.read_text().replace("changed\n", "dependent\n"))
    commands.run("stg", "refresh")
    head = commands.run("git", "rev-parse", "HEAD")
    path.write_text(path.read_text().replace("line 40\n", "staged\n"))
    repository.git.stage_files((path.name,))
    index = repository.git.index_tree()
    path.write_text(path.read_text().replace("line 90\n", "unstaged\n"))
    before = path.read_bytes()
    with pytest.raises(RepositoryError):
        repository.discard_selected_lines(selection, patch_name="current")
    assert repository.has_conflicts()
    assert repository.stack_operation_pending
    repository.undo_stack_operation()
    assert not repository.has_conflicts()
    assert not repository.stack_operation_pending
    assert commands.run("git", "rev-parse", "HEAD") == head
    assert repository.git.index_tree() == index
    assert path.read_bytes() == before


@pytest.mark.parametrize("source", ["staged", "unstaged"])
@pytest.mark.parametrize("target_status", ["current", "applied", "unapplied", "hidden"])
@pytest.mark.parametrize("scope", ["files", "hunk", "lines"])
def test_move_local_changes_to_arbitrary_patch(
    hunk_repository: StGitRepository, source: ChangeArea, target_status: str, scope: str
) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    base = path.read_text()
    if target_status == "applied":
        commands.run("stg", "new", "-m", "Later", "later")
        (repository.root / "other.txt").write_text("later\n")
        commands.run("stg", "refresh")
    elif target_status in ("unapplied", "hidden"):
        commands.run("stg", "pop")
        if target_status == "hidden":
            commands.run("stg", "hide", "current")
    series = repository.stg.series()
    changed = base.replace("line 10\n", "changed\n").replace("line 70\n", "remaining\n")
    path.write_text(changed)
    if source == "staged":
        repository.git.stage_files((path.name,))
    files = (
        repository.load_staged_changes()
        if source == "staged"
        else repository.load_unstaged_changes()
    )
    file = next(file for file in files if file.path == path.name)
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    selection = ChangeSelection(
        MoveTarget(change_area=source),
        (path.name,),
        hunk_diff=hunk.diff_text if scope == "hunk" else None,
        lines=HunkLineSelection(
            file.path, hunk.diff_text, rows.index("-line 10"), rows.index("+changed")
        )
        if scope == "lines"
        else None,
    )
    local_line = "line 90\n" if source == "unstaged" and scope == "files" else "unstaged\n"
    path.write_text(changed.replace("line 90\n", local_line))
    (repository.root / "untracked").write_text("keep\n")
    repository.move_to_target(selection, MoveTarget(patch_name="current"))
    commit = repository.stg.patch_commit("current")
    patch_text = commands.run("git", "show", f"{commit}:text.txt")
    assert "changed\n" in patch_text
    assert ("remaining\n" in patch_text) == (scope == "files")
    assert "unstaged\n" not in patch_text
    assert repository.stg.series() == series
    assert (repository.root / "untracked").read_text() == "keep\n"
    if target_status in ("current", "applied"):
        assert path.read_text() == changed.replace("line 90\n", local_line)
    else:
        expected = base if scope == "files" else base.replace("line 70\n", "remaining\n")
        assert path.read_text() == expected.replace("line 90\n", local_line)
    staged = commands.run("git", "diff", "--cached", "--no-ext-diff")
    assert ("+remaining" in staged) == (source == "staged" and scope != "files")
    assert "+changed" not in staged
    assert not repository.has_conflicts()


@pytest.mark.parametrize("target", ["staged", "unstaged"])
@pytest.mark.parametrize("status", ["current", "applied", "unapplied", "hidden"])
@pytest.mark.parametrize("scope", ["files", "hunk", "lines", "patch"])
def test_move_patch_changes_to_working_area(
    hunk_repository: StGitRepository, target: ChangeArea, status: str, scope: str
) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    base = path.read_text()
    changed = base.replace("line 10\n", "changed\n").replace("line 70\n", "remaining\n")
    path.write_text(changed)
    commands.run("stg", "refresh")
    if status == "applied":
        commands.run("stg", "new", "-m", "Later", "later")
        (repository.root / "other.txt").write_text("later\n")
        commands.run("stg", "refresh")
    elif status in ("unapplied", "hidden"):
        commands.run("stg", "pop")
        if status == "hidden":
            commands.run("stg", "hide", "current")
    series = repository.stg.series()
    patch = next(patch for patch in repository.load_stack() if patch.name == "current")
    file = patch.files[0]
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    selection = ChangeSelection(
        MoveTarget(patch_name="current"),
        (path.name,),
        hunk_diff=hunk.diff_text if scope == "hunk" else None,
        lines=HunkLineSelection(
            file.path, hunk.diff_text, rows.index("-line 10"), rows.index("+changed")
        )
        if scope == "lines"
        else None,
        whole_patch=scope == "patch",
    )
    local = path.read_text().replace("line 40\n", "staged\n")
    path.write_text(local)
    repository.git.stage_files((path.name,))
    path.write_text(local.replace("line 90\n", "unstaged\n"))
    repository.move_to_target(selection, MoveTarget(change_area=target))
    commit = repository.stg.patch_commit("current")
    expected_patch = (
        base if scope in ("files", "patch") else base.replace("line 70\n", "remaining\n")
    )
    assert commands.run("git", "show", f"{commit}:text.txt") == expected_patch
    assert repository.stg.series() == series
    expected = (
        changed
        if status in ("current", "applied") or scope in ("files", "patch")
        else base.replace("line 10\n", "changed\n")
    )
    assert path.read_text() == expected.replace("line 40\n", "staged\n").replace(
        "line 90\n", "unstaged\n"
    )
    staged = commands.run("git", "diff", "--cached", "--no-ext-diff")
    unstaged = commands.run("git", "diff", "--no-ext-diff")
    assert ("+changed" in staged) == (target == "staged")
    assert ("+changed" in unstaged) == (target == "unstaged")
    assert "+staged" in staged
    assert "+unstaged" in unstaged


@pytest.mark.parametrize("toward_newer", [True, False])
@pytest.mark.parametrize("scope", ["files", "hunk", "lines"])
def test_move_changes_between_nonadjacent_patches(
    hunk_repository: StGitRepository, toward_newer: bool, scope: str
) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    base = path.read_text()
    if not toward_newer:
        commands.run("stg", "new", "-m", "Middle", "middle")
        commands.run("stg", "new", "-m", "Source", "source")
    source = "current" if toward_newer else "source"
    path.write_text(base.replace("line 10\n", "changed\n").replace("line 70\n", "keep\n"))
    commands.run("stg", "refresh")
    if toward_newer:
        commands.run("stg", "new", "-m", "Middle", "middle")
        commands.run("stg", "new", "-m", "Target", "target")
    target = "target" if toward_newer else "current"
    patch = next(patch for patch in repository.load_stack() if patch.name == source)
    file = patch.files[0]
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    selection = ChangeSelection(
        MoveTarget(patch_name=source),
        (path.name,),
        hunk_diff=hunk.diff_text if scope == "hunk" else None,
        lines=HunkLineSelection(
            file.path, hunk.diff_text, rows.index("-line 10"), rows.index("+changed")
        )
        if scope == "lines"
        else None,
    )
    before = path.read_bytes()
    repository.move_to_target(selection, MoveTarget(patch_name=target))
    patches = {patch.name: patch for patch in repository.load_stack()}
    assert "+changed" in patches[target].diff_text
    assert "+changed" not in patches[source].diff_text
    assert not patches["middle"].files
    assert path.read_bytes() == before
    assert not repository.has_local_changes()


@pytest.mark.parametrize("source_area", ["staged", "unstaged"])
@pytest.mark.parametrize("applied", [True, False])
@pytest.mark.parametrize("scope", ["file", "lines"])
def test_move_added_file_to_patch(
    hunk_repository: StGitRepository, source_area: ChangeArea, applied: bool, scope: str
) -> None:
    repository = hunk_repository
    if not applied:
        repository.commands.run("stg", "pop")
    path = repository.root / "new.txt"
    path.write_text("one\ntwo\nthree\n")
    if source_area == "staged":
        repository.git.stage_files((path.name,))
    files = (
        repository.load_staged_changes()
        if source_area == "staged"
        else repository.load_unstaged_changes()
    )
    file = next(file for file in files if file.path == path.name)
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+two")
    selection = ChangeSelection(
        MoveTarget(change_area=source_area),
        (path.name,),
        lines=HunkLineSelection(file.path, hunk.diff_text, row, row) if scope == "lines" else None,
    )
    repository.move_to_target(selection, MoveTarget(patch_name="current"))
    commit = repository.stg.patch_commit("current")
    assert repository.commands.run("git", "show", f"{commit}:new.txt") == (
        "two\n" if scope == "lines" else "one\ntwo\nthree\n"
    )
    if applied:
        assert path.read_text() == "one\ntwo\nthree\n"
    elif scope == "lines":
        assert path.read_text() == "one\nthree\n"
    else:
        assert not path.exists()
    assert not repository.has_conflicts()


@pytest.mark.parametrize("newer_target", [True, False])
def test_squash_into_named_target(hunk_repository: StGitRepository, newer_target: bool) -> None:
    repository = hunk_repository
    commands = repository.commands
    (repository.root / "text.txt").write_text("first\n")
    commands.run("stg", "refresh")
    commands.run("stg", "new", "-m", "Middle message", "middle")
    commands.run("stg", "new", "-m", "Second message", "second")
    (repository.root / "other.txt").write_text("second\n")
    commands.run("stg", "refresh")
    source, target = ("current", "second") if newer_target else ("second", "current")
    before = commands.run("git", "rev-parse", "HEAD^{tree}")
    template = repository.squash_message_template(source, target)
    assert "Second message" in template
    assert "current" in template.lower()
    assert len(repository.load_stack()) == 3
    repository.move_to_target(
        ChangeSelection(MoveTarget(patch_name=source), (), whole_patch=True),
        MoveTarget(patch_name=target),
        message="Combined message\n\nDetails\n",
    )
    patches = {patch.name: patch for patch in repository.load_stack()}
    assert source not in patches
    assert patches[target].description == "Combined message"
    assert {file.path for file in patches[target].files} == {"text.txt", "other.txt"}
    assert commands.run("git", "rev-parse", "HEAD^{tree}") == before
    assert (
        commands.run("git", "log", "-1", "--format=%B", repository.stg.patch_commit(target))
        == "Combined message\n\nDetails\n\n"
    )


@pytest.mark.parametrize("source_status", ["current", "unapplied", "hidden"])
@pytest.mark.parametrize("target", ["staged", "unstaged"])
def test_move_added_patch_file_to_working_area(
    hunk_repository: StGitRepository, source_status: str, target: ChangeArea
) -> None:
    repository = hunk_repository
    path = repository.root / "new.bin"
    content = b"\0one\xfftwo\0"
    path.write_bytes(content)
    repository.git.stage_files((path.name,))
    repository.commands.run("stg", "refresh", "--index")
    if source_status != "current":
        repository.commands.run("stg", "pop")
        if source_status == "hidden":
            repository.commands.run("stg", "hide", "current")
    repository.move_to_target(
        ChangeSelection(MoveTarget(patch_name="current"), (path.name,)),
        MoveTarget(change_area=target),
    )
    assert path.read_bytes() == content
    assert not repository.load_stack()[0].files
    assert (path.name in {file.path for file in repository.load_staged_changes()}) == (
        target == "staged"
    )
    assert (path.name in {file.path for file in repository.load_unstaged_changes()}) == (
        target == "unstaged"
    )


@pytest.mark.parametrize("target_applied", [True, False])
def test_move_untracked_binary_to_patch(
    hunk_repository: StGitRepository, target_applied: bool
) -> None:
    repository = hunk_repository
    if not target_applied:
        repository.commands.run("stg", "pop")
    path = repository.root / "new.bin"
    content = b"\0one\xfftwo\0"
    path.write_bytes(content)
    repository.move_to_target(
        ChangeSelection(MoveTarget(change_area="unstaged"), (path.name,)),
        MoveTarget(patch_name="current"),
    )
    assert repository.load_stack()[0].files[0].path == path.name
    assert path.exists() == target_applied
    if path.exists():
        assert path.read_bytes() == content
    assert not repository.has_local_changes()


@pytest.mark.parametrize("source_hidden", [False, True])
def test_squash_unapplied_patch_into_applied_target(
    hunk_repository: StGitRepository, source_hidden: bool
) -> None:
    repository = hunk_repository
    commands = repository.commands
    commands.run("stg", "new", "-m", "Source", "source")
    (repository.root / "text.txt").write_text("source\n")
    commands.run("stg", "refresh")
    commands.run("stg", "pop")
    if source_hidden:
        commands.run("stg", "hide", "source")
    assert "Source" in repository.squash_message_template("source", "current")
    repository.move_to_target(
        ChangeSelection(MoveTarget(patch_name="source"), (), whole_patch=True),
        MoveTarget(patch_name="current"),
        message="Combined\n",
    )
    (patch,) = repository.load_stack()
    assert patch.name == "current"
    assert "+source" in patch.diff_text


@pytest.mark.parametrize("source_area", ["staged", "unstaged"])
@pytest.mark.parametrize("scope", ["files", "hunk", "lines"])
def test_move_working_changes_to_other_area(
    hunk_repository: StGitRepository, source_area: ChangeArea, scope: str
) -> None:
    repository = hunk_repository
    path = repository.root / "text.txt"
    path.write_text(
        path.read_text().replace("line 10\n", "changed\n").replace("line 70\n", "remaining\n")
    )
    if source_area == "staged":
        repository.git.stage_files((path.name,))
    before = path.read_bytes()
    files = (
        repository.load_staged_changes()
        if source_area == "staged"
        else repository.load_unstaged_changes()
    )
    file = files[0]
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    selection = ChangeSelection(
        MoveTarget(change_area=source_area),
        (path.name,),
        hunk_diff=hunk.diff_text if scope == "hunk" else None,
        lines=HunkLineSelection(
            file.path, hunk.diff_text, rows.index("+changed"), rows.index("+changed")
        )
        if scope == "lines"
        else None,
    )
    repository.move_to_target(
        selection, MoveTarget(change_area="staged" if source_area == "unstaged" else "unstaged")
    )
    assert path.read_bytes() == before
    staged = repository.commands.run("git", "diff", "--cached", "--no-ext-diff")
    assert ("+changed" in staged) == (source_area == "unstaged")
    assert ("+remaining" in staged) == (
        source_area == "unstaged" if scope == "files" else source_area == "staged"
    )


def test_target_move_rolls_back_if_local_snapshot_cannot_be_restored(
    hunk_repository: StGitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    commands.run("stg", "new", "-m", "Later", "later")
    path.write_text(path.read_text().replace("line 10\n", "selected\n"))
    repository.git.stage_files((path.name,))
    path.write_text(path.read_text().replace("line 90\n", "unstaged\n"))
    (repository.root / "untracked").write_text("keep\n")
    before = path.read_bytes()
    index = repository.git.index_tree()
    head = commands.run("git", "rev-parse", "HEAD")
    series = repository.stg.series()

    def fail(*args: object) -> str:
        raise RepositoryError("snapshot failed")

    monkeypatch.setattr(repository.git, "create_stash", fail)
    with pytest.raises(RepositoryError, match="snapshot failed"):
        repository.move_to_target(
            ChangeSelection(MoveTarget(change_area="staged"), (path.name,)),
            MoveTarget(patch_name="current"),
        )
    assert path.read_bytes() == before
    assert repository.git.index_tree() == index
    assert commands.run("git", "rev-parse", "HEAD") == head
    assert repository.stg.series() == series
    assert (repository.root / "untracked").read_text() == "keep\n"
    assert not repository.stack_operation_pending


def test_target_move_conflict_can_be_undone(hunk_repository: StGitRepository) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "selected\n"))
    commands.run("stg", "refresh")
    commands.run("stg", "new", "-m", "Dependent", "dependent")
    path.write_text(path.read_text().replace("selected\n", "dependent\n"))
    commands.run("stg", "refresh")
    head = commands.run("git", "rev-parse", "HEAD")
    path.write_text(path.read_text().replace("line 40\n", "staged\n"))
    repository.git.stage_files((path.name,))
    index = repository.git.index_tree()
    path.write_text(path.read_text().replace("line 90\n", "unstaged\n"))
    before = path.read_bytes()
    with pytest.raises(RepositoryError):
        repository.move_to_target(
            ChangeSelection(MoveTarget(patch_name="current"), (path.name,)),
            MoveTarget(change_area="staged"),
        )
    assert repository.has_conflicts()
    assert repository.stack_operation_pending
    repository.undo_stack_operation()
    assert not repository.has_conflicts()
    assert commands.run("git", "rev-parse", "HEAD") == head
    assert repository.git.index_tree() == index
    assert path.read_bytes() == before


def test_target_move_rejects_stale_lines_without_changing_stack(
    hunk_repository: StGitRepository,
) -> None:
    repository = hunk_repository
    path = repository.root / "text.txt"
    path.write_text(path.read_text().replace("line 10\n", "selected\n"))
    file = repository.load_unstaged_changes()[0]
    hunk = file.hunks[0]
    row = hunk.diff_text.splitlines().index("+selected")
    selection = ChangeSelection(
        MoveTarget(change_area="unstaged"),
        (path.name,),
        lines=HunkLineSelection(file.path, hunk.diff_text, row, row),
    )
    path.write_text(path.read_text().replace("selected\n", "edited again\n"))
    before = path.read_bytes()
    index = repository.git.index_tree()
    with pytest.raises(RepositoryError):
        repository.move_to_target(selection, MoveTarget(patch_name="current"))
    assert path.read_bytes() == before
    assert repository.git.index_tree() == index
    assert not repository.load_stack()[0].files


@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
@pytest.mark.parametrize("source_status", ["applied", "unapplied", "hidden"])
@pytest.mark.parametrize("target_status", ["older", "newer", "unapplied", "hidden"])
def test_move_selected_lines_between_arbitrary_patches(
    hunk_repository: StGitRepository, part: str, source_status: str, target_status: str
) -> None:
    repository = hunk_repository
    commands = repository.commands
    path = repository.root / "text.txt"
    base = path.read_text()
    commands.run("stg", "new", "-m", "Middle", "middle")
    commands.run("stg", "new", "-m", "Source", "source")
    path.write_text(base.replace("line 10\n", "first\nsecond\n").replace("line 70\n", "keep\n"))
    commands.run("stg", "refresh")
    commands.run("stg", "new", "-m", "Middle above", "middle-above")
    commands.run("stg", "new", "-m", "Target", "target")
    if source_status != "applied":
        for name in ("target", "middle-above", "source"):
            commands.run("stg", "pop", name)
        if source_status == "hidden":
            commands.run("stg", "hide", "source")
    elif target_status in ("unapplied", "hidden"):
        commands.run("stg", "pop", "target")
    if target_status == "hidden":
        commands.run("stg", "hide", "target")
    target = "current" if target_status == "older" else "target"
    # For a newer applied destination with an unapplied source, apply the
    # independent target without applying the source.
    if target_status == "newer" and source_status != "applied":
        commands.run("stg", "push", "target")
    series = repository.stg.series()
    file = next(patch for patch in repository.load_stack() if patch.name == "source").files[0]
    hunk = file.hunks[0]
    rows = hunk.diff_text.splitlines()
    first = rows.index("+first" if part == "addition" else "-line 10")
    last = rows.index(
        "-line 10" if part == "deletion" else "+second" if part == "replacement" else "+first"
    )
    before = path.read_bytes()
    repository.move_to_target(
        ChangeSelection(
            MoveTarget(patch_name="source"),
            (path.name,),
            lines=HunkLineSelection(file.path, hunk.diff_text, first, last),
        ),
        MoveTarget(patch_name=target),
    )
    patches = {patch.name: patch for patch in repository.load_stack()}
    assert repository.stg.series() == series
    assert ("+first" in patches[target].diff_text) == (part != "deletion")
    assert ("-line 10" in patches[target].diff_text) == (part != "addition")
    assert ("+first" in patches["source"].diff_text) == (part == "deletion")
    assert ("-line 10" in patches["source"].diff_text) == (part == "addition")
    assert "+keep" in patches["source"].diff_text
    assert not patches["middle"].files
    assert not patches["middle-above"].files
    if source_status == "applied" and target_status in ("older", "newer"):
        assert path.read_bytes() == before
    assert not repository.has_local_changes()


@pytest.mark.parametrize("hidden", [True, False])
def test_squash_two_unapplied_patches_combines_all_changes(
    hunk_repository: StGitRepository, hidden: bool
) -> None:
    repository = hunk_repository
    commands = repository.commands
    (repository.root / "text.txt").write_text("first\n")
    commands.run("stg", "refresh")
    commands.run("stg", "new", "-m", "Second", "second")
    (repository.root / "other.txt").write_text("second\n")
    commands.run("stg", "refresh")
    commands.run("stg", "pop", "second")
    commands.run("stg", "pop", "current")
    if hidden:
        commands.run("stg", "hide", "current")
        commands.run("stg", "hide", "second")
    repository.move_to_target(
        ChangeSelection(MoveTarget(patch_name="current"), (), whole_patch=True),
        MoveTarget(patch_name="second"),
        message="Combined\n",
    )
    (patch,) = repository.load_stack()
    assert patch.name == "second"
    assert {file.path for file in patch.files} == {"text.txt", "other.txt"}
