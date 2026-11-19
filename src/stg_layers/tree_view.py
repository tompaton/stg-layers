from textual.binding import Binding
from textual.widgets import TextArea, Tree
from textual.widgets._tree import TreeNode

from stg_layers.models import FileChange, Patch, TreeItem
from stg_layers.tree_builder import TreeBuilder


class LayerTree(Tree[TreeItem]):
    BINDINGS = [
        Binding(
            "enter",
            "apply_selected_change",
            "Stage/unstage",
            show=False,
            priority=True,
        ),
        Binding("n", "create_patch", "New patch"),
        Binding("c", "patch_operation('commit')", "Commit patch"),
        Binding("C,shift+c", "patch_operation('uncommit')", "Uncommit", key_display="Shift+C"),
        Binding("h", "patch_operation('hide')", "Hide patch"),
        Binding("H,shift+h", "patch_operation('unhide')", "Unhide patch", key_display="Shift+H"),
        Binding("delete", "patch_operation('delete')", "Delete patch"),
        Binding("e", "edit_patch", "Edit patch"),
        Binding("p", "patch_operation('push')", "Push patch"),
        Binding("P,shift+p", "patch_operation('pop')", "Pop patch", key_display="Shift+P"),
        Binding(
            "ctrl+down",
            "move_change_down",
            "Move change down",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+up",
            "move_change_up",
            "Move change up",
            show=False,
            priority=True,
        ),
        Binding("left", "collapse_or_parent", show=False),
        Binding("right", "expand_or_child", show=False),
    ]

    async def action_move_change_down(self) -> None:
        await self.app.run_action("move_change_down")

    async def action_move_change_up(self) -> None:
        await self.app.run_action("move_change_up")

    async def action_edit_patch(self) -> None:
        await self.app.run_action("edit_selected_patch")

    async def action_create_patch(self) -> None:
        await self.app.run_action("create_patch")

    async def action_patch_operation(self, operation: str) -> None:
        await self.app.run_action(f"patch_operation('{operation}')")

    async def action_apply_selected_change(self) -> None:
        await self.app.run_action("apply_selected_change")

    def action_collapse_or_parent(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent != self.root:
            self.move_cursor(node.parent, animate=False)

    def action_expand_or_child(self) -> None:
        node = self.cursor_node
        if node is not None and node.data is not None and node.data.is_hunk:
            self.app.query_one("#diff-view", TextArea).focus()
            return
        if node is None or not node.allow_expand:
            return
        if not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0], animate=False)

    def populate(
        self,
        patches: tuple[Patch, ...],
        staged: tuple[FileChange, ...],
        unstaged: tuple[FileChange, ...],
        *,
        newest_first: bool,
        preferred_selection: TreeItem | None = None,
        expansion_fallback_path: str | None = None,
    ) -> None:
        state = TreeBuilder(self).build(
            patches,
            staged,
            unstaged,
            newest_first=newest_first,
            preferred_selection=preferred_selection,
            expansion_fallback_path=expansion_fallback_path,
        )
        if state.selection is not None:
            self.call_after_refresh(
                self._restore_tree_state, state.selection, state.expansion_states
            )

    def _restore_tree_state(
        self,
        node: TreeNode[TreeItem],
        expansion_states: list[tuple[TreeNode[TreeItem], bool]],
    ) -> None:
        for expanded_node, is_expanded in expansion_states:
            if is_expanded:
                expanded_node.expand()
            else:
                expanded_node.collapse()
        self.call_after_refresh(self.restore_selection, node)

    def restore_selection(self, node: TreeNode[TreeItem]) -> None:
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.data is not None and ancestor.data.is_directory:
                ancestor.expand()
            ancestor = ancestor.parent
        self.move_cursor(node, animate=False)
        self.post_message(self.NodeHighlighted(node))
