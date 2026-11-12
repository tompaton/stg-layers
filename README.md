# StGit Layers

A Textual browser for StGit repositories. It displays the patch stack,
staged changes, and unstaged changes as expandable file/hunk trees, with a
preview for the selected node. Files and individual text hunks in the staged
and unstaged sections can be staged or unstaged from the tree.

## Requirements

- Python 3.15 or newer
- Git
- StGit (`stg`)

## Install and run

Install the tool from a checkout with `uv`:

```console
uv tool install .
cd /path/to/stgit-repository
stg-layers
```

The default tree order shows **Unstaged changes**, **Staged changes**, then
**Patch stack**, with patches newest to oldest. Press **Shift+R** to reverse the
order: patches oldest to newest, then staged and unstaged changes. The patch
stack expands to patches > files > hunks; the staged and unstaged sections
expand to files > hunks. On startup, sections and patches are expanded to show
their files, while each file's hunks are collapsed. Non-ignored untracked files
appear as additions in **Unstaged changes**.

Use **Up/Down** to move between visible nodes. **Right** expands a node or moves
to its first child; **Left** collapses a node or moves to its parent. The preview
updates as the highlighted node changes. Press **Tab** to move focus between
the tree and read-only diff preview. The preview scrolls independently with
Page Up/Page Down or the mouse wheel.
Horizontal overflow is clipped in the tree to keep its scrollbar out of view.

With the tree focused, **Enter** stages the selected file or hunk under
**Unstaged changes**, and unstages the selected file or hunk under **Staged
changes**. Hunk actions preserve other hunks in the same file. File actions
include untracked files. These operations update Git's index but leave the
working-tree contents and StGit patch stack unchanged. Patch-stack nodes are
not modified by Enter. Press `r` to refresh repository state and `q` to quit.
Diff additions, removals, and hunk headers are syntax-highlighted.
Preview line numbers are omitted because they are not source-file line numbers.
Tree labels use bold section headings and distinct colors for patch statuses,
patch names, file paths, and file statuses. Patch labels display as
`[status patch-id] title`, with the status marker and patch ID sharing the
status color: `>`, `+`, `-`, and `~`.

## Development

```console
uv sync --dev
uv run pytest
uv run ty check
uv run ruff check
```
