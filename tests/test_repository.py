import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stg_layers.commands import CommandRunner
from stg_layers.errors import CommandError, RepositoryError
from stg_layers.git import Git
from stg_layers.models import FileStatus, PatchStatus
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
