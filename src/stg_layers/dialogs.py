from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, OptionList, RadioButton, RadioSet, Static, TextArea

from stg_layers.change_moves import MoveTarget
from stg_layers.models import Patch, PatchStatus, TreeItem
from stg_layers.settings import ViewSettings


class ViewSettingsDialog(ModalScreen[ViewSettings | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    ViewSettingsDialog { align: center middle; }
    #view-settings-dialog {
        width: 48;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #view-settings-title { text-style: bold; }
    #view-settings-fields { height: auto; max-height: 45vh; margin: 1 0; }
    #display-order { width: 100%; height: auto; }
    #view-settings-actions { height: auto; align-horizontal: right; }
    """

    def __init__(self, settings: ViewSettings) -> None:
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        with Vertical(id="view-settings-dialog"):
            yield Static("View Settings", id="view-settings-title")
            with VerticalScroll(id="view-settings-fields"):
                yield Static("Display order")
                yield RadioSet(
                    RadioButton(
                        "Newest first", value=self.settings.newest_first, id="newest-first"
                    ),
                    RadioButton(
                        "Oldest first", value=not self.settings.newest_first, id="oldest-first"
                    ),
                    id="display-order",
                )
                yield Checkbox(
                    "Show Hidden patches",
                    value=self.settings.show_hidden,
                    id="show-hidden",
                    compact=True,
                )
                yield Checkbox(
                    "Show Unapplied patches",
                    value=self.settings.show_unapplied,
                    id="show-unapplied",
                    compact=True,
                )
                yield Checkbox(
                    "Wrap lines in diff preview",
                    value=self.settings.word_wrap,
                    id="diff-word-wrap",
                    compact=True,
                )
            with Horizontal(id="view-settings-actions"):
                yield Button("Cancel", id="cancel-view-settings")
                yield Button("Apply", id="apply-view-settings", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#display-order", RadioSet).focus()

    @on(Button.Pressed, "#cancel-view-settings")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#apply-view-settings")
    def apply_settings(self) -> None:
        newest_first = self.query_one("#newest-first", RadioButton).value
        word_wrap = self.query_one("#diff-word-wrap", Checkbox).value
        self.dismiss(
            ViewSettings(
                newest_first=newest_first,
                word_wrap=word_wrap,
                show_hidden=self.query_one("#show-hidden", Checkbox).value,
                show_unapplied=self.query_one("#show-unapplied", Checkbox).value,
            )
        )


class PatchMessageDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    PatchMessageDialog {
        align: center middle;
    }

    #patch-dialog {
        width: 80%;
        height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #patch-message {
        height: 1fr;
    }

    #patch-dialog-actions {
        height: auto;
        align-horizontal: right;
    }
    """

    def __init__(self, message: str, *, submit_label: str = "Create") -> None:
        super().__init__()
        self.message = message
        self.submit_label = submit_label

    def compose(self) -> ComposeResult:
        with Vertical(id="patch-dialog"):
            yield Static(f"Edit the patch message, then select {self.submit_label}.")
            yield TextArea(self.message, id="patch-message", soft_wrap=True)
            with Horizontal(id="patch-dialog-actions"):
                yield Button("Cancel", id="cancel-patch")
                yield Button(self.submit_label, id="create-patch", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#patch-message", TextArea).focus()

    @on(Button.Pressed, "#create-patch")
    def create_patch(self) -> None:
        self.dismiss(self.query_one("#patch-message", TextArea).text)

    @on(Button.Pressed, "#cancel-patch")
    def cancel_patch(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DeletePatchDialog(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    DeletePatchDialog { align: center middle; }
    #delete-dialog {
        width: 90;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    #delete-actions { height: auto; align-horizontal: right; }
    #delete-stat { height: auto; max-height: 50vh; margin: 1 0; }
    #delete-stat Static { width: auto; min-width: 100%; }
    """

    def __init__(self, patch_name: str, *, diffstat: str = "") -> None:
        super().__init__()
        self.patch_name = patch_name
        self.diffstat = diffstat

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(Text(f"Delete patch {self.patch_name}?"))
            with VerticalScroll(id="delete-stat"):
                yield Static(Text(self.diffstat or "No changes.", no_wrap=True))
            with Horizontal(id="delete-actions"):
                yield Button("Cancel", id="cancel-delete")
                yield Button("OK", id="confirm-delete", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel-delete", Button).focus()

    @on(Button.Pressed, "#cancel-delete")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-delete")
    def confirm(self) -> None:
        self.dismiss(True)


class DiscardChangesDialog(DeletePatchDialog):
    def __init__(
        self, patch_name: str, *, diffstat: str = "", source: str | None = "unstaged"
    ) -> None:
        super().__init__(patch_name, diffstat=diffstat)
        self.source = source or "unstaged"

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(Text(f"Discard {self.source} changes in {self.patch_name}?"))
            yield Static("This cannot be undone. Other changes will be kept.")
            with VerticalScroll(id="delete-stat"):
                yield Static(Text(self.diffstat or "No changes.", no_wrap=True))
            with Horizontal(id="delete-actions"):
                yield Button("Cancel", id="cancel-delete")
                yield Button("Discard", id="confirm-delete", variant="error")


class ConflictDialog(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "quit", "Quit", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]
    DEFAULT_CSS = """
    ConflictDialog {
        align: center middle;
    }

    #conflict-dialog {
        width: 72;
        height: auto;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }

    #conflict-description {
        height: auto;
        padding-bottom: 1;
    }

    #conflict-actions {
        height: auto;
        align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        operation_failed: bool,
        stash_reference: str | None = None,
        stash_restore_conflict: bool = False,
    ) -> None:
        super().__init__()
        self.operation_failed = operation_failed
        self.stash_reference = stash_reference
        self.stash_restore_conflict = stash_restore_conflict

    def compose(self) -> ComposeResult:
        if self.stash_restore_conflict:
            description = (
                "Restoring your saved local changes conflicted. Undo rolls back the stack "
                "operation when applicable and retries restoring those changes. Resolve the "
                "conflicts in another terminal, then choose Refresh."
            )
        elif self.operation_failed:
            description = (
                "The patch stack operation caused merge conflicts. Undo uses "
                "`stg undo --hard` to restore the previous stack and discard the conflicted state. "
                "Choose Refresh after resolving the conflict in another terminal."
            )
        else:
            description = (
                "This repository has unresolved merge conflicts. StGit Layers is locked "
                "until they are resolved in another terminal. Refresh to check again, "
                "or quit to return to the terminal."
            )
        if self.stash_reference is not None:
            description += (
                f" Your local changes are safely saved in Git stash "
                f"{self.stash_reference}; Quit leaves that stash intact."
            )
        with Vertical(id="conflict-dialog"):
            yield Static("Repository conflict", classes="error")
            yield Static(description, id="conflict-description")
            with Horizontal(id="conflict-actions"):
                if self.operation_failed:
                    yield Button("Undo", id="undo-conflict", variant="warning")
                yield Button("Quit", id="quit-conflict")
                yield Button("Refresh", id="refresh-conflict", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#refresh-conflict", Button).focus()

    @on(Button.Pressed, "#undo-conflict")
    def undo_reorder(self) -> None:
        self.dismiss("undo")

    @on(Button.Pressed, "#refresh-conflict")
    def refresh_repository(self) -> None:
        self.dismiss("refresh")

    @on(Button.Pressed, "#quit-conflict")
    def quit_to_terminal(self) -> None:
        self.dismiss("quit")

    def action_quit(self) -> None:
        self.dismiss("quit")


class MoveTargetDialog(ModalScreen[MoveTarget | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    MoveTargetDialog { align: center middle; }
    #move-dialog {
        width: 80;
        max-width: 95%;
        height: auto;
        max-height: 85%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #move-targets { height: auto; max-height: 60vh; margin: 1 0; }
    #move-actions { height: auto; align-horizontal: right; }
    """

    def __init__(
        self, description: str, targets: tuple[MoveTarget, ...], *, patches: tuple[Patch, ...] = ()
    ) -> None:
        super().__init__()
        self.description = description
        self.targets = targets
        self.patch_labels = {patch.name: TreeItem.for_patch(patch)[1] for patch in patches}
        self.default_target = next(
            (
                MoveTarget(patch_name=patch.name)
                for patch in patches
                if patch.status == PatchStatus.CURRENT
            ),
            None,
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            yield Static(Text(f"Move {self.description} to:"))
            yield OptionList(
                *(
                    self.patch_labels.get(target.patch_name or "", Text(target.label))
                    for target in self.targets
                ),
                id="move-targets",
            )
            with Horizontal(id="move-actions"):
                yield Button("Cancel", id="cancel-move")
                yield Button(
                    "Move", id="confirm-move", variant="primary", disabled=not self.targets
                )

    def on_mount(self) -> None:
        options = self.query_one("#move-targets", OptionList)
        options.highlighted = 0 if self.targets else None
        if self.default_target in self.targets:
            options.highlighted = self.targets.index(self.default_target)
        options.focus()

    @on(OptionList.OptionSelected, "#move-targets")
    def select_target(self) -> None:
        self.query_one("#confirm-move", Button).focus()

    @on(Button.Pressed, "#cancel-move")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-move")
    def confirm(self) -> None:
        index = self.query_one("#move-targets", OptionList).highlighted
        if index is not None:
            self.dismiss(self.targets[index])
