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

Press the arrow keys to navigate, Enter to expand or collapse a node, `r` to
refresh repository state, and `q` to quit. The application only reads Git and
StGit state; it does not modify the index, working tree, or patch stack.
Press Tab to focus the read-only diff preview; use Page Up/Page Down or the
mouse wheel to scroll it. Diff additions, removals, and hunk headers are
syntax-highlighted.

## Development

```console
uv sync --dev
uv run pytest
uv run ty check
uv run ruff check
```
