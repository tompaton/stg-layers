from pathlib import Path
from typing import Literal

from stg_layers.commands import CommandRunner


class GitCommands:
    """Typed Git subcommands backed by a shared command runner."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    @property
    def root(self) -> Path:
        return self._runner.root

    def run(
        self,
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        return self._runner.run(
            executable, *args, allowed_returncodes=allowed_returncodes, input_text=input_text
        )

    def rev_parse(self, revision: str, *, allow_missing: bool = False) -> str:
        return self.run(
            "git",
            "rev-parse",
            *(("--verify", "--quiet") if allow_missing else ()),
            revision,
            allowed_returncodes=(1,) if allow_missing else (),
        ).strip()

    def ls_files(
        self,
        *,
        paths: tuple[str, ...] = (),
        unmerged: bool = False,
        untracked: bool = False,
        error_unmatch: bool = False,
    ) -> str:
        options = {
            "--unmerged": unmerged,
            "--others": untracked,
            "--exclude-standard": untracked,
            "--error-unmatch": error_unmatch,
        }
        return self.run(
            "git",
            "--literal-pathspecs",
            "ls-files",
            "-z",
            *(option for option, enabled in options.items() if enabled),
            "--",
            *paths,
            allowed_returncodes=(1,) if error_unmatch else (),
        )

    def diff(
        self,
        *revisions: str,
        paths: tuple[str, ...] = (),
        cached: bool = False,
        binary: bool = False,
        full_index: bool = False,
        renames: bool | None = None,
        no_index: bool = False,
        output: Literal["patch", "name-status"] = "patch",
    ) -> str:
        """Produce machine-readable diffs without external commands or text conversion."""
        options = {
            "--cached": cached,
            "--binary": binary,
            "--full-index": full_index,
            "--find-renames": renames is True,
            "--no-renames": renames is False,
            "--no-index": no_index,
            "-z": output == "name-status",
        }
        return self.run(
            "git",
            "--literal-pathspecs",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            f"--{output}",
            *(option for option, enabled in options.items() if enabled),
            *revisions,
            "--",
            *paths,
            allowed_returncodes=(1,) if no_index else (),
        )

    def add(
        self,
        paths: tuple[str, ...],
        *,
        patch: bool = False,
        intent_to_add: bool = False,
        input_text: str | None = None,
    ) -> None:
        options = {"--patch": patch, "--intent-to-add": intent_to_add}
        self.run(
            "git",
            "--literal-pathspecs",
            "add",
            *(option for option, enabled in options.items() if enabled),
            "--",
            *paths,
            input_text=input_text,
        )

    def restore(
        self,
        paths: tuple[str, ...],
        *,
        staged: bool = False,
        worktree: bool = False,
        patch: bool = False,
        input_text: str | None = None,
    ) -> None:
        options = {"--staged": staged, "--worktree": worktree, "--patch": patch}
        self.run(
            "git",
            "--literal-pathspecs",
            "restore",
            *(option for option, enabled in options.items() if enabled),
            "--",
            *paths,
            input_text=input_text,
        )

    def stash(
        self,
        action: Literal["push", "apply", "list", "drop"],
        *,
        ref: str | None = None,
        include_untracked: bool = False,
        index: bool = False,
        message: str | None = None,
        format: str | None = None,
    ) -> str:
        options = {"--include-untracked": include_untracked, "--index": index}
        return self.run(
            "git",
            "stash",
            action,
            *(option for option, enabled in options.items() if enabled),
            *(("--message", message) if message is not None else ()),
            *((f"--format={format}",) if format is not None else ()),
            *((ref,) if ref is not None else ()),
        )

    def commit_tree(self, tree: str, *, parents: tuple[str, ...] = (), message: str) -> str:
        parent_args = tuple(arg for parent in parents for arg in ("-p", parent))
        return self.run(
            "git",
            "commit-tree",
            tree,
            *parent_args,
            input_text=message,
        ).strip()

    def apply(
        self,
        diff: str,
        *,
        cached: bool = False,
        reverse: bool = False,
        unidiff_zero: bool = False,
        check: bool = False,
        stat: bool = False,
    ) -> str:
        options = {
            "--cached": cached,
            "--reverse": reverse,
            "--unidiff-zero": unidiff_zero,
            "--check": check,
            "--stat": stat,
        }
        return self.run(
            "git",
            "apply",
            "--binary",
            *(option for option, enabled in options.items() if enabled),
            input_text=diff,
        )

    def ls_tree(self, tree: str, *, paths: tuple[str, ...] = (), recursive: bool = False) -> str:
        return self.run(
            "git",
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            *(("-r",) if recursive else ()),
            tree,
            "--",
            *paths,
        )

    def update_index(
        self,
        *,
        paths: tuple[str, ...] = (),
        force_remove: bool = False,
        index_info: str | None = None,
    ) -> None:
        self.run(
            "git",
            "--literal-pathspecs",
            "update-index",
            *(("--force-remove",) if force_remove else ()),
            *(("-z", "--index-info") if index_info is not None else ("--", *paths)),
            input_text=index_info,
        )
