import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
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
from stg_layers.repository import PatchChangeMoveResult, PatchMoveResult, RepositoryError

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


@dataclass
class _FileDirectory:
    files: list[FileChange] = field(default_factory=list)
    directories: dict[str, "_FileDirectory"] = field(default_factory=dict)


class StackReader(Protocol):
    branch: str | None

    def has_conflicts(self) -> bool: ...

    def load_stack(self) -> tuple[Patch, ...]: ...

    def load_staged_changes(self) -> tuple[FileChange, ...]: ...

    def load_unstaged_changes(self) -> tuple[FileChange, ...]: ...

    def stage_file(self, path: str) -> None: ...

    def unstage_file(self, path: str) -> None: ...

    def stage_files(self, paths: tuple[str, ...]) -> None: ...

    def unstage_files(self, paths: tuple[str, ...]) -> None: ...

    def refresh_staged_files_to_current_patch(self, paths: tuple[str, ...]) -> None: ...

    def spill_current_patch_files(self, paths: tuple[str, ...]) -> None: ...

    def move_patch_changes(
        self, patch_name: str, paths: tuple[str, ...], *, toward_newer: bool
    ) -> tuple[PatchChangeMoveResult, str | None]: ...

    def stage_hunk(self, path: str, hunk_index: int) -> None: ...

    def unstage_hunk(self, path: str, hunk_index: int) -> None: ...

    def new_patch_template(self) -> str: ...

    def create_patch(self, message: str) -> None: ...

    def edit_patch_template(self, patch_name: str) -> str: ...

    def edit_patch(self, patch_name: str, message: str) -> None: ...

    def refresh_staged_file_to_current_patch(self, path: str) -> None: ...

    def spill_current_patch_file(self, path: str) -> None: ...

    def move_patch_one_step(
        self,
        patch_name: str,
        *,
        toward_newer: bool,
    ) -> PatchMoveResult: ...

    def move_patch_change(
        self,
        patch_name: str,
        path: str,
        *,
        toward_newer: bool,
        hunk_diff: str | None = None,
    ) -> tuple[PatchChangeMoveResult, str | None]: ...

    def push_patch(self, patch_name: str) -> None: ...

    def pop_patch(self, patch_name: str) -> None: ...

    def commit_patch(self, patch_name: str) -> None: ...

    def uncommit_patch(self) -> None: ...

    def hide_patch(self, patch_name: str) -> None: ...

    def unhide_patch(self, patch_name: str) -> None: ...

    def delete_patch(self, patch_name: str) -> None: ...

    def undo_stack_operation(self) -> None: ...

    def refresh_stack_operation(self) -> str | None: ...

    @property
    def stack_operation_pending(self) -> bool: ...

    @property
    def stack_operation_stash_reference(self) -> str | None: ...

    @property
    def stash_restore_conflict(self) -> bool: ...


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


class DeletePatchDialog(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    DeletePatchDialog { align: center middle; }
    #delete-dialog {
        width: 64;
        height: auto;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    #delete-actions { height: auto; align-horizontal: right; }
    """

    def __init__(self, patch_name: str) -> None:
        super().__init__()
        self.patch_name = patch_name

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(Text(f"Delete patch {self.patch_name}?"))
            with Horizontal(id="delete-actions"):
                yield Button("Cancel", id="cancel-delete")
                yield Button("OK", id="confirm-delete", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel-delete", Button).focus()

    @on(Button.Pressed, "#cancel-delete")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-delete")
    def confirm(self) -> None:
        self.dismiss(True)


class ConflictDialog(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "quit", "Quit", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]
    DEFAULT_CSS = """
    ConflictDialog {
        align: center middle;
    }

    #conflict-dialog {
        width: 72;
        height: auto;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }

    #conflict-description {
        height: auto;
        padding-bottom: 1;
    }

    #conflict-actions {
        height: auto;
        align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        operation_failed: bool,
        stash_reference: str | None = None,
        stash_restore_conflict: bool = False,
    ) -> None:
        super().__init__()
        self.operation_failed = operation_failed
        self.stash_reference = stash_reference
        self.stash_restore_conflict = stash_restore_conflict

    def compose(self) -> ComposeResult:
        if self.stash_restore_conflict:
            description = (
                "Restoring your saved local changes conflicted. Undo rolls back the stack "
                "operation when applicable and retries restoring those changes. Resolve the "
                "conflicts in another terminal, then choose Refresh."
            )
        elif self.operation_failed:
            description = (
                "The patch stack operation caused merge conflicts. Undo uses "
                "`stg undo --hard` to restore the previous stack and discard the conflicted state. "
                "Choose Refresh after resolving the conflict in another terminal."
            )
        else:
            description = (
                "This repository has unresolved merge conflicts. StGit Layers is locked "
                "until they are resolved in another terminal. Refresh to check again, "
                "or quit to return to the terminal."
            )
        if self.stash_reference is not None:
            description += (
                f" Your local changes are safely saved in Git stash "
                f"{self.stash_reference}; Quit leaves that stash intact."
            )
        with Vertical(id="conflict-dialog"):
            yield Static("Repository conflict", classes="error")
            yield Static(description, id="conflict-description")
            with Horizontal(id="conflict-actions"):
                if self.operation_failed:
                    yield Button("Undo", id="undo-conflict", variant="warning")
                yield Button("Quit", id="quit-conflict")
                yield Button("Refresh", id="refresh-conflict", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#refresh-conflict", Button).focus()

    @on(Button.Pressed, "#undo-conflict")
    def undo_reorder(self) -> None:
        self.dismiss("undo")

    @on(Button.Pressed, "#refresh-conflict")
    def refresh_repository(self) -> None:
        self.dismiss("refresh")

    @on(Button.Pressed, "#quit-conflict")
    def quit_to_terminal(self) -> None:
        self.dismiss("quit")

    def action_quit(self) -> None:
        self.dismiss("quit")


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
        Binding("C,shift+c", "uncommit_patch", "Uncommit", key_display="Shift+C"),
        Binding("h", "patch_operation('hide')", "Hide patch"),
        Binding("H,shift+h", "patch_operation('unhide')", "Unhide patch", key_display="Shift+H"),
        Binding("delete", "patch_operation('delete')", "Delete patch"),
        Binding("e", "edit_patch", "Edit patch"),
        Binding("p", "push_patch", "Push patch"),
        Binding("P,shift+p", "pop_patch", "Pop patch", key_display="Shift+P"),
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

    async def action_push_patch(self) -> None:
        await self.app.run_action("push_selected_patch")

    async def action_pop_patch(self) -> None:
        await self.app.run_action("pop_selected_patch")

    async def action_create_patch(self) -> None:
        await self.app.run_action("create_patch")

    async def action_patch_operation(self, operation: str) -> None:
        await self.app.run_action(f"patch_operation('{operation}')")

    async def action_uncommit_patch(self) -> None:
        await self.app.run_action("uncommit_patch")

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
        if node is not None and node.data is not None and node.data.hunk_index is not None:
            self.app.query_one("#diff-view", TextArea).focus()
            return
        if node is None or not node.allow_expand:
            return
        if not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0], animate=False)


class DiffPreview(TextArea):
    BINDINGS = [
        Binding("n", "next_hunk", "Next hunk", priority=True),
        Binding("p", "previous_hunk", "Previous hunk", priority=True),
        Binding("enter", "apply_change", "Stage/unstage", priority=True),
        Binding("w", "toggle_word_wrap", "Word wrap", priority=True),
    ]

    async def action_next_hunk(self) -> None:
        await self.app.run_action("next_preview_hunk")

    async def action_previous_hunk(self) -> None:
        await self.app.run_action("previous_preview_hunk")

    async def action_apply_change(self) -> None:
        await self.app.run_action("apply_preview_change")

    def action_toggle_word_wrap(self) -> None:
        self.soft_wrap = not self.soft_wrap


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
        self._conflict_locked = False
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
                preview = DiffPreview(
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
        if not await self._ensure_repository_ready():
            return
        self.query_one("#status", Static).update("Preparing a new patch...")
        try:
            template = await asyncio.to_thread(self.repository.new_patch_template)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return

        await self.push_screen(PatchMessageDialog(template), self._on_patch_message)

    async def action_edit_selected_patch(self) -> None:
        if not await self._ensure_repository_ready():
            return
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if (
            item is None
            or item.patch_name is None
            or item.file_path is not None
            or item.directory_path is not None
        ):
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
        if not await self._ensure_repository_ready():
            return
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
        if not await self._ensure_repository_ready():
            return
        self.query_one("#status", Static).update("Creating patch...")
        try:
            await asyncio.to_thread(self.repository.create_patch, message)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return
        await self._load_stack()

    def action_refresh_stack(self) -> None:
        self.run_worker(self._load_stack(), exclusive=True)

    async def _ensure_repository_ready(self) -> bool:
        if self._conflict_locked:
            return False
        try:
            conflicted = await asyncio.to_thread(self.repository.has_conflicts)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return False
        if conflicted:
            self._conflict_locked = True
            await self.push_screen(
                ConflictDialog(operation_failed=False),
                self._on_repository_conflict_resolution,
            )
            return False
        self._conflict_locked = False
        return True

    def _on_repository_conflict_resolution(self, resolution: str | None) -> None:
        if resolution != "refresh":
            self.exit()
            return
        self._conflict_locked = False
        self.run_worker(self._load_stack(), exclusive=True)

    async def _resolve_stack_operation_conflict(self) -> None:
        self._conflict_locked = True
        await self.push_screen(
            ConflictDialog(
                operation_failed=True,
                stash_reference=self.repository.stack_operation_stash_reference,
                stash_restore_conflict=self.repository.stash_restore_conflict,
            ),
            self._on_stack_operation_conflict_resolution,
        )

    def _on_stack_operation_conflict_resolution(self, resolution: str | None) -> None:
        self.run_worker(
            self._finish_stack_operation_conflict_resolution(resolution),
            exclusive=True,
        )

    async def _finish_stack_operation_conflict_resolution(
        self,
        resolution: str | None,
    ) -> None:
        if resolution != "undo" and resolution != "refresh":
            self.exit()
            return
        try:
            retained_stash: str | None = None
            if resolution == "undo":
                await asyncio.to_thread(self.repository.undo_stack_operation)
            else:
                retained_stash = await asyncio.to_thread(self.repository.refresh_stack_operation)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            try:
                conflicted = await asyncio.to_thread(self.repository.has_conflicts)
            except RepositoryError:
                conflicted = False
            if conflicted or self.repository.stack_operation_pending:
                await self._resolve_stack_operation_conflict()
            return
        self._conflict_locked = False
        await self._load_stack()
        if retained_stash is not None:
            self.query_one("#status", Static).update(
                "Repository refreshed. Local changes were manually resolved; recovery "
                f"stash {retained_stash} was retained."
            )

    async def action_push_selected_patch(self) -> None:
        await self._set_selected_patch_application(push=True)

    async def action_pop_selected_patch(self) -> None:
        await self._set_selected_patch_application(push=False)

    async def _set_selected_patch_application(self, *, push: bool) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if (
            item is None
            or item.patch_name is None
            or item.file_path is not None
            or item.directory_path is not None
            or item.hunk_index is not None
            or self._loaded_data is None
        ):
            return

        patch = next(
            (patch for patch in self._loaded_data[0] if patch.name == item.patch_name),
            None,
        )
        if patch is None:
            return
        valid_statuses = (
            {PatchStatus.UNAPPLIED} if push else {PatchStatus.APPLIED, PatchStatus.CURRENT}
        )
        if patch.status not in valid_statuses:
            expected = "unapplied" if push else "applied"
            verb = "push" if push else "pop"
            self.query_one("#status", Static).update(f"Select an {expected} patch to {verb}.")
            return
        if not await self._ensure_repository_ready():
            return

        verb = "Pushing" if push else "Popping"
        self.query_one("#status", Static).update(f"{verb} patch {item.patch_name}...")
        operation = self.repository.push_patch if push else self.repository.pop_patch
        try:
            await asyncio.to_thread(operation, item.patch_name)
        except RepositoryError as error:
            await self._handle_stack_operation_error(error)
            return

        await self._load_stack(
            preferred_selection=TreeItem(
                title=f"Patch: {item.patch_name}",
                diff_text="",
                patch_name=item.patch_name,
            )
        )

    async def action_uncommit_patch(self) -> None:
        if not await self._ensure_repository_ready():
            return
        try:
            await asyncio.to_thread(self.repository.uncommit_patch)
        except RepositoryError as error:
            await self._handle_stack_operation_error(error)
            return
        await self._load_stack()

    async def action_patch_operation(self, operation: str) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if (
            item is None
            or item.patch_name is None
            or item.file_path is not None
            or item.directory_path is not None
        ):
            self.query_one("#status", Static).update(f"Select a patch to {operation}.")
            return
        if not await self._ensure_repository_ready():
            return
        if operation == "delete":
            patch_name = item.patch_name

            def confirmed(result: bool | None) -> None:
                if result:
                    self.run_worker(
                        self._run_patch_operation(operation, patch_name), exclusive=True
                    )

            self.push_screen(DeletePatchDialog(patch_name), confirmed)
            return
        await self._run_patch_operation(operation, item.patch_name)

    async def _run_patch_operation(self, operation: str, patch_name: str) -> None:
        if not await self._ensure_repository_ready():
            return
        operations = {
            "commit": self.repository.commit_patch,
            "hide": self.repository.hide_patch,
            "unhide": self.repository.unhide_patch,
            "delete": self.repository.delete_patch,
        }
        self.query_one("#status", Static).update(f"Running {operation} on patch {patch_name}...")
        try:
            await asyncio.to_thread(operations[operation], patch_name)
        except RepositoryError as error:
            await self._handle_stack_operation_error(error)
            return
        await self._load_stack(
            preferred_selection=TreeItem(title="", diff_text="", patch_name=patch_name)
        )

    async def _handle_stack_operation_error(self, error: RepositoryError) -> None:
        try:
            conflicted = await asyncio.to_thread(self.repository.has_conflicts)
        except RepositoryError as conflict_check_error:
            self.query_one("#status", Static).update(
                f"Error: {error}; could not check for conflicts: {conflict_check_error}"
            )
            return
        if conflicted or self.repository.stack_operation_pending:
            await self._resolve_stack_operation_conflict()
        else:
            self.query_one("#status", Static).update(f"Error: {error}")

    async def action_apply_selected_change(self) -> None:
        if await self._operate_selected_file_group():
            return
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or item.change_area not in {"staged", "unstaged"} or item.file_path is None:
            return

        await self.action_move_file_between_changes(
            stage=item.change_area == "unstaged",
            advance=True,
        )

    async def action_move_staged_file_to_current_patch(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is not None and item.change_area == "unstaged":
            await self.action_move_file_between_changes(stage=True)
            return
        if (
            item is None
            or item.change_area != "staged"
            or item.file_path is None
            or item.hunk_index is not None
        ):
            return
        if not await self._ensure_repository_ready():
            return

        self.query_one("#status", Static).update(
            f"Moving staged file {item.file_path} into the current patch..."
        )
        try:
            await asyncio.to_thread(
                self.repository.refresh_staged_file_to_current_patch,
                item.file_path,
            )
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return
        current_patch = (
            next(
                (patch for patch in self._loaded_data[0] if patch.status == PatchStatus.CURRENT),
                None,
            )
            if self._loaded_data is not None
            else None
        )
        if current_patch is None:
            await self._load_stack()
            return
        await self._load_stack(
            preferred_selection=TreeItem(
                title="",
                diff_text="",
                file_path=item.file_path,
                patch_name=current_patch.name,
            ),
            expansion_fallback_path=item.file_path,
        )

    async def _operate_selected_file_group(self, *, toward_newer: bool | None = None) -> bool:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if node is None or item is None:
            return False
        is_change_group = (
            item.change_area in {"staged", "unstaged"}
            and item.file_path is None
            and item.directory_path is None
        )
        if item.directory_path is None and not is_change_group:
            return False
        if toward_newer is None and item.change_area is None:
            return True
        if not await self._ensure_repository_ready():
            return True
        paths = tuple(
            child.data.file_path for child in self._tree_nodes(node)
            if child.data is not None
            and child.data.file_path is not None
            and child.data.hunk_index is None
        )
        label = item.directory_path or item.title
        self.query_one("#status", Static).update(f"Moving files in {label}...")
        try:
            selection = await self._move_file_group(item, paths, toward_newer)
        except RepositoryError as error:
            await self._handle_stack_operation_error(error)
            return True
        if selection is not None:
            if toward_newer is None:
                selection = TreeItem(
                    title=f"{item.change_area.title()} changes" if item.change_area else "",
                    diff_text="", change_area=item.change_area,
                )
            await self._load_stack(preferred_selection=selection)
        return True

    async def _move_file_group(
        self, item: TreeItem, paths: tuple[str, ...], toward_newer: bool | None
    ) -> TreeItem | None:
        if not paths:
            self.query_one("#status", Static).update("No files to move in this group.")
            return None
        if item.patch_name is not None:
            return await self._move_patch_directory(item, paths, bool(toward_newer))
        if item.change_area == "unstaged":
            if toward_newer:
                self.query_one("#status", Static).update("Unstaged changes are at the edge.")
                return None
            await asyncio.to_thread(self.repository.stage_files, paths)
            return replace(item, change_area="staged")
        if toward_newer is None or toward_newer:
            await asyncio.to_thread(self.repository.unstage_files, paths)
            return replace(item, change_area="unstaged")
        current = next(
            (patch for patch in self._loaded_data[0] if patch.status == PatchStatus.CURRENT), None
        ) if self._loaded_data is not None else None
        if current is None:
            self.query_one("#status", Static).update("There is no current patch to refresh.")
            return None
        await asyncio.to_thread(self.repository.refresh_staged_files_to_current_patch, paths)
        return replace(item, change_area=None, patch_name=current.name)

    async def _move_patch_directory(
        self, item: TreeItem, paths: tuple[str, ...], toward_newer: bool
    ) -> TreeItem | None:
        patch = next(
            (patch for patch in self._loaded_data[0] if patch.name == item.patch_name), None
        ) if self._loaded_data is not None else None
        if patch is None:
            return None
        if patch.status == PatchStatus.CURRENT and toward_newer:
            await asyncio.to_thread(self.repository.spill_current_patch_files, paths)
            return replace(item, patch_name=None, change_area="staged")
        result, target = await asyncio.to_thread(
            self.repository.move_patch_changes, patch.name, paths, toward_newer=toward_newer
        )
        if result != PatchChangeMoveResult.MOVED:
            self._show_patch_change_move_result(
                result, patch.status, patch.name, toward_newer, False
            )
            return None
        return replace(item, patch_name=target)

    async def action_move_change_down(self) -> None:
        if await self._operate_selected_file_group(toward_newer=not self.newest_first):
            return
        if await self._reorder_selected_patch(toward_newer=not self.newest_first):
            return
        if await self._move_selected_patch_change(toward_newer=not self.newest_first):
            return
        if self.newest_first:
            await self.action_move_staged_file_to_current_patch()
        else:
            await self.action_spill_current_patch_file()

    async def action_move_change_up(self) -> None:
        if await self._operate_selected_file_group(toward_newer=self.newest_first):
            return
        if await self._reorder_selected_patch(toward_newer=self.newest_first):
            return
        if await self._move_selected_patch_change(toward_newer=self.newest_first):
            return
        if self.newest_first:
            await self.action_spill_current_patch_file()
        else:
            await self.action_move_staged_file_to_current_patch()

    async def _move_selected_patch_change(self, *, toward_newer: bool) -> bool:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if (
            item is None
            or item.patch_name is None
            or item.file_path is None
            or self._loaded_data is None
        ):
            return False
        if not await self._ensure_repository_ready():
            return True

        patches = self._loaded_data[0]
        source_index = next(
            (index for index, patch in enumerate(patches) if patch.name == item.patch_name),
            None,
        )
        if source_index is None:
            return False
        source_patch = patches[source_index]
        change_name = "hunk" if item.hunk_index is not None else "file"
        direction = "newer" if toward_newer else "older"
        self.query_one("#status", Static).update(
            f"Moving {change_name} {item.file_path} {direction} from patch {item.patch_name}..."
        )
        try:
            result, moved_to = await asyncio.to_thread(
                self.repository.move_patch_change,
                item.patch_name,
                item.file_path,
                toward_newer=toward_newer,
                hunk_diff=item.diff_text if item.hunk_index is not None else None,
            )
        except RepositoryError as error:
            await self._handle_stack_operation_error(error)
            return True

        if result != PatchChangeMoveResult.MOVED:
            return self._show_patch_change_move_result(
                result,
                source_patch.status,
                item.patch_name,
                toward_newer,
                item.hunk_index is None,
            )
        if moved_to is None:
            self.query_one("#status", Static).update(
                f"Could not determine the destination patch for {item.file_path}."
            )
            return True

        await self._load_stack(
            preferred_selection=TreeItem(
                title="",
                diff_text="",
                patch_name=moved_to,
                file_path=item.file_path,
                hunk_index=item.hunk_index,
                hunk_key=item.hunk_key,
            ),
            expansion_fallback_path=item.file_path,
        )
        return True

    def _show_patch_change_move_result(
        self,
        result: PatchChangeMoveResult,
        source_status: PatchStatus,
        patch_name: str,
        toward_newer: bool,
        selected_file: bool,
    ) -> bool:
        if result == PatchChangeMoveResult.STACK_EDGE:
            if toward_newer and source_status == PatchStatus.CURRENT and selected_file:
                return False
            message = f"Patch {patch_name} is at the edge of the stack."
        elif result == PatchChangeMoveResult.APPLICATION_BOUNDARY:
            if toward_newer and source_status == PatchStatus.CURRENT and selected_file:
                return False
            message = "Changes can only be moved between adjacent applied patches."
        else:
            message = "Changes can only be moved from applied patches."
        self.query_one("#status", Static).update(message)
        return True

    async def _reorder_selected_patch(self, *, toward_newer: bool) -> bool:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if (
            item is None
            or item.patch_name is None
            or item.file_path is not None
            or item.directory_path is not None
            or item.hunk_index is not None
        ):
            return False
        if not await self._ensure_repository_ready():
            return True

        direction = "toward newer patches" if toward_newer else "toward older patches"
        self.query_one("#status", Static).update(
            f"Moving patch {item.patch_name} one position {direction}..."
        )
        try:
            result = await asyncio.to_thread(
                self.repository.move_patch_one_step,
                item.patch_name,
                toward_newer=toward_newer,
            )
        except RepositoryError as error:
            await self._handle_stack_operation_error(error)
            return True

        if result == PatchMoveResult.STACK_EDGE:
            position = "top" if toward_newer else "bottom"
            self.query_one("#status", Static).update(
                f"Patch {item.patch_name} is already at the {position} of the stack."
            )
            return True
        if result == PatchMoveResult.APPLICATION_BOUNDARY:
            self.query_one("#status", Static).update(
                "Patches cannot be reordered across the applied/unapplied boundary."
            )
            return True

        await self._load_stack(
            preferred_selection=TreeItem(
                title=f"Patch: {item.patch_name}",
                diff_text="",
                patch_name=item.patch_name,
            )
        )
        return True

    async def action_spill_current_patch_file(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is not None and item.change_area == "staged":
            await self.action_move_file_between_changes(stage=False)
            return
        if (
            item is None
            or item.patch_name is None
            or item.file_path is None
            or item.hunk_index is not None
            or self._loaded_data is None
            or not any(
                patch.name == item.patch_name and patch.status == PatchStatus.CURRENT
                for patch in self._loaded_data[0]
            )
        ):
            return
        if not await self._ensure_repository_ready():
            return

        self.query_one("#status", Static).update(
            f"Moving {item.file_path} out of the current patch..."
        )
        try:
            await asyncio.to_thread(self.repository.spill_current_patch_file, item.file_path)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return
        await self._load_stack(
            preferred_selection=TreeItem(
                title="",
                diff_text="",
                file_path=item.file_path,
                change_area="staged",
            ),
            expansion_fallback_path=item.file_path,
        )

    async def action_move_file_between_changes(
        self,
        *,
        stage: bool,
        advance: bool = False,
    ) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        expected_area = "unstaged" if stage else "staged"
        target_area = "staged" if stage else "unstaged"
        if item is None or item.change_area != expected_area or item.file_path is None:
            return
        if not await self._ensure_repository_ready():
            return

        next_selection = (
            self._next_change_selection(node, item) if advance and node is not None else None
        )
        verb = "Staging" if stage else "Unstaging"
        target = "hunk" if item.hunk_index is not None else "file"
        self.query_one("#status", Static).update(f"{verb} {target} {item.file_path}...")
        try:
            if item.hunk_index is None:
                operation = self.repository.stage_file if stage else self.repository.unstage_file
                await asyncio.to_thread(operation, item.file_path)
            else:
                operation = self.repository.stage_hunk if stage else self.repository.unstage_hunk
                await asyncio.to_thread(operation, item.file_path, item.hunk_index)
        except RepositoryError as error:
            self.query_one("#status", Static).update(f"Error: {error}")
            return

        preferred_selection = (
            next_selection
            or TreeItem(
                title=f"{item.change_area.title()} changes",
                diff_text="",
                change_area=item.change_area,
            )
            if advance
            else TreeItem(
                title="",
                diff_text="",
                file_path=item.file_path,
                hunk_index=item.hunk_index,
                hunk_key=item.hunk_key,
                change_area=target_area,
            )
        )
        await self._load_stack(
            preferred_selection=preferred_selection,
            expansion_fallback_path=item.file_path
            if not advance
            else (item.file_path if preferred_selection.file_path == item.file_path else None),
        )

    @classmethod
    def _next_change_selection(
        cls,
        node: TreeNode[TreeItem],
        item: TreeItem,
    ) -> TreeItem:
        if item.hunk_index is not None:
            file_node = node.parent
            if file_node is not None:
                next_hunk = cls._next_sibling_item(
                    node,
                    tuple(file_node.children),
                    item.change_area,
                )
                if next_hunk is not None:
                    return next_hunk
                group_node = file_node.parent
            else:
                group_node = None
        else:
            file_node = node
            group_node = node.parent

        if file_node is not None and group_node is not None:
            siblings = cls._change_group_files(group_node)
            next_file = cls._next_sibling_file(file_node, siblings)
            if next_file is not None and next_file.data is not None:
                return next_file.data
            previous_file = cls._previous_sibling_file(file_node, siblings)
            if previous_file is not None and previous_file.data is not None:
                return previous_file.data

        return TreeItem(
            title=f"{item.change_area.title()} changes" if item.change_area else "",
            diff_text="",
            change_area=item.change_area,
        )

    @classmethod
    def _change_group_files(cls, node: TreeNode[TreeItem]) -> tuple[TreeNode[TreeItem], ...]:
        while node.data is not None and node.data.directory_path is not None:
            if node.parent is None:
                break
            node = node.parent
        return tuple(
            child
            for child in cls._tree_nodes(node)
            if child.data is not None
            and child.data.file_path is not None
            and child.data.hunk_index is None
        )

    @classmethod
    def _next_sibling_item(
        cls,
        node: TreeNode[TreeItem],
        siblings: Sequence[TreeNode[TreeItem]],
        change_area: str | None,
    ) -> TreeItem | None:
        index = siblings.index(node)
        for sibling in siblings[index + 1 :]:
            if sibling.data is not None and sibling.data.change_area == change_area:
                return sibling.data
        return None

    @staticmethod
    def _next_sibling_file(
        node: TreeNode[TreeItem],
        siblings: Sequence[TreeNode[TreeItem]],
    ) -> TreeNode[TreeItem] | None:
        index = siblings.index(node)
        return next(
            (
                sibling
                for sibling in siblings[index + 1 :]
                if sibling.data is not None
                and sibling.data.file_path is not None
                and sibling.data.hunk_index is None
            ),
            None,
        )

    @staticmethod
    def _previous_sibling_file(
        node: TreeNode[TreeItem],
        siblings: Sequence[TreeNode[TreeItem]],
    ) -> TreeNode[TreeItem] | None:
        index = siblings.index(node)
        return next(
            (
                sibling
                for sibling in reversed(siblings[:index])
                if sibling.data is not None
                and sibling.data.file_path is not None
                and sibling.data.hunk_index is None
            ),
            None,
        )

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

    def action_next_preview_hunk(self) -> None:
        self._navigate_preview_hunk(forward=True)

    def action_previous_preview_hunk(self) -> None:
        self._navigate_preview_hunk(forward=False)

    def _navigate_preview_hunk(self, *, forward: bool) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        nodes = self._tree_nodes(tree.root)
        current = tree.cursor_node
        index = nodes.index(current) if current in nodes else (-1 if forward else len(nodes))
        candidates = nodes[index + 1 :] if forward else reversed(nodes[:index])
        selected = next(
            (
                node for node in candidates
                if node.data is not None and node.data.hunk_index is not None
            ),
            None,
        )
        if selected is None:
            return
        ancestor = selected.parent
        while ancestor is not None:
            ancestor.expand()
            ancestor = ancestor.parent
        # Rebuild the visible tree before moving to a previously collapsed hunk.
        self.call_after_refresh(self._restore_tree_selection, tree, selected)

    async def action_apply_preview_change(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        item = tree.cursor_node.data if tree.cursor_node is not None else None
        if item is None or item.file_path is None:
            return
        await self.action_apply_selected_change()

    def _show_tree_item(self, item: TreeItem | None) -> None:
        if item is None:
            return
        self.query_one("#selection-title", Static).update(item.title)
        preview = self.query_one("#diff-view", TextArea)
        preview.load_text(item.diff_text or "No text diff available.")

    @staticmethod
    def _tree_item_key(item: TreeItem) -> tuple[str | None, ...]:
        if (
            any((item.change_area, item.patch_name, item.file_path, item.directory_path))
            or item.hunk_index is not None
        ):
            return (
                item.change_area,
                item.patch_name,
                item.file_path,
                item.directory_path,
                (item.hunk_key or str(item.hunk_index) if item.hunk_index is not None else None),
            )
        return (item.title,)

    @staticmethod
    def _hunk_key(diff_text: str) -> str:
        changed_lines = (
            line
            for line in diff_text.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        return "\n".join(changed_lines)

    @staticmethod
    def _tree_nodes(root: TreeNode[TreeItem]) -> list[TreeNode[TreeItem]]:
        nodes: list[TreeNode[TreeItem]] = []
        pending = list(reversed(root.children))
        while pending:
            node = pending.pop()
            nodes.append(node)
            pending.extend(reversed(node.children))
        return nodes

    async def _load_stack(
        self,
        preferred_selection: TreeItem | None = None,
        expansion_fallback_path: str | None = None,
    ) -> None:
        status = self.query_one("#status", Static)
        try:
            if await asyncio.to_thread(self.repository.has_conflicts):
                self._conflict_locked = True
                status.update("Merge conflicts detected; repository operations are locked.")
                await self.push_screen(
                    ConflictDialog(operation_failed=False),
                    self._on_repository_conflict_resolution,
                )
                return
            self._conflict_locked = False
            status.update("Loading patch stack and working changes...")
            patches, staged, unstaged = await asyncio.gather(
                asyncio.to_thread(self.repository.load_stack),
                asyncio.to_thread(self.repository.load_staged_changes),
                asyncio.to_thread(self.repository.load_unstaged_changes),
            )
        except RepositoryError as error:
            self._show_error(error)
            return
        self._loaded_data = patches, staged, unstaged
        self._populate_tree(
            patches,
            staged,
            unstaged,
            preferred_selection=preferred_selection,
            expansion_fallback_path=expansion_fallback_path,
        )
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
        preferred_selection: TreeItem | None = None,
        expansion_fallback_path: str | None = None,
    ) -> None:
        tree = self.query_one("#patch-tree", Tree)
        old_nodes = self._tree_nodes(tree.root)
        old_cursor = tree.cursor_node
        previous_selection = old_cursor.data if old_cursor is not None else None
        selection = preferred_selection or previous_selection
        expanded_nodes = {
            self._tree_item_key(node.data): node.is_expanded
            for node in old_nodes
            if node.data is not None
        }
        fallback_expanded = self._selected_file_expansion(
            old_cursor,
            tree.root,
            expansion_fallback_path,
        )

        tree.root.remove_children()
        ordered_patches = tuple(reversed(patches)) if self.newest_first else patches

        if self.newest_first:
            self._add_change_group(tree.root, "Unstaged changes", "unstaged", unstaged)
            self._add_change_group(tree.root, "Staged changes", "staged", staged)
            self._add_patch_stack(tree.root, ordered_patches)
        else:
            self._add_patch_stack(tree.root, ordered_patches)
            self._add_change_group(tree.root, "Staged changes", "staged", staged)
            self._add_change_group(tree.root, "Unstaged changes", "unstaged", unstaged)

        nodes = self._tree_nodes(tree.root)
        expansion_states = self._tree_expansion_states(
            nodes,
            expanded_nodes,
            selection,
            expansion_fallback_path,
            fallback_expanded,
        )
        selected_node = self._find_selection_node(nodes, selection)
        if selected_node is None and nodes:
            selected_node = nodes[0]
        should_restore_selection = preferred_selection is not None or previous_selection is not None
        if selected_node is not None and should_restore_selection:
            self.call_after_refresh(
                self._restore_tree_state,
                tree,
                selected_node,
                expansion_states,
            )

    @staticmethod
    def _selected_file_expansion(
        old_cursor: TreeNode[TreeItem] | None,
        root: TreeNode[TreeItem],
        file_path: str | None,
    ) -> bool | None:
        selected_node = old_cursor
        while file_path is not None and selected_node is not None and selected_node != root:
            item = selected_node.data
            if item is not None and item.file_path == file_path and item.hunk_index is None:
                return selected_node.is_expanded
            selected_node = selected_node.parent
        return None

    @staticmethod
    def _add_patch_stack(
        root: TreeNode[TreeItem],
        patches: tuple[Patch, ...],
    ) -> None:
        patch_stack = root.add(
            Text("Patch stack", style="bold cyan"),
            data=TreeItem(title="Patch stack", diff_text=""),
            expand=True,
        )
        if not patches:
            patch_stack.add("No patches in this stack.", allow_expand=False)
            return
        for patch in patches:
            StgLayersApp._add_patch(patch_stack, patch)

    @staticmethod
    def _matches_expansion_fallback(
        item: TreeItem,
        selection: TreeItem | None,
        file_path: str | None,
    ) -> bool:
        return (
            selection is not None
            and file_path is not None
            and item.file_path == file_path
            and item.hunk_index is None
            and item.file_path == selection.file_path
            and (
                (selection.change_area is not None and item.change_area == selection.change_area)
                or (selection.patch_name is not None and item.patch_name == selection.patch_name)
            )
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
            key = cls._tree_item_key(item)
            if fallback_expanded is not None and cls._matches_expansion_fallback(
                item,
                selection,
                fallback_path,
            ):
                expansion_states.append((node, fallback_expanded))
            elif key in expanded_nodes:
                expansion_states.append((node, expanded_nodes[key]))
        return expansion_states

    @classmethod
    def _find_selection_node(
        cls,
        nodes: list[TreeNode[TreeItem]],
        selection: TreeItem | None,
    ) -> TreeNode[TreeItem] | None:
        if selection is None:
            return None
        selection_key = cls._tree_item_key(selection)
        selected_node = next(
            (
                node
                for node in nodes
                if node.data is not None and cls._tree_item_key(node.data) == selection_key
            ),
            None,
        )
        if selected_node is None and selection.hunk_index is not None:
            return cls._find_file_node(nodes, selection)
        return selected_node

    @staticmethod
    def _find_file_node(
        nodes: list[TreeNode[TreeItem]],
        selection: TreeItem,
    ) -> TreeNode[TreeItem] | None:
        return next(
            (
                node
                for node in nodes
                if node.data is not None
                and node.data.file_path == selection.file_path
                and node.data.change_area == selection.change_area
                and node.data.patch_name == selection.patch_name
                and node.data.hunk_index is None
            ),
            None,
        )

    def _restore_tree_state(
        self,
        tree: LayerTree,
        node: TreeNode[TreeItem],
        expansion_states: list[tuple[TreeNode[TreeItem], bool]],
    ) -> None:
        for expanded_node, is_expanded in expansion_states:
            if is_expanded:
                expanded_node.expand()
            else:
                expanded_node.collapse()
        self.call_after_refresh(self._restore_tree_selection, tree, node)

    def _restore_tree_selection(self, tree: LayerTree, node: TreeNode[TreeItem]) -> None:
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.data is not None and ancestor.data.directory_path is not None:
                ancestor.expand()
            ancestor = ancestor.parent
        tree.move_cursor(node, animate=False)
        self._show_tree_item(node.data)

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

        StgLayersApp._add_grouped_files(group, files, None, change_area)

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

        StgLayersApp._add_grouped_files(patch_node, patch.files, patch.name, None)

    @staticmethod
    def _add_grouped_files(
        parent: TreeNode[TreeItem],
        files: tuple[FileChange, ...],
        patch_name: str | None,
        change_area: str | None,
    ) -> None:
        root = _FileDirectory()
        for file in files:
            directory = root
            for component in file.path.split("/")[:-1]:
                directory = directory.directories.setdefault(component, _FileDirectory())
            directory.files.append(file)
        StgLayersApp._add_directory_contents(parent, root, "", patch_name, change_area)

    @staticmethod
    def _add_directory_contents(
        parent: TreeNode[TreeItem],
        directory: _FileDirectory,
        prefix: str,
        patch_name: str | None,
        change_area: str | None,
    ) -> None:
        for file in directory.files:
            StgLayersApp._add_file(parent, file, patch_name, change_area)
        for name, child in sorted(directory.directories.items()):
            while not child.files and len(child.directories) == 1:
                component, child = next(iter(child.directories.items()))
                name = f"{name}/{component}"
            path = f"{prefix}/{name}" if prefix else name
            node = parent.add(
                Text(name, style="white"),
                data=TreeItem(
                    title=f"{patch_name or change_area} / {path}/",
                    diff_text="",
                    patch_name=patch_name,
                    change_area=change_area,
                    directory_path=path,
                ),
                expand=True,
            )
            StgLayersApp._add_directory_contents(node, child, path, patch_name, change_area)

    @staticmethod
    def _add_file(
        parent: TreeNode[TreeItem],
        file: FileChange,
        patch_name: str | None,
        change_area: str | None,
    ) -> None:
        display_path = file.path.rsplit("/", 1)[-1]
        full_display_path = file.path
        if file.status == FileStatus.RENAMED:
            display_path = f"{file.source_path} -> {file.target_path}"
            full_display_path = display_path
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
                title=f"{section} / {full_display_path}",
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
                    title=f"{section} / {full_display_path} / hunk {hunk.index + 1}",
                    diff_text=hunk.diff_text,
                    patch_name=patch_name,
                    file_path=file.path,
                    hunk_index=hunk.index,
                    hunk_key=StgLayersApp._hunk_key(hunk.diff_text),
                    change_area=change_area,
                ),
                allow_expand=False,
            )
