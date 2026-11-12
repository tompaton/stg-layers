import asyncio
from typing import Protocol

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Static, TextArea, Tree
from textual.widgets._tree import TreeNode
from textual.widgets.text_area import TextAreaTheme
from tree_sitter import Language
from tree_sitter_diff import HIGHLIGHTS_QUERY
from tree_sitter_diff import language as diff_language

from stg_layers.models import FileChange, FileStatus, Patch, PatchStatus, TreeItem
from stg_layers.repository import RepositoryError

_DIFF_THEME = TextAreaTheme(
    name="stg-diff",
    syntax_styles={
        "diff.plus": Style(color="#98c379"),
        "diff.minus": Style(color="#e06c75"),
        "diff.delta": Style(color="#e5c07b", bold=True),
        "label": Style(color="#61afef"),
        "attribute": Style(color="#c678dd"),
    },
)
_PATCH_STATUS_STYLES = {
    PatchStatus.CURRENT: "bold green",
    PatchStatus.APPLIED: "cyan",
    PatchStatus.UNAPPLIED: "yellow",
    PatchStatus.HIDDEN: "dim magenta",
}
_PATCH_STATUS_MARKERS = {
    PatchStatus.CURRENT: ">",
    PatchStatus.APPLIED: "+",
    PatchStatus.UNAPPLIED: "-",
    PatchStatus.HIDDEN: "~",
}
_FILE_STATUS_STYLES = {
    FileStatus.ADDED: "green",
    FileStatus.DELETED: "red",
    FileStatus.RENAMED: "yellow",
    FileStatus.BINARY: "magenta",
    FileStatus.MODIFIED: "blue",
}


class StackReader(Protocol):
    branch: str | None

    def load_stack(self) -> tuple[Patch, ...]: ...

    def load_staged_changes(self) -> tuple[FileChange, ...]: ...

    def load_unstaged_changes(self) -> tuple[FileChange, ...]: ...

    def stage_file(self, path: str) -> None: ...

    def unstage_file(self, path: str) -> None: ...

    def stage_hunk(self, path: str, hunk_index: int) -> None: ...

    def unstage_hunk(self, path: str, hunk_index: int) -> None: ...

    def new_patch_template(self) -> str: ...

    def create_patch(self, message: str) -> None: ...

    def edit_patch_template(self, patch_name: str) -> str: ...

    def edit_patch(self, patch_name: str, message: str) -> None: ...


class PatchMessageDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    PatchMessageDialog {
        align: center middle;
    }

    #patch-dialog {
        width: 80%;
        height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #patch-message {
        height: 1fr;
    }

    #patch-dialog-actions {
        height: auto;
        align-horizontal: right;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="patch-dialog"):
            yield Static("Edit the patch message, then select Create.")
            yield TextArea(self.message, id="patch-message", soft_wrap=True)
            with Horizontal(id="patch-dialog-actions"):
                yield Button("Cancel", id="cancel-patch")
                yield Button("Create", id="create-patch", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#patch-message", TextArea).focus()

    @on(Button.Pressed, "#create-patch")
    def create_patch(self) -> None:
        self.dismiss(self.query_one("#patch-message", TextArea).text)

    @on(Button.Pressed, "#cancel-patch")
    def cancel_patch(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LayerTree(Tree[TreeItem]):
    BINDINGS = [
        Binding(
            "enter",
            "apply_selected_change",
            "Stage/unstage",
            show=False,
            priority=True,
        ),
        Binding("c", "create_patch", "New patch"),
        Binding("e", "edit_patch", "Edit patch"),
        Binding("left", "collapse_or_parent", show=False),
        Binding("right", "expand_or_child", show=False),
    ]

    async def action_edit_patch(self) -> None:
        await self.app.run_action("edit_selected_patch")

    async def action_create_patch(self) -> None:
        await self.app.run_action("create_patch")

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
        if node is None or not node.allow_expand:
            return
        if not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0], animate=False)


class StgLayersApp(App[None]):
    TITLE = "StGit Layers"
    CSS = """
    #main {
        height: 1fr;
    }

    #tree-pane {
        width: 42%;
        border: round $primary;
    }

    #patch-tree {
        overflow-x: hidden;
    }

    #preview-pane {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }

    #selection-title {
        height: auto;
        text-style: bold;
        padding-bottom: 1;
    }

    #diff-view {
        height: 1fr;
    }

    #status {
        height: 1;
        padding: 0 1;
    }
    """
    BINDINGS = [
        Binding("r", "refresh_stack", "Refresh"),
        Binding("R,shift+r", "toggle_order", "Reverse order", key_display="Shift+R"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, repository: StackReader) -> None:
        super().__init__()
        self.repository = repository
        self.sub_title = repository.branch or "detached HEAD"
        self.newest_first = True
        self._loaded_data: (
            tuple[
                tuple[Patch, ...],
                tuple[FileChange, ...],
                tuple[FileChange, ...],
            ]
            | None
        ) = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="tree-pane"):
                tree = LayerTree(Text("Repository", style="bold"), id="patch-tree")
                tree.root.expand()
                yield tree
            with Vertical(id="preview-pane"):
                yield Static("Select a patch, file, or hunk.", id="selection-title", markup=False)
                preview = TextArea(
                    "",
                    read_only=True,
                    show_cursor=False,
                    show_line_numbers=False,
                    soft_wrap=False,
                    compact=True,
                    id="diff-view",
                )
                if HIGHLIGHTS_QUERY is None:
                    raise RuntimeError("The Tree-sitter diff grammar has no highlight query.")
                preview.register_language(
                    "diff",
                    Language(diff_language()),
                    HIGHLIGHTS_QUERY,
                )
                preview.register_theme(_DIFF_THEME)
                preview.language = "diff"
                preview.theme = _DIFF_THEME.name
                yield preview
        yield Static("Loading StGit stack...", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_stack()

    async def action_create_patch(self) -> None:
        self.query_one("#status", Static).update("Preparing a new patch...")
        try:
            template = await asyncio.to_thread(self.repository.new_patch_template)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return

        await self.push_screen(PatchMessageDialog(template), self._on_patch_message)

    async def action_edit_selected_patch(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or item.patch_name is None or item.file_path is not None:
            return

        patch_name = item.patch_name
        self.query_one("#status", Static).update(f"Preparing to edit patch {patch_name}...")
        try:
            template = await asyncio.to_thread(self.repository.edit_patch_template, patch_name)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return

        await self.push_screen(
            PatchMessageDialog(template),
            lambda message: self._on_edited_patch_message(patch_name, message),
        )

    def _on_edited_patch_message(self, patch_name: str, message: str | None) -> None:
        if message is None:
            self.query_one("#status", Static).update(f"Editing patch {patch_name} cancelled.")
            return
        self.run_worker(self._edit_patch(patch_name, message), exclusive=True)

    async def _edit_patch(self, patch_name: str, message: str) -> None:
        self.query_one("#status", Static).update(f"Updating patch {patch_name}...")
        try:
            await asyncio.to_thread(self.repository.edit_patch, patch_name, message)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return
        await self._load_stack()

    def _on_patch_message(self, message: str | None) -> None:
        if message is None:
            self.query_one("#status", Static).update("Patch creation cancelled.")
            return
        self.run_worker(self._create_patch(message), exclusive=True)

    async def _create_patch(self, message: str) -> None:
        self.query_one("#status", Static).update("Creating patch...")
        try:
            await asyncio.to_thread(self.repository.create_patch, message)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return
        await self._load_stack()

    def action_refresh_stack(self) -> None:
        self.run_worker(self._load_stack(), exclusive=True)

    async def action_apply_selected_change(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or item.change_area not in {"staged", "unstaged"} or item.file_path is None:
            return

        staging = item.change_area == "unstaged"
        verb = "Staging" if staging else "Unstaging"
        target = "hunk" if item.hunk_index is not None else "file"
        self.query_one("#status", Static).update(f"{verb} {target} {item.file_path}...")
        try:
            if item.hunk_index is None:
                if staging:
                    await asyncio.to_thread(self.repository.stage_file, item.file_path)
                else:
                    await asyncio.to_thread(self.repository.unstage_file, item.file_path)
            else:
                if staging:
                    await asyncio.to_thread(
                        self.repository.stage_hunk,
                        item.file_path,
                        item.hunk_index,
                    )
                else:
                    await asyncio.to_thread(
                        self.repository.unstage_hunk,
                        item.file_path,
                        item.hunk_index,
                    )
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return

        await self._load_stack()

    def action_toggle_order(self) -> None:
        self.newest_first = not self.newest_first
        if self._loaded_data is not None:
            self._populate_tree(*self._loaded_data)
            order = "newest first" if self.newest_first else "oldest first"
            self.query_one("#status", Static).update(
                f"Showing {order}. Press R to reverse order; r to refresh."
            )

    @on(Tree.NodeSelected)
    def on_tree_node_selected(self, event: Tree.NodeSelected[TreeItem]) -> None:
        self._show_tree_item(event.node.data)

    @on(Tree.NodeHighlighted)
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[TreeItem]) -> None:
        self._show_tree_item(event.node.data)

    def _show_tree_item(self, item: TreeItem | None) -> None:
        if item is None:
            return
        self.query_one("#selection-title", Static).update(item.title)
        preview = self.query_one("#diff-view", TextArea)
        preview.load_text(item.diff_text or "No text diff available.")

    async def _load_stack(self) -> None:
        status = self.query_one("#status", Static)
        status.update("Loading patch stack and working changes...")
        try:
            patches, staged, unstaged = await asyncio.gather(
                asyncio.to_thread(self.repository.load_stack),
                asyncio.to_thread(self.repository.load_staged_changes),
                asyncio.to_thread(self.repository.load_unstaged_changes),
            )
        except RepositoryError as error:
            self._show_error(error)
            return
        self._loaded_data = patches, staged, unstaged
        self._populate_tree(patches, staged, unstaged)
        count = len(patches)
        order = "newest first" if self.newest_first else "oldest first"
        status.update(
            f"Loaded {count} patch{'es' if count != 1 else ''} ({order}). "
            "Press R to reverse order; r to refresh."
        )

    def _show_error(self, error: RepositoryError) -> None:
        tree = self.query_one("#patch-tree", Tree)
        tree.root.remove_children()
        tree.root.add("Unable to load StGit stack", allow_expand=False)
        self.query_one("#status", Static).update(f"Error: {error}")

    def _populate_tree(
        self,
        patches: tuple[Patch, ...],
        staged: tuple[FileChange, ...],
        unstaged: tuple[FileChange, ...],
    ) -> None:
        tree = self.query_one("#patch-tree", Tree)
        tree.root.remove_children()
        ordered_patches = tuple(reversed(patches)) if self.newest_first else patches

        def add_patch_stack() -> None:
            patch_stack = tree.root.add(
                Text("Patch stack", style="bold cyan"),
                data=TreeItem(title="Patch stack", diff_text=""),
                expand=True,
            )
            if not ordered_patches:
                patch_stack.add("No patches in this stack.", allow_expand=False)
            else:
                for patch in ordered_patches:
                    self._add_patch(patch_stack, patch)

        if self.newest_first:
            self._add_change_group(tree.root, "Unstaged changes", "unstaged", unstaged)
            self._add_change_group(tree.root, "Staged changes", "staged", staged)
            add_patch_stack()
        else:
            add_patch_stack()
            self._add_change_group(tree.root, "Staged changes", "staged", staged)
            self._add_change_group(tree.root, "Unstaged changes", "unstaged", unstaged)

    @staticmethod
    def _add_change_group(
        parent: TreeNode[TreeItem],
        label: str,
        change_area: str,
        files: tuple[FileChange, ...],
    ) -> None:
        group = parent.add(
            Text(label, style="bold cyan"),
            data=TreeItem(title=label, diff_text="", change_area=change_area),
            expand=True,
            allow_expand=True,
        )
        if not files:
            group.add(f"No {change_area} changes.", allow_expand=False)
            return

        for file in files:
            StgLayersApp._add_file(group, file, None, change_area)

    @staticmethod
    def _add_patch(parent: TreeNode[TreeItem], patch: Patch) -> None:
        label = Text()
        status_style = _PATCH_STATUS_STYLES[patch.status]
        label.append(f"[{_PATCH_STATUS_MARKERS[patch.status]}{patch.name}]", style=status_style)
        if patch.description:
            label.append(" ")
            label.append(patch.description, style="bold")
        patch_node = parent.add(
            label,
            data=TreeItem(
                title=f"Patch: {patch.name}",
                diff_text=patch.diff_text,
                patch_name=patch.name,
            ),
            expand=True,
        )
        if not patch.files:
            patch_node.add("(empty patch)", allow_expand=False)
            return

        for file in patch.files:
            StgLayersApp._add_file(patch_node, file, patch.name, None)

    @staticmethod
    def _add_file(
        parent: TreeNode[TreeItem],
        file: FileChange,
        patch_name: str | None,
        change_area: str | None,
    ) -> None:
        display_path = file.path
        if file.status == FileStatus.RENAMED:
            display_path = f"{file.source_path} -> {file.target_path}"
        if patch_name is None:
            if change_area is None:
                raise ValueError("A file must belong to a patch or change area.")
            section = f"{change_area.title()} changes"
        else:
            section = patch_name
        label = Text()
        label.append(display_path, style="cyan")
        label.append(f" [{file.status.value}]", style=_FILE_STATUS_STYLES[file.status])
        file_node = parent.add(
            label,
            data=TreeItem(
                title=f"{section} / {display_path}",
                diff_text=file.diff_text,
                patch_name=patch_name,
                file_path=file.path,
                change_area=change_area,
            ),
            expand=False,
            allow_expand=bool(file.hunks),
        )
        for hunk in file.hunks:
            file_node.add(
                Text(hunk.header, style="yellow"),
                data=TreeItem(
                    title=f"{section} / {display_path} / hunk {hunk.index + 1}",
                    diff_text=hunk.diff_text,
                    patch_name=patch_name,
                    file_path=file.path,
                    hunk_index=hunk.index,
                    change_area=change_area,
                ),
                allow_expand=False,
            )
