import codecs
import subprocess
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeVar

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from stg_layers.models import DiffHunk, FileChange, FileStatus, Patch, PatchStatus

_STATUS: Final[dict[str, PatchStatus]] = {
    ">": PatchStatus.CURRENT,
    "+": PatchStatus.APPLIED,
    "-": PatchStatus.UNAPPLIED,
    "!": PatchStatus.HIDDEN,
}


class RepositoryError(RuntimeError):
    pass


class CommandError(RepositoryError):
    def __init__(self, command: tuple[str, ...], returncode: int, detail: str) -> None:
        self.command = command
        self.returncode = returncode
        self.detail = detail
        message = f"Command {' '.join(command)!r} failed (exit {returncode})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class DiffError(RepositoryError):
    pass


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


def _parse_series(output: str) -> list[tuple[str, PatchStatus, str]]:
    patches: list[tuple[str, PatchStatus, str]] = []
    for line in output.splitlines():
        entry = line.strip()
        if not entry:
            continue
        status_char = entry[0]
        status = _STATUS.get(status_char)
        if status is None:
            raise RepositoryError(f"Unrecognized output from 'stg series': {line!r}")

        patch_info = entry[1:].strip()
        name, separator, description = patch_info.partition("#")
        name = name.strip()
        if not name:
            raise RepositoryError(f"Missing patch name in 'stg series' output: {line!r}")
        patches.append((name, status, description.strip() if separator else ""))
    return patches


def _decode_git_path(path: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        escaped_path = path[1:-1].encode("utf-8")
        try:
            path = codecs.escape_decode(escaped_path)[0].decode("utf-8", errors="surrogateescape")
        except ValueError as error:
            raise DiffError(f"Could not decode Git path {path!r}: {error}") from error
    return path


def _strip_diff_prefix(path: str, prefix: str) -> str:
    return _decode_git_path(path).removeprefix(prefix)


def _parse_diff(patch_name: str, output: str) -> tuple[FileChange, ...]:
    try:
        patch_set = PatchSet.from_string(output)
    except (TypeError, UnidiffParseError, ValueError) as error:
        raise DiffError(f"Could not parse diff for patch {patch_name!r}: {error}") from error

    files: list[FileChange] = []
    for patched_file in patch_set:
        hunks = tuple(
            DiffHunk(
                index=index,
                header=str(hunk).splitlines()[0],
                diff_text=str(hunk),
                source_start=hunk.source_start,
                source_length=hunk.source_length,
                target_start=hunk.target_start,
                target_length=hunk.target_length,
            )
            for index, hunk in enumerate(patched_file)
        )
        has_file_metadata = (
            patched_file.is_added_file
            or patched_file.is_removed_file
            or patched_file.is_rename
            or patched_file.is_binary_file
            or patched_file.source_mode is not None
            or patched_file.target_mode is not None
        )
        if not hunks and not has_file_metadata:
            raise DiffError(
                f"Unrecognized diff section in patch {patch_name!r}: {patched_file.path}"
            )

        if patched_file.is_added_file:
            status = FileStatus.ADDED
        elif patched_file.is_removed_file:
            status = FileStatus.DELETED
        elif patched_file.is_rename:
            status = FileStatus.RENAMED
        elif patched_file.is_binary_file:
            status = FileStatus.BINARY
        else:
            status = FileStatus.MODIFIED
        diff_text = "" if status == FileStatus.BINARY else str(patched_file)
        files.append(
            FileChange(
                path=_decode_git_path(patched_file.path),
                source_path=_strip_diff_prefix(patched_file.source_file, "a/"),
                target_path=_strip_diff_prefix(patched_file.target_file, "b/"),
                status=status,
                diff_text=diff_text,
                hunks=hunks,
            )
        )
    return tuple(files)


class StGitRepository:
    def __init__(self, root: Path, branch: str | None = None) -> None:
        self.root = root
        self.branch = branch
        self._pending_stack_operation = False
        self._pending_stack_stash: str | None = None
        self._pending_stash_restore_conflict = False
        self._pending_stack_undo_count = 0

    @classmethod
    def discover(cls, cwd: Path) -> StGitRepository:
        command = ("git", "rev-parse", "--show-toplevel")
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as error:
            raise RepositoryError("Git was not found on PATH.") from error
        except OSError as error:
            raise RepositoryError(f"Could not run Git: {error}") from error
        if result.returncode:
            detail = result.stderr.strip() or "Not inside a Git repository."
            raise CommandError(command, result.returncode, detail)
        repository = cls(Path(result.stdout.strip()))
        branch = repository._run("git", "branch", "--show-current").strip()
        repository.branch = branch or None
        return repository

    def _run(
        self,
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        command = (executable, *args)
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                input=input_text,
            )
        except FileNotFoundError as error:
            raise RepositoryError(f"{executable} was not found on PATH.") from error
        except OSError as error:
            raise RepositoryError(f"Could not run {executable}: {error}") from error
        if result.returncode and result.returncode not in allowed_returncodes:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CommandError(command, result.returncode, detail)
        return result.stdout

    def has_conflicts(self) -> bool:
        return bool(self._run("git", "ls-files", "--unmerged", "-z"))

    def has_local_changes(self) -> bool:
        return bool(self._run("git", "status", "--porcelain", "--untracked-files=all", "-z"))

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
        if not self.has_local_changes():
            return None

        old_stash = self._run(
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/stash",
            allowed_returncodes=(1,),
        ).strip()
        self._run(
            "git",
            "stash",
            "push",
            "--include-untracked",
            "--message",
            "stg-layers stack operation",
        )
        new_stash = self._run(
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/stash",
            allowed_returncodes=(1,),
        ).strip()
        if not new_stash or new_stash == old_stash:
            raise RepositoryError(
                "Git did not create a stash for the local changes; the stack operation was "
                "not run."
            )
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

    def _drop_stash(self, stash_commit: str) -> None:
        stash_entries = self._run(
            "git",
            "stash",
            "list",
            "--format=%gd%x00%H",
        )
        for entry in stash_entries.splitlines():
            fields = entry.split("\0")
            if len(fields) == 2 and fields[1] == stash_commit:
                self._run("git", "stash", "drop", fields[0])
                return
        raise RepositoryError(
            f"Local changes were restored, but their saved stash {stash_commit} could not "
            "be found to remove. The stash was left untouched."
        )

    def _restore_stash(self, stash_commit: str) -> None:
        try:
            self._run("git", "stash", "apply", "--index", stash_commit)
        except RepositoryError:
            self._pending_stack_stash = stash_commit
            self._pending_stash_restore_conflict = True
            raise
        self._pending_stack_stash = None
        self._pending_stash_restore_conflict = False
        try:
            self._drop_stash(stash_commit)
        except RepositoryError as error:
            raise RepositoryError(
                f"Local changes were restored, but the recovery stash {stash_commit} remains "
                "in the stash list."
            ) from error

    def _run_stack_transaction(
        self,
        operation: Callable[[Callable[..., str]], _T],
    ) -> _T:
        self._ensure_patch_stack_is_safe()
        stash_commit = self._stash_current_changes()
        undo_count = 0

        def run_stg(*args: str) -> str:
            nonlocal undo_count
            # StGit records the temporary and final patch steps of refresh separately.
            undo_steps = 2 if args and args[0] == "refresh" else 1
            try:
                output = self._run("stg", *args)
            except RepositoryError:
                if self.has_conflicts():
                    undo_count += undo_steps
                raise
            undo_count += undo_steps
            return output

        try:
            result = operation(run_stg)
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

    def _run_stack_operation(self, *args: str) -> str:
        return self._run_stack_transaction(lambda run_stg: run_stg(*args))

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
                self._run("stg", "undo", "--hard")
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
                f"The stack operation completed, but restoring saved local changes failed: "
                f"{error}"
            ) from error

    def move_patch_one_step(
        self,
        patch_name: str,
        *,
        toward_newer: bool,
    ) -> PatchMoveResult:
        self._ensure_patch_stack_is_safe()
        series = _parse_series(self._run("stg", "series", "--all"))
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
            selected_ref = self._patch_ref(patch_name)
            neighbor_ref = self._patch_ref(series[neighbor_index][0])
            position = "--above" if toward_newer else "--below"
            self._run_stack_operation("sink", position, neighbor_ref, selected_ref)
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
            self._run_stack_operation(
                "float",
                "--noapply",
                *(self._patch_ref(name) for name in reordered_names),
            )
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
        self._ensure_patch_stack_is_safe()
        series = _parse_series(self._run("stg", "series", "--all"))
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
        selected_diff = self._selected_patch_change_diff(
            source_patch,
            patch_name,
            path,
            hunk_diff,
        )
        popped_patches = tuple(
            name
            for name, status, _ in series[patch_index + 1 :]
            if status in {PatchStatus.APPLIED, PatchStatus.CURRENT}
        )
        self._run_stack_transaction(
            lambda run_stg: self._move_patch_change_sequence(
                run_stg,
                patch_name,
                path,
                selected_diff,
                popped_patches,
                toward_newer,
            )
        )
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
            raise RepositoryError(
                f"File {path!r} is no longer present in patch {patch_name!r}."
            )
        selected_diff = source_file.diff_text
        if hunk_diff is not None:
            hunk = next(
                (hunk for hunk in source_file.hunks if hunk.diff_text == hunk_diff),
                None,
            )
            if hunk is None:
                raise RepositoryError(
                    f"The selected hunk in {path!r} is no longer present in "
                    f"patch {patch_name!r}."
                )
            header_end = selected_diff.find("@@")
            if header_end < 0:
                raise RepositoryError(f"Patch {patch_name!r} has no text diff for {path!r}.")
            selected_diff = selected_diff[:header_end] + hunk.diff_text
        elif not source_file.hunks:
            selected_diff = self._raw_patch_file_diff(patch_name, path)
        if not selected_diff:
            raise RepositoryError(f"Patch {patch_name!r} has no applicable diff for {path!r}.")
        return selected_diff

    def _move_patch_change_sequence(
        self,
        run_stg: Callable[..., str],
        source_name: str,
        path: str,
        selected_diff: str,
        popped_patches: tuple[str, ...],
        toward_newer: bool,
    ) -> None:
        for popped_name in reversed(popped_patches):
            run_stg("pop", self._patch_ref(popped_name))

        run_stg("spill", "--", f":(literal){path}")
        self._run(
            "git",
            "apply",
            "--cached",
            "--binary",
            "--reverse",
            input_text=selected_diff,
        )
        run_stg("refresh", "--index")
        self._restore_patch_change_worktree(path)

        if not toward_newer:
            run_stg("pop", self._patch_ref(source_name))
            self._run(
                "git",
                "apply",
                "--cached",
                "--binary",
                input_text=selected_diff,
            )
            run_stg("refresh", "--index")
            self._restore_patch_change_worktree(path)
            run_stg("push", self._patch_ref(source_name))

        for popped_name in popped_patches:
            run_stg("push", "--set-tree", self._patch_ref(popped_name))

    def _restore_patch_change_worktree(self, path: str) -> None:
        if self._is_tracked(path):
            self._run("git", "--literal-pathspecs", "restore", "--worktree", "--", path)
        else:
            # Removing an addition from the index leaves its worktree file behind.
            # Match the index before pushing patches that may add the file again.
            try:
                (self.root / path).unlink(missing_ok=True)
            except OSError as error:
                raise RepositoryError(f"Could not remove spilled file {path!r}: {error}") from error

    def _raw_patch_file_diff(self, patch_name: str, path: str) -> str:
        diff = self._run(
            "stg",
            "show",
            "--patch",
            self._patch_ref(patch_name),
            "--diff-opt=--patch",
            "--diff-opt=--binary",
            "--diff-opt=--no-color",
            "--diff-opt=--no-ext-diff",
            "--diff-opt=--no-textconv",
        )
        try:
            patch_set = PatchSet.from_string(diff)
        except (TypeError, UnidiffParseError, ValueError) as error:
            raise DiffError(f"Could not parse diff for patch {patch_name!r}: {error}") from error
        lines = diff.splitlines(keepends=True)
        section_starts = [
            index for index, line in enumerate(lines) if line.startswith("diff --git ")
        ]
        sections = [
            "".join(lines[start:end])
            for start, end in zip(
                section_starts,
                (*section_starts[1:], len(lines)),
                strict=True,
            )
        ]
        for patched_file, section in zip(patch_set, sections, strict=True):
            if _decode_git_path(patched_file.path) == path:
                return section
        raise RepositoryError(f"Could not find binary diff for {path!r} in patch {patch_name!r}.")

    def push_patch(self, patch_name: str) -> None:
        self._run_stack_operation("push", self._patch_ref(patch_name))

    def pop_patch(self, patch_name: str) -> None:
        self._run_stack_operation("pop", self._patch_ref(patch_name))

    def commit_patch(self, patch_name: str) -> None:
        self._run_stack_operation("commit", self._patch_ref(patch_name))

    def uncommit_patch(self) -> None:
        self._run_stack_operation("uncommit", "--number", "1")

    def hide_patch(self, patch_name: str) -> None:
        series = _parse_series(self._run("stg", "series", "--all"))
        applied_names = [
            name for name, status, _ in series
            if status in {PatchStatus.APPLIED, PatchStatus.CURRENT}
        ]
        if patch_name not in applied_names:
            self._run_stack_operation("hide", self._patch_ref(patch_name))
            return

        def hide_applied(run_stg: Callable[..., str]) -> None:
            # Hide alone changes stack metadata without updating the worktree.
            run_stg("pop", self._patch_ref(patch_name))
            run_stg("hide", self._patch_ref(patch_name))

        self._run_stack_transaction(hide_applied)

    def unhide_patch(self, patch_name: str) -> None:
        self._run_stack_operation("unhide", self._patch_ref(patch_name))

    def delete_patch(self, patch_name: str) -> None:
        self._run_stack_operation("delete", self._patch_ref(patch_name))

    @staticmethod
    def _patch_ref(patch_name: str) -> str:
        return f"\\{patch_name}" if patch_name.startswith("-") else patch_name

    def undo_stack_operation(self) -> None:
        while self._pending_stack_undo_count:
            self._run("stg", "undo", "--hard")
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
            raise RepositoryError(
                "Resolve merge conflicts in the repository before refreshing."
            )
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

    def stage_file(self, path: str) -> None:
        self._run("git", "--literal-pathspecs", "add", "--", path)

    def unstage_file(self, path: str) -> None:
        self._run("git", "--literal-pathspecs", "restore", "--staged", "--", path)

    def new_patch_template(self) -> str:
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            template_path = Path(directory) / "patch-message"
            self._run("stg", "new", "--save-template", str(template_path))
            try:
                return template_path.read_text(encoding="utf-8")
            except OSError as error:
                raise RepositoryError(
                    f"Could not read StGit's patch message template: {error}"
                ) from error

    def create_patch(self, message: str) -> None:
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            message_path = Path(directory) / "patch-message"
            try:
                message_path.write_text(message, encoding="utf-8")
            except OSError as error:
                raise RepositoryError(f"Could not write the patch message: {error}") from error
            self._run("stg", "new", "--file", str(message_path))

    def edit_patch_template(self, patch_name: str) -> str:
        patch_ref = f"\\{patch_name}" if patch_name.startswith("-") else patch_name
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            template_path = Path(directory) / "patch-message"
            self._run("stg", "edit", "--save-template", str(template_path), patch_ref)
            try:
                return template_path.read_text(encoding="utf-8")
            except OSError as error:
                raise RepositoryError(
                    f"Could not read StGit's patch message template: {error}"
                ) from error

    def edit_patch(self, patch_name: str, message: str) -> None:
        patch_ref = f"\\{patch_name}" if patch_name.startswith("-") else patch_name
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            message_path = Path(directory) / "patch-message"
            try:
                message_path.write_text(message, encoding="utf-8")
            except OSError as error:
                raise RepositoryError(f"Could not write the patch message: {error}") from error
            self._run("stg", "edit", "--file", str(message_path), patch_ref)

    def _staged_path_groups(self) -> tuple[tuple[str, ...], ...]:
        fields = self._run(
            "git",
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--name-status",
            "--find-renames",
            "-z",
        ).split("\0")
        groups: list[tuple[str, ...]] = []
        index = 0
        while index < len(fields) and fields[index]:
            status = fields[index]
            index += 1
            path_count = 2 if status.startswith(("R", "C")) else 1
            paths = tuple(fields[index : index + path_count])
            index += path_count
            if len(paths) != path_count:
                raise RepositoryError("Git returned incomplete staged file status.")
            groups.append(paths)
        return tuple(groups)

    def _staged_file_diff(self, path: str) -> str:
        return self._run(
            "git",
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--",
            path,
        )

    def _save_other_staged_changes(
        self,
        selected_paths: set[str],
        staged_paths: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        return [
            (path, diff)
            for path in staged_paths
            if path not in selected_paths
            if (diff := self._staged_file_diff(path))
        ]

    def _restore_staged_changes(self, changes: list[tuple[str, str]]) -> None:
        failed_paths: list[str] = []
        for path, diff in changes:
            try:
                self._run("git", "apply", "--cached", "--binary", input_text=diff)
            except RepositoryError:
                failed_paths.append(path)
        if failed_paths:
            paths = ", ".join(repr(path) for path in failed_paths)
            raise RepositoryError(
                f"Could not restore staged changes for {paths} after refreshing the current "
                "patch. Those changes remain unstaged."
            )

    def refresh_staged_file_to_current_patch(self, path: str) -> None:
        path_groups = self._staged_path_groups()
        selected_paths = {path}
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
                self._run(
                    "git",
                    "--literal-pathspecs",
                    "restore",
                    "--staged",
                    "--",
                    change[0],
                )
                restored_paths.append(change)
            self._run("stg", "refresh", "--index")
        except RepositoryError as error:
            try:
                self._restore_staged_changes(restored_paths)
            except RepositoryError as restore_error:
                raise RepositoryError(f"{error} {restore_error}") from restore_error
            raise

        self._restore_staged_changes(saved_changes)

    def spill_current_patch_file(self, path: str) -> None:
        path_groups = self._staged_path_groups()
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
                self._run(
                    "git",
                    "--literal-pathspecs",
                    "restore",
                    "--staged",
                    "--",
                    change[0],
                )
                unstaged_changes.append(change)
            self._run("stg", "spill", "--", path)
        except RepositoryError as error:
            try:
                self._restore_staged_changes(unstaged_changes)
            except RepositoryError as restore_error:
                raise RepositoryError(f"{error} {restore_error}") from error
            raise

        self._restore_staged_changes(saved_changes)

    def _cached_file_diff(self, path: str) -> str:
        return self._run(
            "git",
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--",
            path,
        )

    def _is_tracked(self, path: str) -> bool:
        return bool(
            self._run(
                "git",
                "--literal-pathspecs",
                "ls-files",
                "--error-unmatch",
                "--",
                path,
                allowed_returncodes=(1,),
            ).strip()
        )

    def _remove_intent_to_add(self, path: str, error: RepositoryError) -> None:
        try:
            self._run("git", "--literal-pathspecs", "reset", "--quiet", "--", path)
        except RepositoryError as cleanup_error:
            raise RepositoryError(
                f"{error} Also failed to remove the temporary intent-to-add index "
                f"entry for {path!r}: {cleanup_error}"
            ) from cleanup_error

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        if hunk_index < 0:
            raise RepositoryError("A hunk index cannot be negative.")
        is_untracked = not self._is_tracked(path)
        if is_untracked:
            self._run("git", "--literal-pathspecs", "add", "--intent-to-add", "--", path)

        selection = "n\n" * hunk_index + "y\nq\n"
        try:
            before_diff = self._cached_file_diff(path)
            self._run(
                "git",
                "--literal-pathspecs",
                "add",
                "--patch",
                "--",
                path,
                input_text=selection,
            )
            after_diff = self._cached_file_diff(path)
        except RepositoryError as error:
            if is_untracked:
                self._remove_intent_to_add(path, error)
            raise

        if after_diff == before_diff:
            error = RepositoryError(
                f"Git did not stage hunk {hunk_index + 1} from {path!r}; the change may be stale."
            )
            if is_untracked:
                self._remove_intent_to_add(path, error)
            raise error

    def unstage_hunk(self, path: str, hunk_index: int) -> None:
        if hunk_index < 0:
            raise RepositoryError("A hunk index cannot be negative.")
        before_diff = self._cached_file_diff(path)
        selection = "n\n" * hunk_index + "y\nq\n"
        self._run(
            "git",
            "--literal-pathspecs",
            "restore",
            "--staged",
            "--patch",
            "--",
            path,
            input_text=selection,
        )
        if self._cached_file_diff(path) == before_diff:
            raise RepositoryError(
                f"Git did not unstage hunk {hunk_index + 1} from {path!r}; the change may be stale."
            )

    def load_stack(self) -> tuple[Patch, ...]:
        series = _parse_series(self._run("stg", "series", "--all", "--description"))
        patches: list[Patch] = []
        for name, status, description in series:
            patch_ref = f"\\{name}" if name.startswith("-") else name
            diff = self._run(
                "stg",
                "show",
                "--patch",
                patch_ref,
                "--diff-opt=--patch",
                "--diff-opt=--no-color",
                "--diff-opt=--no-ext-diff",
                "--diff-opt=--no-textconv",
            )
            patches.append(
                Patch(
                    name=name,
                    status=status,
                    description=description,
                    files=_parse_diff(name, diff),
                )
            )
        return tuple(patches)

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        diff = self._run(
            "git",
            "diff",
            "--cached",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
        )
        return _parse_diff("staged changes", diff)

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        diff = self._run(
            "git",
            "diff",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
        )
        files = list(_parse_diff("unstaged changes", diff))
        untracked_paths = self._run(
            "git", "ls-files", "--others", "--exclude-standard", "-z"
        ).split("\0")
        for path in sorted(path for path in untracked_paths if path):
            untracked_diff = self._run(
                "git",
                "--literal-pathspecs",
                "diff",
                "--no-index",
                "--patch",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                "/dev/null",
                path,
                allowed_returncodes=(1,),
            )
            if not untracked_diff:
                raise RepositoryError(f"Git returned no diff for untracked file {path!r}.")
            files.extend(_parse_diff(f"untracked file {path!r}", untracked_diff))
        return tuple(files)
