from pathlib import Path

import pytest

from stg_layers.commands import CommandRunner
from stg_layers.errors import DiffError, RepositoryError
from stg_layers.git import Git, parse_diff
from stg_layers.models import FileStatus


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

    (file,) = parse_diff("example", output)

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

    (file,) = parse_diff("multiple-hunks", output)

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

    (file,) = parse_diff("rename", output)

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

    (file,) = parse_diff("quoted", output)

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

    (file,) = parse_diff("unicode", output)

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

    (file,) = parse_diff("binary", output)

    assert file.path == "image.bin"
    assert file.status == FileStatus.BINARY
    assert not file.hunks
    assert not file.diff_text


def test_parse_diff_accepts_empty_patches() -> None:
    assert parse_diff("empty", "commit deadbeef\n") == ()


def test_parse_diff_reports_malformed_diff() -> None:
    malformed = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ not a hunk\n"
    with pytest.raises(DiffError, match="malformed"):
        parse_diff("malformed", malformed)


@pytest.fixture
def discard_git(tmp_path: Path) -> Git:
    git = Git(CommandRunner(tmp_path))
    git.commands.run("git", "init", "-q")
    git.commands.run("git", "config", "user.name", "Test")
    git.commands.run("git", "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("".join(f"line {i}\n" for i in range(100)))
    git.stage_files(("tracked.txt",))
    git.commands.run("git", "commit", "-qm", "base")
    return git


def test_discard_files_preserves_index_and_unselected_files(discard_git: Git) -> None:
    git = discard_git
    tracked = git.root / "tracked.txt"
    staged = tracked.read_text().replace("line 40\n", "staged\n")
    tracked.write_text(staged)
    git.stage_files(("tracked.txt",))
    index = git.staged_file_diff("tracked.txt")
    tracked.write_text(staged.replace("line 5\n", "unstaged\n"))
    added = git.root / "new [file].txt"
    added.write_text("remove me\n")
    outside = git.root / "outside.txt"
    outside.write_text("keep me\n")
    git.discard_files(("tracked.txt", added.name))
    assert tracked.read_text() == staged
    assert git.staged_file_diff("tracked.txt") == index
    assert not added.exists()
    assert outside.read_text() == "keep me\n"


def test_discard_hunk_preserves_other_hunks_and_index(discard_git: Git) -> None:
    git = discard_git
    tracked = git.root / "tracked.txt"
    staged = tracked.read_text().replace("line 40\n", "staged\n")
    tracked.write_text(staged)
    git.stage_files(("tracked.txt",))
    index = git.staged_file_diff("tracked.txt")
    tracked.write_text(staged.replace("line 5\n", "first\n").replace("line 80\n", "second\n"))
    (file,) = git.load_unstaged_changes()
    assert len(file.hunks) == 2
    git.discard_hunk(file.path, file.hunks[1].diff_text)
    assert tracked.read_text() == staged.replace("line 5\n", "first\n")
    assert git.staged_file_diff("tracked.txt") == index
    with pytest.raises(RepositoryError, match="no longer present"):
        git.discard_hunk(file.path, file.hunks[1].diff_text)


@pytest.mark.parametrize("addition", [False, True])
def test_discard_added_or_deleted_hunk(discard_git: Git, addition: bool) -> None:
    git = discard_git
    path = git.root / ("new.txt" if addition else "tracked.txt")
    if addition:
        path.write_text("new content\n")
    else:
        path.unlink()
    (file,) = git.load_unstaged_changes()
    git.discard_hunk(file.path, file.hunks[0].diff_text)
    assert path.exists() is not addition
    assert git.load_staged_changes() == ()
    assert git.load_unstaged_changes() == ()


def test_discard_hunk_keeps_unstaged_mode_change(discard_git: Git) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    original = path.read_text()
    git.commands.run("git", "config", "core.filemode", "true")
    path.chmod(0o755)
    path.write_text(original.replace("line 5\n", "changed\n"))
    (file,) = git.load_unstaged_changes()
    git.discard_hunk(file.path, file.hunks[0].diff_text)
    assert path.read_text() == original
    assert path.stat().st_mode & 0o111
    assert git.load_staged_changes() == ()


def test_discard_files_preserves_staged_addition(discard_git: Git) -> None:
    git = discard_git
    path = git.root / "added.txt"
    path.write_text("staged addition\n")
    git.stage_files((path.name,))
    index = git.staged_file_diff(path.name)
    path.write_text("unstaged edit\n")
    git.discard_files((path.name,))
    assert path.read_text() == "staged addition\n"
    assert git.staged_file_diff(path.name) == index


@pytest.mark.parametrize(
    ("remotes", "expected"),
    [
        ((), None),
        (("upstream",), "https://example.com/upstream.git"),
        (("aaa", "origin", "upstream"), "https://example.com/origin.git"),
        (("aaa", "upstream"), "https://example.com/aaa.git"),
    ],
)
def test_remote_url_prefers_origin(
    tmp_path: Path, remotes: tuple[str, ...], expected: str | None
) -> None:
    git = Git(CommandRunner(tmp_path))
    git.commands.run("git", "init", "-q")
    for remote in remotes:
        git.commands.run("git", "remote", "add", remote, f"https://example.com/{remote}.git")
    assert git.remote_url() == expected
