from dataclasses import dataclass, field

from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from stg_layers.models import ChangeArea, FileChange, Patch, TreeItem
from stg_layers.tree_walker import find_selection_node, selected_file_expansion, walk_nodes


@dataclass
class _FileDirectory:
    files: list[FileChange] = field(default_factory=list)
    directories: dict[str, "_FileDirectory"] = field(default_factory=dict)


@dataclass
class TreeBuildState:
    selection: TreeNode[TreeItem] | None
    expansion_states: list[tuple[TreeNode[TreeItem], bool]]


class TreeBuilder:
    """Build layer nodes and a restoration plan without scheduling UI updates."""

    def __init__(self, tree: Tree[TreeItem]) -> None:
        self.tree = tree

    def build(
        self,
        patches: tuple[Patch, ...],
        staged: tuple[FileChange, ...],
        unstaged: tuple[FileChange, ...],
        *,
        newest_first: bool,
        preferred_selection: TreeItem | None = None,
        expansion_fallback_path: str | None = None,
    ) -> TreeBuildState:
        tree = self.tree
        old_nodes = walk_nodes(tree.root)
        old_cursor = tree.cursor_node
        previous_selection = old_cursor.data if old_cursor is not None else None
        selection = preferred_selection or previous_selection
        expanded_nodes = {
            node.data.selection_key: node.is_expanded for node in old_nodes if node.data is not None
        }
        fallback_expanded = selected_file_expansion(old_cursor, tree.root, expansion_fallback_path)

        tree.root.remove_children()
        ordered_patches = tuple(reversed(patches)) if newest_first else patches

        if newest_first:
            self.add_change_group(tree.root, "unstaged", unstaged)
            self.add_change_group(tree.root, "staged", staged)
            self.add_patch_stack(tree.root, ordered_patches)
        else:
            self.add_patch_stack(tree.root, ordered_patches)
            self.add_change_group(tree.root, "staged", staged)
            self.add_change_group(tree.root, "unstaged", unstaged)

        nodes = walk_nodes(tree.root)
        expansion_states = self._tree_expansion_states(
            nodes,
            expanded_nodes,
            selection,
            expansion_fallback_path,
            fallback_expanded,
        )
        selected_node = find_selection_node(nodes, selection)
        if selected_node is None and nodes:
            selected_node = nodes[0]
        should_restore_selection = preferred_selection is not None or previous_selection is not None
        return TreeBuildState(
            selection=selected_node if should_restore_selection else None,
            expansion_states=expansion_states,
        )

    @staticmethod
    def add_patch_stack(
        root: TreeNode[TreeItem],
        patches: tuple[Patch, ...],
    ) -> None:
        item, label = TreeItem.for_patch_stack()
        patch_stack = root.add(label, data=item, expand=True)
        if not patches:
            patch_stack.add("No patches in this stack.", allow_expand=False)
            return
        for patch in patches:
            TreeBuilder.add_patch(patch_stack, patch)

    @staticmethod
    def _matches_expansion_fallback(
        item: TreeItem,
        selection: TreeItem | None,
        file_path: str | None,
    ) -> bool:
        return (
            selection is not None
            and file_path is not None
            and item.is_file
            and item.file_path == file_path
            and item.same_file_as(selection)
        )

    @classmethod
    def _tree_expansion_states(
        cls,
        nodes: list[TreeNode[TreeItem]],
        expanded_nodes: dict[tuple[str | None, ...], bool],
        selection: TreeItem | None,
        fallback_path: str | None,
        fallback_expanded: bool | None,
    ) -> list[tuple[TreeNode[TreeItem], bool]]:
        expansion_states: list[tuple[TreeNode[TreeItem], bool]] = []
        for node in nodes:
            item = node.data
            if item is None or not node.allow_expand:
                continue
            key = item.selection_key
            if fallback_expanded is not None and cls._matches_expansion_fallback(
                item, selection, fallback_path
            ):
                expansion_states.append((node, fallback_expanded))
            elif key in expanded_nodes:
                expansion_states.append((node, expanded_nodes[key]))
        return expansion_states

    @staticmethod
    def add_change_group(
        parent: TreeNode[TreeItem],
        change_area: ChangeArea,
        files: tuple[FileChange, ...],
    ) -> None:
        item, label = TreeItem.for_change_group(change_area)
        group = parent.add(label, data=item, expand=True, allow_expand=True)
        if not files:
            group.add(f"No {change_area} changes.", allow_expand=False)
            return

        TreeBuilder._add_grouped_files(group, files, None, change_area)

    @staticmethod
    def add_patch(parent: TreeNode[TreeItem], patch: Patch) -> None:
        item, label = TreeItem.for_patch(patch)
        patch_node = parent.add(label, data=item, expand=True)
        if not patch.files:
            patch_node.add("(empty patch)", allow_expand=False)
            return

        TreeBuilder._add_grouped_files(patch_node, patch.files, patch.name, None)

    @staticmethod
    def _add_grouped_files(
        parent: TreeNode[TreeItem],
        files: tuple[FileChange, ...],
        patch_name: str | None,
        change_area: ChangeArea | None,
    ) -> None:
        root = _FileDirectory()
        for file in files:
            directory = root
            for component in file.path.split("/")[:-1]:
                directory = directory.directories.setdefault(component, _FileDirectory())
            directory.files.append(file)
        TreeBuilder._add_directory_contents(parent, root, "", patch_name, change_area)

    @staticmethod
    def _add_directory_contents(
        parent: TreeNode[TreeItem],
        directory: _FileDirectory,
        prefix: str,
        patch_name: str | None,
        change_area: ChangeArea | None,
    ) -> None:
        for file in directory.files:
            TreeBuilder._add_file(parent, file, patch_name, change_area)
        for name, child in sorted(directory.directories.items()):
            while not child.files and len(child.directories) == 1:
                component, child = next(iter(child.directories.items()))
                name = f"{name}/{component}"
            path = f"{prefix}/{name}" if prefix else name
            item, label = TreeItem.for_directory(
                path, display_name=name, patch_name=patch_name, change_area=change_area
            )
            node = parent.add(label, data=item, expand=True)
            TreeBuilder._add_directory_contents(node, child, path, patch_name, change_area)

    @staticmethod
    def _add_file(
        parent: TreeNode[TreeItem],
        file: FileChange,
        patch_name: str | None,
        change_area: ChangeArea | None,
    ) -> None:
        item, label = TreeItem.for_file(file, patch_name=patch_name, change_area=change_area)
        file_node = parent.add(label, data=item, expand=False, allow_expand=bool(file.hunks))
        for hunk in file.hunks:
            item, label = TreeItem.for_hunk(
                file, hunk, patch_name=patch_name, change_area=change_area
            )
            file_node.add(label, data=item, allow_expand=False)
