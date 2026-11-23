import codecs
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from stg_layers.commands import CommandRunner
from stg_layers.errors import DiffError, RepositoryError
from stg_layers.git_commands import GitCommands
from stg_layers.models import DiffHunk, FileChange, FileStatus
from stg_layers.partial_diff import HunkLineSelection, selected_lines_patch


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


def extract_hunk_diff(file: FileChange, hunk_diff: str) -> str:
    if not any(hunk.diff_text == hunk_diff for hunk in file.hunks):
        raise RepositoryError(f"The selected hunk in {file.path!r} is no longer present.")
    header_end = file.diff_text.index("@@ ")
    header = "".join(
        line
        for line in file.diff_text[:header_end].splitlines(keepends=True)
        if not line.startswith(("old mode ", "new mode "))
    )
    return header + hunk_diff


class Git:
    """Git status, diffs, staging, worktree, and stash operations."""

    def __init__(self, commands: CommandRunner) -> None:
        self.git = GitCommands(commands)
        self.root = commands.root

    @classmethod
    def discover(cls, cwd: Path) -> Git:
        root = CommandRunner(cwd).run("git", "rev-parse", "--show-toplevel").strip()
        return cls(CommandRunner(Path(root)))

    def _run(
        self,
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        return self.git.run(
            executable, *args, allowed_returncodes=allowed_returncodes, input_text=input_text
        )

    def current_branch(self) -> str | None:
        return self._run("git", "branch", "--show-current").strip() or None

    def remote_url(self) -> str | None:
        remotes = self._run("git", "remote").splitlines()
        if not remotes:
            return None
        remote = "origin" if "origin" in remotes else remotes[0]
        return self._run("git", "remote", "get-url", remote).strip() or None

    def head_commit(self) -> str:
        return self.git.rev_parse("HEAD")

    def is_ancestor(self, older: str, newer: str) -> bool:
        return (
            self._run("git", "merge-base", older, newer, allowed_returncodes=(1,)).strip() == older
        )

    def has_conflicts(self) -> bool:
        return bool(self.git.ls_files(unmerged=True))

    def has_local_changes(self) -> bool:
        return bool(self._run("git", "status", "--porcelain", "--untracked-files=all", "-z"))

    def is_tracked(self, path: str) -> bool:
        return bool(self.git.ls_files(paths=(path,), error_unmatch=True))

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        return parse_diff("staged changes", self.git.diff(cached=True))

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        files = list(parse_diff("unstaged changes", self.git.diff()))
        untracked_paths = self.git.ls_files(untracked=True).split("\0")
        for path in sorted(path for path in untracked_paths if path):
            untracked_diff = self.git.diff(paths=("/dev/null", path), no_index=True)
            if not untracked_diff:
                raise RepositoryError(f"Git returned no diff for untracked file {path!r}.")
            files.extend(parse_diff(f"untracked file {path!r}", untracked_diff))
        return tuple(files)

    def staged_path_groups(self) -> tuple[tuple[str, ...], ...]:
        fields = self.git.diff(cached=True, output="name-status", renames=True).split("\0")
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

    def local_file_diff(self, file: FileChange, *, staged: bool) -> str:
        paths = (file.path,)
        no_index = False
        if staged and file.status == FileStatus.RENAMED:
            paths = (file.source_path, file.path)
        elif not staged and not self.is_tracked(file.path):
            paths = ("/dev/null", file.path)
            no_index = True
        return self.git.diff(
            paths=paths,
            cached=staged,
            binary=True,
            full_index=True,
            renames=False,
            no_index=no_index,
        )

    def staged_file_diff(self, path: str) -> str:
        return self.git.diff(
            paths=(path,), cached=True, binary=True, full_index=True, renames=False
        )

    def diffstat(self, diff: str) -> str:
        if not any(line.startswith("diff --git ") for line in diff.splitlines()):
            return "No changes."
        return self.git.apply(diff, stat=True).rstrip()

    def unstaged_diffstat(self, paths: tuple[str, ...], *, hunk_diff: str | None = None) -> str:
        if hunk_diff is not None:
            file = self._unstaged_file(paths[0])
            return self.diffstat(extract_hunk_diff(file, hunk_diff))
        tracked = tuple(path for path in paths if self.is_tracked(path))
        diffs = []
        if tracked:
            diffs.append(self.git.diff(paths=tracked, binary=True, renames=False))
        for path in paths:
            if path not in tracked:
                diffs.append(self.git.diff(paths=("/dev/null", path), no_index=True, binary=True))
        return self.diffstat("".join(diffs))

    def _unstaged_file(self, path: str) -> FileChange:
        file = next((file for file in self.load_unstaged_changes() if file.path == path), None)
        if file is None:
            raise RepositoryError(f"No changes remain for {path!r}.")
        return file

    def stage_files(self, paths: tuple[str, ...]) -> None:
        if paths:
            self.git.add(paths)

    def unstage_files(self, paths: tuple[str, ...]) -> None:
        if paths:
            self.git.restore(paths, staged=True)

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        if hunk_index < 0:
            raise RepositoryError("A hunk index cannot be negative.")
        is_untracked = not self.is_tracked(path)
        if is_untracked:
            self.git.add((path,), intent_to_add=True)

        selection = "n\n" * hunk_index + "y\nq\n"
        try:
            before_diff = self._cached_file_diff(path)
            self.git.add((path,), patch=True, input_text=selection)
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

    def _remove_intent_to_add(self, path: str, error: RepositoryError) -> None:
        try:
            self._run("git", "--literal-pathspecs", "reset", "--quiet", "--", path)
        except RepositoryError as cleanup_error:
            raise RepositoryError(
                f"{error} Also failed to remove the temporary intent-to-add index "
                f"entry for {path!r}: {cleanup_error}"
            ) from cleanup_error

    def unstage_hunk(self, path: str, hunk_index: int) -> None:
        if hunk_index < 0:
            raise RepositoryError("A hunk index cannot be negative.")
        before_diff = self._cached_file_diff(path)
        selection = "n\n" * hunk_index + "y\nq\n"
        self.git.restore((path,), staged=True, patch=True, input_text=selection)
        if self._cached_file_diff(path) == before_diff:
            raise RepositoryError(
                f"Git did not unstage hunk {hunk_index + 1} from {path!r}; the change may be stale."
            )

    def _cached_file_diff(self, path: str) -> str:
        return self.git.diff(paths=(path,), cached=True)

    def move_selected_lines(self, selection: HunkLineSelection, *, stage: bool) -> None:
        files = self.load_unstaged_changes() if stage else self.load_staged_changes()
        file = next((file for file in files if file.path == selection.path), None)
        if file is None:
            raise RepositoryError(f"No changes remain for {selection.path!r}.")
        patch = selected_lines_patch(file, selection, stage=stage)
        self.git.apply(patch, cached=True, reverse=not stage, unidiff_zero=True)

    def discard_files(self, paths: tuple[str, ...]) -> None:
        for path in paths:
            self.restore_worktree(path)

    def restore_worktree(self, path: str) -> None:
        if self.is_tracked(path):
            self.git.restore((path,), worktree=True)
        else:
            # Removing an addition from the index leaves its worktree file behind.
            # Match the index before pushing patches that may add the file again.
            try:
                (self.root / path).unlink(missing_ok=True)
            except OSError as error:
                raise RepositoryError(f"Could not remove spilled file {path!r}: {error}") from error

    def discard_hunk(self, path: str, hunk_diff: str) -> None:
        file = next((file for file in self.load_unstaged_changes() if file.path == path), None)
        if file is None:
            raise RepositoryError(f"The selected hunk in {path!r} is no longer present.")
        self.git.apply(extract_hunk_diff(file, hunk_diff), reverse=True)

    def discard_selected_lines(self, selection: HunkLineSelection, *, staged: bool = False) -> None:
        file = self._discard_line_file(selection, staged=staged)
        patch = selected_lines_patch(file, selection, stage=False)
        if not staged:
            self.git.apply(patch, reverse=True, unidiff_zero=True)
            return
        self.git.apply(patch, check=True, reverse=True, unidiff_zero=True)
        original_index = self.index_tree()
        try:
            self.apply_index_diff(patch, reverse=True, unidiff_zero=True)
            self.git.apply(patch, reverse=True, unidiff_zero=True)
        except RepositoryError:
            self.set_index_tree(original_index)
            raise

    def discarded_lines_diffstat(
        self, selection: HunkLineSelection, *, staged: bool = False
    ) -> str:
        file = self._discard_line_file(selection, staged=staged)
        return self.diffstat(selected_lines_patch(file, selection, stage=False))

    def _discard_line_file(self, selection: HunkLineSelection, *, staged: bool) -> FileChange:
        files = self.load_staged_changes() if staged else self.load_unstaged_changes()
        file = next((file for file in files if file.path == selection.path), None)
        if file is None:
            raise RepositoryError(f"No changes remain for {selection.path!r}.")
        return file

    def stash(self) -> str | None:
        if not self.has_local_changes():
            return None
        old_stash = self._stash_commit()
        self.git.stash("push", include_untracked=True, message="stg-layers stack operation")
        new_stash = self._stash_commit()
        if not new_stash or new_stash == old_stash:
            raise RepositoryError(
                "Git did not create a stash for the local changes; the stack operation was not run."
            )
        return new_stash

    def _stash_commit(self) -> str:
        return self.git.rev_parse("refs/stash", allow_missing=True)

    def apply_stash(self, commit: str) -> None:
        self.git.stash("apply", ref=commit, index=True)

    def drop_stash(self, stash_commit: str) -> None:
        stash_entries = self.git.stash("list", format="%gd%x00%H")
        for entry in stash_entries.splitlines():
            fields = entry.split("\0")
            if len(fields) == 2 and fields[1] == stash_commit:
                self.git.stash("drop", ref=fields[0])
                return
        raise RepositoryError(
            f"Local changes were restored, but their saved stash {stash_commit} could not "
            "be found to remove. The stash was left untouched."
        )

    def stash_trees(self, stash: str | None, head: str) -> tuple[str, str, str, str | None]:
        if stash is None:
            return head, head, head, None
        untracked = self.git.rev_parse(f"{stash}^3", allow_missing=True)
        return f"{stash}^1", f"{stash}^2", stash, untracked or None

    def create_stash(self, base: str, index: str, worktree: str, untracked: str | None) -> str:
        def commit(tree: str, parents: tuple[str, ...] = ()) -> str:
            return self.git.commit_tree(
                self.git.rev_parse(f"{tree}^{{tree}}"),
                parents=parents,
                message="stg-layers move\n",
            )

        base_commit = commit(base)
        index_commit = commit(index, (base_commit,))
        parents = (base_commit, index_commit)
        if untracked:
            parents += (commit(untracked),)
        return commit(worktree, parents)

    def moved_stash_index(self, commit: str) -> str:
        """Rebase the saved index onto HEAD, including an explicitly absent new file."""
        diff = self.git.diff(f"{commit}^1", f"{commit}^2", binary=True)
        with self.temporary_index():
            if diff:
                self.apply_index_diff(diff)
            return self.index_tree()

    @contextmanager
    def temporary_index(self) -> Generator[None]:
        """Start from HEAD, then restore the original index without changing worktree files."""
        original_tree = self.index_tree()
        try:
            self.set_index_tree("HEAD")
            yield
        finally:
            try:
                self.set_index_tree(original_tree)
            except RepositoryError as error:
                raise RepositoryError(
                    f"Could not restore the saved index tree {original_tree}: {error}"
                ) from error

    def index_tree(self) -> str:
        return self._run("git", "write-tree").strip()

    def set_index_tree(self, tree: str) -> None:
        self._run("git", "read-tree", tree)

    def apply_index_diff(
        self, diff: str, *, reverse: bool = False, unidiff_zero: bool = False
    ) -> None:
        self.git.apply(diff, cached=True, reverse=reverse, unidiff_zero=unidiff_zero)

    def tree_has_path(self, tree: str, path: str) -> bool:
        return bool(self.git.ls_tree(tree, paths=(path,)))

    def copy_tree_paths(self, tree: str, source: str, paths: tuple[str, ...]) -> str:
        entries = self.git.ls_tree(source, paths=paths, recursive=True)
        with self.temporary_index():
            self.set_index_tree(tree)
            self.git.update_index(index_info=entries)
            return self.index_tree()

    def remove_tree_paths(self, tree: str, paths: tuple[str, ...]) -> str:
        with self.temporary_index():
            self.set_index_tree(tree)
            self.git.update_index(paths=paths, force_remove=True)
            return self.index_tree()
