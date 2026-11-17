from dataclasses import dataclass
from enum import StrEnum


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
    change_area: str | None = None
    directory_path: str | None = None
