import asyncio
from typing import Protocol

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets._tree import TreeNode

from stg_layers.models import FileChange, FileStatus, Patch, TreeItem
from stg_layers.repository import RepositoryError


class StackReader(Protocol):
    branch: str | None

    def load_stack(self) -> tuple[Patch, ...]: ...

    def load_staged_changes(self) -> tuple[FileChange, ...]: ...

    def load_unstaged_changes(self) -> tuple[FileChange, ...]: ...


class StgLayersApp(App[None]):
    TITLE = "StGit Layers"
    CSS = """
    #main {
        height: 1fr;
    }

    #tree-pane {
        width: 42%;
        border: round $primary;
    }

    #preview-pane {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }

    #selection-title {
        height: auto;
        text-style: bold;
        padding-bottom: 1;
    }

    #diff-view {
        height: 1fr;
        overflow: auto;
    }

    #status {
        height: 1;
        padding: 0 1;
    }
    """
    BINDINGS = [
        Binding("r", "refresh_stack", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, repository: StackReader) -> None:
        super().__init__()
        self.repository = repository
        self.sub_title = repository.branch or "detached HEAD"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="tree-pane"):
                tree: Tree[TreeItem] = Tree("Repository", id="patch-tree")
                tree.root.expand()
                yield tree
            with Vertical(id="preview-pane"):
                yield Static("Select a patch, file, or hunk.", id="selection-title", markup=False)
                yield Static("", id="diff-view", markup=False)
        yield Static("Loading StGit stack...", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_stack()

    def action_refresh_stack(self) -> None:
        self.run_worker(self._load_stack(), exclusive=True)

    @on(Tree.NodeSelected)
    def on_tree_node_selected(self, event: Tree.NodeSelected[TreeItem]) -> None:
        item = event.node.data
        if item is None:
            return
        self.query_one("#selection-title", Static).update(item.title)
        self.query_one("#diff-view", Static).update(item.diff_text or "No text diff available.")

    async def _load_stack(self) -> None:
        status = self.query_one("#status", Static)
        status.update("Loading patch stack and working changes...")
        try:
            patches, staged, unstaged = await asyncio.gather(
                asyncio.to_thread(self.repository.load_stack),
                asyncio.to_thread(self.repository.load_staged_changes),
                asyncio.to_thread(self.repository.load_unstaged_changes),
            )
        except RepositoryError as error:
            self._show_error(error)
            return
        self._populate_tree(patches, staged, unstaged)
        count = len(patches)
        status.update(f"Loaded {count} patch{'es' if count != 1 else ''}. Press r to refresh.")

    def _show_error(self, error: RepositoryError) -> None:
        tree = self.query_one("#patch-tree", Tree)
        tree.root.remove_children()
        tree.root.add("Unable to load StGit stack", allow_expand=False)
        self.query_one("#status", Static).update(f"Error: {error}")

    def _populate_tree(
        self,
        patches: tuple[Patch, ...],
        staged: tuple[FileChange, ...],
        unstaged: tuple[FileChange, ...],
    ) -> None:
        tree = self.query_one("#patch-tree", Tree)
        tree.root.remove_children()
        patch_stack = tree.root.add(
            Text("Patch stack"),
            data=TreeItem(title="Patch stack", diff_text=""),
            expand=True,
        )
        if not patches:
            patch_stack.add("No patches in this stack.", allow_expand=False)
        else:
            for patch in patches:
                self._add_patch(patch_stack, patch)

        self._add_change_group(tree.root, "Staged changes", "staged", staged)
        self._add_change_group(tree.root, "Unstaged changes", "unstaged", unstaged)

    @staticmethod
    def _add_change_group(
        parent: TreeNode[TreeItem],
        label: str,
        change_area: str,
        files: tuple[FileChange, ...],
    ) -> None:
        group = parent.add(
            Text(label),
            data=TreeItem(title=label, diff_text="", change_area=change_area),
            expand=True,
            allow_expand=True,
        )
        if not files:
            group.add(f"No {change_area} changes.", allow_expand=False)
            return

        for file in files:
            StgLayersApp._add_file(group, file, None, change_area)

    @staticmethod
    def _add_patch(parent: TreeNode[TreeItem], patch: Patch) -> None:
        status = f"[{patch.status.value}]"
        label = f"{status} {patch.name}"
        if patch.description:
            label = f"{label} - {patch.description}"
        patch_node = parent.add(
            Text(label),
            data=TreeItem(
                title=f"Patch: {patch.name}",
                diff_text=patch.diff_text,
                patch_name=patch.name,
            ),
            expand=True,
        )
        if not patch.files:
            patch_node.add("(empty patch)", allow_expand=False)
            return

        for file in patch.files:
            StgLayersApp._add_file(patch_node, file, patch.name, None)

    @staticmethod
    def _add_file(
        parent: TreeNode[TreeItem],
        file: FileChange,
        patch_name: str | None,
        change_area: str | None,
    ) -> None:
        display_path = file.path
        if file.status == FileStatus.RENAMED:
            display_path = f"{file.source_path} -> {file.target_path}"
        if patch_name is None:
            if change_area is None:
                raise ValueError("A file must belong to a patch or change area.")
            section = f"{change_area.title()} changes"
        else:
            section = patch_name
        file_node = parent.add(
            Text(f"{display_path} [{file.status.value}]"),
            data=TreeItem(
                title=f"{section} / {display_path}",
                diff_text=file.diff_text,
                patch_name=patch_name,
                file_path=file.path,
                change_area=change_area,
            ),
            expand=bool(file.hunks),
            allow_expand=bool(file.hunks),
        )
        for hunk in file.hunks:
            file_node.add(
                Text(hunk.header),
                data=TreeItem(
                    title=f"{section} / {display_path} / hunk {hunk.index + 1}",
                    diff_text=hunk.diff_text,
                    patch_name=patch_name,
                    file_path=file.path,
                    hunk_index=hunk.index,
                    change_area=change_area,
                ),
                allow_expand=False,
            )
