import pytest
from textual.widgets import Tree

from stg_layers.models import DiffHunk, FileChange, FileStatus, Patch, PatchStatus, TreeItem
from stg_layers.tree_builder import TreeBuilder


@pytest.mark.parametrize("newest_first", [False, True])
def test_builder_creates_nodes_and_restoration_plan_without_a_running_app(
    newest_first: bool,
) -> None:
    tree: Tree[TreeItem] = Tree("Repository")
    builder = TreeBuilder(tree)
    file = FileChange(
        path="src/layers/file.py",
        source_path="src/layers/file.py",
        target_path="src/layers/file.py",
        status=FileStatus.MODIFIED,
        diff_text="file diff",
        hunks=(),
    )
    patch = Patch("current", PatchStatus.CURRENT, "Patch description", (file,))
    state = builder.build((patch,), (file,), (), newest_first=newest_first)
    labels = [str(node.label) for node in tree.root.children]
    assert labels == (
        ["Unstaged changes", "Staged changes", "Patch stack"]
        if newest_first
        else ["Patch stack", "Staged changes", "Unstaged changes"]
    )
    assert state.selection is None
    staged = next(
        node for node in tree.root.children if node.data and node.data.change_area == "staged"
    )
    directory = staged.children[0]
    assert str(directory.label) == "src/layers"
    directory.collapse()
    previous_file = directory.children[0]
    original_item = previous_file.data
    assert original_item is not None and original_item.file_path == file.path
    preferred = original_item.as_selection()

    state = builder.build(
        (patch,), (file,), (), newest_first=not newest_first, preferred_selection=preferred
    )

    assert state.selection is not None and state.selection.data == original_item
    assert state.selection is not previous_file
    assert any(
        node.data is not None
        and node.data.directory_path == "src/layers"
        and node.data.change_area == "staged"
        and not expanded
        for node, expanded in state.expansion_states
    )


def test_missing_hunk_falls_back_to_the_file_in_the_same_context() -> None:
    tree: Tree[TreeItem] = Tree("Repository")
    file = FileChange("file", "file", "file", FileStatus.MODIFIED, "", ())
    removed_hunk = DiffHunk(0, "header", "diff", 1, 1, 1, 1)
    item, _ = TreeItem.for_hunk(file, removed_hunk, change_area="staged")
    patch = Patch("patch", PatchStatus.CURRENT, "", (file,))

    state = TreeBuilder(tree).build(
        (patch,), (file,), (file,), newest_first=True, preferred_selection=item.as_selection()
    )

    assert state.selection is not None and state.selection.data is not None
    assert state.selection.data.is_file and state.selection.data.is_staged
    assert state.selection.data.same_file_as(item)
