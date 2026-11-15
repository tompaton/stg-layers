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
changes**. After Enter, selection advances to the next file or hunk in the
source group, making repeated presses useful for staging or unstaging a series
of changes. **Ctrl+Down** also stages an unstaged file or hunk; on a staged file
it moves that file's staged contents into the current patch. **Ctrl+Up**
unstages a staged file or hunk; on a current-patch file it spills that file
into **Staged changes**. With the Ctrl shortcuts, selection follows the moved
file or hunk. The preview refreshes after all these operations, and expanded
file state is retained. Hunk actions preserve other hunks in the same file.
File actions include untracked files. Enter and the staged / unstaged
shortcuts update Git's index but leave working-tree contents and the patch
stack unchanged. Patch-stack nodes are not modified by Enter. Press
**c** while the tree is focused to edit StGit's
new-patch message template; select **Create** to add an empty patch on top of
the applied stack, or **Cancel**/Escape to leave the stack unchanged. Press
**e** on a patch node to edit that patch's StGit-generated message template.
Create applies the edited message to the selected patch; cancelling leaves it
unchanged. Moving a staged file into the current patch temporarily preserves
other staged files and leaves unstaged changes in the working tree. Spilling a
current-patch file temporarily preserves other staged files; if the selected
file already has staged changes, unstage those first. Hunk-level moves between
the staged and unstaged sections are supported, but patch moves remain
file-level. Files in other patches are not moved by these shortcuts. In the
reversed tree order, the Ctrl+Up and Ctrl+Down actions swap to follow the
displayed direction: Ctrl+Up stages or moves toward the patch stack, while
Ctrl+Down spills or unstages toward Unstaged changes. Press `r` to refresh
repository state and `q` to quit.

On a patch node, **Ctrl+Up** moves it one position toward newer patches and
**Ctrl+Down** moves it one position toward older patches; these actions swap in
reversed order to continue following the displayed direction. At the top or
bottom, the action does nothing. Patches cannot be moved across the applied /
unapplied boundary. Reordering requires a clean index and working tree so
automatic conflict undo cannot discard pre-existing changes. If reordering
causes a merge conflict, choose **Undo** to run
`stg undo --hard` and restore the pre-reorder stack, **Quit** to resolve it in
the terminal, or **Refresh** after resolving it there. When the repository
already has unresolved index conflicts, StGit Layers is locked behind a
**Refresh**/**Quit** dialog until they are resolved.

On an unapplied patch node, press **p** to run `stg push <patch>`. On an applied
or current patch node, press **Shift+P** to run `stg pop <patch>`. StGit applies
or unapplies the selected patch and may reorder patches as needed. Both
operations require a clean index and working tree so conflict recovery cannot
discard pre-existing changes. If pushing or popping causes a conflict, choose
**Undo** to restore the previous stack, **Quit** to resolve it in the terminal,
or **Refresh** after resolving it there.

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
