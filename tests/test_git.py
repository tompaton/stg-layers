from pathlib import Path

import pytest

from stg_layers.commands import CommandRunner
from stg_layers.errors import DiffError, RepositoryError
from stg_layers.git import Git, parse_diff
from stg_layers.models import FileChange, FileStatus
from stg_layers.partial_diff import HunkLineSelection


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


def _select_lines(file: FileChange, first: str, last: str | None = None) -> HunkLineSelection:
    hunk = next(hunk for hunk in file.hunks if first in hunk.diff_text.splitlines())
    lines = hunk.diff_text.splitlines()
    return HunkLineSelection(
        file.path, hunk.diff_text, lines.index(first), lines.index(last or first)
    )


@pytest.mark.parametrize("stage", [False, True])
@pytest.mark.parametrize("target", [10, 70])
@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
def test_move_selected_lines_preserves_other_changes(
    discard_git: Git, stage: bool, target: int, part: str
) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    base = path.read_text()
    full = base.replace("line 10\n", "first 10\nsecond 10\n").replace(
        "line 70\n", "first 70\nsecond 70\n"
    )
    path.write_text(full)
    if not stage:
        git.stage_files((path.name,))
    (git.root / "unrelated.txt").write_text("keep staged\n")
    git.stage_files(("unrelated.txt",))
    worktree = full.replace("line 50\n", "keep unstaged\n")
    path.write_text(worktree)
    files = git.load_unstaged_changes() if stage else git.load_staged_changes()
    file = next(file for file in files if file.path == path.name)
    first = f"+first {target}" if part == "addition" else f"-line {target}"
    last = f"+first {target}" if part == "replacement" else first
    git.move_selected_lines(_select_lines(file, first, last), stage=stage)
    old = f"line {target}\n"
    new = f"first {target}\n"
    staged_versions = {"addition": old + new, "deletion": "", "replacement": new}
    unstaged_versions = {"addition": "", "deletion": old + new, "replacement": old}
    expected = (
        base.replace(old, staged_versions[part])
        if stage
        else full.replace(new, unstaged_versions[part])
    )
    assert git.commands.run("git", "show", f":{path.name}") == expected
    assert git.commands.run("git", "show", ":unrelated.txt") == "keep staged\n"
    assert path.read_text() == worktree


@pytest.mark.parametrize("stage", [False, True])
def test_move_selected_lines_in_added_file(discard_git: Git, stage: bool) -> None:
    git = discard_git
    path = git.root / "new ü [file].txt"
    text = "one\ntwo\nthree\n"
    path.write_text(text)
    if not stage:
        git.stage_files((path.name,))
    files = git.load_unstaged_changes() if stage else git.load_staged_changes()
    file = next(file for file in files if file.path == path.name)
    git.move_selected_lines(_select_lines(file, "+two"), stage=stage)
    assert git.commands.run("git", "show", f":{path.name}") == (
        "two\n" if stage else "one\nthree\n"
    )
    assert path.read_text() == text


@pytest.mark.parametrize("stage", [False, True])
def test_move_selected_lines_in_deleted_file(discard_git: Git, stage: bool) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    base = path.read_text()
    path.unlink()
    if not stage:
        git.stage_files((path.name,))
    files = git.load_unstaged_changes() if stage else git.load_staged_changes()
    (file,) = files
    git.move_selected_lines(_select_lines(file, "-line 10"), stage=stage)
    assert git.commands.run("git", "show", f":{path.name}") == (
        base.replace("line 10\n", "") if stage else "line 10\n"
    )
    assert not path.exists()


@pytest.mark.parametrize("stage", [False, True])
def test_move_selected_lines_without_final_newline(discard_git: Git, stage: bool) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    path.write_text("before")
    git.stage_files((path.name,))
    git.commands.run("git", "commit", "-qm", "no final newline")
    path.write_text("after")
    if not stage:
        git.stage_files((path.name,))
    (file,) = git.load_unstaged_changes() if stage else git.load_staged_changes()
    git.move_selected_lines(_select_lines(file, "-before", "+after"), stage=stage)
    assert git.commands.run("git", "show", f":{path.name}") == ("after" if stage else "before")
    assert path.read_text() == "after"


def test_selected_lines_reject_stale_or_unchanged_selections(discard_git: Git) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    base = path.read_text()
    path.write_text(base.replace("line 10\n", "changed\n"))
    (file,) = git.load_unstaged_changes()
    hunk = file.hunks[0]
    index = git.commands.run("git", "write-tree")
    for row in (0, 1):
        selection = HunkLineSelection(file.path, hunk.diff_text, row, row)
        with pytest.raises(RepositoryError, match="Select added or removed"):
            git.move_selected_lines(selection, stage=True)
    selection = _select_lines(file, "+changed")
    path.write_text(base.replace("line 10\n", "newer change\n"))
    with pytest.raises(RepositoryError, match="no longer present"):
        git.move_selected_lines(selection, stage=True)
    assert git.commands.run("git", "write-tree") == index


@pytest.mark.parametrize("stage", [False, True])
@pytest.mark.parametrize("addition", [False, True])
def test_move_single_line_at_missing_final_newline(
    discard_git: Git, stage: bool, addition: bool
) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    path.write_text("before")
    git.stage_files((path.name,))
    git.commands.run("git", "commit", "-qm", "no final newline")
    path.write_text("after")
    if not stage:
        git.stage_files((path.name,))
    (file,) = git.load_unstaged_changes() if stage else git.load_staged_changes()
    git.move_selected_lines(_select_lines(file, "+after" if addition else "-before"), stage=stage)
    expected = "before\nafter" if stage == addition else ""
    assert git.commands.run("git", "show", f":{path.name}") == expected
    assert path.read_text() == "after"


@pytest.mark.parametrize("stage", [False, True])
@pytest.mark.parametrize("addition", [False, True])
def test_move_all_lines_adds_or_removes_index_entry(
    discard_git: Git, stage: bool, addition: bool
) -> None:
    git = discard_git
    path = git.root / ("new.txt" if addition else "tracked.txt")
    if addition:
        path.write_text("one\ntwo\n")
    else:
        path.unlink()
    if not stage:
        git.stage_files((path.name,))
    (file,) = git.load_unstaged_changes() if stage else git.load_staged_changes()
    hunk = file.hunks[0]
    selection = HunkLineSelection(
        file.path, hunk.diff_text, 1, len(hunk.diff_text.splitlines()) - 1
    )
    git.move_selected_lines(selection, stage=stage)
    assert git.is_tracked(path.name) == (stage == addition)
    assert path.exists() == addition


@pytest.mark.parametrize("stage", [False, True])
def test_selected_lines_preserve_staged_rename(discard_git: Git, stage: bool) -> None:
    git = discard_git
    old = git.root / "tracked.txt"
    path = git.root / "renamed file.txt"
    base = old.read_text()
    old.rename(path)
    git.stage_files((old.name, path.name))
    changed = base.replace("line 10\n", "changed\n")
    path.write_text(changed)
    if not stage:
        git.stage_files((path.name,))
    (file,) = git.load_unstaged_changes() if stage else git.load_staged_changes()
    git.move_selected_lines(_select_lines(file, "-line 10", "+changed"), stage=stage)
    assert git.commands.run("git", "show", f":{path.name}") == (changed if stage else base)
    assert not git.is_tracked(old.name)
    assert path.read_text() == changed


@pytest.mark.parametrize("stage", [False, True])
def test_selected_lines_leave_file_mode_unchanged_in_index(discard_git: Git, stage: bool) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    git.commands.run("git", "config", "core.filemode", "true")
    path.chmod(0o755)
    path.write_text(path.read_text().replace("line 10\n", "changed\n"))
    if not stage:
        git.stage_files((path.name,))
    mode = git.commands.run("git", "ls-files", "--stage").split()[0]
    (file,) = git.load_unstaged_changes() if stage else git.load_staged_changes()
    git.move_selected_lines(_select_lines(file, "-line 10", "+changed"), stage=stage)
    assert git.commands.run("git", "ls-files", "--stage").split()[0] == mode
    assert path.stat().st_mode & 0o111


@pytest.mark.parametrize("target", [10, 70])
@pytest.mark.parametrize("part", ["addition", "deletion", "replacement"])
def test_discard_selected_lines_preserves_index_and_other_worktree_changes(
    discard_git: Git, target: int, part: str
) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    staged = path.read_text().replace("line 30\n", "staged\n")
    path.write_text(staged)
    git.stage_files((path.name,))
    index = git.commands.run("git", "write-tree")
    worktree = staged.replace("line 10\n", "first 10\nsecond 10\n").replace(
        "line 70\n", "first 70\nsecond 70\n"
    )
    path.write_text(worktree)
    path.chmod(0o755)
    (file,) = git.load_unstaged_changes()
    first = f"+first {target}" if part == "addition" else f"-line {target}"
    last = f"+first {target}" if part == "replacement" else first
    git.discard_selected_lines(_select_lines(file, first, last))
    new = f"first {target}\n"
    old = f"line {target}\n"
    replacement = {"addition": "", "deletion": old + new, "replacement": old}[part]
    assert path.read_text() == worktree.replace(new, replacement)
    assert git.commands.run("git", "write-tree") == index
    assert path.stat().st_mode & 0o111


@pytest.mark.parametrize("all_lines", [False, True])
@pytest.mark.parametrize("addition", [False, True])
def test_discard_lines_in_added_or_deleted_file(
    discard_git: Git, all_lines: bool, addition: bool
) -> None:
    git = discard_git
    path = git.root / ("new.txt" if addition else "tracked.txt")
    original = "one\ntwo\nthree\n" if addition else path.read_text()
    if addition:
        path.write_text(original)
    else:
        path.unlink()
    index = git.commands.run("git", "write-tree")
    (file,) = git.load_unstaged_changes()
    hunk = file.hunks[0]
    selection = (
        HunkLineSelection(file.path, hunk.diff_text, 1, len(hunk.diff_text.splitlines()) - 1)
        if all_lines
        else _select_lines(file, "+two" if addition else "-line 10")
    )
    git.discard_selected_lines(selection)
    if addition and all_lines:
        assert not path.exists()
    else:
        expected = (
            original if all_lines else original.replace("two\n", "") if addition else "line 10\n"
        )
        assert path.read_text() == expected
    assert git.commands.run("git", "write-tree") == index


def test_discard_lines_rejects_stale_selection(discard_git: Git) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    base = path.read_text()
    path.write_text(base.replace("line 10\n", "changed\n"))
    (file,) = git.load_unstaged_changes()
    selection = _select_lines(file, "+changed")
    newer = base.replace("line 10\n", "newer\n")
    path.write_text(newer)
    index = git.commands.run("git", "write-tree")
    with pytest.raises(RepositoryError, match="no longer present"):
        git.discard_selected_lines(selection)
    assert path.read_text() == newer
    assert git.commands.run("git", "write-tree") == index


@pytest.mark.parametrize("addition", [False, True])
def test_discard_selected_line_without_final_newline(discard_git: Git, addition: bool) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    path.write_text("before")
    git.stage_files((path.name,))
    git.commands.run("git", "commit", "-qm", "no final newline")
    path.write_text("after")
    (file,) = git.load_unstaged_changes()
    index = git.commands.run("git", "write-tree")
    git.discard_selected_lines(_select_lines(file, "+after" if addition else "-before"))
    assert path.read_text() == ("" if addition else "before\nafter")
    assert git.commands.run("git", "write-tree") == index


def test_unstaged_diffstat_matches_git_and_preserves_index(discard_git: Git) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    staged = path.read_text().replace("line 40\n", "staged\n")
    path.write_text(staged)
    git.stage_files((path.name,))
    path.write_text(staged.replace("line 10\n", "unstaged\n"))
    git.commands.run("git", "config", "color.ui", "always")
    index = git.commands.run("git", "write-tree")
    expected = git.commands.run("git", "diff", "--stat", "--no-color", "--", path.name).rstrip()
    assert git.unstaged_diffstat((path.name,)).split() == expected.split()
    assert git.commands.run("git", "write-tree") == index
    assert path.read_text() == staged.replace("line 10\n", "unstaged\n")


def test_unstaged_diffstat_includes_only_selected_untracked_and_binary_files(
    discard_git: Git,
) -> None:
    git = discard_git
    (git.root / "new [file].txt").write_text("one\ntwo\n")
    (git.root / "binary.dat").write_bytes(b"\0binary\xff")
    (git.root / "outside.txt").write_text("not selected\n")
    git.commands.run("git", "config", "color.ui", "always")
    index = git.commands.run("git", "write-tree")
    stat = git.unstaged_diffstat(("new [file].txt", "binary.dat"))
    assert "new [file].txt" in stat and "binary.dat" in stat
    assert "2 files changed" in stat and "2 insertions(+)" in stat
    assert "Bin" in stat and "outside.txt" not in stat
    assert git.commands.run("git", "write-tree") == index
    assert (git.root / "binary.dat").read_bytes() == b"\0binary\xff"


def test_hunk_and_line_diffstats_only_include_selected_changes(discard_git: Git) -> None:
    git = discard_git
    path = git.root / "tracked.txt"
    path.write_text(
        path.read_text().replace("line 10\n", "first\n").replace("line 70\n", "second\n")
    )
    (file,) = git.load_unstaged_changes()
    hunk_stat = git.unstaged_diffstat((path.name,), hunk_diff=file.hunks[0].diff_text)
    assert "1 insertion(+)" in hunk_stat and "1 deletion(-)" in hunk_stat
    line_stat = git.discarded_lines_diffstat(_select_lines(file, "+first"))
    assert "1 insertion(+)" in line_stat and "deletion" not in line_stat
    assert "2 insertions" in git.unstaged_diffstat((path.name,))
