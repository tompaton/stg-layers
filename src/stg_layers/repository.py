import codecs
import subprocess
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

    def _run(self, executable: str, *args: str) -> str:
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
            )
        except FileNotFoundError as error:
            raise RepositoryError(f"{executable} was not found on PATH.") from error
        except OSError as error:
            raise RepositoryError(f"Could not run {executable}: {error}") from error
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CommandError(command, result.returncode, detail)
        return result.stdout

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
