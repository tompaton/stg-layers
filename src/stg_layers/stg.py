import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from stg_layers.commands import CommandRunner
from stg_layers.errors import DiffError, RepositoryError
from stg_layers.git import Git, decode_git_path, parse_diff
from stg_layers.models import Patch, PatchStatus

_STATUS: Final[dict[str, PatchStatus]] = {
    ">": PatchStatus.CURRENT,
    "+": PatchStatus.APPLIED,
    "-": PatchStatus.UNAPPLIED,
    "!": PatchStatus.HIDDEN,
}


def parse_series(output: str) -> list[tuple[str, PatchStatus, str]]:
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


class StGit:
    """StGit commands with optional transaction bookkeeping for mutations."""

    def __init__(
        self,
        commands: CommandRunner,
        after_mutation: Callable[[int, RepositoryError | None], None] | None = None,
    ) -> None:
        self.commands = commands
        self._after_mutation = after_mutation

    def _mutate(self, *args: str, undo_steps: int = 1) -> str:
        try:
            output = self.commands.run("stg", *args)
        except RepositoryError as error:
            if self._after_mutation is not None:
                self._after_mutation(undo_steps, error)
            raise
        if self._after_mutation is not None:
            self._after_mutation(undo_steps, None)
        return output

    def series(self, *, descriptions: bool = False) -> list[tuple[str, PatchStatus, str]]:
        return parse_series(
            self.commands.run(
                "stg", "series", "--all", *(("--description",) if descriptions else ())
            )
        )

    def show(self, name: str, *, binary: bool = False) -> str:
        return self.commands.run(
            "stg",
            "show",
            "--patch",
            self._patch_ref(name),
            "--diff-opt=--patch",
            *(("--diff-opt=--binary",) if binary else ()),
            "--diff-opt=--no-color",
            "--diff-opt=--no-ext-diff",
            "--diff-opt=--no-textconv",
        )

    def load_stack(self) -> tuple[Patch, ...]:
        return tuple(
            Patch(name, status, description, parse_diff(name, self.show(name)))
            for name, status, description in self.series(descriptions=True)
        )

    def push(self, name: str, *, set_tree: bool = False) -> str:
        return self._mutate("push", *(("--set-tree",) if set_tree else ()), self._patch_ref(name))

    def pop(self, name: str) -> str:
        return self._mutate("pop", self._patch_ref(name))

    def undo(self) -> str:
        return self._mutate("undo", "--hard")

    def commit(self, name: str) -> str:
        return self._mutate("commit", self._patch_ref(name))

    def uncommit(self) -> str:
        return self._mutate("uncommit", "--number", "1")

    def hide(self, name: str) -> str:
        return self._mutate("hide", self._patch_ref(name))

    def unhide(self, name: str) -> str:
        return self._mutate("unhide", self._patch_ref(name))

    def delete(self, name: str) -> str:
        return self._mutate("delete", self._patch_ref(name))

    def sink(self, name: str, neighbor: str, *, above: bool) -> str:
        return self._mutate(
            "sink",
            "--above" if above else "--below",
            self._patch_ref(neighbor),
            self._patch_ref(name),
        )

    def float_unapplied(self, names: tuple[str, ...]) -> str:
        return self._mutate("float", "--noapply", *(self._patch_ref(name) for name in names))

    def spill(self, paths: tuple[str, ...]) -> str:
        return self._mutate("spill", "--", *(f":(literal){path}" for path in paths))

    def refresh(self) -> str:
        # StGit records temporary and final patch steps separately.
        return self._mutate("refresh", "--index", undo_steps=2)

    def new_patch_template(self) -> str:
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            template_path = Path(directory) / "patch-message"
            self.commands.run("stg", "new", "--save-template", str(template_path))
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
            self._mutate("new", "--file", str(message_path))

    def edit_patch_template(self, patch_name: str) -> str:
        patch_ref = self._patch_ref(patch_name)
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            template_path = Path(directory) / "patch-message"
            self.commands.run("stg", "edit", "--save-template", str(template_path), patch_ref)
            try:
                return template_path.read_text(encoding="utf-8")
            except OSError as error:
                raise RepositoryError(
                    f"Could not read StGit's patch message template: {error}"
                ) from error

    def edit_patch(self, patch_name: str, message: str) -> None:
        patch_ref = self._patch_ref(patch_name)
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            message_path = Path(directory) / "patch-message"
            try:
                message_path.write_text(message, encoding="utf-8")
            except OSError as error:
                raise RepositoryError(f"Could not write the patch message: {error}") from error
            self._mutate("edit", "--file", str(message_path), patch_ref)

    def squash_message_template(self, source: str, target: str) -> str:
        return self.commands.run(
            "stg",
            "squash",
            f"--name={target}",
            "--save-template",
            "-",
            *self._squash_names(source, target),
        )

    def _squash_names(self, source: str, target: str) -> tuple[str, ...]:
        names = [name for name, _, _ in self.series() if name in (source, target)]
        # Hidden patches can be listed in reverse ancestry order. Squashing a
        # descendant before its ancestor can lose the ancestor's changes.
        if len(names) == 2 and Git(self.commands).is_ancestor(
            self.patch_commit(names[1]), self.patch_commit(names[0])
        ):
            names.reverse()
        return tuple(self._patch_ref(name) for name in names)

    def squash(self, source: str, target: str, message: str) -> str:
        with tempfile.TemporaryDirectory(prefix="stg-layers-") as directory:
            message_path = Path(directory) / "patch-message"
            try:
                message_path.write_text(message, encoding="utf-8")
            except OSError as error:
                raise RepositoryError(f"Could not write the patch message: {error}") from error
            return self._mutate(
                "squash",
                f"--name={target}",
                "--file",
                str(message_path),
                *self._squash_names(source, target),
            )

    def patch_commit(self, name: str) -> str:
        return self.commands.run("stg", "id", self._patch_ref(name)).strip()

    def set_patch_tree(self, name: str, tree: str) -> str:
        return self._mutate("edit", "--set-tree", tree, self._patch_ref(name))

    def file_diff(self, patch_name: str, path: str) -> str:
        diff = self.show(patch_name, binary=True)
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
            if decode_git_path(patched_file.path) == path:
                return section
        raise RepositoryError(f"Could not find binary diff for {path!r} in patch {patch_name!r}.")

    @staticmethod
    def _patch_ref(patch_name: str) -> str:
        return f"\\{patch_name}" if patch_name.startswith("-") else patch_name
