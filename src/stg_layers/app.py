import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static, TextArea, Tree

from stg_layers.dialogs import ConflictDialog, DeletePatchDialog, PatchMessageDialog
from stg_layers.diff_preview import DiffPreview
from stg_layers.errors import RepositoryError
from stg_layers.models import FileChange, Patch, PatchStatus, TreeItem
from stg_layers.repository import PatchChangeMoveResult, PatchMoveResult
from stg_layers.tree_view import LayerTree
from stg_layers.tree_walker import next_change_selection, walk_nodes


class StackReader(Protocol):
    branch: str | None

    def has_conflicts(self) -> bool: ...

    def load_stack(self) -> tuple[Patch, ...]: ...

    def load_staged_changes(self) -> tuple[FileChange, ...]: ...

    def load_unstaged_changes(self) -> tuple[FileChange, ...]: ...

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
                yield DiffPreview(id="diff-view")
        yield Static("Loading StGit stack...", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_stack()

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

    def _show_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    async def _load_stack(
        self,
        preferred_selection: TreeItem | None = None,
        expansion_fallback_path: str | None = None,
    ) -> None:
        async with self._report_repository_errors(on_error=self._show_error):
            if await asyncio.to_thread(self.repository.has_conflicts):
                self._conflict_locked = True
                self._show_status("Merge conflicts detected; repository operations are locked.")
                await self.push_screen(
                    ConflictDialog(operation_failed=False),
                    self._on_repository_conflict_resolution,
                )
                return
            self._conflict_locked = False
            self._show_status("Loading patch stack and working changes...")
            patches, staged, unstaged = await asyncio.gather(
                asyncio.to_thread(self.repository.load_stack),
                asyncio.to_thread(self.repository.load_staged_changes),
                asyncio.to_thread(self.repository.load_unstaged_changes),
            )
            self._loaded_data = patches, staged, unstaged
            self.query_one("#patch-tree", LayerTree).populate(
                patches,
                staged,
                unstaged,
                newest_first=self.newest_first,
                preferred_selection=preferred_selection,
                expansion_fallback_path=expansion_fallback_path,
            )
            count = len(patches)
            order = "newest first" if self.newest_first else "oldest first"
            self._show_status(
                f"Loaded {count} patch{'es' if count != 1 else ''} ({order}). "
                "Press R to reverse order; r to refresh."
            )

    async def _show_error(self, _error: RepositoryError) -> None:
        tree = self.query_one("#patch-tree", Tree)
        tree.root.remove_children()
        tree.root.add("Unable to load StGit stack", allow_expand=False)

    async def _ensure_repository_ready(self) -> bool:
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
        try:
            yield
        except RepositoryError as error:
            self._show_status(f"Error: {error}")
            if on_error is not None:
                await on_error(error)

    def _handle_repository_conflict_error(self) -> AbstractAsyncContextManager[None]:
        async def handle_error(error: RepositoryError) -> None:
            try:
                conflicted = await asyncio.to_thread(self.repository.has_conflicts)
            except RepositoryError as conflict_check_error:
                self._show_status(
                    f"Error: {error}; could not check for conflicts: {conflict_check_error}"
                )
                return
            if conflicted or self.repository.stack_operation_pending:
                await self._resolve_stack_operation_conflict()

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
        try:
            retained_stash: str | None = None
            if resolution == "undo":
                await asyncio.to_thread(self.repository.undo_stack_operation)
            else:
                retained_stash = await asyncio.to_thread(self.repository.refresh_stack_operation)
        except RepositoryError as error:
            self._show_status(f"Error: {error}")
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
            self._show_status(
                "Repository refreshed. Local changes were manually resolved; recovery "
                f"stash {retained_stash} was retained."
            )

    async def action_create_patch(self) -> None:
        if not await self._ensure_repository_ready():
            return
        self._show_status("Preparing a new patch...")
        async with self._report_repository_errors():
            template = await asyncio.to_thread(self.repository.new_patch_template)

            await self.push_screen(PatchMessageDialog(template), self._on_patch_message)

    async def action_edit_selected_patch(self) -> None:
        if not await self._ensure_repository_ready():
            return
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or not item.is_patch:
            return

        patch_name = item.require_patch_name()
        self._show_status(f"Preparing to edit patch {patch_name}...")
        async with self._report_repository_errors():
            template = await asyncio.to_thread(self.repository.edit_patch_template, patch_name)

            await self.push_screen(
                PatchMessageDialog(template),
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
        async with self._report_repository_errors():
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
        async with self._report_repository_errors():
            await asyncio.to_thread(self.repository.create_patch, message)
            await self._load_stack()

    def action_refresh_stack(self) -> None:
        self.run_worker(self._load_stack(), exclusive=True)

    async def action_push_selected_patch(self) -> None:
        await self._set_selected_patch_application(push=True)

    async def action_pop_selected_patch(self) -> None:
        await self._set_selected_patch_application(push=False)

    async def _set_selected_patch_application(self, *, push: bool) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or not item.is_patch or self._loaded_data is None:
            return

        patch_name = item.require_patch_name()
        patch = next(
            (patch for patch in self._loaded_data[0] if patch.name == patch_name),
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
            self._show_status(f"Select an {expected} patch to {verb}.")
            return
        await self._run_patch_operation("push" if push else "pop", patch_name)

    async def action_uncommit_patch(self) -> None:
        await self._run_patch_operation("uncommit")

    async def action_patch_operation(self, operation: str) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or not item.is_patch:
            self._show_status(f"Select a patch to {operation}.")
            return
        if not await self._ensure_repository_ready():
            return
        if operation == "delete":
            patch_name = item.require_patch_name()

            def confirmed(result: bool | None) -> None:
                if result:
                    self.run_worker(
                        self._run_patch_operation(operation, patch_name), exclusive=True
                    )

            self.push_screen(DeletePatchDialog(patch_name), confirmed)
            return
        await self._run_patch_operation(operation, item.require_patch_name())

    async def _run_patch_operation(self, operation: str, patch_name: str | None = None) -> None:
        if not await self._ensure_repository_ready():
            return
        target = f" on patch {patch_name}" if patch_name is not None else ""
        self._show_status(f"Running {operation}{target}...")
        async with self._handle_repository_conflict_error():
            await asyncio.to_thread(self.repository.patch_operation, operation, patch_name)
            await self._load_stack(
                preferred_selection=(
                    TreeItem.for_patch_selection(patch_name) if patch_name is not None else None
                )
            )

    async def action_move_staged_file_to_current_patch(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is not None and item.is_unstaged:
            await self.action_move_file_between_changes(stage=True)
            return
        if item is None or not item.is_staged or not item.is_file:
            return
        path = item.require_file_path()
        if not await self._ensure_repository_ready():
            return

        self._show_status(f"Moving staged file {path} into the current patch...")
        async with self._report_repository_errors():
            await asyncio.to_thread(
                self.repository.refresh_staged_files_to_current_patch,
                (path,),
            )
            current_patch = (
                next(
                    (
                        patch
                        for patch in self._loaded_data[0]
                        if patch.status == PatchStatus.CURRENT
                    ),
                    None,
                )
                if self._loaded_data is not None
                else None
            )
            if current_patch is None:
                await self._load_stack()
                return
            await self._load_stack(
                preferred_selection=item.as_selection(patch_name=current_patch.name),
                expansion_fallback_path=path,
            )

    async def _operate_selected_file_group(self, *, toward_newer: bool | None = None) -> bool:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if node is None or item is None:
            return False
        if not item.is_file_group:
            return False
        if toward_newer is None and item.change_area is None:
            return True
        if not await self._ensure_repository_ready():
            return True
        paths = tuple(
            child.data.require_file_path()
            for child in walk_nodes(node)
            if child.data is not None and child.data.is_file
        )
        label = item.directory_path or item.title
        self._show_status(f"Moving files in {label}...")
        async with self._handle_repository_conflict_error():
            selection = await self._move_file_group(item, paths, toward_newer)
            if selection is not None:
                if toward_newer is None:
                    selection = item.as_group_selection()
                await self._load_stack(preferred_selection=selection)
        return True

    async def _move_file_group(
        self, item: TreeItem, paths: tuple[str, ...], toward_newer: bool | None
    ) -> TreeItem | None:
        if not paths:
            self._show_status("No files to move in this group.")
            return None
        if item.patch_name is not None:
            return await self._move_patch_directory(item, paths, bool(toward_newer))
        if item.is_unstaged:
            if toward_newer:
                self._show_status("Unstaged changes are at the edge.")
                return None
            await asyncio.to_thread(self.repository.stage_files, paths)
            return item.as_selection(change_area="staged")
        if toward_newer is None or toward_newer:
            await asyncio.to_thread(self.repository.unstage_files, paths)
            return item.as_selection(change_area="unstaged")
        current = (
            next(
                (patch for patch in self._loaded_data[0] if patch.status == PatchStatus.CURRENT),
                None,
            )
            if self._loaded_data is not None
            else None
        )
        if current is None:
            self._show_status("There is no current patch to refresh.")
            return None
        await asyncio.to_thread(self.repository.refresh_staged_files_to_current_patch, paths)
        return item.as_selection(patch_name=current.name)

    async def _move_patch_directory(
        self, item: TreeItem, paths: tuple[str, ...], toward_newer: bool
    ) -> TreeItem | None:
        patch = (
            next((patch for patch in self._loaded_data[0] if patch.name == item.patch_name), None)
            if self._loaded_data is not None
            else None
        )
        if patch is None:
            return None
        if patch.status == PatchStatus.CURRENT and toward_newer:
            await asyncio.to_thread(self.repository.spill_current_patch_files, paths)
            return item.as_selection(change_area="staged")
        result, target = await asyncio.to_thread(
            self.repository.move_patch_changes, patch.name, paths, toward_newer=toward_newer
        )
        if result != PatchChangeMoveResult.MOVED:
            self._show_patch_change_move_result(
                result, patch.status, patch.name, toward_newer, False
            )
            return None
        return item.as_selection(patch_name=target)

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

    async def action_apply_selected_change(self) -> None:
        if await self._operate_selected_file_group():
            return
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or not item.is_working_change:
            return

        await self.action_move_file_between_changes(
            stage=item.is_unstaged,
            advance=True,
        )

    async def _move_selected_patch_change(self, *, toward_newer: bool) -> bool:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or not item.is_patch_change or self._loaded_data is None:
            return False
        patch_name = item.require_patch_name()
        path = item.require_file_path()
        if not await self._ensure_repository_ready():
            return True

        patches = self._loaded_data[0]
        source_index = next(
            (index for index, patch in enumerate(patches) if patch.name == patch_name),
            None,
        )
        if source_index is None:
            return False
        source_patch = patches[source_index]
        change_name = "hunk" if item.is_hunk else "file"
        direction = "newer" if toward_newer else "older"
        self._show_status(f"Moving {change_name} {path} {direction} from patch {patch_name}...")
        async with self._handle_repository_conflict_error():
            result, moved_to = await asyncio.to_thread(
                self.repository.move_patch_change,
                patch_name,
                path,
                toward_newer=toward_newer,
                hunk_diff=item.diff_text if item.is_hunk else None,
            )

            if result != PatchChangeMoveResult.MOVED:
                return self._show_patch_change_move_result(
                    result,
                    source_patch.status,
                    patch_name,
                    toward_newer,
                    item.is_file,
                )
            if moved_to is None:
                self._show_status(f"Could not determine the destination patch for {path}.")
                return True

            await self._load_stack(
                preferred_selection=item.as_selection(patch_name=moved_to),
                expansion_fallback_path=path,
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
        self._show_status(message)
        return True

    async def _reorder_selected_patch(self, *, toward_newer: bool) -> bool:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is None or not item.is_patch:
            return False
        patch_name = item.require_patch_name()
        if not await self._ensure_repository_ready():
            return True

        direction = "toward newer patches" if toward_newer else "toward older patches"
        self._show_status(f"Moving patch {patch_name} one position {direction}...")
        async with self._handle_repository_conflict_error():
            result = await asyncio.to_thread(
                self.repository.move_patch_one_step,
                patch_name,
                toward_newer=toward_newer,
            )

            if result == PatchMoveResult.STACK_EDGE:
                position = "top" if toward_newer else "bottom"
                self._show_status(f"Patch {patch_name} is already at the {position} of the stack.")
                return True
            if result == PatchMoveResult.APPLICATION_BOUNDARY:
                self._show_status(
                    "Patches cannot be reordered across the applied/unapplied boundary."
                )
                return True

            await self._load_stack(preferred_selection=item.as_selection())
        return True

    async def action_spill_current_patch_file(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        node = tree.cursor_node
        item = node.data if node is not None else None
        if item is not None and item.is_staged:
            await self.action_move_file_between_changes(stage=False)
            return
        if (
            item is None
            or not item.is_file
            or item.patch_name is None
            or self._loaded_data is None
            or not any(
                patch.name == item.patch_name and patch.status == PatchStatus.CURRENT
                for patch in self._loaded_data[0]
            )
        ):
            return
        path = item.require_file_path()
        if not await self._ensure_repository_ready():
            return

        self._show_status(f"Moving {path} out of the current patch...")
        async with self._report_repository_errors():
            await asyncio.to_thread(self.repository.spill_current_patch_files, (path,))
            await self._load_stack(
                preferred_selection=item.as_selection(change_area="staged"),
                expansion_fallback_path=path,
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
        if item is None or item.change_area != expected_area or not item.is_file_or_hunk:
            return
        path = item.require_file_path()
        if not await self._ensure_repository_ready():
            return

        next_selection = next_change_selection(node, item) if advance and node is not None else None
        verb = "Staging" if stage else "Unstaging"
        target = "hunk" if item.is_hunk else "file"
        self._show_status(f"{verb} {target} {path}...")
        async with self._report_repository_errors():
            if item.hunk_index is None:
                operation = self.repository.stage_files if stage else self.repository.unstage_files
                await asyncio.to_thread(operation, (path,))
            else:
                operation = self.repository.stage_hunk if stage else self.repository.unstage_hunk
                await asyncio.to_thread(operation, path, item.hunk_index)

            preferred_selection = (
                next_selection or item.as_group_selection()
                if advance
                else item.as_selection(change_area=target_area)
            )
            await self._load_stack(
                preferred_selection=preferred_selection,
                expansion_fallback_path=path
                if not advance
                else (path if preferred_selection.file_path == path else None),
            )

    def action_toggle_order(self) -> None:
        self.newest_first = not self.newest_first
        if self._loaded_data is not None:
            self.query_one("#patch-tree", LayerTree).populate(
                *self._loaded_data, newest_first=self.newest_first
            )
            order = "newest first" if self.newest_first else "oldest first"
            self._show_status(f"Showing {order}. Press R to reverse order; r to refresh.")

    async def action_apply_preview_change(self) -> None:
        tree = self.query_one("#patch-tree", LayerTree)
        item = tree.cursor_node.data if tree.cursor_node is not None else None
        if item is None or not item.is_file_or_hunk:
            return
        await self.action_apply_selected_change()
