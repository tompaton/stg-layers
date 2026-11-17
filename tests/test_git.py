import pytest

from stg_layers.errors import DiffError
from stg_layers.git import parse_diff
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
