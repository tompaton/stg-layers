import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, Static, TextArea, Tree
from textual.widgets.text_area import Selection

from stg_layers.app import StgLayersApp
from stg_layers.commands import CommandRunner
from stg_layers.dialogs import ConflictDialog, DeletePatchDialog, DiscardChangesDialog
from stg_layers.errors import RepositoryError
from stg_layers.git import Git
from stg_layers.models import DiffHunk, FileChange, FileStatus, Patch, PatchStatus, hunk_key
from stg_layers.partial_diff import HunkLineSelection
from stg_layers.repository import PatchChangeMoveResult, PatchMoveResult, StGitRepository
from stg_layers.tree_builder import TreeBuilder
from stg_layers.tree_walker import walk_nodes


@pytest.fixture(autouse=True)
def disable_periodic_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Most tests trigger refresh explicitly; automatic-refresh tests opt into a short interval.
    monkeypatch.setattr(StgLayersApp, "AUTO_REFRESH_INTERVAL", 3600.0)


class FixtureRepository:
    root = Path("/tmp/stg-layers-test")
    remote_url: str | None = "https://example.com/stg-layers.git"
    branch: str | None = "feature/read-browser"

    def __init__(self) -> None:
        self.actions: list[tuple[str, str, int | None]] = []
        self.discarded_hunks: list[tuple[str, str]] = []
        self.created_messages: list[str] = []
        self.edited_patches: list[tuple[str, str]] = []
        self.moved_files: list[str] = []
        self.spilled_files: list[str] = []
        self.patch_moves: list[tuple[str, bool]] = []
        self.patch_change_moves: list[tuple[str, str, bool, str | None]] = []
        self.patch_pushes: list[str] = []
        self.patch_pops: list[str] = []
        self.patch_operations: list[tuple[str, str | None]] = []
        self.undo_count = 0
        self.conflicted = False
        self.conflict_on_older_move = False
        self.conflict_on_push = False
        self.pending_stash_reference: str | None = None
        self.restore_stash_conflict = False

    def refresh_identity(self) -> None:
        pass

    def has_conflicts(self) -> bool:
        return self.conflicted

    def _file_change(self, path: str, prefix: str, content: str) -> FileChange:
        hunk = DiffHunk(
            index=0,
            header="@@ -1 +1 @@",
            diff_text=f"@@ -1 +1 @@\n-{prefix}\n+{content}\n",
            source_start=1,
            source_length=1,
            target_start=1,
            target_length=1,
        )
        return FileChange(
            path=path,
            source_path=f"a/{path}",
            target_path=f"b/{path}",
            status=FileStatus.MODIFIED,
            diff_text=f"diff --git a/{path} b/{path}\n{hunk.diff_text}",
            hunks=(hunk,),
        )

    def load_stack(self) -> tuple[Patch, ...]:
        return (
            Patch(
                name="types [notes]",
                status=PatchStatus.CURRENT,
                description="Type [annotations]",
                files=(self._file_change("example [typing].py", "before", "after"),),
            ),
        )

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        return (self._file_change("staged.py", "old staged", "new staged"),)

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        return (self._file_change("unstaged.py", "old unstaged", "new unstaged"),)

    def stage_files(self, paths: tuple[str, ...]) -> None:
        for path in paths:
            self.actions.append(("stage_files", path, None))

    def unstage_files(self, paths: tuple[str, ...]) -> None:
        for path in paths:
            self.actions.append(("unstage_files", path, None))

    def refresh_staged_files_to_current_patch(self, paths: tuple[str, ...]) -> None:
        self.moved_files.extend(paths)

    def spill_current_patch_files(self, paths: tuple[str, ...]) -> None:
        self.spilled_files.extend(paths)

    def move_patch_changes(
        self, patch_name: str, paths: tuple[str, ...], *, toward_newer: bool
    ) -> tuple[PatchChangeMoveResult, str | None]:
        result, target = PatchChangeMoveResult.STACK_EDGE, None
        for path in paths:
            result, target = self.move_patch_change(patch_name, path, toward_newer=toward_newer)
            if result != PatchChangeMoveResult.MOVED:
                break
        return result, target

    def refresh_staged_hunk_to_current_patch(self, path: str, hunk_diff: str) -> None:
        self.actions.append(("refresh_hunk", path, None))

    def spill_current_patch_hunk(self, path: str, hunk_diff: str) -> None:
        self.actions.append(("spill_hunk", path, None))

    def patch_diffstat(self, patch_name: str) -> str:
        return f" {patch_name}.txt | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)"

    def unstaged_diffstat(self, paths: tuple[str, ...], *, hunk_diff: str | None = None) -> str:
        return "\n".join(f" {path} | 2 +-" for path in paths)

    def discarded_lines_diffstat(self, selection: HunkLineSelection) -> str:
        return f" {selection.path} | 1 +\n 1 file changed, 1 insertion(+)"

    def discard_files(self, paths: tuple[str, ...]) -> None:
        self.actions.extend(("discard_files", path, None) for path in paths)

    def discard_hunk(self, path: str, hunk_diff: str) -> None:
        self.actions.append(("discard_hunk", path, None))
        self.discarded_hunks.append((path, hunk_diff))

    def move_selected_lines(self, selection: HunkLineSelection, *, stage: bool) -> None:
        self.actions.append(
            ("stage_lines" if stage else "unstage_lines", selection.path, selection.first_line)
        )

    def discard_selected_lines(self, selection: HunkLineSelection) -> None:
        self.actions.append(("discard_lines", selection.path, selection.first_line))

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        self.actions.append(("stage_hunk", path, hunk_index))

    def unstage_hunk(self, path: str, hunk_index: int) -> None:
        self.actions.append(("unstage_hunk", path, hunk_index))

    def new_patch_template(self) -> str:
        return "Patch: \nAuthor: Test <test@example.invalid>\n\n# Edit this message\n"

    def create_patch(self, message: str) -> None:
        self.created_messages.append(message)

    def edit_patch_template(self, patch_name: str) -> str:
        return f"Patch: {patch_name}\nAuthor: Test <test@example.invalid>\n\nPatch body\n"

    def edit_patch(self, patch_name: str, message: str) -> None:
        self.edited_patches.append((patch_name, message))

    def move_patch_one_step(self, patch_name: str, *, toward_newer: bool) -> PatchMoveResult:
        self.patch_moves.append((patch_name, toward_newer))
        if self.conflict_on_older_move and not toward_newer:
            self.conflicted = True
            raise RepositoryError("Merge conflict")
        return PatchMoveResult.MOVED

    def move_patch_change(
        self,
        patch_name: str,
        path: str,
        *,
        toward_newer: bool,
        hunk_diff: str | None = None,
    ) -> tuple[PatchChangeMoveResult, str | None]:
        self.patch_change_moves.append((patch_name, path, toward_newer, hunk_diff))
        return PatchChangeMoveResult.STACK_EDGE, None

    def patch_operation(self, operation: str, patch_name: str | None = None) -> None:
        if operation == "push":
            assert patch_name is not None
            self.patch_pushes.append(patch_name)
        elif operation == "pop":
            assert patch_name is not None
            self.patch_pops.append(patch_name)
        else:
            self.patch_operations.append((operation, patch_name))
        if operation in {"push", "commit"} and self.conflict_on_push:
            self.conflicted = True
            raise RepositoryError("Merge conflict")

    def undo_stack_operation(self) -> None:
        self.undo_count += 1
        self.conflicted = False
        self.pending_stash_reference = None
        self.restore_stash_conflict = False

    def refresh_stack_operation(self) -> str | None:
        retained = self.pending_stash_reference if self.restore_stash_conflict else None
        self.conflicted = False
        self.pending_stash_reference = None
        self.restore_stash_conflict = False
        return retained

    @property
    def stack_operation_pending(self) -> bool:
        return self.conflicted or self.pending_stash_reference is not None

    @property
    def stack_operation_stash_reference(self) -> str | None:
        return self.pending_stash_reference

    @property
    def stash_restore_conflict(self) -> bool:
        return self.restore_stash_conflict


class MutableFixtureRepository(FixtureRepository):
    def __init__(
        self,
        *,
        staged: tuple[FileChange, ...] = (),
        unstaged: tuple[FileChange, ...] = (),
    ) -> None:
        super().__init__()
        self.staged = list(staged)
        self.unstaged = list(unstaged)
        self.patch_files = list(super().load_stack()[0].files)

    def load_stack(self) -> tuple[Patch, ...]:
        patch = super().load_stack()[0]
        return (replace(patch, files=tuple(self.patch_files)),)

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        return tuple(self.staged)

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        return tuple(self.unstaged)

    @staticmethod
    def _file_with_hunks(path: str, hunks: tuple[DiffHunk, ...]) -> FileChange:
        base = FixtureRepository()._file_change(path, "old", "new")
        indexed_hunks = tuple(replace(hunk, index=index) for index, hunk in enumerate(hunks))
        diff_text = f"diff --git a/{path} b/{path}\n"
        diff_text += "".join(hunk.diff_text for hunk in indexed_hunks)
        return replace(base, diff_text=diff_text, hunks=indexed_hunks)

    def _move_file(
        self,
        source: list[FileChange],
        target: list[FileChange],
        path: str,
    ) -> None:
        file_index = next(index for index, file in enumerate(source) if file.path == path)
        target.append(source.pop(file_index))

    def _move_hunk(
        self,
        source: list[FileChange],
        target: list[FileChange],
        path: str,
        hunk_index: int,
    ) -> None:
        file_index = next(index for index, file in enumerate(source) if file.path == path)
        source_file = source[file_index]
        moved_hunk = source_file.hunks[hunk_index]
        remaining_hunks = tuple(
            hunk for index, hunk in enumerate(source_file.hunks) if index != hunk_index
        )
        if remaining_hunks:
            source[file_index] = self._file_with_hunks(path, remaining_hunks)
        else:
            source.pop(file_index)

        target_index = next(
            (index for index, file in enumerate(target) if file.path == path),
            None,
        )
        target_hunks = target[target_index].hunks if target_index is not None else ()
        target_file = self._file_with_hunks(path, (*target_hunks, moved_hunk))
        if target_index is None:
            target.append(target_file)
        else:
            target[target_index] = target_file

    def stage_files(self, paths: tuple[str, ...]) -> None:
        super().stage_files(paths)
        for path in paths:
            self._move_file(self.unstaged, self.staged, path)

    def unstage_files(self, paths: tuple[str, ...]) -> None:
        super().unstage_files(paths)
        for path in paths:
            self._move_file(self.staged, self.unstaged, path)

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        super().stage_hunk(path, hunk_index)
        self._move_hunk(self.unstaged, self.staged, path, hunk_index)

    def unstage_hunk(self, path: str, hunk_index: int) -> None:
        super().unstage_hunk(path, hunk_index)
        self._move_hunk(self.staged, self.unstaged, path, hunk_index)

    def refresh_staged_files_to_current_patch(self, paths: tuple[str, ...]) -> None:
        super().refresh_staged_files_to_current_patch(paths)
        for path in paths:
            self._move_file(self.staged, self.patch_files, path)

    def spill_current_patch_files(self, paths: tuple[str, ...]) -> None:
        super().spill_current_patch_files(paths)
        for path in paths:
            self._move_file(self.patch_files, self.staged, path)

    def refresh_staged_hunk_to_current_patch(self, path: str, hunk_diff: str) -> None:
        super().refresh_staged_hunk_to_current_patch(path, hunk_diff)
        file = next(file for file in self.staged if file.path == path)
        index = next(hunk.index for hunk in file.hunks if hunk.diff_text == hunk_diff)
        self._move_hunk(self.staged, self.patch_files, path, index)

    def spill_current_patch_hunk(self, path: str, hunk_diff: str) -> None:
        super().spill_current_patch_hunk(path, hunk_diff)
        file = next(file for file in self.patch_files if file.path == path)
        index = next(hunk.index for hunk in file.hunks if hunk.diff_text == hunk_diff)
        self._move_hunk(self.patch_files, self.staged, path, index)


class PatchChangeFixtureRepository(FixtureRepository):
    def __init__(self) -> None:
        super().__init__()
        self.patch_files = {
            "older": [
                self._file_with_hunks(
                    "shared.py",
                    (
                        self._hunk(1, "old first", "new first"),
                        self._hunk(20, "old second", "new second"),
                    ),
                )
            ],
            "current": [],
        }

    @staticmethod
    def _hunk(line: int, old: str, new: str) -> DiffHunk:
        return DiffHunk(
            index=0,
            header=f"@@ -{line} +{line} @@",
            diff_text=f"@@ -{line} +{line} @@\n-{old}\n+{new}\n",
            source_start=line,
            source_length=1,
            target_start=line,
            target_length=1,
        )

    @staticmethod
    def _file_with_hunks(path: str, hunks: tuple[DiffHunk, ...]) -> FileChange:
        base = FixtureRepository()._file_change(path, "old", "new")
        indexed_hunks = tuple(replace(hunk, index=index) for index, hunk in enumerate(hunks))
        diff_text = f"diff --git a/{path} b/{path}\n"
        diff_text += "".join(hunk.diff_text for hunk in indexed_hunks)
        return replace(base, diff_text=diff_text, hunks=indexed_hunks)

    def load_stack(self) -> tuple[Patch, ...]:
        return (
            Patch("older", PatchStatus.APPLIED, "Older patch", tuple(self.patch_files["older"])),
            Patch(
                "current",
                PatchStatus.CURRENT,
                "Current patch",
                tuple(self.patch_files["current"]),
            ),
        )

    def move_patch_change(
        self,
        patch_name: str,
        path: str,
        *,
        toward_newer: bool,
        hunk_diff: str | None = None,
    ) -> tuple[PatchChangeMoveResult, str | None]:
        self.patch_change_moves.append((patch_name, path, toward_newer, hunk_diff))
        source_index = ("older", "current").index(patch_name)
        target_name = ("older", "current")[source_index + (1 if toward_newer else -1)]
        source_files = self.patch_files[patch_name]
        file_index = next(index for index, file in enumerate(source_files) if file.path == path)
        source_file = source_files[file_index]
        if hunk_diff is None:
            moved_file = source_files.pop(file_index)
            self.patch_files[target_name].append(moved_file)
        else:
            hunk_index = next(
                index for index, hunk in enumerate(source_file.hunks) if hunk.diff_text == hunk_diff
            )
            moved_hunk = source_file.hunks[hunk_index]
            remaining_hunks = tuple(
                hunk for index, hunk in enumerate(source_file.hunks) if index != hunk_index
            )
            if remaining_hunks:
                source_files[file_index] = self._file_with_hunks(path, remaining_hunks)
            else:
                source_files.pop(file_index)
            target_files = self.patch_files[target_name]
            target_index = next(
                (index for index, file in enumerate(target_files) if file.path == path),
                None,
            )
            target_hunks = target_files[target_index].hunks if target_index is not None else ()
            moved_file = self._file_with_hunks(path, (*target_hunks, moved_hunk))
            if target_index is None:
                target_files.append(moved_file)
            else:
                target_files[target_index] = moved_file
        return PatchChangeMoveResult.MOVED, target_name


@pytest.mark.parametrize("reversed_order", [False, True])
def test_preview_navigates_hunks_across_collapsed_files(reversed_order: bool) -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            if reversed_order:
                app.action_toggle_order()
                await pilot.pause()
            hunks = [
                node
                for node in walk_nodes(tree.root)
                if node.data is not None and node.data.hunk_index is not None
            ]
            tree.move_cursor(tree.root)
            preview.focus()
            for hunk in hunks:
                await pilot.press("n")
                await pilot.pause()
                assert tree.cursor_node is hunk
                assert hunk.parent is not None and hunk.parent.is_expanded
                assert preview.has_focus
                assert hunk.data is not None and preview.text == hunk.data.diff_text
            await pilot.press("n")
            assert tree.cursor_node is hunks[-1]
            for hunk in reversed(hunks[:-1]):
                await pilot.press("p")
                await pilot.pause()
                assert tree.cursor_node is hunk
                assert preview.has_focus
                assert hunk.data is not None and preview.text == hunk.data.diff_text
            await pilot.press("p")
            assert tree.cursor_node is hunks[0]
            assert not repository.created_messages
            assert not repository.patch_pushes

            patch_hunk = next(hunk for hunk in hunks if hunk.data and hunk.data.patch_name)
            tree.move_cursor(patch_hunk)
            await pilot.pause()
            await pilot.press("enter")
            assert repository.actions == []

    asyncio.run(run_app())


class FailingRepository(FixtureRepository):
    failure: str | None = None
    loads = 0
    templates = 0

    def fail(self, operation: str) -> None:
        if self.failure == operation:
            raise RepositoryError(f"injected {operation}")

    def load_stack(self) -> tuple[Patch, ...]:
        self.loads += 1
        self.fail("load")
        return super().load_stack()

    def has_conflicts(self) -> bool:
        self.fail("ready")
        return super().has_conflicts()

    def new_patch_template(self) -> str:
        self.templates += 1
        self.fail("template")
        return super().new_patch_template()

    def stage_files(self, paths: tuple[str, ...]) -> None:
        self.fail("stage")
        super().stage_files(paths)

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        self.fail("hunk")
        super().stage_hunk(path, hunk_index)

    def patch_operation(self, operation: str, patch_name: str | None = None) -> None:
        self.fail("patch")
        super().patch_operation(operation, patch_name)


@pytest.mark.parametrize("failure", ["stage", "hunk", "template", "patch", "load", "ready"])
def test_repository_errors_are_reported_without_running_success_steps(failure: str) -> None:
    async def run_app() -> None:
        repository = FailingRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            original_loads = repository.loads
            repository.failure = failure
            if failure in {"stage", "hunk"}:
                file = tree.root.children[0].children[0]
                file.expand()
                await pilot.pause()
                selected = file.children[0] if failure == "hunk" else file
                tree.move_cursor(selected)
                await pilot.press("enter")
                assert tree.cursor_node is selected
                assert repository.actions == []
            elif failure == "patch":
                tree.move_cursor(tree.root.children[2].children[0])
                await pilot.press("c")
            else:
                await pilot.press("r" if failure == "load" else "n")
            await pilot.pause()
            assert str(app.query_one("#status", Static).render()) == f"Error: injected {failure}"
            assert app.screen is app.screen_stack[0]
            if failure == "load":
                assert str(tree.root.children[0].label) == "Unable to load StGit stack"
            else:
                assert repository.loads == original_loads
            if failure == "ready":
                assert repository.templates == 0

    asyncio.run(run_app())


def test_conflict_after_nontransactional_error_does_not_offer_undo() -> None:
    class ConflictingStageRepository(FixtureRepository):
        @property
        def stack_operation_pending(self) -> bool:
            return False

        def stage_files(self, paths: tuple[str, ...]) -> None:
            self.conflicted = True
            raise RepositoryError("stage failed")

    async def run_app() -> None:
        repository = ConflictingStageRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            tree.move_cursor(tree.root.children[0].children[0])
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConflictDialog)
            assert not app.screen.operation_failed
            assert not list(app.screen.query("#undo-conflict"))
            repository.conflicted = False
            await pilot.click("#refresh-conflict")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert repository.undo_count == 0

    asyncio.run(run_app())


@pytest.mark.parametrize("selection", ["patch", "file", "spill"])
def test_move_error_aborts_dispatch_without_spilling(selection: str) -> None:
    class FailedMoveRepository(FixtureRepository):
        def move_patch_one_step(self, patch_name: str, *, toward_newer: bool) -> PatchMoveResult:
            raise RepositoryError("move failed")

        def spill_current_patch_files(self, paths: tuple[str, ...]) -> None:
            raise RepositoryError("move failed")

        def move_patch_change(
            self,
            patch_name: str,
            path: str,
            *,
            toward_newer: bool,
            hunk_diff: str | None = None,
        ) -> tuple[PatchChangeMoveResult, str | None]:
            raise RepositoryError("move failed")

    async def run_app() -> None:
        repository = FailedMoveRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch = tree.root.children[2].children[0]
            selected = patch if selection == "patch" else patch.children[0]
            tree.focus()
            tree.move_cursor(selected)
            await pilot.press("ctrl+down" if selection == "file" else "ctrl+up")
            await pilot.pause()
            assert tree.cursor_node is selected
            assert str(app.query_one("#status", Static).render()) == "Error: move failed"
            assert repository.spilled_files == repository.moved_files == repository.actions == []

    asyncio.run(run_app())


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (PatchChangeMoveResult.STACK_EDGE, "Patch types [notes] is at the edge of the stack."),
        (
            PatchChangeMoveResult.APPLICATION_BOUNDARY,
            "Changes can only be moved between adjacent applied patches.",
        ),
        (
            PatchChangeMoveResult.SOURCE_NOT_APPLIED,
            "Changes can only be moved from applied patches.",
        ),
    ],
)
def test_rejected_patch_change_reports_status_without_reloading(
    result: PatchChangeMoveResult, message: str
) -> None:
    class RejectedMoveRepository(FixtureRepository):
        def move_patch_change(
            self,
            patch_name: str,
            path: str,
            *,
            toward_newer: bool,
            hunk_diff: str | None = None,
        ) -> tuple[PatchChangeMoveResult, str | None]:
            return result, None

    async def run_app() -> None:
        repository = RejectedMoveRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            # Rejected transfers between patches must not reload the tree.
            patch = tree.root.children[2].children[0]
            patch.expand()
            file = patch.children[0]
            file.expand()
            selected = file.children[0]
            tree.focus()
            await pilot.pause()
            tree.move_cursor(selected)
            await pilot.press("ctrl+down")
            await pilot.pause()
            assert tree.cursor_node is selected
            assert str(app.query_one("#status", Static).render()) == message
            assert repository.spilled_files == repository.actions == []

    asyncio.run(run_app())


@pytest.mark.parametrize("resolution", ["undo", "refresh"])
def test_recovery_failure_keeps_pending_transaction_recoverable(resolution: str) -> None:
    class FailedRecoveryRepository(FixtureRepository):
        probe_failed = False

        def has_conflicts(self) -> bool:
            if self.probe_failed:
                raise RepositoryError("conflict check failed")
            return False

        def undo_stack_operation(self) -> None:
            self.probe_failed = True
            raise RepositoryError("undo failed")

        def refresh_stack_operation(self) -> str | None:
            self.probe_failed = True
            raise RepositoryError("refresh failed")

    async def run_app() -> None:
        repository = FailedRecoveryRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            repository.pending_stash_reference = "saved-changes"
            app._conflict_locked = True
            await app._finish_stack_operation_conflict_resolution(resolution)
            await pilot.pause()
            assert isinstance(app.screen, ConflictDialog)
            assert app.screen.operation_failed
            assert list(app.screen.query("#undo-conflict"))
            assert repository.stack_operation_stash_reference == "saved-changes"

    asyncio.run(run_app())


def test_move_dispatch_keeps_the_original_selection_across_awaits() -> None:
    class SwitchingSelectionApp(StgLayersApp):
        switched = False

        async def _ensure_repository_ready(self) -> bool:
            ready = await super()._ensure_repository_ready()
            if ready and not self.switched:
                self.switched = True
                tree = self.query_one("#patch-tree", Tree)
                tree.move_cursor(tree.root.children[2].children[0].children[1])
            return ready

    async def run_app() -> None:
        repository = MutableFixtureRepository()
        fixture = FixtureRepository()
        repository.patch_files = [
            fixture._file_change(path, "old", "new") for path in ("first.py", "second.py")
        ]
        app = SwitchingSelectionApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            tree.move_cursor(tree.root.children[2].children[0].children[0])
            await pilot.press("ctrl+up")
            await pilot.pause()

            assert repository.spilled_files == ["first.py"]
            assert [file.path for file in repository.patch_files] == ["second.py"]
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.file_path == "first.py" and selected.data.is_staged

    asyncio.run(run_app())


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("action", ["spill", "refresh", "stage", "preview"])
def test_move_and_enter_check_readiness_once_before_dispatch(ready: bool, action: str) -> None:
    class ReadinessApp(StgLayersApp):
        readiness_checks = 0

        async def _ensure_repository_ready(self) -> bool:
            self.readiness_checks += 1
            return ready and await super()._ensure_repository_ready()

    async def run_app() -> None:
        repository = MutableFixtureRepository(
            unstaged=(FixtureRepository()._file_change("unstaged.py", "old", "new"),),
            staged=(FixtureRepository()._file_change("staged.py", "old", "new"),),
        )
        app = ReadinessApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            nodes = {
                "spill": tree.root.children[2].children[0].children[0],
                "refresh": tree.root.children[1].children[0],
            }
            tree.move_cursor(nodes.get(action, tree.root.children[0].children[0]))
            if action == "preview":
                app.query_one("#diff-view", TextArea).focus()
            else:
                tree.focus()
            key = {"spill": "ctrl+up", "refresh": "ctrl+down"}.get(action, "enter")
            await pilot.press(key)
            await pilot.pause()

            assert app.readiness_checks == 1
            if not ready:
                assert repository.actions == []
                assert repository.spilled_files == repository.moved_files == []
                assert repository.patch_change_moves == []
            elif action == "spill":
                assert repository.spilled_files == ["example [typing].py"]
            elif action == "refresh":
                assert repository.moved_files == ["staged.py"]
            else:
                assert repository.actions == [("stage_files", "unstaged.py", None)]

    asyncio.run(run_app())


def test_repository_error_context_does_not_suppress_other_errors() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            status = app.query_one("#status", Static)
            original = str(status.render())
            with pytest.raises(ValueError, match="programming error"):
                async with app._report_repository_errors():
                    raise ValueError("programming error")
            assert str(status.render()) == original

    asyncio.run(run_app())


def test_preview_enter_stages_and_unstages_only_the_selected_hunk() -> None:
    async def run_app() -> None:
        hunk_one = PatchChangeFixtureRepository._hunk(1, "old one", "new one")
        hunk_two = PatchChangeFixtureRepository._hunk(20, "old two", "new two")
        repository = MutableFixtureRepository(
            unstaged=(
                MutableFixtureRepository._file_with_hunks("src/shared.py", (hunk_one, hunk_two)),
                FixtureRepository()._file_change("tests/other.py", "old", "new"),
            )
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            file = next(
                node
                for node in walk_nodes(tree.root)
                if node.data is not None
                and node.data.file_path == "src/shared.py"
                and node.data.hunk_index is None
            )
            tree.move_cursor(file)
            preview.focus()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert repository.actions == [("stage_hunk", "src/shared.py", 0)]
            assert repository.staged[0].hunks[0].diff_text == hunk_one.diff_text
            assert repository.unstaged[0].hunks[0].diff_text == hunk_two.diff_text
            assert preview.has_focus
            assert preview.text == hunk_two.diff_text
            await pilot.press("enter")
            await pilot.pause()
            assert len(repository.staged[0].hunks) == 2
            assert [file.path for file in repository.unstaged] == ["tests/other.py"]
            assert preview.has_focus

            staged_hunk = next(
                node
                for node in walk_nodes(tree.root)
                if node.data is not None
                and node.data.change_area == "staged"
                and node.data.hunk_index == 0
            )
            assert staged_hunk.parent is not None
            staged_hunk.parent.expand()
            await pilot.pause()
            tree.move_cursor(staged_hunk)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert repository.actions[-1] == ("unstage_hunk", "src/shared.py", 0)
            assert len(repository.staged[0].hunks) == 1
            assert preview.has_focus

    asyncio.run(run_app())


@pytest.mark.parametrize("stage", [False, True])
def test_preview_enter_operates_on_the_whole_selected_file(stage: bool) -> None:
    async def run_app() -> None:
        selected = MutableFixtureRepository._file_with_hunks(
            "src/selected.py",
            (
                PatchChangeFixtureRepository._hunk(1, "old one", "new one"),
                PatchChangeFixtureRepository._hunk(20, "old two", "new two"),
            ),
        )
        sibling = FixtureRepository()._file_change("tests/other.py", "old", "new")
        files = (selected, sibling)
        repository = MutableFixtureRepository(
            unstaged=files if stage else (), staged=() if stage else files
        )
        source, target = ("unstaged", "staged") if stage else ("staged", "unstaged")
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            group = next(
                node for node in tree.root.children if node.data and node.data.change_area == source
            )
            preview.focus()
            for node in (group, group.children[0]):
                tree.move_cursor(node)
                await pilot.press("enter")
                assert repository.actions == []
            file = group.children[0].children[0]
            tree.move_cursor(file)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            verb = "stage_files" if stage else "unstage_files"
            assert repository.actions == [(verb, "src/selected.py", None)]
            assert getattr(repository, source) == [sibling]
            assert getattr(repository, target) == [selected]
            assert preview.has_focus

    asyncio.run(run_app())


def test_right_on_a_hunk_focuses_the_diff_preview() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            hunks = [
                node
                for node in walk_nodes(tree.root)
                if node.data is not None and node.data.hunk_index is not None
            ]
            for hunk in hunks:
                assert hunk.parent is not None
                hunk.parent.expand()
                await pilot.pause()
                tree.focus()
                tree.move_cursor(hunk)
                await pilot.pause()
                await pilot.press("right")
                assert preview.has_focus
                assert tree.cursor_node is hunk
                assert hunk.data is not None and preview.text == hunk.data.diff_text
                await pilot.press("tab")
                assert tree.has_focus

    asyncio.run(run_app())


@pytest.mark.parametrize("reversed_order", [False, True])
def test_change_group_actions_move_all_files(reversed_order: bool) -> None:
    async def run_app() -> None:
        paths = ("root.txt", "dir/one.txt", "dir/nested/two.txt")
        fixture = FixtureRepository()
        repository = MutableFixtureRepository(
            unstaged=tuple(fixture._file_change(path, "old", "new") for path in paths)
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            if reversed_order:
                await pilot.press("shift+r")
                await pilot.pause()
            older = "ctrl+up" if reversed_order else "ctrl+down"
            newer = "ctrl+down" if reversed_order else "ctrl+up"

            def select_group(area: str) -> None:
                group = next(
                    node
                    for node in tree.root.children
                    if node.data is not None and node.data.change_area == area
                )
                group.collapse()
                tree.move_cursor(group)

            select_group("staged")
            await pilot.press("enter", older)
            assert repository.moved_files == []
            assert repository.actions == []

            for key, source, target in (
                ("enter", "unstaged", "staged"),
                ("enter", "staged", "unstaged"),
                (older, "unstaged", "staged"),
                (newer, "staged", "unstaged"),
                (older, "unstaged", "staged"),
            ):
                select_group(source)
                await pilot.press(key)
                await pilot.pause()
                assert {file.path for file in getattr(repository, target)} == set(paths)
                assert not getattr(repository, source)
                selected = tree.cursor_node
                assert selected is not None and selected.data is not None
                assert selected.data.change_area == (source if key == "enter" else target)

            select_group("staged")
            await pilot.press(older)
            await pilot.pause()
            assert repository.moved_files == list(paths)
            assert not repository.staged
            assert set(paths) <= {file.path for file in repository.patch_files}
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.patch_name == "types [notes]"
            assert selected.data.file_path is None

    asyncio.run(run_app())


@pytest.mark.parametrize("reversed_order", [False, True])
def test_directory_actions_include_descendants_and_follow_selection(reversed_order: bool) -> None:
    async def run_app() -> None:
        fixture = FixtureRepository()
        paths = ("dir/root.txt", "dir/nested/one.txt", "dir/nested/deep/two.txt")
        repository = MutableFixtureRepository(
            unstaged=tuple(
                fixture._file_change(path, "old", "new")
                for path in (*paths, "dir-other/outside.txt")
            ),
            staged=(fixture._file_change("other/staged.txt", "old", "new"),),
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            if reversed_order:
                await pilot.press("shift+r")
                await pilot.pause()
            older = "ctrl+up" if reversed_order else "ctrl+down"
            newer = "ctrl+down" if reversed_order else "ctrl+up"
            directory = next(
                node
                for node in walk_nodes(tree.root)
                if node.data is not None
                and node.data.directory_path == "dir"
                and node.data.change_area == "unstaged"
            )
            tree.move_cursor(directory)
            for key, area, patch_name in (
                (older, "staged", None),
                (older, None, "types [notes]"),
                (newer, "staged", None),
                (newer, "unstaged", None),
            ):
                assert tree.cursor_node is not None
                tree.cursor_node.collapse()
                await pilot.press(key)
                await pilot.pause()
                selected = tree.cursor_node
                assert selected is not None and selected.data is not None
                assert selected.data.directory_path == "dir"
                assert selected.data.change_area == area
                assert selected.data.patch_name == patch_name
            assert repository.moved_files == list(paths)
            assert repository.spilled_files == list(paths)
            assert {file.path for file in repository.staged} == {"other/staged.txt"}
            assert {file.path for file in repository.unstaged} == {*paths, "dir-other/outside.txt"}

            await pilot.press("enter")
            await pilot.pause()
            assert {file.path for file in repository.unstaged} == {"dir-other/outside.txt"}
            staged_directory = next(
                node
                for node in walk_nodes(tree.root)
                if node.data is not None
                and node.data.directory_path == "dir"
                and node.data.change_area == "staged"
            )
            tree.move_cursor(staged_directory)
            await pilot.press("enter")
            await pilot.pause()
            assert {file.path for file in repository.staged} == {"other/staged.txt"}
            assert {file.path for file in repository.unstaged} == {*paths, "dir-other/outside.txt"}

    asyncio.run(run_app())


def test_directory_moves_between_patches() -> None:
    async def run_app() -> None:
        repository = PatchChangeFixtureRepository()
        fixture = FixtureRepository()
        paths = ("dir/first.txt", "dir/nested/second.txt")
        repository.patch_files["older"] = [
            fixture._file_change(path, "old", "new") for path in paths
        ]
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            directory = next(
                node
                for node in walk_nodes(tree.root)
                if node.data is not None and node.data.directory_path == "dir"
            )
            tree.focus()
            tree.move_cursor(directory)
            for key, target in (("ctrl+up", "current"), ("ctrl+down", "older")):
                await pilot.press(key)
                await pilot.pause()
                selected = tree.cursor_node
                assert selected is not None and selected.data is not None
                assert selected.data.directory_path == "dir"
                assert selected.data.patch_name == target
                assert {file.path for file in repository.patch_files[target]} == set(paths)

    asyncio.run(run_app())


def test_directory_grouping_compacts_single_directory_chains() -> None:
    fixture = FixtureRepository()
    paths = (
        "README.md",
        "src/stg_layers/app.py",
        "src/stg_layers/models.py",
        "tests/test_app.py",
        "pkg/root.txt",
        "pkg/nested/deep/file.txt",
        "branches/left/file.txt",
        "branches/right/file.txt",
    )
    files = tuple(fixture._file_change(path, "old", "new") for path in paths)
    tree = Tree("root")
    TreeBuilder.add_change_group(tree.root, "staged", files)
    TreeBuilder.add_change_group(tree.root, "unstaged", files)
    TreeBuilder.add_patch(tree.root, Patch("patch", PatchStatus.CURRENT, "", files))

    for group in tree.root.children:
        assert [str(node.label) for node in group.children] == [
            "README.md [modified] +1 -1",
            "branches",
            "pkg",
            "src/stg_layers",
            "tests",
        ]
        branches, pkg, src, tests = group.children[1:]
        assert [str(node.label) for node in branches.children] == ["left", "right"]
        assert [str(node.label) for node in pkg.children] == [
            "root.txt [modified] +1 -1",
            "nested/deep",
        ]
        assert [str(node.label) for node in src.children] == [
            "app.py [modified] +1 -1",
            "models.py [modified] +1 -1",
        ]
        assert [str(node.label) for node in tests.children] == ["test_app.py [modified] +1 -1"]
        assert src.is_expanded
        assert src.data is not None and src.data.directory_path == "src/stg_layers"
        file = src.children[0]
        assert file.is_collapsed
        assert file.data is not None and file.data.file_path == "src/stg_layers/app.py"
        assert "src/stg_layers/app.py" in file.data.title
        assert file.children[0].data is not None
        assert file.children[0].data.file_path == "src/stg_layers/app.py"
        assert group.data is not None
        assert src.data.selection_key != group.data.selection_key


def test_grouped_tree_preserves_state_and_file_actions() -> None:
    async def run_app() -> None:
        fixture = FixtureRepository()
        repository = MutableFixtureRepository(
            staged=(fixture._file_change("src/stg_layers/staged.py", "old", "new"),),
            unstaged=tuple(
                fixture._file_change(path, "old", "new")
                for path in ("src/stg_layers/app.py", "tests/test_app.py")
            ),
        )
        repository.patch_files = [fixture._file_change("src/stg_layers/patch.py", "old", "new")]
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            staged_directory = tree.root.children[1].children[0]
            tree.move_cursor(staged_directory)
            staged_directory.collapse()
            await pilot.press("r")
            await pilot.pause()
            assert tree.cursor_node is not None and tree.cursor_node.data is not None
            assert tree.cursor_node.data.directory_path == "src/stg_layers"
            assert tree.cursor_node.is_collapsed
            await pilot.press("shift+r")
            await pilot.pause()
            assert tree.cursor_node is not None and tree.cursor_node.is_collapsed
            await pilot.press("shift+r")
            await pilot.pause()

            patch_directory = tree.root.children[2].children[0].children[0]
            tree.move_cursor(patch_directory)
            await pilot.press("enter", "c", "h", "shift+h", "delete", "e", "p")
            assert repository.patch_moves == []
            assert repository.patch_operations == []
            assert repository.edited_patches == []
            assert repository.patch_pushes == []
            assert repository.actions == []
            assert app.screen is app.screen_stack[0]

            directory = tree.root.children[0].children[0]
            tree.move_cursor(directory)
            file = directory.children[0]
            file.expand()
            tree.move_cursor(file)
            await pilot.press("enter")
            await pilot.pause()
            assert tree.cursor_node is not None and tree.cursor_node.data is not None
            assert tree.cursor_node.data.file_path == "tests/test_app.py"
            assert tree.cursor_node.parent is not None and tree.cursor_node.parent.is_expanded
            assert repository.actions == [("stage_files", "src/stg_layers/app.py", None)]

            staged_directory = tree.root.children[1].children[0]
            staged_directory.expand()
            staged_file = staged_directory.children[1]
            staged_file.expand()
            tree.move_cursor(staged_file)
            await pilot.press("ctrl+up")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.file_path == "src/stg_layers/app.py"
            assert selected.data.change_area == "unstaged"
            assert selected.is_expanded
            assert "+new" in app.query_one("#diff-view", TextArea).text
            tree.root.children[1].children[0].collapse()
            await pilot.press("ctrl+down")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.file_path == "src/stg_layers/app.py"
            assert selected.data.change_area == "staged"
            assert selected.parent is not None and selected.parent.is_expanded

    asyncio.run(run_app())


def test_app_renders_patch_file_hunk_tree() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            assert [str(child.label) for child in tree.root.children] == [
                "Unstaged changes",
                "Staged changes",
                "Patch stack",
            ]
            patch_stack = tree.root.children[2]
            patch_node = patch_stack.children[0]
            file_node = patch_node.children[0]
            hunk_node = file_node.children[0]
            assert "types [notes]" in str(patch_node.label)
            assert "example [typing].py" in str(file_node.label)
            assert "@@ -1 +1 @@" in str(hunk_node.label)
            assert isinstance(patch_stack.label, Text)
            assert isinstance(patch_node.label, Text)
            assert isinstance(file_node.label, Text)
            assert isinstance(hunk_node.label, Text)
            assert str(patch_stack.label.style) == "bold cyan"
            assert str(patch_node.label) == "[>types [notes]] Type [annotations]"
            assert str(patch_node.label.spans[0].style) == "bold green"
            assert str(file_node.label.spans[0].style) == "cyan"
            assert str(file_node.label.spans[1].style) == "blue"
            assert str(hunk_node.label.style) == "yellow"
            assert hunk_node.data is not None
            assert hunk_node.data.patch_name == "types [notes]"
            assert hunk_node.data.file_path == "example [typing].py"
            assert hunk_node.data.hunk_index == 0
            assert app.sub_title == "https://example.com/stg-layers.git"

            assert patch_stack.is_expanded
            assert patch_node.is_expanded
            assert file_node.is_collapsed

            staged_group = tree.root.children[1]
            staged_file = staged_group.children[0]
            staged_hunk = staged_file.children[0]
            assert "staged.py" in str(staged_file.label)
            assert staged_hunk.data is not None
            assert staged_hunk.data.change_area == "staged"

            unstaged_group = tree.root.children[0]
            unstaged_file = unstaged_group.children[0]
            unstaged_hunk = unstaged_file.children[0]
            assert "unstaged.py" in str(unstaged_file.label)
            assert unstaged_hunk.data is not None
            assert unstaged_hunk.data.change_area == "unstaged"

            assert tree.root.is_expanded
            assert tree.styles.overflow_x == "hidden"
            tree.focus()
            await pilot.press("down", "down", "down", "down", "down", "down")
            await pilot.pause()
            title = app.query_one("#selection-title", Static).render()
            assert "Patch: types [notes]" in str(title)
            preview = app.query_one("#diff-view", TextArea)
            patch_diff = preview.text

            await pilot.press("enter")
            await pilot.pause()
            assert tree.cursor_node is patch_node
            assert preview.text == patch_diff

            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is file_node
            assert "example [typing].py" in str(app.query_one("#selection-title", Static).render())
            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is file_node
            assert file_node.is_expanded
            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is hunk_node
            assert "hunk 1" in str(app.query_one("#selection-title", Static).render())
            assert "+after" in preview.text
            assert preview.read_only
            assert not preview.show_line_numbers
            assert preview.language == "diff"
            assert preview.theme == "stg-diff"
            assert any(name == "attribute" for _, _, name in preview._highlights[0])
            assert any(name == "diff.minus" for _, _, name in preview._highlights[1])
            assert any(name == "diff.plus" for _, _, name in preview._highlights[2])

            await pilot.press("left")
            await pilot.pause()
            assert tree.cursor_node is file_node
            assert "example [typing].py" in preview.text
            await pilot.press("left")
            await pilot.pause()
            assert file_node.is_collapsed
            await pilot.press("right")
            await pilot.pause()
            assert file_node.is_expanded
            assert tree.cursor_node is file_node

    asyncio.run(run_app())


def test_preview_word_wrap_toggle_persists_across_hunk_navigation() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            tree.move_cursor(tree.root)
            preview.focus()
            assert not preview.soft_wrap
            text = preview.text
            await pilot.press("w")
            assert preview.soft_wrap
            assert preview.text == text
            assert preview.has_focus
            await pilot.press("n")
            await pilot.pause()
            assert preview.soft_wrap
            text = preview.text
            await pilot.press("w")
            assert not preview.soft_wrap
            assert preview.text == text
            assert preview.has_focus

    asyncio.run(run_app())


def test_diff_preview_can_focus_and_scroll_without_editing() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            original_text = "\n".join(f"+added line {index}" for index in range(100))
            preview.load_text(original_text)
            tree.focus()
            await pilot.press("tab")
            await pilot.pause()

            assert preview.has_focus
            assert preview.read_only
            await pilot.press("pagedown")
            await pilot.pause()
            assert preview.scroll_y > 0
            await pilot.press("a")
            assert preview.text == original_text

    asyncio.run(run_app())


def test_app_shows_empty_staged_and_unstaged_sections() -> None:
    class EmptyRepository(FixtureRepository):
        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        app = StgLayersApp(EmptyRepository())
        async with app.run_test():
            tree = app.query_one("#patch-tree", Tree)
            assert "No staged changes." in str(tree.root.children[1].children[0].label)
            assert "No unstaged changes." in str(tree.root.children[0].children[0].label)
            assert tree.root.children[1].is_expanded
            assert tree.root.children[2].is_expanded

    asyncio.run(run_app())


def test_enter_advances_through_files_in_the_source_group() -> None:
    async def run_app() -> None:
        repository = MutableFixtureRepository(
            unstaged=(
                FixtureRepository()._file_change("first.py", "old first", "new first"),
                FixtureRepository()._file_change("second.py", "old second", "new second"),
            )
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            first_file = tree.root.children[0].children[0]
            tree.focus()
            tree.move_cursor(first_file)
            await pilot.press("enter")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.file_path == "second.py"
            await pilot.press("enter")
            await pilot.pause()

            assert repository.actions == [
                ("stage_files", "first.py", None),
                ("stage_files", "second.py", None),
            ]
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.title == "Unstaged changes"

    asyncio.run(run_app())


def test_enter_advances_while_unstaging_files() -> None:
    async def run_app() -> None:
        repository = MutableFixtureRepository(
            staged=(
                FixtureRepository()._file_change("first.py", "old first", "new first"),
                FixtureRepository()._file_change("second.py", "old second", "new second"),
            )
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            first_file = tree.root.children[1].children[0]
            tree.move_cursor(first_file)
            tree.focus()
            await pilot.press("enter")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.file_path == "second.py"
            await pilot.press("enter")
            await pilot.pause()

            assert repository.actions == [
                ("unstage_files", "first.py", None),
                ("unstage_files", "second.py", None),
            ]
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.title == "Staged changes"

    asyncio.run(run_app())


def test_staging_hunk_refreshes_preview_and_preserves_expanded_file() -> None:
    async def run_app() -> None:
        first_hunk = DiffHunk(
            index=0,
            header="@@ -1 +1 @@ first",
            diff_text="@@ -1 +1 @@ first\n-old first\n+new first\n",
            source_start=1,
            source_length=1,
            target_start=1,
            target_length=1,
        )
        second_hunk = DiffHunk(
            index=1,
            header="@@ -10 +10 @@ second",
            diff_text="@@ -10 +10 @@ second\n-old second\n+new second\n",
            source_start=10,
            source_length=1,
            target_start=10,
            target_length=1,
        )
        change = MutableFixtureRepository._file_with_hunks(
            "multi.txt",
            (first_hunk, second_hunk),
        )
        repository = MutableFixtureRepository(unstaged=(change,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            file_node = tree.root.children[0].children[0]
            tree.move_cursor(file_node)
            tree.focus()
            await pilot.press("right", "right")
            await pilot.press("enter")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "unstaged"
            assert selected.data.hunk_index == 0
            assert selected.data.hunk_key == hunk_key(second_hunk.diff_text)
            unstaged_file = tree.root.children[0].children[0]
            assert unstaged_file.is_expanded
            assert "+new second" in app.query_one("#diff-view", TextArea).text
            assert "Unstaged changes / multi.txt / hunk 1" in str(
                app.query_one("#selection-title", Static).render()
            )

            await pilot.press("enter")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.title == "Unstaged changes"
            assert repository.actions == [
                ("stage_hunk", "multi.txt", 0),
                ("stage_hunk", "multi.txt", 0),
            ]

    asyncio.run(run_app())


def test_ctrl_down_moves_file_from_unstaged_to_staged_then_current_patch() -> None:
    async def run_app() -> None:
        change = FixtureRepository()._file_change("moving.py", "old", "new")
        repository = MutableFixtureRepository(unstaged=(change,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.move_cursor(tree.root.children[0].children[0])
            tree.focus()
            await pilot.press("ctrl+down")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "staged"
            assert selected.data.file_path == "moving.py"
            await pilot.press("ctrl+down")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.patch_name == "types [notes]"
            assert selected.data.file_path == "moving.py"
            assert repository.moved_files == ["moving.py"]

    asyncio.run(run_app())


def test_ctrl_up_and_down_move_hunks_between_changes_and_follow_selection() -> None:
    async def run_app() -> None:
        hunk = DiffHunk(
            index=0,
            header="@@ -1 +1 @@",
            diff_text="@@ -1 +1 @@\n-old\n+new\n",
            source_start=1,
            source_length=1,
            target_start=1,
            target_length=1,
        )
        change = MutableFixtureRepository._file_with_hunks("move.txt", (hunk,))
        repository = MutableFixtureRepository(unstaged=(change,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            unstaged_file = tree.root.children[0].children[0]
            tree.move_cursor(unstaged_file)
            tree.focus()
            await pilot.press("right", "right")
            await pilot.press("ctrl+down")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "staged"
            assert selected.data.file_path == "move.txt"
            assert selected.data.hunk_index == 0
            assert "+new" in app.query_one("#diff-view", TextArea).text

            await pilot.press("ctrl+up")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "unstaged"
            assert selected.data.file_path == "move.txt"
            assert selected.data.hunk_index == 0
            assert "+new" in app.query_one("#diff-view", TextArea).text

    asyncio.run(run_app())


def test_ctrl_shortcuts_follow_reversed_tree_direction() -> None:
    async def run_app() -> None:
        moving_file = FixtureRepository()._file_change("moving.py", "old", "new")
        repository = MutableFixtureRepository(staged=(moving_file,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()

            tree = app.query_one("#patch-tree", Tree)
            staged_file = tree.root.children[1].children[0]
            tree.move_cursor(staged_file)
            tree.focus()
            await pilot.press("ctrl+up")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.patch_name == "types [notes]"
            assert selected.data.file_path == "moving.py"
            assert repository.moved_files == ["moving.py"]

            await pilot.press("ctrl+down")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "staged"
            assert selected.data.file_path == "moving.py"
            assert repository.spilled_files == ["moving.py"]

            await pilot.press("ctrl+down")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "unstaged"
            assert selected.data.file_path == "moving.py"
            assert repository.actions[-1] == ("unstage_files", "moving.py", None)

    asyncio.run(run_app())


def test_patch_management_shortcuts_and_delete_confirmation() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            tree.move_cursor(tree.root.children[2].children[0])
            for key in ("c", "shift+c", "h", "shift+h"):
                await pilot.press(key)
                await pilot.pause()
            name = "types [notes]"
            assert repository.patch_operations == [
                ("commit", name),
                ("uncommit", None),
                ("hide", name),
                ("unhide", name),
            ]

            await pilot.press("delete")
            assert isinstance(app.screen, DeletePatchDialog)
            summary = app.screen.query_one("#delete-stat", VerticalScroll).query_one(Static)
            assert "types [notes].txt" in str(summary.render())
            assert "1 insertion(+)" in str(summary.render())
            assert len(repository.patch_operations) == 4
            await pilot.click("#cancel-delete")
            await pilot.pause()
            assert len(repository.patch_operations) == 4

            await pilot.press("delete")
            await pilot.press("escape")
            await pilot.pause()
            assert len(repository.patch_operations) == 4

            await pilot.press("delete")
            await pilot.click("#confirm-delete")
            await pilot.pause()
            assert repository.patch_operations[-1] == ("delete", name)
            assert not isinstance(app.screen, DeletePatchDialog)

            tree.move_cursor(tree.root.children[2].children[0].children[0])
            await pilot.press("c", "h", "shift+h", "delete")
            assert len(repository.patch_operations) == 5

            # Uncommit does not require a patch selection.
            await pilot.press("shift+c")
            await pilot.pause()
            assert repository.patch_operations[-1] == ("uncommit", None)
            assert len(repository.patch_operations) == 6

    asyncio.run(run_app())


def test_conflicting_commit_offers_undo() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        repository.conflict_on_push = True
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            tree.move_cursor(tree.root.children[2].children[0])
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConflictDialog)
            await pilot.click("#undo-conflict")
            await pilot.pause()
            assert repository.undo_count == 1
            assert not repository.conflicted

    asyncio.run(run_app())


def test_create_patch_dialog_passes_edited_template_to_repository() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#patch-tree", Tree).focus()
            await pilot.press("n")
            await pilot.pause()

            message = app.screen.query_one("#patch-message", TextArea)
            assert message.text == repository.new_patch_template()
            edited_message = "Patch: dialog-created\nAuthor: Test <test@example.invalid>\n"
            message.load_text(edited_message)
            await pilot.click("#create-patch")
            await pilot.pause()

            assert repository.created_messages == [edited_message]
            assert not list(app.query("#patch-message"))

    asyncio.run(run_app())


def test_create_patch_dialog_can_be_cancelled() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#patch-tree", Tree).focus()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert repository.created_messages == []
            assert not list(app.query("#patch-message"))
            assert "cancelled" in str(app.query_one("#status", Static).render()).lower()

    asyncio.run(run_app())


def test_edit_selected_patch_opens_template_and_updates_patch_message() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("e")
            await pilot.pause()

            dialog = app.screen
            message = dialog.query_one("#patch-message", TextArea)
            assert message.text == repository.edit_patch_template("types [notes]")
            edited_message = "Patch: types [notes]\n\nEdited patch body\n"
            message.load_text(edited_message)
            await pilot.click("#create-patch")
            await pilot.pause()

            assert repository.edited_patches == [("types [notes]", edited_message)]

    asyncio.run(run_app())


def test_edit_patch_is_ignored_when_a_file_is_selected_and_cancel_works() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            file_node = tree.root.children[2].children[0].children[0]
            tree.move_cursor(file_node)
            await pilot.press("e")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]

            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("e")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert repository.edited_patches == []
            assert "cancelled" in str(app.query_one("#status", Static).render()).lower()

    asyncio.run(run_app())


def test_ctrl_down_moves_staged_file_and_hunk_to_current_patch() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            staged_file = tree.root.children[1].children[0]
            tree.move_cursor(staged_file)
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert repository.moved_files == ["staged.py"]

            staged_file = tree.root.children[1].children[0]
            staged_file.expand()
            staged_hunk = staged_file.children[0]
            tree.move_cursor(staged_hunk)
            await pilot.press("ctrl+down")
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("ctrl+down")
            assert repository.moved_files == ["staged.py"]

    asyncio.run(run_app())


def test_ctrl_up_spills_current_patch_file_and_hunk() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            current_patch_file = tree.root.children[2].children[0].children[0]
            tree.move_cursor(current_patch_file)
            await pilot.press("ctrl+up")
            await pilot.pause()
            assert repository.spilled_files == ["example [typing].py"]

            current_patch_file = tree.root.children[2].children[0].children[0]
            current_patch_file.expand()
            tree.move_cursor(current_patch_file.children[0])
            await pilot.press("ctrl+up")
            staged_file = tree.root.children[1].children[0]
            tree.move_cursor(staged_file)
            await pilot.press("ctrl+up")
            assert repository.spilled_files == ["example [typing].py"]

    asyncio.run(run_app())


def test_ctrl_up_down_move_selected_hunks_between_adjacent_patches() -> None:
    async def run_app() -> None:
        repository = PatchChangeFixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            patch_stack = tree.root.children[2]
            older_patch = next(
                node
                for node in patch_stack.children
                if node.data is not None and node.data.patch_name == "older"
            )
            older_file = older_patch.children[0]
            older_file.expand()
            await pilot.pause()
            selected_hunk = older_file.children[0]
            selected_hunk_data = selected_hunk.data
            assert selected_hunk_data is not None
            tree.move_cursor(selected_hunk)

            await pilot.press("ctrl+up")
            await pilot.pause()

            selected_node = tree.cursor_node
            assert selected_node is not None
            selected = selected_node.data
            assert selected is not None
            assert selected.patch_name == "current"
            assert selected.file_path == "shared.py"
            assert selected.hunk_key == hunk_key(
                repository.patch_files["current"][0].hunks[0].diff_text
            )
            assert repository.patch_change_moves == [
                ("older", "shared.py", True, selected_hunk_data.diff_text)
            ]

            await pilot.press("ctrl+down")
            await pilot.pause()
            selected_node = tree.cursor_node
            assert selected_node is not None
            selected = selected_node.data
            assert selected is not None
            assert selected.patch_name == "older"
            assert repository.patch_change_moves[-1][2] is False

            await pilot.press("R")
            await pilot.pause()
            patch_stack = tree.root.children[0]
            older_patch = next(
                node
                for node in patch_stack.children
                if node.data is not None and node.data.patch_name == "older"
            )
            older_file = older_patch.children[0]
            older_file.expand()
            await pilot.pause()
            tree.move_cursor(older_file.children[0])
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert repository.patch_change_moves[-1][2] is True
            selected_node = tree.cursor_node
            assert selected_node is not None
            selected = selected_node.data
            assert selected is not None
            assert selected.patch_name == "current"

    asyncio.run(run_app())


def test_ctrl_up_and_down_move_selected_patch_one_step_in_each_order() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            tree.focus()
            await pilot.press("ctrl+up")
            await pilot.pause()
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert repository.patch_moves == [
                ("types [notes]", True),
                ("types [notes]", False),
            ]

            await pilot.press("R")
            await pilot.pause()
            patch_node = tree.root.children[0].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("ctrl+up")
            await pilot.pause()
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert repository.patch_moves == [
                ("types [notes]", True),
                ("types [notes]", False),
                ("types [notes]", False),
                ("types [notes]", True),
            ]

    asyncio.run(run_app())


def test_conflicting_patch_reorder_offers_undo() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        repository.conflict_on_older_move = True
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            tree.focus()
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert app.screen.query_one("#undo-conflict")
            await pilot.click("#undo-conflict")
            await pilot.pause()
            assert repository.undo_count == 1
            assert not repository.conflicted
            assert app.screen is app.screen_stack[0]

    asyncio.run(run_app())


def test_stash_restore_conflict_refresh_retains_recovery_stash() -> None:
    class PatchOperationRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return (Patch("queued", PatchStatus.UNAPPLIED, "", ()),)

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def patch_operation(self, operation: str, patch_name: str | None = None) -> None:
            super().patch_operation(operation, patch_name)
            if operation == "push":
                self.conflicted = False
                self.pending_stash_reference = "stash-commit"
                self.restore_stash_conflict = True
                raise RepositoryError("Stash restoration conflict")

    async def run_app() -> None:
        repository = PatchOperationRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.move_cursor(tree.root.children[2].children[0])
            tree.focus()
            await pilot.press("p")
            await pilot.pause()

            dialog = app.screen
            assert isinstance(dialog, ConflictDialog)
            description = str(dialog.query_one("#conflict-description", Static).render())
            assert "stash-commit" in description
            await pilot.click("#refresh-conflict")
            await pilot.pause()

            assert app.screen is app.screen_stack[0]
            assert "stash-commit was retained" in str(app.query_one("#status", Static).render())
            assert not repository.stack_operation_pending

    asyncio.run(run_app())


def test_push_and_pop_selected_patches_with_p_and_shift_p() -> None:
    class PatchOperationRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return (
                Patch("older", PatchStatus.APPLIED, "", ()),
                Patch("current", PatchStatus.CURRENT, "", ()),
                Patch("queued", PatchStatus.UNAPPLIED, "", ()),
            )

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        repository = PatchOperationRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            nodes = {
                node.data.patch_name: node
                for node in tree.root.children[2].children
                if node.data is not None
            }
            tree.move_cursor(nodes["queued"])
            tree.focus()
            await pilot.press("shift+p")
            assert repository.patch_pops == []
            await pilot.press("p")
            await pilot.pause()
            assert repository.patch_pushes == ["queued"]

            nodes = {
                node.data.patch_name: node
                for node in tree.root.children[2].children
                if node.data is not None
            }
            tree.move_cursor(nodes["current"])
            await pilot.press("p")
            assert repository.patch_pushes == ["queued"]
            await pilot.press("shift+p")
            await pilot.pause()
            assert repository.patch_pops == ["current"]

    asyncio.run(run_app())


def test_conflicting_patch_push_offers_undo() -> None:
    class PatchOperationRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return (Patch("queued", PatchStatus.UNAPPLIED, "", ()),)

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        repository = PatchOperationRepository()
        repository.conflict_on_push = True
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.move_cursor(tree.root.children[2].children[0])
            tree.focus()
            await pilot.press("p")
            await pilot.pause()

            assert app.screen.query_one("#undo-conflict")
            await pilot.click("#undo-conflict")
            await pilot.pause()
            assert repository.undo_count == 1
            assert not repository.conflicted

    asyncio.run(run_app())


def test_repository_conflict_locks_actions_until_refresh() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        repository.conflicted = True
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#refresh-conflict")
            await pilot.press("enter")
            await pilot.pause()
            assert repository.actions == []

            repository.conflicted = False
            await pilot.click("#refresh-conflict")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert app._loaded_data is not None

    asyncio.run(run_app())


def test_repository_conflict_detected_before_mutating_action() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.move_cursor(tree.root.children[0].children[0])
            tree.focus()
            repository.conflicted = True
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ConflictDialog)
            assert app.screen.query_one("#refresh-conflict")
            assert repository.actions == []
            repository.conflicted = False
            await pilot.click("#refresh-conflict")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]

    asyncio.run(run_app())


def test_order_toggle_reverses_sections_and_patch_sequence() -> None:
    class MultiplePatchRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return tuple(
                Patch(name=name, status=PatchStatus.APPLIED, description="", files=())
                for name in ("oldest", "middle", "newest")
            )

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        app = StgLayersApp(MultiplePatchRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            assert [str(node.label) for node in tree.root.children] == [
                "Unstaged changes",
                "Staged changes",
                "Patch stack",
            ]
            patch_stack = tree.root.children[2]
            assert all(node.data is not None for node in patch_stack.children)
            assert [node.data.patch_name for node in patch_stack.children if node.data] == [
                "newest",
                "middle",
                "oldest",
            ]

            await pilot.press("R")
            await pilot.pause()
            assert [str(node.label) for node in tree.root.children] == [
                "Patch stack",
                "Staged changes",
                "Unstaged changes",
            ]
            patch_stack = tree.root.children[0]
            assert all(node.data is not None for node in patch_stack.children)
            assert [node.data.patch_name for node in patch_stack.children if node.data] == [
                "oldest",
                "middle",
                "newest",
            ]
            assert "oldest first" in str(app.query_one("#status", Static).render())

            await pilot.press("shift+r")
            await pilot.pause()
            assert [str(node.label) for node in tree.root.children] == [
                "Unstaged changes",
                "Staged changes",
                "Patch stack",
            ]
            assert "newest first" in str(app.query_one("#status", Static).render())

    asyncio.run(run_app())


def test_patch_labels_use_compact_ids_titles_and_status_markers() -> None:
    class PatchStatusRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return tuple(
                Patch(name=name, status=status, description="Patch title", files=())
                for name, status in (
                    ("current", PatchStatus.CURRENT),
                    ("applied", PatchStatus.APPLIED),
                    ("unapplied", PatchStatus.UNAPPLIED),
                    ("hidden", PatchStatus.HIDDEN),
                )
            )

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        app = StgLayersApp(PatchStatusRepository())
        async with app.run_test():
            tree = app.query_one("#patch-tree", Tree)
            patch_stack = tree.root.children[2]
            assert [str(node.label) for node in patch_stack.children] == [
                "[~hidden] Patch title",
                "[-unapplied] Patch title",
                "[+applied] Patch title",
                "[>current] Patch title",
            ]
            assert [
                str(node.label.spans[0].style)
                for node in patch_stack.children
                if isinstance(node.label, Text)
            ] == ["dim magenta", "yellow", "cyan", "bold green"]

    asyncio.run(run_app())


def test_loaded_patch_accessors_distinguish_unloaded_and_empty_stacks() -> None:
    app = StgLayersApp(FixtureRepository())
    assert app._get_patch("missing") is None
    assert app._get_patch(None) is None
    assert app._current_patch is None
    with pytest.raises(RepositoryError, match="has not been loaded"):
        app._require_loaded_patches()

    app._loaded_data = (), (), ()
    assert app._require_loaded_patches() == ()
    assert app._get_patch("missing") is None
    assert app._get_patch(None) is None
    assert app._current_patch is None


def test_patch_accessors_follow_the_latest_loaded_snapshot() -> None:
    app = StgLayersApp(FixtureRepository())
    earlier = Patch("earlier", PatchStatus.APPLIED, "", ())
    current = Patch("current", PatchStatus.CURRENT, "", ())
    queued = Patch("queued", PatchStatus.UNAPPLIED, "", ())
    patches = (earlier, current, queued)
    app._loaded_data = patches, (), ()
    assert app._require_loaded_patches() is patches
    assert app._get_patch("earlier") is earlier
    assert app._get_patch("missing") is None
    assert app._get_patch(None) is None
    assert app._current_patch is current

    app._loaded_data = (queued,), (), ()
    assert app._get_patch("current") is None
    assert app._get_patch("queued") is queued
    assert app._current_patch is None

    new_current = replace(queued, status=PatchStatus.CURRENT)
    app._loaded_data = (new_current,), (), ()
    assert app._get_patch("queued") is new_current
    assert app._current_patch is new_current


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("empty", "No files to move in this group."),
        ("unstaged_edge", "Unstaged changes are at the edge."),
        ("no_current_group", "There is no current patch to refresh."),
        ("no_current_file", "There is no current patch to refresh."),
        ("patch_edge", "Patch types [notes] is already at the top of the stack."),
        ("patch_boundary", "Patches cannot be reordered across the applied/unapplied boundary."),
    ],
)
def test_move_status_aborts_without_reloading(case: str, message: str) -> None:
    class StatusRepository(FixtureRepository):
        loads = 0

        def load_stack(self) -> tuple[Patch, ...]:
            self.loads += 1
            return () if case.startswith("no_current") else super().load_stack()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return () if case == "empty" else super().load_unstaged_changes()

        def move_patch_one_step(self, patch_name: str, *, toward_newer: bool) -> PatchMoveResult:
            if case == "patch_boundary":
                return PatchMoveResult.APPLICATION_BOUNDARY
            return PatchMoveResult.STACK_EDGE

    async def run_app() -> None:
        repository = StatusRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            if case.startswith("patch"):
                selected = tree.root.children[2].children[0]
            elif case.startswith("no_current"):
                selected = tree.root.children[1]
                if case.endswith("file"):
                    selected = selected.children[0]
            else:
                selected = tree.root.children[0]
            tree.move_cursor(selected)
            loads = repository.loads
            await pilot.press("ctrl+down" if case.startswith("no_current") else "ctrl+up")
            await pilot.pause()
            assert str(app.query_one("#status", Static).render()) == message
            assert repository.loads == loads
            assert tree.cursor_node is selected
            assert repository.actions == repository.moved_files == repository.spilled_files == []

    asyncio.run(run_app())


@pytest.mark.parametrize("scope", ["group", "directory", "file", "hunk"])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_discard_unstaged_selection_requires_confirmation(scope: str, reversed_order: bool) -> None:
    async def run_app() -> None:
        fixture = FixtureRepository()
        paths = ("dir/one.txt", "dir/nested/two.txt", "outside.txt")
        repository = MutableFixtureRepository(
            unstaged=tuple(fixture._file_change(path, "before", "after") for path in paths),
            staged=(fixture._file_change("staged.txt", "before", "after"),),
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            if reversed_order:
                await pilot.press("shift+r")
            group = next(
                node
                for node in tree.root.children
                if node.data is not None and node.data.is_unstaged
            )
            directory = next(
                node for node in group.children if node.data and node.data.is_directory
            )
            file = next(
                node
                for node in walk_nodes(directory)
                if node.data and node.data.is_file and node.data.file_path == paths[0]
            )
            file.expand()
            await pilot.pause()
            selected = {
                "group": group,
                "directory": directory,
                "file": file,
                "hunk": file.children[0],
            }[scope]
            tree.move_cursor(selected)
            await pilot.press("delete")
            assert isinstance(app.screen, DiscardChangesDialog)
            assert app.focused is app.screen.query_one("#cancel-delete")
            assert repository.actions == []
            await pilot.press("escape")
            assert repository.actions == []
            await pilot.press("delete")
            await pilot.click("#cancel-delete")
            assert repository.actions == []
            await pilot.press("delete")
            await pilot.click("#confirm-delete")
            await pilot.pause()
            expected = (
                paths if scope == "group" else paths[:2] if scope == "directory" else paths[:1]
            )
            operation = "discard_hunk" if scope == "hunk" else "discard_files"
            assert sorted(repository.actions) == sorted(
                (operation, path, None) for path in expected
            )
            assert selected.data is not None
            assert repository.discarded_hunks == (
                [(paths[0], selected.data.diff_text)] if scope == "hunk" else []
            )
            assert repository.staged[0].path == "staged.txt"
            assert not isinstance(app.screen, DiscardChangesDialog)

    asyncio.run(run_app())


def test_discard_error_preserves_selection_and_reports_status() -> None:
    class FailedDiscardRepository(FixtureRepository):
        def discard_files(self, paths: tuple[str, ...]) -> None:
            raise RepositoryError("discard failed")

    async def run_app() -> None:
        repository = FailedDiscardRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            selected = next(
                node
                for node in walk_nodes(tree.root)
                if node.data and node.data.is_unstaged and node.data.is_file
            )
            tree.move_cursor(selected)
            await pilot.press("delete")
            await pilot.click("#confirm-delete")
            await pilot.pause()
            assert tree.cursor_node is selected
            assert str(app.query_one("#status", Static).render()) == "Error: discard failed"
            assert repository.actions == []

    asyncio.run(run_app())


@pytest.mark.parametrize("spill", [False, True])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_hunk_moves_between_current_patch_and_staged_follow_selection(
    spill: bool, reversed_order: bool
) -> None:
    async def run_app() -> None:
        fixture = FixtureRepository()
        staged_hunks = tuple(
            fixture._file_change("shared.py", "old", text).hunks[0]
            for text in ("staged one", "staged two")
        )
        patch_hunks = tuple(
            fixture._file_change("shared.py", "old", text).hunks[0]
            for text in ("patch one", "patch two")
        )
        repository = MutableFixtureRepository(
            staged=(MutableFixtureRepository._file_with_hunks("shared.py", staged_hunks),),
            unstaged=(fixture._file_change("shared.py", "old", "unstaged"),),
        )
        repository.patch_files = [
            MutableFixtureRepository._file_with_hunks("shared.py", patch_hunks)
        ]
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            if reversed_order:
                await pilot.press("shift+r")
            source = next(
                node
                for node in walk_nodes(tree.root)
                if node.data
                and node.data.is_file
                and (node.data.patch_name is not None if spill else node.data.is_staged)
            )
            source.expand()
            await pilot.pause()
            selected = source.children[0]
            assert selected.data is not None
            key = selected.data.hunk_key
            tree.move_cursor(selected)
            newer = "ctrl+down" if reversed_order else "ctrl+up"
            older = "ctrl+up" if reversed_order else "ctrl+down"
            await pilot.press(newer if spill else older)
            await pilot.pause()
            assert tree.cursor_node is not None and tree.cursor_node.data is not None
            item = tree.cursor_node.data
            assert item.hunk_key == key
            assert item.is_staged if spill else item.patch_name == "types [notes]"
            assert len(repository.patch_files[0].hunks) == (1 if spill else 3)
            assert len(repository.staged[0].hunks) == (3 if spill else 1)
            assert repository.unstaged[0].hunks[0].diff_text.endswith("+unstaged\n")
            await pilot.press(older if spill else newer)
            await pilot.pause()
            assert len(repository.patch_files[0].hunks) == len(repository.staged[0].hunks) == 2
            assert tree.cursor_node is not None and tree.cursor_node.data is not None
            assert tree.cursor_node.data.hunk_key == key

    asyncio.run(run_app())


@pytest.mark.parametrize("under_home", [False, True])
@pytest.mark.parametrize("branch", ["main", None])
@pytest.mark.parametrize("remote_url", [None, "git@example.com:repo.git"])
def test_repository_root_and_remote_title(
    under_home: bool, branch: str | None, remote_url: str | None
) -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        repository.root = Path.home() / "dev/stg-layers" if under_home else Path("/tmp/repo [test]")
        repository.branch = branch
        repository.remote_url = remote_url
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            directory = "~/dev/stg-layers" if under_home else "/tmp/repo [test]"
            assert str(tree.root.label) == f"Repository {directory} [{branch or 'detached HEAD'}]"
            assert app.sub_title == (remote_url or "")
            await pilot.press("r")
            await pilot.pause()
            assert str(tree.root.label) == f"Repository {directory} [{branch or 'detached HEAD'}]"

    asyncio.run(run_app())


def test_diff_preview_resets_scroll_when_changing_items() -> None:
    class LongDiffRepository(FixtureRepository):
        def load_staged_changes(self) -> tuple[FileChange, ...]:
            text = "\n".join("+" + "x" * 200 for _ in range(100))
            return tuple(
                replace(self._file_change(path, "before", "after"), diff_text=text)
                for path in ("one.txt", "two.txt")
            )

    async def run_app() -> None:
        app = StgLayersApp(LongDiffRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            files = tree.root.children[1].children
            tree.focus()
            tree.move_cursor(files[0])
            await pilot.pause()
            for node in (files[1], files[0]):
                preview.scroll_to(x=40, y=40, animate=False, immediate=True)
                await pilot.pause()
                assert preview.scroll_x > 0 and preview.scroll_y > 0
                tree.move_cursor(node)
                await pilot.pause()
                assert preview.scroll_x == preview.scroll_y == 0
                assert tree.cursor_node is node

    asyncio.run(run_app())


async def _select_first_staged_file(app: StgLayersApp, pilot) -> tuple[Tree, TextArea]:
    tree = app.query_one("#patch-tree", Tree)
    tree.focus()
    file = next(
        node
        for node in walk_nodes(tree.root)
        if node.data and node.data.is_staged and node.data.is_file
    )
    file.expand()
    await pilot.pause()
    tree.move_cursor(file)
    await pilot.pause()
    return tree, app.query_one("#diff-view", TextArea)


def test_periodic_refresh_detects_changes_without_disturbing_unchanged_selection() -> None:
    async def run_app() -> None:
        fixture = FixtureRepository()
        long_file = replace(
            fixture._file_change("selected.txt", "old", "new"),
            diff_text="\n".join("+" + "x" * 200 for _ in range(100)),
        )
        repository = MutableFixtureRepository(staged=(long_file,))
        app = StgLayersApp(repository)
        app.AUTO_REFRESH_INTERVAL = 0.1
        async with app.run_test() as pilot:
            await pilot.pause()
            tree, preview = await _select_first_staged_file(app, pilot)
            preview.move_cursor((35, 0))
            line_selection = preview.selection
            preview.scroll_to(x=30, y=30, animate=False, immediate=True)
            await pilot.pause()
            offset = preview.scroll_offset
            repository.staged.append(fixture._file_change("new.txt", "old", "new"))
            await pilot.pause(0.4)
            assert any(
                node.data and node.data.file_path == "new.txt" for node in walk_nodes(tree.root)
            )
            assert tree.cursor_node is not None and tree.cursor_node.data is not None
            assert tree.cursor_node.data.file_path == "selected.txt"
            assert tree.cursor_node.is_expanded
            assert preview.scroll_offset == offset
            selected = tree.cursor_node
            app._show_status("Keep this status")
            await pilot.pause(0.3)
            assert tree.cursor_node is selected
            assert str(app.query_one("#status", Static).render()) == "Keep this status"
            repository.staged[0] = replace(long_file, diff_text=long_file.diff_text + "\n+new line")
            await pilot.pause(0.4)
            assert preview.text.endswith("+new line")
            assert preview.selection == line_selection
            assert preview.scroll_offset == offset

    asyncio.run(run_app())


@pytest.mark.parametrize("reason", ["dialog", "operation", "pending", "worker"])
def test_automatic_refresh_waits_until_idle(reason: str) -> None:
    async def run_app() -> None:
        repository = MutableFixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            original = app._loaded_data
            repository.staged.append(repository._file_change("new.txt", "old", "new"))
            release = asyncio.Event()
            if reason == "dialog":
                app.push_screen(DeletePatchDialog("example"))
                await pilot.pause()
            elif reason == "pending":
                repository.pending_stash_reference = "saved"
            elif reason == "worker":
                app.run_worker(release.wait())
            else:
                app._repository_busy += 1
            await app._refresh_changed_repository()
            assert app._loaded_data == original
            if reason == "dialog":
                await pilot.press("escape")
            elif reason == "pending":
                repository.pending_stash_reference = None
            elif reason == "worker":
                release.set()
                await app.workers.wait_for_complete()
            else:
                app._repository_busy -= 1
            await app._refresh_changed_repository()
            assert app._loaded_data != original

    asyncio.run(run_app())


def test_auto_refresh_error_keeps_tree_and_retries() -> None:
    class FailingRefreshRepository(MutableFixtureRepository):
        fail = False

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            if self.fail:
                raise RepositoryError("temporary read failure")
            return super().load_staged_changes()

    async def run_app() -> None:
        repository = FailingRefreshRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            nodes = list(tree.root.children)
            repository.fail = True
            await app._refresh_changed_repository()
            assert list(tree.root.children) == nodes
            assert "temporary read failure" in str(app.query_one("#status", Static).render())
            assert app._repository_busy == 0 and app._auto_refresh_done.is_set()
            repository.fail = False
            repository.staged.append(repository._file_change("new.txt", "old", "new"))
            await app._refresh_changed_repository()
            assert any(
                node.data and node.data.file_path == "new.txt" for node in walk_nodes(tree.root)
            )

    asyncio.run(run_app())


def test_refresh_detects_external_git_and_worktree_changes(tmp_path: Path) -> None:
    git = Git(CommandRunner(tmp_path))
    git.commands.run("git", "init", "-q")
    git.commands.run("git", "config", "user.name", "Test")
    git.commands.run("git", "config", "user.email", "test@example.invalid")
    path = tmp_path / "file.txt"
    path.write_text("before\n")
    git.stage_files((path.name,))
    git.commands.run("git", "commit", "-qm", "base")
    git.commands.run("stg", "init")
    path.write_text("dirty one\n")

    async def run_app() -> None:
        app = StgLayersApp(StGitRepository.discover(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            node = next(node for node in walk_nodes(tree.root) if node.data and node.data.is_file)
            tree.move_cursor(node)
            await pilot.pause()
            path.write_text("dirty two\n")
            await app._refresh_changed_repository()
            await pilot.pause()
            assert "+dirty two" in app.query_one("#diff-view", TextArea).text
            git.stage_files((path.name,))
            await app._refresh_changed_repository()
            assert app._loaded_data is not None
            assert len(app._loaded_data[1]) == 1 and not app._loaded_data[2]
            git.commands.run("git", "checkout", "-b", "switched")
            git.commands.run("stg", "init")
            git.commands.run("git", "remote", "add", "origin", "git@example.com:repo.git")
            await app._refresh_changed_repository()
            assert "[switched]" in str(tree.root.label)
            assert app.sub_title == "git@example.com:repo.git"
            git.commands.run("stg", "new", "-m", "External patch", "external")
            git.commands.run("stg", "refresh", "--index")
            await app._refresh_changed_repository()
            assert app._loaded_data is not None
            assert app._loaded_data[0][0].name == "external"
            assert app._loaded_data[1] == ()
            git.commands.run("stg", "pop")
            await app._refresh_changed_repository()
            assert app._loaded_data is not None
            assert app._loaded_data[0][0].status == PatchStatus.UNAPPLIED

    asyncio.run(run_app())


def test_repository_operation_waits_for_background_refresh() -> None:
    class SlowRepository(MutableFixtureRepository):
        pause_load = False
        started = threading.Event()
        release = threading.Event()

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            if self.pause_load:
                self.started.set()
                assert self.release.wait(timeout=5)
            return super().load_staged_changes()

    async def run_app() -> None:
        repository = SlowRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            repository.pause_load = True
            refresh = asyncio.create_task(app._refresh_changed_repository())
            assert await asyncio.to_thread(repository.started.wait, 5)
            ready = asyncio.create_task(app._ensure_repository_ready())
            await asyncio.sleep(0)
            assert not ready.done()
            repository.release.set()
            await refresh
            assert await ready
            assert app._repository_busy == 0

    asyncio.run(run_app())


def test_diff_preview_arrows_move_cursor_and_shift_arrows_select() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            selected = tree.cursor_node
            preview = app.query_one("#diff-view", TextArea)
            text = "zero\none\ntwo\nthree\nfour\nfive"
            preview.load_text(text)
            preview.focus()
            await pilot.pause()
            assert preview.show_cursor and preview.highlight_cursor_line and preview.read_only
            await pilot.press("down", "down")
            assert preview.selected_text == "two"
            await pilot.press("shift+down", "shift+down")
            assert preview.selection.start == (2, 0)
            assert preview.selection.end == (4, 4)
            assert preview.selected_text == "two\nthree\nfour"
            await pilot.press("shift+up")
            assert preview.selected_text == "two\nthree"
            await pilot.press("up")
            assert preview.selected_text == "two"
            await pilot.press("home", "right", "shift+right")
            assert preview.selected_text == "two\nthree"
            await pilot.press("shift+left")
            assert preview.selected_text == "two"
            await pilot.press("a", "backspace", "delete")
            assert preview.text == text
            assert preview.has_focus
            assert tree.cursor_node is selected

    asyncio.run(run_app())


@pytest.mark.parametrize("soft_wrap", [False, True])
def test_diff_preview_selects_whole_lines_at_boundaries_and_in_reverse(soft_wrap: bool) -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            preview = app.query_one("#diff-view", TextArea)
            lines = ["first", "long " * 50, "", "last"]
            preview.load_text("\n".join(lines))
            preview.soft_wrap = soft_wrap
            preview.focus()
            await pilot.pause()
            preview.move_cursor((1, 5))
            await pilot.press("shift+down")
            assert preview.selected_text == lines[1] + "\n"
            await pilot.press("shift+down")
            assert preview.selected_text == lines[1] + "\n\nlast"
            await pilot.press("shift+up", "shift+up")
            assert preview.selected_text == lines[1]
            await pilot.press("shift+up")
            assert preview.selected_text == "first\n" + lines[1]
            await pilot.press("shift+down")
            assert preview.selected_text == lines[1]
            preview.move_cursor((3, 2))
            await pilot.press("shift+down", "shift+down")
            assert preview.selected_text == "last"
            await pilot.press("shift+up")
            assert preview.selected_text == "\nlast"
            # Mouse drags, word selection and other inherited selection actions also snap.
            preview.selection = Selection((0, 2), (1, 3))
            assert preview.selected_text == "first\n" + lines[1]
            preview.selection = Selection((1, 3), (0, 2))
            assert preview.selected_text == "first\n" + lines[1]
            assert preview.selection.start == (1, len(lines[1]))
            assert preview.selection.end == (0, 0)
            await pilot.press("backspace", "delete", "a")
            assert preview.text == "\n".join(lines)

    asyncio.run(run_app())


@pytest.mark.parametrize("soft_wrap", [False, True])
def test_diff_preview_always_selects_current_line(soft_wrap: bool) -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            preview = app.query_one("#diff-view", TextArea)
            lines = ["first", "long " * 50, "last"]
            preview.load_text("\n".join(lines))
            preview.soft_wrap = soft_wrap
            preview.focus()
            await pilot.pause()
            assert preview.selected_text == "first"
            await pilot.press("up")
            assert preview.selected_text == "first"
            await pilot.press("down")
            assert preview.selected_text == lines[1]
            await pilot.press("shift+down")
            assert preview.selected_text == lines[1] + "\nlast"
            await pilot.press("shift+up")
            assert preview.selected_text == lines[1]
            await pilot.press("shift+up")
            assert preview.selected_text == "first\n" + lines[1]
            await pilot.press("shift+down")
            assert preview.selected_text == lines[1]
            await pilot.press("down", "down")
            assert preview.selected_text == "last"
            preview.move_cursor((0, 2))
            assert preview.selected_text == "first"
            preview.load_text("replacement\nsecond")
            assert preview.selected_text == "replacement"
            preview.load_text("")
            await pilot.press("up", "down", "shift+up", "shift+down")
            assert preview.text == ""

    asyncio.run(run_app())


@pytest.fixture
def line_repository(tmp_path: Path) -> StGitRepository:
    git = Git(CommandRunner(tmp_path))
    git.commands.run("git", "init", "-q")
    git.commands.run("git", "config", "user.name", "Test")
    git.commands.run("git", "config", "user.email", "test@example.invalid")
    (tmp_path / "src").mkdir()
    path = tmp_path / "src/tracked.txt"
    base = "".join(f"line {i}\n" for i in range(100))
    path.write_text(base)
    (tmp_path / "other.txt").write_text("other\n")
    git.stage_files(("src/tracked.txt", "other.txt"))
    git.commands.run("git", "commit", "-qm", "base")
    git.commands.run("stg", "init")
    path.write_text(
        base.replace("line 10\nline 11\n", "new 10\nnew 11\n").replace("line 70\n", "new 70\n")
    )
    (tmp_path / "other.txt").write_text("other changed\n")
    return StGitRepository.discover(tmp_path)


@pytest.mark.parametrize("stage", [False, True])
@pytest.mark.parametrize("scope", ["group", "directory", "file", "hunk"])
def test_preview_keys_move_selected_lines_only(
    line_repository: StGitRepository, stage: bool, scope: str
) -> None:
    repository = line_repository
    git = repository.git
    path = repository.root / "src/tracked.txt"
    worktree = path.read_text()
    base = git.commands.run("git", "show", "HEAD:src/tracked.txt")
    if not stage:
        git.stage_files(("src/tracked.txt", "other.txt"))
    other_index = git.commands.run("git", "show", ":other.txt")

    async def run_app() -> None:
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            area = "unstaged" if stage else "staged"
            candidates = [
                node
                for node in walk_nodes(tree.root)
                if node.data and node.data.change_area == area
            ]
            selected = next(
                node
                for node in candidates
                if node.data
                and {
                    "group": node.data.is_change_group,
                    "directory": node.data.is_directory,
                    "file": node.data.is_file and node.data.file_path == "src/tracked.txt",
                    "hunk": node.data.is_hunk and node.data.file_path == "src/tracked.txt",
                }[scope]
            )
            if selected.parent is not None:
                selected.parent.expand()
            tree.focus()
            await pilot.pause()
            tree.move_cursor(selected)
            await pilot.pause()
            preview = app.query_one("#diff-view", TextArea)
            if scope in {"group", "directory"}:
                preview.focus()
                index = git.commands.run("git", "write-tree")
                await pilot.press("s", "d")
                assert git.commands.run("git", "write-tree") == index
                return
            row = preview.text.splitlines().index("+new 10")
            preview.focus()
            preview.move_cursor((row, 0))
            index = git.commands.run("git", "write-tree")
            await pilot.press("d" if stage else "s")
            assert git.commands.run("git", "write-tree") == index
            if stage:
                assert isinstance(app.screen, DiscardChangesDialog)
                await pilot.press("escape")
            await pilot.press("s" if stage else "d")
            await pilot.pause()
            expected = (
                base.replace("line 11\n", "line 11\nnew 10\n")
                if stage
                else worktree.replace("new 10\n", "")
            )
            assert git.commands.run("git", "show", ":src/tracked.txt") == expected
            assert git.commands.run("git", "show", ":other.txt") == other_index
            assert path.read_text() == worktree
            assert preview.has_focus

    asyncio.run(run_app())


def test_preview_line_selection_rejects_multiple_hunks_and_supports_reverse_selection(
    line_repository: StGitRepository,
) -> None:
    repository = line_repository
    git = repository.git

    async def run_app() -> None:
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            selected = next(
                node
                for node in walk_nodes(tree.root)
                if node.data and node.data.is_file and node.data.file_path == "src/tracked.txt"
            )
            tree.focus()
            tree.move_cursor(selected)
            await pilot.pause()
            preview = app.query_one("#diff-view", TextArea)
            preview.focus()
            lines = preview.text.splitlines()
            first = lines.index("+new 10")
            second = lines.index("+new 70")
            preview.selection = Selection((first, 0), (second, 0))
            index = git.commands.run("git", "write-tree")
            await pilot.press("s")
            assert "single diff hunk" in str(app.query_one("#status", Static).render())
            assert git.commands.run("git", "write-tree") == index
            await pilot.press("d")
            assert not isinstance(app.screen, DiscardChangesDialog)
            assert git.commands.run("git", "write-tree") == index
            preview.move_cursor(
                (next(i for i, line in enumerate(lines) if line.startswith("@@")), 0)
            )
            await pilot.press("d")
            assert not isinstance(app.screen, DiscardChangesDialog)
            assert "Select added or removed" in str(app.query_one("#status", Static).render())
            assert git.commands.run("git", "write-tree") == index
            preview.selection = Selection((first + 1, 0), (first, 0))
            await pilot.press("s")
            staged = git.commands.run("git", "show", ":src/tracked.txt")
            assert "line 10\nline 11\nnew 10\nnew 11\n" in staged
            assert "new 70" not in staged

    asyncio.run(run_app())


@pytest.mark.parametrize("scope", ["file", "hunk"])
def test_preview_discard_selected_lines_confirms_and_preserves_staged_changes(
    line_repository: StGitRepository, scope: str
) -> None:
    repository = line_repository
    git = repository.git
    path = repository.root / "src/tracked.txt"
    worktree = path.read_text().replace("line 30\n", "staged 30\n")
    base = git.commands.run("git", "show", "HEAD:src/tracked.txt")
    path.write_text(base.replace("line 30\n", "staged 30\n"))
    git.stage_files(("src/tracked.txt",))
    path.write_text(worktree)
    index = git.commands.run("git", "write-tree")

    async def run_app() -> None:
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            node = next(
                node
                for node in walk_nodes(tree.root)
                if node.data
                and node.data.is_unstaged
                and node.data.file_path == "src/tracked.txt"
                and (node.data.is_file if scope == "file" else node.data.is_hunk)
            )
            if node.parent is not None:
                node.parent.expand()
            tree.focus()
            await pilot.pause()
            tree.move_cursor(node)
            await pilot.pause()
            preview = app.query_one("#diff-view", TextArea)
            row = preview.text.splitlines().index("+new 10")
            preview.focus()
            preview.move_cursor((row, 0))
            for cancel in ("escape", "button"):
                await pilot.press("d")
                assert isinstance(app.screen, DiscardChangesDialog)
                assert app.focused is app.screen.query_one("#cancel-delete")
                assert path.read_text() == worktree
                if cancel == "escape":
                    await pilot.press("escape")
                else:
                    await pilot.click("#cancel-delete")
                assert path.read_text() == worktree
                assert git.commands.run("git", "write-tree") == index
            await pilot.press("d")
            summary = app.screen.query_one("#delete-stat", VerticalScroll).query_one(Static)
            assert "1 insertion(+)" in str(summary.render())
            assert "deletion" not in str(summary.render())
            # The confirmation refers to the original selection, even if the cursor changes.
            preview.move_cursor((row + 1, 0))
            await pilot.click("#confirm-delete")
            await pilot.pause()
            assert path.read_text() == worktree.replace("new 10\n", "")
            assert git.commands.run("git", "write-tree") == index
            assert preview.has_focus

    asyncio.run(run_app())


def test_preview_discard_selected_lines_rechecks_after_confirmation(
    line_repository: StGitRepository,
) -> None:
    repository = line_repository
    git = repository.git
    path = repository.root / "src/tracked.txt"

    async def run_app() -> None:
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            node = next(
                node
                for node in walk_nodes(tree.root)
                if node.data and node.data.is_file and node.data.file_path == "src/tracked.txt"
            )
            tree.focus()
            tree.move_cursor(node)
            await pilot.pause()
            preview = app.query_one("#diff-view", TextArea)
            preview.focus()
            preview.move_cursor((preview.text.splitlines().index("+new 10"), 0))
            await pilot.press("d")
            assert isinstance(app.screen, DiscardChangesDialog)
            newer = path.read_text().replace("new 10\n", "newer 10\n")
            path.write_text(newer)
            index = git.commands.run("git", "write-tree")
            await pilot.click("#confirm-delete")
            await pilot.pause()
            assert path.read_text() == newer
            assert git.commands.run("git", "write-tree") == index
            assert "no longer present" in str(app.query_one("#status", Static).render())

    asyncio.run(run_app())


@pytest.mark.parametrize("discard", [False, True])
def test_delete_confirmation_scrolls_long_diffstat(discard: bool) -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            summary = "\n".join(f" dir/{'long' * 25}{i}.txt | 1 +" for i in range(100))
            dialog = DiscardChangesDialog if discard else DeletePatchDialog
            app.push_screen(dialog("example", diffstat=summary))
            await pilot.pause()
            stat = app.screen.query_one("#delete-stat", VerticalScroll)
            assert stat.max_scroll_y > 0
            assert stat.max_scroll_x > 0
            container = app.screen.query_one("#delete-dialog")
            button = app.screen.query_one("#confirm-delete", Button)
            assert button.region.bottom <= container.region.bottom
            assert app.focused is app.screen.query_one("#cancel-delete")
            assert await pilot.click("#cancel-delete")
            assert not isinstance(app.screen, DeletePatchDialog)

    asyncio.run(run_app())



def test_delete_summary_error_does_not_open_confirmation_or_delete_patch() -> None:
    class FailedStatRepository(FixtureRepository):
        def patch_diffstat(self, patch_name: str) -> str:
            raise RepositoryError("summary unavailable")

    async def run_app() -> None:
        repository = FailedStatRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.focus()
            tree.move_cursor(tree.root.children[2].children[0])
            await pilot.press("delete")
            assert not isinstance(app.screen, DeletePatchDialog)
            assert repository.patch_operations == []
            assert "summary unavailable" in str(app.query_one("#status", Static).render())

    asyncio.run(run_app())
