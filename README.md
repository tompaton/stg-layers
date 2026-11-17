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
**n** while the tree is focused to edit StGit's
new-patch message template; select **Create** to add an empty patch on top of
the applied stack, or **Cancel**/Escape to leave the stack unchanged. Press
**e** on a patch node to edit that patch's StGit-generated message template.
Create applies the edited message to the selected patch; cancelling leaves it
unchanged. Moving a staged file into the current patch temporarily preserves
other staged files and leaves unstaged changes in the working tree. Spilling a
current-patch file temporarily preserves other staged files; if the selected
file already has staged changes, unstage those first. Hunk-level moves between
the staged and unstaged sections are supported. In the reversed tree order,
the Ctrl+Up and Ctrl+Down actions swap to follow the displayed direction:
Ctrl+Up stages or moves toward the patch stack, while Ctrl+Down spills or
unstages toward Unstaged changes. Press `r` to refresh repository state and
`q` to quit.

On a patch node, **Ctrl+Up** moves it one position toward newer patches and
**Ctrl+Down** moves it one position toward older patches; these actions swap in
reversed order to continue following the displayed direction. At the top or
bottom, the action does nothing. Patches cannot be moved across the applied /
unapplied boundary. Before reordering, StGit Layers stashes staged, unstaged,
and untracked changes, then restores them with their staged state after the
operation. The stash is removed only after restoration succeeds. If reordering
or restoring the changes causes a conflict, choose **Undo** to run
`stg undo --hard`, restore the prior stack, and retry restoring the stash;
**Quit** to resolve it in the terminal (the stash is retained); or **Refresh**
after resolving it there. If a stash-restore conflict is resolved manually,
the stash is retained as a backup. When the repository already has unresolved
index conflicts, StGit Layers is locked behind a **Refresh**/**Quit** dialog
until they are resolved.

On a file or hunk within an applied patch, **Ctrl+Up/Down** moves that file or
selected hunk to the adjacent applied patch in that visual direction. The
direction swaps with the tree order. File moves transfer all of the file's
patch changes; hunk moves preserve the other hunks in the source patch. The
selection follows the moved change, and moves do not cross the applied /
unapplied boundary. A file in the current patch still spills to **Staged
changes** when moving toward newer patches, where no applied destination
exists. Local staged, unstaged, and untracked changes are stashed and restored
with their staged state preserved for the entire transfer.

On a patch node, press **c** to commit that patch into the stack's base using
`stg commit <patch>`. StGit rearranges the stack as needed to commit the selected
patch. Press **Shift+C** to convert the newest commit beneath the patch stack
into one applied patch using `stg uncommit --number 1`; this works even with an
empty stack. Press **h** to hide the selected patch, or **Shift+H** to unhide it.
Hidden patches remain visible here with the `~` status marker; unhiding leaves
them unapplied. Press **Delete** on a patch to open an **OK/Cancel** confirmation
before deleting it. These operations preserve local changes and use the same
conflict recovery as reordering.

On an unapplied patch node, press **p** to run `stg push <patch>`. On an applied
or current patch node, press **Shift+P** to run `stg pop <patch>`. StGit applies
or unapplies the selected patch and may reorder patches as needed. Staged,
unstaged, and untracked changes are stashed and restored with the staged state
preserved. If pushing, popping, or restoring the changes causes a conflict,
the same Undo/Quit/Refresh recovery applies.

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
