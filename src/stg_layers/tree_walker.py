"""Traversal and selection queries over existing layer tree nodes."""

from collections.abc import Sequence

from textual.widgets._tree import TreeNode

from stg_layers.models import TreeItem


def _next_sibling_file(
    node: TreeNode[TreeItem],
    siblings: Sequence[TreeNode[TreeItem]],
) -> TreeNode[TreeItem] | None:
    index = siblings.index(node)
    return next(
        (
            sibling
            for sibling in siblings[index + 1 :]
            if sibling.data is not None and sibling.data.is_file
        ),
        None,
    )


def _previous_sibling_file(
    node: TreeNode[TreeItem],
    siblings: Sequence[TreeNode[TreeItem]],
) -> TreeNode[TreeItem] | None:
    index = siblings.index(node)
    return next(
        (
            sibling
            for sibling in reversed(siblings[:index])
            if sibling.data is not None and sibling.data.is_file
        ),
        None,
    )


def _next_sibling_item(
    node: TreeNode[TreeItem],
    siblings: Sequence[TreeNode[TreeItem]],
    change_area: str | None,
) -> TreeItem | None:
    index = siblings.index(node)
    for sibling in siblings[index + 1 :]:
        if sibling.data is not None and sibling.data.change_area == change_area:
            return sibling.data
    return None


def next_change_selection(
    node: TreeNode[TreeItem],
    item: TreeItem,
) -> TreeItem:
    if item.is_hunk:
        file_node = node.parent
        if file_node is not None:
            next_hunk = _next_sibling_item(
                node,
                tuple(file_node.children),
                item.change_area,
            )
            if next_hunk is not None:
                return next_hunk.as_selection()
            group_node = file_node.parent
        else:
            group_node = None
    else:
        file_node = node
        group_node = node.parent

    if file_node is not None and group_node is not None:
        siblings = change_group_files(group_node)
        next_file = _next_sibling_file(file_node, siblings)
        if next_file is not None and next_file.data is not None:
            return next_file.data.as_selection()
        previous_file = _previous_sibling_file(file_node, siblings)
        if previous_file is not None and previous_file.data is not None:
            return previous_file.data.as_selection()

    return item.as_group_selection()


def change_group_files(node: TreeNode[TreeItem]) -> tuple[TreeNode[TreeItem], ...]:
    while node.data is not None and node.data.is_directory:
        if node.parent is None:
            break
        node = node.parent
    return tuple(
        child for child in walk_nodes(node) if child.data is not None and child.data.is_file
    )


def walk_nodes(root: TreeNode[TreeItem]) -> list[TreeNode[TreeItem]]:
    nodes: list[TreeNode[TreeItem]] = []
    pending = list(reversed(root.children))
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(reversed(node.children))
    return nodes


def selected_file_expansion(
    old_cursor: TreeNode[TreeItem] | None,
    root: TreeNode[TreeItem],
    file_path: str | None,
) -> bool | None:
    selected_node = old_cursor
    while file_path is not None and selected_node is not None and selected_node != root:
        item = selected_node.data
        if item is not None and item.is_file and item.file_path == file_path:
            return selected_node.is_expanded
        selected_node = selected_node.parent
    return None


def find_selection_node(
    nodes: list[TreeNode[TreeItem]],
    selection: TreeItem | None,
) -> TreeNode[TreeItem] | None:
    if selection is None:
        return None
    selected_node = next(
        (
            node
            for node in nodes
            if node.data is not None and node.data.matches_selection(selection)
        ),
        None,
    )
    if selected_node is None and selection.is_hunk:
        return _find_file_node(nodes, selection)
    return selected_node


def _find_file_node(
    nodes: list[TreeNode[TreeItem]],
    selection: TreeItem,
) -> TreeNode[TreeItem] | None:
    return next(
        (
            node
            for node in nodes
            if node.data is not None and node.data.is_file and node.data.same_file_as(selection)
        ),
        None,
    )
