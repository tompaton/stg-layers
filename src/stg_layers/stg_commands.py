from pathlib import Path

from stg_layers.commands import CommandRunner


class StgCommands:
    """Typed StGit subcommands backed by a shared command runner."""

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

    def series(self, *, all_patches: bool = False, descriptions: bool = False) -> str:
        options = {"--all": all_patches, "--description": descriptions}
        return self.run(
            "stg", "series", *(option for option, enabled in options.items() if enabled)
        )

    def show(self, name: str, *, binary: bool = False) -> str:
        return self.run(
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

    @staticmethod
    def _patch_ref(name: str) -> str:
        return f"\\{name}" if name.startswith("-") else name

    def push(self, name: str, *, set_tree: bool = False) -> str:
        return self.run(
            "stg", "push", *(("--set-tree",) if set_tree else ()), self._patch_ref(name)
        )

    def pop(self, name: str) -> str:
        return self.run("stg", "pop", self._patch_ref(name))

    def undo(self, *, hard: bool = False) -> str:
        return self.run("stg", "undo", *(("--hard",) if hard else ()))

    def commit(self, name: str) -> str:
        return self.run("stg", "commit", self._patch_ref(name))

    def uncommit(self, *, number: int) -> str:
        return self.run("stg", "uncommit", "--number", str(number))

    def hide(self, name: str) -> str:
        return self.run("stg", "hide", self._patch_ref(name))

    def unhide(self, name: str) -> str:
        return self.run("stg", "unhide", self._patch_ref(name))

    def delete(self, name: str) -> str:
        return self.run("stg", "delete", self._patch_ref(name))

    def sink(self, name: str, neighbor: str, *, above: bool) -> str:
        return self.run(
            "stg",
            "sink",
            "--above" if above else "--below",
            self._patch_ref(neighbor),
            self._patch_ref(name),
        )

    def float(self, names: tuple[str, ...], *, noapply: bool = False) -> str:
        return self.run(
            "stg",
            "float",
            *(("--noapply",) if noapply else ()),
            *(self._patch_ref(name) for name in names),
        )

    def spill(self, paths: tuple[str, ...]) -> str:
        return self.run("stg", "spill", "--", *(f":(literal){path}" for path in paths))

    def refresh(self, *, index: bool = False) -> str:
        return self.run("stg", "refresh", *(("--index",) if index else ()))

    def new(self, *, file: Path | None = None, save_template: Path | str | None = None) -> str:
        return self.run(
            "stg", "new", *self._message_options(file=file, save_template=save_template)
        )

    def edit(
        self,
        name: str,
        *,
        file: Path | None = None,
        save_template: Path | str | None = None,
        set_tree: str | None = None,
    ) -> str:
        return self.run(
            "stg",
            "edit",
            *self._message_options(file=file, save_template=save_template),
            *(("--set-tree", set_tree) if set_tree is not None else ()),
            self._patch_ref(name),
        )

    @staticmethod
    def _message_options(*, file: Path | None, save_template: Path | str | None) -> tuple[str, ...]:
        return (
            *(("--file", str(file)) if file is not None else ()),
            *(("--save-template", str(save_template)) if save_template is not None else ()),
        )

    def squash(
        self,
        names: tuple[str, ...],
        *,
        name: str,
        file: Path | None = None,
        save_template: Path | str | None = None,
    ) -> str:
        return self.run(
            "stg",
            "squash",
            f"--name={name}",
            *self._message_options(file=file, save_template=save_template),
            *(self._patch_ref(patch) for patch in names),
        )

    def id(self, name: str) -> str:
        return self.run("stg", "id", self._patch_ref(name)).strip()
