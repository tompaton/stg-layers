from dataclasses import replace

import pytest

from stg_layers.models import (
    ChangeArea,
    DiffHunk,
    FileChange,
    FileStatus,
    Patch,
    PatchStatus,
    TreeItem,
)


def test_tree_item_factories_keep_rename_labels_and_hunk_identity() -> None:
    hunk = DiffHunk(0, "@@ -1 +1 @@", "@@ -1 +1 @@\n-old\n+new\n", 1, 1, 1, 1)
    file = FileChange(
        "new [file].py", "old/file.py", "new [file].py", FileStatus.RENAMED, "file diff", (hunk,)
    )
    file_item, file_label = TreeItem.for_file(file, change_area="staged")
    hunk_item, hunk_label = TreeItem.for_hunk(file, hunk, change_area="staged")

    assert str(file_label) == "old/file.py -> new [file].py [renamed] +1 -1"
    assert file_item.title == "Staged changes / old/file.py -> new [file].py"
    assert file_item.file_path == file.path
    assert file_item.hunk_index is None
    assert str(hunk_label) == hunk.header
    assert str(hunk_label.style) == "yellow"
    assert hunk_item.hunk_key == "-old\n+new"
    assert (
        hunk_item.hunk_key
        == TreeItem.for_hunk(
            file, replace(hunk, index=2, header="@@ -20 +20 @@"), change_area="staged"
        )[0].hunk_key
    )

    patch_item, patch_label = TreeItem.for_patch(
        Patch("patch [name]", PatchStatus.CURRENT, "Description", (file,))
    )
    assert str(patch_label) == "[>patch [name]] Description"
    assert str(patch_label.spans[0].style) == "bold green"
    assert patch_item.file_path is None
    assert patch_item.change_area is None


@pytest.mark.parametrize("patch_name,change_area", [(None, None), ("patch", "staged")])
def test_scoped_tree_item_factories_reject_missing_or_ambiguous_context(
    patch_name: str | None, change_area: ChangeArea | None
) -> None:
    file = FileChange("file", "file", "file", FileStatus.MODIFIED, "", ())
    hunk = DiffHunk(0, "header", "diff", 1, 1, 1, 1)
    for factory in (
        lambda: TreeItem.for_file(file, patch_name=patch_name, change_area=change_area),
        lambda: TreeItem.for_hunk(file, hunk, patch_name=patch_name, change_area=change_area),
        lambda: TreeItem.for_directory("src/pkg", patch_name=patch_name, change_area=change_area),
    ):
        with pytest.raises(ValueError, match="exactly one patch or change area"):
            factory()


def test_selection_preserves_hunk_identity_and_replaces_context() -> None:
    file = FileChange("src/file.py", "src/file.py", "src/file.py", FileStatus.MODIFIED, "diff", ())
    hunk = DiffHunk(2, "header", "@@ -1 +1 @@\n-old\n+new\n", 1, 1, 1, 1)
    item, _ = TreeItem.for_hunk(file, hunk, change_area="unstaged")

    selection = item.as_selection(patch_name="destination")

    assert selection.title == selection.diff_text == ""
    assert selection.patch_name == "destination"
    assert selection.change_area is None
    assert selection.file_path == file.path
    assert selection.hunk_index == 2
    assert selection.hunk_key == "-old\n+new"
    assert item.change_area == "unstaged" and item.diff_text == hunk.diff_text

    staged = selection.as_selection(change_area="staged")
    assert staged.patch_name is None and staged.change_area == "staged"
    assert staged.as_selection() == staged
    assert staged.as_group_selection() == TreeItem.for_change_group("staged")[0].as_selection()


def test_selection_preserves_directory_identity_but_group_selection_drops_it() -> None:
    item, _ = TreeItem.for_directory("src/pkg", display_name="pkg", patch_name="source")
    selection = item.as_selection(patch_name="destination")
    assert selection.directory_path == "src/pkg"
    assert selection.as_group_selection() == TreeItem.for_patch_selection("destination")
    stack, _ = TreeItem.for_patch_stack()
    assert stack.as_selection().title == "Patch stack"


def test_selection_rejects_conflicting_destination_contexts() -> None:
    item = TreeItem.for_patch_selection("source")
    with pytest.raises(ValueError, match="not both"):
        item.as_selection(patch_name="destination", change_area="staged")


def test_item_kinds_distinguish_hunks_directories_and_groups() -> None:
    file = FileChange("src/file", "src/file", "src/file", FileStatus.MODIFIED, "", ())
    hunk = DiffHunk(0, "header", "diff", 1, 1, 1, 1)
    item, _ = TreeItem.for_hunk(file, hunk, patch_name="patch")
    assert item.is_hunk and item.is_file_or_hunk and item.is_patch_change
    assert not item.is_file and not item.is_patch
    assert item.require_file_path() == "src/file"
    assert item.require_patch_name() == "patch"

    directory, _ = TreeItem.for_directory("src", patch_name="patch")
    assert directory.is_directory and directory.is_file_group
    assert not directory.is_patch and not directory.is_file_or_hunk

    group, _ = TreeItem.for_change_group("staged")
    assert group.is_change_group and group.is_file_group and group.is_staged
    assert not group.is_working_change and not group.is_unstaged
    with pytest.raises(ValueError, match="does not refer to a file"):
        group.require_file_path()
    with pytest.raises(ValueError, match="does not belong to a patch"):
        group.require_patch_name()


def test_matching_keeps_contexts_separate_and_hunks_stable_after_reindexing() -> None:
    file = FileChange("file", "file", "file", FileStatus.MODIFIED, "", ())
    hunk = DiffHunk(0, "@@ -1 +1 @@", "@@ -1 +1 @@\n-old\n+new\n", 1, 1, 1, 1)
    item, _ = TreeItem.for_hunk(file, hunk, change_area="staged")
    moved_hunk = replace(hunk, index=2, diff_text="@@ -20 +20 @@\n-old\n+new\n")
    reindexed, _ = TreeItem.for_hunk(file, moved_hunk, change_area="staged")
    file_item, _ = TreeItem.for_file(file, change_area="staged")
    assert item.matches_selection(reindexed.as_selection())
    assert file_item.same_file_as(item)
    assert not file_item.matches_selection(item)
    assert item.is_working_change and not item.is_patch_change

    for other in (
        item.as_selection(change_area="unstaged"),
        item.as_selection(patch_name="patch"),
    ):
        assert not item.same_context_as(other)
        assert not item.same_file_as(other)
        assert not item.matches_selection(other)


@pytest.mark.parametrize(
    ("status", "hunks", "counts"),
    [
        (FileStatus.ADDED, ("@@ -0,0 +1,2 @@\n+one\n+two\n",), (2, 0)),
        (FileStatus.DELETED, ("@@ -1,2 +0,0 @@\n-one\n-two\n",), (0, 2)),
        (
            FileStatus.MODIFIED,
            (
                "@@ -1,2 +1,3 @@\n context\n-before\n+after\n+extra\n",
                "@@ -20 +21 @@\n---literal minus\n+++literal plus\n\\ No newline at end of file\n",
            ),
            (3, 2),
        ),
        (FileStatus.RENAMED, (), (0, 0)),
        (FileStatus.BINARY, (), (0, 0)),
    ],
)
def test_file_labels_show_changed_line_counts(
    status: FileStatus, hunks: tuple[str, ...], counts: tuple[int, int]
) -> None:
    file = FileChange(
        "dir/file.txt",
        "dir/file.txt",
        "dir/file.txt",
        status,
        "--- file header\n+++ file header\n",
        tuple(DiffHunk(i, text.splitlines()[0], text, 0, 0, 0, 0) for i, text in enumerate(hunks)),
    )
    assert (file.insertions, file.deletions) == counts
    _, label = TreeItem.for_file(file, change_area="unstaged")
    assert str(label).endswith(f"[{status.value}] +{counts[0]} -{counts[1]}")
    assert str(label.spans[-2].style) == "#98c379"
    assert str(label.spans[-1].style) == "#e06c75"
