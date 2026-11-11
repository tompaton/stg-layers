import asyncio

from textual.widgets import Static, Tree

from stg_layers.app import StgLayersApp
from stg_layers.models import DiffHunk, FileChange, FileStatus, Patch, PatchStatus


class FixtureRepository:
    branch: str | None = "feature/read-browser"

    def load_stack(self) -> tuple[Patch, ...]:
        hunk = DiffHunk(
            index=0,
            header="@@ -1 +1 @@",
            diff_text="@@ -1 +1 @@\n-before\n+after\n",
            source_start=1,
            source_length=1,
            target_start=1,
            target_length=1,
        )
        file = FileChange(
            path="example [typing].py",
            source_path="a/example [typing].py",
            target_path="b/example [typing].py",
            status=FileStatus.MODIFIED,
            diff_text="diff --git a/example.py b/example.py\n" + hunk.diff_text,
            hunks=(hunk,),
        )
        return (
            Patch(
                name="types [notes]",
                status=PatchStatus.CURRENT,
                description="Type [annotations]",
                files=(file,),
            ),
        )


def test_app_renders_patch_file_hunk_tree() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch_node = tree.root.children[0]
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

            assert tree.root.is_expanded
            tree.focus()
            await pilot.press("down", "enter")
            await pilot.pause()
            title = app.query_one("#selection-title", Static).render()
            assert "Patch: types [notes]" in str(title)

            tree.select_node(hunk_node)
            await pilot.pause()
            title = app.query_one("#selection-title", Static).render()
            diff = app.query_one("#diff-view", Static).render()
            assert "hunk 1" in str(title)
            assert "+after" in str(diff)

    asyncio.run(run_app())
