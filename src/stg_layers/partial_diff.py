"""Build an index-only patch for selected lines within one diff hunk."""

import re
from dataclasses import dataclass

from stg_layers.errors import RepositoryError
from stg_layers.models import FileChange, FileStatus


@dataclass(frozen=True)
class HunkLineSelection:
    path: str
    hunk_diff: str
    first_line: int
    last_line: int


def _quote_path(path: str) -> str:
    encoded = path.encode("utf-8", errors="surrogateescape")
    return (
        '"'
        + "".join(
            chr(byte) if 32 <= byte < 127 and byte not in (34, 92) else f"\\{byte:03o}"
            for byte in encoded
        )
        + '"'
    )


def _selected_body(selection: HunkLineSelection, *, stage: bool) -> list[str]:
    result: list[str] = []
    changed = False
    kept = False
    lines = selection.hunk_diff.splitlines(keepends=True)
    if not 0 <= selection.first_line <= selection.last_line < len(lines):
        raise RepositoryError("The selected line range is no longer present.")
    for index, line in enumerate(lines[1:], 1):
        if line.startswith("\\"):
            if kept:
                result.append(line)
            continue
        selected = selection.first_line <= index <= selection.last_line
        sign = line[:1]
        changed |= selected and sign in {"+", "-"}
        kept = sign == " " or selected or sign == ("-" if stage else "+")
        if not kept:
            continue
        if sign in {"+", "-"} and not selected:
            line = " " + line[1:]
        result.append(line)
    if not changed:
        raise RepositoryError("Select added or removed lines within one diff hunk.")
    return result


def _normalize_newlines(body: list[str]) -> list[str]:
    """A missing final newline applies only at the end of each side of the patch."""
    old_last = max((i for i, line in enumerate(body) if line.startswith((" ", "-"))), default=-1)
    new_last = max((i for i, line in enumerate(body) if line.startswith((" ", "+"))), default=-1)
    result: list[str] = []
    marker = "\\ No newline at end of file\n"
    for index, line in enumerate(body):
        if line.startswith("\\"):
            continue
        missing = index + 1 < len(body) and body[index + 1].startswith("\\")
        old_missing = missing and index == old_last
        new_missing = missing and index == new_last
        if line.startswith(" ") and old_missing != new_missing:
            result.append("-" + line[1:])
            if old_missing:
                result.append(marker)
            result.append("+" + line[1:])
            if new_missing:
                result.append(marker)
        else:
            result.append(line)
            if (line.startswith("-") and old_missing) or new_missing:
                result.append(marker)
    return result


def selected_lines_patch(file: FileChange, selection: HunkLineSelection, *, stage: bool) -> str:
    hunk = next((hunk for hunk in file.hunks if hunk.diff_text == selection.hunk_diff), None)
    if hunk is None:
        raise RepositoryError(f"The selected hunk in {selection.path!r} is no longer present.")
    body = _normalize_newlines(_selected_body(selection, stage=stage))
    old_count = sum(line.startswith((" ", "-")) for line in body)
    new_count = sum(line.startswith((" ", "+")) for line in body)
    if stage:
        old_start = hunk.source_start
        new_start = old_start + (1 if not old_count else -1 if not new_count else 0)
    else:
        new_start = hunk.target_start
        old_start = new_start + (1 if not new_count else -1 if not old_count else 0)
    added = file.status == FileStatus.ADDED and (stage or not old_count)
    deleted = file.status == FileStatus.DELETED and (not stage or not new_count)
    source = _quote_path("a/" + file.path)
    target = _quote_path("b/" + file.path)
    header = f"diff --git {source} {target}\n"
    mode = re.search(r"^(?:new|deleted) file mode (\d+)$", file.diff_text, re.MULTILINE)
    file_mode = mode[1] if mode else "100644"
    if added:
        header += f"new file mode {file_mode}\n"
    elif deleted:
        header += f"deleted file mode {file_mode}\n"
    header += f"--- {'/dev/null' if added else source}\n+++ {'/dev/null' if deleted else target}\n"
    return header + f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n" + "".join(body)
