import codecs
import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Final

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
        if self.has_conflicts():
            raise RepositoryError(
                "Cannot modify the patch stack while the repository has merge conflicts."
            )
        if self.has_local_changes():
            raise RepositoryError(
                "Cannot safely modify the patch stack while there are staged, unstaged, "
                "or untracked "
                "changes. Commit or move those changes first."
            )

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
            self._run("stg", "sink", position, neighbor_ref, selected_ref)
            return PatchMoveResult.MOVED

        if patch_status == PatchStatus.UNAPPLIED and neighbor_status == PatchStatus.UNAPPLIED:
            start_index = min(patch_index, neighbor_index)
            reordered_names = patch_names[start_index:]
            reordered_names[0], reordered_names[1] = reordered_names[1], reordered_names[0]
            self._run(
                "stg",
                "float",
                "--noapply",
                *(self._patch_ref(name) for name in reordered_names),
            )
            return PatchMoveResult.MOVED

        return PatchMoveResult.APPLICATION_BOUNDARY

    def push_patch(self, patch_name: str) -> None:
        self._ensure_patch_stack_is_safe()
        self._run("stg", "push", self._patch_ref(patch_name))

    def pop_patch(self, patch_name: str) -> None:
        self._ensure_patch_stack_is_safe()
        self._run("stg", "pop", self._patch_ref(patch_name))

    @staticmethod
    def _patch_ref(patch_name: str) -> str:
        return f"\\{patch_name}" if patch_name.startswith("-") else patch_name

    def undo_stack_operation(self) -> None:
        self._run("stg", "undo", "--hard")

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
