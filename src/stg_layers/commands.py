import subprocess
from pathlib import Path

from stg_layers.errors import CommandError, RepositoryError


class CommandRunner:
    """Run commands in one repository, preserving stdout and failure details."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run(
        self,
        executable: str,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (),
        input_text: str | None = None,
    ) -> str:
        command = (executable, *args)
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                input=input_text,
            )
        except FileNotFoundError as error:
            raise RepositoryError(f"{executable} was not found on PATH.") from error
        except OSError as error:
            raise RepositoryError(f"Could not run {executable}: {error}") from error
        if result.returncode and result.returncode not in allowed_returncodes:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CommandError(command, result.returncode, detail)
        return result.stdout
