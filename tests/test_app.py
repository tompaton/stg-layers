import asyncio

from rich.text import Text
from textual.widgets import Static, TextArea, Tree

from stg_layers.app import StgLayersApp
from stg_layers.models import DiffHunk, FileChange, FileStatus, Patch, PatchStatus


class FixtureRepository:
    branch: str | None = "feature/read-browser"

    def __init__(self) -> None:
        self.actions: list[tuple[str, str, int | None]] = []
        self.created_messages: list[str] = []
        self.edited_patches: list[tuple[str, str]] = []
        self.moved_files: list[str] = []

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

    def stage_file(self, path: str) -> None:
        self.actions.append(("stage_file", path, None))

    def unstage_file(self, path: str) -> None:
        self.actions.append(("unstage_file", path, None))

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        self.actions.append(("stage_hunk", path, hunk_index))

    def unstage_hunk(self, path: str, hunk_index: int) -> None:
        self.actions.append(("unstage_hunk", path, hunk_index))

    def new_patch_template(self) -> str:
        return "Patch: \nAuthor: Test <test@example.invalid>\n\n# Edit this message\n"

    def create_patch(self, message: str) -> None:
        self.created_messages.append(message)

    def edit_patch_template(self, patch_name: str) -> str:
        return f"Patch: {patch_name}\nAuthor: Test <test@example.invalid>\n\nPatch body\n"

    def edit_patch(self, patch_name: str, message: str) -> None:
        self.edited_patches.append((patch_name, message))

    def refresh_staged_file_to_current_patch(self, path: str) -> None:
        self.moved_files.append(path)


def test_app_renders_patch_file_hunk_tree() -> None:
    async def run_app() -> None:
        app = StgLayersApp(FixtureRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            assert [str(child.label) for child in tree.root.children] == [
                "Unstaged changes",
                "Staged changes",
                "Patch stack",
            ]
            patch_stack = tree.root.children[2]
            patch_node = patch_stack.children[0]
            file_node = patch_node.children[0]
            hunk_node = file_node.children[0]
            assert "types [notes]" in str(patch_node.label)
            assert "example [typing].py" in str(file_node.label)
            assert "@@ -1 +1 @@" in str(hunk_node.label)
            assert isinstance(patch_stack.label, Text)
            assert isinstance(patch_node.label, Text)
            assert isinstance(file_node.label, Text)
            assert isinstance(hunk_node.label, Text)
            assert str(patch_stack.label.style) == "bold cyan"
            assert str(patch_node.label) == "[>types [notes]] Type [annotations]"
            assert str(patch_node.label.spans[0].style) == "bold green"
            assert str(file_node.label.spans[0].style) == "cyan"
            assert str(file_node.label.spans[1].style) == "blue"
            assert str(hunk_node.label.style) == "yellow"
            assert hunk_node.data is not None
            assert hunk_node.data.patch_name == "types [notes]"
            assert hunk_node.data.file_path == "example [typing].py"
            assert hunk_node.data.hunk_index == 0
            assert app.sub_title == "feature/read-browser"

            assert patch_stack.is_expanded
            assert patch_node.is_expanded
            assert file_node.is_collapsed

            staged_group = tree.root.children[1]
            staged_file = staged_group.children[0]
            staged_hunk = staged_file.children[0]
            assert "staged.py" in str(staged_file.label)
            assert staged_hunk.data is not None
            assert staged_hunk.data.change_area == "staged"

            unstaged_group = tree.root.children[0]
            unstaged_file = unstaged_group.children[0]
            unstaged_hunk = unstaged_file.children[0]
            assert "unstaged.py" in str(unstaged_file.label)
            assert unstaged_hunk.data is not None
            assert unstaged_hunk.data.change_area == "unstaged"

            assert tree.root.is_expanded
            assert tree.styles.overflow_x == "hidden"
            tree.focus()
            await pilot.press("down", "down", "down", "down", "down", "down")
            await pilot.pause()
            title = app.query_one("#selection-title", Static).render()
            assert "Patch: types [notes]" in str(title)
            preview = app.query_one("#diff-view", TextArea)
            patch_diff = preview.text

            await pilot.press("enter")
            await pilot.pause()
            assert tree.cursor_node is patch_node
            assert preview.text == patch_diff

            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is file_node
            assert "example [typing].py" in str(app.query_one("#selection-title", Static).render())
            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is file_node
            assert file_node.is_expanded
            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is hunk_node
            assert "hunk 1" in str(app.query_one("#selection-title", Static).render())
            assert "+after" in preview.text
            assert preview.read_only
            assert not preview.show_line_numbers
            assert preview.language == "diff"
            assert preview.theme == "stg-diff"
            assert any(name == "attribute" for _, _, name in preview._highlights[0])
            assert any(name == "diff.minus" for _, _, name in preview._highlights[1])
            assert any(name == "diff.plus" for _, _, name in preview._highlights[2])

            await pilot.press("left")
            await pilot.pause()
            assert tree.cursor_node is file_node
            assert "example [typing].py" in preview.text
            await pilot.press("left")
            await pilot.pause()
            assert file_node.is_collapsed
            await pilot.press("right")
            await pilot.pause()
            assert file_node.is_expanded
            assert tree.cursor_node is file_node

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
            assert "No unstaged changes." in str(tree.root.children[0].children[0].label)
            assert tree.root.children[1].is_expanded
            assert tree.root.children[2].is_expanded

    asyncio.run(run_app())


def test_enter_stages_or_unstages_selected_files_and_hunks() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            unstaged_file = tree.root.children[0].children[0]
            tree.focus()
            tree.move_cursor(unstaged_file)
            await pilot.press("right", "right")
            assert tree.cursor_node is not None
            assert tree.cursor_node.data is not None
            assert tree.cursor_node.data.hunk_index == 0
            await pilot.press("enter")
            await pilot.pause()

            staged_file = tree.root.children[1].children[0]
            tree.move_cursor(staged_file)
            await pilot.press("enter")
            await pilot.pause()

            assert repository.actions == [
                ("stage_hunk", "unstaged.py", 0),
                ("unstage_file", "staged.py", None),
            ]

    asyncio.run(run_app())


def test_create_patch_dialog_passes_edited_template_to_repository() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#patch-tree", Tree).focus()
            await pilot.press("c")
            await pilot.pause()

            message = app.screen.query_one("#patch-message", TextArea)
            assert message.text == repository.new_patch_template()
            edited_message = "Patch: dialog-created\nAuthor: Test <test@example.invalid>\n"
            message.load_text(edited_message)
            await pilot.click("#create-patch")
            await pilot.pause()

            assert repository.created_messages == [edited_message]
            assert not list(app.query("#patch-message"))

    asyncio.run(run_app())


def test_create_patch_dialog_can_be_cancelled() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#patch-tree", Tree).focus()
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert repository.created_messages == []
            assert not list(app.query("#patch-message"))
            assert "cancelled" in str(app.query_one("#status", Static).render()).lower()

    asyncio.run(run_app())


def test_edit_selected_patch_opens_template_and_updates_patch_message() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("e")
            await pilot.pause()

            dialog = app.screen
            message = dialog.query_one("#patch-message", TextArea)
            assert message.text == repository.edit_patch_template("types [notes]")
            edited_message = "Patch: types [notes]\n\nEdited patch body\n"
            message.load_text(edited_message)
            await pilot.click("#create-patch")
            await pilot.pause()

            assert repository.edited_patches == [("types [notes]", edited_message)]

    asyncio.run(run_app())


def test_edit_patch_is_ignored_when_a_file_is_selected_and_cancel_works() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            file_node = tree.root.children[2].children[0].children[0]
            tree.move_cursor(file_node)
            await pilot.press("e")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]

            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("e")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert repository.edited_patches == []
            assert "cancelled" in str(app.query_one("#status", Static).render()).lower()

    asyncio.run(run_app())


def test_ctrl_down_moves_only_a_staged_file_to_the_current_patch() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            staged_file = tree.root.children[1].children[0]
            tree.move_cursor(staged_file)
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert repository.moved_files == ["staged.py"]

            staged_file = tree.root.children[1].children[0]
            staged_file.expand()
            staged_hunk = staged_file.children[0]
            tree.move_cursor(staged_hunk)
            await pilot.press("ctrl+down")
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("ctrl+down")
            assert repository.moved_files == ["staged.py"]

    asyncio.run(run_app())


def test_order_toggle_reverses_sections_and_patch_sequence() -> None:
    class MultiplePatchRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return tuple(
                Patch(name=name, status=PatchStatus.APPLIED, description="", files=())
                for name in ("oldest", "middle", "newest")
            )

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        app = StgLayersApp(MultiplePatchRepository())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            assert [str(node.label) for node in tree.root.children] == [
                "Unstaged changes",
                "Staged changes",
                "Patch stack",
            ]
            patch_stack = tree.root.children[2]
            assert all(node.data is not None for node in patch_stack.children)
            assert [node.data.patch_name for node in patch_stack.children if node.data] == [
                "newest",
                "middle",
                "oldest",
            ]

            await pilot.press("R")
            await pilot.pause()
            assert [str(node.label) for node in tree.root.children] == [
                "Patch stack",
                "Staged changes",
                "Unstaged changes",
            ]
            patch_stack = tree.root.children[0]
            assert all(node.data is not None for node in patch_stack.children)
            assert [node.data.patch_name for node in patch_stack.children if node.data] == [
                "oldest",
                "middle",
                "newest",
            ]
            assert "oldest first" in str(app.query_one("#status", Static).render())

            await pilot.press("shift+r")
            await pilot.pause()
            assert [str(node.label) for node in tree.root.children] == [
                "Unstaged changes",
                "Staged changes",
                "Patch stack",
            ]
            assert "newest first" in str(app.query_one("#status", Static).render())

    asyncio.run(run_app())


def test_patch_labels_use_compact_ids_titles_and_status_markers() -> None:
    class PatchStatusRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return tuple(
                Patch(name=name, status=status, description="Patch title", files=())
                for name, status in (
                    ("current", PatchStatus.CURRENT),
                    ("applied", PatchStatus.APPLIED),
                    ("unapplied", PatchStatus.UNAPPLIED),
                    ("hidden", PatchStatus.HIDDEN),
                )
            )

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        app = StgLayersApp(PatchStatusRepository())
        async with app.run_test():
            tree = app.query_one("#patch-tree", Tree)
            patch_stack = tree.root.children[2]
            assert [str(node.label) for node in patch_stack.children] == [
                "[~hidden] Patch title",
                "[-unapplied] Patch title",
                "[+applied] Patch title",
                "[>current] Patch title",
            ]
            assert [
                str(node.label.spans[0].style)
                for node in patch_stack.children
                if isinstance(node.label, Text)
            ] == ["dim magenta", "yellow", "cyan", "bold green"]

    asyncio.run(run_app())
