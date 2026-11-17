from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, Self

from rich.text import Text

ChangeArea = Literal["staged", "unstaged"]


def hunk_key(diff_text: str) -> str:
    return "\n".join(
        line
        for line in diff_text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


class PatchStatus(StrEnum):
    APPLIED = "applied"
    CURRENT = "current"
    UNAPPLIED = "unapplied"
    HIDDEN = "hidden"


class FileStatus(StrEnum):
    ADDED = "added"
    DELETED = "deleted"
    RENAMED = "renamed"
    BINARY = "binary"
    MODIFIED = "modified"


_PATCH_STATUS_STYLES = {
    PatchStatus.CURRENT: "bold green",
    PatchStatus.APPLIED: "cyan",
    PatchStatus.UNAPPLIED: "yellow",
    PatchStatus.HIDDEN: "dim magenta",
}
_PATCH_STATUS_MARKERS = {
    PatchStatus.CURRENT: ">",
    PatchStatus.APPLIED: "+",
    PatchStatus.UNAPPLIED: "-",
    PatchStatus.HIDDEN: "~",
}
_FILE_STATUS_STYLES = {
    FileStatus.ADDED: "green",
    FileStatus.DELETED: "red",
    FileStatus.RENAMED: "yellow",
    FileStatus.BINARY: "magenta",
    FileStatus.MODIFIED: "blue",
}


@dataclass(frozen=True)
class DiffHunk:
    index: int
    header: str
    diff_text: str
    source_start: int
    source_length: int
    target_start: int
    target_length: int


@dataclass(frozen=True)
class FileChange:
    path: str
    source_path: str
    target_path: str
    status: FileStatus
    diff_text: str
    hunks: tuple[DiffHunk, ...]


@dataclass(frozen=True)
class Patch:
    name: str
    status: PatchStatus
    description: str
    files: tuple[FileChange, ...]

    @property
    def diff_text(self) -> str:
        return "\n".join(file.diff_text.rstrip("\n") for file in self.files)


@dataclass(frozen=True)
class TreeItem:
    title: str
    diff_text: str
    patch_name: str | None = None
    file_path: str | None = None
    hunk_index: int | None = None
    hunk_key: str | None = None
    change_area: ChangeArea | None = None
    directory_path: str | None = None

    @property
    def is_hunk(self) -> bool:
        return self.hunk_index is not None

    @property
    def is_file(self) -> bool:
        return self.file_path is not None and not self.is_hunk

    @property
    def is_file_or_hunk(self) -> bool:
        return self.file_path is not None

    @property
    def is_directory(self) -> bool:
        return self.directory_path is not None

    @property
    def is_patch(self) -> bool:
        return (
            self.patch_name is not None
            and not self.is_file_or_hunk
            and not self.is_directory
            and not self.is_hunk
        )

    @property
    def is_change_group(self) -> bool:
        return (
            self.change_area is not None
            and not self.is_file_or_hunk
            and not self.is_directory
            and not self.is_hunk
        )

    @property
    def is_file_group(self) -> bool:
        return self.is_directory or self.is_change_group

    @property
    def is_staged(self) -> bool:
        return self.change_area == "staged"

    @property
    def is_unstaged(self) -> bool:
        return self.change_area == "unstaged"

    @property
    def is_patch_change(self) -> bool:
        return self.patch_name is not None and self.is_file_or_hunk

    @property
    def is_working_change(self) -> bool:
        return self.change_area is not None and self.is_file_or_hunk

    @property
    def selection_key(self) -> tuple[str | None, ...]:
        if (
            any((self.change_area, self.patch_name, self.file_path, self.directory_path))
            or self.is_hunk
        ):
            return (
                self.change_area,
                self.patch_name,
                self.file_path,
                self.directory_path,
                (self.hunk_key or str(self.hunk_index)) if self.is_hunk else None,
            )
        return (self.title,)

    def matches_selection(self, other: TreeItem) -> bool:
        return self.selection_key == other.selection_key

    def same_context_as(self, other: TreeItem) -> bool:
        return self.patch_name == other.patch_name and self.change_area == other.change_area

    def same_file_as(self, other: TreeItem) -> bool:
        return (
            self.file_path is not None
            and self.file_path == other.file_path
            and self.same_context_as(other)
        )

    def require_patch_name(self) -> str:
        if self.patch_name is None:
            raise ValueError("This tree item does not belong to a patch.")
        return self.patch_name

    def require_file_path(self) -> str:
        if self.file_path is None:
            raise ValueError("This tree item does not refer to a file.")
        return self.file_path

    def as_selection(
        self, *, patch_name: str | None = None, change_area: ChangeArea | None = None
    ) -> Self:
        """Keep matching fields, optionally moving to a different patch or change area."""
        if patch_name is not None and change_area is not None:
            raise ValueError("Select a patch or change area, not both.")
        if patch_name is None and change_area is None:
            patch_name, change_area = self.patch_name, self.change_area
        has_identity = any((patch_name, change_area, self.file_path, self.directory_path))
        return replace(
            self,
            title="" if has_identity or self.hunk_index is not None else self.title,
            diff_text="",
            patch_name=patch_name,
            change_area=change_area,
        )

    def as_group_selection(self) -> Self:
        """Select the enclosing patch or staged/unstaged group."""
        return replace(
            self.as_selection(),
            file_path=None,
            directory_path=None,
            hunk_index=None,
            hunk_key=None,
        )

    @classmethod
    def for_patch_selection(cls, patch_name: str) -> Self:
        return cls(title="", diff_text="", patch_name=patch_name)

    @classmethod
    def for_patch_stack(cls) -> tuple[Self, Text]:
        return cls(title="Patch stack", diff_text=""), Text("Patch stack", style="bold cyan")

    @classmethod
    def for_change_group(cls, change_area: ChangeArea) -> tuple[Self, Text]:
        title = f"{change_area.title()} changes"
        return cls(title=title, diff_text="", change_area=change_area), Text(
            title, style="bold cyan"
        )

    @classmethod
    def for_patch(cls, patch: Patch) -> tuple[Self, Text]:
        label = Text()
        label.append(
            f"[{_PATCH_STATUS_MARKERS[patch.status]}{patch.name}]",
            style=_PATCH_STATUS_STYLES[patch.status],
        )
        if patch.description:
            label.append(" ")
            label.append(patch.description, style="bold")
        return cls(
            title=f"Patch: {patch.name}", diff_text=patch.diff_text, patch_name=patch.name
        ), label

    @classmethod
    def for_directory(
        cls,
        path: str,
        *,
        display_name: str | None = None,
        patch_name: str | None = None,
        change_area: ChangeArea | None = None,
    ) -> tuple[Self, Text]:
        cls._section(patch_name, change_area)
        return cls(
            title=f"{patch_name or change_area} / {path}/",
            diff_text="",
            patch_name=patch_name,
            change_area=change_area,
            directory_path=path,
        ), Text(display_name or path, style="white")

    @classmethod
    def for_file(
        cls,
        file: FileChange,
        *,
        patch_name: str | None = None,
        change_area: ChangeArea | None = None,
    ) -> tuple[Self, Text]:
        section = cls._section(patch_name, change_area)
        path = (
            f"{file.source_path} -> {file.target_path}"
            if file.status == FileStatus.RENAMED
            else file.path
        )
        display_path = path if file.status == FileStatus.RENAMED else file.path.rsplit("/", 1)[-1]
        label = Text()
        label.append(display_path, style="cyan")
        label.append(f" [{file.status.value}]", style=_FILE_STATUS_STYLES[file.status])
        return cls(
            title=f"{section} / {path}",
            diff_text=file.diff_text,
            patch_name=patch_name,
            file_path=file.path,
            change_area=change_area,
        ), label

    @classmethod
    def for_hunk(
        cls,
        file: FileChange,
        hunk: DiffHunk,
        *,
        patch_name: str | None = None,
        change_area: ChangeArea | None = None,
    ) -> tuple[Self, Text]:
        item, _label = cls.for_file(file, patch_name=patch_name, change_area=change_area)
        return replace(
            item,
            title=f"{item.title} / hunk {hunk.index + 1}",
            diff_text=hunk.diff_text,
            hunk_index=hunk.index,
            hunk_key=hunk_key(hunk.diff_text),
        ), Text(hunk.header, style="yellow")

    @staticmethod
    def _section(patch_name: str | None, change_area: ChangeArea | None) -> str:
        if (patch_name is None) == (change_area is None):
            raise ValueError("Select exactly one patch or change area.")
        if patch_name is not None:
            return patch_name
        assert change_area is not None
        return f"{change_area.title()} changes"
