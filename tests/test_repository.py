import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stg_layers.models import FileStatus, PatchStatus
from stg_layers.repository import (
    CommandError,
    DiffError,
    RepositoryError,
    StGitRepository,
    _parse_diff,
    _parse_series,
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
    _run(root, environment, "git", "add", ".")
    _run(root, environment, "git", "commit", "-qm", "base")
    _run(root, environment, "stg", "init")
    _run(root, environment, "stg", "new", "-m", "First patch", "first")
    (root / "base.txt").write_text("base\nfirst layer\n", encoding="utf-8")
    _run(root, environment, "stg", "refresh", "-m", "First patch")
    _run(root, environment, "stg", "new", "-m", "Second patch", "second")
    (root / "other.txt").write_text("other\ncurrent layer\n", encoding="utf-8")
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


def test_parse_series_records_patch_state_and_description() -> None:
    output = "> current # current patch\n+ earlier # applied patch\n- next # queued patch\n! hidden"

    assert _parse_series(output) == [
        ("current", PatchStatus.CURRENT, "current patch"),
        ("earlier", PatchStatus.APPLIED, "applied patch"),
        ("next", PatchStatus.UNAPPLIED, "queued patch"),
        ("hidden", PatchStatus.HIDDEN, ""),
    ]


def test_parse_series_rejects_unknown_status() -> None:
    with pytest.raises(RepositoryError, match="Unrecognized output"):
        _parse_series("? patch")


def test_parse_diff_retains_file_hunks_and_line_ranges() -> None:
    output = """commit deadbeef

diff --git a/example.txt b/example.txt
index 1234567..7654321 100644
--- a/example.txt
+++ b/example.txt
@@ -2,2 +2,2 @@ context
 unchanged
-before
+after
"""

    (file,) = _parse_diff("example", output)

    assert file.path == "example.txt"
    assert file.status == FileStatus.MODIFIED
    assert len(file.hunks) == 1
    hunk = file.hunks[0]
    assert hunk.header == "@@ -2,2 +2,2 @@ context"
    assert hunk.source_start == 2
    assert hunk.target_start == 2
    assert "-before" in hunk.diff_text
    assert "+after" in hunk.diff_text


def test_parse_diff_keeps_multiple_hunks_distinct_within_one_file() -> None:
    output = """diff --git a/example.txt b/example.txt
--- a/example.txt
+++ b/example.txt
@@ -1,2 +1,2 @@
-first
+first changed
 context
@@ -10,2 +10,2 @@
-last
+last changed
 context
"""

    (file,) = _parse_diff("multiple-hunks", output)

    assert file.path == "example.txt"
    assert [hunk.index for hunk in file.hunks] == [0, 1]
    assert [hunk.header for hunk in file.hunks] == [
        "@@ -1,2 +1,2 @@",
        "@@ -10,2 +10,2 @@",
    ]


def test_parse_diff_preserves_rename_paths() -> None:
    output = """diff --git a/old.txt b/new.txt
similarity index 90%
rename from old.txt
rename to new.txt
--- a/old.txt
+++ b/new.txt
@@ -1 +1 @@
-before
+after
"""

    (file,) = _parse_diff("rename", output)

    assert file.status == FileStatus.RENAMED
    assert file.source_path == "old.txt"
    assert file.target_path == "new.txt"
    assert file.path == "new.txt"


def test_parse_diff_decodes_quoted_paths() -> None:
    output = """diff --git "a/file with spaces.txt" "b/file with spaces.txt"
index 1234567..7654321 100644
--- "a/file with spaces.txt"
+++ "b/file with spaces.txt"
@@ -1 +1 @@
-before
+after
"""

    (file,) = _parse_diff("quoted", output)

    assert file.path == "file with spaces.txt"
    assert file.source_path == "file with spaces.txt"
    assert file.target_path == "file with spaces.txt"


def test_parse_diff_decodes_git_octal_path_escapes() -> None:
    output = r"""diff --git "a/caf\303\251.txt" "b/caf\303\251.txt"
index 1234567..7654321 100644
--- "a/caf\303\251.txt"
+++ "b/caf\303\251.txt"
@@ -1 +1 @@
-before
+after
"""

    (file,) = _parse_diff("unicode", output)

    assert file.path == "café.txt"


def test_parse_diff_marks_binary_files_without_fake_text_diff() -> None:
    output = """diff --git a/image.bin b/image.bin
index 1234567..7654321 100644
GIT binary patch
literal 1
AcmZQ

literal 1
AcmZQ
"""

    (file,) = _parse_diff("binary", output)

    assert file.path == "image.bin"
    assert file.status == FileStatus.BINARY
    assert not file.hunks
    assert not file.diff_text


def test_parse_diff_accepts_empty_patches() -> None:
    assert _parse_diff("empty", "commit deadbeef\n") == ()


def test_parse_diff_reports_malformed_diff() -> None:
    malformed = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ not a hunk\n"
    with pytest.raises(DiffError, match="malformed"):
        _parse_diff("malformed", malformed)


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


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for integration tests")
def test_stage_and_unstage_file_preserves_worktree_changes(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_git_repository(tmp_path, environment)
    repository = StGitRepository(tmp_path)
    path = "file.txt"
    file_path = tmp_path / path
    file_path.write_text(
        file_path.read_text(encoding="utf-8") + "staged change\n",
        encoding="utf-8",
    )

    repository.stage_file(path)
    file_path.write_text(
        file_path.read_text(encoding="utf-8") + "unstaged change\n",
        encoding="utf-8",
    )
    repository.unstage_file(path)

    assert not _run(tmp_path, environment, "git", "diff", "--cached")
    worktree_diff = _run(tmp_path, environment, "git", "diff", "--", path)
    assert "+staged change" in worktree_diff
    assert "+unstaged change" in worktree_diff

    untracked_path = "new file.txt"
    (tmp_path / untracked_path).write_text("new content\n", encoding="utf-8")
    repository.stage_file(untracked_path)
    assert "+new content" in _run(tmp_path, environment, "git", "diff", "--cached")
    repository.unstage_file(untracked_path)
    status = _run(tmp_path, environment, "git", "status", "--short")
    assert any(line.startswith("?? ") for line in status.splitlines())


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for integration tests")
def test_stage_and_unstage_selected_hunk_preserves_sibling_hunks(tmp_path: Path) -> None:
    environment = _git_environment()
    _create_git_repository(tmp_path, environment)
    repository = StGitRepository(tmp_path)
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
    repository = StGitRepository(tmp_path)
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
