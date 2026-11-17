import codecs
from pathlib import Path

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from stg_layers.commands import CommandRunner
from stg_layers.errors import DiffError, RepositoryError
from stg_layers.models import DiffHunk, FileChange, FileStatus


def decode_git_path(path: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        escaped_path = path[1:-1].encode("utf-8")
        try:
            path = codecs.escape_decode(escaped_path)[0].decode("utf-8", errors="surrogateescape")
        except ValueError as error:
            raise DiffError(f"Could not decode Git path {path!r}: {error}") from error
    return path


def _strip_diff_prefix(path: str, prefix: str) -> str:
    return decode_git_path(path).removeprefix(prefix)


def parse_diff(patch_name: str, output: str) -> tuple[FileChange, ...]:
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
                path=decode_git_path(patched_file.path),
                source_path=_strip_diff_prefix(patched_file.source_file, "a/"),
                target_path=_strip_diff_prefix(patched_file.target_file, "b/"),
                status=status,
                diff_text=diff_text,
                hunks=hunks,
            )
        )
    return tuple(files)


class Git:
    """Git status, diffs, staging, worktree, and stash operations."""

    def __init__(self, commands: CommandRunner) -> None:
        self.commands = commands
        self.root = commands.root

    def has_conflicts(self) -> bool:
        return bool(self.commands.run("git", "ls-files", "--unmerged", "-z"))

    def has_local_changes(self) -> bool:
        return bool(
            self.commands.run("git", "status", "--porcelain", "--untracked-files=all", "-z")
        )

    def drop_stash(self, stash_commit: str) -> None:
        stash_entries = self.commands.run(
            "git",
            "stash",
            "list",
            "--format=%gd%x00%H",
        )
        for entry in stash_entries.splitlines():
            fields = entry.split("\0")
            if len(fields) == 2 and fields[1] == stash_commit:
                self.commands.run("git", "stash", "drop", fields[0])
                return
        raise RepositoryError(
            f"Local changes were restored, but their saved stash {stash_commit} could not "
            "be found to remove. The stash was left untouched."
        )

    def stage_files(self, paths: tuple[str, ...]) -> None:
        if paths:
            self.commands.run("git", "--literal-pathspecs", "add", "--", *paths)

    def unstage_files(self, paths: tuple[str, ...]) -> None:
        if paths:
            self.commands.run("git", "--literal-pathspecs", "restore", "--staged", "--", *paths)

    def staged_path_groups(self) -> tuple[tuple[str, ...], ...]:
        fields = self.commands.run(
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

    def staged_file_diff(self, path: str) -> str:
        return self.commands.run(
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

    def _cached_file_diff(self, path: str) -> str:
        return self.commands.run(
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

    def is_tracked(self, path: str) -> bool:
        return bool(
            self.commands.run(
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
            self.commands.run("git", "--literal-pathspecs", "reset", "--quiet", "--", path)
        except RepositoryError as cleanup_error:
            raise RepositoryError(
                f"{error} Also failed to remove the temporary intent-to-add index "
                f"entry for {path!r}: {cleanup_error}"
            ) from cleanup_error

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        if hunk_index < 0:
            raise RepositoryError("A hunk index cannot be negative.")
        is_untracked = not self.is_tracked(path)
        if is_untracked:
            self.commands.run("git", "--literal-pathspecs", "add", "--intent-to-add", "--", path)

        selection = "n\n" * hunk_index + "y\nq\n"
        try:
            before_diff = self._cached_file_diff(path)
            self.commands.run(
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
        self.commands.run(
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

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        diff = self.commands.run(
            "git",
            "diff",
            "--cached",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
        )
        return parse_diff("staged changes", diff)

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        diff = self.commands.run(
            "git",
            "diff",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
        )
        files = list(parse_diff("unstaged changes", diff))
        untracked_paths = self.commands.run(
            "git", "ls-files", "--others", "--exclude-standard", "-z"
        ).split("\0")
        for path in sorted(path for path in untracked_paths if path):
            untracked_diff = self.commands.run(
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
            files.extend(parse_diff(f"untracked file {path!r}", untracked_diff))
        return tuple(files)

    def restore_worktree(self, path: str) -> None:
        if self.is_tracked(path):
            self.commands.run("git", "--literal-pathspecs", "restore", "--worktree", "--", path)
        else:
            # Removing an addition from the index leaves its worktree file behind.
            # Match the index before pushing patches that may add the file again.
            try:
                (self.root / path).unlink(missing_ok=True)
            except OSError as error:
                raise RepositoryError(f"Could not remove spilled file {path!r}: {error}") from error

    @classmethod
    def discover(cls, cwd: Path) -> Git:
        root = CommandRunner(cwd).run("git", "rev-parse", "--show-toplevel").strip()
        return cls(CommandRunner(Path(root)))

    def current_branch(self) -> str | None:
        return self.commands.run("git", "branch", "--show-current").strip() or None

    def stash(self) -> str | None:
        if not self.has_local_changes():
            return None
        old_stash = self._stash_commit()
        self.commands.run(
            "git",
            "stash",
            "push",
            "--include-untracked",
            "--message",
            "stg-layers stack operation",
        )
        new_stash = self._stash_commit()
        if not new_stash or new_stash == old_stash:
            raise RepositoryError(
                "Git did not create a stash for the local changes; the stack operation was not run."
            )
        return new_stash

    def _stash_commit(self) -> str:
        return self.commands.run(
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/stash",
            allowed_returncodes=(1,),
        ).strip()

    def apply_stash(self, commit: str) -> None:
        self.commands.run("git", "stash", "apply", "--index", commit)

    def apply_index_diff(self, diff: str, *, reverse: bool = False) -> None:
        self.commands.run(
            "git",
            "apply",
            "--cached",
            "--binary",
            *(("--reverse",) if reverse else ()),
            input_text=diff,
        )
