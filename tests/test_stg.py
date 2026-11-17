import pytest

from stg_layers.errors import RepositoryError
from stg_layers.models import PatchStatus
from stg_layers.stg import parse_series


def test_parse_series_records_patch_state_and_description() -> None:
    output = "> current # current patch\n+ earlier # applied patch\n- next # queued patch\n! hidden"

    assert parse_series(output) == [
        ("current", PatchStatus.CURRENT, "current patch"),
        ("earlier", PatchStatus.APPLIED, "applied patch"),
        ("next", PatchStatus.UNAPPLIED, "queued patch"),
        ("hidden", PatchStatus.HIDDEN, ""),
    ]


def test_parse_series_rejects_unknown_status() -> None:
    with pytest.raises(RepositoryError, match="Unrecognized output"):
        parse_series("? patch")
