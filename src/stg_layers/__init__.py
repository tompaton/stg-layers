import sys
from pathlib import Path

from setproctitle import setproctitle

from stg_layers.app import StgLayersApp
from stg_layers.errors import RepositoryError
from stg_layers.repository import StGitRepository


def main() -> None:
    try:
        repository = StGitRepository.discover(Path.cwd())
    except RepositoryError as error:
        print(f"stg-layers: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    setproctitle("stg-layers")
    StgLayersApp(repository).run()


__all__ = ["main"]
