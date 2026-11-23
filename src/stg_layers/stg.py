import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from stg_layers.commands import CommandRunner
from stg_layers.errors import DiffError, RepositoryError
from stg_layers.git import Git, decode_git_path, parse_diff
from stg_layers.models import Patch, PatchStatus
from stg_layers.stg_commands import StgCommands

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


class TempMessageFile:
    def __enter__(self) -> Self:
        self._directory = tempfile.TemporaryDirectory(prefix="stg-layers-")
        self.path = Path(self._directory.name) / "patch-message"
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._directory.cleanup()

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise RepositoryError(
                f"Could not read StGit's patch message template: {error}"
            ) from error

    def write(self, message: str) -> None:
        try:
            self.path.write_text(message, encoding="utf-8")
        except OSError as error:
            raise RepositoryError(f"Could not write the patch message: {error}") from error


class StGit:
    """StGit commands with optional transaction bookkeeping for mutations."""

    def __init__(
        self,
        commands: CommandRunner,
        after_mutation: Callable[[int, RepositoryError | None], None] | None = None,
    ) -> None:
        self.stg = StgCommands(commands)
        self.git = Git(commands)
        self._after_mutation = after_mutation

    @contextmanager
    def _mutate(self, *, undo_steps: int = 1) -> Generator[None]:
        try:
            yield
        except RepositoryError as error:
            if self._after_mutation is not None:
                self._after_mutation(undo_steps, error)
            raise
        if self._after_mutation is not None:
            self._after_mutation(undo_steps, None)

    def series(self, *, descriptions: bool = False) -> list[tuple[str, PatchStatus, str]]:
        return parse_series(self.stg.series(all_patches=True, descriptions=descriptions))

    def show(self, name: str, *, binary: bool = False) -> str:
        return self.stg.show(name, binary=binary)

    def load_stack(self) -> tuple[Patch, ...]:
        return tuple(
            Patch(name, status, description, parse_diff(name, self.show(name)))
            for name, status, description in self.series(descriptions=True)
        )

    def push(self, name: str, *, set_tree: bool = False) -> str:
        with self._mutate():
            return self.stg.push(name, set_tree=set_tree)

    def pop(self, name: str) -> str:
        with self._mutate():
            return self.stg.pop(name)

    def undo(self) -> str:
        with self._mutate():
            return self.stg.undo(hard=True)

    def commit(self, name: str) -> str:
        with self._mutate():
            return self.stg.commit(name)

    def uncommit(self) -> str:
        with self._mutate():
            return self.stg.uncommit(number=1)

    def hide(self, name: str) -> str:
        with self._mutate():
            return self.stg.hide(name)

    def unhide(self, name: str) -> str:
        with self._mutate():
            return self.stg.unhide(name)

    def delete(self, name: str) -> str:
        with self._mutate():
            return self.stg.delete(name)

    def sink(self, name: str, neighbor: str, *, above: bool) -> str:
        with self._mutate():
            return self.stg.sink(name, neighbor, above=above)

    def float_unapplied(self, names: tuple[str, ...]) -> str:
        with self._mutate():
            return self.stg.float(names, noapply=True)

    def spill(self, paths: tuple[str, ...]) -> str:
        with self._mutate():
            return self.stg.spill(paths)

    def refresh(self) -> str:
        # StGit records temporary and final patch steps separately.
        with self._mutate(undo_steps=2):
            return self.stg.refresh(index=True)

    def new_patch_template(self) -> str:
        with TempMessageFile() as message_file:
            self.stg.new(save_template=message_file.path)
            return message_file.read()

    def create_patch(self, message: str) -> None:
        with TempMessageFile() as message_file:
            message_file.write(message)
            with self._mutate():
                self.stg.new(file=message_file.path)

    def edit_patch_template(self, patch_name: str) -> str:
        with TempMessageFile() as message_file:
            self.stg.edit(patch_name, save_template=message_file.path)
            return message_file.read()

    def edit_patch(self, patch_name: str, message: str) -> None:
        with TempMessageFile() as message_file:
            message_file.write(message)
            with self._mutate():
                self.stg.edit(patch_name, file=message_file.path)

    def squash_message_template(self, source: str, target: str) -> str:
        return self.stg.squash(self._squash_names(source, target), name=target, save_template="-")

    def _squash_names(self, source: str, target: str) -> tuple[str, ...]:
        names = [name for name, _, _ in self.series() if name in (source, target)]
        # Hidden patches can be listed in reverse ancestry order. Squashing a
        # descendant before its ancestor can lose the ancestor's changes.
        if len(names) == 2 and self.git.is_ancestor(
            self.patch_commit(names[1]), self.patch_commit(names[0])
        ):
            names.reverse()
        return tuple(names)

    def squash(self, source: str, target: str, message: str) -> str:
        with TempMessageFile() as message_file:
            message_file.write(message)
            names = self._squash_names(source, target)
            with self._mutate():
                return self.stg.squash(names, name=target, file=message_file.path)

    def patch_commit(self, name: str) -> str:
        return self.stg.id(name)

    def set_patch_tree(self, name: str, tree: str) -> str:
        with self._mutate():
            return self.stg.edit(name, set_tree=tree)

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
