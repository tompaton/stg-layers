# StGit Layers

A Textual browser for StGit repositories, with a patch tree and a read-only diff
preview. Stage, unstage, discard, and move files or individual hunks between layers.

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

## Tree layout

The default order is **Unstaged changes → Staged changes → Patch stack**, with
patches newest to oldest. The tree root shows the repository directory and branch;
the title bar shows the remote URL, preferring `origin`.

| Item | Display and scope |
| --- | --- |
| Change group | All staged or unstaged files, including files in collapsed directories. Non-ignored untracked files appear as unstaged additions. |
| Patch | Expands into directories, files, and hunks. Labels show `[status patch-id] title`. |
| Directory | Contains all descendant files. Directory chains without intermediate files are combined, e.g. `src/stg_layers`. |
| File | Shows its status and insertion/deletion counts, e.g. `app.py [modified] +12-3`. File operations include all its hunks. |
| Hunk | One text hunk. Hunk operations preserve the other hunks in the file. |

Sections, patches, and directories start expanded; file hunks start collapsed.
Patch status markers are:

| Marker | Meaning |
| --- | --- |
| `>` | Current patch |
| `+` | Applied patch |
| `-` | Unapplied patch |
| `~` | Hidden patch |

## Keyboard commands

### General and tree navigation

| Key | Focus / selected item | Action |
| --- | --- | --- |
| **q** | Either pane | Quit. |
| **r** | Either pane | Refresh repository state. |
| **Shift+R** | Either pane | Reverse tree order. Movement shortcuts continue to follow the displayed direction. |
| **Tab / Shift+Tab** | Either pane | Move focus between the tree and diff preview. |
| **Up / Down** | Tree | Select the previous / next visible node and update the preview. |
| **Right** | Expandable tree node | Expand it, or move to its first child if already expanded. |
| **Right** | Hunk | Focus the diff preview. |
| **Left** | Tree | Collapse the node, or move to its parent. |

### Stage, unstage, and discard

These commands apply when the **tree** is focused.

| Key | Selected item | Action |
| --- | --- | --- |
| **Enter** | Unstaged/Staged group, directory, file, or hunk | Stage/Unstage the selection. |
| **Delete** | Unstaged group, directory, file, or hunk | Confirm, then discard the selection. Tracked files are restored from the index; untracked files are removed. Staged changes are preserved. **Discard cannot be undone.** |

After Enter on a file or hunk, selection advances within the source group.
On a group or directory, selection stays with that source group or directory.
Empty groups are left unchanged. Staging and unstaging leave working-tree contents
unchanged.

### Move changes and reorder patches

**Ctrl+Up / Ctrl+Down** move in the displayed direction. This table describes the
**default tree order**; the two keys swap roles after **Shift+R**.

| Selected item | Ctrl+Up — toward newer layers | Ctrl+Down — toward older layers |
| --- | --- | --- |
| Unstaged group, directory, file, or hunk | No move: already at the edge. | Stage the selection. |
| Staged group, directory, file, or hunk | Unstage the selection. | Move the selection into the current patch. |
| Current-patch directory, file, or hunk | Spill the selection into staged changes. | Move the selection into the adjacent older applied patch. |
| Other applied-patch directory, file, or hunk | Move the selection into the adjacent newer applied patch. | Move the selection into the adjacent older applied patch. |
| Unapplied or hidden patch contents | No move. | No move. |
| Patch node | Reorder one position toward newer patches. | Reorder one position toward older patches. |

Selection follows the moved item. Directory
and group operations include all contained files, including collapsed descendants.
Transfers and reordering stop at stack edges and do not cross the applied/unapplied
boundary.

Whole-file and directory spills require the selected paths to have no staged
changes; unstage those changes first. Hunk moves between the current patch and
staged changes preserve other staged and unstaged changes, including those in the
same file.

### Patch commands

These commands apply when the **tree** is focused.

| Key | Selected item | Action |
| --- | --- | --- |
| **n** | Any | Open the new-patch message editor. **Create** adds an empty patch on top of the applied stack. |
| **e** | Patch | Open its message editor. **Create** applies the edited message. |
| **p** | Unapplied patch | Push/apply the patch using `stg push`. StGit may reorder patches as needed. |
| **Shift+P** | Applied or current patch | Pop/unapply the patch using `stg pop`. |
| **c** | Patch | Commit the patch into the stack's base using `stg commit`. StGit rearranges the stack as needed. |
| **Shift+C** | Any, including an empty stack | Convert the newest commit beneath the stack into one applied patch using `stg uncommit --number 1`. |
| **h** | Patch | Hide the patch, unapplying it first if necessary. |
| **Shift+H** | Patch | Unhide the patch, leaving it unapplied. |
| **Delete** | Patch | Open an **OK/Cancel** confirmation before deleting it. |

### Diff preview

These commands apply when the **diff preview** is focused.

| Key | Selected item | Action |
| --- | --- | --- |
| **n / p** | Any | Select the next / previous hunk in displayed tree order, including hunks in collapsed files. Focus stays in the preview. |
| **Enter** | Unstaged/Staged file or hunk | Stage/Unstage the selection; selecting a file stages/unstages all its hunks. |
| **w** | Any | Toggle word wrap in the diff preview. |
| **Page Up / Page Down** | Any | Scroll the preview. The mouse wheel also scrolls. |
| **Tab / Shift+Tab** | Any | Move focus back to the tree. |

The preview is read-only. Insertions, deletions, and hunk headers are highlighted.

### Conflict recovery

If a stack operation fails (i.e. due to a merge conflict), a dialog is displayed:

| Button | Action |
| --- | --- |
| **Undo** | Restore the prior stack with `stg undo --hard` and retry restoring saved local changes. |
| **Refresh** | Check again after resolving the conflict in another terminal. |
| **Quit / q / Escape** | Quit to resolve the conflict outside the app. Any recovery stash is retained. |

Stack operations preserve staged, unstaged, and untracked changes. A recovery
stash is removed only after successful restoration; if a stash conflict is
resolved manually, the stash remains as a backup. Existing unresolved index
conflicts lock repository operations behind a **Refresh/Quit** dialog.

## Automatic refresh

The repository is checked every two seconds for changes. 

## Development

```console
uv sync --dev
uv run pytest
uv run ty check
uv run ruff check
```
