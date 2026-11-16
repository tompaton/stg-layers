# StGit Layer Browser — Design and Development Plan

## Prior art and the workflow gap

[svenaron/stgit-tui](https://github.com/svenaron/stgit-tui) is a small Rust terminal UI for StGit. Its source includes patch and file views, unified-diff hunk parsing, and StGit/Git operations such as `stg series`, `stg files`, `stg show`, and `stg refresh`. That makes it useful prior art for browsing and interaction, but the existence of a patch/file/hunk view is not the workflow goal by itself.

[PileGit's stacked-PR workflow](https://hokwangchoi.com/blog/pilegit-stacked-prs) is commit-oriented: it organizes work into commits, then maps each commit to a PR branch. Its reorder, squash, and amend operations act on commits. That is a different abstraction from assigning in-progress file and hunk changes to layers while continuing to work.

The intended distinction is **change-level organization, not commit-level organization**. A patch/layer is a bucket for a concern such as type hints, tests, refactoring, or TODOs. A file may have hunks in several layers, and work can be reassigned between layers without first treating each layer as an atomic commit to reorder or amend. The main product question is therefore how to make those assignments safe and convenient on top of StGit's patch model—not how to rearrange commits.

StGit documents a command-line interface and stores patches as Git commits. No supported Python API was found in the initial check. Prefer StGit commands for stack information and Git's diff formats where appropriate; verify whether the installed StGit version offers a stable machine-readable format for each command rather than assuming it does. Prototype the change-level assignment operation before choosing how to implement it: StGit commands, Git's index and patch application facilities, or another supported mechanism. Do not edit StGit's internal metadata directly.

The [Hacker News discussion](https://news.ycombinator.com/item?id=41505266) highlights useful Git-based techniques, especially `git commit --fixup=<commit>` followed by `git rebase -i --autosquash`, and mentions `git absorb` and Sapling's interactive `absorb` workflow. These can inform the implementation, but they are not equivalent to the desired interaction: fixup/autosquash creates and later folds commits, while absorb tools infer a destination commit. The TUI should keep explicit file/hunk selection and destination-layer choice central, and use such operations as backend mechanisms only if tests show they preserve the user's work and StGit stack.

For unified-diff parsing, use an existing library instead of implementing a parser. [`unidiff`](https://pypi.org/project/unidiff/) is the leading candidate: its `PatchSet` API exposes files, hunks, and lines and can preserve the original diff text. Confirm its behavior on StGit/Git diff fixtures and Python 3.15 before selecting a version. The suggested [`diff-parser`](https://pypi.org/project/diff-parser/) is worth noting, but its published 1.1 README says block `content` is not implemented, so it does not appear sufficient for rendering or selecting hunks by itself. Re-evaluate it only if a later release supplies the required hunk/line structure. If no maintained compatible library meets requirements, document that finding before writing a narrow parser.

## Implementation and prototype findings

- The initial read-only Textual browser is implemented. It uses `stg series` for patch state, `stg show` for patch diffs, `git diff --cached --patch` for staged changes, `git diff --patch` plus no-index diffs for non-ignored untracked files, and `unidiff` for file/hunk parsing. On the tested StGit version, `stg show` must receive `--diff-opt=--patch` to produce unified diffs.
- In a disposable stack, staging a worktree hunk and running `stg refresh --index --patch <earlier-applied-patch>` moved that hunk into the earlier patch while leaving a separate unstaged worktree hunk unchanged. The integration test simulates the UI's selection with `git add -p`; the application does not invoke Git's interactive prompt.
- Adjacent file/hunk transfers between applied patches are implemented. The adapter temporarily pops patches above the source, spills the change, removes or reapplies the selected diff through Git's index, refreshes the current patch, then restores the affected patches. It stashes staged, unstaged, and untracked work once for the full transaction and tracks StGit undo steps for rollback.
- Integration tests cover whole-file moves in both directions, isolated hunks with sibling hunks retained in the source, binary files, later patches, local staged/unstaged/untracked work, applied/unapplied boundaries, and both automatic rollback and conflict recovery. StGit `refresh` creates two undo records in the tested version, so transaction rollback accounts for both.

## Goal

Build a small Python 3.15 terminal application, using Textual, that visualizes an StGit repository as an interactive change tree:

```text
Repository
├── Patch stack
│   └── patch
│       └── file
│           └── hunk
├── Staged changes
│   └── file
│       └── hunk
└── Unstaged changes
    └── file
        └── hunk
```

The application visualizes the patch stack and current index/worktree changes, and supports assigning a file or hunk to an adjacent applied patch without making manual push/pop or `stg refresh --patch` the user's workflow. The design does not assume that one file belongs to one patch or that each patch's contents are an indivisible, finalized unit.

## Initial scope

- Detect the repository and current branch; report missing Git/StGit, invalid repository state, and command failures clearly.
- Read the patch series and applied/unapplied status using StGit.
- Read each patch's changed files and unified diff, then use the selected diff library to obtain file and hunk boundaries. Keep patch identity and hunk identity distinct so multiple hunks from one file can belong to different layers.
- Display a Textual tree with a reversible order. Default newest-first order is `Unstaged changes`, `Staged changes`, `Patch stack`, with patches newest to oldest; Shift+R switches to `Patch stack`, `Staged changes`, `Unstaged changes`, with patches oldest to newest. Under the patch stack show patch > file > hunk; under staged and unstaged show file > hunk.
- Read staged changes with `git diff --cached --patch`. Read unstaged tracked changes with `git diff --patch` and include non-ignored untracked files as additions; parse all diffs with the same library as StGit patch diffs.
- Show explicit empty states for an empty patch stack and for no staged or unstaged changes.
- Start with top-level sections, patches, and files expanded so files are visible immediately; keep each file's hunks collapsed until expanded with Right.
- Display the selected patch, staged/unstaged group, file, or hunk diff in a read-only, focusable TextArea with scrolling and Tree-sitter highlighting for additions, removals, and hunk headers. Do not show sequential preview line numbers, which do not correspond to source-file line numbers.
- Style tree labels so section headings are bold, patch labels use `[status patch-id] title` with `>`, `+`, `-`, or `~` status markers and the whole bracketed ID colored by status, file paths and file statuses are distinguishable, and hunk headers are easy to scan.
- Up/Down moves between visible nodes; Right expands a collapsed node or moves to the first child; Left collapses an expanded node or moves to its parent. Highlighting a node immediately updates the preview; Enter acts on files and hunks in the staged and unstaged sections.
- Tab moves focus between the tree and preview. Hide horizontal overflow in the tree rather than showing a horizontal scrollbar.
- Provide refresh and useful empty/error states.
- Do not change the index, working tree, or patch stack in the browser milestone.

### Current boundaries

- Moving files or hunks to non-adjacent patches or across the applied/unapplied boundary.
- Deleting patches or manually pushing/popping and refreshing them.
- Supporting non-StGit Git branches as if they were StGit stacks.

## Simple design

Keep the first implementation to four responsibilities:

1. **CLI entry point and app** — start Textual from a `uv tool`-installable console command, resolve the repository working directory, and compose the screen.
2. **StGit adapter** — run `stg` and `git` with argument arrays (never a shell), capture exit status and stderr, and expose typed operations for stack, file, and diff reads.
3. **Diff model** — use `unidiff` (or another validated library) to convert StGit patch diffs and staged/unstaged Git diffs into typed `Patch`, `FileChange`, and `Hunk` records. Keep source/category, patch identity, paths, source diff, and library hunk/line data so the model can support later mutations. Do not write a second unified-diff parser.
4. **Textual widgets** — render the tree and selected diff. Use a read-only TextArea for the preview so it can receive keyboard focus, scroll independently, and highlight unified-diff syntax with Tree-sitter. Keep widgets independent of subprocess execution and parsing.

Use the StGit CLI as the source of truth for stack membership and patch state. Obtain patch diffs from `stg show` or Git against the patch commits, whichever proves simplest and most reliable for applied and unapplied patches. Obtain index changes from `git diff --cached --patch` and worktree changes from `git diff --patch`, and include non-ignored untracked files in the unstaged group as additions. Keep these categories separate and do not merge or count changes twice. Disable external diff drivers and text conversion for predictable, read-only output. Prefer documented structured output or explicit formatting options when available. Delegate diff syntax handling to the chosen library; add only the application-specific mapping and guards needed for quoted paths, renames, binary files, and unsupported diff sections.

Keep reading and mutation as separate adapter responsibilities. Before enabling mutation, define what happens to a selected hunk as it is transferred between patch layers, including the relationship between committed patch contents, the index, and worktree changes. Validate stack state before acting, preserve unrelated changes, and never edit StGit's internal metadata directly.

## Packaging and quality

- `pyproject.toml` with a `requires-python` floor of `>=3.15`, a console-script entry point, and dependencies declared for `uv`.
- Build a distributable wheel so installation works with `uv tool install .` and published packages can be installed the same way.
- Use Textual for the TUI and type annotations throughout application code.
- Use pytest for unit tests and isolated StGit integration tests.
- Use `ty check` for static type checking.
- Use Ruff for formatting and linting; enable McCabe complexity diagnostics (`C901`) with a defined maximum complexity.
- Use a maintained unified-diff library (initial candidate: `unidiff`) rather than implementing patch parsing; verify its API, edge-case behavior, and Python 3.15 compatibility before adding/pinning it.
- Use Textual's TextArea and a Tree-sitter grammar for unified diffs; pin the grammar to a tagged upstream release and verify installation and rendering on Python 3.15.
- Keep runtime dependencies within Python packages plus the external `stg`/`git` executables; do not require additional services or Git extensions.
- `tree-sitter-diff` is currently sourced from its tagged GitHub release because it is not published on PyPI. `uv tool install .` from a checkout is verified; revisit the grammar distribution source before publishing this application to PyPI.
- Verify that Textual and the packaging/build dependencies support Python 3.15 before fixing dependency versions.

## Development plan

### 1. Confirm the change-level model

- Evaluate `stgit-tui` as prior art for its tree, diff display, and existing StGit operations; evaluate PileGit as prior art for commit-oriented stacking.
- Specify the target interaction with a concrete case: two hunks in one file assigned to different layers, with an additional unassigned working-tree hunk.
- Decide how the UI represents committed patch content versus unassigned index/worktree changes. Do not equate moving/reordering whole commits with assigning individual changes to layers.
- Prototype the assignment semantics in a disposable repository and confirm the workflow does not require the user to manually push/pop or use `stg refresh --patch`.
- Compare Git mechanisms for carrying the selected change to its chosen layer: interactive hunk staging (for example `git add -p`), a fixup commit plus `git rebase -i --autosquash`, and `git absorb`/Sapling-style absorb. Determine which are suitable for explicit destination selection, and whether any operation rewrites commits or refs.
- Treat `git rebase --update-refs` and `git rebase --onto` as possible stack/ref maintenance tools, not as hunk-selection mechanisms. Check compatibility with StGit before depending on them.

### 2. Establish the package and tooling

- Add package metadata, the console script, and the Textual application skeleton.
- Configure pytest, `ty`, and Ruff, including complexity warnings.
- Verify installation and startup with `uv tool install .` in a clean environment.

### 3. Implement and test read-only data loading

- Probe the supported StGit commands and output formats against a disposable repository, including applied and unapplied patches.
- Implement typed subprocess wrappers and actionable error reporting.
- Read staged and unstaged diffs separately with Git, including non-ignored untracked files under unstaged; map patch series and each diff source into the shared tree model.
- Parse changes with the chosen unified-diff library's file/hunk/line objects.
- Add library behavior tests using representative fixtures, plus integration tests that create a temporary Git repository and StGit stack. Verify changes staged and unstaged in the same file remain in distinct groups. Test quoted paths, renames, binary files, and unsupported diff sections without duplicating the library's parser.

### 4. Build the tree browser

- Render the top-level groups and patch sequence in the selected order (newest-first by default, reversible with Shift+R), with patch > file > hunk and file > hunk trees respectively, plus an adjacent diff preview.
- Use a read-only TextArea for the diff preview, with Tree-sitter highlighting and no misleading sequential line numbers. Support Tab focus between the tree and preview, and scroll the preview with keyboard or mouse.
- Bind Up/Down to visible-node navigation, Left/Right to collapse/parent and expand/first-child behavior, and update the preview immediately on node highlighting. Disable horizontal tree overflow so long labels are clipped rather than adding a horizontal scrollbar.
- Default to newest-first navigation: unstaged, staged, then patches newest to oldest. Shift+R switches to the existing oldest-first order; keep the order indicator in the status bar.
- Start with all top-level sections and patches expanded, and file nodes collapsed so hunks are hidden until requested.
- Apply semantic Rich styles to tree labels: bold section headings; patch labels in `[status patch-id] title` form with `>`, `+`, `-`, or `~` status markers and the whole bracketed ID colored by status; distinct colors for patch ID, file path/status, and hunk header.
- Add keyboard navigation, expand/collapse, and refresh.
- Bind Enter on files and hunks under Staged/Unstaged to Git index operations: stage or unstage a whole file, or only the selected hunk. Keep patch-stack nodes read-only and preserve unrelated hunks; use Git's patch-selection commands through the repository adapter rather than shell fragments or a custom patch parser.
- Bind `c` in the patch tree to open a message editor initialized with `stg new --save-template`; Create writes the edited template to a temporary file and calls `stg new --file` to add an empty patch at the top of the applied stack. Cancel without changing the stack.
- Bind `e` on a patch node to open the same editor with `stg edit --save-template <file> <patch>`; Create applies the edited message with `stg edit --file <file> <patch>`. Keep non-patch tree nodes inert and allow any patch in the stack.
- In the default order, bind Ctrl+Down on an unstaged file or hunk to stage it, or on a staged file to refresh its staged contents into the current patch with `stg refresh --index`. In reversed order, Ctrl+Up performs these operations. Because the installed StGit version disallows path arguments with `--index`, temporarily remove other staged file changes from the index and restore them after the refresh; preserve unstaged worktree changes.
- In the default order, bind Ctrl+Up on a staged file or hunk to unstage it, or on a file under the current patch to use `stg spill -- <file>` and move the patch change into Staged changes. In reversed order, Ctrl+Down performs these operations. Preserve unrelated staged files around `stg spill`, which requires a clean index; reject the operation if the selected file itself already has staged changes, and leave its unstaged worktree edits intact.
- After Enter moves a file or hunk between staged and unstaged changes, refresh the tree and preview, then select the next file or hunk in the source group. For Ctrl+Up/Ctrl+Down staged/unstaged moves, keep selection on the moved item when it still exists (otherwise its file). Retain the file's expanded state in either case.
- On a patch node, move exactly one position in the selected direction. For applied patches, use `stg sink --above/--below <adjacent patch> <selected patch>`. For unapplied patches, use `stg float --noapply` with an ordered suffix list that swaps only the selected patch and its adjacent neighbor. Do not cross the applied/unapplied boundary; reverse the Ctrl+Up/Ctrl+Down mapping with the displayed patch order.
- Before patch reordering or push/pop, stash staged, unstaged, and untracked changes, run the operation, then restore with `git stash apply --index` so the staged split is preserved. Drop only the exact saved stash after successful restoration. A stack-operation or stash-restore conflict offers `stg undo --hard`, Quit, or Refresh; Undo restores the prior stack and retries restoration, Quit leaves the stash intact, and Refresh restores or retains the stash as a backup after manual resolution.
- Before loading or mutating repository state, detect unmerged index entries and lock the UI behind Refresh/Quit. Refresh retries detection and reloads only when conflicts have been resolved externally.
- Manually verify behavior for empty patches, empty staged/unstaged changes, unapplied patches, file renames, binary files, and command errors.

### 5. Implement adjacent patch assignment

- Implement file and isolated-hunk transfers between adjacent applied patches through the repository adapter. Moves are limited to applied neighbors and do not cross the applied/unapplied boundary.
- Use StGit stack operations and Git's cached patch application to preserve unselected hunks. When needed, pop patches above the source, refresh the source, refresh the destination as current, and restore the popped patches.
- Stash staged, unstaged, and untracked local changes for the whole transaction; restore their staged split after success. Automatically undo completed steps on non-conflict failures, and use the existing Undo/Quit/Refresh recovery for conflicts.
- Preserve selection and expanded-file state after a move, reversing Ctrl+Up/Ctrl+Down with the tree order. Keep current-patch file spilling to Staged changes when moving toward a newer patch is not possible.
- Defer non-adjacent destinations, unapplied-patch transfers, dry-run previews, and any operation that edits StGit's internal metadata directly.

## Acceptance criteria for the initial release

- `uv tool install .` installs a working command on Python 3.15.
- In an StGit repository, the command defaults to Unstaged changes, Staged changes, then Patch stack with patches newest to oldest. Shift+R reverses to Patch stack oldest to newest, Staged changes, then Unstaged changes. Both modes show patch > file > hunk in the stack and file > hunk under staged and unstaged (including non-ignored untracked files), and preview the selected diff.
- It gives a clear error for a missing `stg`/`git`, a non-repository directory, or a failed StGit command.
- Empty staged and unstaged sections are visible when there are no changes in those areas.
- Enter stages or unstages the selected working-change file/hunk without changing working-tree contents or the StGit patch stack; unrelated hunks are preserved, and selection advances through the source group after each operation.
- Ctrl+Down stages an unstaged file or hunk, then moves a staged file into the current patch. Ctrl+Up unstages a staged file or hunk, and spills a current-patch file into the index. The selection follows staged/unstaged moves, the preview refreshes, and expanded-file state is retained.
- In reversed display order, the Ctrl+Up and Ctrl+Down actions swap to follow visual direction: Ctrl+Up stages/moves toward the patch stack, and Ctrl+Down spills/unstages toward Unstaged changes.
- On patch nodes, Ctrl+Up moves one position toward newer patches and Ctrl+Down moves one position toward older patches in newest-first order; those actions reverse in oldest-first order. Stack edges and the applied/unapplied boundary are respected. Reordering stashes and restores pre-existing local changes, preserving staged state. When stack-operation conflicts occur, Undo restores the original stack and local changes, Quit exits to manual terminal recovery with the stash retained, and Refresh retries/restores changes after manual resolution.
- On a file or hunk in an applied patch, Ctrl+Up/Down moves it to the adjacent applied patch in the visual direction; reversing the tree reverses the key direction. A file moves as a whole, while a hunk move preserves sibling hunks. Selection follows the moved file/hunk, and the applied/unapplied boundary is not crossed.
- On an unapplied patch, `p` runs `stg push <patch>`; on an applied/current patch, Shift+P runs `stg pop <patch>`. Push and pop stash and restore local changes, preserving staged state, and use the same Undo/Quit/Refresh conflict recovery.
- If Git has unmerged index entries, the app performs no repository operations until the user resolves them elsewhere and selects Refresh, or quits to the terminal.
- `c` in the patch tree opens StGit's generated patch-message template; creating it adds an empty top patch, and cancelling leaves the stack unchanged.
- `e` on any patch node edits its StGit-generated message; other tree nodes do not trigger patch editing, and cancelling leaves the patch unchanged.
- Ctrl+Down on a staged file moves its staged contents into the current patch while preserving unrelated staged files and unstaged worktree changes; Ctrl+Down on a staged hunk does not move it into a patch in newest-first order.
- Ctrl+Up on a current-patch file spills its patch change into the index while preserving unrelated staged files and unstaged worktree changes; hunk nodes and files in other patches are not spilled in newest-first order.
- pytest, `ty check`, and Ruff linting pass; complexity diagnostics are enabled.

## Main risks

- Existing StGit TUI functionality may overlap with browsing and staging, but commit-level stacking does not by itself solve the intended change-level assignment workflow.
- StGit output and capabilities can vary by version. Keep command handling isolated and test against actual supported versions.
- Safely assigning a hunk between layers is more involved than displaying one: it must preserve patch ordering and unrelated user changes even when one file spans multiple layers. Treat it as a separate milestone with disposable-repository tests.
- Python 3.15 support in Textual and build dependencies should be verified before pinning versions or claiming compatibility.
