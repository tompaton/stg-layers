# StGit Layers

A read-only Textual browser for StGit repositories. It displays the patch stack,
staged changes, and unstaged changes as expandable file/hunk trees, with a
preview for the selected node.

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

The tree contains three top-level sections: **Patch stack**, **Staged changes**,
and **Unstaged changes**. The patch stack expands to patches > files > hunks;
the staged and unstaged sections expand to files > hunks. Non-ignored untracked
files appear as additions in **Unstaged changes**.

Use **Up/Down** to move between visible nodes. **Right** expands a node or moves
to its first child; **Left** collapses a node or moves to its parent. The preview
updates as the highlighted node changes; **Enter** currently has no action.
Press **Tab** to move focus between the tree and read-only diff preview. The
preview scrolls independently with Page Up/Page Down or the mouse wheel.
Horizontal overflow is clipped in the tree to keep its scrollbar out of view.

Press `r` to refresh repository state and `q` to quit. The application only
reads Git and StGit state; it does not modify the index, working tree, or patch
stack. Diff additions, removals, and hunk headers are syntax-highlighted.
Preview line numbers are omitted because they are not source-file line numbers.
Tree labels use bold section headings and distinct colors for patch statuses,
patch names, file paths, and file statuses.

## Development

```console
uv sync --dev
uv run pytest
uv run ty check
uv run ruff check
```
