from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from stg_layers.commands import CommandRunner
from stg_layers.errors import RepositoryError
from stg_layers.git import Git
from stg_layers.models import FileChange, Patch, PatchStatus
from stg_layers.stg import StGit


class PatchMoveResult(StrEnum):
    MOVED = "moved"
    STACK_EDGE = "stack_edge"
    APPLICATION_BOUNDARY = "application_boundary"


class PatchChangeMoveResult(StrEnum):
    MOVED = "moved"
    STACK_EDGE = "stack_edge"
    APPLICATION_BOUNDARY = "application_boundary"
    SOURCE_NOT_APPLIED = "source_not_applied"


_T = TypeVar("_T")


class StGitRepository:
    def __init__(self, root: Path, branch: str | None = None) -> None:
        self.root = root
        self.commands = CommandRunner(root)
        self.git = Git(self.commands)
        self.stg = StGit(self.commands)
        self.branch = branch
        self._pending_stack_operation = False
        self._pending_stack_stash: str | None = None
        self._pending_stash_restore_conflict = False
        self._pending_stack_undo_count = 0

    @classmethod
    def discover(cls, cwd: Path) -> StGitRepository:
        git = Git.discover(cwd)
        return cls(git.root, git.current_branch())

    def has_conflicts(self) -> bool:
        return self.git.has_conflicts()

    def has_local_changes(self) -> bool:
        return self.git.has_local_changes()

    def _ensure_patch_stack_is_safe(self) -> None:
        if self._pending_stack_operation:
            raise RepositoryError(
                "Cannot modify the patch stack while a previous stack operation is awaiting "
                "Undo or Refresh."
            )
        if self.has_conflicts():
            raise RepositoryError(
                "Cannot modify the patch stack while the repository has merge conflicts."
            )

    @property
    def stack_operation_pending(self) -> bool:
        return self._pending_stack_operation

    @property
    def stack_operation_stash_reference(self) -> str | None:
        return self._pending_stack_stash

    @property
    def stash_restore_conflict(self) -> bool:
        return self._pending_stash_restore_conflict

    def _stash_current_changes(self) -> str | None:
        new_stash = self.git.stash()
        if new_stash is None:
            return None
        if self.has_local_changes():
            try:
                self._restore_stash(new_stash)
            except RepositoryError as restore_error:
                self._pending_stack_operation = True
                self._pending_stack_stash = new_stash
                self._pending_stash_restore_conflict = True
                raise RepositoryError(
                    f"Git created stash {new_stash}, but local changes remain and could not be "
                    f"restored: {restore_error}"
                ) from restore_error
            raise RepositoryError(
                f"Git created stash {new_stash}, but local changes remain. "
                "The stack operation was not run."
            )
        return new_stash

    def _restore_stash(self, stash_commit: str) -> None:
        try:
            self.git.apply_stash(stash_commit)
        except RepositoryError:
            self._pending_stack_stash = stash_commit
            self._pending_stash_restore_conflict = True
            raise
        self._pending_stack_stash = None
        self._pending_stash_restore_conflict = False
        try:
            self.git.drop_stash(stash_commit)
        except RepositoryError as error:
            raise RepositoryError(
                f"Local changes were restored, but the recovery stash {stash_commit} remains "
                "in the stash list."
            ) from error

    def _run_stack_transaction(self, operation: Callable[[StGit], _T]) -> _T:
        self._ensure_patch_stack_is_safe()
        stash_commit = self._stash_current_changes()
        undo_count = 0

        def record_mutation(steps: int, error: RepositoryError | None) -> None:
            nonlocal undo_count
            if error is None or self.has_conflicts():
                undo_count += steps

        stg = StGit(self.commands, after_mutation=record_mutation)
        try:
            result = operation(stg)
        except RepositoryError as error:
            self._recover_failed_stack_transaction(error, stash_commit, undo_count)
            raise

        self._pending_stack_undo_count = undo_count
        if stash_commit is not None:
            self._restore_stash_after_stack_transaction(stash_commit)
        self._pending_stack_operation = False
        self._pending_stack_stash = None
        self._pending_stash_restore_conflict = False
        self._pending_stack_undo_count = 0
        return result

    def _recover_failed_stack_transaction(
        self,
        operation_error: RepositoryError,
        stash_commit: str | None,
        undo_count: int,
    ) -> None:
        if self.has_conflicts():
            self._pending_stack_operation = True
            self._pending_stack_stash = stash_commit
            self._pending_stash_restore_conflict = False
            self._pending_stack_undo_count = undo_count
            return

        try:
            while undo_count:
                self.stg.undo()
                undo_count -= 1
        except RepositoryError as undo_error:
            self._pending_stack_operation = True
            self._pending_stack_stash = stash_commit
            self._pending_stash_restore_conflict = False
            self._pending_stack_undo_count = undo_count
            raise RepositoryError(
                f"The stack operation failed, and reverting its completed steps also failed: "
                f"{undo_error}"
            ) from operation_error

        self._pending_stack_undo_count = 0
        if stash_commit is None:
            return
        try:
            self._restore_stash(stash_commit)
        except RepositoryError as restore_error:
            if self._pending_stack_stash is None:
                self._pending_stack_operation = False
                raise restore_error from operation_error
            self._pending_stack_operation = True
            self._pending_stash_restore_conflict = True
            raise RepositoryError(
                f"The stack operation failed, and restoring saved local changes also failed: "
                f"{restore_error}"
            ) from operation_error

    def _restore_stash_after_stack_transaction(self, stash_commit: str) -> None:
        try:
            self._restore_stash(stash_commit)
        except RepositoryError as error:
            if self._pending_stack_stash is None:
                self._pending_stack_operation = False
                raise
            self._pending_stack_operation = True
            self._pending_stash_restore_conflict = True
            raise RepositoryError(
                f"The stack operation completed, but restoring saved local changes failed: {error}"
            ) from error

    def move_patch_one_step(
        self,
        patch_name: str,
        *,
        toward_newer: bool,
    ) -> PatchMoveResult:
        self._ensure_patch_stack_is_safe()
        series = self.stg.series()
        patch_names = [name for name, _, _ in series]
        try:
            patch_index = patch_names.index(patch_name)
        except ValueError as error:
            raise RepositoryError(f"Patch {patch_name!r} is not in the StGit stack.") from error

        neighbor_index = patch_index + (1 if toward_newer else -1)
        if neighbor_index < 0 or neighbor_index >= len(series):
            return PatchMoveResult.STACK_EDGE

        patch_status = series[patch_index][1]
        neighbor_status = series[neighbor_index][1]
        if patch_status in {PatchStatus.APPLIED, PatchStatus.CURRENT} and neighbor_status in {
            PatchStatus.APPLIED,
            PatchStatus.CURRENT,
        }:
            neighbor = series[neighbor_index][0]
            self._run_stack_transaction(
                lambda stg: stg.sink(patch_name, neighbor, above=toward_newer)
            )
            return PatchMoveResult.MOVED

        if patch_status == PatchStatus.UNAPPLIED and neighbor_status == PatchStatus.UNAPPLIED:
            patch_names[patch_index], patch_names[neighbor_index] = (
                patch_names[neighbor_index],
                patch_names[patch_index],
            )
            # Float prepends to the unapplied group, so include the whole group
            # to preserve patches before the swapped pair (and exclude hidden patches).
            reordered_names = [
                name
                for name, (_, status, _) in zip(patch_names, series, strict=True)
                if status == PatchStatus.UNAPPLIED
            ]
            self._run_stack_transaction(lambda stg: stg.float_unapplied(tuple(reordered_names)))
            return PatchMoveResult.MOVED

        return PatchMoveResult.APPLICATION_BOUNDARY

    def move_patch_change(
        self,
        patch_name: str,
        path: str,
        *,
        toward_newer: bool,
        hunk_diff: str | None = None,
    ) -> tuple[PatchChangeMoveResult, str | None]:
        return self._move_patch_changes(patch_name, ((path, hunk_diff),), toward_newer=toward_newer)

    def move_patch_changes(
        self, patch_name: str, paths: tuple[str, ...], *, toward_newer: bool
    ) -> tuple[PatchChangeMoveResult, str | None]:
        return self._move_patch_changes(
            patch_name, tuple((path, None) for path in paths), toward_newer=toward_newer
        )

    def _move_patch_changes(
        self,
        patch_name: str,
        changes: tuple[tuple[str, str | None], ...],
        *,
        toward_newer: bool,
    ) -> tuple[PatchChangeMoveResult, str | None]:
        self._ensure_patch_stack_is_safe()
        series = self.stg.series()
        move_result, target_name, patch_index = self._patch_change_destination(
            series,
            patch_name,
            toward_newer,
        )
        if move_result != PatchChangeMoveResult.MOVED:
            return move_result, None
        if target_name is None:
            raise RepositoryError("StGit did not identify an adjacent destination patch.")

        source_patch = next(
            (patch for patch in self.load_stack() if patch.name == patch_name),
            None,
        )
        if source_patch is None:
            raise RepositoryError(f"Patch {patch_name!r} disappeared from the StGit stack.")
        selected_diffs = [
            (path, self._selected_patch_change_diff(source_patch, patch_name, path, hunk_diff))
            for path, hunk_diff in changes
        ]
        popped_patches = tuple(
            name
            for name, status, _ in series[patch_index + 1 :]
            if status in {PatchStatus.APPLIED, PatchStatus.CURRENT}
        )

        def move_all(stg: StGit) -> None:
            for path, selected_diff in selected_diffs:
                self._move_patch_change_sequence(
                    stg, patch_name, path, selected_diff, popped_patches, toward_newer
                )

        if selected_diffs:
            self._run_stack_transaction(move_all)
        return PatchChangeMoveResult.MOVED, target_name

    @staticmethod
    def _patch_change_destination(
        series: list[tuple[str, PatchStatus, str]],
        patch_name: str,
        toward_newer: bool,
    ) -> tuple[PatchChangeMoveResult, str | None, int]:
        patch_names = [name for name, _, _ in series]
        try:
            patch_index = patch_names.index(patch_name)
        except ValueError as error:
            raise RepositoryError(f"Patch {patch_name!r} is not in the StGit stack.") from error
        if series[patch_index][1] not in {PatchStatus.APPLIED, PatchStatus.CURRENT}:
            return PatchChangeMoveResult.SOURCE_NOT_APPLIED, None, patch_index

        neighbor_index = patch_index + (1 if toward_newer else -1)
        if neighbor_index < 0 or neighbor_index >= len(series):
            return PatchChangeMoveResult.STACK_EDGE, None, patch_index
        target_name, target_status, _ = series[neighbor_index]
        if target_status not in {PatchStatus.APPLIED, PatchStatus.CURRENT}:
            return PatchChangeMoveResult.APPLICATION_BOUNDARY, None, patch_index
        return PatchChangeMoveResult.MOVED, target_name, patch_index

    def _selected_patch_change_diff(
        self,
        source_patch: Patch,
        patch_name: str,
        path: str,
        hunk_diff: str | None,
    ) -> str:
        source_file = next((file for file in source_patch.files if file.path == path), None)
        if source_file is None:
            raise RepositoryError(f"File {path!r} is no longer present in patch {patch_name!r}.")
        selected_diff = source_file.diff_text
        if hunk_diff is not None:
            hunk = next(
                (hunk for hunk in source_file.hunks if hunk.diff_text == hunk_diff),
                None,
            )
            if hunk is None:
                raise RepositoryError(
                    f"The selected hunk in {path!r} is no longer present in patch {patch_name!r}."
                )
            header_end = selected_diff.find("@@")
            if header_end < 0:
                raise RepositoryError(f"Patch {patch_name!r} has no text diff for {path!r}.")
            selected_diff = selected_diff[:header_end] + hunk.diff_text
        elif not source_file.hunks:
            selected_diff = self.stg.file_diff(patch_name, path)
        if not selected_diff:
            raise RepositoryError(f"Patch {patch_name!r} has no applicable diff for {path!r}.")
        return selected_diff

    def _move_patch_change_sequence(
        self,
        stg: StGit,
        source_name: str,
        path: str,
        selected_diff: str,
        popped_patches: tuple[str, ...],
        toward_newer: bool,
    ) -> None:
        for popped_name in reversed(popped_patches):
            stg.pop(popped_name)

        stg.spill((path,))
        self.git.apply_index_diff(selected_diff, reverse=True)
        stg.refresh()
        self.git.restore_worktree(path)

        if not toward_newer:
            stg.pop(source_name)
            self.git.apply_index_diff(selected_diff)
            stg.refresh()
            self.git.restore_worktree(path)
            stg.push(source_name)

        for popped_name in popped_patches:
            stg.push(popped_name, set_tree=True)

    def patch_operation(self, operation: str, patch_name: str | None = None) -> None:
        if operation not in {"push", "pop", "commit", "uncommit", "hide", "unhide", "delete"}:
            raise RepositoryError(f"Unknown patch operation {operation!r}.")
        if operation == "uncommit" and patch_name is not None:
            raise RepositoryError("Operation 'uncommit' requires no patch name.")
        if operation != "uncommit" and patch_name is None:
            raise RepositoryError(f"Operation {operation!r} requires a patch name.")

        def run(stg: StGit) -> None:
            if (
                operation == "hide"
                and patch_name is not None
                and any(
                    name == patch_name and status in {PatchStatus.APPLIED, PatchStatus.CURRENT}
                    for name, status, _ in stg.series()
                )
            ):
                # Hide alone changes metadata without updating the worktree.
                stg.pop(patch_name)
            method = getattr(stg, operation)
            if patch_name is None:
                method()
            else:
                method(patch_name)

        self._run_stack_transaction(run)

    def undo_stack_operation(self) -> None:
        while self._pending_stack_undo_count:
            self.stg.undo()
            self._pending_stack_undo_count -= 1
        if self._pending_stack_stash is not None:
            stash_commit = self._pending_stack_stash
            try:
                self._restore_stash(stash_commit)
            except RepositoryError:
                if self._pending_stack_stash is None:
                    self._pending_stack_operation = False
                    raise
                self._pending_stack_operation = True
                self._pending_stash_restore_conflict = True
                raise
        self._pending_stack_operation = False
        self._pending_stack_stash = None
        self._pending_stash_restore_conflict = False
        self._pending_stack_undo_count = 0

    def refresh_stack_operation(self) -> str | None:
        if self.has_conflicts():
            raise RepositoryError("Resolve merge conflicts in the repository before refreshing.")
        if self._pending_stack_stash is None:
            self._pending_stack_operation = False
            self._pending_stash_restore_conflict = False
            self._pending_stack_undo_count = 0
            return None

        stash_commit = self._pending_stack_stash
        if self._pending_stash_restore_conflict:
            self._pending_stack_stash = None
            self._pending_stash_restore_conflict = False
            self._pending_stack_operation = False
            self._pending_stack_undo_count = 0
            return stash_commit

        try:
            self._restore_stash(stash_commit)
        except RepositoryError:
            self._pending_stash_restore_conflict = True
            raise
        self._pending_stack_operation = False
        self._pending_stack_undo_count = 0
        return None

    def stage_files(self, paths: tuple[str, ...]) -> None:
        self.git.stage_files(paths)

    def unstage_files(self, paths: tuple[str, ...]) -> None:
        self.git.unstage_files(paths)

    def new_patch_template(self) -> str:
        return self.stg.new_patch_template()

    def create_patch(self, message: str) -> None:
        self.stg.create_patch(message)

    def edit_patch_template(self, patch_name: str) -> str:
        return self.stg.edit_patch_template(patch_name)

    def edit_patch(self, patch_name: str, message: str) -> None:
        self.stg.edit_patch(patch_name, message)

    def _save_other_staged_changes(
        self,
        selected_paths: set[str],
        staged_paths: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        return [
            (path, diff)
            for path in staged_paths
            if path not in selected_paths
            if (diff := self.git.staged_file_diff(path))
        ]

    def _restore_staged_changes(self, changes: list[tuple[str, str]]) -> None:
        failed_paths: list[str] = []
        for path, diff in changes:
            try:
                self.git.apply_index_diff(diff)
            except RepositoryError:
                failed_paths.append(path)
        if failed_paths:
            paths = ", ".join(repr(path) for path in failed_paths)
            raise RepositoryError(
                f"Could not restore staged changes for {paths} after refreshing the current "
                "patch. Those changes remain unstaged."
            )

    def refresh_staged_files_to_current_patch(self, paths: tuple[str, ...]) -> None:
        self._ensure_patch_stack_is_safe()
        if not paths:
            return
        path_groups = self.git.staged_path_groups()
        selected_paths = set(paths)
        for path in paths:
            for group in path_groups:
                if path in group:
                    selected_paths.update(group)
                    break
            else:
                raise RepositoryError(f"No staged changes remain for {path!r}.")

        staged_paths = tuple(
            staged_path
            for group in path_groups
            for staged_path in group
            if staged_path not in selected_paths
        )
        saved_changes = self._save_other_staged_changes(selected_paths, staged_paths)
        restored_paths: list[tuple[str, str]] = []
        try:
            for change in saved_changes:
                self.git.unstage_files((change[0],))
                restored_paths.append(change)
            self.stg.refresh()
        except RepositoryError as error:
            try:
                self._restore_staged_changes(restored_paths)
            except RepositoryError as restore_error:
                raise RepositoryError(f"{error} {restore_error}") from restore_error
            raise

        self._restore_staged_changes(saved_changes)

    def spill_current_patch_files(self, paths: tuple[str, ...]) -> None:
        self._ensure_patch_stack_is_safe()
        if not paths:
            return
        path_groups = self.git.staged_path_groups()
        for path in paths:
            if any(path in group for group in path_groups):
                raise RepositoryError(
                    f"Cannot spill {path!r} while it has staged changes. Unstage those changes "
                    "first so they are not mixed with the patch contents."
                )

        staged_paths = tuple(staged_path for group in path_groups for staged_path in group)
        saved_changes = self._save_other_staged_changes(set(), staged_paths)
        unstaged_changes: list[tuple[str, str]] = []
        try:
            for change in saved_changes:
                self.git.unstage_files((change[0],))
                unstaged_changes.append(change)
            self.stg.spill(paths)
        except RepositoryError as error:
            try:
                self._restore_staged_changes(unstaged_changes)
            except RepositoryError as restore_error:
                raise RepositoryError(f"{error} {restore_error}") from error
            raise

        self._restore_staged_changes(saved_changes)

    def discard_files(self, paths: tuple[str, ...]) -> None:
        self._ensure_patch_stack_is_safe()
        self.git.discard_files(paths)

    def discard_hunk(self, path: str, hunk_diff: str) -> None:
        self._ensure_patch_stack_is_safe()
        self.git.discard_hunk(path, hunk_diff)

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        self.git.stage_hunk(path, hunk_index)

    def unstage_hunk(self, path: str, hunk_index: int) -> None:
        self.git.unstage_hunk(path, hunk_index)

    def load_stack(self) -> tuple[Patch, ...]:
        return self.stg.load_stack()

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        return self.git.load_staged_changes()

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        return self.git.load_unstaged_changes()
