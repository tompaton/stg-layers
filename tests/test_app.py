import asyncio
from dataclasses import replace

from rich.text import Text
from textual.widgets import Static, TextArea, Tree

from stg_layers.app import ConflictDialog, StgLayersApp
from stg_layers.models import DiffHunk, FileChange, FileStatus, Patch, PatchStatus
from stg_layers.repository import PatchMoveResult, RepositoryError


class FixtureRepository:
    branch: str | None = "feature/read-browser"

    def __init__(self) -> None:
        self.actions: list[tuple[str, str, int | None]] = []
        self.created_messages: list[str] = []
        self.edited_patches: list[tuple[str, str]] = []
        self.moved_files: list[str] = []
        self.spilled_files: list[str] = []
        self.patch_moves: list[tuple[str, bool]] = []
        self.patch_pushes: list[str] = []
        self.patch_pops: list[str] = []
        self.undo_count = 0
        self.conflicted = False
        self.conflict_on_older_move = False
        self.conflict_on_push = False

    def has_conflicts(self) -> bool:
        return self.conflicted

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

    def spill_current_patch_file(self, path: str) -> None:
        self.spilled_files.append(path)

    def move_patch_one_step(self, patch_name: str, *, toward_newer: bool) -> PatchMoveResult:
        self.patch_moves.append((patch_name, toward_newer))
        if self.conflict_on_older_move and not toward_newer:
            self.conflicted = True
            raise RepositoryError("Merge conflict")
        return PatchMoveResult.MOVED

    def push_patch(self, patch_name: str) -> None:
        self.patch_pushes.append(patch_name)
        if self.conflict_on_push:
            self.conflicted = True
            raise RepositoryError("Merge conflict")

    def pop_patch(self, patch_name: str) -> None:
        self.patch_pops.append(patch_name)

    def undo_stack_operation(self) -> None:
        self.undo_count += 1
        self.conflicted = False


class MutableFixtureRepository(FixtureRepository):
    def __init__(
        self,
        *,
        staged: tuple[FileChange, ...] = (),
        unstaged: tuple[FileChange, ...] = (),
    ) -> None:
        super().__init__()
        self.staged = list(staged)
        self.unstaged = list(unstaged)
        self.patch_files = list(super().load_stack()[0].files)

    def load_stack(self) -> tuple[Patch, ...]:
        patch = super().load_stack()[0]
        return (replace(patch, files=tuple(self.patch_files)),)

    def load_staged_changes(self) -> tuple[FileChange, ...]:
        return tuple(self.staged)

    def load_unstaged_changes(self) -> tuple[FileChange, ...]:
        return tuple(self.unstaged)

    @staticmethod
    def _file_with_hunks(path: str, hunks: tuple[DiffHunk, ...]) -> FileChange:
        base = FixtureRepository()._file_change(path, "old", "new")
        indexed_hunks = tuple(replace(hunk, index=index) for index, hunk in enumerate(hunks))
        diff_text = f"diff --git a/{path} b/{path}\n"
        diff_text += "".join(hunk.diff_text for hunk in indexed_hunks)
        return replace(base, diff_text=diff_text, hunks=indexed_hunks)

    def _move_file(
        self,
        source: list[FileChange],
        target: list[FileChange],
        path: str,
    ) -> None:
        file_index = next(index for index, file in enumerate(source) if file.path == path)
        target.append(source.pop(file_index))

    def _move_hunk(
        self,
        source: list[FileChange],
        target: list[FileChange],
        path: str,
        hunk_index: int,
    ) -> None:
        file_index = next(index for index, file in enumerate(source) if file.path == path)
        source_file = source[file_index]
        moved_hunk = source_file.hunks[hunk_index]
        remaining_hunks = tuple(
            hunk for index, hunk in enumerate(source_file.hunks) if index != hunk_index
        )
        if remaining_hunks:
            source[file_index] = self._file_with_hunks(path, remaining_hunks)
        else:
            source.pop(file_index)

        target_index = next(
            (index for index, file in enumerate(target) if file.path == path),
            None,
        )
        target_hunks = target[target_index].hunks if target_index is not None else ()
        target_file = self._file_with_hunks(path, (*target_hunks, moved_hunk))
        if target_index is None:
            target.append(target_file)
        else:
            target[target_index] = target_file

    def stage_file(self, path: str) -> None:
        super().stage_file(path)
        self._move_file(self.unstaged, self.staged, path)

    def unstage_file(self, path: str) -> None:
        super().unstage_file(path)
        self._move_file(self.staged, self.unstaged, path)

    def stage_hunk(self, path: str, hunk_index: int) -> None:
        super().stage_hunk(path, hunk_index)
        self._move_hunk(self.unstaged, self.staged, path, hunk_index)

    def unstage_hunk(self, path: str, hunk_index: int) -> None:
        super().unstage_hunk(path, hunk_index)
        self._move_hunk(self.staged, self.unstaged, path, hunk_index)

    def refresh_staged_file_to_current_patch(self, path: str) -> None:
        super().refresh_staged_file_to_current_patch(path)
        self._move_file(self.staged, self.patch_files, path)

    def spill_current_patch_file(self, path: str) -> None:
        super().spill_current_patch_file(path)
        self._move_file(self.patch_files, self.staged, path)


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


def test_enter_advances_through_files_in_the_source_group() -> None:
    async def run_app() -> None:
        repository = MutableFixtureRepository(
            unstaged=(
                FixtureRepository()._file_change("first.py", "old first", "new first"),
                FixtureRepository()._file_change("second.py", "old second", "new second"),
            )
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            first_file = tree.root.children[0].children[0]
            tree.focus()
            tree.move_cursor(first_file)
            await pilot.press("enter")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.file_path == "second.py"
            await pilot.press("enter")
            await pilot.pause()

            assert repository.actions == [
                ("stage_file", "first.py", None),
                ("stage_file", "second.py", None),
            ]
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.title == "Unstaged changes"

    asyncio.run(run_app())


def test_enter_advances_while_unstaging_files() -> None:
    async def run_app() -> None:
        repository = MutableFixtureRepository(
            staged=(
                FixtureRepository()._file_change("first.py", "old first", "new first"),
                FixtureRepository()._file_change("second.py", "old second", "new second"),
            )
        )
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            first_file = tree.root.children[1].children[0]
            tree.move_cursor(first_file)
            tree.focus()
            await pilot.press("enter")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.file_path == "second.py"
            await pilot.press("enter")
            await pilot.pause()

            assert repository.actions == [
                ("unstage_file", "first.py", None),
                ("unstage_file", "second.py", None),
            ]
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.title == "Staged changes"

    asyncio.run(run_app())


def test_staging_hunk_refreshes_preview_and_preserves_expanded_file() -> None:
    async def run_app() -> None:
        first_hunk = DiffHunk(
            index=0,
            header="@@ -1 +1 @@ first",
            diff_text="@@ -1 +1 @@ first\n-old first\n+new first\n",
            source_start=1,
            source_length=1,
            target_start=1,
            target_length=1,
        )
        second_hunk = DiffHunk(
            index=1,
            header="@@ -10 +10 @@ second",
            diff_text="@@ -10 +10 @@ second\n-old second\n+new second\n",
            source_start=10,
            source_length=1,
            target_start=10,
            target_length=1,
        )
        change = MutableFixtureRepository._file_with_hunks(
            "multi.txt",
            (first_hunk, second_hunk),
        )
        repository = MutableFixtureRepository(unstaged=(change,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            file_node = tree.root.children[0].children[0]
            tree.move_cursor(file_node)
            tree.focus()
            await pilot.press("right", "right")
            await pilot.press("enter")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "unstaged"
            assert selected.data.hunk_index == 0
            assert selected.data.hunk_key == StgLayersApp._hunk_key(second_hunk.diff_text)
            unstaged_file = tree.root.children[0].children[0]
            assert unstaged_file.is_expanded
            assert "+new second" in app.query_one("#diff-view", TextArea).text
            assert "Unstaged changes / multi.txt / hunk 1" in str(
                app.query_one("#selection-title", Static).render()
            )

            await pilot.press("enter")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.title == "Unstaged changes"
            assert repository.actions == [
                ("stage_hunk", "multi.txt", 0),
                ("stage_hunk", "multi.txt", 0),
            ]

    asyncio.run(run_app())


def test_ctrl_down_moves_file_from_unstaged_to_staged_then_current_patch() -> None:
    async def run_app() -> None:
        change = FixtureRepository()._file_change("moving.py", "old", "new")
        repository = MutableFixtureRepository(unstaged=(change,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.move_cursor(tree.root.children[0].children[0])
            tree.focus()
            await pilot.press("ctrl+down")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "staged"
            assert selected.data.file_path == "moving.py"
            await pilot.press("ctrl+down")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.patch_name == "types [notes]"
            assert selected.data.file_path == "moving.py"
            assert repository.moved_files == ["moving.py"]

    asyncio.run(run_app())


def test_ctrl_up_and_down_move_hunks_between_changes_and_follow_selection() -> None:
    async def run_app() -> None:
        hunk = DiffHunk(
            index=0,
            header="@@ -1 +1 @@",
            diff_text="@@ -1 +1 @@\n-old\n+new\n",
            source_start=1,
            source_length=1,
            target_start=1,
            target_length=1,
        )
        change = MutableFixtureRepository._file_with_hunks("move.txt", (hunk,))
        repository = MutableFixtureRepository(unstaged=(change,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            unstaged_file = tree.root.children[0].children[0]
            tree.move_cursor(unstaged_file)
            tree.focus()
            await pilot.press("right", "right")
            await pilot.press("ctrl+down")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "staged"
            assert selected.data.file_path == "move.txt"
            assert selected.data.hunk_index == 0
            assert "+new" in app.query_one("#diff-view", TextArea).text

            await pilot.press("ctrl+up")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "unstaged"
            assert selected.data.file_path == "move.txt"
            assert selected.data.hunk_index == 0
            assert "+new" in app.query_one("#diff-view", TextArea).text

    asyncio.run(run_app())


def test_ctrl_shortcuts_follow_reversed_tree_direction() -> None:
    async def run_app() -> None:
        moving_file = FixtureRepository()._file_change("moving.py", "old", "new")
        repository = MutableFixtureRepository(staged=(moving_file,))
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()

            tree = app.query_one("#patch-tree", Tree)
            staged_file = tree.root.children[1].children[0]
            tree.move_cursor(staged_file)
            tree.focus()
            await pilot.press("ctrl+up")
            await pilot.pause()

            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.patch_name == "types [notes]"
            assert selected.data.file_path == "moving.py"
            assert repository.moved_files == ["moving.py"]

            await pilot.press("ctrl+down")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "staged"
            assert selected.data.file_path == "moving.py"
            assert repository.spilled_files == ["moving.py"]

            await pilot.press("ctrl+down")
            await pilot.pause()
            selected = tree.cursor_node
            assert selected is not None and selected.data is not None
            assert selected.data.change_area == "unstaged"
            assert selected.data.file_path == "moving.py"
            assert repository.actions[-1] == ("unstage_file", "moving.py", None)

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


def test_ctrl_up_spills_only_a_current_patch_file() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            current_patch_file = tree.root.children[2].children[0].children[0]
            tree.move_cursor(current_patch_file)
            await pilot.press("ctrl+up")
            await pilot.pause()
            assert repository.spilled_files == ["example [typing].py"]

            current_patch_file = tree.root.children[2].children[0].children[0]
            current_patch_file.expand()
            tree.move_cursor(current_patch_file.children[0])
            await pilot.press("ctrl+up")
            staged_file = tree.root.children[1].children[0]
            tree.move_cursor(staged_file)
            await pilot.press("ctrl+up")
            assert repository.spilled_files == ["example [typing].py"]

    asyncio.run(run_app())


def test_ctrl_up_and_down_move_selected_patch_one_step_in_each_order() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            tree.focus()
            await pilot.press("ctrl+up")
            await pilot.pause()
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert repository.patch_moves == [
                ("types [notes]", True),
                ("types [notes]", False),
            ]

            await pilot.press("R")
            await pilot.pause()
            patch_node = tree.root.children[0].children[0]
            tree.move_cursor(patch_node)
            await pilot.press("ctrl+up")
            await pilot.pause()
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert repository.patch_moves == [
                ("types [notes]", True),
                ("types [notes]", False),
                ("types [notes]", False),
                ("types [notes]", True),
            ]

    asyncio.run(run_app())


def test_conflicting_patch_reorder_offers_undo() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        repository.conflict_on_older_move = True
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            patch_node = tree.root.children[2].children[0]
            tree.move_cursor(patch_node)
            tree.focus()
            await pilot.press("ctrl+down")
            await pilot.pause()

            assert app.screen.query_one("#undo-conflict")
            await pilot.click("#undo-conflict")
            await pilot.pause()
            assert repository.undo_count == 1
            assert not repository.conflicted
            assert app.screen is app.screen_stack[0]

    asyncio.run(run_app())


def test_push_and_pop_selected_patches_with_p_and_shift_p() -> None:
    class PatchOperationRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return (
                Patch("older", PatchStatus.APPLIED, "", ()),
                Patch("current", PatchStatus.CURRENT, "", ()),
                Patch("queued", PatchStatus.UNAPPLIED, "", ()),
            )

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        repository = PatchOperationRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            nodes = {
                node.data.patch_name: node
                for node in tree.root.children[2].children
                if node.data is not None
            }
            tree.move_cursor(nodes["queued"])
            tree.focus()
            await pilot.press("p")
            await pilot.pause()
            assert repository.patch_pushes == ["queued"]

            nodes = {
                node.data.patch_name: node
                for node in tree.root.children[2].children
                if node.data is not None
            }
            tree.move_cursor(nodes["current"])
            await pilot.press("shift+p")
            await pilot.pause()
            assert repository.patch_pops == ["current"]

    asyncio.run(run_app())


def test_conflicting_patch_push_offers_undo() -> None:
    class PatchOperationRepository(FixtureRepository):
        def load_stack(self) -> tuple[Patch, ...]:
            return (Patch("queued", PatchStatus.UNAPPLIED, "", ()),)

        def load_staged_changes(self) -> tuple[FileChange, ...]:
            return ()

        def load_unstaged_changes(self) -> tuple[FileChange, ...]:
            return ()

    async def run_app() -> None:
        repository = PatchOperationRepository()
        repository.conflict_on_push = True
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.move_cursor(tree.root.children[2].children[0])
            tree.focus()
            await pilot.press("p")
            await pilot.pause()

            assert app.screen.query_one("#undo-conflict")
            await pilot.click("#undo-conflict")
            await pilot.pause()
            assert repository.undo_count == 1
            assert not repository.conflicted

    asyncio.run(run_app())


def test_repository_conflict_locks_actions_until_refresh() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        repository.conflicted = True
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#refresh-conflict")
            await pilot.press("enter")
            await pilot.pause()
            assert repository.actions == []

            repository.conflicted = False
            await pilot.click("#refresh-conflict")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert app._loaded_data is not None

    asyncio.run(run_app())


def test_repository_conflict_detected_before_mutating_action() -> None:
    async def run_app() -> None:
        repository = FixtureRepository()
        app = StgLayersApp(repository)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#patch-tree", Tree)
            tree.move_cursor(tree.root.children[0].children[0])
            tree.focus()
            repository.conflicted = True
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ConflictDialog)
            assert app.screen.query_one("#refresh-conflict")
            assert repository.actions == []
            repository.conflicted = False
            await pilot.click("#refresh-conflict")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]

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
