from rich.style import Style
from textual.binding import Binding
from textual.widgets import TextArea
from textual.widgets.text_area import Selection, TextAreaTheme
from tree_sitter import Language
from tree_sitter_diff import HIGHLIGHTS_QUERY
from tree_sitter_diff import language as diff_language

from stg_layers.errors import RepositoryError
from stg_layers.git import parse_diff
from stg_layers.models import TreeItem
from stg_layers.partial_diff import HunkLineSelection
from stg_layers.tree_view import LayerTree
from stg_layers.tree_walker import walk_nodes

_DIFF_THEME = TextAreaTheme(
    name="stg-diff",
    syntax_styles={
        "diff.plus": Style(color="#98c379"),
        "diff.minus": Style(color="#e06c75"),
        "diff.delta": Style(color="#e5c07b", bold=True),
        "label": Style(color="#61afef"),
        "attribute": Style(color="#c678dd"),
    },
)


class DiffPreview(TextArea):
    BINDINGS = [
        Binding("m", "choose_move_target", "Move to…", priority=True),
        Binding("n", "next_hunk", "Next hunk", priority=True),
        Binding("p", "previous_hunk", "Previous hunk", priority=True),
        Binding("enter", "apply_change", "Stage/unstage", priority=True),
        Binding("w", "toggle_word_wrap", "Word wrap", priority=True),
        Binding("s", "stage_selected_lines", "Stage lines", priority=True),
        Binding("delete", "discard_preview_lines", "Discard lines", priority=True),
        Binding("d", "remove_selected_lines", "Unstage/discard lines", priority=True),
        Binding("shift+up,shift+left", "extend_line_selection(False)", show=False, priority=True),
        Binding("shift+down,shift+right", "extend_line_selection(True)", show=False, priority=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(
            "",
            read_only=True,
            show_cursor=True,
            show_line_numbers=False,
            soft_wrap=False,
            compact=True,
            id=id,
        )
        if HIGHLIGHTS_QUERY is None:
            raise RuntimeError("The Tree-sitter diff grammar has no highlight query.")
        self.register_language("diff", Language(diff_language()), HIGHLIGHTS_QUERY)
        self.register_theme(_DIFF_THEME)
        self.language = "diff"
        self.theme = _DIFF_THEME.name

    def _validate_selection(self, selection: Selection) -> Selection:
        selection = super()._validate_selection(selection)
        first, last = sorted(selection)
        start = (first[0], 0)
        end = (last[0], len(self.document[last[0]]))
        return Selection(start, end) if selection.start <= selection.end else Selection(end, start)

    def action_extend_line_selection(self, forward: bool) -> None:
        anchor, cursor = self.selection
        row = max(0, min(self.document.line_count - 1, cursor[0] + (1 if forward else -1)))
        if row >= anchor[0]:
            self.selection = Selection((anchor[0], 0), (row, len(self.document[row])))
        else:
            self.selection = Selection((anchor[0], len(self.document[anchor[0]])), (row, 0))
        self.record_cursor_width()

    def action_cursor_up(self, select: bool = False) -> None:
        if select:
            self.action_extend_line_selection(False)
        else:
            self.move_cursor((max(0, self.cursor_location[0] - 1), 0))

    def action_cursor_down(self, select: bool = False) -> None:
        if select:
            self.action_extend_line_selection(True)
        else:
            self.move_cursor((min(self.document.line_count - 1, self.cursor_location[0] + 1), 0))

    def action_next_hunk(self) -> None:
        self._navigate_hunk(forward=True)

    def action_previous_hunk(self) -> None:
        self._navigate_hunk(forward=False)

    async def action_choose_move_target(self) -> None:
        await self.app.run_action("choose_move_target(True)")

    async def action_apply_change(self) -> None:
        await self.app.run_action("apply_preview_change")

    async def action_stage_selected_lines(self) -> None:
        await self.app.run_action("stage_selected_lines")

    async def action_remove_selected_lines(self) -> None:
        await self.app.run_action("remove_selected_lines")

    async def action_discard_preview_lines(self) -> None:
        await self.app.run_action("discard_preview_lines")

    def selected_lines(self, item: TreeItem) -> HunkLineSelection:
        first, last = sorted(self.selection)
        candidates = (
            [(item.require_file_path(), item.diff_text)]
            if item.is_hunk
            else [
                (file.path, hunk.diff_text)
                for file in parse_diff("preview", item.diff_text)
                for hunk in file.hunks
            ]
        )
        lines = item.diff_text.splitlines()
        offset = 0
        for path, diff in candidates:
            hunk_lines = diff.splitlines()
            start = lines.index(hunk_lines[0], offset)
            offset = start + len(hunk_lines)
            if start <= first[0] <= last[0] < offset:
                selected = hunk_lines[first[0] - start : last[0] - start + 1]
                if not any(line.startswith(("+", "-")) for line in selected):
                    raise RepositoryError("Select added or removed lines within one diff hunk.")
                return HunkLineSelection(path, diff, first[0] - start, last[0] - start)
        raise RepositoryError("Select lines within a single diff hunk.")

    def action_toggle_word_wrap(self) -> None:
        self.soft_wrap = not self.soft_wrap

    def _navigate_hunk(self, *, forward: bool) -> None:
        tree = self.app.query_one("#patch-tree", LayerTree)
        nodes = walk_nodes(tree.root)
        current = tree.cursor_node
        index = nodes.index(current) if current in nodes else (-1 if forward else len(nodes))
        candidates = nodes[index + 1 :] if forward else reversed(nodes[:index])
        selected = next(
            (node for node in candidates if node.data is not None and node.data.is_hunk),
            None,
        )
        if selected is None:
            return
        ancestor = selected.parent
        while ancestor is not None:
            ancestor.expand()
            ancestor = ancestor.parent
        # Rebuild the visible tree before moving to a previously collapsed hunk.
        self.call_after_refresh(tree.restore_selection, selected)
