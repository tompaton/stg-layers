from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


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

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="patch-dialog"):
            yield Static("Edit the patch message, then select Create.")
            yield TextArea(self.message, id="patch-message", soft_wrap=True)
            with Horizontal(id="patch-dialog-actions"):
                yield Button("Cancel", id="cancel-patch")
                yield Button("Create", id="create-patch", variant="primary")

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
        width: 64;
        height: auto;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    #delete-actions { height: auto; align-horizontal: right; }
    """

    def __init__(self, patch_name: str) -> None:
        super().__init__()
        self.patch_name = patch_name

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(Text(f"Delete patch {self.patch_name}?"))
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
    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(Text(f"Discard unstaged changes in {self.patch_name}?"))
            yield Static("This cannot be undone. Staged changes will be kept.")
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
