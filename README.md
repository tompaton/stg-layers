# StGit Layers

A read-only Textual browser for StGit patch stacks. It displays each patch as a
tree of changed files and unified-diff hunks, with a preview for the selected
node.

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

Press the arrow keys to navigate, Enter to expand or collapse a node, `r` to
refresh the stack, and `q` to quit. The initial version only reads repository
state; it does not modify the working tree or patch stack.

## Development

```console
uv sync --dev
uv run pytest
uv run ty check
uv run ruff check
```
