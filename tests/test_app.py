import asyncio

from textual.widgets import Static, TextArea, Tree

from stg_layers.app import StgLayersApp
from stg_layers.models import DiffHunk, FileChange, FileStatus, Patch, PatchStatus


class FixtureRepository:
    branch: str | None = "feature/read-browser"

    def _file_change(self, path: str, prefix: str, content: str) -> FileChange:
        hunk = DiffHunk(
            index=0,
            header="@@ -1 +1 @@",
            diff_text=f"@@ -1 +1 @@\n-{prefix}\n+{content}\n",
            source_start=1,
            source_length=1,
            target_start=1,
            target_length=1,
        )
        return FileChange(
            path=path,
            source_path=f"a/{path}",
            target_path=f"b/{path}",
            status=FileStatus.MODIFIED,
            diff_text=f"diff --git a/{path} b/{path}\n{hunk.diff_text}",
            hunks=(hunk,),
        )

    def load_stack(self) -> tuple[Patch, ...]:
        return (
            Patch(
                name="types [notes]",
                status=PatchStatus.CURRENT,
                description="Type [annotations]",
                files=(self._file_change("example [typing].py", "before", "after"),),
            ),
        )

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        return (self._file_change("staged.py", "old staged", "new staged"),)

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        return (self._file_change("unstaged.py", "old unstaged", "new unstaged"),)


def test_app_renders_patch_file_hunk_tree() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            assert [str(child.label) for child in tree.root.children] == [
                "Patch stack",
                "Staged changes",
                "Unstaged changes",
            ]
            patch_stack = tree.root.children[0]
            patch_node = patch_stack.children[0]
            file_node = patch_node.children[0]
            hunk_node = file_node.children[0]
            assert "types [notes]" in str(patch_node.label)
            assert "example [typing].py" in str(file_node.label)
            assert "@@ -1 +1 @@" in str(hunk_node.label)
            assert hunk_node.data is not None
            assert hunk_node.data.patch_name == "types [notes]"
            assert hunk_node.data.file_path == "example [typing].py"
            assert hunk_node.data.hunk_index == 0
            assert app.sub_title == "feature/read-browser"

            staged_group = tree.root.children[1]
            staged_file = staged_group.children[0]
            staged_hunk = staged_file.children[0]
            assert "staged.py" in str(staged_file.label)
            assert staged_hunk.data is not None
            assert staged_hunk.data.change_area == "staged"

            unstaged_group = tree.root.children[2]
            unstaged_file = unstaged_group.children[0]
            unstaged_hunk = unstaged_file.children[0]
            assert "unstaged.py" in str(unstaged_file.label)
            assert unstaged_hunk.data is not None
            assert unstaged_hunk.data.change_area == "unstaged"

            assert tree.root.is_expanded
            tree.focus()
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            title = app.query_one("#selection-title", Static).render()
            assert "Patch: types [notes]" in str(title)

            tree.select_node(hunk_node)
            await pilot.pause()
            title = app.query_one("#selection-title", Static).render()
            diff = app.query_one("#diff-view", TextArea)
            assert "hunk 1" in str(title)
            assert "+after" in diff.text
            assert diff.read_only
            assert diff.language == "diff"
            assert diff.theme == "stg-diff"
            assert any(name == "attribute" for _, _, name in diff._highlights[0])
            assert any(name == "diff.minus" for _, _, name in diff._highlights[1])
            assert any(name == "diff.plus" for _, _, name in diff._highlights[2])

    asyncio.run(run_app())


def test_diff_preview_can_focus_and_scroll_without_editing() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            preview = app.query_one("#diff-view", TextArea)
            original_text = "\n".join(f"+added line {index}" for index in range(100))
            preview.load_text(original_text)
            tree.focus()
            await pilot.press("tab")
            await pilot.pause()

            assert preview.has_focus
            assert preview.read_only
            await pilot.press("pagedown")
            await pilot.pause()
            assert preview.scroll_y > 0
            await pilot.press("a")
            assert preview.text == original_text

    asyncio.run(run_app())


def test_app_shows_empty_staged_and_unstaged_sections() -> None:
    class EmptyRepository(FixtureRepository):
        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        app = StgLayersApp(EmptyRepository())
        async with app.run_test():
            tree = app.query_one("#patch-tree", Tree)
            assert "No staged changes." in str(tree.root.children[1].children[0].label)
            assert "No unstaged changes." in str(tree.root.children[2].children[0].label)
            assert tree.root.children[1].is_expanded
            assert tree.root.children[2].is_expanded

    asyncio.run(run_app())
