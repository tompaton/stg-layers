# StGit Layer Browser — Design and Development Plan

## Prior art and the workflow gap

[svenaron/stgit-tui](https://github.com/svenaron/stgit-tui) is a small Rust terminal UI for StGit. Its source includes patch and file views, unified-diff hunk parsing, and StGit/Git operations such as `stg series`, `stg files`, `stg show`, and `stg refresh`. That makes it useful prior art for browsing and interaction, but the existence of a patch/file/hunk view is not the workflow goal by itself.

[PileGit's stacked-PR workflow](https://hokwangchoi.com/blog/pilegit-stacked-prs) is commit-oriented: it organizes work into commits, then maps each commit to a PR branch. Its reorder, squash, and amend operations act on commits. That is a different abstraction from assigning in-progress file and hunk changes to layers while continuing to work.

The intended distinction is **change-level organization, not commit-level organization**. A patch/layer is a bucket for a concern such as type hints, tests, refactoring, or TODOs. A file may have hunks in several layers, and work can be reassigned between layers without first treating each layer as an atomic commit to reorder or amend. The main product question is therefore how to make those assignments safe and convenient on top of StGit's patch model—not how to rearrange commits.

StGit documents a command-line interface and stores patches as Git commits. No supported Python API was found in the initial check. Prefer StGit commands for stack information and Git's diff formats where appropriate; verify whether the installed StGit version offers a stable machine-readable format for each command rather than assuming it does. Prototype the change-level assignment operation before choosing how to implement it: StGit commands, Git's index and patch application facilities, or another supported mechanism. Do not edit StGit's internal metadata directly.

The [Hacker News discussion](https://news.ycombinator.com/item?id=41505266) highlights useful Git-based techniques, especially `git commit --fixup=<commit>` followed by `git rebase -i --autosquash`, and mentions `git absorb` and Sapling's interactive `absorb` workflow. These can inform the implementation, but they are not equivalent to the desired interaction: fixup/autosquash creates and later folds commits, while absorb tools infer a destination commit. The TUI should keep explicit file/hunk selection and destination-layer choice central, and use such operations as backend mechanisms only if tests show they preserve the user's work and StGit stack.

For unified-diff parsing, use an existing library instead of implementing a parser. [`unidiff`](https://pypi.org/project/unidiff/) is the leading candidate: its `PatchSet` API exposes files, hunks, and lines and can preserve the original diff text. Confirm its behavior on StGit/Git diff fixtures and Python 3.15 before selecting a version. The suggested [`diff-parser`](https://pypi.org/project/diff-parser/) is worth noting, but its published 1.1 README says block `content` is not implemented, so it does not appear sufficient for rendering or selecting hunks by itself. Re-evaluate it only if a later release supplies the required hunk/line structure. If no maintained compatible library meets requirements, document that finding before writing a narrow parser.

## Goal

Build a small Python 3.15 terminal application, using Textual, that visualizes StGit patches as layers and their changes as a tree:

```text
patch
└── file
    └── hunk
```

The first milestone is a read-only browser for patches, files, and hunks. Its model and adapter should be designed around a subsequent change-level workflow: inspect the current changes, select a file or hunk, and assign it to another patch without making push/pop or `stg refresh --patch` the user's manual workflow. The design must not assume that one file belongs to one patch or that each patch's contents are already an indivisible, finalized unit.

## Initial scope

- Detect the repository and current branch; report missing Git/StGit, invalid repository state, and command failures clearly.
- Read the patch series and applied/unapplied status using StGit.
- Read each patch's changed files and unified diff, then use the selected diff library to obtain file and hunk boundaries. Keep patch identity and hunk identity distinct so multiple hunks from one file can belong to different layers.
- Display a Textual tree with patch > file > hunk nodes and a diff preview for the selected item.
- Provide keyboard navigation, expand/collapse, refresh, and useful empty/error states.
- Do not change the working tree or patch stack in the browser milestone.

### Not in the first release

- Moving files or hunks between patches.
- Creating, deleting, pushing, popping, refreshing, or reordering patches.
- Supporting non-StGit Git branches as if they were StGit stacks.

## Simple design

Keep the first implementation to four responsibilities:

1. **CLI entry point and app** — start Textual from a `uv tool`-installable console command, resolve the repository working directory, and compose the screen.
2. **StGit adapter** — run `stg` and `git` with argument arrays (never a shell), capture exit status and stderr, and expose typed operations for stack, file, and diff reads.
3. **Diff model** — use `unidiff` (or another validated library) to convert unified diffs into typed `Patch`, `FileChange`, and `Hunk` records. Keep original patch names, paths, source diff, and library hunk/line data so the model can support later mutations. Do not write a second unified-diff parser.
4. **Textual widgets** — render the tree and selected diff. Keep widgets independent of subprocess execution and parsing.

Use the StGit CLI as the source of truth for stack membership and patch state. Obtain diffs from `stg show` or Git against the patch commits, whichever proves simplest and most reliable for applied and unapplied patches. Prefer documented structured output or explicit formatting options when available. Delegate diff syntax handling to the chosen library; add only the application-specific mapping and guards needed for quoted paths, renames, binary files, and unsupported diff sections.

Keep reading and mutation as separate adapter responsibilities. Before enabling mutation, define what happens to a selected hunk as it is transferred between patch layers, including the relationship between committed patch contents, the index, and worktree changes. Validate stack state before acting, preserve unrelated changes, and never edit StGit's internal metadata directly.

## Packaging and quality

- `pyproject.toml` with a `requires-python` floor of `>=3.15`, a console-script entry point, and dependencies declared for `uv`.
- Build a distributable wheel so installation works with `uv tool install .` and published packages can be installed the same way.
- Use Textual for the TUI and type annotations throughout application code.
- Use pytest for unit tests and isolated StGit integration tests.
- Use `ty check` for static type checking.
- Use Ruff for formatting and linting; enable McCabe complexity diagnostics (`C901`) with a defined maximum complexity.
- Use a maintained unified-diff library (initial candidate: `unidiff`) rather than implementing patch parsing; verify its API, edge-case behavior, and Python 3.15 compatibility before adding/pinning it.
- Keep the tool's runtime requirements limited to Python and the external `stg`/`git` executables.
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
- Parse patch series and map the chosen unified-diff library's file/hunk/line objects into the tree model.
- Add library behavior tests using representative fixtures, plus integration tests that create a temporary Git repository and StGit stack. Test quoted paths, renames, binary files, and unsupported diff sections without duplicating the library's parser.

### 4. Build the tree browser

- Render patch > file > hunk nodes and an adjacent diff preview.
- Add keyboard navigation, expand/collapse, and refresh.
- Manually verify behavior for empty patches, unapplied patches, file renames, binary files, and command errors.

### 5. Design and validate change assignment

- Prototype changes only in disposable repositories.
- Determine a supported sequence for assigning an isolated hunk from one layer to another, including moving unassigned work into a layer, while preserving patch order, unrelated hunks, pre-existing index/worktree changes, and stack invariants.
- Test multiple hunks from one file in different layers, edits to earlier layers, and moving a TODO hunk out of the current layer.
- Compare explicit hunk staging and destination selection against fixup/autosquash and absorb-based approaches. Record when the mechanism rewrites history, how it handles conflicts, and how the UI can preview and recover from an operation.
- Add dry-run/preview and recovery behavior before enabling writes.
- Keep any required lower-level Git mechanics behind the adapter; implement mutation only after integration tests demonstrate the change-level workflow without turning it into commit reordering or requiring manual push/pop or `stg refresh --patch`.

## Acceptance criteria for the initial release

- `uv tool install .` installs a working command on Python 3.15.
- In an StGit repository, the command displays the stack as an expandable patch > file > hunk tree and previews the selected diff.
- It gives a clear error for a missing `stg`/`git`, a non-repository directory, or a failed StGit command.
- Browsing and refreshing the view do not modify Git or StGit state.
- pytest, `ty check`, and Ruff linting pass; complexity diagnostics are enabled.

## Main risks

- Existing StGit TUI functionality may overlap with browsing and staging, but commit-level stacking does not by itself solve the intended change-level assignment workflow.
- StGit output and capabilities can vary by version. Keep command handling isolated and test against actual supported versions.
- Safely assigning a hunk between layers is more involved than displaying one: it must preserve patch ordering and unrelated user changes even when one file spans multiple layers. Treat it as a separate milestone with disposable-repository tests.
- Python 3.15 support in Textual and build dependencies should be verified before pinning versions or claiming compatibility.
