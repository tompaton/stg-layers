"""Move a captured selection between working areas and named patches."""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from stg_layers.errors import RepositoryError
from stg_layers.git import extract_hunk_diff
from stg_layers.models import ChangeArea, FileChange, FileStatus, Patch, PatchStatus
from stg_layers.partial_diff import HunkLineSelection, selected_lines_patch

if TYPE_CHECKING:
    from stg_layers.repository import StGitRepository
    from stg_layers.stg import StGit


@dataclass(frozen=True)
class MoveTarget:
    change_area: ChangeArea | None = None
    patch_name: str | None = None

    def __post_init__(self) -> None:
        if (self.change_area is None) == (self.patch_name is None):
            raise ValueError("A move target must identify one working area or patch.")

    @property
    def label(self) -> str:
        return self.change_area.title() if self.change_area else f"Patch: {self.patch_name}"


@dataclass(frozen=True)
class ChangeSelection:
    source: MoveTarget
    paths: tuple[str, ...]
    hunk_diff: str | None = None
    lines: HunkLineSelection | None = None
    whole_patch: bool = False


@dataclass(frozen=True)
class _SelectedDiff:
    path: str
    addition: str
    removal: str
    zero_context: bool = False


class ChangeMover:
    def __init__(self, repository: StGitRepository) -> None:
        self.repository = repository
        self.git = repository.git

    def move(self, selection: ChangeSelection, target: MoveTarget, message: str | None) -> None:
        self.repository._ensure_patch_stack_is_safe()
        if selection.source == target:
            raise RepositoryError("The source and destination are the same.")
        patches = self._validate_targets(selection.source, target)
        if selection.whole_patch and target.patch_name:
            if message is None:
                raise RepositoryError("A message is required for the squashed patch.")
            self.repository._run_stack_transaction(
                lambda stg: stg.squash(
                    selection.source.patch_name or "", target.patch_name or "", message
                )
            )
            return
        diffs = self._selected_diffs(selection)
        if selection.source.change_area and target.change_area:
            self.git.apply_index_diff(
                "".join(
                    diff.addition if target.change_area == "staged" else diff.removal
                    for diff in diffs
                ),
                reverse=target.change_area == "unstaged",
                unidiff_zero=selection.lines is not None,
            )
            return
        old_head = self.git.head_commit()

        def operation(stg: StGit) -> None:
            self._transfer(stg, selection.source, target, diffs)

        source_applied = self._is_applied(patches, selection.source)
        target_applied = self._is_applied(patches, target)

        def transform(stash: str | None) -> str:
            return self._local_changes(
                stash, old_head, selection, target, diffs, source_applied, target_applied
            )

        self.repository._run_stack_transaction(
            operation,
            transform_stash=transform
            if selection.source.change_area or target.change_area
            else None,
        )

    def _validate_targets(self, source: MoveTarget, target: MoveTarget) -> dict[str, Patch]:
        patches = {patch.name: patch for patch in self.repository.load_stack()}
        for location in (source, target):
            if location.patch_name is not None and location.patch_name not in patches:
                raise RepositoryError(f"Patch {location.patch_name!r} is no longer present.")
        return patches

    @staticmethod
    def _is_applied(patches: dict[str, Patch], location: MoveTarget) -> bool:
        return location.patch_name is not None and patches[location.patch_name].status in (
            PatchStatus.APPLIED,
            PatchStatus.CURRENT,
        )

    def _selected_diffs(self, selection: ChangeSelection) -> tuple[_SelectedDiff, ...]:
        source = selection.source
        if source.patch_name:
            patch = next(
                patch for patch in self.repository.load_stack() if patch.name == source.patch_name
            )
            files = patch.files
        else:
            files = (
                self.git.load_staged_changes()
                if source.change_area == "staged"
                else self.git.load_unstaged_changes()
            )
        paths = tuple(file.path for file in files) if selection.whole_patch else selection.paths
        if not paths:
            raise RepositoryError("There are no changes to move.")
        return tuple(self._file_diff(selection, files, path) for path in paths)

    def _file_diff(
        self, selection: ChangeSelection, files: tuple[FileChange, ...], path: str
    ) -> _SelectedDiff:
        file = next((file for file in files if file.path == path), None)
        if file is None:
            raise RepositoryError(f"No changes remain for {path!r}.")
        if selection.lines:
            addition_file = (
                replace(file, path=file.source_path) if file.status == FileStatus.RENAMED else file
            )
            return _SelectedDiff(
                path,
                selected_lines_patch(addition_file, selection.lines, stage=True),
                selected_lines_patch(file, selection.lines, stage=False),
                True,
            )
        if selection.hunk_diff:
            diff = extract_hunk_diff(file, selection.hunk_diff)
        elif selection.source.patch_name:
            diff = self.repository.stg.file_diff(selection.source.patch_name, path)
        else:
            diff = self.git.local_file_diff(file, staged=selection.source.change_area == "staged")
        return _SelectedDiff(path, diff, diff)

    def _transfer(
        self, stg: StGit, source: MoveTarget, target: MoveTarget, diffs: tuple[_SelectedDiff, ...]
    ) -> None:
        if source.patch_name and target.patch_name:
            self._move_between_patches(stg, source.patch_name, target.patch_name, diffs)
        elif source.patch_name:
            self._edit_patch(stg, source.patch_name, diffs, remove=True)
        elif target.patch_name:
            self._edit_patch(stg, target.patch_name, diffs, remove=False)

    def _move_between_patches(
        self, stg: StGit, source: str, target: str, diffs: tuple[_SelectedDiff, ...]
    ) -> None:
        series = stg.series()
        statuses = {name: status for name, status, _ in series}
        applied = (PatchStatus.APPLIED, PatchStatus.CURRENT)
        source_applied = statuses[source] in applied
        target_applied = statuses[target] in applied
        if not source_applied:
            self._activate_patch(stg, source, statuses[source])
        if target_applied:
            self._move_applied_changes(stg, source, target, diffs)
        else:
            self._edit_patch(stg, source, diffs, remove=True)
            self._activate_patch(stg, target, statuses[target])
            # This patch applies to the source with the selected changes removed.
            additions = tuple(replace(diff, addition=diff.removal) for diff in diffs)
            tree = self._edit_tree(stg.patch_commit(target), additions, remove=False)
            stg.set_patch_tree(target, tree)
            self._deactivate_patch(stg, target, statuses[target])
        if not source_applied:
            self._deactivate_patch(stg, source, statuses[source])
        self._restore_patch_order(stg, series)

    @staticmethod
    def _restore_patch_order(stg: StGit, series: list[tuple[str, PatchStatus, str]]) -> None:
        hidden = tuple(name for name, status, _ in series if status == PatchStatus.HIDDEN)
        now_hidden = tuple(name for name, status, _ in stg.series() if status == PatchStatus.HIDDEN)
        if hidden != now_hidden:
            for name in reversed(hidden):
                stg.unhide(name)
                stg.hide(name)
        unapplied = tuple(name for name, status, _ in series if status == PatchStatus.UNAPPLIED)
        now = tuple(name for name, status, _ in stg.series() if status == PatchStatus.UNAPPLIED)
        if unapplied != now:
            stg.float_unapplied(unapplied)

    @staticmethod
    def _activate_patch(stg: StGit, name: str, status: PatchStatus) -> None:
        if status == PatchStatus.HIDDEN:
            stg.unhide(name)
        stg.push(name)

    @staticmethod
    def _deactivate_patch(stg: StGit, name: str, status: PatchStatus) -> None:
        stg.pop(name)
        if status == PatchStatus.HIDDEN:
            stg.hide(name)

    def _move_applied_changes(
        self, stg: StGit, source: str, target: str, diffs: tuple[_SelectedDiff, ...]
    ) -> None:
        applied = [
            name
            for name, status, _ in stg.series()
            if status in (PatchStatus.APPLIED, PatchStatus.CURRENT)
        ]
        source_index, target_index = applied.index(source), applied.index(target)
        first, last = sorted((source_index, target_index))
        for name in reversed(applied[first + 1 :]):
            stg.pop(name)
        older = applied[first]
        tree = self._edit_tree(stg.patch_commit(older), diffs, remove=older == source)
        stg.set_patch_tree(older, tree)
        for name in applied[first + 1 : last]:
            stg.push(name)
        # The newer endpoint retains its combined tree. Its diff against the
        # rebased parent now includes/excludes exactly the transferred changes.
        stg.push(applied[last], set_tree=True)
        for name in applied[last + 1 :]:
            stg.push(name)

    def _edit_patch(
        self, stg: StGit, name: str, diffs: tuple[_SelectedDiff, ...], *, remove: bool
    ) -> None:
        series = stg.series()
        index = next(index for index, (patch, _, _) in enumerate(series) if patch == name)
        applied = series[index][1] in (PatchStatus.APPLIED, PatchStatus.CURRENT)
        above = [
            patch
            for patch, status, _ in series[index + 1 :]
            if applied and status in (PatchStatus.APPLIED, PatchStatus.CURRENT)
        ]
        for patch in reversed(above):
            stg.pop(patch)
        tree = self._edit_tree(stg.patch_commit(name), diffs, remove=remove)
        stg.set_patch_tree(name, tree)
        for patch in above:
            stg.push(patch)

    def _edit_tree(self, tree: str, diffs: tuple[_SelectedDiff, ...], *, remove: bool) -> str:
        with self.git.temporary_index():
            self.git.set_index_tree(tree)
            for diff in diffs:
                self.git.apply_index_diff(
                    diff.removal if remove else diff.addition,
                    reverse=remove,
                    unidiff_zero=diff.zero_context,
                )
            return self.git.index_tree()

    def _local_changes(
        self,
        stash: str | None,
        old_head: str,
        selection: ChangeSelection,
        target: MoveTarget,
        diffs: tuple[_SelectedDiff, ...],
        source_applied: bool,
        target_applied: bool,
    ) -> str:
        # A synthetic stash describes only the edits that should remain local.
        # Its base accounts for changes transferred into or out of applied patches.
        base, index, worktree, untracked = self.git.stash_trees(stash, old_head)
        if selection.source.change_area:
            return self._consume_local_changes(
                base, index, worktree, untracked, selection, diffs, target_applied
            )
        if source_applied:
            base = self._edit_tree(base, diffs, remove=True)
            if target.change_area == "unstaged":
                index = self._edit_tree(index, diffs, remove=True)
        else:
            worktree = self._edit_tree(worktree, diffs, remove=False)
            if target.change_area == "staged":
                index = self._edit_tree(index, diffs, remove=False)
        return self.git.create_stash(base, index, worktree, untracked)

    def _consume_local_changes(
        self,
        base: str,
        index: str,
        worktree: str,
        untracked: str | None,
        selection: ChangeSelection,
        diffs: tuple[_SelectedDiff, ...],
        target_applied: bool,
    ) -> str:
        # Stash keeps untracked files in a separate tree. Promote selected files
        # to the working tree before they become tracked in an applied patch.
        tracked = tuple(
            diff
            for diff in diffs
            if not untracked or not self.git.tree_has_path(untracked, diff.path)
        )
        new = tuple(diff for diff in diffs if diff not in tracked)
        if target_applied:
            base = self._edit_tree(base, diffs, remove=False)
            if selection.source.change_area == "unstaged":
                index = self._edit_tree(index, diffs, remove=False)
            if new and untracked:
                worktree = self.git.copy_tree_paths(
                    worktree, untracked, tuple(diff.path for diff in new)
                )
                untracked = self.git.remove_tree_paths(untracked, tuple(diff.path for diff in new))
        else:
            worktree = self._edit_tree(worktree, tracked, remove=True)
            if new and untracked:
                untracked = self._edit_tree(untracked, new, remove=True)
            if selection.source.change_area == "staged":
                index = self._edit_tree(index, diffs, remove=True)
        return self.git.create_stash(base, index, worktree, untracked)
