from rich.style import Style
from textual.binding import Binding
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme
from tree_sitter import Language
from tree_sitter_diff import HIGHLIGHTS_QUERY
from tree_sitter_diff import language as diff_language

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
        Binding("n", "next_hunk", "Next hunk", priority=True),
        Binding("p", "previous_hunk", "Previous hunk", priority=True),
        Binding("enter", "apply_change", "Stage/unstage", priority=True),
        Binding("w", "toggle_word_wrap", "Word wrap", priority=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(
            "",
            read_only=True,
            show_cursor=False,
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

    def action_next_hunk(self) -> None:
        self._navigate_hunk(forward=True)

    def action_previous_hunk(self) -> None:
        self._navigate_hunk(forward=False)

    async def action_apply_change(self) -> None:
        await self.app.run_action("apply_preview_change")

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
