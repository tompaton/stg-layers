class RepositoryError(RuntimeError):
    pass


class CommandError(RepositoryError):
    def __init__(self, command: tuple[str, ...], returncode: int, detail: str) -> None:
        self.command = command
        self.returncode = returncode
        self.detail = detail
        message = f"Command {' '.join(command)!r} failed (exit {returncode})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class DiffError(RepositoryError):
    pass
