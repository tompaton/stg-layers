from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from stg_layers.models import PatchStatus


@dataclass(frozen=True, slots=True)
class ViewSettings:
    newest_first: bool = True
    word_wrap: bool = False
    show_hidden: bool = True
    show_unapplied: bool = True

    @property
    def omitted_patch_statuses(self) -> tuple[PatchStatus, ...]:
        return tuple(
            status
            for status, show in (
                (PatchStatus.HIDDEN, self.show_hidden),
                (PatchStatus.UNAPPLIED, self.show_unapplied),
            )
            if not show
        )

    @classmethod
    def load(cls, path: Path) -> ViewSettings:
        try:
            with path.open("rb") as config:
                view = tomllib.load(config).get("view")
        except OSError, UnicodeError, tomllib.TOMLDecodeError:
            return cls()
        if not isinstance(view, dict):
            return cls()
        return cls(**{key: view[key] for key in asdict(cls()) if isinstance(view.get(key), bool)})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = "[view]\n" + "".join(
            f"{key} = {str(value).lower()}\n" for key, value in asdict(self).items()
        )
        with TemporaryDirectory(dir=path.parent) as directory:
            temporary_path = Path(directory) / path.name
            temporary_path.write_text(contents, encoding="utf-8")
            temporary_path.replace(path)
