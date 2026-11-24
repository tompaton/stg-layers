import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Protocol

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static, TextArea, Tree
from textual.widgets._tree import TreeNode

from stg_layers.change_moves import ChangeSelection, MoveTarget
from stg_layers.dialogs import (
    ConflictDialog,
    DeletePatchDialog,
    DiscardChangesDialog,
    MoveTargetDialog,
    PatchMessageDialog,
    ViewSettingsDialog,
)
from stg_layers.diff_preview import DiffPreview
from stg_layers.errors import RepositoryError
from stg_layers.models import FileChange, Patch, PatchStatus, TreeItem
from stg_layers.partial_diff import HunkLineSelection
from stg_layers.repository import PatchChangeMoveResult, PatchMoveResult
from stg_layers.settings import ViewSettings
from stg_layers.tree_view import LayerTree
from stg_layers.tree_walker import next_change_selection, walk_nodes


class MoveNotPerformed(Exception):
    """Stop a move and display its status without reloading the tree."""


class PatchChangeMoveError(Exception):
    def __init__(self, result: PatchChangeMoveResult, patch_name: str) -> None:
        self.result = result
        self.patch_name = patch_name
        super().__init__(result)


class StackReader(Protocol):
    root: Path
    branch: str | None
    remote_url: str | None

    def refresh_identity(self) -> None: ...

    def has_conflicts(self) -> bool: ...

    def load_stack(self) -> tuple[Patch, ...]: ...

    def load_staged_changes(self) -> tuple[FileChange, ...]: ...

    def load_unstaged_changes(self) -> tuple[FileChange, ...]: ...

    def stage_files(self, paths: tuple[str, ...]) -> None: ...

    def unstage_files(self, paths: tuple[str, ...]) -> None: ...

    def refresh_staged_files_to_current_patch(self, paths: tuple[str, ...]) -> None: ...

    def spill_current_patch_files(self, paths: tuple[str, ...]) -> None: ...

    def move_patch_lines(
        self, patch_name: str, selection: HunkLineSelection, *, toward_newer: bool
    ) -> tuple[PatchChangeMoveResult, str | None]: ...

    def move_patch_changes(
        self, patch_name: str, paths: tuple[str, ...], *, toward_newer: bool
    ) -> tuple[PatchChangeMoveResult, str | None]: ...

    def refresh_staged_hunk_to_current_patch(self, path: str, hunk_diff: str) -> None: ...

    def refresh_staged_lines_to_current_patch(self, selection: HunkLineSelection) -> None: ...

    def move_current_patch_lines_to_staged(self, selection: HunkLineSelection) -> None: ...

    def spill_current_patch_hunk(self, path: str, hunk_diff: str) -> None: ...

    def patch_diffstat(self, patch_name: str) -> str: ...

    def unstaged_diffstat(self, paths: tuple[str, ...], *, hunk_diff: str | None = None) -> str: ...

    def discarded_lines_diffstat(
        self, selection: HunkLineSelection, *, staged: bool = False, patch_name: str | None = None
    ) -> str: ...

    def discard_files(self, paths: tuple[str, ...]) -> None: ...

    def discard_hunk(self, path: str, hunk_diff: str) -> None: ...

    def move_selected_lines(self, selection: HunkLineSelection, *, stage: bool) -> None: ...

    def discard_selected_lines(
        self, selection: HunkLineSelection, *, staged: bool = False, patch_name: str | None = None
    ) -> None: ...

    def stage_hunk(self, path: str, hunk_index: int) -> None: ...

    def unstage_hunk(self, path: str, hunk_index: int) -> None: ...

    def new_patch_template(self) -> str: ...

    def create_patch(self, message: str) -> None: ...

    def edit_patch_template(self, patch_name: str) -> str: ...

    def edit_patch(self, patch_name: str, message: str) -> None: ...

    def move_to_target(
        self, selection: ChangeSelection, target: MoveTarget, *, message: str | None = None
    ) -> None: ...

    def squash_message_template(self, source: str, target: str) -> str: ...

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

    def patch_operation(self, operation: str, patch_name: str | None = None) -> None: ...

    def undo_stack_operation(self) -> None: ...

    def refresh_stack_operation(self) -> str | None: ...

    @property
    def stack_operation_pending(self) -> bool: ...

    @property
    def stack_operation_stash_reference(self) -> str | None: ...

    @property
    def stash_restore_conflict(self) -> bool: ...


class StgLayersApp(App[None]):
    TITLE = "StGit Layers"
    AUTO_REFRESH_INTERVAL = 2.0
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
        Binding("v", "view_settings", "View Settings"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, repository: StackReader, *, config_path: Path | None = None) -> None:
        super().__init__()
        self.repository = repository
        self.sub_title = repository.remote_url or ""
        self._config_path = config_path
        self._view_settings = ViewSettings.load(config_path) if config_path else ViewSettings()
        self._conflict_locked = False
        self._repository_busy = 0
        self._auto_refresh_done = asyncio.Event()
        self._auto_refresh_done.set()
        self._shown_item: TreeItem | None = None
        self._loaded_identity: tuple[str | None, str | None] | None = None
        self._loaded_data: (
            tuple[
                tuple[Patch, ...],
                tuple[FileChange, ...],
                tuple[FileChange, ...],
            ]
            | None
        ) = None

    @property
    def newest_first(self) -> bool:
        return self._view_settings.newest_first

    def _require_loaded_patches(self) -> tuple[Patch, ...]:
        """Return the loaded stack, including an empty one, or report missing data."""
        if self._loaded_data is None:
            raise RepositoryError("The patch stack has not been loaded.")
        return self._loaded_data[0]

    def _get_patch(self, name: str | None) -> Patch | None:
        """Find a patch in the current snapshot, or return None if unavailable."""
        if name is None or self._loaded_data is None:
            return None
        return next((patch for patch in self._require_loaded_patches() if patch.name == name), None)

    @property
    def _current_patch(self) -> Patch | None:
        if self._loaded_data is None:
            return None
        return next(
            (
                patch
                for patch in self._require_loaded_patches()
                if patch.status == PatchStatus.CURRENT
            ),
            None,
        )

    def _is_current(self, item: TreeItem) -> bool:
        """Return True if the given item is part of the current patch."""
        current = self._current_patch
        return current is not None and item.patch_name == current.name

    @staticmethod
    def _selected_paths(node: TreeNode[TreeItem]) -> tuple[str, ...]:
        return tuple(
            child.data.require_file_path()
            for child in walk_nodes(node)
            if child.data is not None and child.data.is_file
        )

    def _get_selected_patch(self) -> Patch | None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        return self._get_patch(item.patch_name) if item is not None and item.is_patch else None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="tree-pane"):
                tree = LayerTree(self._repository_label(), id="patch-tree")
                tree.root.expand()
                yield tree
            with Vertical(id="preview-pane"):
                yield Static("Select a patch, file, or hunk.", id="selection-title", markup=False)
                yield DiffPreview(
                    id="diff-view",
                    soft_wrap=self._view_settings.word_wrap,
                    show_line_numbers=self._view_settings.show_diff_line_numbers,
                )
        yield Static("Loading StGit stack...", id="status", markup=False)
        yield Footer()

    def _repository_label(self) -> Text:
        root = self.repository.root
        home = Path.home()
        directory = f"~/{root.relative_to(home)}" if root.is_relative_to(home) else str(root)
        if root == home:
            directory = "~"
        branch = self.repository.branch or "detached HEAD"
        return Text(f"Repository {directory} [{branch}]", style="bold")

    async def on_mount(self) -> None:
        await self._load_stack()
        self.set_interval(self.AUTO_REFRESH_INTERVAL, self._refresh_changed_repository)

    async def _refresh_changed_repository(self) -> None:
        if (
            self._repository_busy
            or self.workers
            or len(self.screen_stack) > 1
            or self._conflict_locked
            or self.repository.stack_operation_pending
        ):
            return
        self._auto_refresh_done.clear()
        try:
            await self._load_stack(only_if_changed=True)
        finally:
            self._auto_refresh_done.set()

    @on(Tree.NodeSelected)
    def on_tree_node_selected(self, event: Tree.NodeSelected[TreeItem]) -> None:
        self._show_tree_item(event.node.data)

    @on(Tree.NodeHighlighted)
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[TreeItem]) -> None:
        self._show_tree_item(event.node.data)

    def _show_tree_item(self, item: TreeItem | None) -> None:
        if item is None or item == self._shown_item:
            return
        same_selection = (
            self._shown_item is not None and item.selection_key == self._shown_item.selection_key
        )
        self._shown_item = item
        self.query_one("#selection-title", Static).update(item.title)
        preview = self.query_one("#diff-view", TextArea)
        scroll = preview.scroll_offset
        selection = preview.selection
        preview.load_text(item.diff_text or "No text diff available.")
        if same_selection:
            preview.selection = selection
            preview.scroll_to(scroll.x, scroll.y, animate=False, immediate=True)
        else:
            preview.scroll_home(animate=False, immediate=True)

    def _show_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    async def _load_stack(
        self,
        preferred_selection: TreeItem | None = None,
        expansion_fallback_path: str | None = None,
        *,
        only_if_changed: bool = False,
    ) -> None:
        async def on_error(error: RepositoryError) -> None:
            self._loaded_data = None
            if not only_if_changed:
                await self._show_error(error)

        async with self._report_repository_errors(on_error=on_error):
            if await asyncio.to_thread(self.repository.has_conflicts):
                self._conflict_locked = True
                self._show_status("Merge conflicts detected; repository operations are locked.")
                await self.push_screen(
                    ConflictDialog(operation_failed=False),
                    self._on_repository_conflict_resolution,
                )
                return
            self._conflict_locked = False
            if not only_if_changed:
                self._show_status("Loading patch stack and working changes...")
            patches, staged, unstaged, _ = await asyncio.gather(
                asyncio.to_thread(self.repository.load_stack),
                asyncio.to_thread(self.repository.load_staged_changes),
                asyncio.to_thread(self.repository.load_unstaged_changes),
                asyncio.to_thread(self.repository.refresh_identity),
            )
            data = patches, staged, unstaged
            identity = self.repository.branch, self.repository.remote_url
            if only_if_changed and data == self._loaded_data and identity == self._loaded_identity:
                return
            self._loaded_data = data
            self._loaded_identity = identity
            self.sub_title = self.repository.remote_url or ""
            self.query_one("#patch-tree", LayerTree).root.set_label(self._repository_label())
            self.query_one("#patch-tree", LayerTree).populate(
                patches,
                staged,
                unstaged,
                newest_first=self.newest_first,
                omitted_patch_statuses=self._view_settings.omitted_patch_statuses,
                preferred_selection=preferred_selection,
                expansion_fallback_path=expansion_fallback_path,
            )
            count = len(patches)
            order = "newest first" if self.newest_first else "oldest first"
            self._show_status(
                f"Loaded {count} patch{'es' if count != 1 else ''} ({order}). "
                "Press v for View Settings or r to refresh."
            )

    async def _show_error(self, _error: RepositoryError) -> None:
        tree = self.query_one("#patch-tree", Tree)
        tree.root.remove_children()
        tree.root.add("Unable to load StGit stack", allow_expand=False)

    async def _ensure_repository_ready(self) -> bool:
        await self._auto_refresh_done.wait()
        if self._conflict_locked:
            return False
        async with self._report_repository_errors():
            conflicted = await asyncio.to_thread(self.repository.has_conflicts)
            if conflicted:
                self._conflict_locked = True
                await self.push_screen(
                    ConflictDialog(operation_failed=False),
                    self._on_repository_conflict_resolution,
                )
                return False
            self._conflict_locked = False
            return True
        return False

    def _on_repository_conflict_resolution(self, resolution: str | None) -> None:
        if resolution != "refresh":
            self.exit()
            return
        self._conflict_locked = False
        self.run_worker(self._load_stack(), exclusive=True)

    @asynccontextmanager
    async def _report_repository_errors(
        self,
        *,
        on_error: Callable[[RepositoryError], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[None]:
        """Report repository failures and optionally run recovery or cleanup."""
        self._repository_busy += 1
        try:
            yield
        except RepositoryError as error:
            self._show_status(f"Error: {error}")
            if on_error is not None:
                await on_error(error)
        finally:
            self._repository_busy -= 1

    def _handle_repository_conflict_error(self) -> AbstractAsyncContextManager[None]:
        async def handle_error(error: RepositoryError) -> None:
            try:
                conflicted = await asyncio.to_thread(self.repository.has_conflicts)
            except RepositoryError as conflict_check_error:
                self._show_status(
                    f"Error: {error}; could not check for conflicts: {conflict_check_error}"
                )
                conflicted = False
            if self.repository.stack_operation_pending:
                await self._resolve_stack_operation_conflict()
            elif conflicted:
                self._conflict_locked = True
                await self.push_screen(
                    ConflictDialog(operation_failed=False),
                    self._on_repository_conflict_resolution,
                )

        return self._report_repository_errors(on_error=handle_error)

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
        async with self._handle_repository_conflict_error():
            retained_stash: str | None = None
            if resolution == "undo":
                await asyncio.to_thread(self.repository.undo_stack_operation)
            else:
                retained_stash = await asyncio.to_thread(self.repository.refresh_stack_operation)
            self._conflict_locked = False
            await self._load_stack()
            if retained_stash is not None:
                self._show_status(
                    "Repository refreshed. Local changes were manually resolved; recovery "
                    f"stash {retained_stash} was retained."
                )

    def action_view_settings(self) -> None:
        self.push_screen(ViewSettingsDialog(self._view_settings), self._apply_view_settings)

    def _apply_view_settings(self, settings: ViewSettings | None) -> None:
        if settings is None:
            return
        previous = self._view_settings
        self._view_settings = settings
        preview = self.query_one("#diff-view", DiffPreview)
        preview.soft_wrap = settings.word_wrap
        preview.show_line_numbers = settings.show_diff_line_numbers
        if self._loaded_data is not None and (
            settings.newest_first != previous.newest_first
            or settings.omitted_patch_statuses != previous.omitted_patch_statuses
        ):
            self.query_one("#patch-tree", LayerTree).populate(
                *self._loaded_data,
                newest_first=self.newest_first,
                omitted_patch_statuses=settings.omitted_patch_statuses,
            )
            order = "newest first" if self.newest_first else "oldest first"
            self._show_status(f"Showing {order}.")
        if self._config_path is not None:
            try:
                settings.save(self._config_path)
            except OSError as error:
                self._show_status(f"Could not save View Settings: {error}")

    async def action_create_patch(self) -> None:
        if not await self._ensure_repository_ready():
            return
        self._show_status("Preparing a new patch...")
        async with self._handle_repository_conflict_error():
            template = await asyncio.to_thread(self.repository.new_patch_template)

            await self.push_screen(PatchMessageDialog(template), self._on_patch_message)

    async def action_edit_selected_patch(self) -> None:
        if not await self._ensure_repository_ready():
            return

        patch = self._get_selected_patch()
        if patch is None:
            return

        patch_name = patch.name
        self._show_status(f"Preparing to edit patch {patch_name}...")
        async with self._handle_repository_conflict_error():
            template = await asyncio.to_thread(self.repository.edit_patch_template, patch_name)

            await self.push_screen(
                PatchMessageDialog(template, submit_label="Update"),
                lambda message: self._on_edited_patch_message(patch_name, message),
            )

    def _on_edited_patch_message(self, patch_name: str, message: str | None) -> None:
        if message is None:
            self._show_status(f"Editing patch {patch_name} cancelled.")
            return
        self.run_worker(self._edit_patch(patch_name, message), exclusive=True)

    async def _edit_patch(self, patch_name: str, message: str) -> None:
        if not await self._ensure_repository_ready():
            return
        self._show_status(f"Updating patch {patch_name}...")
        async with self._handle_repository_conflict_error():
            await asyncio.to_thread(self.repository.edit_patch, patch_name, message)
            await self._load_stack()

    def _on_patch_message(self, message: str | None) -> None:
        if message is None:
            self._show_status("Patch creation cancelled.")
            return
        self.run_worker(self._create_patch(message), exclusive=True)

    async def _create_patch(self, message: str) -> None:
        if not await self._ensure_repository_ready():
            return
        self._show_status("Creating patch...")
        async with self._handle_repository_conflict_error():
            await asyncio.to_thread(self.repository.create_patch, message)
            await self._load_stack()

    def action_refresh_stack(self) -> None:
        self.run_worker(self._load_stack(), exclusive=True)

    async def action_delete_selected(self) -> None:
        node = self.query_one("#patch-tree", LayerTree).cursor_node
        item = node.data if node is not None else None
        if node is None or item is None or not item.is_unstaged:
            await self.action_patch_operation("delete")
            return
        if not await self._ensure_repository_ready():
            return
        async with self._handle_repository_conflict_error(), self._handle_move_status():
            paths = self._move_paths(item, node)
            selection = item.as_group_selection()
            if item.is_file_or_hunk:
                selection = next_change_selection(node, item) or selection

            def confirmed(result: bool | None) -> None:
                if result:
                    self.run_worker(self._discard_changes(item, paths, selection), exclusive=True)

            diffstat = await asyncio.to_thread(
                self.repository.unstaged_diffstat,
                paths,
                hunk_diff=item.diff_text if item.is_hunk else None,
            )
            self.push_screen(DiscardChangesDialog(item.title, diffstat=diffstat), confirmed)

    async def _discard_changes(
        self, item: TreeItem, paths: tuple[str, ...], selection: TreeItem
    ) -> None:
        if not await self._ensure_repository_ready():
            return
        async with self._handle_repository_conflict_error():
            self._show_status(f"Discarding unstaged changes in {item.title}...")
            if item.is_hunk:
                await asyncio.to_thread(
                    self.repository.discard_hunk, item.require_file_path(), item.diff_text
                )
            else:
                await asyncio.to_thread(self.repository.discard_files, paths)
            await self._load_stack(preferred_selection=selection)

    async def action_patch_operation(self, operation: str) -> None:
        if not await self._ensure_repository_ready():
            return

        if operation == "uncommit":
            await self._run_patch_operation(operation, None)
            return

        patch = self._get_selected_patch()
        if patch is None:
            self._show_status(f"Select a patch to {operation}.")
            return

        if operation == "push" and patch.status != PatchStatus.UNAPPLIED:
            self._show_status("Select an unapplied patch to push.")
            return

        if operation == "pop" and patch.status not in {PatchStatus.APPLIED, PatchStatus.CURRENT}:
            self._show_status("Select an applied patch to pop.")
            return

        patch_name = patch.name

        def delete_callback(result: bool | None) -> None:
            if result:
                self.run_worker(self._run_patch_operation(operation, patch_name), exclusive=True)

        if operation == "delete":
            async with self._handle_repository_conflict_error():
                diffstat = await asyncio.to_thread(self.repository.patch_diffstat, patch_name)
                self.push_screen(DeletePatchDialog(patch_name, diffstat=diffstat), delete_callback)
        else:
            await self._run_patch_operation(operation, patch_name)

    async def _run_patch_operation(self, operation: str, patch_name: str | None = None) -> None:
        target = f" on patch {patch_name}" if patch_name is not None else ""
        self._show_status(f"Running {operation}{target}...")
        async with self._handle_repository_conflict_error():
            await asyncio.to_thread(self.repository.patch_operation, operation, patch_name)
            await self._load_stack(
                preferred_selection=(
                    TreeItem.for_patch_selection(patch_name) if patch_name is not None else None
                )
            )

    async def action_move_change_down(self) -> None:
        await self._move_change(toward_newer=not self.newest_first)

    async def action_move_change_up(self) -> None:
        await self._move_change(toward_newer=self.newest_first)

    async def action_stage_selected_lines(self) -> None:
        await self._move_change(toward_newer=False, lines_only=True)

    async def action_remove_selected_lines(self) -> None:
        await self._move_change(toward_newer=True, lines_only=True)

    async def action_apply_preview_change(self) -> None:
        await self.action_apply_selected_change(allow_groups=False)

    async def action_apply_selected_change(self, *, allow_groups: bool = True) -> None:
        node = self.query_one("#patch-tree", LayerTree).cursor_node
        item = node.data if node is not None else None
        if node is None or item is None:
            return
        if not allow_groups and not item.is_file_or_hunk:
            return
        if item.is_staged or item.is_unstaged:
            await self._move_change(toward_newer=item.is_staged, advance_selection=True)

    async def _move_change(
        self,
        *,
        toward_newer: bool,
        advance_selection: bool = False,
        lines_only: bool = False,
    ) -> None:
        node = self.query_one("#patch-tree", LayerTree).cursor_node
        if node is None:
            return
        item = self._shown_item if lines_only else node.data
        if item is None:
            return
        if lines_only and not (item.is_working_change or item.is_patch or item.is_patch_change):
            return
        async with self._handle_repository_conflict_error(), self._handle_move_status():
            line_selection = (
                self.query_one("#diff-view", DiffPreview).selected_lines(item)
                if lines_only
                else None
            )
            if not await self._ensure_repository_ready():
                return
            selection = await self._dispatch_move(
                item, node, toward_newer=toward_newer, line_selection=line_selection
            )
            if advance_selection:
                if item.is_file_group:
                    selection = item.as_group_selection()
                else:
                    selection = next_change_selection(node, item) or selection
            path = item.file_path
            await self._load_stack(
                preferred_selection=selection,
                expansion_fallback_path=path if selection.file_path == path else None,
            )

    @asynccontextmanager
    async def _handle_move_status(self) -> AsyncGenerator[None]:
        try:
            yield
        except MoveNotPerformed as error:
            if str(error):
                self._show_status(str(error))
        except PatchChangeMoveError as error:
            if error.result == PatchChangeMoveResult.STACK_EDGE:
                message = f"Patch {error.patch_name} is at the edge of the stack."
            elif error.result == PatchChangeMoveResult.APPLICATION_BOUNDARY:
                message = "Changes can only be moved between adjacent applied patches."
            else:
                message = "Changes can only be moved from applied patches."
            self._show_status(message)

    async def _dispatch_move(
        self,
        item: TreeItem,
        node: TreeNode[TreeItem],
        *,
        toward_newer: bool,
        line_selection: HunkLineSelection | None = None,
    ) -> TreeItem:
        if item.is_patch and line_selection is None:
            return await self._reorder_selected_patch(item, toward_newer=toward_newer)
        elif item.is_unstaged:
            if toward_newer:
                if line_selection is not None:
                    await self._confirm_discard_selected_lines(item, line_selection)
                    raise MoveNotPerformed()
                self._move_paths(item, node)
                raise MoveNotPerformed(
                    "Unstaged changes are at the edge." if item.is_file_group else ""
                )
            return await self._move_changes_between_areas(item, node, line_selection)
        elif item.is_staged:
            if toward_newer:
                return await self._move_changes_between_areas(item, node, line_selection)
            else:
                return await self._move_staged_changes_to_current_patch(item, node, line_selection)
        elif item.patch_name is not None:
            if self._is_current(item) and toward_newer:
                return await self._spill_current_patch_changes(item, node, line_selection)
            else:
                return await self._move_patch_changes(
                    item, node, line_selection, toward_newer=toward_newer
                )

        raise MoveNotPerformed()

    def _move_paths(self, item: TreeItem, node: TreeNode[TreeItem]) -> tuple[str, ...]:
        paths = self._selected_paths(node) if item.is_file_group else (item.require_file_path(),)
        if not paths:
            raise MoveNotPerformed("No files to move in this group.")
        return paths

    async def _reorder_selected_patch(self, item: TreeItem, *, toward_newer: bool) -> TreeItem:
        patch_name = item.require_patch_name()

        direction = "toward newer patches" if toward_newer else "toward older patches"
        self._show_status(f"Moving patch {patch_name} one position {direction}...")
        result = await asyncio.to_thread(
            self.repository.move_patch_one_step,
            patch_name,
            toward_newer=toward_newer,
        )

        if result == PatchMoveResult.STACK_EDGE:
            position = "top" if toward_newer else "bottom"
            raise MoveNotPerformed(f"Patch {patch_name} is already at the {position} of the stack.")
        elif result == PatchMoveResult.APPLICATION_BOUNDARY:
            raise MoveNotPerformed(
                "Patches cannot be reordered across the applied/unapplied boundary."
            )
        return item.as_selection()

    async def _move_patch_changes(
        self,
        item: TreeItem,
        node: TreeNode[TreeItem],
        line_selection: HunkLineSelection | None,
        *,
        toward_newer: bool,
    ) -> TreeItem:
        source_patch = self._get_patch(item.patch_name)
        if source_patch is None:
            raise MoveNotPerformed()
        patch_name = source_patch.name
        direction = "newer" if toward_newer else "older"
        self._show_status(f"Moving {item.title} {direction} from patch {patch_name}...")
        if line_selection is not None:
            result, moved_to = await asyncio.to_thread(
                self.repository.move_patch_lines,
                patch_name,
                line_selection,
                toward_newer=toward_newer,
            )
        elif item.is_hunk:
            result, moved_to = await asyncio.to_thread(
                self.repository.move_patch_change,
                patch_name,
                item.require_file_path(),
                toward_newer=toward_newer,
                hunk_diff=item.diff_text,
            )
        else:
            result, moved_to = await asyncio.to_thread(
                self.repository.move_patch_changes,
                patch_name,
                self._move_paths(item, node),
                toward_newer=toward_newer,
            )
        if result != PatchChangeMoveResult.MOVED:
            raise PatchChangeMoveError(result, patch_name)
        if moved_to is None:
            raise MoveNotPerformed(f"Could not determine the destination patch for {item.title}.")
        return (
            item.as_selection()
            if line_selection is not None
            else item.as_selection(patch_name=moved_to)
        )

    async def _move_changes_between_areas(
        self,
        item: TreeItem,
        node: TreeNode[TreeItem],
        line_selection: HunkLineSelection | None,
    ) -> TreeItem:
        stage = item.is_unstaged
        if line_selection is not None:
            await asyncio.to_thread(
                self.repository.move_selected_lines, line_selection, stage=stage
            )
            return item.as_selection()
        target_area = "staged" if stage else "unstaged"
        paths = self._move_paths(item, node)

        verb = "Staging" if stage else "Unstaging"
        target = "hunk" if item.is_hunk else "files"
        self._show_status(f"{verb} {target} in {item.title}...")
        if item.hunk_index is None:
            operation = self.repository.stage_files if stage else self.repository.unstage_files
            await asyncio.to_thread(operation, paths)
        else:
            operation = self.repository.stage_hunk if stage else self.repository.unstage_hunk
            await asyncio.to_thread(operation, item.require_file_path(), item.hunk_index)

        preferred_selection = item.as_selection(change_area=target_area)
        return preferred_selection

    async def _move_staged_changes_to_current_patch(
        self,
        item: TreeItem,
        node: TreeNode[TreeItem],
        line_selection: HunkLineSelection | None,
    ) -> TreeItem:
        paths = self._move_paths(item, node)
        current_patch = self._current_patch
        if current_patch is None:
            raise MoveNotPerformed("There is no current patch to refresh.")
        self._show_status(f"Moving {item.title} into the current patch...")
        if line_selection is not None:
            await asyncio.to_thread(
                self.repository.refresh_staged_lines_to_current_patch, line_selection
            )
            return item.as_selection()
        if item.is_hunk:
            await asyncio.to_thread(
                self.repository.refresh_staged_hunk_to_current_patch,
                item.require_file_path(),
                item.diff_text,
            )
        else:
            await asyncio.to_thread(self.repository.refresh_staged_files_to_current_patch, paths)
        return item.as_selection(patch_name=current_patch.name)

    async def _spill_current_patch_changes(
        self,
        item: TreeItem,
        node: TreeNode[TreeItem],
        line_selection: HunkLineSelection | None,
    ) -> TreeItem:
        if line_selection is not None:
            self._show_status(f"Moving selected lines in {item.title} into staged changes...")
            await asyncio.to_thread(
                self.repository.move_current_patch_lines_to_staged, line_selection
            )
            return item.as_selection()
        paths = self._move_paths(item, node)
        self._show_status(f"Moving {item.title} out of the current patch...")
        if item.is_hunk:
            await asyncio.to_thread(
                self.repository.spill_current_patch_hunk, item.require_file_path(), item.diff_text
            )
        else:
            await asyncio.to_thread(self.repository.spill_current_patch_files, paths)
        return item.as_selection(change_area="staged")

    async def action_choose_move_target(self, preview: bool = False) -> None:
        node = self.query_one("#patch-tree", LayerTree).cursor_node
        item = self._shown_item if preview else node.data if node else None
        if item is None or node is None:
            return
        if not (item.change_area or item.patch_name):
            return
        async with self._handle_repository_conflict_error(), self._handle_move_status():
            lines = (
                self.query_one("#diff-view", DiffPreview).selected_lines(item) if preview else None
            )
            paths = (lines.path,) if lines else self._target_move_paths(item, node)
            selection = ChangeSelection(
                MoveTarget(change_area=item.change_area, patch_name=item.patch_name),
                paths,
                hunk_diff=item.diff_text if item.is_hunk and not preview else None,
                lines=lines,
                whole_patch=item.is_patch and not preview,
            )
            if not await self._ensure_repository_ready():
                return
            patches = self._require_loaded_patches()
            if self.newest_first:
                patches = tuple(reversed(patches))
            targets = (
                MoveTarget(change_area="unstaged"),
                MoveTarget(change_area="staged"),
            ) + tuple(MoveTarget(patch_name=patch.name) for patch in patches)
            self.push_screen(
                MoveTargetDialog(
                    f"selected lines of {lines.path}" if lines else item.title,
                    tuple(target for target in targets if target != selection.source),
                    patches=patches,
                ),
                lambda target: self._on_move_target(selection, target),
            )

    def _target_move_paths(self, item: TreeItem, node: TreeNode[TreeItem]) -> tuple[str, ...]:
        if item.is_patch:
            patch = self._get_patch(item.patch_name)
            return tuple(file.path for file in patch.files) if patch else ()
        return self._move_paths(item, node)

    def _on_move_target(self, selection: ChangeSelection, target: MoveTarget | None) -> None:
        if target is not None:
            self.run_worker(self._prepare_target_move(selection, target), exclusive=True)

    async def _prepare_target_move(self, selection: ChangeSelection, target: MoveTarget) -> None:
        if not selection.whole_patch or target.patch_name is None:
            await self._move_to_target(selection, target)
            return
        if not await self._ensure_repository_ready():
            return
        async with self._handle_repository_conflict_error():
            template = await asyncio.to_thread(
                self.repository.squash_message_template,
                selection.source.patch_name or "",
                target.patch_name,
            )
            self.push_screen(
                PatchMessageDialog(template, submit_label="Update"),
                lambda message: self._on_squash_message(selection, target, message),
            )

    def _on_squash_message(
        self, selection: ChangeSelection, target: MoveTarget, message: str | None
    ) -> None:
        if message is not None:
            self.run_worker(
                self._move_to_target(selection, target, message=message), exclusive=True
            )

    async def _move_to_target(
        self, selection: ChangeSelection, target: MoveTarget, *, message: str | None = None
    ) -> None:
        if not await self._ensure_repository_ready():
            return
        async with self._handle_repository_conflict_error():
            self._show_status(f"Moving changes to {target.label}...")
            await asyncio.to_thread(
                self.repository.move_to_target, selection, target, message=message
            )
            preferred = (
                TreeItem.for_patch_selection(target.patch_name)
                if target.patch_name is not None
                else TreeItem.for_change_group(target.change_area or "unstaged")[0].as_selection()
            )
            await self._load_stack(preferred_selection=preferred)

    async def action_discard_preview_lines(self) -> None:
        item = self._shown_item
        if item is None or not (item.is_working_change or item.is_patch or item.is_patch_change):
            return
        async with self._handle_repository_conflict_error():
            selection = self.query_one("#diff-view", DiffPreview).selected_lines(item)
            if await self._ensure_repository_ready():
                await self._confirm_discard_selected_lines(item, selection)

    async def _confirm_discard_selected_lines(
        self, item: TreeItem, selection: HunkLineSelection
    ) -> None:
        def confirmed(result: bool | None) -> None:
            if result:
                self.run_worker(self._discard_selected_lines(item, selection), exclusive=True)

        diffstat = await asyncio.to_thread(
            self.repository.discarded_lines_diffstat,
            selection,
            staged=item.is_staged,
            patch_name=item.patch_name,
        )
        self.push_screen(
            DiscardChangesDialog(
                f"selected lines of {selection.path}",
                diffstat=diffstat,
                source=f"patch {item.patch_name}" if item.patch_name else item.change_area,
            ),
            confirmed,
        )

    async def _discard_selected_lines(self, item: TreeItem, selection: HunkLineSelection) -> None:
        if not await self._ensure_repository_ready():
            return
        async with self._handle_repository_conflict_error():
            self._show_status(f"Discarding selected lines in {selection.path}...")
            await asyncio.to_thread(
                self.repository.discard_selected_lines,
                selection,
                staged=item.is_staged,
                patch_name=item.patch_name,
            )
            await self._load_stack(
                preferred_selection=item.as_selection(), expansion_fallback_path=selection.path
            )
